from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import sqlite3
import threading

import pytest

from openevo.backend.contracts.v2.models import (
    AttemptAppendRequestV2,
    EffectiveExecutionSnapshotRefV2,
    EvolutionRevisionRefV2,
    ProjectHeadRefV2,
    RuntimeContextSnapshotRefV2,
    TaskActionRequestV2,
    TaskSubmitRequestV2,
    TaskV2,
    WorkspaceSnapshotRefV2,
    task_admission_sha256_for,
)
from openevo.backend.run_control import (
    RUN_OPERATION_IDS,
    TASK_OPERATION_IDS_V2,
    CoreTaskControlError,
)
from openevo.backend.science_run_owner import CoreScienceTaskOwnerV2
import openevo.backend.science_run_store as task_store_module
from openevo.backend.science_run_store import (
    ScienceProjectAdmissionAuthorityV2,
    ScienceProjectReadinessBlockerV2,
    ScienceTaskStoreV2Error,
)


class _Clock:
    def __init__(self) -> None:
        self._next = datetime(2026, 7, 23, tzinfo=timezone.utc)
        self._lock = threading.Lock()

    def __call__(self) -> datetime:
        with self._lock:
            value = self._next
            self._next += timedelta(microseconds=1)
            return value


def _workspace(project_id: str = "project-1", seed: str = "1") -> WorkspaceSnapshotRefV2:
    return WorkspaceSnapshotRefV2(
        workspace_snapshot_id=f"workspace-{seed}",
        project_id=project_id,
        manifest_sha256=seed * 64,
        entry_count=7,
        byte_size=4096,
    )


def _head(project_id: str = "project-1", *, seed: str = "2") -> ProjectHeadRefV2:
    evolution = EvolutionRevisionRefV2(
        evolution_revision_id=f"evolution-{seed}",
        project_id=project_id,
        manifest_sha256="3" * 64,
        artifact_count=3,
    )
    context = RuntimeContextSnapshotRefV2(
        runtime_context_snapshot_id=f"runtime-context-{seed}",
        project_id=project_id,
        evolution_revision_id=evolution.evolution_revision_id,
        evolution_revision_manifest_sha256=evolution.manifest_sha256,
        registry_sha256="a" * 64,
        runtime_contract_sha256="b" * 64,
        manifest_sha256="4" * 64,
    )
    execution = EffectiveExecutionSnapshotRefV2(
        effective_execution_snapshot_id=f"execution-{seed}",
        project_id=project_id,
        execution_mode="codex_subscription_transcript",
        capture_mode="transcript",
        token_level_metrics_available=False,
        producer_id="subscription-snapshot-issuer-v1",
        snapshot_sha256="5" * 64,
    )
    return ProjectHeadRefV2(
        project_head_id=f"project-head-{seed}",
        project_id=project_id,
        generation=0,
        predecessor_project_head_id=None,
        workspace_snapshot=_workspace(project_id, "6"),
        evolution_revision=evolution,
        runtime_context_snapshot=context,
        effective_execution_snapshot=execution,
        registry_sha256=context.registry_sha256,
        manifest_sha256=seed * 64,
    )


def _unowned_successor_head(
    predecessor: ProjectHeadRefV2,
) -> ProjectHeadRefV2:
    return ProjectHeadRefV2(
        project_head_id="project-head-unowned",
        project_id=predecessor.project_id,
        generation=predecessor.generation + 1,
        predecessor_project_head_id=(
            predecessor.project_head_id
        ),
        workspace_snapshot=_workspace(
            predecessor.project_id,
            "f",
        ),
        evolution_revision=predecessor.evolution_revision,
        runtime_context_snapshot=(
            predecessor.runtime_context_snapshot
        ),
        effective_execution_snapshot=(
            predecessor.effective_execution_snapshot
        ),
        registry_sha256=predecessor.registry_sha256,
        manifest_sha256="f" * 64,
    )


def _authority(
    *,
    blockers: tuple[ScienceProjectReadinessBlockerV2, ...] = (),
    head: ProjectHeadRefV2 | None = None,
    workspace: WorkspaceSnapshotRefV2 | None = None,
) -> ScienceProjectAdmissionAuthorityV2:
    head = head or _head()
    return ScienceProjectAdmissionAuthorityV2(
        project_id=head.project_id,
        active_project_head=head,
        project_config_sha256="7" * 64,
        workspace_snapshot=workspace or head.workspace_snapshot,
        normalized_evolution_intent_sha256="9" * 64,
        blockers=blockers,
    )


