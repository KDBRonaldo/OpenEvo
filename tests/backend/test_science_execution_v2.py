from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import hashlib
import json
import sqlite3
import threading
import time

import pytest

from openevo.backend.contracts.v2.models import (
    AttemptAppendRequestV2,
    EffectiveExecutionSnapshotRefV2,
    EvolutionRevisionRefV2,
    ProjectHeadRefV2,
    RuntimeContextSnapshotRefV2,
    TaskSubmitRequestV2,
    WorkspaceSnapshotRefV2,
    WorkspaceArchiveDeclarationV2,
    ScienceProjectConfigV2,
    project_config_sha256_for,
)
from openevo.backend.contracts.v2.store import ProjectRecordV2
from openevo.backend.science_execution_v2 import (
    ScienceAttemptCancelledV2,
    ScienceAttemptExecutionEvidenceV2,
    ScienceAttemptExecutionReceiptV2,
    ScienceAttemptExecutionRecordV2,
    canonical_subscription_session_result,
    compile_science_attempt_v2,
    ScienceAttemptExecutorV2,
    science_attempt_execution_receipt_sha256,
    science_session_result_bytes,
    science_session_result_sha256,
)
from openevo.backend.science_run_owner import CoreScienceTaskOwnerV2
from openevo.backend.run_admission import (
    EffectiveExecutionSettings,
    resolve_genesis_execution_snapshot,
)
from openevo.backend.service_supervisor import ServiceExecutionMode, ServiceRunBinding
from openevo.backend.service_supervisor import (
    ServiceGroupSnapshot,
    ServiceRunLease,
    ServiceRunReadinessCode,
)
from openevo.backend.workspace_handoff_v2 import (
    WorkspaceHandoffBindingV2,
    WorkspaceResultReceiptV2,
    WorkspaceHandoffStoreV2,
)
from openevo.evolution.framework import canonical_digest
from openevo.backend.science_run_store import (
    ScienceProjectAdmissionAuthorityV2,
    ScienceTaskConflictV2,
    ScienceTaskPreconditionFailedV2,
    ScienceTaskStoreV2,
    ScienceTaskStoreV2Error,
    ScienceTaskTerminalV2,
)
from openevo.backend.science_successor import ScienceSuccessorPlanV2
from openevo.internal_auth import (
    GenerationBoundRunAdmissionCheck,
    InternalServiceIdentity,
    RunAdmissionOperation,
)
from openevo.projects.science.compiler import MANAGED_RUNTIME_IMAGES
from openevo.rollout.models import SessionResult, TaskRequest, TaskStatus
from openevo.runtime.managed import MANAGED_RUNTIME_RELEASES
from openevo.trajectory.models import Trace, Trajectory
from tests.framework_testkit import verified_builtin_registry
from openevo.backend.workspace_store_v2 import WorkspaceStoreV2


class _Clock:
    def __init__(self) -> None:
        self._next = datetime(2026, 7, 23, 2, tzinfo=timezone.utc)
        self._lock = threading.Lock()

    def __call__(self) -> datetime:
        with self._lock:
            value = self._next
            self._next += timedelta(microseconds=1)
            return value


def _workspace(project_id: str = "project-execution") -> WorkspaceSnapshotRefV2:
    return WorkspaceSnapshotRefV2(
        workspace_snapshot_id="workspace-input",
        project_id=project_id,
        manifest_sha256="1" * 64,
        entry_count=2,
        byte_size=128,
    )


def _head(
    project_id: str = "project-execution",
    *,
    registry_sha256: str = "a" * 64,
    workspace: WorkspaceSnapshotRefV2 | None = None,
    effective_execution: EffectiveExecutionSnapshotRefV2 | None = None,
) -> ProjectHeadRefV2:
    workspace = workspace or _workspace(project_id)
    evolution = EvolutionRevisionRefV2(
        evolution_revision_id="evolution-genesis",
        project_id=project_id,
        manifest_sha256="2" * 64,
        artifact_count=0,
    )
    context = RuntimeContextSnapshotRefV2(
        runtime_context_snapshot_id="runtime-context-genesis",
        project_id=project_id,
        evolution_revision_id=evolution.evolution_revision_id,
        evolution_revision_manifest_sha256=evolution.manifest_sha256,
        registry_sha256=registry_sha256,
        runtime_contract_sha256="b" * 64,
        manifest_sha256="3" * 64,
    )
    execution = effective_execution or EffectiveExecutionSnapshotRefV2(
        effective_execution_snapshot_id="exec-subscription",
        project_id=project_id,
        execution_mode="codex_subscription_transcript",
        capture_mode="transcript",
        token_level_metrics_available=False,
        producer_id="subscription-snapshot-issuer-v1",
        snapshot_sha256="4" * 64,
    )
    return ProjectHeadRefV2(
        project_head_id="project-head-genesis",
        project_id=project_id,
        generation=0,
        predecessor_project_head_id=None,
        workspace_snapshot=workspace,
        evolution_revision=evolution,
        runtime_context_snapshot=context,
        effective_execution_snapshot=execution,
        registry_sha256=registry_sha256,
        manifest_sha256="5" * 64,
    )


def _authority(
    *,
    project_config_sha256: str = "6" * 64,
    normalized_evolution_intent_sha256: str = "7" * 64,
    registry_sha256: str = "a" * 64,
) -> ScienceProjectAdmissionAuthorityV2:
    head = _head(registry_sha256=registry_sha256)
    return ScienceProjectAdmissionAuthorityV2(
        project_id=head.project_id,
        active_project_head=head,
        project_config_sha256=project_config_sha256,
        workspace_snapshot=head.workspace_snapshot,
        normalized_evolution_intent_sha256=normalized_evolution_intent_sha256,
    )


def _admit(
    store: ScienceTaskStoreV2,
    clock: _Clock,
    authority: ScienceProjectAdmissionAuthorityV2 | None = None,
):
    authority = authority or _authority()
    store.publish_project_admission_authority(authority)
    task, replayed = store.submit_task(
        request=TaskSubmitRequestV2(
            project_id=authority.project_id,
            expected_project_admission_etag=authority.project_etag,
            expected_project_head_id=authority.active_project_head.project_head_id,
            expected_project_head_manifest_sha256=(authority.active_project_head.manifest_sha256),
            expected_project_config_sha256=authority.project_config_sha256,
        ),
        idempotency_key="submit-execution",
        now=clock(),
    )
    assert replayed is False
    return task


