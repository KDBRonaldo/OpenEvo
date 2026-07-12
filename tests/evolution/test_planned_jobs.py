from __future__ import annotations

import json
import sqlite3

import pytest
from fastapi.testclient import TestClient

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
    JobCreateRequest,
    WorkerClaimRequest,
    WorkerCompleteRequest,
)
from openevo.evolution.planned_jobs import (
    PlanBoundJobCreateRequest,
    PlannedInputBinding,
)
from openevo.evolution.store import EvolutionStore
from openevo.evolution.server import create_app


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


def _method_identities(request: PlanBoundJobCreateRequest) -> dict[str, str]:
    selection = request.plan.selections[0]
    return {selection.method_id: selection.method_identity_digest}


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