def _request(
    authority: ScienceProjectAdmissionAuthorityV2,
    **changes: object,
) -> TaskSubmitRequestV2:
    payload = {
        "project_id": authority.project_id,
        "expected_project_admission_etag": authority.project_etag,
        "expected_project_head_id": authority.active_project_head.project_head_id,
        "expected_project_head_manifest_sha256": (authority.active_project_head.manifest_sha256),
        "expected_project_config_sha256": authority.project_config_sha256,
    }
    payload.update(changes)
    return TaskSubmitRequestV2.model_validate(payload)


def _owner(tmp_path: Path) -> CoreScienceTaskOwnerV2:
    return CoreScienceTaskOwnerV2(state_root=tmp_path, clock=_Clock())


def test_project_authority_workspace_must_match_its_active_head() -> None:
    head = _head()
    with pytest.raises(ValueError, match="workspace differs"):
        _authority(
            head=head,
            workspace=_workspace(head.project_id, "f"),
        )


@pytest.mark.parametrize(
    "blocker",
    [
        ScienceProjectReadinessBlockerV2.SETTINGS_TRANSITION,
        ScienceProjectReadinessBlockerV2.CONTEXT_REBIND,
        ScienceProjectReadinessBlockerV2.WORKSPACE_PUBLICATION,
        ScienceProjectReadinessBlockerV2.PROJECT_CONFIG_REBIND,
    ],
)
def test_unresolved_project_state_creates_no_task_admission_or_attempt(
    tmp_path: Path,
    blocker: ScienceProjectReadinessBlockerV2,
) -> None:
    owner = _owner(tmp_path)
    if blocker is ScienceProjectReadinessBlockerV2.PROJECT_CONFIG_REBIND:
        authority = _authority()
        owner.publish_project_admission_authority(authority)
        authority = owner.begin_project_admission_authority_rebind(authority)
    else:
        authority = _authority(blockers=(blocker,))
        owner.publish_project_admission_authority(authority)
    try:
        with pytest.raises(CoreTaskControlError) as failure:
            owner.invoke(
                "submitCoreTaskV2",
                {"request": _request(authority), "idempotency_key": "submit-1"},
            )

        assert failure.value.code == "project_not_ready"
        assert failure.value.http_status == 409
        assert failure.value.retryable is True
        assert owner.ownership_counts() == (0, 0, 0)
    finally:
        owner.close()


@pytest.mark.parametrize(
    "blocker",
    [
        ScienceProjectReadinessBlockerV2.SUCCESSOR_TRANSITION,
        ScienceProjectReadinessBlockerV2.PROJECT_CONFIG_REBIND,
    ],
)
def test_reserved_project_blocker_requires_store_owned_transition(
    tmp_path: Path,
    blocker: ScienceProjectReadinessBlockerV2,
) -> None:
    owner = _owner(tmp_path)
    authority = _authority(
        blockers=(blocker,),
    )
    try:
        with pytest.raises(CoreTaskControlError) as rejected:
            owner.publish_project_admission_authority(authority)
        assert rejected.value.code == "task_precondition_failed"
        assert owner.ownership_counts() == (0, 0, 0)
    finally:
        owner.close()


def test_generic_authority_publisher_cannot_clear_project_config_rebind(
    tmp_path: Path,
) -> None:
    owner = _owner(tmp_path)
    authority = _authority()
    owner.publish_project_admission_authority(authority)
    blocked = owner.begin_project_admission_authority_rebind(authority)
    assert blocked.blockers == (
        ScienceProjectReadinessBlockerV2.PROJECT_CONFIG_REBIND,
    )
    try:
        with pytest.raises(CoreTaskControlError) as rejected:
            owner.publish_project_admission_authority(
                authority,
                expected_project_head_id=(
                    authority.active_project_head.project_head_id
                ),
            )
        assert rejected.value.code == "task_precondition_failed"
        assert owner.project_admission_authority(authority.project_id) == blocked
        assert owner.ownership_counts() == (0, 0, 0)
    finally:
        owner.close()


