from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import sqlite3
import threading

import pytest
from fastapi.testclient import TestClient

import openevo.evolution.store as store_module
from openevo.evolution.framework import (
    EvolutionExecutionProfile,
    EvolutionPlan,
    EvolutionTargetSelection,
    MethodExecutionEnvelope,
)
from openevo.evolution.framework.builtins import (
    ImplementationDistributionIdentity,
    build_builtin_registry,
)
from openevo.evolution.models import (
    ArtifactRegisterRequest,
    ArtifactType,
    DatasetCreateRequest,
    EventIngestRequest,
    JobCreateRequest,
    WorkerClaimRequest,
    WorkerCompleteRequest,
    WorkerFailRequest,
    WorkerHeartbeatRequest,
)
from openevo.evolution.methods import _read_dataset_artifact, run_method
from openevo.evolution.planned_jobs import (
    PlanBoundJobCreateRequest,
    PlanBoundJobRetryRequest,
    PlannedInputBinding,
)
from openevo.evolution.store import DatasetIntegrityError, EvolutionStore
from openevo.evolution.server import create_app
from tests.framework_testkit import verified_builtin_registry


def _snapshot():
    return build_builtin_registry(
        ImplementationDistributionIdentity(
            distribution="openevo-test",
            distribution_version="1.0.0",
            distribution_digest="a" * 64,
        )
    )


def _profile() -> EvolutionExecutionProfile:
    return EvolutionExecutionProfile(
        execution_mode="self_deployed",
        capture_mode="transcript",
        harness_id="codex",
    )


def _plan() -> EvolutionPlan:
    return _snapshot().compile_plan(
        plan_id="plan-skill-round-0",
        selections=(
            EvolutionTargetSelection(
                target_id="skill_bundle",
                enabled=True,
                method_id="skill_bundle_reflector",
                config={
                    "max_records": 13,
                    "reflector_llm": {
                        "provider": "codex_cli",
                        "model": "gpt-5.1-codex-mini",
                    },
                },
            ),
        ),
        profile=_profile(),
    )


def _store(tmp_path) -> EvolutionStore:
    store = EvolutionStore(
        db_path=tmp_path / "evolution.db",
        artifact_root=tmp_path / "artifacts",
    )
    store.initialize()
    return store


def _artifact(store: EvolutionStore, artifact_type: ArtifactType, name: str):
    return store.register_artifact(
        ArtifactRegisterRequest(
            type=artifact_type,
            name=name,
            uri=f"file:///tmp/{name}",
            promoted=True,
        )
    )


def _request(store: EvolutionStore) -> PlanBoundJobCreateRequest:
    dataset = _artifact(store, ArtifactType.DATASET, "dataset-current")
    prior_skill = _artifact(store, ArtifactType.SKILL_BUNDLE, "skill-prior")
    return PlanBoundJobCreateRequest(
        plan=_plan(),
        target_id="skill_bundle",
        job_type="openevo:run:task:round-0:skill_bundle_reflector",
        input_bindings=(
            PlannedInputBinding(
                binding_id="current_dataset",
                artifact_ids=(dataset.artifact_id,),
            ),
            PlannedInputBinding(
                binding_id="prior_target_artifacts",
                artifact_ids=(prior_skill.artifact_id,),
            ),
        ),
        core_config={
            "name": "task:skill_bundle:round-0",
            "task_id": "task",
            "round_index": 0,
            "promoted": True,
            "lineage": {
                "input_artifact_ids": [dataset.artifact_id, prior_skill.artifact_id]
            },
            "compatibility": {
                "task_tags": ["openevo_run_task:run:task"],
                "agent_harness": ["codex"],
            },
        },
    )


def _request_with_sealed_dataset(
    store: EvolutionStore,
    *,
    idempotency_key: str | None = "planned-job-sealed-dataset",
) -> tuple[PlanBoundJobCreateRequest, str]:
    store.ingest_event(
        EventIngestRequest(
            source="openevo",
            event_type="openevo.session_completed",
            source_event_id="session:planned-job-dataset",
            task_id="task-planned-job-dataset",
            session_id="session-planned-job-dataset",
            status="COMPLETED",
            payload={
                "session_result": {
                    "trajectory": {
                        "traces": [
                            {
                                "prompt_messages": [
                                    {"role": "user", "content": "Verify the sealed dataset."}
                                ],
                                "response_messages": [
                                    {"role": "assistant", "content": "Verified."}
                                ],
                            }
                        ]
                    }
                }
            },
        )
    )
    dataset = store.create_dataset(
        DatasetCreateRequest(
            idempotency_key=idempotency_key,
            name="planned job sealed dataset",
            purpose="skill_distillation",
            query={
                "event_types": ["openevo.session_completed"],
                "status": ["COMPLETED"],
            },
            limits={"max_events": 1, "max_traces": 1},
        )
    )
    base = _request(store)
    request = PlanBoundJobCreateRequest(
        plan=base.plan,
        target_id=base.target_id,
        job_type=base.job_type,
        input_bindings=(
            PlannedInputBinding(
                binding_id="current_dataset",
                artifact_ids=(dataset.artifact_id,),
            ),
            base.input_bindings[1],
        ),
        core_config=base.core_config,
        priority=base.priority,
    )
    return request, dataset.dataset_id


def _method_identities(request: PlanBoundJobCreateRequest) -> dict[str, str]:
    selection = request.plan.selections[0]
    return {selection.method_id: selection.method_identity_digest}


def _complete_transition_bound_skill_job(
    store: EvolutionStore,
    request: PlanBoundJobCreateRequest,
    *,
    payload_name: str,
) -> tuple[str, str]:
    created = store.create_plan_bound_job(
        request,
        snapshot=_snapshot(),
    )
    claim = store.claim_job(
        WorkerClaimRequest(
            worker_id=f"worker-{payload_name}",
            capabilities=[request.job_type],
            method_capabilities=["skill_bundle_reflector"],
            method_identity_capabilities=(
                _method_identities(request)
            ),
        )
    )
    assert claim.job is not None
    assert claim.job.job_id == created.job_id
    payload = (
        store.files.root
        / "worker-output"
        / payload_name
    )
    payload.mkdir(parents=True)
    (payload / "SKILL.md").write_text(
        f"# {payload_name}\n",
        encoding="utf-8",
    )
    completed = store.complete_job(
        created.job_id,
        WorkerCompleteRequest(
            lease_id=claim.job.lease_id,
            artifacts=[
                ArtifactRegisterRequest(
                    type=ArtifactType.SKILL_BUNDLE,
                    name=payload_name,
                    uri=payload.as_uri(),
                    manifest={"content_path": "SKILL.md"},
                    promoted=True,
                )
            ],
        ),
    )
    return created.job_id, completed["artifact_ids"][0]