def _plan(task) -> ScienceSuccessorPlanV2:
    return ScienceSuccessorPlanV2(
        project_id=task.project_id,
        task_id=task.task_id,
        task_admission_id=task.admission.task_admission_id,
        admission_sha256=task.admission.admission_sha256,
        accepted_attempt_id=task.attempts[0].attempt_id,
        predecessor_project_head_id=(task.admission.predecessor_project_head.project_head_id),
        normalized_evolution_intent_sha256=(task.admission.normalized_evolution_intent_sha256),
        enabled_methods=(),
    )


def _workspace_result(task) -> WorkspaceResultReceiptV2:
    attempt = task.attempts[0]
    archive = WorkspaceArchiveDeclarationV2(
        format="openevo_deterministic_tar_v1",
        media_type="application/vnd.openevo.workspace-tar",
        content_sha256="c" * 64,
        byte_size=1024,
        entry_count=0,
        extracted_byte_size=0,
    )
    provisional = WorkspaceResultReceiptV2.model_construct(
        workspace_result_contract_version="2",
        handoff_id="workspace-handoff-1",
        task_id=f"rollout-{attempt.attempt_id}",
        attempt_id=attempt.attempt_id,
        task_admission_id=task.admission.task_admission_id,
        admission_sha256=task.admission.admission_sha256,
        project_id=task.project_id,
        session_id="sk-openevo-session-1",
        input_workspace_snapshot_id=(task.admission.workspace_snapshot.workspace_snapshot_id),
        input_workspace_manifest_sha256=(task.admission.workspace_snapshot.manifest_sha256),
        service_generation_sha256="d" * 64,
        registry_sha256=task.admission.registry_sha256,
        framework_lock_sha256="e" * 64,
        output_archive=archive,
        published_at="2026-07-23T02:00:00.000000Z",
        result_manifest_sha256="0" * 64,
    )
    return WorkspaceResultReceiptV2.model_validate(
        {
            **provisional.model_dump(mode="python"),
            "result_manifest_sha256": hashlib.sha256(
                provisional.canonical_manifest_bytes()
            ).hexdigest(),
        }
    )


def _session_result(task) -> SessionResult:
    attempt = task.attempts[0]
    return canonical_subscription_session_result(
        SessionResult(
            session_id="sk-openevo-session-1",
            task_id=f"rollout-{attempt.attempt_id}",
            status="COMPLETED",
            trajectory=Trajectory(
                status="COMPLETED",
                metadata={
                    "capture_mode": "transcript",
                    "token_level_metrics_available": False,
                },
                traces=[
                    Trace(
                        prompt_messages=[{"role": "user", "content": "Solve it"}],
                        response_messages=[{"role": "assistant", "content": "Done"}],
                        finish_reason="transcript",
                        metadata={
                            "capture_mode": "transcript",
                            "token_level_metrics_available": False,
                            "transcript": "Done",
                        },
                    )
                ],
            ),
            metadata={"policy_version": f"openevo:{task.task_id}:{attempt.attempt_id}"},
            workspace_result=_workspace_result(task),
        )
    )


def _evidence(task, result: SessionResult) -> ScienceAttemptExecutionEvidenceV2:
    attempt = task.attempts[0]
    assert result.workspace_result is not None
    return ScienceAttemptExecutionEvidenceV2(
        task_id=task.task_id,
        attempt_id=attempt.attempt_id,
        rollout_task_id=f"rollout-{attempt.attempt_id}",
        policy_version=f"openevo:{task.task_id}:{attempt.attempt_id}",
        session_id="sk-openevo-session-1",
        session_status="COMPLETED",
        session_result_sha256=science_session_result_sha256(result),
        workspace_handoff_id=result.workspace_result.handoff_id,
        workspace_result_manifest_sha256=(result.workspace_result.result_manifest_sha256),
        workspace_archive_sha256=(result.workspace_result.output_archive.content_sha256),
        workspace_archive_byte_size=result.workspace_result.output_archive.byte_size,
        workspace_entry_count=result.workspace_result.output_archive.entry_count,
        workspace_extracted_byte_size=(result.workspace_result.output_archive.extracted_byte_size),
        capture_mode="transcript",
        token_level_metrics_available=False,
        transcript_record_count=1,
    )


def _receipt(task, evidence) -> ScienceAttemptExecutionReceiptV2:
    attempt = task.attempts[0]
    head = task.admission.predecessor_project_head
    provisional = ScienceAttemptExecutionReceiptV2.model_construct(
        execution_receipt_contract_version="2",
        task_id=task.task_id,
        attempt_id=attempt.attempt_id,
        task_admission_id=task.admission.task_admission_id,
        admission_sha256=task.admission.admission_sha256,
        project_id=task.project_id,
        predecessor_project_head_id=head.project_head_id,
        predecessor_project_head_manifest_sha256=head.manifest_sha256,
        workspace_snapshot_id=task.admission.workspace_snapshot.workspace_snapshot_id,
        workspace_manifest_sha256=task.admission.workspace_snapshot.manifest_sha256,
        evolution_revision_id=head.evolution_revision.evolution_revision_id,
        evolution_revision_manifest_sha256=head.evolution_revision.manifest_sha256,
        runtime_context_snapshot_id=(head.runtime_context_snapshot.runtime_context_snapshot_id),
        runtime_context_manifest_sha256=head.runtime_context_snapshot.manifest_sha256,
        effective_execution_snapshot_id=(
            head.effective_execution_snapshot.effective_execution_snapshot_id
        ),
        effective_execution_snapshot_sha256=(head.effective_execution_snapshot.snapshot_sha256),
        registry_sha256=task.admission.registry_sha256,
        service_generation_sha256="d" * 64,
        framework_lock_sha256="e" * 64,
        runtime_identity_sha256="f" * 64,
        harness_id="codex",
        capture_mode="transcript",
        token_level_metrics_available=False,
        model_ref="gpt-5.5",
        task_network_allow_internet=False,
        rollout_task_id=evidence.rollout_task_id,
        rollout_payload_sha256="0" * 64,
        session_id=evidence.session_id,
        session_result_sha256=evidence.session_result_sha256,
        workspace_handoff_id=evidence.workspace_handoff_id,
        workspace_result_manifest_sha256=(evidence.workspace_result_manifest_sha256),
        terminal_status=evidence.session_status,
        completed_at="2026-07-23T02:00:00.000000Z",
        receipt_sha256="0" * 64,
    )
    return ScienceAttemptExecutionReceiptV2.model_validate(
        {
            **provisional.model_dump(mode="python"),
            "receipt_sha256": science_attempt_execution_receipt_sha256(provisional),
        }
    )