def test_generic_authority_publisher_cannot_rollback_staged_or_released_config(
    tmp_path: Path,
) -> None:
    owner = _owner(tmp_path)
    original = _authority()
    owner.publish_project_admission_authority(original)
    desired = ScienceProjectAdmissionAuthorityV2(
        project_id=original.project_id,
        active_project_head=original.active_project_head,
        project_config_sha256="a" * 64,
        workspace_snapshot=original.workspace_snapshot,
        normalized_evolution_intent_sha256="b" * 64,
    )
    owner.begin_project_admission_authority_rebind(original)
    staged = owner.finish_project_admission_authority_rebind(desired)
    assert staged == ScienceProjectAdmissionAuthorityV2(
        project_id=desired.project_id,
        active_project_head=desired.active_project_head,
        project_config_sha256=desired.project_config_sha256,
        workspace_snapshot=desired.workspace_snapshot,
        normalized_evolution_intent_sha256=(
            desired.normalized_evolution_intent_sha256
        ),
        blockers=(
            ScienceProjectReadinessBlockerV2.PROJECT_CONFIG_REBIND,
        ),
    )
    try:
        with pytest.raises(CoreTaskControlError) as staged_rollback:
            owner.publish_project_admission_authority(
                original,
                expected_project_head_id=(
                    original.active_project_head.project_head_id
                ),
            )
        assert staged_rollback.value.code == "task_precondition_failed"
        assert owner.project_admission_authority(original.project_id) == staged

        assert owner.release_project_admission_authority_rebind(desired) == desired
        with pytest.raises(CoreTaskControlError) as released_rollback:
            owner.publish_project_admission_authority(
                original,
                expected_project_head_id=(
                    original.active_project_head.project_head_id
                ),
            )
        assert released_rollback.value.code == "task_precondition_failed"
        assert owner.project_admission_authority(original.project_id) == desired
    finally:
        owner.close()


def test_restart_rejects_orphaned_successor_transition_blocker(
    tmp_path: Path,
) -> None:
    owner = _owner(tmp_path)
    authority = _authority()
    owner.publish_project_admission_authority(authority)
    owner.close()

    blocked = ScienceProjectAdmissionAuthorityV2(
        project_id=authority.project_id,
        active_project_head=authority.active_project_head,
        project_config_sha256=authority.project_config_sha256,
        workspace_snapshot=authority.workspace_snapshot,
        normalized_evolution_intent_sha256=(authority.normalized_evolution_intent_sha256),
        blockers=(ScienceProjectReadinessBlockerV2.SUCCESSOR_TRANSITION,),
    )
    database = tmp_path / "science-tasks-v2" / "science-tasks-v2.sqlite3"
    with sqlite3.connect(database) as connection:
        connection.execute(
            "UPDATE project_admission_authorities SET authority_json = ? WHERE project_id = ?",
            (
                task_store_module._v2_authority_bytes(blocked),
                authority.project_id,
            ),
        )
        connection.commit()

    with pytest.raises(
        ScienceTaskStoreV2Error,
        match="successor readiness blocker",
    ):
        _owner(tmp_path)


def test_project_authority_publisher_cannot_advance_active_head(
    tmp_path: Path,
) -> None:
    owner = _owner(tmp_path)
    authority = _authority()
    owner.publish_project_admission_authority(authority)
    forged_head = _unowned_successor_head(
        authority.active_project_head
    )
    forged_authority = ScienceProjectAdmissionAuthorityV2(
        project_id=authority.project_id,
        active_project_head=forged_head,
        project_config_sha256=authority.project_config_sha256,
        workspace_snapshot=forged_head.workspace_snapshot,
        normalized_evolution_intent_sha256=(
            authority.normalized_evolution_intent_sha256
        ),
    )
    try:
        with pytest.raises(CoreTaskControlError) as rejected:
            owner.publish_project_admission_authority(
                forged_authority,
                expected_project_head_id=(
                    authority.active_project_head
                    .project_head_id
                ),
            )
        assert rejected.value.code == "task_precondition_failed"
        assert (
            owner.project_admission_authority(
                authority.project_id
            )
            == authority
        )
    finally:
        owner.close()


def test_restart_rejects_non_genesis_head_without_successor_receipt(
    tmp_path: Path,
) -> None:
    owner = _owner(tmp_path)
    authority = _authority()
    owner.publish_project_admission_authority(authority)
    owner.close()

    forged_head = _unowned_successor_head(
        authority.active_project_head
    )
    forged_authority = ScienceProjectAdmissionAuthorityV2(
        project_id=authority.project_id,
        active_project_head=forged_head,
        project_config_sha256=authority.project_config_sha256,
        workspace_snapshot=forged_head.workspace_snapshot,
        normalized_evolution_intent_sha256=(
            authority.normalized_evolution_intent_sha256
        ),
    )
    database = (
        tmp_path
        / "science-tasks-v2"
        / "science-tasks-v2.sqlite3"
    )
    with sqlite3.connect(database) as connection:
        connection.execute(
            "INSERT INTO project_heads("
            "project_head_id, project_id, generation, "
            "predecessor_project_head_id, manifest_sha256, "
            "head_json) VALUES (?, ?, ?, ?, ?, ?)",
            (
                forged_head.project_head_id,
                forged_head.project_id,
                forged_head.generation,
                forged_head.predecessor_project_head_id,
                forged_head.manifest_sha256,
                task_store_module._v2_model_bytes(
                    forged_head
                ),
            ),
        )
        connection.execute(
            "UPDATE project_admission_authorities "
            "SET authority_json = ? WHERE project_id = ?",
            (
                task_store_module._v2_authority_bytes(
                    forged_authority
                ),
                authority.project_id,
            ),
        )
        connection.commit()

    with pytest.raises(
        ScienceTaskStoreV2Error,
        match="project head.*successor receipt",
    ):
        _owner(tmp_path)