def _request_using_sealed_artifact(
    base: PlanBoundJobCreateRequest,
    *,
    artifact_id: str,
    predecessor_transition_id: str,
    suffix: str,
) -> PlanBoundJobCreateRequest:
    selection = base.plan.selections[0]
    plan = _snapshot().compile_plan(
        plan_id=f"plan-skill-{suffix}",
        selections=(
            EvolutionTargetSelection(
                target_id=selection.target_id,
                enabled=True,
                method_id=selection.method_id,
                config=selection.config(),
            ),
        ),
        profile=_profile(),
    )
    return base.model_copy(
        update={
            "plan": plan,
            "job_type": (
                f"openevo:run:task:{suffix}:"
                "skill_bundle_reflector"
            ),
            "input_bindings": (
                base.input_bindings[0],
                PlannedInputBinding(
                    binding_id="prior_target_artifacts",
                    artifact_ids=(artifact_id,),
                ),
            ),
            "successor_transition_id": (
                f"successor-transition-{suffix}"
            ),
            "predecessor_successor_transition_id": (
                predecessor_transition_id
            ),
        }
    )


def test_plan_bound_job_persists_plan_envelope_and_exact_worker_projection(tmp_path) -> None:
    store = _store(tmp_path)
    request = _request(store)

    created = store.create_plan_bound_job(request, snapshot=_snapshot())

    with store.connect() as connection:
        plan_row = connection.execute(
            "SELECT * FROM evolution_plans WHERE plan_id = ?",
            (request.plan.plan_id,),
        ).fetchone()
        job_row = connection.execute(
            "SELECT * FROM jobs WHERE job_id = ?",
            (created.job_id,),
        ).fetchone()

    assert plan_row is not None
    assert json.loads(plan_row["plan_json"]) == request.plan.model_dump(mode="json")
    assert plan_row["registry_snapshot_digest"] == request.plan.registry_snapshot_digest
    assert job_row["plan_id"] == request.plan.plan_id
    assert job_row["target_id"] == "skill_bundle"
    selection = request.plan.selections[0]
    assert job_row["method_identity_digest"] == selection.method_identity_digest

    envelope = MethodExecutionEnvelope.model_validate_json(
        job_row["execution_envelope_json"]
    )
    assert envelope.plan_id == request.plan.plan_id
    assert envelope.method_id == "skill_bundle_reflector"
    assert envelope.user_config() == selection.config()
    assert envelope.core_config() == request.core_config
    assert list(envelope.input_artifact_ids()) == [
        artifact_id
        for binding in request.input_bindings
        for artifact_id in binding.artifact_ids
    ]
    assert json.loads(job_row["config_json"]) == envelope.legacy_flat_config()
    assert json.loads(job_row["declared_output_artifact_types_json"]) == [
        "skill_bundle"
    ]


def test_plan_bound_job_create_is_idempotent_and_rejects_identity_reuse(
    tmp_path,
) -> None:
    store = _store(tmp_path)
    request = _request(store)

    first = store.create_plan_bound_job(request, snapshot=_snapshot())
    repeated = store.create_plan_bound_job(request, snapshot=_snapshot())

    assert repeated == first
    with store.connect() as connection:
        count = connection.execute(
            "SELECT COUNT(*) FROM jobs WHERE plan_id = ? AND target_id = ?",
            (request.plan.plan_id, request.target_id),
        ).fetchone()[0]
    assert count == 1

    conflicting = request.model_copy(update={"job_type": "different-queue"})
    with pytest.raises(ValueError, match="different job request"):
        store.create_plan_bound_job(conflicting, snapshot=_snapshot())


def test_transition_bound_job_outputs_remain_sealed_until_head_authority(
    tmp_path,
) -> None:
    store = _store(tmp_path)
    request = _request(store).model_copy(
        update={"successor_transition_id": "successor-transition-1"}
    )
    created = store.create_plan_bound_job(request, snapshot=_snapshot())
    claim = store.claim_job(
        WorkerClaimRequest(
            worker_id="verified-skill-worker",
            capabilities=[request.job_type],
            method_capabilities=["skill_bundle_reflector"],
            method_identity_capabilities=_method_identities(request),
        )
    )
    assert claim.job is not None
    payload = store.files.root / "worker-output" / "skill"
    payload.mkdir(parents=True)
    (payload / "SKILL.md").write_text("# Sealed skill\n", encoding="utf-8")

    completed = store.complete_job(
        created.job_id,
        WorkerCompleteRequest(
            lease_id=claim.job.lease_id,
            artifacts=[
                ArtifactRegisterRequest(
                    type=ArtifactType.SKILL_BUNDLE,
                    name="sealed successor skill",
                    uri=payload.as_uri(),
                    manifest={"content_path": "SKILL.md"},
                    promoted=True,
                )
            ],
        ),
    )

    with store.connect() as connection:
        artifact = connection.execute(
            "SELECT state, staging_job_id FROM artifacts WHERE artifact_id = ?",
            (completed["artifact_ids"][0],),
        ).fetchone()
    assert artifact["state"] == "sealed"
    assert artifact["staging_job_id"] == created.job_id
    with pytest.raises(ValueError, match="unknown artifact"):
        store.get_artifact(completed["artifact_ids"][0])
    assert completed["artifact_ids"][0] not in {
        str(item["artifact_id"])
        for item in store._promoted_artifact_rows()
    }
    restarted = EvolutionStore(
        db_path=tmp_path / "evolution.db",
        artifact_root=tmp_path / "artifacts",
    )
    restarted.initialize()
    with restarted.connect() as connection:
        recovered = connection.execute(
            "SELECT state, staging_job_id FROM artifacts WHERE artifact_id = ?",
            (completed["artifact_ids"][0],),
        ).fetchone()
    assert recovered["state"] == "sealed"
    assert recovered["staging_job_id"] == created.job_id