def _terminal_bundle(task):
    result = _session_result(task)
    evidence = _evidence(task, result)
    return _receipt(task, evidence), evidence, _plan(task), result


def _register_rollout_admission(store: ScienceTaskStoreV2, task) -> None:
    attempt = task.attempts[0]
    assert store.register_attempt_run_admission(
        task_id=task.task_id,
        attempt_id=attempt.attempt_id,
        check=GenerationBoundRunAdmissionCheck(
            operation=RunAdmissionOperation.ROLLOUT_TASK_SUBMIT,
            generation_digest="d" * 64,
            registry_digest=task.admission.registry_sha256,
            framework_lock_digest="e" * 64,
            payload_sha256="0" * 64,
            task_id=f"rollout-{attempt.attempt_id}",
            session_id=None,
        ),
        allow_create=True,
    )


def test_attempt_progress_and_verified_terminal_capture_are_durable(tmp_path) -> None:
    clock = _Clock()
    store = ScienceTaskStoreV2(tmp_path / "state")
    task = _admit(store, clock)
    attempt = task.attempts[0]
    try:
        preparing = store.begin_attempt_execution(
            task_id=task.task_id,
            attempt_id=attempt.attempt_id,
            now=clock(),
        )
        _register_rollout_admission(store, task)
        assert preparing.state == "preparing"
        assert store.get_task(task.task_id).state == "preparing"

        running = store.mark_attempt_running(
            task_id=task.task_id,
            attempt_id=attempt.attempt_id,
            now=clock(),
        )
        assert running.state == "running"
        assert store.get_task(task.task_id).state == "running"

        receipt, evidence, plan, result = _terminal_bundle(task)
        captured = store.record_terminal_attempt(
            task_id=task.task_id,
            attempt_id=attempt.attempt_id,
            receipt=receipt,
            evidence=evidence,
            successor_plan=plan,
            terminal_result=result,
            now=clock(),
        )
        assert captured.state == "captured"
        authoritative = store.get_task(task.task_id)
        assert authoritative.state == "waiting_for_successor"
        assert authoritative.authoritative_attempt_id == attempt.attempt_id

        replay = store.record_terminal_attempt(
            task_id=task.task_id,
            attempt_id=attempt.attempt_id,
            receipt=receipt,
            evidence=evidence,
            successor_plan=plan,
            terminal_result=result,
            now=clock(),
        )
        assert replay == captured
        assert store.captured_attempt_ids() == [attempt.attempt_id]
    finally:
        store.close()

    restarted = ScienceTaskStoreV2(tmp_path / "state")
    try:
        recovered = restarted.get_attempt_execution(task.task_id, attempt.attempt_id)
        assert recovered == captured
        assert recovered.receipt == receipt
        assert recovered.evidence == evidence
        assert recovered.successor_plan == plan
        assert (
            restarted.get_captured_session_result(
                task.task_id,
                attempt.attempt_id,
            )
            == result
        )
    finally:
        restarted.close()


def test_terminal_capture_requires_the_exact_preregistered_rollout(tmp_path) -> None:
    clock = _Clock()
    store = ScienceTaskStoreV2(tmp_path / "state")
    task = _admit(store, clock)
    attempt = task.attempts[0]
    store.begin_attempt_execution(
        task_id=task.task_id,
        attempt_id=attempt.attempt_id,
        now=clock(),
    )
    receipt, evidence, plan, result = _terminal_bundle(task)

    with pytest.raises(ScienceTaskPreconditionFailedV2):
        store.record_terminal_attempt(
            task_id=task.task_id,
            attempt_id=attempt.attempt_id,
            receipt=receipt,
            evidence=evidence,
            successor_plan=plan,
            terminal_result=result,
            now=clock(),
        )
    store.close()


def test_recovery_rejects_a_captured_result_rewritten_with_a_new_digest(tmp_path) -> None:
    clock = _Clock()
    root = tmp_path / "state"
    store = ScienceTaskStoreV2(root)
    task = _admit(store, clock)
    attempt = task.attempts[0]
    store.begin_attempt_execution(
        task_id=task.task_id,
        attempt_id=attempt.attempt_id,
        now=clock(),
    )
    _register_rollout_admission(store, task)
    receipt, evidence, plan, result = _terminal_bundle(task)
    store.record_terminal_attempt(
        task_id=task.task_id,
        attempt_id=attempt.attempt_id,
        receipt=receipt,
        evidence=evidence,
        successor_plan=plan,
        terminal_result=result,
        now=clock(),
    )
    store.close()

    changed = result.model_dump(mode="python")
    changed["trajectory"]["traces"][0]["response_messages"] = [
        {"role": "assistant", "content": "Rewritten after capture"}
    ]
    changed_result = canonical_subscription_session_result(SessionResult.model_validate(changed))
    changed_bytes = science_session_result_bytes(changed_result)
    with sqlite3.connect(root / "science-tasks-v2.sqlite3") as connection:
        connection.execute(
            "UPDATE attempt_executions SET session_result_sha256 = ?, "
            "session_result_json = ? WHERE attempt_id = ?",
            (
                hashlib.sha256(changed_bytes).hexdigest(),
                changed_bytes,
                attempt.attempt_id,
            ),
        )
        connection.commit()

    with pytest.raises(ScienceTaskStoreV2Error):
        ScienceTaskStoreV2(root)