@pytest.mark.parametrize(
    ("change", "expected_code"),
    [
        (
            {"expected_project_admission_etag": '"' + ("f" * 64) + '"'},
            "task_submission_stale",
        ),
        ({"expected_project_head_id": "project-head-stale"}, "task_submission_stale"),
        ({"expected_project_head_manifest_sha256": "f" * 64}, "task_submission_stale"),
        ({"expected_project_config_sha256": "f" * 64}, "task_submission_stale"),
    ],
)
def test_stale_submission_creates_no_partial_ownership(
    tmp_path: Path,
    change: dict[str, object],
    expected_code: str,
) -> None:
    owner = _owner(tmp_path)
    authority = _authority()
    owner.publish_project_admission_authority(authority)
    try:
        with pytest.raises(CoreTaskControlError) as failure:
            owner.invoke(
                "submitCoreTaskV2",
                {
                    "request": _request(authority, **change),
                    "idempotency_key": "submit-stale",
                },
            )

        assert failure.value.code == expected_code
        assert owner.ownership_counts() == (0, 0, 0)
    finally:
        owner.close()


def test_submission_atomically_pins_the_complete_project_head_and_first_attempt(
    tmp_path: Path,
) -> None:
    owner = _owner(tmp_path)
    authority = _authority()
    request = _request(authority)
    owner.publish_project_admission_authority(authority)
    try:
        task = owner.invoke(
            "submitCoreTaskV2",
            {"request": request, "idempotency_key": "submit-complete"},
        )

        assert isinstance(task, TaskV2)
        assert task.state == "admitted"
        assert task.admission.predecessor_project_head == authority.active_project_head
        assert (
            task.admission.predecessor_project_head.evolution_revision
            == authority.active_project_head.evolution_revision
        )
        assert (
            task.admission.predecessor_project_head.runtime_context_snapshot
            == authority.active_project_head.runtime_context_snapshot
        )
        assert (
            task.admission.predecessor_project_head.effective_execution_snapshot
            == authority.active_project_head.effective_execution_snapshot
        )
        assert task.admission.workspace_snapshot == authority.workspace_snapshot
        assert task.admission.project_config_sha256 == authority.project_config_sha256
        assert len(task.admission.task_envelope_sha256) == 64
        assert (
            task.admission.normalized_evolution_intent_sha256
            == authority.normalized_evolution_intent_sha256
        )
        assert task.admission.registry_sha256 == authority.active_project_head.registry_sha256
        assert task.admission.admission_sha256 == task_admission_sha256_for(task.admission)
        assert len(task.attempts) == 1
        assert task.attempts[0].ordinal == 1
        assert task.attempts[0].admission_sha256 == task.admission.admission_sha256
        assert task.attempts[0].predecessor_project_head_id == (
            authority.active_project_head.project_head_id
        )
        assert owner.ownership_counts() == (1, 1, 1)

        replay = owner.invoke(
            "submitCoreTaskV2",
            {"request": request, "idempotency_key": "submit-complete"},
        )
        assert replay == task
        assert owner.ownership_counts() == (1, 1, 1)
    finally:
        owner.close()


def test_conflicting_submission_idempotency_and_project_concurrency_fail_closed(
    tmp_path: Path,
) -> None:
    owner = _owner(tmp_path)
    authority = _authority()
    owner.publish_project_admission_authority(authority)
    try:
        request = _request(authority)
        accepted = owner.invoke(
            "submitCoreTaskV2",
            {"request": request, "idempotency_key": "submit-one"},
        )

        with pytest.raises(CoreTaskControlError) as reused:
            owner.invoke(
                "submitCoreTaskV2",
                {
                    "request": request.model_copy(
                        update={"expected_project_config_sha256": "d" * 64}
                    ),
                    "idempotency_key": "submit-one",
                },
            )
        assert reused.value.code == "task_idempotency_key_reused"

        with pytest.raises(CoreTaskControlError) as in_flight:
            owner.invoke(
                "submitCoreTaskV2",
                {"request": request, "idempotency_key": "submit-two"},
            )
        assert in_flight.value.code == "task_project_in_flight"
        assert owner.ownership_counts() == (1, 1, 1)
        assert owner.invoke("getCoreTaskV2", {"task_id": accepted.task_id}) == accepted
    finally:
        owner.close()