@pytest.mark.parametrize(
    "corruption",
    (
        "staging_job",
        "lineage_job",
        "lineage_transition",
        "lineage_target",
        "lineage_method",
        "artifact_type",
    ),
)
def test_sealed_successor_output_rejects_cross_bound_authority(
    tmp_path,
    corruption: str,
) -> None:
    store = _store(tmp_path)
    transition_a = "successor-transition-cross-bind-a"
    transition_b = "successor-transition-cross-bind-b"
    request_a = _request(store).model_copy(
        update={"successor_transition_id": transition_a}
    )
    selection = request_a.plan.selections[0]
    plan_b = _snapshot().compile_plan(
        plan_id="plan-skill-cross-bind-b",
        selections=(
            EvolutionTargetSelection(
                target_id=selection.target_id,
                enabled=True,
                method_id=selection.method_id,
                config=selection.config(),
            ),
        ),
        profile=_profile(),
    )
    request_b = request_a.model_copy(
        update={
            "plan": plan_b,
            "job_type": (
                "openevo:run:task:cross-bind:"
                "skill_bundle_reflector"
            ),
            "successor_transition_id": transition_b,
        }
    )
    job_a, artifact_a = _complete_transition_bound_skill_job(
        store,
        request_a,
        payload_name="cross-bind-a",
    )
    job_b, _artifact_b = _complete_transition_bound_skill_job(
        store,
        request_b,
        payload_name="cross-bind-b",
    )

    with store.connect() as connection:
        if corruption == "staging_job":
            connection.execute(
                "UPDATE artifacts SET staging_job_id = ? "
                "WHERE artifact_id = ?",
                (job_b, artifact_a),
            )
        elif corruption == "artifact_type":
            connection.execute(
                "UPDATE artifacts SET type = ? "
                "WHERE artifact_id = ?",
                ("text_memory", artifact_a),
            )
        else:
            row = connection.execute(
                "SELECT lineage_json FROM artifacts "
                "WHERE artifact_id = ?",
                (artifact_a,),
            ).fetchone()
            lineage = json.loads(row["lineage_json"])
            execution = lineage["openevo_execution"]
            if corruption == "lineage_job":
                execution["job_id"] = job_b
            elif corruption == "lineage_transition":
                execution["successor_transition_id"] = (
                    transition_b
                )
            elif corruption == "lineage_method":
                execution["method_id"] = "wrong_method"
            else:
                execution["target_id"] = "text_memory"
            connection.execute(
                "UPDATE artifacts SET lineage_json = ? "
                "WHERE artifact_id = ?",
                (
                    json.dumps(
                        lineage,
                        ensure_ascii=True,
                        allow_nan=False,
                        sort_keys=True,
                        separators=(",", ":"),
                    ),
                    artifact_a,
                ),
            )
        connection.commit()

    plan_c = _snapshot().compile_plan(
        plan_id=f"plan-skill-cross-bind-c-{corruption}",
        selections=(
            EvolutionTargetSelection(
                target_id=selection.target_id,
                enabled=True,
                method_id=selection.method_id,
                config=selection.config(),
            ),
        ),
        profile=_profile(),
    )
    request_c = request_a.model_copy(
        update={
            "plan": plan_c,
            "job_type": (
                "openevo:run:task:cross-bind-c:"
                "skill_bundle_reflector"
            ),
            "input_bindings": (
                request_a.input_bindings[0],
                PlannedInputBinding(
                    binding_id="prior_target_artifacts",
                    artifact_ids=(artifact_a,),
                ),
            ),
            "successor_transition_id": (
                f"successor-transition-cross-bind-c-{corruption}"
            ),
            "predecessor_successor_transition_id": (
                transition_b
                if corruption == "staging_job"
                else transition_a
            ),
        }
    )
    with pytest.raises(
        ValueError,
        match="sealed transition artifact authority",
    ):
        store.create_plan_bound_job(
            request_c,
            snapshot=_snapshot(),
        )

    with pytest.raises(
        ValueError,
        match="sealed transition artifact authority",
    ):
        store.get_internal_job_result(job_a)
    with pytest.raises(
        ValueError,
        match="sealed transition artifact authority",
    ):
        store.get_internal_successor_artifact(
            transition_a,
            artifact_a,
        )

    restarted = EvolutionStore(
        db_path=tmp_path / "evolution.db",
        artifact_root=tmp_path / "artifacts",
    )
    with pytest.raises(
        ValueError,
        match="sealed transition artifact authority",
    ):
        restarted.initialize()


@pytest.mark.parametrize(
    "validation_phase",
    ("claim", "complete"),
)
def test_sealed_predecessor_authority_is_rechecked_at_worker_boundary(
    tmp_path,
    validation_phase: str,
) -> None:
    store = _store(tmp_path)
    transition_id = "successor-transition-worker-input"
    base = _request(store).model_copy(
        update={"successor_transition_id": transition_id}
    )
    _job_id, artifact_id = (
        _complete_transition_bound_skill_job(
            store,
            base,
            payload_name="worker-input-source",
        )
    )
    request = _request_using_sealed_artifact(
        base,
        artifact_id=artifact_id,
        predecessor_transition_id=transition_id,
        suffix=f"worker-input-{validation_phase}",
    )
    created = store.create_plan_bound_job(
        request,
        snapshot=_snapshot(),
    )
    claim = None
    if validation_phase == "complete":
        claim = store.claim_job(
            WorkerClaimRequest(
                worker_id="worker-input-consumer",
                capabilities=[request.job_type],
                method_capabilities=[
                    "skill_bundle_reflector"
                ],
                method_identity_capabilities=(
                    _method_identities(request)
                ),
            )
        )
        assert claim.job is not None
        assert claim.job.job_id == created.job_id

    with store.connect() as connection:
        row = connection.execute(
            "SELECT lineage_json FROM artifacts "
            "WHERE artifact_id = ?",
            (artifact_id,),
        ).fetchone()
        lineage = json.loads(row["lineage_json"])
        lineage["openevo_execution"]["method_id"] = (
            "wrong_method"
        )
        connection.execute(
            "UPDATE artifacts SET lineage_json = ? "
            "WHERE artifact_id = ?",
            (
                json.dumps(
                    lineage,
                    ensure_ascii=True,
                    allow_nan=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ),
                artifact_id,
            ),
        )
        connection.commit()

    if validation_phase == "claim":
        observed = store.claim_job(
            WorkerClaimRequest(
                worker_id="worker-input-consumer",
                capabilities=[request.job_type],
                method_capabilities=[
                    "skill_bundle_reflector"
                ],
                method_identity_capabilities=(
                    _method_identities(request)
                ),
            )
        )
        assert observed.job is None
    else:
        assert claim is not None
        assert claim.job is not None
        output = (
            store.files.root
            / "worker-output"
            / "worker-input-consumer"
        )
        output.mkdir(parents=True)
        (output / "SKILL.md").write_text(
            "# Consumer\n",
            encoding="utf-8",
        )
        with pytest.raises(
            ValueError,
            match="sealed transition artifact authority",
        ):
            store.complete_job(
                created.job_id,
                WorkerCompleteRequest(
                    lease_id=claim.job.lease_id,
                    artifacts=[
                        ArtifactRegisterRequest(
                            type=ArtifactType.SKILL_BUNDLE,
                            name="worker input consumer",
                            uri=output.as_uri(),
                            manifest={
                                "content_path": "SKILL.md"
                            },
                            promoted=True,
                        )
                    ],
                ),
            )

    with store.connect() as connection:
        state = connection.execute(
            "SELECT state FROM jobs WHERE job_id = ?",
            (created.job_id,),
        ).fetchone()["state"]
    assert state == (
        "failed"
        if validation_phase == "claim"
        else "claimed"
    )