def test_cancellation_and_terminal_capture_have_one_atomic_winner(tmp_path) -> None:
    clock = _Clock()
    store = ScienceTaskStoreV2(tmp_path / "state")
    task = _admit(store, clock)
    attempt = task.attempts[0]
    store.begin_attempt_execution(
        task_id=task.task_id,
        attempt_id=attempt.attempt_id,
        now=clock(),
    )
    _register_rollout_admission(store, task)
    store.mark_attempt_running(
        task_id=task.task_id,
        attempt_id=attempt.attempt_id,
        now=clock(),
    )
    receipt, evidence, plan, result = _terminal_bundle(task)
    barrier = threading.Barrier(2)

    def capture():
        barrier.wait()
        try:
            return store.record_terminal_attempt(
                task_id=task.task_id,
                attempt_id=attempt.attempt_id,
                receipt=receipt,
                evidence=evidence,
                successor_plan=plan,
                terminal_result=result,
                now=clock(),
            )
        except ScienceTaskTerminalV2:
            return "lost"

    def cancel():
        barrier.wait()
        try:
            requested = store.request_attempt_cancellation(
                task_id=task.task_id,
                attempt_id=attempt.attempt_id,
                now=clock(),
            )
            return (
                store.finish_attempt_cancelled(
                    task_id=task.task_id,
                    attempt_id=attempt.attempt_id,
                    now=clock(),
                )
                if requested.state == "cancelling"
                else requested
            )
        except ScienceTaskTerminalV2:
            return "lost"

    with ThreadPoolExecutor(max_workers=2) as pool:
        capture_future = pool.submit(capture)
        cancel_future = pool.submit(cancel)
        capture_result = capture_future.result()
        cancel_result = cancel_future.result()

    terminal = store.get_task(task.task_id)
    if terminal.authoritative_attempt_id is None:
        assert terminal.state == "cancelled"
        assert capture_result == "lost"
        assert isinstance(cancel_result, ScienceAttemptExecutionRecordV2)
    else:
        assert terminal.authoritative_attempt_id == attempt.attempt_id
        assert terminal.state == "waiting_for_successor"
        assert cancel_result == "lost"
        assert isinstance(capture_result, ScienceAttemptExecutionRecordV2)
    store.close()


def test_infrastructure_failure_allows_retry_without_changing_admission(tmp_path) -> None:
    clock = _Clock()
    store = ScienceTaskStoreV2(tmp_path / "state")
    task = _admit(store, clock)
    first = task.attempts[0]
    store.begin_attempt_execution(
        task_id=task.task_id,
        attempt_id=first.attempt_id,
        now=clock(),
    )
    failed = store.finish_attempt_failed(
        task_id=task.task_id,
        attempt_id=first.attempt_id,
        error_code="service_generation_unavailable",
        now=clock(),
    )
    assert failed.state == "failed"
    assert store.get_task(task.task_id).state == "failed"

    second, replayed = store.append_attempt(
        task_id=task.task_id,
        request=AttemptAppendRequestV2(
            task_admission_id=task.admission.task_admission_id,
            admission_sha256=task.admission.admission_sha256,
            expected_previous_attempt_id=first.attempt_id,
            expected_next_ordinal=2,
        ),
        idempotency_key="retry-2",
        now=clock(),
    )
    assert replayed is False
    assert second.admission_sha256 == task.admission.admission_sha256
    assert (
        store.begin_attempt_execution(
            task_id=task.task_id,
            attempt_id=second.attempt_id,
            now=clock(),
        ).state
        == "preparing"
    )
    store.close()


def test_restart_fails_interrupted_attempt_closed_without_fabricating_capture(
    tmp_path,
) -> None:
    clock = _Clock()
    store = ScienceTaskStoreV2(tmp_path / "state")
    task = _admit(store, clock)
    attempt = task.attempts[0]
    store.begin_attempt_execution(
        task_id=task.task_id,
        attempt_id=attempt.attempt_id,
        now=clock(),
    )
    store.mark_attempt_running(
        task_id=task.task_id,
        attempt_id=attempt.attempt_id,
        now=clock(),
    )
    store.close()

    restarted = ScienceTaskStoreV2(tmp_path / "state")
    try:
        recovered = restarted.recover_interrupted_attempts(now=clock())
        assert recovered == [attempt.attempt_id]
        record = restarted.get_attempt_execution(task.task_id, attempt.attempt_id)
        assert record.state == "failed"
        assert record.receipt is None
        assert record.evidence is None
        assert restarted.get_task(task.task_id).state == "failed"
    finally:
        restarted.close()


def test_generation_bound_service_admission_is_exact_and_recoverable(tmp_path) -> None:
    clock = _Clock()
    store = ScienceTaskStoreV2(tmp_path / "state")
    task = _admit(store, clock)
    attempt = task.attempts[0]
    store.begin_attempt_execution(
        task_id=task.task_id,
        attempt_id=attempt.attempt_id,
        now=clock(),
    )
    rollout_task_id = f"rollout-{attempt.attempt_id}"
    rollout = GenerationBoundRunAdmissionCheck(
        operation=RunAdmissionOperation.ROLLOUT_TASK_SUBMIT,
        generation_digest="d" * 64,
        registry_digest=task.admission.registry_sha256,
        framework_lock_digest="e" * 64,
        payload_sha256="0" * 64,
        task_id=rollout_task_id,
        session_id=None,
    )
    assert store.register_attempt_run_admission(
        task_id=task.task_id,
        attempt_id=attempt.attempt_id,
        check=rollout,
        allow_create=True,
    )
    assert store.verify_attempt_run_admission(rollout)

    gateway = GenerationBoundRunAdmissionCheck(
        operation=RunAdmissionOperation.GATEWAY_SESSION_DISPATCH,
        generation_digest=rollout.generation_digest,
        registry_digest=rollout.registry_digest,
        framework_lock_digest=rollout.framework_lock_digest,
        payload_sha256=hashlib.sha256(b"gateway-dispatch").hexdigest(),
        task_id=rollout_task_id,
        session_id="sk-openevo-session-1",
    )
    assert store.verify_attempt_run_admission(gateway)
    assert store.verify_attempt_run_admission(gateway)

    changed = GenerationBoundRunAdmissionCheck(
        operation=gateway.operation,
        generation_digest=gateway.generation_digest,
        registry_digest=gateway.registry_digest,
        framework_lock_digest=gateway.framework_lock_digest,
        payload_sha256="f" * 64,
        task_id=gateway.task_id,
        session_id=gateway.session_id,
    )
    assert not store.verify_attempt_run_admission(changed)

    with pytest.raises(ScienceTaskConflictV2):
        store.register_attempt_run_admission(
            task_id=task.task_id,
            attempt_id=attempt.attempt_id,
            check=changed,
            allow_create=False,
        )
    store.close()

    restarted = ScienceTaskStoreV2(tmp_path / "state")
    try:
        assert restarted.verify_attempt_run_admission(rollout)
        assert restarted.verify_attempt_run_admission(gateway)
        assert not restarted.verify_attempt_run_admission(changed)
    finally:
        restarted.close()