def test_two_owners_atomically_admit_only_one_task_for_a_project(
    tmp_path: Path,
) -> None:
    authority = _authority()
    first_owner = _owner(tmp_path)
    second_owner = _owner(tmp_path)
    first_owner.publish_project_admission_authority(authority)
    barrier = threading.Barrier(2)

    def submit(owner: CoreScienceTaskOwnerV2, key: str) -> object:
        barrier.wait()
        try:
            return owner.invoke(
                "submitCoreTaskV2",
                {"request": _request(authority), "idempotency_key": key},
            )
        except CoreTaskControlError as exc:
            return exc

    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(
                pool.map(
                    lambda item: submit(*item),
                    ((first_owner, "submit-a"), (second_owner, "submit-b")),
                )
            )

        accepted = [item for item in results if isinstance(item, TaskV2)]
        rejected = [item for item in results if isinstance(item, CoreTaskControlError)]
        assert len(accepted) == 1
        assert len(rejected) == 1
        assert rejected[0].code == "task_project_in_flight"
        assert first_owner.ownership_counts() == (1, 1, 1)
        assert second_owner.ownership_counts() == (1, 1, 1)
    finally:
        first_owner.close()
        second_owner.close()


def test_infrastructure_retry_appends_attempt_without_changing_any_pin(
    tmp_path: Path,
) -> None:
    owner = _owner(tmp_path)
    authority = _authority()
    owner.publish_project_admission_authority(authority)
    try:
        task = owner.invoke(
            "submitCoreTaskV2",
            {"request": _request(authority), "idempotency_key": "submit-retry"},
        )
        first = task.attempts[0]
        append = AttemptAppendRequestV2(
            task_admission_id=task.admission.task_admission_id,
            admission_sha256=task.admission.admission_sha256,
            expected_previous_attempt_id=first.attempt_id,
            expected_next_ordinal=2,
        )
        second = owner.invoke(
            "appendCoreTaskAttemptV2",
            {
                "task_id": task.task_id,
                "request": append,
                "idempotency_key": "retry-2",
            },
        )

        updated = owner.invoke("getCoreTaskV2", {"task_id": task.task_id})
        assert second.ordinal == 2
        assert updated.admission == task.admission
        assert updated.task_id == task.task_id
        assert updated.attempts == [first, second]
        assert all(
            attempt.admission_sha256 == task.admission.admission_sha256
            for attempt in updated.attempts
        )
        assert owner.ownership_counts() == (1, 1, 2)

        assert (
            owner.invoke(
                "appendCoreTaskAttemptV2",
                {
                    "task_id": task.task_id,
                    "request": append,
                    "idempotency_key": "retry-2",
                },
            )
            == second
        )
        assert owner.ownership_counts() == (1, 1, 2)

        with pytest.raises(CoreTaskControlError) as reused:
            owner.invoke(
                "appendCoreTaskAttemptV2",
                {
                    "task_id": task.task_id,
                    "request": append.model_copy(update={"expected_next_ordinal": 3}),
                    "idempotency_key": "retry-2",
                },
            )
        assert reused.value.code == "task_idempotency_key_reused"

        with pytest.raises(CoreTaskControlError) as stale:
            owner.invoke(
                "appendCoreTaskAttemptV2",
                {
                    "task_id": task.task_id,
                    "request": append,
                    "idempotency_key": "retry-stale",
                },
            )
        assert stale.value.code == "attempt_precondition_failed"
        assert owner.ownership_counts() == (1, 1, 2)

        owner.close()
        restarted = _owner(tmp_path)
        try:
            assert restarted.invoke("getCoreTaskV2", {"task_id": task.task_id}) == updated
            assert restarted.ownership_counts() == (1, 1, 2)
        finally:
            restarted.close()
    finally:
        owner.close()