def test_sealed_predecessor_rejects_corrupt_owner_input_inventory(
    tmp_path,
) -> None:
    store = _store(tmp_path)
    transition_id = "successor-transition-owner-input"
    base = _request(store).model_copy(
        update={"successor_transition_id": transition_id}
    )
    job_id, artifact_id = (
        _complete_transition_bound_skill_job(
            store,
            base,
            payload_name="owner-input-source",
        )
    )
    with store.connect() as connection:
        connection.execute(
            "UPDATE jobs SET input_artifact_ids_json = '[]' "
            "WHERE job_id = ?",
            (job_id,),
        )
        connection.commit()

    request = _request_using_sealed_artifact(
        base,
        artifact_id=artifact_id,
        predecessor_transition_id=transition_id,
        suffix="owner-input-consumer",
    )
    with pytest.raises(
        ValueError,
        match="sealed transition artifact authority",
    ):
        store.create_plan_bound_job(
            request,
            snapshot=_snapshot(),
        )


def test_discard_fences_claimed_job_late_completion_and_restart(
    tmp_path,
    monkeypatch,
) -> None:
    store = _store(tmp_path)
    transition_id = "successor-transition-discard-race"
    request = _request(store).model_copy(
        update={"successor_transition_id": transition_id}
    )
    created = store.create_plan_bound_job(
        request,
        snapshot=_snapshot(),
    )
    claim = store.claim_job(
        WorkerClaimRequest(
            worker_id="verified-skill-worker",
            capabilities=[request.job_type],
            method_capabilities=["skill_bundle_reflector"],
            method_identity_capabilities=(
                _method_identities(request)
            ),
        )
    )
    assert claim.job is not None
    payload = store.files.root / "worker-output" / "late-skill"
    payload.mkdir(parents=True)
    (payload / "SKILL.md").write_text(
        "# Late sealed skill\n",
        encoding="utf-8",
    )
    staged = threading.Event()
    release_completion = threading.Event()
    registered_artifact_ids: list[str] = []
    original_register = store._register_artifact

    def _stage_then_pause(*args, **kwargs):
        artifact = original_register(*args, **kwargs)
        registered_artifact_ids.append(artifact.artifact_id)
        staged.set()
        assert release_completion.wait(timeout=5)
        return artifact

    monkeypatch.setattr(
        store,
        "_register_artifact",
        _stage_then_pause,
    )
    completion = WorkerCompleteRequest(
        lease_id=claim.job.lease_id,
        artifacts=[
            ArtifactRegisterRequest(
                type=ArtifactType.SKILL_BUNDLE,
                name="late successor skill",
                uri=payload.as_uri(),
                manifest={"content_path": "SKILL.md"},
                promoted=True,
            )
        ],
    )

    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(
            store.complete_job,
            created.job_id,
            completion,
        )
        assert staged.wait(timeout=5)
        try:
            receipt = store.discard_successor_transition_outputs(
                transition_id
            )
            with pytest.raises(ValueError, match="lease|state"):
                store.heartbeat_job(
                    created.job_id,
                    WorkerHeartbeatRequest(
                        lease_id=claim.job.lease_id,
                        progress=0.5,
                    ),
                )
            with pytest.raises(ValueError, match="lease|state"):
                store.fail_job(
                    created.job_id,
                    WorkerFailRequest(
                        lease_id=claim.job.lease_id,
                        error="late worker failure",
                        retryable=True,
                    ),
                )
        finally:
            release_completion.set()
        with pytest.raises(ValueError, match="lease|discard"):
            future.result(timeout=5)

    assert receipt == {
        "successor_transition_id": transition_id,
        "discarded_artifact_ids": [],
        "discarded_materialized_context_ids": [],
    }
    assert len(registered_artifact_ids) == 1
    with store.connect() as connection:
        job = connection.execute(
            "SELECT state, claimed_by, lease_id, "
            "lease_expires_at, lease_duration_seconds "
            "FROM jobs WHERE job_id = ?",
            (created.job_id,),
        ).fetchone()
        artifact_count = connection.execute(
            "SELECT COUNT(*) FROM artifacts WHERE artifact_id = ?",
            (registered_artifact_ids[0],),
        ).fetchone()[0]
        tombstone_count = connection.execute(
            "SELECT COUNT(*) FROM successor_transition_discards "
            "WHERE successor_transition_id = ?",
            (transition_id,),
        ).fetchone()[0]
    assert tuple(job) == ("cancelled", None, None, None, None)
    assert artifact_count == 0
    assert tombstone_count == 1

    retry = PlanBoundJobRetryRequest(
        retry_request_id="discarded-transition-retry",
        plan_id=request.plan.plan_id,
        target_id=request.target_id,
    )
    with pytest.raises(ValueError, match="discarded"):
        store.retry_plan_bound_job(
            created.job_id,
            retry,
            snapshot=_snapshot(),
        )
    with pytest.raises(ValueError, match="discarded"):
        store.create_plan_bound_job(
            request,
            snapshot=_snapshot(),
        )

    restarted = EvolutionStore(
        db_path=tmp_path / "evolution.db",
        artifact_root=tmp_path / "artifacts",
    )
    restarted.initialize()
    assert restarted.discard_successor_transition_outputs(
        transition_id
    ) == {
        "successor_transition_id": transition_id,
        "discarded_artifact_ids": [],
        "discarded_materialized_context_ids": [],
    }
    with pytest.raises(ValueError, match="discarded"):
        restarted.retry_plan_bound_job(
            created.job_id,
            retry,
            snapshot=_snapshot(),
        )
    with pytest.raises(ValueError, match="discarded"):
        restarted.create_plan_bound_job(
            request,
            snapshot=_snapshot(),
        )


