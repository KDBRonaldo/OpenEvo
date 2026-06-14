from __future__ import annotations

from datetime import UTC, datetime, timedelta
import json
import math
from pathlib import Path
import sqlite3

import pytest

import polar_evolution.store as store_module
from polar_evolution.models import (
    ArtifactRegisterRequest,
    ArtifactType,
    DatasetCreateRequest,
    EventIngestRequest,
    JobCreateRequest,
    JobState,
    WorkerClaimRequest,
    WorkerCompleteRequest,
    WorkerFailRequest,
    WorkerHeartbeatRequest,
)
from polar_evolution.store import EvolutionStore


def _parse_utc(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def test_create_dataset_filters_events(tmp_path):
    store = EvolutionStore(db_path=tmp_path / "evolution.db", artifact_root=tmp_path / "artifacts")
    store.initialize()
    good_event = store.ingest_event(
        EventIngestRequest(
            source="polar",
            event_type="polar.session_completed",
            source_event_id="session:good",
            task_id="task_good",
            status="COMPLETED",
            reward=1.0,
            policy_version="policy_1",
            payload={"session_result": {"trajectory": {"traces": [{"reward": 1.0}]}}},
        )
    )
    store.ingest_event(
        EventIngestRequest(
            source="polar",
            event_type="polar.session_completed",
            source_event_id="session:bad",
            task_id="task_bad",
            status="ERROR",
            reward=0.0,
            policy_version="policy_1",
            payload={"session_result": {"trajectory": {"traces": []}}},
        )
    )

    response = store.create_dataset(
        DatasetCreateRequest(
            name="good_policy_1",
            purpose="skill_distillation",
            query={
                "event_types": ["polar.session_completed"],
                "status": ["COMPLETED"],
                "reward_min": 0.8,
                "policy_version": "policy_1",
            },
        )
    )

    assert response.dataset_id.startswith("ds_")
    assert response.artifact_id.startswith("art_")
    assert response.event_count == 1
    assert response.trace_count == 1

    with store.connect() as conn:
        dataset_row = conn.execute(
            "SELECT * FROM datasets WHERE dataset_id = ?",
            (response.dataset_id,),
        ).fetchone()
        dataset_event_rows = conn.execute(
            "SELECT event_id FROM dataset_events WHERE dataset_id = ?",
            (response.dataset_id,),
        ).fetchall()
        artifact_row = conn.execute(
            "SELECT * FROM artifacts WHERE artifact_id = ?",
            (response.artifact_id,),
        ).fetchone()

    assert dataset_row is not None
    assert dataset_row["name"] == "good_policy_1"
    assert dataset_row["purpose"] == "skill_distillation"
    assert dataset_row["state"] == "active"
    assert json.loads(dataset_row["query_json"]) == {
        "event_types": ["polar.session_completed"],
        "policy_version": "policy_1",
        "reward_min": 0.8,
        "status": ["COMPLETED"],
        "task_tags": [],
    }
    assert dataset_row["event_count"] == 1
    assert dataset_row["trace_count"] == 1
    assert dataset_row["artifact_id"] == response.artifact_id
    assert [row["event_id"] for row in dataset_event_rows] == [good_event.event_id]

    manifest_path = Path(dataset_row["manifest_path"])
    assert manifest_path == (
        tmp_path / "artifacts" / "datasets" / response.dataset_id / "manifest.json"
    ).resolve()
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert manifest == {
        "dataset_id": response.dataset_id,
        "name": "good_policy_1",
        "purpose": "skill_distillation",
        "query": {
            "event_types": ["polar.session_completed"],
            "policy_version": "policy_1",
            "reward_min": 0.8,
            "status": ["COMPLETED"],
            "task_tags": [],
        },
        "limits": {"max_events": 10000, "max_traces": 50000},
        "event_ids": [good_event.event_id],
        "event_count": 1,
        "trace_count": 1,
    }

    assert artifact_row is not None
    assert artifact_row["type"] == "dataset"
    assert artifact_row["name"] == "good_policy_1"
    assert artifact_row["uri"] == manifest_path.as_uri()
    assert json.loads(artifact_row["lineage_json"]) == {"event_ids": [good_event.event_id]}
    assert json.loads(artifact_row["compatibility_json"]) == {"purpose": "skill_distillation"}
    assert json.loads(artifact_row["tags_json"]) == ["skill_distillation"]
    assert artifact_row["promoted"] == 1


def test_create_dataset_rejects_non_finite_query_without_writes(tmp_path):
    store = EvolutionStore(db_path=tmp_path / "evolution.db", artifact_root=tmp_path / "artifacts")
    store.initialize()

    with pytest.raises(ValueError, match="non-finite float at query.reward_min"):
        store.create_dataset(
            DatasetCreateRequest(
                name="invalid",
                purpose="skill_distillation",
                query={"reward_min": math.nan},
            )
        )

    with store.connect() as conn:
        dataset_count = conn.execute("SELECT COUNT(*) FROM datasets").fetchone()[0]
        artifact_count = conn.execute("SELECT COUNT(*) FROM artifacts").fetchone()[0]

    assert dataset_count == 0
    assert artifact_count == 0
    assert not list((tmp_path / "artifacts" / "datasets").glob("*/manifest.json"))


def test_create_dataset_rejects_task_tags_query_without_writes(tmp_path):
    store = EvolutionStore(db_path=tmp_path / "evolution.db", artifact_root=tmp_path / "artifacts")
    store.initialize()
    store.ingest_event(
        EventIngestRequest(
            source="polar",
            event_type="polar.session_completed",
            source_event_id="session:tagged",
            status="COMPLETED",
            payload={"session_result": {"trajectory": {"traces": [{"reward": 1.0}]}}},
        )
    )

    with pytest.raises(ValueError, match="task_tags.*not supported"):
        store.create_dataset(
            DatasetCreateRequest(
                name="tagged",
                purpose="skill_distillation",
                query={"task_tags": ["calculator"]},
            )
        )

    with store.connect() as conn:
        dataset_count = conn.execute("SELECT COUNT(*) FROM datasets").fetchone()[0]
        dataset_event_count = conn.execute("SELECT COUNT(*) FROM dataset_events").fetchone()[0]
        artifact_count = conn.execute("SELECT COUNT(*) FROM artifacts").fetchone()[0]

    assert dataset_count == 0
    assert dataset_event_count == 0
    assert artifact_count == 0
    assert not list((tmp_path / "artifacts" / "datasets").glob("*/manifest.json"))
    assert not list((tmp_path / "artifacts" / "artifacts" / "datasets").glob("*/manifest.json"))


def test_create_dataset_retries_id_collision_without_overwriting_manifest(tmp_path, monkeypatch):
    store = EvolutionStore(db_path=tmp_path / "evolution.db", artifact_root=tmp_path / "artifacts")
    store.initialize()
    store.ingest_event(
        EventIngestRequest(
            source="polar",
            event_type="polar.session_completed",
            source_event_id="session:one",
            status="COMPLETED",
            payload={"session_result": {"trajectory": {"traces": [{"reward": 1.0}]}}},
        )
    )
    dataset_ids = iter(["ds_collision", "ds_collision", "ds_retry"])
    artifact_ids = iter(["art_first", "art_second"])

    def fake_new_id(prefix: str) -> str:
        if prefix == "ds":
            return next(dataset_ids)
        if prefix == "art":
            return next(artifact_ids)
        return f"{prefix}_generated"

    monkeypatch.setattr(store_module, "new_id", fake_new_id)

    first_response = store.create_dataset(
        DatasetCreateRequest(name="first", purpose="skill_distillation")
    )
    first_manifest_path = tmp_path / "artifacts" / "datasets" / "ds_collision" / "manifest.json"
    first_manifest_before = first_manifest_path.read_text(encoding="utf-8")

    second_response = store.create_dataset(
        DatasetCreateRequest(name="second", purpose="skill_distillation")
    )

    assert first_response.dataset_id == "ds_collision"
    assert second_response.dataset_id == "ds_retry"
    assert first_manifest_path.read_text(encoding="utf-8") == first_manifest_before
    assert json.loads(first_manifest_before)["name"] == "first"
    second_manifest_path = tmp_path / "artifacts" / "datasets" / "ds_retry" / "manifest.json"
    assert json.loads(second_manifest_path.read_text(encoding="utf-8"))["name"] == "second"


def test_create_dataset_stops_selecting_events_after_trace_limit(tmp_path):
    store = EvolutionStore(db_path=tmp_path / "evolution.db", artifact_root=tmp_path / "artifacts")
    store.initialize()
    first_event = store.ingest_event(
        EventIngestRequest(
            source="polar",
            event_type="polar.session_completed",
            source_event_id="session:first",
            status="COMPLETED",
            payload={
                "session_result": {
                    "trajectory": {"traces": [{"reward": 1.0}, {"reward": 0.9}]}
                }
            },
        )
    )
    second_event = store.ingest_event(
        EventIngestRequest(
            source="polar",
            event_type="polar.session_completed",
            source_event_id="session:second",
            status="COMPLETED",
            payload={"session_result": {"trajectory": {"traces": [{"reward": 0.8}]}}},
        )
    )
    with store.connect() as conn:
        conn.execute(
            "UPDATE events SET ingested_at = ? WHERE event_id = ?",
            ("2026-06-14T00:00:00Z", first_event.event_id),
        )
        conn.execute(
            "UPDATE events SET ingested_at = ? WHERE event_id = ?",
            ("2026-06-14T00:00:01Z", second_event.event_id),
        )
        conn.commit()

    response = store.create_dataset(
        DatasetCreateRequest(
            name="limited",
            purpose="skill_distillation",
            limits={"max_traces": 2},
        )
    )

    with store.connect() as conn:
        dataset_row = conn.execute(
            "SELECT * FROM datasets WHERE dataset_id = ?",
            (response.dataset_id,),
        ).fetchone()
        dataset_event_rows = conn.execute(
            "SELECT event_id FROM dataset_events WHERE dataset_id = ? ORDER BY event_id",
            (response.dataset_id,),
        ).fetchall()

    manifest_path = Path(dataset_row["manifest_path"])
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert response.event_count == 1
    assert response.trace_count == 2
    assert manifest["event_ids"] == [first_event.event_id]
    assert manifest["trace_count"] == 2
    assert [row["event_id"] for row in dataset_event_rows] == [first_event.event_id]


def test_create_dataset_keeps_single_event_that_exceeds_trace_limit(tmp_path):
    store = EvolutionStore(db_path=tmp_path / "evolution.db", artifact_root=tmp_path / "artifacts")
    store.initialize()
    event = store.ingest_event(
        EventIngestRequest(
            source="polar",
            event_type="polar.session_completed",
            source_event_id="session:oversized",
            status="COMPLETED",
            payload={
                "session_result": {
                    "trajectory": {
                        "traces": [{"reward": 1.0}, {"reward": 0.9}, {"reward": 0.8}]
                    }
                }
            },
        )
    )

    response = store.create_dataset(
        DatasetCreateRequest(
            name="oversized",
            purpose="skill_distillation",
            limits={"max_traces": 1},
        )
    )

    with store.connect() as conn:
        dataset_row = conn.execute(
            "SELECT * FROM datasets WHERE dataset_id = ?",
            (response.dataset_id,),
        ).fetchone()

    manifest = json.loads(Path(dataset_row["manifest_path"]).read_text(encoding="utf-8"))
    assert response.event_count == 1
    assert response.trace_count == 3
    assert manifest["event_ids"] == [event.event_id]
    assert manifest["trace_count"] == 3


def test_create_dataset_cleans_up_dataset_and_artifact_when_backfill_fails(
    tmp_path,
    monkeypatch,
):
    store = EvolutionStore(db_path=tmp_path / "evolution.db", artifact_root=tmp_path / "artifacts")
    store.initialize()
    store.ingest_event(
        EventIngestRequest(
            source="polar",
            event_type="polar.session_completed",
            source_event_id="session:backfill",
            status="COMPLETED",
            payload={"session_result": {"trajectory": {"traces": [{"reward": 1.0}]}}},
        )
    )

    def fake_new_id(prefix: str) -> str:
        if prefix == "ds":
            return "ds_backfill_failure"
        if prefix == "art":
            return "art_backfill_failure"
        return f"{prefix}_generated"

    monkeypatch.setattr(store_module, "new_id", fake_new_id)
    with store.connect() as conn:
        conn.execute(
            """
            CREATE TRIGGER datasets_artifact_backfill_failure
            BEFORE UPDATE OF artifact_id ON datasets
            WHEN NEW.artifact_id IS NOT NULL
            BEGIN
                SELECT RAISE(ABORT, 'forced artifact backfill failure');
            END;
            """
        )
        conn.execute(
            """
            CREATE TRIGGER artifact_lineage_for_failed_dataset
            AFTER INSERT ON artifacts
            WHEN NEW.artifact_id = 'art_backfill_failure'
            BEGIN
                INSERT INTO artifact_lineage (
                    parent_artifact_id, child_artifact_id, relation
                )
                VALUES ('art_parent', NEW.artifact_id, 'test_relation');
            END;
            """
        )
        conn.commit()

    with pytest.raises(sqlite3.DatabaseError, match="forced artifact backfill failure"):
        store.create_dataset(DatasetCreateRequest(name="failed", purpose="skill_distillation"))

    dataset_manifest_path = (
        tmp_path / "artifacts" / "datasets" / "ds_backfill_failure" / "manifest.json"
    )
    artifact_manifest_path = (
        tmp_path
        / "artifacts"
        / "artifacts"
        / "datasets"
        / "art_backfill_failure"
        / "manifest.json"
    )
    with store.connect() as conn:
        dataset_count = conn.execute(
            "SELECT COUNT(*) FROM datasets WHERE dataset_id = 'ds_backfill_failure'"
        ).fetchone()[0]
        dataset_event_count = conn.execute(
            "SELECT COUNT(*) FROM dataset_events WHERE dataset_id = 'ds_backfill_failure'"
        ).fetchone()[0]
        artifact_count = conn.execute(
            "SELECT COUNT(*) FROM artifacts WHERE artifact_id = 'art_backfill_failure'"
        ).fetchone()[0]
        artifact_lineage_count = conn.execute(
            """
            SELECT COUNT(*) FROM artifact_lineage
            WHERE parent_artifact_id = 'art_backfill_failure'
               OR child_artifact_id = 'art_backfill_failure'
            """
        ).fetchone()[0]

    assert dataset_count == 0
    assert dataset_event_count == 0
    assert artifact_count == 0
    assert artifact_lineage_count == 0
    assert not dataset_manifest_path.exists()
    assert not artifact_manifest_path.exists()


def test_create_dataset_counts_malformed_trace_shape_as_zero(tmp_path):
    store = EvolutionStore(db_path=tmp_path / "evolution.db", artifact_root=tmp_path / "artifacts")
    store.initialize()
    store.ingest_event(
        EventIngestRequest(
            source="polar",
            event_type="polar.session_completed",
            source_event_id="session:malformed",
            status="COMPLETED",
            payload={"session_result": {"trajectory": {"traces": {"unexpected": "mapping"}}}},
        )
    )

    response = store.create_dataset(DatasetCreateRequest(name="malformed", purpose="distill"))

    assert response.event_count == 1
    assert response.trace_count == 0


def test_create_dataset_rejects_missing_payload_file_before_writes(tmp_path):
    store = EvolutionStore(db_path=tmp_path / "evolution.db", artifact_root=tmp_path / "artifacts")
    store.initialize()
    event = store.ingest_event(
        EventIngestRequest(
            source="polar",
            event_type="polar.session_completed",
            source_event_id="session:missing",
            status="COMPLETED",
            payload={"session_result": {"trajectory": {"traces": [{"reward": 1.0}]}}},
        )
    )
    with store.connect() as conn:
        payload_path = Path(
            conn.execute(
                "SELECT payload_path FROM events WHERE event_id = ?",
                (event.event_id,),
            ).fetchone()["payload_path"]
        )
    payload_path.unlink()

    with pytest.raises(ValueError, match=rf"{event.event_id}.*payload file is missing"):
        store.create_dataset(DatasetCreateRequest(name="missing", purpose="distill"))

    with store.connect() as conn:
        dataset_count = conn.execute("SELECT COUNT(*) FROM datasets").fetchone()[0]
        dataset_event_count = conn.execute("SELECT COUNT(*) FROM dataset_events").fetchone()[0]
        artifact_count = conn.execute("SELECT COUNT(*) FROM artifacts").fetchone()[0]

    assert dataset_count == 0
    assert dataset_event_count == 0
    assert artifact_count == 0
    assert not list((tmp_path / "artifacts" / "datasets").glob("*/manifest.json"))
    assert not list((tmp_path / "artifacts" / "artifacts" / "datasets").glob("*/manifest.json"))


def test_create_dataset_rejects_corrupt_payload_json_before_writes(tmp_path):
    store = EvolutionStore(db_path=tmp_path / "evolution.db", artifact_root=tmp_path / "artifacts")
    store.initialize()
    event = store.ingest_event(
        EventIngestRequest(
            source="polar",
            event_type="polar.session_completed",
            source_event_id="session:corrupt",
            status="COMPLETED",
            payload={"session_result": {"trajectory": {"traces": [{"reward": 1.0}]}}},
        )
    )
    with store.connect() as conn:
        payload_path = Path(
            conn.execute(
                "SELECT payload_path FROM events WHERE event_id = ?",
                (event.event_id,),
            ).fetchone()["payload_path"]
        )
    payload_path.write_text("{not valid json", encoding="utf-8")

    with pytest.raises(ValueError, match=rf"{event.event_id}.*payload file is not valid JSON"):
        store.create_dataset(DatasetCreateRequest(name="corrupt", purpose="distill"))

    with store.connect() as conn:
        dataset_count = conn.execute("SELECT COUNT(*) FROM datasets").fetchone()[0]
        dataset_event_count = conn.execute("SELECT COUNT(*) FROM dataset_events").fetchone()[0]
        artifact_count = conn.execute("SELECT COUNT(*) FROM artifacts").fetchone()[0]

    assert dataset_count == 0
    assert dataset_event_count == 0
    assert artifact_count == 0
    assert not list((tmp_path / "artifacts" / "datasets").glob("*/manifest.json"))
    assert not list((tmp_path / "artifacts" / "artifacts" / "datasets").glob("*/manifest.json"))


def test_job_claim_heartbeat_and_complete(tmp_path):
    store = EvolutionStore(db_path=tmp_path / "evolution.db", artifact_root=tmp_path / "artifacts")
    store.initialize()
    dataset = store.register_artifact(
        ArtifactRegisterRequest(
            type=ArtifactType.DATASET,
            name="dataset",
            uri="file:///tmp/dataset.json",
            promoted=True,
        )
    )
    job = store.create_job(
        JobCreateRequest(
            method="mock_lora",
            job_type="parametric_memory_train",
            input_artifact_ids=[dataset.artifact_id],
            config={"base_model": "Qwen/Qwen3.6-27B"},
        )
    )

    claim = store.claim_job(
        WorkerClaimRequest(
            worker_id="worker_1",
            capabilities=["parametric_memory_train"],
            lease_seconds=60,
        )
    )
    assert claim.job is not None
    assert claim.job.job_id == job.job_id
    assert claim.job.state == JobState.CLAIMED
    assert claim.job.priority == 100
    assert claim.job.input_artifacts[0].artifact_id == dataset.artifact_id
    assert claim.job.input_artifacts[0].name == "dataset"
    lease_id = claim.job.lease_id

    store.heartbeat_job(job.job_id, WorkerHeartbeatRequest(lease_id=lease_id, progress=0.5))
    complete = store.complete_job(
        job.job_id,
        WorkerCompleteRequest(
            lease_id=lease_id,
            artifacts=[
                ArtifactRegisterRequest(
                    type=ArtifactType.PARAMETRIC_MEMORY,
                    name="pmem_calc",
                    uri="file:///tmp/adapter",
                    manifest={"base_model": "Qwen/Qwen3.6-27B", "adapter_format": "lora"},
                    compatibility={"base_model": "Qwen/Qwen3.6-27B"},
                    promoted=True,
                )
            ],
        ),
    )

    assert complete["state"] == "succeeded"
    assert complete["artifact_ids"][0].startswith("art_")
    with store.connect() as conn:
        row = conn.execute(
            "SELECT state, error FROM jobs WHERE job_id = ?",
            (job.job_id,),
        ).fetchone()
    assert row["state"] == "succeeded"
    assert row["error"] is None


def test_job_failure_records_error(tmp_path):
    store = EvolutionStore(db_path=tmp_path / "evolution.db", artifact_root=tmp_path / "artifacts")
    store.initialize()
    job = store.create_job(JobCreateRequest(method="mock", job_type="text_memory_mining"))
    claim = store.claim_job(WorkerClaimRequest(worker_id="worker_1", capabilities=["text_memory_mining"]))
    assert claim.job is not None

    result = store.fail_job(
        job.job_id,
        WorkerFailRequest(
            lease_id=claim.job.lease_id,
            error="worker command failed",
            retryable=False,
        ),
    )

    assert result["state"] == "failed"
    assert "worker command failed" in result["error"]


def test_claim_job_skips_incompatible_higher_priority_job(tmp_path):
    store = EvolutionStore(db_path=tmp_path / "evolution.db", artifact_root=tmp_path / "artifacts")
    store.initialize()
    high_priority = store.create_job(
        JobCreateRequest(method="mock", job_type="parametric_memory_train", priority=200)
    )
    compatible = store.create_job(
        JobCreateRequest(method="mock", job_type="text_memory_mining", priority=100)
    )

    claim = store.claim_job(
        WorkerClaimRequest(worker_id="worker_1", capabilities=["text_memory_mining"])
    )

    assert claim.job is not None
    assert claim.job.job_id == compatible.job_id
    with store.connect() as conn:
        high_priority_row = conn.execute(
            "SELECT state FROM jobs WHERE job_id = ?",
            (high_priority.job_id,),
        ).fetchone()
    assert high_priority_row["state"] == "pending"


def test_job_lease_mismatch_is_rejected_without_state_change(tmp_path):
    store = EvolutionStore(db_path=tmp_path / "evolution.db", artifact_root=tmp_path / "artifacts")
    store.initialize()
    job = store.create_job(JobCreateRequest(method="mock", job_type="text_memory_mining"))
    claim = store.claim_job(WorkerClaimRequest(worker_id="worker_1", capabilities=["text_memory_mining"]))
    assert claim.job is not None

    with pytest.raises(ValueError, match="invalid lease"):
        store.fail_job(
            job.job_id,
            WorkerFailRequest(lease_id="lease_wrong", error="wrong worker", retryable=False),
        )

    with store.connect() as conn:
        row = conn.execute(
            "SELECT state, error FROM jobs WHERE job_id = ?",
            (job.job_id,),
        ).fetchone()
    assert row["state"] == "claimed"
    assert row["error"] is None


def test_claim_job_requeues_expired_leases_before_selecting(tmp_path):
    store = EvolutionStore(db_path=tmp_path / "evolution.db", artifact_root=tmp_path / "artifacts")
    store.initialize()
    job = store.create_job(JobCreateRequest(method="mock", job_type="text_memory_mining"))
    first_claim = store.claim_job(
        WorkerClaimRequest(
            worker_id="worker_1",
            capabilities=["text_memory_mining"],
            lease_seconds=1,
        )
    )
    assert first_claim.job is not None
    old_lease_id = first_claim.job.lease_id
    expired_at = (datetime.now(UTC) - timedelta(seconds=1)).isoformat().replace("+00:00", "Z")
    with store.connect() as conn:
        conn.execute(
            """
            UPDATE jobs
            SET state = ?, lease_expires_at = ?
            WHERE job_id = ?
            """,
            (str(JobState.RUNNING), expired_at, job.job_id),
        )
        conn.commit()

    second_claim = store.claim_job(
        WorkerClaimRequest(worker_id="worker_2", capabilities=["text_memory_mining"])
    )

    assert second_claim.job is not None
    assert second_claim.job.job_id == job.job_id
    assert second_claim.job.lease_id != old_lease_id
    with store.connect() as conn:
        row = conn.execute(
            """
            SELECT state, claimed_by, lease_id, attempt_count, error
            FROM jobs
            WHERE job_id = ?
            """,
            (job.job_id,),
        ).fetchone()
    assert row["state"] == "claimed"
    assert row["claimed_by"] == "worker_2"
    assert row["lease_id"] == second_claim.job.lease_id
    assert row["attempt_count"] == 2
    assert "lease expired" in row["error"]


def test_heartbeat_renews_lease_expiration(tmp_path):
    store = EvolutionStore(db_path=tmp_path / "evolution.db", artifact_root=tmp_path / "artifacts")
    store.initialize()
    job = store.create_job(JobCreateRequest(method="mock", job_type="text_memory_mining"))
    claim = store.claim_job(
        WorkerClaimRequest(
            worker_id="worker_1",
            capabilities=["text_memory_mining"],
            lease_seconds=1,
        )
    )
    assert claim.job is not None
    with store.connect() as conn:
        original_expires_at = conn.execute(
            "SELECT lease_expires_at FROM jobs WHERE job_id = ?",
            (job.job_id,),
        ).fetchone()["lease_expires_at"]

    result = store.heartbeat_job(
        job.job_id,
        WorkerHeartbeatRequest(lease_id=claim.job.lease_id, progress=0.25, message="still running"),
    )

    with store.connect() as conn:
        row = conn.execute(
            "SELECT state, lease_expires_at FROM jobs WHERE job_id = ?",
            (job.job_id,),
        ).fetchone()
    assert result["state"] == "running"
    assert row["state"] == "running"
    assert _parse_utc(row["lease_expires_at"]) > _parse_utc(original_expires_at)


def test_complete_job_invalid_artifact_marks_failed_and_cleans_registered_artifacts(tmp_path):
    store = EvolutionStore(db_path=tmp_path / "evolution.db", artifact_root=tmp_path / "artifacts")
    store.initialize()
    job = store.create_job(JobCreateRequest(method="mock", job_type="text_memory_mining"))
    claim = store.claim_job(WorkerClaimRequest(worker_id="worker_1", capabilities=["text_memory_mining"]))
    assert claim.job is not None

    with pytest.raises(ValueError, match="non-finite float"):
        store.complete_job(
            job.job_id,
            WorkerCompleteRequest(
                lease_id=claim.job.lease_id,
                artifacts=[
                    ArtifactRegisterRequest(
                        type=ArtifactType.TEXT_MEMORY,
                        name="partial",
                        uri="file:///tmp/partial.md",
                    ),
                    ArtifactRegisterRequest(
                        type=ArtifactType.TEXT_MEMORY,
                        name="invalid",
                        uri="file:///tmp/invalid.md",
                        scores={"quality": math.nan},
                    ),
                ],
            ),
        )

    with store.connect() as conn:
        job_row = conn.execute(
            "SELECT state, error, lease_id, lease_expires_at FROM jobs WHERE job_id = ?",
            (job.job_id,),
        ).fetchone()
        artifact_count = conn.execute("SELECT COUNT(*) FROM artifacts").fetchone()[0]

    assert job_row["state"] == "failed"
    assert "non-finite float" in job_row["error"]
    assert job_row["lease_id"] is None
    assert job_row["lease_expires_at"] is None
    assert artifact_count == 0
    assert not list((tmp_path / "artifacts" / "artifacts" / "text_memory").glob("*/manifest.json"))


def test_complete_job_final_update_failure_marks_failed_and_cleans_artifacts(tmp_path):
    store = EvolutionStore(db_path=tmp_path / "evolution.db", artifact_root=tmp_path / "artifacts")
    store.initialize()
    job = store.create_job(JobCreateRequest(method="mock", job_type="text_memory_mining"))
    claim = store.claim_job(WorkerClaimRequest(worker_id="worker_1", capabilities=["text_memory_mining"]))
    assert claim.job is not None
    with store.connect() as conn:
        conn.execute(
            """
            CREATE TRIGGER jobs_succeed_failure
            BEFORE UPDATE OF state ON jobs
            WHEN NEW.state = 'succeeded'
            BEGIN
                SELECT RAISE(ABORT, 'forced job succeed failure');
            END;
            """
        )
        conn.commit()

    with pytest.raises(sqlite3.DatabaseError, match="forced job succeed failure"):
        store.complete_job(
            job.job_id,
            WorkerCompleteRequest(
                lease_id=claim.job.lease_id,
                artifacts=[
                    ArtifactRegisterRequest(
                        type=ArtifactType.TEXT_MEMORY,
                        name="partial",
                        uri="file:///tmp/partial.md",
                    )
                ],
            ),
        )

    with store.connect() as conn:
        job_row = conn.execute(
            "SELECT state, error FROM jobs WHERE job_id = ?",
            (job.job_id,),
        ).fetchone()
        artifact_count = conn.execute("SELECT COUNT(*) FROM artifacts").fetchone()[0]

    assert job_row["state"] == "failed"
    assert "forced job succeed failure" in job_row["error"]
    assert artifact_count == 0
    assert not list((tmp_path / "artifacts" / "artifacts" / "text_memory").glob("*/manifest.json"))


def test_complete_job_expired_final_lease_cleans_artifacts_without_failing_job(
    tmp_path,
    monkeypatch,
):
    store = EvolutionStore(db_path=tmp_path / "evolution.db", artifact_root=tmp_path / "artifacts")
    store.initialize()
    job = store.create_job(JobCreateRequest(method="mock", job_type="text_memory_mining"))
    claim = store.claim_job(
        WorkerClaimRequest(
            worker_id="worker_1",
            capabilities=["text_memory_mining"],
            lease_seconds=60,
        )
    )
    assert claim.job is not None

    original_register_artifact = store.register_artifact

    def expire_lease_after_register(request: ArtifactRegisterRequest):
        artifact = original_register_artifact(request)
        expired_at = (datetime.now(UTC) - timedelta(seconds=1)).isoformat().replace(
            "+00:00",
            "Z",
        )
        with store.connect() as conn:
            conn.execute(
                "UPDATE jobs SET state = ?, lease_expires_at = ? WHERE job_id = ?",
                (str(JobState.RUNNING), expired_at, job.job_id),
            )
            conn.commit()
        return artifact

    monkeypatch.setattr(store, "register_artifact", expire_lease_after_register)

    with pytest.raises(ValueError, match="lease expired"):
        store.complete_job(
            job.job_id,
            WorkerCompleteRequest(
                lease_id=claim.job.lease_id,
                artifacts=[
                    ArtifactRegisterRequest(
                        type=ArtifactType.TEXT_MEMORY,
                        name="transient",
                        uri="file:///tmp/transient.md",
                    )
                ],
            ),
        )

    with store.connect() as conn:
        job_row = conn.execute(
            "SELECT state, error, lease_id, lease_expires_at FROM jobs WHERE job_id = ?",
            (job.job_id,),
        ).fetchone()
        artifact_count = conn.execute("SELECT COUNT(*) FROM artifacts").fetchone()[0]

    assert job_row["state"] == "running"
    assert job_row["error"] is None
    assert job_row["lease_id"] == claim.job.lease_id
    assert _parse_utc(job_row["lease_expires_at"]) <= datetime.now(UTC)
    assert artifact_count == 0
    assert not list((tmp_path / "artifacts" / "artifacts" / "text_memory").glob("*/manifest.json"))

    next_claim = store.claim_job(
        WorkerClaimRequest(worker_id="worker_2", capabilities=["text_memory_mining"])
    )

    assert next_claim.job is not None
    assert next_claim.job.job_id == job.job_id
    assert next_claim.job.lease_id != claim.job.lease_id


def test_claim_job_handles_malformed_active_lease_and_continues(tmp_path):
    store = EvolutionStore(db_path=tmp_path / "evolution.db", artifact_root=tmp_path / "artifacts")
    store.initialize()
    malformed_job = store.create_job(
        JobCreateRequest(method="mock", job_type="text_memory_mining", priority=200)
    )
    compatible_job = store.create_job(
        JobCreateRequest(method="mock", job_type="text_memory_mining", priority=100)
    )
    malformed_claim = store.claim_job(
        WorkerClaimRequest(worker_id="worker_1", capabilities=["text_memory_mining"])
    )
    assert malformed_claim.job is not None
    assert malformed_claim.job.job_id == malformed_job.job_id
    with store.connect() as conn:
        conn.execute(
            """
            UPDATE jobs
            SET state = ?, lease_expires_at = ?
            WHERE job_id = ?
            """,
            (str(JobState.RUNNING), "not-a-timestamp", malformed_job.job_id),
        )
        conn.commit()

    next_claim = store.claim_job(
        WorkerClaimRequest(worker_id="worker_2", capabilities=["text_memory_mining"])
    )

    assert next_claim.job is not None
    assert next_claim.job.job_id == compatible_job.job_id
    with store.connect() as conn:
        malformed_row = conn.execute(
            "SELECT state, error FROM jobs WHERE job_id = ?",
            (malformed_job.job_id,),
        ).fetchone()

    assert malformed_row["state"] == "failed"
    assert "invalid lease_expires_at" in malformed_row["error"]