def test_closed_task_cannot_be_mutated_or_retried_and_survives_restart(
    tmp_path: Path,
) -> None:
    authority = _authority()
    owner = _owner(tmp_path)
    owner.publish_project_admission_authority(authority)
    task = owner.invoke(
        "submitCoreTaskV2",
        {"request": _request(authority), "idempotency_key": "submit-close"},
    )
    closed = owner.close_task(
        task.task_id,
        TaskActionRequestV2(
            task_admission_id=task.admission.task_admission_id,
            admission_sha256=task.admission.admission_sha256,
        ),
        close_request_id="close-task-direct",
    )
    assert closed.state == "closed"
    owner.close()

    restarted = _owner(tmp_path)
    try:
        recovered = restarted.invoke("getCoreTaskV2", {"task_id": task.task_id})
        assert recovered == closed
        with pytest.raises(CoreTaskControlError) as terminal:
            restarted.invoke(
                "appendCoreTaskAttemptV2",
                {
                    "task_id": task.task_id,
                    "request": AttemptAppendRequestV2(
                        task_admission_id=task.admission.task_admission_id,
                        admission_sha256=task.admission.admission_sha256,
                        expected_previous_attempt_id=task.attempts[0].attempt_id,
                        expected_next_ordinal=2,
                    ),
                    "idempotency_key": "retry-closed",
                },
            )
        assert terminal.value.code == "task_terminal"
        assert restarted.ownership_counts() == (1, 1, 1)
    finally:
        restarted.close()


def test_restart_rejects_deleted_task_close_receipt(
    tmp_path: Path,
) -> None:
    authority = _authority()
    owner = _owner(tmp_path)
    owner.publish_project_admission_authority(authority)
    task = owner.invoke(
        "submitCoreTaskV2",
        {
            "request": _request(authority),
            "idempotency_key": "submit-delete-close-receipt",
        },
    )
    owner.close_task(
        task.task_id,
        TaskActionRequestV2(
            task_admission_id=task.admission.task_admission_id,
            admission_sha256=task.admission.admission_sha256,
        ),
        close_request_id="close-before-receipt-delete",
    )
    owner.close()

    database = tmp_path / "science-tasks-v2" / "science-tasks-v2.sqlite3"
    with sqlite3.connect(database) as connection:
        connection.execute(
            "DELETE FROM task_close_receipts WHERE task_id = ?",
            (task.task_id,),
        )
        connection.commit()

    with pytest.raises(
        ScienceTaskStoreV2Error,
        match="close authority receipt",
    ):
        _owner(tmp_path)


def test_restart_rejects_cross_bound_task_close_receipt(
    tmp_path: Path,
) -> None:
    authority = _authority()
    owner = _owner(tmp_path)
    owner.publish_project_admission_authority(authority)
    first = owner.invoke(
        "submitCoreTaskV2",
        {
            "request": _request(authority),
            "idempotency_key": "submit-close-cross-bind-1",
        },
    )
    owner.close_task(
        first.task_id,
        TaskActionRequestV2(
            task_admission_id=(first.admission.task_admission_id),
            admission_sha256=first.admission.admission_sha256,
        ),
        close_request_id="close-cross-bind-1",
    )
    second = owner.invoke(
        "submitCoreTaskV2",
        {
            "request": _request(authority),
            "idempotency_key": "submit-close-cross-bind-2",
        },
    )
    owner.close_task(
        second.task_id,
        TaskActionRequestV2(
            task_admission_id=(second.admission.task_admission_id),
            admission_sha256=(second.admission.admission_sha256),
        ),
        close_request_id="close-cross-bind-2",
    )
    owner.close()

    database = tmp_path / "science-tasks-v2" / "science-tasks-v2.sqlite3"
    with sqlite3.connect(database) as connection:
        connection.execute(
            "UPDATE task_close_receipts SET "
            "task_admission_id = ?, admission_sha256 = ? "
            "WHERE task_id = ?",
            (
                second.admission.task_admission_id,
                second.admission.admission_sha256,
                first.task_id,
            ),
        )
        connection.commit()

    with pytest.raises(
        ScienceTaskStoreV2Error,
        match="close receipt has no exact closed authority",
    ):
        _owner(tmp_path)