def test_discard_cancels_pending_job_and_blocks_exact_retry_replay(
    tmp_path,
) -> None:
    store = _store(tmp_path)
    transition_id = "successor-transition-pending-discard"
    request = _request(store).model_copy(
        update={"successor_transition_id": transition_id}
    )
    created = store.create_plan_bound_job(
        request,
        snapshot=_snapshot(),
    )
    retry = PlanBoundJobRetryRequest(
        retry_request_id="retry-before-discard",
        plan_id=request.plan.plan_id,
        target_id=request.target_id,
    )
    with store.connect() as connection:
        connection.execute(
            "UPDATE jobs SET state = ? WHERE job_id = ?",
            ("expired", created.job_id),
        )
        connection.commit()
    assert store.retry_plan_bound_job(
        created.job_id,
        retry,
        snapshot=_snapshot(),
    ).state.value == "pending"

    receipt = store.discard_successor_transition_outputs(
        transition_id
    )
    assert receipt == {
        "successor_transition_id": transition_id,
        "discarded_artifact_ids": [],
        "discarded_materialized_context_ids": [],
    }
    claim = store.claim_job(
        WorkerClaimRequest(
            worker_id="verified-skill-worker",
            capabilities=[request.job_type],
            method_capabilities=["skill_bundle_reflector"],
            method_identity_capabilities=(
                _method_identities(request)
            ),
        )
    )
    assert claim.job is None
    with store.connect() as connection:
        job = connection.execute(
            "SELECT state, claimed_by, lease_id FROM jobs "
            "WHERE job_id = ?",
            (created.job_id,),
        ).fetchone()
    assert tuple(job) == ("cancelled", None, None)

    with pytest.raises(ValueError, match="discarded"):
        store.retry_plan_bound_job(
            created.job_id,
            retry,
            snapshot=_snapshot(),
        )
    with pytest.raises(ValueError, match="discarded"):
        store.retry_plan_bound_job(
            created.job_id,
            retry.model_copy(
                update={
                    "retry_request_id": (
                        "new-retry-after-discard"
                    )
                }
            ),
            snapshot=_snapshot(),
        )


def test_empty_discard_authority_survives_restart_and_blocks_late_binding(
    tmp_path,
) -> None:
    store = _store(tmp_path)
    transition_id = "successor-transition-never-staged"
    receipt = {
        "successor_transition_id": transition_id,
        "discarded_artifact_ids": [],
        "discarded_materialized_context_ids": [],
    }

    assert store.discard_successor_transition_outputs(
        transition_id
    ) == receipt
    assert store.discard_successor_transition_outputs(
        transition_id
    ) == receipt

    restarted = EvolutionStore(
        db_path=tmp_path / "evolution.db",
        artifact_root=tmp_path / "artifacts",
    )
    restarted.initialize()
    assert restarted.discard_successor_transition_outputs(
        transition_id
    ) == receipt
    request = _request(restarted).model_copy(
        update={"successor_transition_id": transition_id}
    )
    with pytest.raises(ValueError, match="discarded"):
        restarted.create_plan_bound_job(
            request,
            snapshot=_snapshot(),
        )


def test_restart_adds_discard_authority_to_prior_current_schema(
    tmp_path,
) -> None:
    store = _store(tmp_path)
    with store.connect() as connection:
        connection.execute(
            "DROP TABLE successor_transition_discards"
        )
        connection.commit()

    restarted = EvolutionStore(
        db_path=tmp_path / "evolution.db",
        artifact_root=tmp_path / "artifacts",
    )
    restarted.initialize()
    with restarted.connect() as connection:
        table = connection.execute(
            "SELECT 1 FROM sqlite_schema "
            "WHERE type = 'table' "
            "AND name = 'successor_transition_discards'"
        ).fetchone()
    assert table is not None


@pytest.mark.parametrize(
    ("corruption", "message"),
    [
        ("receipt", "discard receipt"),
        ("active_job", "retained active work"),
    ],
)
def test_restart_rejects_corrupt_discard_authority(
    tmp_path,
    corruption: str,
    message: str,
) -> None:
    store = _store(tmp_path)
    transition_id = "successor-transition-corrupt-discard"
    request = _request(store).model_copy(
        update={"successor_transition_id": transition_id}
    )
    created = store.create_plan_bound_job(
        request,
        snapshot=_snapshot(),
    )
    store.discard_successor_transition_outputs(transition_id)

    with store.connect() as connection:
        if corruption == "receipt":
            connection.execute(
                "UPDATE successor_transition_discards "
                "SET receipt_sha256 = ? "
                "WHERE successor_transition_id = ?",
                ("0" * 64, transition_id),
            )
        else:
            connection.execute(
                "UPDATE jobs SET state = ? WHERE job_id = ?",
                ("pending", created.job_id),
            )
        connection.commit()

    restarted = EvolutionStore(
        db_path=tmp_path / "evolution.db",
        artifact_root=tmp_path / "artifacts",
    )
    with pytest.raises(ValueError, match=message):
        restarted.initialize()


def test_sealed_predecessor_input_requires_exact_transition_authority(
    tmp_path,
) -> None:
    store = _store(tmp_path)
    first_request = _request(store).model_copy(
        update={"successor_transition_id": "successor-transition-1"}
    )
    first = store.create_plan_bound_job(first_request, snapshot=_snapshot())
    claim = store.claim_job(
        WorkerClaimRequest(
            worker_id="verified-skill-worker",
            capabilities=[first_request.job_type],
            method_capabilities=["skill_bundle_reflector"],
            method_identity_capabilities=_method_identities(first_request),
        )
    )
    assert claim.job is not None
    payload = store.files.root / "worker-output" / "skill"
    payload.mkdir(parents=True)
    (payload / "SKILL.md").write_text("# Sealed skill\n", encoding="utf-8")
    completed = store.complete_job(
        first.job_id,
        WorkerCompleteRequest(
            lease_id=claim.job.lease_id,
            artifacts=[
                ArtifactRegisterRequest(
                    type=ArtifactType.SKILL_BUNDLE,
                    name="sealed predecessor skill",
                    uri=payload.as_uri(),
                    manifest={"content_path": "SKILL.md"},
                    promoted=True,
                )
            ],
        ),
    )
    sealed_id = completed["artifact_ids"][0]
    dataset_id = first_request.input_bindings[0].artifact_ids[0]
    second_plan = _snapshot().compile_plan(
        plan_id="plan-skill-round-1",
        selections=tuple(
            EvolutionTargetSelection(
                target_id=selection.target_id,
                enabled=True,
                method_id=selection.method_id,
                config=selection.config(),
            )
            for selection in first_request.plan.selections
        ),
        profile=_profile(),
    )
    base_second = first_request.model_copy(
        update={
            "plan": second_plan,
            "input_bindings": (
                PlannedInputBinding(
                    binding_id="current_dataset",
                    artifact_ids=(dataset_id,),
                ),
                PlannedInputBinding(
                    binding_id="prior_target_artifacts",
                    artifact_ids=(sealed_id,),
                ),
            ),
            "successor_transition_id": "successor-transition-2",
        }
    )

    with pytest.raises(ValueError, match="unknown artifact"):
        store.create_plan_bound_job(
            base_second.model_copy(
                update={"predecessor_successor_transition_id": None}
            ),
            snapshot=_snapshot(),
        )
    with pytest.raises(ValueError, match="unknown artifact"):
        store.create_plan_bound_job(
            base_second.model_copy(
                update={
                    "predecessor_successor_transition_id": (
                        "different-successor-transition"
                    )
                }
            ),
            snapshot=_snapshot(),
        )

    created = store.create_plan_bound_job(
        base_second.model_copy(
            update={
                "predecessor_successor_transition_id": (
                    "successor-transition-1"
                )
            }
        ),
        snapshot=_snapshot(),
    )
    assert created.state.value == "pending"


