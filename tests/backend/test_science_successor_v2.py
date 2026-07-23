from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import hashlib
from pathlib import Path
import sqlite3
import threading

import pytest

from openevo.backend.contracts.v2.models import (
    EffectiveExecutionSnapshotRefV2,
    EvolutionRevisionRefV2,
    ProjectHeadRefV2,
    RuntimeContextSnapshotRefV2,
    ScienceProjectConfigV2,
    TaskSubmitRequestV2,
    TaskV2,
    TransitionChangedEventV2,
    WorkspaceSnapshotRefV2,
    project_config_sha256_for,
)
from openevo.backend.run_control import CoreTaskControlError
from openevo.backend.science_execution import (
    AcceptedWorkspaceResultV2,
    ScienceMethodOutputV2,
    ScienceSuccessorMethodPlanV2,
    ScienceSuccessorPlanV2,
    ScienceSuccessorPreparationContextV2,
    SealedTranscriptDatasetV2,
    SuccessorMaterializationV2,
    ValidatedScienceOutputsV2,
)
import openevo.backend.science_run_store as task_store_module
from openevo.backend.science_run_owner import (
    CoreScienceTaskOwnerV2,
    ScienceSuccessorPreparerV2,
)
from openevo.backend.science_run_store import (
    ScienceProjectAdmissionAuthorityV2,
    ScienceTaskStoreV2Error,
)
from openevo.evolution.revisions import atomic_successor_manifest_sha256
from openevo.evolution.framework.handlers import (
    PayloadManifestEntry,
    payload_tree_digest,
)
from openevo.evolution.runtime_injection import build_runtime_injection_plan