def test_legacy_closed_task_migrates_to_unattributed_exemption(
    tmp_path: Path,
) -> None:
    authority = _authority()
    owner = _owner(tmp_path)
    owner.publish_project_admission_authority(authority)
    task = owner.invoke(
        "submitCoreTaskV2",
        {
            "request": _request(authority),
            "idempotency_key": "submit-legacy-close",
        },
    )
    request = TaskActionRequestV2(
        task_admission_id=task.admission.task_admission_id,
        admission_sha256=task.admission.admission_sha256,
    )
    owner.close_task(
        task.task_id,
        request,
        close_request_id="close-before-legacy-downgrade",
    )
    owner.close()

    database = tmp_path / "science-tasks-v2" / "science-tasks-v2.sqlite3"
    with sqlite3.connect(database) as connection:
        for table in (
            "task_close_receipts",
            "legacy_task_close_exemptions",
            "successor_abandon_receipts",
            "legacy_successor_abandon_exemptions",
            "action_receipt_migrations",
        ):
            connection.execute(f"DROP TABLE {table}")
        connection.commit()

    migrated = _owner(tmp_path)
    try:
        with sqlite3.connect(database) as connection:
            exemption = connection.execute(
                "SELECT task_admission_id, admission_sha256 "
                "FROM legacy_task_close_exemptions "
                "WHERE task_id = ?",
                (task.task_id,),
            ).fetchone()
        assert exemption == (
            task.admission.task_admission_id,
            task.admission.admission_sha256,
        )
        with pytest.raises(CoreTaskControlError) as replay:
            migrated.close_task(
                task.task_id,
                request,
                close_request_id=("close-before-legacy-downgrade"),
                allow_closed_recovery=True,
            )
        assert replay.value.code == "task_terminal"
    finally:
        migrated.close()


def test_action_receipt_schema_migration_serializes_startup(
    tmp_path: Path,
) -> None:
    root = tmp_path / "concurrent-receipt-migration"
    barrier = threading.Barrier(2)

    def start_store() -> None:
        barrier.wait()
        store = task_store_module.ScienceTaskStoreV2(root)
        store.close()

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(start_store) for _ in range(2)]
        for future in futures:
            future.result(timeout=10)

    reopened = task_store_module.ScienceTaskStoreV2(root)
    reopened.close()


def test_action_receipt_schema_migration_rolls_back_as_one_unit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "interrupted-receipt-migration"
    root.mkdir(mode=0o700)
    original = task_store_module._migrate_v2_action_receipt_authority

    def interrupt_after_migration(
        connection: sqlite3.Connection,
        *,
        schema_preexisting: bool,
    ) -> None:
        original(
            connection,
            schema_preexisting=schema_preexisting,
        )
        raise SystemExit("simulated receipt migration crash")

    monkeypatch.setattr(
        task_store_module,
        "_migrate_v2_action_receipt_authority",
        interrupt_after_migration,
    )
    with pytest.raises(
        SystemExit,
        match="receipt migration crash",
    ):
        task_store_module.ScienceTaskStoreV2(root)

    database = root / "science-tasks-v2.sqlite3"
    with sqlite3.connect(database) as connection:
        names = {
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_schema WHERE type = 'table'"
            ).fetchall()
        }
    assert not names.intersection(task_store_module._V2_ACTION_RECEIPT_SCHEMA_TABLES)

    monkeypatch.setattr(
        task_store_module,
        "_migrate_v2_action_receipt_authority",
        original,
    )
    recovered = task_store_module.ScienceTaskStoreV2(root)
    recovered.close()


def test_restart_rejects_partial_action_receipt_schema(
    tmp_path: Path,
) -> None:
    root = tmp_path / "partial-receipt-schema"
    root.mkdir(mode=0o700)
    database = root / "science-tasks-v2.sqlite3"
    with sqlite3.connect(database) as connection:
        connection.execute(
            "CREATE TABLE action_receipt_migrations ("
            "singleton INTEGER PRIMARY KEY, "
            "migration_version INTEGER NOT NULL)"
        )
        connection.commit()

    with pytest.raises(
        ScienceTaskStoreV2Error,
        match="only partially present",
    ):
        task_store_module.ScienceTaskStoreV2(root)


def test_restart_rejects_action_receipt_schema_near_match(
    tmp_path: Path,
) -> None:
    owner = _owner(tmp_path)
    owner.close()
    database = tmp_path / "science-tasks-v2" / "science-tasks-v2.sqlite3"
    with sqlite3.connect(database) as connection:
        connection.execute("CREATE INDEX receipt_near_match ON task_close_receipts(expected_etag)")
        connection.commit()

    with pytest.raises(
        ScienceTaskStoreV2Error,
        match="schema fingerprint",
    ):
        _owner(tmp_path)


def test_restart_rejects_deleted_action_receipt_migration_marker(
    tmp_path: Path,
) -> None:
    owner = _owner(tmp_path)
    owner.close()
    database = tmp_path / "science-tasks-v2" / "science-tasks-v2.sqlite3"
    with sqlite3.connect(database) as connection:
        connection.execute("DELETE FROM action_receipt_migrations")
        connection.commit()

    with pytest.raises(
        ScienceTaskStoreV2Error,
        match="migration authority",
    ):
        _owner(tmp_path)