def test_terminal_attempt_cannot_replay_a_previously_accepted_service_call(
    tmp_path,
) -> None:
    clock = _Clock()
    store = ScienceTaskStoreV2(tmp_path / "state")
    task = _admit(store, clock)
    attempt = task.attempts[0]
    store.begin_attempt_execution(
        task_id=task.task_id,
        attempt_id=attempt.attempt_id,
        now=clock(),
    )
    rollout = GenerationBoundRunAdmissionCheck(
        operation=RunAdmissionOperation.ROLLOUT_TASK_SUBMIT,
        generation_digest="d" * 64,
        registry_digest=task.admission.registry_sha256,
        framework_lock_digest="e" * 64,
        payload_sha256="0" * 64,
        task_id=f"rollout-{attempt.attempt_id}",
        session_id=None,
    )
    assert store.register_attempt_run_admission(
        task_id=task.task_id,
        attempt_id=attempt.attempt_id,
        check=rollout,
        allow_create=True,
    )
    gateway = GenerationBoundRunAdmissionCheck(
        operation=RunAdmissionOperation.GATEWAY_SESSION_DISPATCH,
        generation_digest=rollout.generation_digest,
        registry_digest=rollout.registry_digest,
        framework_lock_digest=rollout.framework_lock_digest,
        payload_sha256=hashlib.sha256(b"gateway-dispatch").hexdigest(),
        task_id=rollout.task_id,
        session_id="sk-openevo-session-1",
    )
    assert store.verify_attempt_run_admission(gateway)

    store.finish_attempt_failed(
        task_id=task.task_id,
        attempt_id=attempt.attempt_id,
        error_code="rollout_failed",
        now=clock(),
    )

    assert not store.verify_attempt_run_admission(rollout)
    assert not store.verify_attempt_run_admission(gateway)
    store.close()


def test_captured_attempt_starts_the_same_successor_plan(tmp_path) -> None:
    clock = _Clock()
    store = ScienceTaskStoreV2(tmp_path / "state")
    task = _admit(store, clock)
    attempt = task.attempts[0]
    store.begin_attempt_execution(
        task_id=task.task_id,
        attempt_id=attempt.attempt_id,
        now=clock(),
    )
    _register_rollout_admission(store, task)
    receipt, evidence, plan, result = _terminal_bundle(task)
    store.record_terminal_attempt(
        task_id=task.task_id,
        attempt_id=attempt.attempt_id,
        receipt=receipt,
        evidence=evidence,
        successor_plan=plan,
        terminal_result=result,
        now=clock(),
    )

    transition = store.start_successor_transition(
        task_id=task.task_id,
        accepted_attempt_id=attempt.attempt_id,
        plan=plan,
        now=clock(),
    )

    assert transition.state == "pending"
    assert transition.transition.accepted_attempt == attempt
    assert store.get_task(task.task_id).successor_transition == transition.transition
    store.close()


def _project_config() -> ScienceProjectConfigV2:
    return ScienceProjectConfigV2.model_validate(
        {
            "task": {
                "title": "Compile one v2 Attempt",
                "objective": "Solve the science task and preserve the workspace.",
            },
            "workspace": {"kind": "scratch", "display_name": "Scratch"},
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
                        "enabled": True,
                        "method": "text_memory_reflector",
                        "config": {},
                    }
                }
            },
        }
    )


def _service_binding(registry_sha256: str) -> ServiceRunBinding:
    identity = InternalServiceIdentity(
        service_id="core-control",
        generation_digest="d" * 64,
        registry_digest=registry_sha256,
        framework_lock_digest="e" * 64,
        credential="science-v2-compiler-credential-" + "x" * 40,
    )
    image = MANAGED_RUNTIME_IMAGES["managed_science"]
    return ServiceRunBinding(
        execution_mode=ServiceExecutionMode.CODEX_SUBSCRIPTION_TRANSCRIPT,
        codex_model="gpt-5.5",
        runtime_image=image,
        runtime_image_immutable_reference=(
            MANAGED_RUNTIME_RELEASES["managed_science"].trusted_digest
        ),
        runtime_identity_digest="f" * 64,
        generation_digest=identity.generation_digest,
        registry_digest=registry_sha256,
        framework_lock_digest=identity.framework_lock_digest,
        rollout_url="http://127.0.0.1:41001",
        evolution_backend_url="http://127.0.0.1:41002",
        gateway_url="http://127.0.0.1:41003",
        _identity=identity,
    )


def _workspace_handoff(task, binding: ServiceRunBinding) -> WorkspaceHandoffBindingV2:
    attempt = task.attempts[0]
    archive = WorkspaceArchiveDeclarationV2(
        format="openevo_deterministic_tar_v1",
        media_type="application/vnd.openevo.workspace-tar",
        content_sha256="8" * 64,
        byte_size=2560,
        entry_count=2,
        extracted_byte_size=task.admission.workspace_snapshot.byte_size,
    )
    return WorkspaceHandoffBindingV2(
        handoff_id="workspace-handoff-compile",
        task_id=f"rollout-{attempt.attempt_id}",
        attempt_id=attempt.attempt_id,
        task_admission_id=task.admission.task_admission_id,
        admission_sha256=task.admission.admission_sha256,
        project_id=task.project_id,
        input_workspace_snapshot=task.admission.workspace_snapshot,
        input_archive=archive,
        service_generation_sha256=binding.generation_digest,
        registry_sha256=binding.registry_digest,
        framework_lock_sha256=binding.framework_lock_digest,
        created_at="2026-07-23T02:00:00.000000Z",
    )