def test_nonretryable_plan_bound_job_requires_replacement_plan(tmp_path) -> None:
    store = _store(tmp_path)
    request = _request(store)
    created = store.create_plan_bound_job(request, snapshot=_snapshot())
    claim = store.claim_job(
        WorkerClaimRequest(
            worker_id="verified-skill-worker",
            capabilities=[request.job_type],
            method_capabilities=["skill_bundle_reflector"],
            method_identity_capabilities=_method_identities(request),
        )
    )
    assert claim.job is not None
    store.fail_job(
        created.job_id,
        WorkerFailRequest(
            lease_id=claim.job.lease_id,
            error="sanitized deterministic failure",
            retryable=False,
        ),
    )
    retry = PlanBoundJobRetryRequest(
        retry_request_id="transition-attempt-2",
        plan_id=request.plan.plan_id,
        target_id=request.target_id,
    )

    with pytest.raises(ValueError, match="replacement plan"):
        store.retry_plan_bound_job(
            created.job_id,
            retry,
            snapshot=_snapshot(),
        )
    with store.connect() as connection:
        row = connection.execute(
            "SELECT attempt_count, error FROM jobs WHERE job_id = ?",
            (created.job_id,),
        ).fetchone()
    assert row["attempt_count"] == 1
    assert row["error"] == "sanitized deterministic failure"


def test_plan_bound_claim_filters_loaded_methods_and_survives_restart(tmp_path) -> None:
    store = _store(tmp_path)
    request = _request(store)
    created = store.create_plan_bound_job(request, snapshot=_snapshot())

    incompatible = store.claim_job(
        WorkerClaimRequest(
            worker_id="wrong-worker",
            capabilities=[request.job_type],
            method_capabilities=["text_memory_expel_reflector"],
        )
    )
    assert incompatible.job is None

    wrong_identity = store.claim_job(
        WorkerClaimRequest(
            worker_id="stale-skill-worker",
            capabilities=[request.job_type],
            method_capabilities=["skill_bundle_reflector"],
            method_identity_capabilities={"skill_bundle_reflector": "f" * 64},
        )
    )
    assert wrong_identity.job is None

    restarted = EvolutionStore(
        db_path=tmp_path / "evolution.db",
        artifact_root=tmp_path / "artifacts",
        registry_snapshot=_snapshot(),
    )
    restarted.initialize()
    claim = restarted.claim_job(
        WorkerClaimRequest(
            worker_id="skill-worker",
            capabilities=[request.job_type],
            method_capabilities=["skill_bundle_reflector"],
            method_identity_capabilities=_method_identities(request),
        )
    )

    assert claim.job is not None
    assert claim.job.job_id == created.job_id
    assert claim.job.plan == request.plan.model_dump(mode="json")
    assert claim.job.target_id == "skill_bundle"
    assert claim.job.registry_snapshot_digest == request.plan.registry_snapshot_digest
    assert (
        claim.job.method_identity_digest
        == request.plan.selections[0].method_identity_digest
    )
    envelope = MethodExecutionEnvelope.model_validate(claim.job.execution_envelope)
    assert [artifact.artifact_id for artifact in claim.job.input_artifacts] == list(
        envelope.input_artifact_ids()
    )
    assert claim.job.config == envelope.legacy_flat_config()


def test_plan_bound_worker_rejects_dataset_changed_after_claim(tmp_path) -> None:
    store = _store(tmp_path)
    request, dataset_id = _request_with_sealed_dataset(store)
    store.create_plan_bound_job(request, snapshot=_snapshot())
    claim = store.claim_job(
        WorkerClaimRequest(
            worker_id="verified-skill-worker",
            capabilities=[request.job_type],
            method_capabilities=["skill_bundle_reflector"],
            method_identity_capabilities=_method_identities(request),
        )
    )
    assert claim.job is not None
    dataset_artifact = next(
        artifact
        for artifact in claim.job.input_artifacts
        if artifact.type == ArtifactType.DATASET
    )
    assert dataset_artifact.manifest_sha256 is not None

    records_path = store.files.dataset_manifest_path(dataset_id).with_name(
        "records.jsonl"
    )
    records_path.write_text(
        '{"event_id":"forged-after-claim","trace_count":0}\n',
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="dataset records"):
        run_method(claim.job, artifact_root=tmp_path / "worker-artifacts")