def _project_config() -> ScienceProjectConfigV2:
    return ScienceProjectConfigV2.model_validate(
        {
            "task": {
                "title": "Successor task",
                "objective": "Produce a verified successor.",
            },
            "workspace": {
                "kind": "scratch",
                "display_name": "Successor workspace",
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
        self._next = datetime(2026, 7, 23, 1, tzinfo=timezone.utc)
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
    runtime_contract_sha256: str = "b" * 64,
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


def _authority(head: ProjectHeadRefV2 | None = None) -> ScienceProjectAdmissionAuthorityV2:
    head = head or _head()
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


def _plan(task: TaskV2) -> ScienceSuccessorPlanV2:
    return ScienceSuccessorPlanV2(
        project_id=task.project_id,
        task_id=task.task_id,
        task_admission_id=task.admission.task_admission_id,
        admission_sha256=task.admission.admission_sha256,
        accepted_attempt_id=task.attempts[-1].attempt_id,
        predecessor_project_head_id=(
            task.admission.predecessor_project_head.project_head_id
        ),
        normalized_evolution_intent_sha256=(
            task.admission.normalized_evolution_intent_sha256
        ),
        enabled_methods=(
            ScienceSuccessorMethodPlanV2(
                target_id="text_memory",
                method_id="openevo.text-memory.reflect.v1",
                output_artifact_type="text_memory",
            ),
        ),
    )


class _Preparer(ScienceSuccessorPreparerV2):
    def __init__(
        self,
        *,
        fail_phase: str | None = None,
        block_methods: tuple[threading.Event, threading.Event] | None = None,
        crash: bool = False,
    ) -> None:
        self.fail_phase = fail_phase
        self.block_methods = block_methods
        self.crash = crash
        self.calls: list[str] = []

    def _enter(self, phase: str) -> None:
        self.calls.append(phase)
        if phase == "running_methods" and self.block_methods is not None:
            entered, release = self.block_methods
            entered.set()
            assert release.wait(timeout=5)
        if self.crash and phase == "running_methods":
            raise SystemExit("simulated process interruption")
        if self.fail_phase == phase:
            raise RuntimeError(f"injected {phase} failure with secret detail")

    def seal_dataset(
        self,
        context: ScienceSuccessorPreparationContextV2,
    ) -> SealedTranscriptDatasetV2:
        self._enter("sealing_dataset")
        return SealedTranscriptDatasetV2(
            dataset_id="dataset-1",
            artifact_id="artifact-dataset-1",
            manifest_sha256="b" * 64,
            record_count=12,
            task_id=context.task.task_id,
            task_admission_id=context.task.admission.task_admission_id,
            accepted_attempt_id=context.accepted_attempt.attempt_id,
            capture_mode="transcript",
            token_level_metrics_available=False,
            sealed=True,
        )

    def run_methods(
        self,
        context: ScienceSuccessorPreparationContextV2,
        dataset: SealedTranscriptDatasetV2,
    ) -> tuple[ScienceMethodOutputV2, ...]:
        self._enter("running_methods")
        assert dataset.accepted_attempt_id == context.accepted_attempt.attempt_id
        return (
            ScienceMethodOutputV2(
                target_id="text_memory",
                method_id="openevo.text-memory.reflect.v1",
                artifact_id="artifact-memory-1",
                artifact_type="text_memory",
                manifest_sha256="c" * 64,
                byte_size=1024,
                execution_boundary="outside_inference",
            ),
        )

    def validate_outputs(
        self,
        context: ScienceSuccessorPreparationContextV2,
        dataset: SealedTranscriptDatasetV2,
        outputs: tuple[ScienceMethodOutputV2, ...],
    ) -> ValidatedScienceOutputsV2:
        self._enter("validating")
        return ValidatedScienceOutputsV2(
            project_id=context.task.project_id,
            successor_transition_id=context.transition.transition.successor_transition_id,
            predecessor_project_head_id=(
                context.task.admission.predecessor_project_head.project_head_id
            ),
            dataset=dataset,
            outputs=outputs,
            evolution_revision=EvolutionRevisionRefV2(
                evolution_revision_id="evolution-1",
                project_id=context.task.project_id,
                manifest_sha256="d" * 64,
                artifact_count=1,
            ),
        )

    def materialize_context(
        self,
        context: ScienceSuccessorPreparationContextV2,
        validated: ValidatedScienceOutputsV2,
    ) -> SuccessorMaterializationV2:
        self._enter("materializing")
        evolution = validated.evolution_revision
        return SuccessorMaterializationV2(
            project_id=context.task.project_id,
            successor_transition_id=context.transition.transition.successor_transition_id,
            predecessor_project_head_id=(
                context.task.admission.predecessor_project_head.project_head_id
            ),
            materialized_context_id="ctx-successor-1",
            materialized_context_manifest_sha256="e" * 64,
            runtime_context_snapshot=RuntimeContextSnapshotRefV2(
                runtime_context_snapshot_id="runtime-context-1",
                project_id=context.task.project_id,
                evolution_revision_id=evolution.evolution_revision_id,
                evolution_revision_manifest_sha256=evolution.manifest_sha256,
                registry_sha256=context.task.admission.registry_sha256,
                runtime_contract_sha256=(
                    context.task.admission.predecessor_project_head
                    .runtime_context_snapshot.runtime_contract_sha256
                ),
                manifest_sha256="1" * 64,
            ),
        )

    def capture_workspace_result(
        self,
        context: ScienceSuccessorPreparationContextV2,
    ) -> AcceptedWorkspaceResultV2:
        self._enter("workspace")
        return AcceptedWorkspaceResultV2(
            project_id=context.task.project_id,
            task_id=context.task.task_id,
            accepted_attempt_id=context.accepted_attempt.attempt_id,
            workspace_snapshot=_workspace(context.task.project_id, "f"),
        )


def _owner(tmp_path: Path, preparer: _Preparer) -> CoreScienceTaskOwnerV2:
    return CoreScienceTaskOwnerV2(
        state_root=tmp_path,
        clock=_Clock(),
        successor_preparer=preparer,
    )


def _admit(owner: CoreScienceTaskOwnerV2) -> tuple[ScienceProjectAdmissionAuthorityV2, TaskV2]:
    authority = _authority()
    owner.publish_project_admission_authority(authority)
    task = owner.invoke(
        "submitCoreTaskV2",
        {"request": _request(authority), "idempotency_key": "submit-1"},
    )
    assert isinstance(task, TaskV2)
    return authority, task


def test_completed_attempt_commits_one_complete_adjacent_successor(
    tmp_path: Path,
) -> None:
    preparer = _Preparer()
    owner = _owner(tmp_path, preparer)
    predecessor, task = _admit(owner)
    try:
        transition = owner.run_successor_transition(
            task.task_id,
            accepted_attempt_id=task.attempts[0].attempt_id,
            plan=_plan(task),
        )

        assert transition.state == "committed"
        successor = transition.transition.successor_project_head
        assert successor is not None
        assert successor.generation == 1
        assert successor.predecessor_project_head_id == (
            predecessor.active_project_head.project_head_id
        )
        assert successor.workspace_snapshot.workspace_snapshot_id == "workspace-f"
        assert successor.evolution_revision.evolution_revision_id == "evolution-1"
        assert (
            successor.runtime_context_snapshot.runtime_context_snapshot_id
            == "runtime-context-1"
        )
        assert (
            successor.effective_execution_snapshot
            == predecessor.active_project_head.effective_execution_snapshot
        )
        assert preparer.calls == [
            "sealing_dataset",
            "running_methods",
            "validating",
            "materializing",
            "workspace",
        ]
        assert owner.active_project_head(task.project_id) == successor
        assert owner.list_project_heads(task.project_id) == [
            predecessor.active_project_head,
            successor,
        ]
        commit = owner.successor_commit(
            transition.transition.successor_transition_id
        )
        assert commit is not None
        assert commit.manifest_sha256 == atomic_successor_manifest_sha256(
            commit.manifest
        )
        assert commit.manifest.dataset_id == "dataset-1"
        assert commit.manifest.method_artifact_ids == ("artifact-memory-1",)
        assert commit.manifest.materialized_context_id == "ctx-successor-1"
        completed = owner.invoke("getCoreTaskV2", {"task_id": task.task_id})
        assert completed.state == "completed"
        assert completed.authoritative_attempt_id == task.attempts[0].attempt_id
        assert completed.successor_transition == transition.transition
    finally:
        owner.close()


@pytest.mark.parametrize(
    "phase",
    [
        "sealing_dataset",
        "running_methods",
        "validating",
        "materializing",
        "workspace",
    ],
)
def test_preparation_failure_keeps_predecessor_active_and_next_task_not_ready(
    tmp_path: Path,
    phase: str,
) -> None:
    preparer = _Preparer(fail_phase=phase)
    owner = _owner(tmp_path, preparer)
    predecessor, task = _admit(owner)
    try:
        with pytest.raises(CoreTaskControlError) as failed:
            owner.run_successor_transition(
                task.task_id,
                accepted_attempt_id=task.attempts[0].attempt_id,
                plan=_plan(task),
            )
        assert failed.value.code == "successor_transition_failed"
        assert "secret detail" not in str(failed.value)

        transition = owner.get_successor_transition_for_task(task.task_id)
        assert transition.state == "failed"
        assert transition.error is not None
        assert transition.transition.successor_project_head is None
        final_event = owner.list_task_events(task.task_id)[-1]
        assert final_event.event_type == "transition_changed"
        assert final_event.state == "failed"
        assert owner.active_project_head(task.project_id) == predecessor.active_project_head
        assert owner.list_project_heads(task.project_id) == [
            predecessor.active_project_head
        ]
        assert owner.successor_commit(
            transition.transition.successor_transition_id
        ) is None
        with pytest.raises(CoreTaskControlError) as not_ready:
            owner.invoke(
                "submitCoreTaskV2",
                {"request": _request(predecessor), "idempotency_key": "next-task"},
            )
        assert not_ready.value.code == "project_not_ready"
        assert owner.ownership_counts() == (1, 1, 1)
    finally:
        owner.close()


def test_db_commit_failure_exposes_no_partial_successor(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    preparer = _Preparer()
    owner = _owner(tmp_path, preparer)
    predecessor, task = _admit(owner)

    def fail_commit(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("injected commit failure")

    monkeypatch.setattr(task_store_module, "_before_v2_successor_commit", fail_commit)
    try:
        with pytest.raises(CoreTaskControlError):
            owner.run_successor_transition(
                task.task_id,
                accepted_attempt_id=task.attempts[0].attempt_id,
                plan=_plan(task),
            )
        transition = owner.get_successor_transition_for_task(task.task_id)
        assert transition.state == "failed"
        assert owner.active_project_head(task.project_id) == predecessor.active_project_head
        assert owner.list_project_heads(task.project_id) == [
            predecessor.active_project_head
        ]
        assert owner.successor_commit(
            transition.transition.successor_transition_id
        ) is None
    finally:
        owner.close()


def test_next_task_is_not_created_while_successor_methods_are_running(
    tmp_path: Path,
) -> None:
    entered = threading.Event()
    release = threading.Event()
    preparer = _Preparer(block_methods=(entered, release))
    owner = _owner(tmp_path, preparer)
    predecessor, task = _admit(owner)
    try:
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(
                owner.run_successor_transition,
                task.task_id,
                accepted_attempt_id=task.attempts[0].attempt_id,
                plan=_plan(task),
            )
            assert entered.wait(timeout=5)
            with pytest.raises(CoreTaskControlError) as not_ready:
                owner.invoke(
                    "submitCoreTaskV2",
                    {
                        "request": _request(predecessor),
                        "idempotency_key": "concurrent-next",
                    },
                )
            assert not_ready.value.code == "project_not_ready"
            assert owner.ownership_counts() == (1, 1, 1)
            release.set()
            assert future.result(timeout=5).state == "committed"
    finally:
        release.set()
        owner.close()


def test_interrupted_transition_recovers_failed_without_advancing_head(
    tmp_path: Path,
) -> None:
    owner = _owner(tmp_path, _Preparer(crash=True))
    predecessor, task = _admit(owner)
    with pytest.raises(SystemExit):
        owner.run_successor_transition(
            task.task_id,
            accepted_attempt_id=task.attempts[0].attempt_id,
            plan=_plan(task),
        )
    transition_id = owner.get_successor_transition_for_task(
        task.task_id
    ).transition.successor_transition_id
    owner.close()

    restarted = _owner(tmp_path, _Preparer())
    try:
        recovered = restarted.get_successor_transition(transition_id)
        assert recovered.state == "failed"
        assert recovered.error is not None
        assert recovered.error.code == "successor_transition_interrupted"
        assert restarted.active_project_head(task.project_id) == (
            predecessor.active_project_head
        )
        assert restarted.list_project_heads(task.project_id) == [
            predecessor.active_project_head
        ]
    finally:
        restarted.close()


def test_restart_fails_closed_on_tampered_atomic_successor_receipt(
    tmp_path: Path,
) -> None:
    owner = _owner(tmp_path, _Preparer())
    _predecessor, task = _admit(owner)
    owner.run_successor_transition(
        task.task_id,
        accepted_attempt_id=task.attempts[0].attempt_id,
        plan=_plan(task),
    )
    owner.close()

    database = (
        tmp_path / "science-tasks-v2" / "science-tasks-v2.sqlite3"
    )
    with sqlite3.connect(database) as connection:
        row = connection.execute(
            "SELECT successor_transition_id, commit_json FROM successor_commits"
        ).fetchone()
        assert row is not None
        corrupted = bytes(row[1]).replace(b'"dataset_id":"dataset-1"', b'"dataset_id":"dataset-2"')
        assert corrupted != bytes(row[1])
        connection.execute(
            "UPDATE successor_commits SET commit_json = ? "
            "WHERE successor_transition_id = ?",
            (corrupted, row[0]),
        )
        connection.commit()

    with pytest.raises(ScienceTaskStoreV2Error, match="persisted v2 document"):
        _owner(tmp_path, _Preparer())


def test_restart_fails_closed_on_rewritten_successor_event_history(
    tmp_path: Path,
) -> None:
    owner = _owner(tmp_path, _Preparer())
    _predecessor, task = _admit(owner)
    owner.run_successor_transition(
        task.task_id,
        accepted_attempt_id=task.attempts[0].attempt_id,
        plan=_plan(task),
    )
    owner.close()

    database = tmp_path / "science-tasks-v2" / "science-tasks-v2.sqlite3"
    with sqlite3.connect(database) as connection:
        row = connection.execute(
            "SELECT sequence, event_json FROM events "
            "WHERE task_id = ? AND event_type = 'transition_changed' "
            "ORDER BY sequence LIMIT 1",
            (task.task_id,),
        ).fetchone()
        assert row is not None
        event = TransitionChangedEventV2.model_validate_json(bytes(row[1]))
        rewritten = event.model_copy(
            update={"state": "committing", "progress_completed": 5}
        )
        rewritten = rewritten.model_copy(
            update={"event_id": task_store_module._v2_event_id(rewritten)}
        )
        connection.execute(
            "UPDATE events SET event_id = ?, event_json = ? WHERE sequence = ?",
            (
                rewritten.event_id,
                task_store_module._v2_model_bytes(rewritten),
                row[0],
            ),
        )
        connection.commit()

    with pytest.raises(ScienceTaskStoreV2Error, match="event history"):
        _owner(tmp_path, _Preparer())


def test_committed_successor_recovers_as_the_exact_active_head(tmp_path: Path) -> None:
    owner = _owner(tmp_path, _Preparer())
    _predecessor, task = _admit(owner)
    committed = owner.run_successor_transition(
        task.task_id,
        accepted_attempt_id=task.attempts[0].attempt_id,
        plan=_plan(task),
    )
    transition_id = committed.transition.successor_transition_id
    successor = committed.transition.successor_project_head
    receipt = owner.successor_commit(transition_id)
    owner.close()

    restarted = _owner(tmp_path, _Preparer())
    try:
        assert restarted.get_successor_transition(transition_id) == committed
        assert restarted.successor_commit(transition_id) == receipt
        assert restarted.active_project_head(task.project_id) == successor
        assert restarted.invoke("getCoreTaskV2", {"task_id": task.task_id}).state == (
            "completed"
        )
    finally:
        restarted.close()


def test_committed_context_is_pinned_only_by_the_second_task(
    tmp_path: Path,
) -> None:
    owner = _owner(tmp_path, _Preparer())
    predecessor, first = _admit(owner)
    try:
        committed = owner.run_successor_transition(
            first.task_id,
            accepted_attempt_id=first.attempts[0].attempt_id,
            plan=_plan(first),
        )
        successor = committed.transition.successor_project_head
        assert successor is not None
        assert first.admission.predecessor_project_head == predecessor.active_project_head
        assert first.admission.predecessor_project_head.runtime_context_snapshot != (
            successor.runtime_context_snapshot
        )

        next_authority = owner.project_admission_authority(first.project_id)
        second = owner.invoke(
            "submitCoreTaskV2",
            {"request": _request(next_authority), "idempotency_key": "submit-2"},
        )
        assert second.admission.predecessor_project_head == successor
        assert second.admission.predecessor_project_head.runtime_context_snapshot == (
            successor.runtime_context_snapshot
        )
        assert second.admission.predecessor_project_head.runtime_context_snapshot != (
            first.admission.predecessor_project_head.runtime_context_snapshot
        )
        receipt = owner.successor_commit(
            committed.transition.successor_transition_id
        )
        assert receipt is not None
        memory = b"Memory accepted only after the producing task completed."
        entry = PayloadManifestEntry(
            relative_path="memory.md",
            media_type="text/markdown",
            size_bytes=len(memory),
            sha256=hashlib.sha256(memory).hexdigest(),
        )
        artifact_id = receipt.manifest.method_artifact_ids[0]
        runtime_context = {
            "context_id": receipt.manifest.materialized_context_id,
            "memory": {
                "artifact_ids": [artifact_id],
                "rendered_text": memory.decode(),
                "items": [
                    {
                        "artifact_id": artifact_id,
                        "rendered_text": memory.decode(),
                    }
                ],
            },
            "agent_system": {
                "artifact_ids": [],
                "rendered_text": "",
                "target_path": "AGENTS.md",
                "targets": [],
            },
            "skills": [],
            "adapter_merge_spec": {
                "base_model": None,
                "merge_mode": "reference_only",
                "adapters": [],
            },
            "selection": {
                "artifact_ids": [artifact_id],
                "artifacts": [
                    {
                        "artifact_id": artifact_id,
                        "artifact_type": "text_memory",
                        "content_sha256": payload_tree_digest((entry,)),
                        "payload_entries": [entry.model_dump(mode="json")],
                    }
                ],
                "reasons": ["committed successor context"],
            },
        }
        injection = build_runtime_injection_plan(
            context=runtime_context,
            revision_id=successor.evolution_revision.evolution_revision_id,
            instruction="second task",
            expected_artifact_ids=(artifact_id,),
        )
        assert memory.decode() in injection.effective_instruction
        assert injection.authority["context_id"] == (
            receipt.manifest.materialized_context_id
        )
        assert injection.authority["revision_id"] == (
            second.admission.predecessor_project_head.evolution_revision.evolution_revision_id
        )
        assert injection.authority["revision_id"] != (
            first.admission.predecessor_project_head.evolution_revision.evolution_revision_id
        )
        assert owner.ownership_counts() == (2, 2, 2)
    finally:
        owner.close()