def test_compiler_uses_saved_v2_authority_without_legacy_context_routes(tmp_path) -> None:
    clock = _Clock()
    registry = verified_builtin_registry(tmp_path / "registry")
    config = _project_config()
    authority = _authority(
        project_config_sha256=project_config_sha256_for(config),
        normalized_evolution_intent_sha256=canonical_digest(config.evolution),
        registry_sha256=registry.snapshot.registry_digest,
    )
    store = ScienceTaskStoreV2(tmp_path / "state")
    task = _admit(store, clock, authority)
    attempt = task.attempts[0]
    binding = _service_binding(registry.snapshot.registry_digest)
    project = ProjectRecordV2(
        project_id=task.project_id,
        display_name="Compiler project",
        config=config,
        project_config_sha256=project_config_sha256_for(config),
        created_at="2026-07-23T02:00:00.000000Z",
        updated_at="2026-07-23T02:00:00.000000Z",
        resource_version=1,
    )

    compiled = compile_science_attempt_v2(
        task=task,
        attempt=attempt,
        project=project,
        binding=binding,
        workspace_handoff=_workspace_handoff(task, binding),
        executable_registry=registry,
    )

    request = compiled.rollout.request
    assert request.task_id == f"rollout-{attempt.attempt_id}"
    assert request.instruction == config.task.objective
    assert request.num_samples == 1
    assert request.workspace_handoff is not None
    assert request.runtime_context_binding is not None
    assert request.runtime_context_binding.source == "empty_genesis"
    assert request.runtime_context_binding.materialized_context_id is None
    assert request.runtime_context_binding.selected_artifact_ids == ()
    assert request.runtime is not None
    assert request.runtime.image == binding.runtime_image_immutable_reference
    assert request.runtime.allow_internet is False
    assert request.agent.harness == "codex"
    assert request.agent.model_name == "gpt-5.5"
    assert request.agent.settings["capture_mode"] == "transcript"
    assert request.builder.strategy == "agent_transcript"
    assert "evolution" not in request.metadata
    assert "context_artifact_ids" not in json.dumps(
        compiled.rollout.payload,
        sort_keys=True,
    )
    assert len(compiled.evolution_plan.registry_snapshot_digest) == 64
    assert compiled.evolution_methods[0].registry_snapshot_digest == (
        compiled.evolution_plan.registry_snapshot_digest
    )
    assert [item.target_id for item in compiled.evolution_methods] == ["text_memory"]
    assert compiled.successor_plan.enabled_methods[0].method_id == ("text_memory_reflector")
    store.close()


class _Catalog:
    def __init__(self, project: ProjectRecordV2) -> None:
        self.project = project

    def get_project(self, project_id: str) -> ProjectRecordV2:
        assert project_id == self.project.project_id
        return self.project


class _Services:
    def __init__(self, binding: ServiceRunBinding) -> None:
        self.binding = binding
        self.released = False

    def ensure_run_binding(self, *_args, **_kwargs):
        snapshot = ServiceGroupSnapshot(
            execution_mode=self.binding.execution_mode,
            services_available=True,
            run_ready=True,
            run_readiness_code=ServiceRunReadinessCode.READY,
            generation_digest=self.binding.generation_digest,
            services=(),
            runtime_image=self.binding.runtime_image,
            runtime_image_immutable_reference=(self.binding.runtime_image_immutable_reference),
            runtime_identity_digest=self.binding.runtime_identity_digest,
        )
        return snapshot, ServiceRunLease(
            binding=self.binding,
            _release=lambda: setattr(self, "released", True),
        )


class _Rollout:
    def __init__(
        self,
        handoffs: WorkspaceHandoffStoreV2,
        binding: ServiceRunBinding,
        session_root,
    ) -> None:
        self.handoffs = handoffs
        self.binding = binding
        self.session_root = session_root
        self.request: TaskRequest | None = None
        self.requests: list[TaskRequest] = []
        self.input_answer_before_run: list[str | None] = []
        self.result: SessionResult | None = None
        self.closed = False

    def submit_task(self, payload):
        request = TaskRequest.model_validate(payload)
        assert request.workspace_handoff is not None
        session_id = f"sk-openevo-executor-session-{len(self.requests) + 1}"
        session_parent = self.session_root / session_id
        session_parent.mkdir(mode=0o700)
        self.handoffs.claim(
            request.workspace_handoff,
            session_id=session_id,
            generation_sha256=self.binding.generation_digest,
            registry_sha256=self.binding.registry_digest,
            framework_lock_sha256=self.binding.framework_lock_digest,
        )
        self.handoffs.materialize_input(
            request.workspace_handoff,
            session_id=session_id,
            destination_parent=session_parent,
        )
        answer = session_parent / "workspace" / "answer.txt"
        self.input_answer_before_run.append(
            answer.read_text(encoding="utf-8") if answer.exists() else None
        )
        answer.write_text(
            "accepted\n",
            encoding="utf-8",
        )
        workspace_result = self.handoffs.publish_result(
            request.workspace_handoff,
            session_id=session_id,
            workspace_root=session_parent / "workspace",
            now=datetime(2026, 7, 23, 3, tzinfo=timezone.utc),
        )
        self.request = request
        self.requests.append(request)
        metadata = dict(request.metadata)
        runtime_context = request.runtime_context_binding
        assert runtime_context is not None
        evolution_metadata = {
            "context_id": runtime_context.materialized_context_id,
            "context_injected": runtime_context.source == "materialized_successor",
            "context_source": runtime_context.source,
            "runtime_context_snapshot_id": (
                runtime_context.project_head.runtime_context_snapshot.runtime_context_snapshot_id
            ),
        }
        if runtime_context.source == "materialized_successor":
            empty_tree = canonical_digest({"files": []})
            evolution_metadata["runtime_injection_receipt"] = {
                "schema_version": "4",
                "context_id": runtime_context.materialized_context_id,
                "context_manifest_sha256": (runtime_context.materialized_context_manifest_sha256),
                "revision_id": (
                    runtime_context.project_head.evolution_revision.evolution_revision_id
                ),
                "runtime_context_snapshot_id": (
                    runtime_context.project_head.runtime_context_snapshot.runtime_context_snapshot_id
                ),
                "project_head_id": runtime_context.project_head.project_head_id,
                "instruction_sha256": "0" * 64,
                "runtime_tree_sha256": empty_tree,
                "files": [],
                "artifacts": [
                    {"artifact_id": artifact_id}
                    for artifact_id in runtime_context.selected_artifact_ids
                ],
            }
        metadata["evolution"] = evolution_metadata
        self.result = SessionResult(
            session_id=session_id,
            task_id=request.task_id,
            status="COMPLETED",
            trajectory=Trajectory(
                status="COMPLETED",
                metadata={
                    "capture_mode": "transcript",
                    "token_level_metrics_available": False,
                },
                traces=[
                    Trace(
                        prompt_messages=[{"role": "user", "content": request.instruction}],
                        response_messages=[{"role": "assistant", "content": "Accepted"}],
                        finish_reason="transcript",
                        metadata={
                            "capture_mode": "transcript",
                            "token_level_metrics_available": False,
                            "transcript": "Accepted",
                        },
                    )
                ],
            ),
            metadata=metadata,
            workspace_result=workspace_result,
        )
        return request.task_id

    def get_task(self, task_id: str):
        assert self.request is not None and self.result is not None
        assert task_id == self.request.task_id
        return TaskStatus(
            task_id=task_id,
            status="completed",
            total_sessions=1,
            completed_sessions=1,
            results=[self.result],
        ).model_dump(mode="json")

    def cancel_task(self, task_id: str):
        return {"task_id": task_id, "status": "cancelled"}

    def close(self) -> None:
        self.closed = True