def test_base_layout_dataset_and_pending_plan_job_survive_upgrade(tmp_path) -> None:
    store = _store(tmp_path)
    request, dataset_id = _request_with_sealed_dataset(
        store,
        idempotency_key=None,
    )
    with store.connect() as connection:
        dataset_row = connection.execute(
            "SELECT * FROM datasets WHERE dataset_id = ?",
            (dataset_id,),
        ).fetchone()
        artifact_row = connection.execute(
            "SELECT * FROM artifacts WHERE artifact_id = ?",
            (dataset_row["artifact_id"],),
        ).fetchone()
    manifest_path = Path(str(dataset_row["manifest_path"]))
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest.pop("records_byte_size")
    manifest.pop("records_sha256")
    manifest_path.write_text(
        json.dumps(manifest, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    records_path = manifest_path.with_name("records.jsonl")
    manifest_path.chmod(0o644)
    records_path.chmod(0o644)
    artifact_manifest_path = Path(str(artifact_row["manifest_path"]))
    artifact_wrapper = json.loads(
        artifact_manifest_path.read_text(encoding="utf-8")
    )
    artifact_wrapper["manifest"] = manifest
    artifact_manifest_path.write_text(
        json.dumps(artifact_wrapper, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    with store.connect() as connection:
        connection.execute(
            "UPDATE artifacts SET manifest_json = ? WHERE artifact_id = ?",
            (
                json.dumps(manifest, sort_keys=True),
                dataset_row["artifact_id"],
            ),
        )
        connection.commit()

    created = store.create_plan_bound_job(request, snapshot=_snapshot())
    restarted = EvolutionStore(
        db_path=tmp_path / "evolution.db",
        artifact_root=tmp_path / "artifacts",
        registry_snapshot=_snapshot(),
    )
    restarted.initialize()
    assert manifest_path.stat().st_mode & 0o777 == 0o600
    assert records_path.stat().st_mode & 0o777 == 0o600
    claim = restarted.claim_job(
        WorkerClaimRequest(
            worker_id="upgraded-skill-worker",
            capabilities=[request.job_type],
            method_capabilities=["skill_bundle_reflector"],
            method_identity_capabilities=_method_identities(request),
        )
    )

    assert claim.job is not None
    assert claim.job.job_id == created.job_id
    dataset_artifact = next(
        artifact
        for artifact in claim.job.input_artifacts
        if artifact.artifact_id == dataset_row["artifact_id"]
    )
    assert dataset_artifact.manifest_sha256 is not None
    assert dataset_artifact.records_byte_size is not None
    assert dataset_artifact.records_sha256 is not None
    worker_manifest, worker_records = _read_dataset_artifact(dataset_artifact)
    assert worker_manifest["dataset_id"] == dataset_id
    assert [record["event_id"] for record in worker_records] == manifest["event_ids"]


def test_startup_rejects_group_writable_dataset_without_chmod(tmp_path) -> None:
    store = _store(tmp_path)
    _request_value, dataset_id = _request_with_sealed_dataset(store)
    manifest_path = store.files.dataset_manifest_path(dataset_id)
    manifest_path.chmod(0o660)

    restarted = EvolutionStore(
        db_path=tmp_path / "evolution.db",
        artifact_root=tmp_path / "artifacts",
        registry_snapshot=_snapshot(),
    )
    with pytest.raises(DatasetIntegrityError, match="group/other writable"):
        restarted.initialize()

    assert manifest_path.stat().st_mode & 0o777 == 0o660


def test_legacy_dataset_mode_migration_rechecks_path_before_chmod(
    tmp_path,
    monkeypatch,
) -> None:
    store = _store(tmp_path)
    _request_value, dataset_id = _request_with_sealed_dataset(store)
    manifest_path = store.files.dataset_manifest_path(dataset_id)
    records_path = manifest_path.with_name("records.jsonl")
    original_bytes = manifest_path.read_bytes()
    manifest_path.chmod(0o644)
    records_path.chmod(0o644)
    moved_name = "manifest-before-mode-migration.json"

    def replace_before_fchmod(
        directory_descriptor: int,
        observed_dataset_id: str,
        name: str,
        descriptor: int,
    ) -> None:
        del descriptor
        assert observed_dataset_id == dataset_id
        assert name == manifest_path.name
        os.rename(
            name,
            moved_name,
            src_dir_fd=directory_descriptor,
            dst_dir_fd=directory_descriptor,
        )
        replacement_descriptor = os.open(
            name,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC,
            0o644,
            dir_fd=directory_descriptor,
        )
        try:
            assert os.write(replacement_descriptor, original_bytes) == len(
                original_bytes
            )
            os.fchmod(replacement_descriptor, 0o644)
            os.fsync(replacement_descriptor)
        finally:
            os.close(replacement_descriptor)

    monkeypatch.setattr(
        store_module,
        "_before_legacy_dataset_fchmod",
        replace_before_fchmod,
    )
    restarted = EvolutionStore(
        db_path=tmp_path / "evolution.db",
        artifact_root=tmp_path / "artifacts",
        registry_snapshot=_snapshot(),
    )
    with pytest.raises(DatasetIntegrityError, match="changed before chmod"):
        restarted.initialize()

    moved_path = manifest_path.with_name(moved_name)
    assert moved_path.read_bytes() == original_bytes
    assert moved_path.stat().st_mode & 0o777 == 0o644
    assert manifest_path.read_bytes() == original_bytes
    assert manifest_path.stat().st_mode & 0o777 == 0o644
    assert records_path.stat().st_mode & 0o777 == 0o644


def test_legacy_dataset_mode_migration_rechecks_dataset_root_binding(
    tmp_path,
    monkeypatch,
) -> None:
    store = _store(tmp_path)
    _request_value, dataset_id = _request_with_sealed_dataset(store)
    manifest_path = store.files.dataset_manifest_path(dataset_id)
    records_path = manifest_path.with_name("records.jsonl")
    manifest_bytes = manifest_path.read_bytes()
    records_bytes = records_path.read_bytes()
    manifest_path.chmod(0o644)
    records_path.chmod(0o644)
    dataset_root = manifest_path.parents[1]
    moved_root = dataset_root.with_name(
        "datasets-before-mode-migration"
    )
    rebound = False

    def rebind_dataset_root(
        directory_descriptor: int,
        observed_dataset_id: str,
        name: str,
        descriptor: int,
    ) -> None:
        nonlocal rebound
        del directory_descriptor, descriptor
        if rebound:
            return
        assert observed_dataset_id == dataset_id
        assert name == manifest_path.name
        rebound = True
        dataset_root.rename(moved_root)
        replacement_directory = dataset_root / dataset_id
        replacement_directory.mkdir(parents=True)
        dataset_root.chmod(0o755)
        replacement_directory.chmod(0o755)
        replacement_manifest = replacement_directory / "manifest.json"
        replacement_records = replacement_directory / "records.jsonl"
        replacement_manifest.write_bytes(manifest_bytes)
        replacement_records.write_bytes(records_bytes)
        replacement_manifest.chmod(0o644)
        replacement_records.chmod(0o644)

    monkeypatch.setattr(
        store_module,
        "_before_legacy_dataset_fchmod",
        rebind_dataset_root,
    )
    restarted = EvolutionStore(
        db_path=tmp_path / "evolution.db",
        artifact_root=tmp_path / "artifacts",
        registry_snapshot=_snapshot(),
    )
    with pytest.raises(
        DatasetIntegrityError,
        match="dataset materialization root binding changed",
    ):
        restarted.initialize()

    detached_directory = moved_root / dataset_id
    assert (
        detached_directory.joinpath("manifest.json").stat().st_mode
        & 0o777
        == 0o600
    )
    assert (
        detached_directory.joinpath("records.jsonl").stat().st_mode
        & 0o777
        == 0o600
    )
    assert manifest_path.stat().st_mode & 0o777 == 0o644
    assert records_path.stat().st_mode & 0o777 == 0o644


def test_unverified_worker_claims_legacy_job_without_consuming_planned_job(
    tmp_path,
) -> None:
    store = _store(tmp_path)
    request = _request(store)
    planned = store.create_plan_bound_job(request, snapshot=_snapshot())
    legacy = store.create_job(
        JobCreateRequest(
            method="legacy-method",
            job_type=request.job_type,
            priority=1,
        )
    )

    claim = store.claim_job(
        WorkerClaimRequest(
            worker_id="legacy-worker",
            capabilities=[request.job_type],
        )
    )

    assert claim.job is not None
    assert claim.job.job_id == legacy.job_id
    assert claim.job.plan is None
    with store.connect() as connection:
        planned_row = connection.execute(
            "SELECT state, lease_id FROM jobs WHERE job_id = ?",
            (planned.job_id,),
        ).fetchone()
    assert planned_row["state"] == "pending"
    assert planned_row["lease_id"] is None


def test_plan_bound_complete_rejects_undeclared_output_type(tmp_path) -> None:
    store = _store(tmp_path)
    request = _request(store)
    created = store.create_plan_bound_job(request, snapshot=_snapshot())
    claim = store.claim_job(
        WorkerClaimRequest(
            worker_id="verified-skill-worker",
            capabilities=[request.job_type],
            method_capabilities=["skill_bundle_reflector"],
            method_identity_capabilities=_method_identities(request),
        )
    )
    assert claim.job is not None

    with pytest.raises(ValueError, match="undeclared artifact type"):
        store.complete_job(
            created.job_id,
            WorkerCompleteRequest(
                lease_id=claim.job.lease_id,
                artifacts=[
                    ArtifactRegisterRequest(
                        type=ArtifactType.TEXT_MEMORY,
                        name="wrong-output",
                        uri="file:///tmp/wrong-output",
                    )
                ],
            ),
        )

    with store.connect() as connection:
        row = connection.execute(
            "SELECT state, lease_id FROM jobs WHERE job_id = ?",
            (created.job_id,),
        ).fetchone()
    assert row["state"] == "claimed"
    assert row["lease_id"] == claim.job.lease_id


def test_completion_cleanup_preserves_artifact_referenced_by_another_job(
    tmp_path,
) -> None:
    store = _store(tmp_path)
    artifact = _artifact(store, ArtifactType.SKILL_BUNDLE, "shared-output")
    store.create_job(
        JobCreateRequest(
            method="legacy-consumer",
            job_type="legacy-consumer",
            input_artifact_ids=[artifact.artifact_id],
        )
    )

    store._cleanup_registered_artifacts([artifact.artifact_id])

    assert store.get_artifact(artifact.artifact_id).artifact_id == artifact.artifact_id


def test_plan_bound_job_rejects_tampered_plan_before_writing(tmp_path) -> None:
    store = _store(tmp_path)
    request = _request(store)
    payload = request.model_dump(mode="python")
    payload["plan"]["selections"][0]["method_identity_digest"] = "f" * 64
    tampered = PlanBoundJobCreateRequest.model_validate(payload)

    with pytest.raises(ValueError, match="plan.*registry|identity"):
        store.create_plan_bound_job(tampered, snapshot=_snapshot())

    with store.connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM evolution_plans").fetchone()[0] == 0
        assert connection.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 0


def test_plan_id_cannot_be_reused_for_different_plan_content(tmp_path) -> None:
    store = _store(tmp_path)
    request = _request(store)
    store.create_plan_bound_job(request, snapshot=_snapshot())

    changed_plan_payload = request.plan.model_dump(mode="python")
    changed_plan_payload["execution_profile"]["capture_mode"] = "token_level"
    changed_plan = EvolutionPlan.model_validate(changed_plan_payload)
    changed = request.model_copy(update={"plan": changed_plan})

    with pytest.raises(ValueError, match="plan_id.*different plan"):
        store.create_plan_bound_job(changed, snapshot=_snapshot())


def test_schema_migration_preserves_unbound_legacy_jobs_without_fabricated_identity(
    tmp_path,
) -> None:
    db_path = tmp_path / "evolution.db"
    connection = sqlite3.connect(db_path)
    connection.executescript(
        """
        CREATE TABLE jobs (
            job_id TEXT PRIMARY KEY,
            job_type TEXT NOT NULL,
            method TEXT NOT NULL,
            state TEXT NOT NULL,
            priority INTEGER NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            claimed_by TEXT,
            lease_id TEXT,
            lease_expires_at TEXT,
            input_artifact_ids_json TEXT NOT NULL,
            config_json TEXT NOT NULL,
            error TEXT,
            attempt_count INTEGER NOT NULL
        );
        INSERT INTO jobs VALUES (
            'job-old', 'legacy-capability', 'legacy-method', 'pending', 100,
            '2026-01-01T00:00:00Z', '2026-01-01T00:00:00Z', NULL, NULL, NULL,
            '[]', '{}', NULL, 0
        );
        """
    )
    connection.commit()
    connection.close()

    store = EvolutionStore(db_path=db_path, artifact_root=tmp_path / "artifacts")
    store.initialize()

    with store.connect() as migrated:
        row = migrated.execute("SELECT * FROM jobs WHERE job_id = 'job-old'").fetchone()
    assert row["plan_id"] is None
    assert row["target_id"] is None
    assert row["method_identity_digest"] is None
    assert row["execution_envelope_json"] is None
    assert row["execution_envelope_digest"] is None
    assert row["declared_output_artifact_types_json"] is None
    with store.connect() as migrated:
        artifact_columns = {
            column["name"]
            for column in migrated.execute("PRAGMA table_info(artifacts)").fetchall()
        }
    assert "staging_job_id" in artifact_columns

    claim = store.claim_job(
        WorkerClaimRequest(
            worker_id="legacy-worker",
            capabilities=["legacy-capability"],
        )
    )
    assert claim.job is not None
    assert claim.job.job_id == "job-old"
    assert claim.job.plan is None
    assert claim.job.execution_envelope is None


def test_planned_job_api_requires_and_uses_the_active_registry(tmp_path) -> None:
    unavailable_app = create_app(
        db_path=tmp_path / "unavailable.db",
        artifact_root=tmp_path / "unavailable-artifacts",
    )
    unavailable_store = unavailable_app.state.store
    unavailable_request = _request(unavailable_store)
    unavailable = TestClient(unavailable_app).post(
        "/v1/planned-jobs",
        json=unavailable_request.model_dump(mode="json"),
    )
    assert unavailable.status_code == 503

    app = create_app(
        db_path=tmp_path / "evolution-api.db",
        artifact_root=tmp_path / "api-artifacts",
        registry_snapshot=_snapshot(),
    )
    request = _request(app.state.store)
    response = TestClient(app).post(
        "/v1/planned-jobs",
        json=request.model_dump(mode="json"),
    )

    assert response.status_code == 200
    assert response.json()["job_id"].startswith("job_")


def test_planned_job_api_uses_snapshot_from_executable_registry(tmp_path) -> None:
    registry = verified_builtin_registry(tmp_path / "verified-registry")
    app = create_app(
        db_path=tmp_path / "evolution-api.db",
        artifact_root=tmp_path / "api-artifacts",
        executable_registry=registry,
    )
    request = _request(app.state.store)

    response = TestClient(app).post(
        "/v1/planned-jobs",
        json=request.model_dump(mode="json"),
    )

    assert response.status_code == 422
    assert "active verified registry" not in response.text