def test_v1_and_v2_run_control_operations_are_disjoint() -> None:
    assert "submitCoreTaskV2" in TASK_OPERATION_IDS_V2
    assert "createCoreRunV1" in RUN_OPERATION_IDS
    assert TASK_OPERATION_IDS_V2.isdisjoint(RUN_OPERATION_IDS)


def test_v2_task_owner_never_dispatches_a_frozen_v1_run_operation(
    tmp_path: Path,
) -> None:
    owner = _owner(tmp_path)
    try:
        with pytest.raises(CoreTaskControlError) as unavailable:
            owner.invoke("createCoreRunV1", {})
        assert unavailable.value.code == "task_operation_unavailable"
        assert owner.ownership_counts() == (0, 0, 0)
    finally:
        owner.close()


def test_v2_event_journal_survives_restart_and_tamper_fails_closed(
    tmp_path: Path,
) -> None:
    authority = _authority()
    owner = _owner(tmp_path)
    owner.publish_project_admission_authority(authority)
    task = owner.invoke(
        "submitCoreTaskV2",
        {"request": _request(authority), "idempotency_key": "event-journal"},
    )
    event = owner.list_task_events(task.task_id)[0]
    assert event.event_type == "task_admitted"
    assert event.event_id.startswith("event-")
    assert len(event.event_id) == len("event-") + 64
    owner.close()

    restarted = _owner(tmp_path)
    assert restarted.list_events()[0] == event
    restarted.close()

    database = tmp_path / "science-tasks-v2" / "science-tasks-v2.sqlite3"
    with sqlite3.connect(database) as connection:
        connection.execute(
            "UPDATE events SET event_type = 'attempt_appended' WHERE event_id = ?",
            (event.event_id,),
        )
        connection.commit()
    with pytest.raises(ScienceTaskStoreV2Error, match="event journal"):
        _owner(tmp_path)


def test_v2_task_recovery_rejects_a_rewritten_admission_authority_etag(
    tmp_path: Path,
) -> None:
    authority = _authority()
    owner = _owner(tmp_path)
    owner.publish_project_admission_authority(authority)
    task = owner.invoke(
        "submitCoreTaskV2",
        {"request": _request(authority), "idempotency_key": "etag-tamper"},
    )
    owner.close()

    database = tmp_path / "science-tasks-v2" / "science-tasks-v2.sqlite3"
    with sqlite3.connect(database) as connection:
        request_json = connection.execute(
            "SELECT request_json FROM tasks WHERE task_id = ?",
            (task.task_id,),
        ).fetchone()[0]
        payload = json.loads(bytes(request_json))
        payload["expected_project_admission_etag"] = '"' + ("f" * 64) + '"'
        rewritten = json.dumps(
            payload,
            ensure_ascii=True,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        connection.execute(
            "UPDATE tasks SET request_json = ?, request_sha256 = ? WHERE task_id = ?",
            (rewritten, hashlib.sha256(rewritten).hexdigest(), task.task_id),
        )
        connection.commit()

    with pytest.raises(ScienceTaskStoreV2Error, match="admission closure"):
        _owner(tmp_path)


def test_v2_task_timeline_outlives_the_bounded_sse_replay_window(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(task_store_module, "_MAX_V2_EVENT_REPLAY", 3)
    authority = _authority()
    owner = _owner(tmp_path)
    owner.publish_project_admission_authority(authority)
    try:
        first = owner.invoke(
            "submitCoreTaskV2",
            {"request": _request(authority), "idempotency_key": "timeline-first"},
        )
        first_timeline = owner.list_task_events(first.task_id)
        assert [event.event_type for event in first_timeline] == [
            "task_admitted",
            "attempt_appended",
        ]
        owner.close_task(
            first.task_id,
            TaskActionRequestV2(
                task_admission_id=first.admission.task_admission_id,
                admission_sha256=first.admission.admission_sha256,
            ),
            close_request_id="close-timeline-first",
        )
        second = owner.invoke(
            "submitCoreTaskV2",
            {"request": _request(authority), "idempotency_key": "timeline-second"},
        )

        assert len(owner.list_events()) == 3
        assert owner.list_task_events(first.task_id) == first_timeline
        assert len(owner.list_task_events(second.task_id)) == 2
        with pytest.raises(CoreTaskControlError) as expired:
            owner.list_events(after_event_id=first_timeline[0].event_id)
        assert expired.value.code == "event_cursor_expired"
        assert expired.value.http_status == 410
    finally:
        owner.close()