class _NotifyingExecutor:
    def __init__(self, delegate, completed: threading.Event) -> None:
        self.delegate = delegate
        self.completed = completed

    def execute(self, **kwargs):
        try:
            return self.delegate.execute(**kwargs)
        finally:
            self.completed.set()


class _BlockingExecutor:
    def __init__(self) -> None:
        self.started = threading.Event()

    def execute(self, *, task, attempt, cancellation):
        del task, attempt
        self.started.set()
        cancellation.wait()
        raise ScienceAttemptCancelledV2()


class _StoppingPreparer:
    def __init__(self) -> None:
        self.stopped = threading.Event()

    def request_stop(self) -> None:
        self.stopped.set()


def _wait_task_state(
    owner: CoreScienceTaskOwnerV2,
    task_id: str,
    expected: str,
) -> None:
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        task = owner.invoke("getCoreTaskV2", {"task_id": task_id})
        if task.state == expected:
            return
        threading.Event().wait(0.01)
    raise AssertionError(f"v2 Task did not reach {expected}")


def test_executor_captures_one_real_workspace_result_and_releases_generation(
    tmp_path,
) -> None:
    clock = _Clock()
    registry = verified_builtin_registry(tmp_path / "registry")
    config = _project_config()
    binding = _service_binding(registry.snapshot.registry_digest)
    project_id = "project-execution"
    workspaces = WorkspaceStoreV2(tmp_path / "workspaces")
    workspace = workspaces.ensure_empty_snapshot(project_id)
    verified = resolve_genesis_execution_snapshot(
        settings=EffectiveExecutionSettings(
            execution_mode=config.execution.mode,
            capture_mode=config.execution.capture_mode,
            harness_id=config.execution.harness_id,
            model_ref=config.execution.codex_model,
            token_limit=config.execution.token_limit,
            task_network_allow_internet=config.execution.task_network_allow_internet,
        ),
        service_binding=binding,
    )
    execution_sha256 = canonical_digest(verified.snapshot)
    effective = EffectiveExecutionSnapshotRefV2(
        effective_execution_snapshot_id=f"exec-{execution_sha256}",
        project_id=project_id,
        execution_mode=config.execution.mode,
        capture_mode=config.execution.capture_mode,
        token_level_metrics_available=False,
        producer_id=verified.producer_id,
        snapshot_sha256=execution_sha256,
    )
    head = _head(
        project_id,
        registry_sha256=registry.snapshot.registry_digest,
        workspace=workspace,
        effective_execution=effective,
    )
    authority = ScienceProjectAdmissionAuthorityV2(
        project_id=project_id,
        active_project_head=head,
        project_config_sha256=project_config_sha256_for(config),
        workspace_snapshot=workspace,
        normalized_evolution_intent_sha256=canonical_digest(config.evolution),
    )
    ledger = ScienceTaskStoreV2(tmp_path / "state")
    task = _admit(ledger, clock, authority)
    attempt = task.attempts[0]
    ledger.begin_attempt_execution(
        task_id=task.task_id,
        attempt_id=attempt.attempt_id,
        now=clock(),
    )
    project = ProjectRecordV2(
        project_id=project_id,
        display_name="Executor project",
        config=config,
        project_config_sha256=project_config_sha256_for(config),
        created_at="2026-07-23T02:00:00.000000Z",
        updated_at="2026-07-23T02:00:00.000000Z",
        resource_version=1,
    )
    handoffs = WorkspaceHandoffStoreV2(tmp_path / "workspace-handoffs")
    services = _Services(binding)
    rollout = _Rollout(handoffs, binding, tmp_path / "gateway-sessions")
    (tmp_path / "gateway-sessions").mkdir(mode=0o700)
    executor = ScienceAttemptExecutorV2(
        catalog=_Catalog(project),
        workspaces=workspaces,
        workspace_handoffs=handoffs,
        ledger=ledger,
        services=services,
        executable_registry=registry,
        rollout_factory=lambda _binding: rollout,
        clock=clock,
        poll_interval_seconds=0,
        max_poll_attempts=2,
    )

    executed = executor.execute(
        task=task,
        attempt=attempt,
        cancellation=threading.Event(),
    )

    assert executed.record.state == "captured"
    assert executed.receipt.rollout_payload_sha256 == (executed.compiled.rollout.payload_sha256)
    assert executed.evidence.workspace_entry_count == 1
    assert (
        ledger.get_captured_session_result(
            task.task_id,
            attempt.attempt_id,
        )
        == executed.session_result
    )
    assert ledger.get_task(task.task_id).authoritative_attempt_id == attempt.attempt_id
    assert services.released is True
    assert rollout.closed is True
    handoffs.close()
    ledger.close()
    workspaces.close()


def test_task_owner_automatically_executes_a_new_immutable_attempt(tmp_path) -> None:
    clock = _Clock()
    registry = verified_builtin_registry(tmp_path / "registry")
    config = _project_config()
    binding = _service_binding(registry.snapshot.registry_digest)
    project_id = "project-execution"
    workspaces = WorkspaceStoreV2(tmp_path / "workspaces")
    workspace = workspaces.ensure_empty_snapshot(project_id)
    verified = resolve_genesis_execution_snapshot(
        settings=EffectiveExecutionSettings(
            execution_mode=config.execution.mode,
            capture_mode=config.execution.capture_mode,
            harness_id=config.execution.harness_id,
            model_ref=config.execution.codex_model,
            token_limit=config.execution.token_limit,
            task_network_allow_internet=config.execution.task_network_allow_internet,
        ),
        service_binding=binding,
    )
    execution_sha256 = canonical_digest(verified.snapshot)
    head = _head(
        project_id,
        registry_sha256=registry.snapshot.registry_digest,
        workspace=workspace,
        effective_execution=EffectiveExecutionSnapshotRefV2(
            effective_execution_snapshot_id=f"exec-{execution_sha256}",
            project_id=project_id,
            execution_mode=config.execution.mode,
            capture_mode=config.execution.capture_mode,
            token_level_metrics_available=False,
            producer_id=verified.producer_id,
            snapshot_sha256=execution_sha256,
        ),
    )
    authority = ScienceProjectAdmissionAuthorityV2(
        project_id=project_id,
        active_project_head=head,
        project_config_sha256=project_config_sha256_for(config),
        workspace_snapshot=workspace,
        normalized_evolution_intent_sha256=canonical_digest(config.evolution),
    )
    project = ProjectRecordV2(
        project_id=project_id,
        display_name="Automatic executor project",
        config=config,
        project_config_sha256=project_config_sha256_for(config),
        created_at="2026-07-23T02:00:00.000000Z",
        updated_at="2026-07-23T02:00:00.000000Z",
        resource_version=1,
    )
    handoffs = WorkspaceHandoffStoreV2(tmp_path / "workspace-handoffs")
    services = _Services(binding)
    rollout = _Rollout(handoffs, binding, tmp_path / "gateway-sessions")
    (tmp_path / "gateway-sessions").mkdir(mode=0o700)
    completed = threading.Event()

    def executor_factory(ledger):
        return _NotifyingExecutor(
            ScienceAttemptExecutorV2(
                catalog=_Catalog(project),
                workspaces=workspaces,
                workspace_handoffs=handoffs,
                ledger=ledger,
                services=services,
                executable_registry=registry,
                rollout_factory=lambda _binding: rollout,
                clock=clock,
                poll_interval_seconds=0,
                max_poll_attempts=2,
            ),
            completed,
        )

    owner = CoreScienceTaskOwnerV2(
        state_root=tmp_path / "owner",
        clock=clock,
        attempt_executor_factory=executor_factory,
    )
    try:
        owner.publish_project_admission_authority(authority)
        task = owner.invoke(
            "submitCoreTaskV2",
            {
                "request": TaskSubmitRequestV2(
                    project_id=project_id,
                    expected_project_admission_etag=authority.project_etag,
                    expected_project_head_id=head.project_head_id,
                    expected_project_head_manifest_sha256=head.manifest_sha256,
                    expected_project_config_sha256=project.project_config_sha256,
                ),
                "idempotency_key": "automatic-execution",
            },
        )
        assert completed.wait(timeout=5)
        _wait_task_state(owner, task.task_id, "waiting_for_successor")
        authoritative = owner.invoke("getCoreTaskV2", {"task_id": task.task_id})
        assert authoritative.authoritative_attempt_id == task.attempts[0].attempt_id
        assert services.released is True
    finally:
        owner.close()
        handoffs.close()
        workspaces.close()


def test_task_owner_cancellation_wins_before_terminal_capture(tmp_path) -> None:
    clock = _Clock()
    runner = _BlockingExecutor()
    owner = CoreScienceTaskOwnerV2(
        state_root=tmp_path / "owner",
        clock=clock,
        attempt_executor_factory=lambda _ledger: runner,
    )
    authority = _authority()
    try:
        owner.publish_project_admission_authority(authority)
        task = owner.invoke(
            "submitCoreTaskV2",
            {
                "request": TaskSubmitRequestV2(
                    project_id=authority.project_id,
                    expected_project_admission_etag=authority.project_etag,
                    expected_project_head_id=(authority.active_project_head.project_head_id),
                    expected_project_head_manifest_sha256=(
                        authority.active_project_head.manifest_sha256
                    ),
                    expected_project_config_sha256=authority.project_config_sha256,
                ),
                "idempotency_key": "cancel-automatic-execution",
            },
        )
        assert runner.started.wait(timeout=5)
        cancelled = owner.cancel_attempt(
            task.task_id,
            task.attempts[0].attempt_id,
        )
        assert cancelled.state in {"cancelling", "cancelled"}
        _wait_task_state(owner, task.task_id, "cancelled")
        terminal = owner.invoke("getCoreTaskV2", {"task_id": task.task_id})
        assert terminal.authoritative_attempt_id is None
    finally:
        owner.close()


def test_task_owner_requests_successor_preparer_stop_before_shutdown(tmp_path) -> None:
    preparer = _StoppingPreparer()
    owner = CoreScienceTaskOwnerV2(
        state_root=tmp_path / "owner",
        successor_preparer=preparer,
    )

    owner.close()

    assert preparer.stopped.is_set()
