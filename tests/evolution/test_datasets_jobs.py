from __future__ import annotations

from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
import hashlib
import json
import math
import os
from pathlib import Path
import sqlite3
import threading

import pytest

import openevo.evolution.artifact_payloads as artifact_payloads_module
import openevo.evolution.store as store_module
from openevo.evolution.framework import (
    EvolutionExecutionProfile,
    EvolutionTargetSelection,
    PayloadManifestEntry,
    payload_tree_digest,
)
from openevo.evolution.framework.builtins import (
    ImplementationDistributionIdentity,
    build_builtin_registry,
)
from openevo.gateway.node import build_evolution_session_event
from openevo.rollout.models import SessionResult, SessionStatus, SessionTiming
from openevo.trajectory.models import Trace, Trajectory
from openevo.evolution.models import (
    ArtifactRegisterRequest,
    ArtifactState,
    ArtifactType,
    DatasetCreateRequest,
    EventIngestRequest,
    HumanFeedbackCreateRequest,
    JobCreateRequest,
    JobState,
    ReviewClaimRequest,
    ReviewRequestCreateRequest,
    WorkerClaimRequest,
    WorkerCompleteRequest,
    WorkerFailRequest,
    WorkerHeartbeatRequest,
)
from openevo.evolution.store import EvolutionStore
from openevo.evolution.planned_jobs import (
    PlanBoundJobCreateRequest,
    PlannedInputBinding,
)


def _parse_utc(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def test_create_dataset_filters_events(tmp_path):
    store = EvolutionStore(db_path=tmp_path / "evolution.db", artifact_root=tmp_path / "artifacts")
    store.initialize()
    good_event = store.ingest_event(
        EventIngestRequest(
            source="openevo",
            event_type="openevo.session_completed",
            source_event_id="session:good",
            task_id="task_good",
            session_id="session_good",
            status="COMPLETED",
            reward=1.0,
            policy_version="policy_1",
            rollout_step=7,
            base_model="Qwen/Qwen3.6-27B",
            payload={"session_result": {"trajectory": {"traces": [{"reward": 1.0}]}}},
        )
    )
    store.ingest_event(
        EventIngestRequest(
            source="openevo",
            event_type="openevo.session_completed",
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
                "event_types": ["openevo.session_completed"],
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
        "event_types": ["openevo.session_completed"],
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
    assert (
        manifest_path
        == (tmp_path / "artifacts" / "datasets" / response.dataset_id / "manifest.json").resolve()
    )
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    records_path = manifest_path.parent / "records.jsonl"
    assert manifest == {
        "dataset_id": response.dataset_id,
        "name": "good_policy_1",
        "purpose": "skill_distillation",
        "query": {
            "event_types": ["openevo.session_completed"],
            "policy_version": "policy_1",
            "reward_min": 0.8,
            "status": ["COMPLETED"],
            "task_tags": [],
        },
        "limits": {"max_events": 10000, "max_traces": 50000},
        "event_ids": [good_event.event_id],
        "event_count": 1,
        "trace_count": 1,
        "records_path": "records.jsonl",
        "records_uri": records_path.as_uri(),
        "records_byte_size": records_path.stat().st_size,
        "records_sha256": hashlib.sha256(
            records_path.read_bytes()
        ).hexdigest(),
    }
    records = [
        json.loads(line)
        for line in records_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert records == [
        {
            "event_id": good_event.event_id,
            "source": "openevo",
            "event_type": "openevo.session_completed",
            "source_event_id": "session:good",
            "created_at": records[0]["created_at"],
            "ingested_at": records[0]["ingested_at"],
            "task_id": "task_good",
            "session_id": "session_good",
            "policy_version": "policy_1",
            "rollout_step": 7,
            "agent_harness": None,
            "agent_model": None,
            "base_model": "Qwen/Qwen3.6-27B",
            "status": "COMPLETED",
            "reward": 1.0,
            "trace_count": 1,
            "traces": [{"reward": 1.0}],
            "payload": {
                "session_result": {
                    "trajectory": {
                        "traces": [
                            {"reward": 1.0},
                        ],
                    },
                },
            },
        }
    ]
    assert artifact_row is not None
    assert artifact_row["type"] == "dataset"
    assert artifact_row["name"] == "good_policy_1"
    assert artifact_row["uri"] == manifest_path.as_uri()
    assert json.loads(artifact_row["lineage_json"]) == {"event_ids": [good_event.event_id]}
    assert json.loads(artifact_row["compatibility_json"]) == {"purpose": "skill_distillation"}
    assert json.loads(artifact_row["tags_json"]) == ["skill_distillation"]
    assert artifact_row["promoted"] == 1
    worker_manifest_path = Path(artifact_row["uri"].removeprefix("file://"))
    worker_manifest = json.loads(worker_manifest_path.read_text(encoding="utf-8"))
    worker_records_path = worker_manifest_path.parent / worker_manifest["records_path"]
    worker_records = [
        json.loads(line)
        for line in worker_records_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert worker_records[0]["event_id"] == good_event.event_id
    assert worker_records[0]["traces"] == [{"reward": 1.0}]


def test_dataset_create_is_exact_session_bound_and_response_loss_idempotent(
    tmp_path,
) -> None:
    store = EvolutionStore(
        db_path=tmp_path / "evolution.db",
        artifact_root=tmp_path / "artifacts",
    )
    store.initialize()
    for suffix in ("older", "accepted"):
        store.ingest_event(
            EventIngestRequest(
                source="openevo",
                event_type="openevo.session_completed",
                source_event_id=f"session:{suffix}",
                task_id=f"task-{suffix}",
                session_id=f"session-{suffix}",
                status="COMPLETED",
                policy_version="shared-policy",
                payload={
                        "session_result": {
                            "task_id": f"task-{suffix}",
                            "session_id": f"session-{suffix}",
                            "metadata": (
                                {"human_feedback": "retain source digest"}
                                if suffix == "accepted"
                                else {}
                            ),
                            "trajectory": {
                            "traces": [{"response": suffix}],
                        },
                    }
                },
            )
    )
    store.ingest_event(
        EventIngestRequest(
            source="other-producer",
            event_type="openevo.session_completed",
            source_event_id="session:accepted",
            task_id="task-accepted",
            session_id="session-accepted",
            status="COMPLETED",
            policy_version="shared-policy",
            payload={
                "session_result": {
                    "task_id": "task-accepted",
                    "session_id": "session-accepted",
                    "trajectory": {
                        "traces": [{"response": "wrong producer"}],
                    },
                }
            },
        )
    )
    request = DatasetCreateRequest(
        idempotency_key="successor-dataset-attempt-accepted",
        name="accepted transcript",
        purpose="openevo_science_successor_v2",
        query={
            "source": "openevo",
            "event_types": ["openevo.session_completed"],
            "status": ["COMPLETED"],
            "policy_version": "shared-policy",
            "source_event_id": "session:accepted",
            "task_id": "task-accepted",
            "session_id": "session-accepted",
        },
        limits={"max_events": 1, "max_traces": 1},
    )

    created = store.create_dataset(request)
    with store.connect() as connection:
        connection.execute(
            "UPDATE dataset_create_requests SET response_json = NULL "
            "WHERE idempotency_key = ?",
            (request.idempotency_key,),
        )
        connection.commit()
    recovered = store.create_dataset(request)

    assert recovered == created
    with store.connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM datasets").fetchone()[0] == 1
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM dataset_create_requests"
            ).fetchone()[0]
            == 1
        )
        row = connection.execute(
            "SELECT manifest_path FROM datasets WHERE dataset_id = ?",
            (created.dataset_id,),
        ).fetchone()
    manifest = json.loads(Path(row["manifest_path"]).read_text(encoding="utf-8"))
    assert manifest["query"]["source"] == "openevo"
    assert manifest["query"]["source_event_id"] == "session:accepted"
    assert manifest["query"]["task_id"] == "task-accepted"
    assert manifest["query"]["session_id"] == "session-accepted"
    assert manifest["source_event_evidence"]["source_event_id"] == (
        "session:accepted"
    )
    assert manifest["source_event_evidence"]["source"] == "openevo"
    expected_result = {
        "task_id": "task-accepted",
        "session_id": "session-accepted",
        "metadata": {"human_feedback": "retain source digest"},
        "trajectory": {"traces": [{"response": "accepted"}]},
    }
    expected_sha256 = hashlib.sha256(
        json.dumps(
            expected_result,
            ensure_ascii=True,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    assert manifest["source_event_evidence"]["session_result_sha256"] == (
        expected_sha256
    )
    with pytest.raises(ValueError, match="idempotency"):
        store.create_dataset(
            request.model_copy(update={"name": "different transcript"})
        )


def test_dataset_create_recovers_an_unjournaled_exact_active_artifact(
    tmp_path,
    monkeypatch,
) -> None:
    store = EvolutionStore(
        db_path=tmp_path / "evolution.db",
        artifact_root=tmp_path / "artifacts",
    )
    store.initialize()
    store.ingest_event(
        EventIngestRequest(
            source="openevo",
            event_type="openevo.session_completed",
            source_event_id="session:recover-artifact",
            task_id="task-recover-artifact",
            session_id="recover-artifact",
            status="COMPLETED",
            policy_version="policy-recover-artifact",
            payload={
                "session_result": {
                    "trajectory": {"traces": [{"response": "accepted"}]}
                }
            },
        )
    )
    request = DatasetCreateRequest(
        idempotency_key="successor-dataset-recover-artifact",
        name="recover active artifact",
        purpose="openevo_science_successor_v2",
        query={
            "source": "openevo",
            "event_types": ["openevo.session_completed"],
            "status": ["COMPLETED"],
            "policy_version": "policy-recover-artifact",
            "source_event_id": "session:recover-artifact",
            "task_id": "task-recover-artifact",
            "session_id": "recover-artifact",
        },
        limits={"max_events": 1, "max_traces": 1},
    )
    original_backfill = store._backfill_dataset_artifact_id

    def interrupt_backfill(_dataset_id: str, _artifact_id: str) -> None:
        raise KeyboardInterrupt

    monkeypatch.setattr(
        store,
        "_backfill_dataset_artifact_id",
        interrupt_backfill,
    )
    with pytest.raises(KeyboardInterrupt):
        store.create_dataset(request)
    monkeypatch.setattr(
        store,
        "_backfill_dataset_artifact_id",
        original_backfill,
    )

    with store.connect() as connection:
        dataset_id = str(
            connection.execute(
                "SELECT dataset_id FROM dataset_create_requests "
                "WHERE idempotency_key = ?",
                (request.idempotency_key,),
            ).fetchone()["dataset_id"]
        )
    restarted = EvolutionStore(
        db_path=tmp_path / "evolution.db",
        artifact_root=tmp_path / "artifacts",
    )
    restarted.initialize()
    recovered = restarted.get_dataset(dataset_id)
    assert restarted.create_dataset(request) == recovered

    assert recovered.event_count == 1
    assert recovered.trace_count == 1
    with restarted.connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM datasets").fetchone()[0] == 1
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM artifacts WHERE type = 'dataset'"
            ).fetchone()[0]
            == 1
        )


def test_dataset_create_recovers_after_crash_between_dataset_file_publications(
    tmp_path,
    monkeypatch,
) -> None:
    store = EvolutionStore(
        db_path=tmp_path / "evolution.db",
        artifact_root=tmp_path / "artifacts",
    )
    store.initialize()
    store.ingest_event(
        EventIngestRequest(
            source="openevo",
            event_type="openevo.session_completed",
            source_event_id="session:dataset-file-crash",
            task_id="task-dataset-file-crash",
            session_id="dataset-file-crash",
            status="COMPLETED",
            payload={
                "session_result": {
                    "trajectory": {
                        "traces": [{"response": "recover exact publication"}]
                    }
                }
            },
        )
    )
    request = DatasetCreateRequest(
        idempotency_key="successor-dataset-file-crash",
        name="recover dataset files",
        purpose="openevo_science_successor_v2",
        query={
            "source": "openevo",
            "source_event_id": "session:dataset-file-crash",
            "task_id": "task-dataset-file-crash",
            "session_id": "dataset-file-crash",
        },
        limits={"max_events": 1, "max_traces": 1},
    )
    original_write_records = store_module._write_jsonl_strict_exclusive

    def interrupt_records(*_args, **_kwargs):
        raise KeyboardInterrupt

    monkeypatch.setattr(
        store_module,
        "_write_jsonl_strict_exclusive",
        interrupt_records,
    )
    with pytest.raises(KeyboardInterrupt):
        store.create_dataset(request)
    monkeypatch.setattr(
        store_module,
        "_write_jsonl_strict_exclusive",
        original_write_records,
    )

    restarted = EvolutionStore(
        db_path=tmp_path / "evolution.db",
        artifact_root=tmp_path / "artifacts",
    )
    restarted.initialize()
    recovered = restarted.create_dataset(request)

    assert recovered.event_count == 1
    assert recovered.trace_count == 1
    with restarted.connect() as connection:
        row = connection.execute(
            "SELECT artifact_id FROM datasets WHERE dataset_id = ?",
            (recovered.dataset_id,),
        ).fetchone()
        assert row is not None
        assert row["artifact_id"] == recovered.artifact_id
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM artifacts WHERE type = 'dataset'"
            ).fetchone()[0]
            == 1
        )


def test_concurrent_exact_dataset_create_has_one_dataset_artifact(
    tmp_path,
    monkeypatch,
) -> None:
    database_path = tmp_path / "evolution.db"
    artifact_root = tmp_path / "artifacts"
    first_store = EvolutionStore(
        db_path=database_path,
        artifact_root=artifact_root,
    )
    first_store.initialize()
    second_store = EvolutionStore(
        db_path=database_path,
        artifact_root=artifact_root,
    )
    second_store.initialize()
    first_store.ingest_event(
        EventIngestRequest(
            source="openevo",
            event_type="openevo.session_completed",
            source_event_id="session:concurrent-dataset",
            task_id="task-concurrent-dataset",
            session_id="concurrent-dataset",
            status="COMPLETED",
            payload={
                "session_result": {
                    "trajectory": {
                        "traces": [{"response": "one exact artifact"}]
                    }
                }
            },
        )
    )
    request = DatasetCreateRequest(
        idempotency_key="successor-dataset-concurrent",
        name="concurrent exact dataset",
        purpose="openevo_science_successor_v2",
        query={
            "source": "openevo",
            "source_event_id": "session:concurrent-dataset",
            "task_id": "task-concurrent-dataset",
            "session_id": "concurrent-dataset",
        },
        limits={"max_events": 1, "max_traces": 1},
    )
    registration_count = 0
    registration_guard = threading.Lock()
    second_registration_entered = threading.Event()

    def gated_register(store, original, artifact_request):
        nonlocal registration_count
        with registration_guard:
            registration_count += 1
            ordinal = registration_count
            if ordinal == 2:
                second_registration_entered.set()
        if ordinal == 1:
            second_registration_entered.wait(timeout=0.25)
        return original(artifact_request)

    first_register = first_store.register_artifact
    second_register = second_store.register_artifact
    monkeypatch.setattr(
        first_store,
        "register_artifact",
        lambda artifact_request: gated_register(
            first_store,
            first_register,
            artifact_request,
        ),
    )
    monkeypatch.setattr(
        second_store,
        "register_artifact",
        lambda artifact_request: gated_register(
            second_store,
            second_register,
            artifact_request,
        ),
    )
    results: list[object] = []

    def create(store: EvolutionStore) -> None:
        try:
            results.append(store.create_dataset(request))
        except BaseException as exc:
            results.append(exc)

    first_thread = threading.Thread(target=create, args=(first_store,))
    second_thread = threading.Thread(target=create, args=(second_store,))
    first_thread.start()
    second_thread.start()
    first_thread.join(timeout=5)
    second_thread.join(timeout=5)

    assert not first_thread.is_alive()
    assert not second_thread.is_alive()
    assert len(results) == 2
    assert all(not isinstance(result, BaseException) for result in results)
    assert results[0] == results[1]
    with first_store.connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM datasets").fetchone()[0] == 1
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM artifacts WHERE type = 'dataset'"
            ).fetchone()[0]
            == 1
        )


def test_dataset_startup_recovery_enforces_monotonic_file_byte_budget(
    tmp_path,
    monkeypatch,
) -> None:
    database_path = tmp_path / "evolution.db"
    artifact_root = tmp_path / "artifacts"
    store = EvolutionStore(
        db_path=database_path,
        artifact_root=artifact_root,
    )
    store.initialize()
    store.ingest_event(
        EventIngestRequest(
            source="openevo",
            event_type="openevo.session_completed",
            source_event_id="session:dataset-recovery-budget",
            status="COMPLETED",
            payload={
                "session_result": {
                    "trajectory": {
                        "traces": [{"response": "bounded startup recovery"}]
                    }
                }
            },
        )
    )
    store.create_dataset(
        DatasetCreateRequest(
            idempotency_key="successor-dataset-recovery-budget",
            name="bounded recovery dataset",
            purpose="openevo_science_successor_v2",
            query={
                "source": "openevo",
                "source_event_id": "session:dataset-recovery-budget",
            },
            limits={"max_events": 1, "max_traces": 1},
        )
    )
    monkeypatch.setattr(
        store_module,
        "MAX_DATASET_STARTUP_RECOVERY_BYTES",
        1,
    )

    with pytest.raises(
        ValueError,
        match="dataset startup recovery exceeds the aggregate byte limit",
    ):
        EvolutionStore(
            db_path=database_path,
            artifact_root=artifact_root,
        ).initialize()


def test_dataset_create_admission_cannot_ack_unrestartable_recovery_state(
    tmp_path,
    monkeypatch,
) -> None:
    database_path = tmp_path / "evolution.db"
    artifact_root = tmp_path / "artifacts"
    store = EvolutionStore(
        db_path=database_path,
        artifact_root=artifact_root,
    )
    store.initialize()
    store.ingest_event(
        EventIngestRequest(
            source="openevo",
            event_type="openevo.session_completed",
            source_event_id="session:dataset-recovery-admission",
            status="COMPLETED",
            payload={
                "session_result": {
                    "trajectory": {
                        "traces": [{"response": "durable recovery admission"}]
                    }
                }
            },
        )
    )
    first = store.create_dataset(
        DatasetCreateRequest(
            idempotency_key="successor-dataset-recovery-admission-a",
            name="recovery admission a",
            purpose="openevo_science_successor_v2",
            query={
                "source": "openevo",
                "source_event_id": "session:dataset-recovery-admission",
            },
            limits={"max_events": 1, "max_traces": 1},
        )
    )
    with store.connect() as connection:
        admitted = connection.execute(
            "SELECT recovery_byte_size, recovery_file_count "
            "FROM dataset_create_requests WHERE dataset_id = ?",
            (first.dataset_id,),
        ).fetchone()
    assert admitted["recovery_byte_size"] > 0
    assert admitted["recovery_file_count"] == 4
    monkeypatch.setattr(
        store_module,
        "MAX_DATASET_STARTUP_RECOVERY_BYTES",
        admitted["recovery_byte_size"],
    )

    with pytest.raises(
        ValueError,
        match="dataset startup recovery capacity is exhausted",
    ):
        store.create_dataset(
            DatasetCreateRequest(
                idempotency_key="successor-dataset-recovery-admission-b",
                name="recovery admission b",
                purpose="openevo_science_successor_v2",
                query={
                    "source": "openevo",
                    "source_event_id": "session:dataset-recovery-admission",
                },
                limits={"max_events": 1, "max_traces": 1},
            )
        )

    restarted = EvolutionStore(
        db_path=database_path,
        artifact_root=artifact_root,
    )
    restarted.initialize()
    assert restarted.get_dataset(first.dataset_id) == first


def test_dataset_create_request_inventory_fails_closed_on_forged_receipt(
    tmp_path,
) -> None:
    store = EvolutionStore(
        db_path=tmp_path / "evolution.db",
        artifact_root=tmp_path / "artifacts",
    )
    store.initialize()
    with store.connect() as connection:
        connection.execute(
            "INSERT INTO dataset_create_requests("
            "idempotency_key, request_sha256, request_json, dataset_id, "
            "response_json, created_at) VALUES (?, ?, ?, ?, NULL, ?)",
            (
                "forged-dataset-request",
                "0" * 64,
                '{"idempotency_key":"forged-dataset-request"}',
                "ds-forged",
                "2026-07-25T00:00:00Z",
            ),
        )
        connection.commit()

    with pytest.raises(ValueError, match="dataset create request"):
        EvolutionStore(
            db_path=tmp_path / "evolution.db",
            artifact_root=tmp_path / "artifacts",
        ).initialize()


def test_get_dataset_rejects_transcript_content_tampering(tmp_path) -> None:
    store = EvolutionStore(
        db_path=tmp_path / "evolution.db",
        artifact_root=tmp_path / "artifacts",
    )
    store.initialize()
    store.ingest_event(
        EventIngestRequest(
            source="openevo",
            event_type="openevo.session_completed",
            source_event_id="session:record-integrity",
            task_id="task-record-integrity",
            session_id="record-integrity",
            status="COMPLETED",
            payload={
                "session_result": {
                    "trajectory": {
                        "traces": [{"response": "authoritative transcript"}]
                    }
                }
            },
        )
    )
    created = store.create_dataset(
        DatasetCreateRequest(
            name="record integrity",
            purpose="openevo_science_successor_v2",
            query={
                "source": "openevo",
                "event_types": ["openevo.session_completed"],
                "source_event_id": "session:record-integrity",
                "task_id": "task-record-integrity",
                "session_id": "record-integrity",
            },
            limits={"max_events": 1, "max_traces": 1},
        )
    )
    with store.connect() as connection:
        row = connection.execute(
            "SELECT manifest_path FROM datasets WHERE dataset_id = ?",
            (created.dataset_id,),
        ).fetchone()
    records_path = Path(row["manifest_path"]).with_name("records.jsonl")
    record = json.loads(records_path.read_text(encoding="utf-8"))
    record["traces"][0]["response"] = "forged transcript"
    records_path.write_text(
        json.dumps(record, sort_keys=True) + "\n",
        encoding="utf-8",
    )

    with pytest.raises(
        store_module.DatasetIntegrityError,
        match="records differ from event membership",
    ):
        store.get_dataset(created.dataset_id)


def test_create_dataset_excludes_non_openevo_session_event_identity(tmp_path):
    store = EvolutionStore(db_path=tmp_path / "evolution.db", artifact_root=tmp_path / "artifacts")
    store.initialize()
    non_openevo_event_type = "pol" + "ar.session_completed"
    store.ingest_event(
        EventIngestRequest(
            source="openevo",
            event_type=non_openevo_event_type,
            source_event_id="session:non-openevo",
            task_id="task_non_openevo",
            session_id="non-openevo",
            status="COMPLETED",
            policy_version="policy_1",
            payload={"session_result": {"trajectory": {"traces": [{"reward": 1.0}]}}},
        )
    )
    store.initialize()

    response = store.create_dataset(
        DatasetCreateRequest(
            name="non_openevo_session_event",
            purpose="skill_distillation",
            query={
                "event_types": ["openevo.session_completed"],
                "status": ["COMPLETED"],
                "policy_version": "policy_1",
            },
        )
    )

    assert response.event_count == 0
    with store.connect() as conn:
        dataset_event_rows = conn.execute(
            "SELECT event_id FROM dataset_events WHERE dataset_id = ?",
            (response.dataset_id,),
        ).fetchall()

    assert dataset_event_rows == []


def test_create_dataset_sanitizes_validated_human_feedback(tmp_path):
    store = EvolutionStore(db_path=tmp_path / "evolution.db", artifact_root=tmp_path / "artifacts")
    store.initialize()
    event = store.ingest_event(
        EventIngestRequest(
            source="openevo",
            event_type="openevo.session_completed",
            source_event_id="session:human-feedback",
            task_id="task_human_feedback",
            session_id="session_human_feedback",
            status="COMPLETED",
            reward=0.4,
            payload={
                "session_result": {
                    "trajectory": {"traces": [{"reward": 0.4}]},
                    "metadata": {
                        "evolution_feedback": {
                            "human": [
                                {
                                    "feedback_id": "hfb_keep",
                                    "status": "available_for_evolution",
                                    "normalized_payload": {
                                        "decision": "revise",
                                        "confidence": 0.85,
                                        "score": 0.72,
                                        "observed_issues": [
                                            "Still encourages unbounded repository search.",
                                            {
                                                "text": "Nested typed issue.",
                                                "rationale": "Nested raw rationale must not leak.",
                                                "raw_payload": {"secret": "nested-secret"},
                                            },
                                        ],
                                        "suggested_changes": [
                                            "Add a bounded source inventory step.",
                                            ["nested raw change must not leak"],
                                        ],
                                        "risks": ["May overfit to the review packet."],
                                        "validation_checks": ["Run timeout-heavy tasks."],
                                        "labels": ["bounded-search"],
                                        "raw_payload": {"approved": False},
                                    },
                                    "raw_payload": {"approved": False},
                                    "rationale": "Do not leak this reviewer note.",
                                },
                                {
                                    "feedback_id": "hfb_invalid_score",
                                    "status": "available_for_evolution",
                                    "normalized_payload": {
                                        "decision": "approve",
                                        "score": 1.5,
                                        "observed_issues": [
                                            "Invalid score should not be stringified."
                                        ],
                                    },
                                },
                                {
                                    "feedback_id": "hfb_drop",
                                    "status": "rejected_invalid",
                                    "normalized_payload": {
                                        "decision": "approve",
                                        "observed_issues": ["invalid item"],
                                    },
                                },
                                {
                                    "feedback_id": "hfb_missing_status",
                                    "normalized_payload": {
                                        "decision": "revise",
                                        "observed_issues": ["missing-status issue"],
                                    },
                                },
                                {
                                    "feedback_id": "hfb_non_string_status",
                                    "status": {"state": "available_for_evolution"},
                                    "normalized_payload": {
                                        "decision": "revise",
                                        "observed_issues": ["non-string-status issue"],
                                    },
                                },
                                {
                                    "feedback_id": "hfb_submitted",
                                    "status": "submitted",
                                    "normalized_payload": {
                                        "decision": "revise",
                                        "observed_issues": ["submitted issue"],
                                    },
                                },
                                {
                                    "feedback_id": "hfb_validated",
                                    "status": "validated",
                                    "normalized_payload": {
                                        "decision": "revise",
                                        "observed_issues": ["validated issue"],
                                    },
                                },
                                {
                                    "feedback_id": "hfb_consumed",
                                    "status": "consumed",
                                    "normalized_payload": {
                                        "decision": "revise",
                                        "observed_issues": ["consumed issue"],
                                    },
                                },
                            ]
                        }
                    },
                }
            },
        )
    )

    response = store.create_dataset(
        DatasetCreateRequest(
            name="human_feedback_dataset",
            purpose="agent_system_evolution",
            query={
                "event_types": ["openevo.session_completed"],
                "status": ["COMPLETED"],
            },
        )
    )

    with store.connect() as conn:
        dataset_row = conn.execute(
            "SELECT * FROM datasets WHERE dataset_id = ?",
            (response.dataset_id,),
        ).fetchone()
    manifest_path = Path(dataset_row["manifest_path"])
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    records_path = manifest_path.parent / manifest["records_path"]
    records = [
        json.loads(line)
        for line in records_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]

    assert [record["event_id"] for record in records] == [event.event_id]
    human_feedback = (
        records[0]["payload"]["session_result"]["metadata"]["evolution_feedback"]["human"]
    )
    assert human_feedback == [
        {
            "feedback_id": "hfb_keep",
            "status": "available_for_evolution",
            "decision": "revise",
            "confidence": 0.85,
            "score": 0.72,
            "observed_issues": ["Still encourages unbounded repository search."],
            "suggested_changes": ["Add a bounded source inventory step."],
            "risks": ["May overfit to the review packet."],
            "validation_checks": ["Run timeout-heavy tasks."],
            "labels": ["bounded-search"],
        },
        {
            "feedback_id": "hfb_invalid_score",
            "status": "available_for_evolution",
            "decision": "approve",
            "observed_issues": ["Invalid score should not be stringified."],
        }
    ]
    assert "raw_payload" not in json.dumps(records[0], sort_keys=True)
    assert "Do not leak this reviewer note." not in json.dumps(records[0], sort_keys=True)
    assert "Nested raw rationale must not leak." not in json.dumps(records[0], sort_keys=True)
    assert "nested-secret" not in json.dumps(records[0], sort_keys=True)
    assert "nested raw change must not leak" not in json.dumps(records[0], sort_keys=True)
    assert "missing-status issue" not in json.dumps(records[0], sort_keys=True)
    assert "non-string-status issue" not in json.dumps(records[0], sort_keys=True)
    assert "submitted issue" not in json.dumps(records[0], sort_keys=True)
    assert "validated issue" not in json.dumps(records[0], sort_keys=True)
    assert "consumed issue" not in json.dumps(records[0], sort_keys=True)


def test_create_dataset_merges_human_feedback_alias(tmp_path):
    store = EvolutionStore(db_path=tmp_path / "evolution.db", artifact_root=tmp_path / "artifacts")
    store.initialize()
    event = store.ingest_event(
        EventIngestRequest(
            source="openevo",
            event_type="openevo.session_completed",
            source_event_id="session:human-feedback-alias",
            task_id="task_human_feedback_alias",
            session_id="session_human_feedback_alias",
            status="COMPLETED",
            reward=0.5,
            payload={
                "session_result": {
                    "trajectory": {"traces": [{"reward": 0.5}]},
                    "metadata": {
                        "evolution_feedback": {
                            "human": [],
                            "human_feedback": [
                                {
                                    "feedback_id": "hfb_alias",
                                    "status": "available_for_evolution",
                                    "normalized_payload": {
                                        "decision": "revise",
                                        "observed_issues": ["alias feedback survives"],
                                    },
                                }
                            ],
                        }
                    },
                }
            },
        )
    )

    response = store.create_dataset(
        DatasetCreateRequest(
            name="human_feedback_alias_dataset",
            purpose="agent_system_evolution",
            query={
                "event_types": ["openevo.session_completed"],
                "status": ["COMPLETED"],
            },
        )
    )

    with store.connect() as conn:
        dataset_row = conn.execute(
            "SELECT * FROM datasets WHERE dataset_id = ?",
            (response.dataset_id,),
        ).fetchone()
    manifest_path = Path(dataset_row["manifest_path"])
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    records_path = manifest_path.parent / manifest["records_path"]
    records = [
        json.loads(line)
        for line in records_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]

    assert [record["event_id"] for record in records] == [event.event_id]
    evolution_feedback = records[0]["payload"]["session_result"]["metadata"]["evolution_feedback"]
    assert evolution_feedback == {
        "human": [
            {
                "feedback_id": "hfb_alias",
                "status": "available_for_evolution",
                "decision": "revise",
                "observed_issues": ["alias feedback survives"],
            }
        ]
    }


def test_create_dataset_canonicalizes_human_feedback_from_session_result_aliases(tmp_path):
    store = EvolutionStore(db_path=tmp_path / "evolution.db", artifact_root=tmp_path / "artifacts")
    store.initialize()
    event = store.ingest_event(
        EventIngestRequest(
            source="openevo",
            event_type="openevo.session_completed",
            source_event_id="session:human-feedback-session-alias",
            task_id="task_human_feedback_session_alias",
            session_id="session_human_feedback_session_alias",
            status="COMPLETED",
            reward=0.6,
            payload={
                "human_feedback": [
                    {
                        "feedback_id": "hfb_payload_alias",
                        "status": "available_for_evolution",
                        "normalized_payload": {
                            "decision": "revise",
                            "observed_issues": ["payload alias survives"],
                        },
                        "raw_payload": {"secret": "payload-secret"},
                    }
                ],
                "session_result": {
                    "human_feedback": [
                        {
                            "feedback_id": "hfb_session_alias",
                            "status": "available_for_evolution",
                            "normalized_payload": {
                                "decision": "revise",
                                "observed_issues": [
                                    "session result alias survives",
                                    {
                                        "text": "nested safe text ignored",
                                        "rationale": "nested raw rationale",
                                    },
                                ],
                            },
                            "raw_payload": {"secret": "session-secret"},
                            "rationale": "raw session rationale",
                        }
                    ],
                    "metadata": {
                        "human": [
                            {
                                "feedback_id": "hfb_session_alias",
                                "status": "available_for_evolution",
                                "normalized_payload": {
                                    "decision": "revise",
                                    "suggested_changes": ["metadata merge suggestion"],
                                    "risks": ["metadata merge risk"],
                                },
                            },
                            {
                                "feedback_id": "hfb_metadata_human",
                                "status": "available_for_evolution",
                                "normalized_payload": {
                                    "decision": "revise",
                                    "observed_issues": ["metadata human alias survives"],
                                },
                                "raw_payload": {"secret": "metadata-secret"},
                                "rationale": "raw metadata rationale",
                            }
                        ]
                    },
                    "trajectory": {"traces": [{"reward": 0.6}]},
                },
            },
        )
    )

    response = store.create_dataset(
        DatasetCreateRequest(
            name="human_feedback_session_alias_dataset",
            purpose="agent_system_evolution",
            query={
                "event_types": ["openevo.session_completed"],
                "status": ["COMPLETED"],
            },
        )
    )

    with store.connect() as conn:
        dataset_row = conn.execute(
            "SELECT * FROM datasets WHERE dataset_id = ?",
            (response.dataset_id,),
        ).fetchone()
    manifest_path = Path(dataset_row["manifest_path"])
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    records_path = manifest_path.parent / manifest["records_path"]
    records = [
        json.loads(line)
        for line in records_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]

    assert [record["event_id"] for record in records] == [event.event_id]
    payload = records[0]["payload"]
    session_result = payload["session_result"]
    assert "human_feedback" not in payload
    assert "human_feedback" not in session_result
    assert "human" not in session_result["metadata"]
    assert session_result["metadata"]["evolution_feedback"] == {
        "human": [
            {
                "feedback_id": "hfb_payload_alias",
                "status": "available_for_evolution",
                "decision": "revise",
                "observed_issues": ["payload alias survives"],
            },
            {
                "feedback_id": "hfb_session_alias",
                "status": "available_for_evolution",
                "decision": "revise",
                "observed_issues": ["session result alias survives"],
                "suggested_changes": ["metadata merge suggestion"],
                "risks": ["metadata merge risk"],
            },
            {
                "feedback_id": "hfb_metadata_human",
                "status": "available_for_evolution",
                "decision": "revise",
                "observed_issues": ["metadata human alias survives"],
            },
        ]
    }
    rendered_record = json.dumps(records[0], sort_keys=True)
    assert "raw_payload" not in rendered_record
    assert "payload-secret" not in rendered_record
    assert "session-secret" not in rendered_record
    assert "metadata-secret" not in rendered_record
    assert "raw session rationale" not in rendered_record
    assert "raw metadata rationale" not in rendered_record
    assert "nested raw rationale" not in rendered_record


@pytest.mark.parametrize(
    "shared_feedback",
    [
        "Shared evaluator guidance survives.",
        ["Shared list guidance survives.", "Shared second item survives."],
    ],
)
def test_create_dataset_preserves_non_mapping_metadata_evolution_feedback(
    tmp_path,
    shared_feedback,
):
    store = EvolutionStore(db_path=tmp_path / "evolution.db", artifact_root=tmp_path / "artifacts")
    store.initialize()
    event = store.ingest_event(
        EventIngestRequest(
            source="openevo",
            event_type="openevo.session_completed",
            source_event_id=f"session:shared-feedback-{type(shared_feedback).__name__}",
            task_id="task_shared_feedback",
            session_id="session_shared_feedback",
            status="COMPLETED",
            reward=0.7,
            payload={
                "session_result": {
                    "metadata": {
                        "evolution_feedback": shared_feedback,
                        "human_feedback": [
                            {
                                "feedback_id": "hfb_shared_preserved",
                                "status": "available_for_evolution",
                                "decision": "revise",
                                "observed_issues": ["human alias survives shared feedback"],
                            }
                        ],
                    },
                    "trajectory": {"traces": [{"reward": 0.7}]},
                },
            },
        )
    )

    response = store.create_dataset(
        DatasetCreateRequest(
            name=f"shared_feedback_{type(shared_feedback).__name__}",
            purpose="agent_system_evolution",
            query={
                "event_types": ["openevo.session_completed"],
                "status": ["COMPLETED"],
            },
        )
    )

    with store.connect() as conn:
        dataset_row = conn.execute(
            "SELECT * FROM datasets WHERE dataset_id = ?",
            (response.dataset_id,),
        ).fetchone()
    manifest_path = Path(dataset_row["manifest_path"])
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    records_path = manifest_path.parent / manifest["records_path"]
    records = [
        json.loads(line)
        for line in records_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]

    assert [record["event_id"] for record in records] == [event.event_id]
    evolution_feedback = records[0]["payload"]["session_result"]["metadata"]["evolution_feedback"]
    assert evolution_feedback["shared"] == shared_feedback
    assert evolution_feedback["human"] == [
        {
            "feedback_id": "hfb_shared_preserved",
            "status": "available_for_evolution",
            "decision": "revise",
            "observed_issues": ["human alias survives shared feedback"],
        }
    ]
    assert "human_feedback" not in records[0]["payload"]["session_result"]["metadata"]


def test_create_dataset_redacts_secrets_from_human_feedback_strings(tmp_path):
    store = EvolutionStore(db_path=tmp_path / "evolution.db", artifact_root=tmp_path / "artifacts")
    store.initialize()
    event = store.ingest_event(
        EventIngestRequest(
            source="openevo",
            event_type="openevo.session_completed",
            source_event_id="session:redacted-human-feedback",
            task_id="task_redacted_feedback",
            session_id="session_redacted_feedback",
            status="COMPLETED",
            reward=0.8,
            payload={
                "session_result": {
                    "metadata": {
                        "evolution_feedback": {
                            "human": [
                                {
                                    "feedback_id": "hfb_redacted",
                                    "status": "available_for_evolution",
                                    "decision": "revise",
                                    "observed_issues": [
                                        "ordinary issue text survives",
                                        (
                                            "Credentialed URL "
                                            "https://reviewer:tok_secret@example.com/path"
                                            "?secret=query_secret#frag"
                                        ),
                                        (
                                            "Credentialed URL with at sign "
                                            "https://user:p@ss@example.com/path"
                                        ),
                                    ],
                                    "suggested_changes": [
                                        "Set token=tok_123 and api_key=key_456",
                                        (
                                            "Fetch signed URL "
                                            "https://example.com/download"
                                            "?X-Amz-Signature=signed-dataset"
                                            "&AWSAccessKeyId=dataset-access"
                                            "#signed-fragment"
                                        ),
                                        "Fetch short URL https://example.com/download?sig=shortsig",
                                        (
                                            "Fetch object "
                                            "s3://bucket/key?X-Amz-Signature=s3-dataset"
                                            "#s3-fragment"
                                        ),
                                        "Fetch custom openevo+artifact://host/path?secret=query-secret#frag",
                                        "Inspect file:///home/ziyi/.ssh/id_rsa",
                                        "Inspect /tmp/openevo-secret.txt",
                                        "Inspect /etc/passwd",
                                        "Inspect /mnt/data/secret.txt",
                                        "Inspect /gpfs/projects/private/key.txt",
                                        "Inspect /Users/alice/key.pem",
                                        "Inspect /secret.txt",
                                        "Inspect /workspace/prod/key.pem",
                                        "Inspect /app/secret.txt",
                                        "Inspect /openevo/session/evolution/memory.md",
                                        "Call route /api/v1/feedback",
                                        "Probe endpoint /healthz",
                                        "Call reviews route /v1/reviews",
                                        r"Open C:\Users\alice\secret.txt",
                                        r"Open C:\Program Files\secret.txt",
                                        r"Open C:\Users\Alice Smith\secret.txt",
                                        r"Open \\server\share\secret.txt",
                                        "Open C:/Users/Alice/secret.txt",
                                        "Check postgres://alice:pg_secret@example.com/db",
                                        "Clone ssh://bob:ssh_secret@example.com/repo",
                                    ],
                                    "risks": [
                                        "password=hunter2 secret=rawsecret",
                                        "api_key: sk-dataset-colon token: tok-dataset-colon",
                                        "OPENAI_API_KEY=sk-dataset-env",
                                        "password: dataset-password secret: dataset-secret",
                                        "Authorization: Bearer sk-dataset-bearer",
                                        "AWS_ACCESS_KEY_ID=AKIA_DATASET_ACCESS",
                                        "access_key_id: AKIA_DATASET_COLON",
                                        "AWS_SECRET_ACCESS_KEY=dataset-aws-secret",
                                    ],
                                    "labels": ["safe-label"],
                                }
                            ]
                        }
                    },
                    "trajectory": {"traces": [{"reward": 0.8}]},
                },
            },
        )
    )

    response = store.create_dataset(
        DatasetCreateRequest(
            name="redacted_human_feedback",
            purpose="agent_system_evolution",
            query={
                "event_types": ["openevo.session_completed"],
                "status": ["COMPLETED"],
            },
        )
    )

    with store.connect() as conn:
        dataset_row = conn.execute(
            "SELECT * FROM datasets WHERE dataset_id = ?",
            (response.dataset_id,),
        ).fetchone()
    manifest_path = Path(dataset_row["manifest_path"])
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    records_path = manifest_path.parent / manifest["records_path"]
    records = [
        json.loads(line)
        for line in records_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]

    assert [record["event_id"] for record in records] == [event.event_id]
    rendered_record = json.dumps(records[0], sort_keys=True)
    assert "ordinary issue text survives" in rendered_record
    assert "safe-label" in rendered_record
    assert "tok_secret" not in rendered_record
    assert "query_secret" not in rendered_record
    assert "p@ss" not in rendered_record
    assert "https://user:p@ss@example.com/path" not in rendered_record
    assert "https://ss@example.com/path" not in rendered_record
    assert "tok_123" not in rendered_record
    assert "key_456" not in rendered_record
    assert "signed-dataset" not in rendered_record
    assert "dataset-access" not in rendered_record
    assert "shortsig" not in rendered_record
    assert "s3-dataset" not in rendered_record
    assert "s3-fragment" not in rendered_record
    assert "query-secret" not in rendered_record
    assert "signed-fragment" not in rendered_record
    assert "X-Amz-Signature" not in rendered_record
    assert "AWSAccessKeyId" not in rendered_record
    assert "sig=shortsig" not in rendered_record
    assert "alice" not in rendered_record
    assert "pg_secret" not in rendered_record
    assert "postgres://alice:pg_secret@example.com/db" not in rendered_record
    assert "bob" not in rendered_record
    assert "ssh_secret" not in rendered_record
    assert "ssh://bob:ssh_secret@example.com/repo" not in rendered_record
    assert "hunter2" not in rendered_record
    assert "rawsecret" not in rendered_record
    assert "sk-dataset-colon" not in rendered_record
    assert "tok-dataset-colon" not in rendered_record
    assert "sk-dataset-env" not in rendered_record
    assert "dataset-password" not in rendered_record
    assert "dataset-secret" not in rendered_record
    assert "sk-dataset-bearer" not in rendered_record
    assert "AKIA_DATASET_ACCESS" not in rendered_record
    assert "AKIA_DATASET_COLON" not in rendered_record
    assert "dataset-aws-secret" not in rendered_record
    assert "file://" not in rendered_record
    assert "/home/ziyi/.ssh/id_rsa" not in rendered_record
    assert "/tmp/openevo-secret.txt" not in rendered_record
    assert "/etc/passwd" not in rendered_record
    assert "/mnt/data/secret.txt" not in rendered_record
    assert "/gpfs/projects/private/key.txt" not in rendered_record
    assert "/Users/alice/key.pem" not in rendered_record
    assert "/secret.txt" not in rendered_record
    assert "/workspace/prod/key.pem" not in rendered_record
    assert "/app/secret.txt" not in rendered_record
    assert "/openevo/session/evolution/memory.md" not in rendered_record
    assert "/api/v1/feedback" not in rendered_record
    assert "/healthz" not in rendered_record
    assert "/v1/reviews" not in rendered_record
    assert r"C:\Users\alice\secret.txt" not in rendered_record
    assert "Program Files" not in rendered_record
    assert "Alice Smith" not in rendered_record
    assert "server" not in rendered_record
    assert "share" not in rendered_record
    assert "C:/Users/Alice/secret.txt" not in rendered_record


def test_create_dataset_accepts_subscription_transcript_trajectory_event(tmp_path):
    store = EvolutionStore(db_path=tmp_path / "evolution.db", artifact_root=tmp_path / "artifacts")
    store.initialize()
    session_result = SessionResult(
        session_id="session_sub",
        task_id="task_sub",
        status=SessionStatus.COMPLETED,
        trajectory=Trajectory(
            status="COMPLETED",
            metadata={
                "builder": "agent_transcript",
                "capture_mode": "transcript",
                "token_level_metrics_available": False,
                "trace_count": 1,
            },
            traces=[
                Trace(
                    prompt_messages=[{"role": "user", "content": "Do work."}],
                    response_messages=[
                        {"role": "assistant", "content": "Used subscription mode."}
                    ],
                    finish_reason="transcript",
                    metadata={
                        "capture_mode": "transcript",
                        "token_level_metrics_available": False,
                        "transcript_path": "/tmp/session/logs/agent/step.00.stdout.log",
                    },
                )
            ],
        ),
        timing=SessionTiming(),
        node_id="node-a",
        metadata={
            "policy_version": "policy_sub",
            "rollout_step": 5,
            "agent": {"harness": "codex", "model_name": "gpt-5.5"},
        },
    )
    event = store.ingest_event(
        EventIngestRequest.model_validate(build_evolution_session_event(session_result))
    )

    response = store.create_dataset(
        DatasetCreateRequest(
            name="subscription_transcript",
            purpose="memory_mining",
            query={
                "event_types": ["openevo.session_completed"],
                "status": ["COMPLETED"],
                "policy_version": "policy_sub",
            },
        )
    )

    assert response.event_count == 1
    assert response.trace_count == 1

    with store.connect() as conn:
        dataset_row = conn.execute(
            "SELECT * FROM datasets WHERE dataset_id = ?",
            (response.dataset_id,),
        ).fetchone()

    manifest_path = Path(dataset_row["manifest_path"])
    records_path = manifest_path.parent / "records.jsonl"
    records = [
        json.loads(line)
        for line in records_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]

    assert records[0]["event_id"] == event.event_id
    assert records[0]["agent_harness"] == "codex"
    assert records[0]["agent_model"] == "gpt-5.5"
    assert records[0]["trace_count"] == 1
    trace = records[0]["traces"][0]
    assert trace["prompt_messages"] == [{"role": "user", "content": "Do work."}]
    assert trace["response_messages"] == [
        {"role": "assistant", "content": "Used subscription mode."}
    ]
    assert trace["response_ids"] == []
    assert trace["loss_mask"] == []
    assert trace["response_logprobs"] is None
    assert trace["metadata"]["capture_mode"] == "transcript"
    assert trace["metadata"]["token_level_metrics_available"] is False
    assert (
        records[0]["payload"]["session_result"]["trajectory"]["metadata"]["builder"]
        == "agent_transcript"
    )


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
    assert not list((tmp_path / "artifacts" / "datasets").glob("*/records.jsonl"))


def test_create_dataset_rejects_task_tags_query_without_writes(tmp_path):
    store = EvolutionStore(db_path=tmp_path / "evolution.db", artifact_root=tmp_path / "artifacts")
    store.initialize()
    store.ingest_event(
        EventIngestRequest(
            source="openevo",
            event_type="openevo.session_completed",
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
    assert not list((tmp_path / "artifacts" / "datasets").glob("*/records.jsonl"))
    assert not list((tmp_path / "artifacts" / "artifacts" / "datasets").glob("*/manifest.json"))


def test_create_dataset_retries_id_collision_without_overwriting_manifest(tmp_path, monkeypatch):
    store = EvolutionStore(db_path=tmp_path / "evolution.db", artifact_root=tmp_path / "artifacts")
    store.initialize()
    store.ingest_event(
        EventIngestRequest(
            source="openevo",
            event_type="openevo.session_completed",
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
    first_records_path = tmp_path / "artifacts" / "datasets" / "ds_collision" / "records.jsonl"
    first_manifest_before = first_manifest_path.read_text(encoding="utf-8")
    first_records_before = first_records_path.read_text(encoding="utf-8")

    second_response = store.create_dataset(
        DatasetCreateRequest(name="second", purpose="skill_distillation")
    )

    assert first_response.dataset_id == "ds_collision"
    assert second_response.dataset_id == "ds_retry"
    assert first_manifest_path.read_text(encoding="utf-8") == first_manifest_before
    assert first_records_path.read_text(encoding="utf-8") == first_records_before
    assert json.loads(first_manifest_before)["name"] == "first"
    second_manifest_path = tmp_path / "artifacts" / "datasets" / "ds_retry" / "manifest.json"
    second_records_path = tmp_path / "artifacts" / "datasets" / "ds_retry" / "records.jsonl"
    assert json.loads(second_manifest_path.read_text(encoding="utf-8"))["name"] == "second"
    assert second_records_path.exists()


def test_create_dataset_stops_selecting_events_after_trace_limit(tmp_path):
    store = EvolutionStore(db_path=tmp_path / "evolution.db", artifact_root=tmp_path / "artifacts")
    store.initialize()
    first_event = store.ingest_event(
        EventIngestRequest(
            source="openevo",
            event_type="openevo.session_completed",
            source_event_id="session:first",
            status="COMPLETED",
            payload={
                "session_result": {"trajectory": {"traces": [{"reward": 1.0}, {"reward": 0.9}]}}
            },
        )
    )
    second_event = store.ingest_event(
        EventIngestRequest(
            source="openevo",
            event_type="openevo.session_completed",
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
            source="openevo",
            event_type="openevo.session_completed",
            source_event_id="session:oversized",
            status="COMPLETED",
            payload={
                "session_result": {
                    "trajectory": {"traces": [{"reward": 1.0}, {"reward": 0.9}, {"reward": 0.8}]}
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
            source="openevo",
            event_type="openevo.session_completed",
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
    dataset_records_path = (
        tmp_path / "artifacts" / "datasets" / "ds_backfill_failure" / "records.jsonl"
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
    assert not dataset_records_path.exists()
    assert not artifact_manifest_path.exists()


def test_create_dataset_counts_malformed_trace_shape_as_zero(tmp_path):
    store = EvolutionStore(db_path=tmp_path / "evolution.db", artifact_root=tmp_path / "artifacts")
    store.initialize()
    store.ingest_event(
        EventIngestRequest(
            source="openevo",
            event_type="openevo.session_completed",
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
            source="openevo",
            event_type="openevo.session_completed",
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
    assert not list((tmp_path / "artifacts" / "datasets").glob("*/records.jsonl"))
    assert not list((tmp_path / "artifacts" / "artifacts" / "datasets").glob("*/manifest.json"))


def test_create_dataset_rejects_corrupt_payload_json_before_writes(tmp_path):
    store = EvolutionStore(db_path=tmp_path / "evolution.db", artifact_root=tmp_path / "artifacts")
    store.initialize()
    event = store.ingest_event(
        EventIngestRequest(
            source="openevo",
            event_type="openevo.session_completed",
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
    assert not list((tmp_path / "artifacts" / "datasets").glob("*/records.jsonl"))
    assert not list((tmp_path / "artifacts" / "artifacts" / "datasets").glob("*/manifest.json"))


def test_plan_bound_job_rejects_each_mismatched_existing_plan_identity_field(tmp_path):
    snapshot = build_builtin_registry(
        ImplementationDistributionIdentity(
            distribution="openevo-test",
            distribution_version="1.0.0",
            distribution_digest="a" * 64,
        )
    )
    plan = snapshot.compile_plan(
        plan_id="plan-store-identity",
        selections=(
            EvolutionTargetSelection(
                target_id="skill_bundle",
                enabled=True,
                method_id="skill_bundle_reflector",
                config={
                    "reflector_llm": {
                        "provider": "codex_cli",
                        "model": "gpt-5.1-codex-mini",
                    }
                },
            ),
        ),
        profile=EvolutionExecutionProfile(
            execution_mode="self_deployed",
            capture_mode="transcript",
            harness_id="codex",
        ),
    )
    store = EvolutionStore(
        db_path=tmp_path / "evolution.db",
        artifact_root=tmp_path / "artifacts",
    )
    store.initialize()
    dataset = store.register_artifact(
        ArtifactRegisterRequest(
            type=ArtifactType.DATASET,
            name="plan-dataset",
            uri="file:///tmp/plan-dataset.json",
        )
    )
    request = PlanBoundJobCreateRequest(
        plan=plan,
        target_id="skill_bundle",
        job_type="skill_bundle",
        input_bindings=(
            PlannedInputBinding(
                binding_id="current_dataset",
                artifact_ids=(dataset.artifact_id,),
            ),
            PlannedInputBinding(
                binding_id="prior_target_artifacts",
                artifact_ids=(),
            ),
        ),
    )
    store.create_plan_bound_job(request, snapshot=snapshot)

    replacements = {
        "schema_version": "different-schema-version",
        "registry_snapshot_digest": "f" * 64,
        "plan_digest": "f" * 64,
        "plan_json": "{}",
    }
    with store.connect() as conn:
        original = conn.execute(
            "SELECT * FROM evolution_plans WHERE plan_id = ?",
            (plan.plan_id,),
        ).fetchone()
    for field, replacement in replacements.items():
        with store.connect() as conn:
            conn.execute(
                f"UPDATE evolution_plans SET {field} = ? WHERE plan_id = ?",
                (replacement, plan.plan_id),
            )
            conn.commit()
        with pytest.raises(ValueError, match="different plan"):
            store.create_plan_bound_job(request, snapshot=snapshot)
        with store.connect() as conn:
            conn.execute(
                f"UPDATE evolution_plans SET {field} = ? WHERE plan_id = ?",
                (original[field], plan.plan_id),
            )
            conn.commit()


def test_job_claim_heartbeat_and_complete(tmp_path):
    store = EvolutionStore(db_path=tmp_path / "evolution.db", artifact_root=tmp_path / "artifacts")
    store.initialize()
    adapter = store.files.root / "worker-output" / "adapter.bin"
    adapter.parent.mkdir()
    adapter.write_bytes(b"adapter")
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
            input_artifact_ids=[dataset.artifact_id, dataset.artifact_id],
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
                    uri=adapter.as_uri(),
                    manifest={
                        "adapter_format": "lora",
                        "base_model": "Qwen/Qwen3.6-27B",
                        "content_path": "adapter.bin",
                    },
                    lineage={"openevo_execution": {"job_id": job.job_id}},
                    compatibility={"base_model": "Qwen/Qwen3.6-27B"},
                    scores={"quality": 0.75},
                    promoted=True,
                )
            ],
        ),
    )

    assert complete["state"] == "succeeded"
    assert complete["artifact_ids"][0].startswith("art_")
    result = store.get_internal_job_result(job.job_id)
    assert result == {
        "artifact_ids": complete["artifact_ids"],
        "error": None,
        "job_id": job.job_id,
        "retryable": None,
        "successor_transition_id": None,
        "outputs": [
            {
                "artifact_id": complete["artifact_ids"][0],
                "compatibility": {"base_model": "Qwen/Qwen3.6-27B"},
                "created_at": result["outputs"][0]["created_at"],
                "lineage": {"openevo_execution": {"job_id": job.job_id}},
                "manifest": {
                    "adapter_format": "lora",
                    "base_model": "Qwen/Qwen3.6-27B",
                    "content_path": "adapter.bin",
                },
                "name": "pmem_calc",
                "payload_byte_size": 7,
                "payload_file_count": 1,
                "payload_manifest_digest": payload_tree_digest(
                    (
                        PayloadManifestEntry(
                            relative_path="adapter.bin",
                            media_type="application/octet-stream",
                            size_bytes=7,
                            sha256=hashlib.sha256(b"adapter").hexdigest(),
                        ),
                    )
                ),
                "promoted": True,
                "scores": {"quality": 0.75},
                "type": "parametric_memory",
            }
        ],
        "state": "succeeded",
    }
    assert set(result["outputs"][0]) == {
        "artifact_id",
        "type",
        "name",
        "manifest",
        "lineage",
        "compatibility",
        "scores",
        "promoted",
        "created_at",
        "payload_manifest_digest",
        "payload_byte_size",
        "payload_file_count",
    }
    serialized = json.dumps(result, sort_keys=True)
    assert str(store.files.root) not in serialized
    assert adapter.as_uri() not in serialized
    assert "uri" not in result["outputs"][0]
    assert "payload_handle" not in result["outputs"][0]
    with store.connect() as conn:
        row = conn.execute(
            "SELECT state, error FROM jobs WHERE job_id = ?",
            (job.job_id,),
        ).fetchone()
        lineage_rows = conn.execute(
            """
            SELECT parent_artifact_id, child_artifact_id, relation
            FROM artifact_lineage
            WHERE child_artifact_id = ?
            """,
            (complete["artifact_ids"][0],),
        ).fetchall()
    assert row["state"] == "succeeded"
    assert row["error"] is None
    assert [
        (lineage["parent_artifact_id"], lineage["child_artifact_id"], lineage["relation"])
        for lineage in lineage_rows
    ] == [(dataset.artifact_id, complete["artifact_ids"][0], "job_input")]


def test_internal_job_result_does_not_expose_worker_error_text_or_scan_payloads(
    tmp_path,
    monkeypatch,
):
    store = EvolutionStore(db_path=tmp_path / "evolution.db", artifact_root=tmp_path / "artifacts")
    store.initialize()
    job = store.create_job(
        JobCreateRequest(
            method="mock_lora",
            job_type="parametric_memory_train",
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
    store.fail_job(
        job.job_id,
        WorkerFailRequest(
            lease_id=claim.job.lease_id,
            error="private worker failure at /srv/secret",
            retryable=False,
        ),
    )

    def unexpected_payload_service(*args, **kwargs):
        raise AssertionError("non-succeeded jobs must not scan artifact payloads")

    monkeypatch.setattr(store_module, "ArtifactPayloadService", unexpected_payload_service)

    assert store.get_internal_job_result(job.job_id) == {
        "artifact_ids": [],
        "error": "evolution_job_failed",
        "job_id": job.job_id,
        "retryable": False,
        "state": "failed",
        "successor_transition_id": None,
    }


def test_internal_job_result_projects_ordered_file_and_directory_outputs(
    tmp_path,
    monkeypatch,
):
    store = EvolutionStore(db_path=tmp_path / "evolution.db", artifact_root=tmp_path / "artifacts")
    store.initialize()
    memory_path = store.files.root / "outputs" / "memory.md"
    skill_dir = store.files.root / "outputs" / "skill"
    inactive_path = store.files.root / "outputs" / "inactive.txt"
    memory_path.parent.mkdir()
    memory_path.write_text("remember", encoding="utf-8")
    inactive_path.write_text("inactive", encoding="utf-8")
    skill_dir.mkdir()
    (skill_dir / "SKILL.md").write_text("# Skill\n", encoding="utf-8")
    (skill_dir / "helpers").mkdir()
    (skill_dir / "helpers" / "run.txt").write_text("run", encoding="utf-8")
    job = store.create_job(
        JobCreateRequest(method="output_projection", job_type="projection_test")
    )
    claim = store.claim_job(
        WorkerClaimRequest(
            worker_id="worker_1",
            capabilities=["projection_test"],
            lease_seconds=60,
        )
    )
    assert claim.job is not None
    completed = store.complete_job(
        job.job_id,
        WorkerCompleteRequest(
            lease_id=claim.job.lease_id,
            artifacts=[
                ArtifactRegisterRequest(
                    type=ArtifactType.TEXT_MEMORY,
                    name="memory",
                    uri=memory_path.as_uri(),
                    manifest={"content_path": "memory.md"},
                    lineage={"openevo_execution": {"job_id": job.job_id}},
                ),
                ArtifactRegisterRequest(
                    type=ArtifactType.SKILL_BUNDLE,
                    name="skill",
                    uri=skill_dir.as_uri(),
                    lineage={"openevo_execution": {"job_id": job.job_id}},
                    compatibility={"agent_harness": "codex"},
                    promoted=True,
                ),
                ArtifactRegisterRequest(
                    type=ArtifactType.REPORT,
                    name="inactive",
                    uri=inactive_path.as_uri(),
                    manifest={"content_path": "inactive.txt"},
                    lineage={"openevo_execution": {"job_id": job.job_id}},
                ),
            ],
        ),
    )
    with store.connect() as conn:
        conn.execute(
            "UPDATE artifacts SET created_at = ? WHERE artifact_id = ?",
            ("2026-07-15T00:00:02Z", completed["artifact_ids"][0]),
        )
        conn.execute(
            "UPDATE artifacts SET created_at = ? WHERE artifact_id = ?",
            ("2026-07-15T00:00:01Z", completed["artifact_ids"][1]),
        )
        conn.execute(
            "UPDATE artifacts SET state = ? WHERE artifact_id = ?",
            (str(ArtifactState.DEPRECATED), completed["artifact_ids"][2]),
        )
        conn.commit()
    inactive_path.unlink()
    payload_services = []

    def recording_payload_service(root):
        service = artifact_payloads_module.ArtifactPayloadService(root)
        payload_services.append(service)
        return service

    monkeypatch.setattr(store_module, "ArtifactPayloadService", recording_payload_service)

    result = store.get_internal_job_result(job.job_id)

    assert result["artifact_ids"] == list(reversed(completed["artifact_ids"][:2]))
    assert len(payload_services) == 1
    assert [output["name"] for output in result["outputs"]] == ["skill", "memory"]
    assert result["outputs"][0]["payload_byte_size"] == len(b"# Skill\nrun")
    assert result["outputs"][0]["payload_file_count"] == 2
    assert result["outputs"][1]["payload_byte_size"] == len(b"remember")
    assert result["outputs"][1]["payload_file_count"] == 1


@pytest.mark.parametrize(
    "payload_kind",
    ["outside", "missing", "symlink", "hardlink", "scheme"],
)
def test_internal_job_result_rejects_untrusted_payloads(tmp_path, payload_kind):
    store = EvolutionStore(db_path=tmp_path / "evolution.db", artifact_root=tmp_path / "artifacts")
    store.initialize()
    managed = store.files.root / "outputs"
    managed.mkdir()
    external = tmp_path / "external.txt"
    external.write_text("secret", encoding="utf-8")
    payload = managed / "payload.txt"
    if payload_kind == "outside":
        payload = external
    elif payload_kind == "missing":
        pass
    elif payload_kind == "symlink":
        payload.symlink_to(external)
    elif payload_kind == "hardlink":
        os.link(external, payload)
    uri = "https://example.invalid/output" if payload_kind == "scheme" else payload.as_uri()
    job = store.create_job(
        JobCreateRequest(method="output_projection", job_type="projection_test")
    )
    claim = store.claim_job(
        WorkerClaimRequest(
            worker_id="worker_1",
            capabilities=["projection_test"],
            lease_seconds=60,
        )
    )
    assert claim.job is not None
    store.complete_job(
        job.job_id,
        WorkerCompleteRequest(
            lease_id=claim.job.lease_id,
            artifacts=[
                ArtifactRegisterRequest(
                    type=ArtifactType.REPORT,
                    name="untrusted",
                    uri=uri,
                    manifest={"content_path": "payload.txt"},
                    lineage={"openevo_execution": {"job_id": job.job_id}},
                )
            ],
        ),
    )

    with pytest.raises(ValueError):
        store.get_internal_job_result(job.job_id)


def test_internal_job_result_rejects_payload_identity_drift(tmp_path, monkeypatch):
    store = EvolutionStore(db_path=tmp_path / "evolution.db", artifact_root=tmp_path / "artifacts")
    store.initialize()
    payload = store.files.root / "outputs" / "payload.txt"
    payload.parent.mkdir()
    payload.write_text("original", encoding="utf-8")
    job = store.create_job(
        JobCreateRequest(method="output_projection", job_type="projection_test")
    )
    claim = store.claim_job(
        WorkerClaimRequest(
            worker_id="worker_1",
            capabilities=["projection_test"],
            lease_seconds=60,
        )
    )
    assert claim.job is not None
    store.complete_job(
        job.job_id,
        WorkerCompleteRequest(
            lease_id=claim.job.lease_id,
            artifacts=[
                ArtifactRegisterRequest(
                    type=ArtifactType.REPORT,
                    name="drifting",
                    uri=payload.as_uri(),
                    manifest={"content_path": "payload.txt"},
                    lineage={"openevo_execution": {"job_id": job.job_id}},
                )
            ],
        ),
    )
    original_stream = artifact_payloads_module._stream_fd_chunks
    mutated = False

    def replace_during_hash(fd):
        nonlocal mutated
        if not mutated:
            mutated = True
            payload.rename(payload.with_suffix(".old"))
            payload.write_text("replacement", encoding="utf-8")
        yield from original_stream(fd)

    monkeypatch.setattr(artifact_payloads_module, "_stream_fd_chunks", replace_during_hash)

    with pytest.raises(ValueError, match="drift|changed|mutated|safely"):
        store.get_internal_job_result(job.job_id)


def test_internal_job_result_reads_job_and_outputs_from_one_sqlite_snapshot(
    tmp_path,
    monkeypatch,
):
    store = EvolutionStore(db_path=tmp_path / "evolution.db", artifact_root=tmp_path / "artifacts")
    store.initialize()
    writer = EvolutionStore(db_path=tmp_path / "evolution.db", artifact_root=tmp_path / "artifacts")
    raced_payload = store.files.root / "outputs" / "raced-adapter.bin"
    raced_payload.parent.mkdir()
    raced_payload.write_bytes(b"raced")
    job = store.create_job(
        JobCreateRequest(
            method="mock_lora",
            job_type="parametric_memory_train",
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
    original_connect = store.connect
    completed_artifact_ids: list[str] = []
    statements: list[str] = []

    class CursorAfterJobRead:
        def __init__(self, cursor):
            self._cursor = cursor

        def fetchone(self):
            row = self._cursor.fetchone()
            completed = writer.complete_job(
                job.job_id,
                WorkerCompleteRequest(
                    lease_id=claim.job.lease_id,
                    artifacts=[
                        ArtifactRegisterRequest(
                            type=ArtifactType.PARAMETRIC_MEMORY,
                            name="raced-output",
                            uri=raced_payload.as_uri(),
                            manifest={
                                "base_model": "Qwen/Qwen3.6-27B",
                                "adapter_format": "lora",
                                "content_path": "raced-adapter.bin",
                            },
                            lineage={"openevo_execution": {"job_id": job.job_id}},
                            compatibility={"base_model": "Qwen/Qwen3.6-27B"},
                        )
                    ],
                ),
            )
            completed_artifact_ids.extend(completed["artifact_ids"])
            return row

    class ConnectionAfterJobRead:
        def __init__(self, connection):
            self._connection = connection

        def execute(self, sql, parameters=()):
            statements.append(sql)
            cursor = self._connection.execute(sql, parameters)
            if "SELECT job_id, state, error FROM jobs" in sql:
                return CursorAfterJobRead(cursor)
            return cursor

        def __getattr__(self, name):
            return getattr(self._connection, name)

    @contextmanager
    def raced_connect():
        with original_connect() as connection:
            yield ConnectionAfterJobRead(connection)

    monkeypatch.setattr(store, "connect", raced_connect)

    raced_result = store.get_internal_job_result(job.job_id)
    assert raced_result == {
        "artifact_ids": [],
        "error": None,
        "job_id": job.job_id,
        "retryable": None,
        "state": "claimed",
        "successor_transition_id": None,
    }
    assert statements[0] == "BEGIN"
    monkeypatch.setattr(store, "connect", original_connect)
    completed_result = store.get_internal_job_result(job.job_id)
    assert completed_result == {
        "artifact_ids": completed_artifact_ids,
        "error": None,
        "job_id": job.job_id,
        "retryable": None,
        "successor_transition_id": None,
        "outputs": [
            {
                "artifact_id": completed_artifact_ids[0],
                "compatibility": {"base_model": "Qwen/Qwen3.6-27B"},
                "created_at": completed_result["outputs"][0]["created_at"],
                "lineage": {"openevo_execution": {"job_id": job.job_id}},
                "manifest": {
                    "adapter_format": "lora",
                    "base_model": "Qwen/Qwen3.6-27B",
                    "content_path": "raced-adapter.bin",
                },
                "name": "raced-output",
                "payload_byte_size": 5,
                "payload_file_count": 1,
                "payload_manifest_digest": payload_tree_digest(
                    (
                        PayloadManifestEntry(
                            relative_path="raced-adapter.bin",
                            media_type="application/octet-stream",
                            size_bytes=5,
                            sha256=hashlib.sha256(b"raced").hexdigest(),
                        ),
                    )
                ),
                "promoted": False,
                "scores": {},
                "type": "parametric_memory",
            }
        ],
        "state": "succeeded",
    }


def test_internal_job_result_enforces_output_count_before_payload_scanning(
    tmp_path,
    monkeypatch,
):
    store = EvolutionStore(db_path=tmp_path / "evolution.db", artifact_root=tmp_path / "artifacts")
    store.initialize()
    outputs = store.files.root / "outputs"
    outputs.mkdir()
    payloads = []
    for index in range(2):
        payload = outputs / f"{index}.txt"
        payload.write_text(str(index), encoding="utf-8")
        payloads.append(payload)
    job = store.create_job(
        JobCreateRequest(method="output_projection", job_type="projection_test")
    )
    claim = store.claim_job(
        WorkerClaimRequest(
            worker_id="worker_1",
            capabilities=["projection_test"],
            lease_seconds=60,
        )
    )
    assert claim.job is not None
    store.complete_job(
        job.job_id,
        WorkerCompleteRequest(
            lease_id=claim.job.lease_id,
            artifacts=[
                ArtifactRegisterRequest(
                    type=ArtifactType.REPORT,
                    name=f"output-{index}",
                    uri=payload.as_uri(),
                    manifest={"content_path": payload.name},
                    lineage={"openevo_execution": {"job_id": job.job_id}},
                )
                for index, payload in enumerate(payloads)
            ],
        ),
    )
    monkeypatch.setattr(store_module, "MAX_INTERNAL_JOB_OUTPUTS", 1)

    def unexpected_payload_service(*args, **kwargs):
        raise AssertionError("over-count results must fail before scanning")

    monkeypatch.setattr(store_module, "ArtifactPayloadService", unexpected_payload_service)

    with pytest.raises(ValueError, match="count exceeds"):
        store.get_internal_job_result(job.job_id)


def test_internal_job_result_enforces_serialized_byte_bound(tmp_path, monkeypatch):
    store = EvolutionStore(db_path=tmp_path / "evolution.db", artifact_root=tmp_path / "artifacts")
    store.initialize()
    payload = store.files.root / "outputs" / "result.txt"
    payload.parent.mkdir()
    payload.write_text("result", encoding="utf-8")
    job = store.create_job(
        JobCreateRequest(method="output_projection", job_type="projection_test")
    )
    claim = store.claim_job(
        WorkerClaimRequest(
            worker_id="worker_1",
            capabilities=["projection_test"],
            lease_seconds=60,
        )
    )
    assert claim.job is not None
    store.complete_job(
        job.job_id,
        WorkerCompleteRequest(
            lease_id=claim.job.lease_id,
            artifacts=[
                ArtifactRegisterRequest(
                    type=ArtifactType.REPORT,
                    name="large-metadata",
                    uri=payload.as_uri(),
                    manifest={"content_path": "result.txt", "summary": "x" * 512},
                    lineage={"openevo_execution": {"job_id": job.job_id}},
                )
            ],
        ),
    )
    monkeypatch.setattr(store_module, "MAX_INTERNAL_JOB_RESULT_BYTES", 256)

    with pytest.raises(ValueError, match="serialized byte bound"):
        store.get_internal_job_result(job.job_id)


def test_complete_job_honors_unpromoted_job_config(tmp_path):
    store = EvolutionStore(db_path=tmp_path / "evolution.db", artifact_root=tmp_path / "artifacts")
    store.initialize()
    job = store.create_job(
        JobCreateRequest(
            method="text_memory_reflector",
            job_type="text_memory_mining",
            config={"promoted": False},
        )
    )
    claim = store.claim_job(
        WorkerClaimRequest(
            worker_id="worker_1",
            capabilities=["text_memory_mining"],
            lease_seconds=60,
        )
    )
    assert claim.job is not None

    complete = store.complete_job(
        job.job_id,
        WorkerCompleteRequest(
            lease_id=claim.job.lease_id,
            artifacts=[
                ArtifactRegisterRequest(
                    type=ArtifactType.TEXT_MEMORY,
                    name="worker-promoted-memory",
                    uri="file:///tmp/memory.md",
                    promoted=True,
                )
            ],
        ),
    )

    artifact = store.get_artifact(str(complete["artifact_ids"][0]))

    assert artifact.promoted is False


def test_complete_job_records_feedback_applications_for_consumed_human_feedback(tmp_path):
    store = EvolutionStore(db_path=tmp_path / "evolution.db", artifact_root=tmp_path / "artifacts")
    store.initialize()
    review = store.create_review_request(
        ReviewRequestCreateRequest(
            review_type="promotion",
            artifact_ids=["art_candidate"],
            packet={"questions": ["Approve?"]},
        )
    )
    store.claim_review_request(review.review_id, ReviewClaimRequest(reviewer_id="alice"))
    feedback = store.submit_human_feedback(
        review.review_id,
        HumanFeedbackCreateRequest(
            reviewer_id="alice",
            decision="revise",
            suggested_changes=["Use bounded source inventory."],
        ),
    )
    job = store.create_job(
        JobCreateRequest(
            method="agent_system_history_reflector",
            job_type="agent_system",
            config={"promoted": False},
        )
    )
    claim = store.claim_job(
        WorkerClaimRequest(
            worker_id="worker_1",
            capabilities=["agent_system"],
            lease_seconds=60,
        )
    )
    assert claim.job is not None

    complete = store.complete_job(
        job.job_id,
        WorkerCompleteRequest(
            lease_id=claim.job.lease_id,
            artifacts=[
                ArtifactRegisterRequest(
                    type=ArtifactType.AGENT_SYSTEM,
                    name="feedback-aware-agent-system",
                    uri="file:///tmp/AGENTS.md",
                    manifest={
                        "target_path": "AGENTS.md",
                        "method": "spoofed_manifest_method",
                        "human_feedback_ids": [
                            feedback.feedback_id,
                            "hfb_external_dataset_feedback",
                        ],
                        "human_feedback_application_summary": (
                            "Applied feedback from memory.md?signature=relative-secret#frag "
                            "and ?signature=relative-query-secret#frag with "
                            "bearer:summary-token."
                        ),
                    },
                    promoted=True,
                )
            ],
        ),
    )

    artifact_id = str(complete["artifact_ids"][0])
    applications = store.list_feedback_applications(feedback_id=feedback.feedback_id)
    updated_feedback = store.list_human_feedback(review_id=review.review_id)[0]

    assert complete["state"] == "succeeded"
    assert updated_feedback.status == "consumed"
    assert len(applications) == 1
    assert applications[0].feedback_id == feedback.feedback_id
    assert applications[0].target_type == "prompt_seed"
    assert applications[0].target_id == artifact_id
    assert applications[0].consumed_by_method == "agent_system_history_reflector"
    assert applications[0].consumed_in_job_id == job.job_id
    assert "relative-secret" not in applications[0].effect_summary
    assert "relative-query-secret" not in applications[0].effect_summary
    assert "summary-token" not in applications[0].effect_summary
    assert "memory.md?<redacted>" in applications[0].effect_summary
    assert "[REDACTED]" in applications[0].effect_summary


def test_create_job_rejects_missing_input_artifact_ids_without_writes(tmp_path):
    store = EvolutionStore(db_path=tmp_path / "evolution.db", artifact_root=tmp_path / "artifacts")
    store.initialize()
    existing = store.register_artifact(
        ArtifactRegisterRequest(
            type=ArtifactType.DATASET,
            name="dataset",
            uri="file:///tmp/dataset.json",
        )
    )

    with pytest.raises(ValueError, match="unknown input artifact_id.*art_missing"):
        store.create_job(
            JobCreateRequest(
                method="mock",
                job_type="text_memory_mining",
                input_artifact_ids=[existing.artifact_id, "art_missing"],
            )
        )

    empty_input_job = store.create_job(
        JobCreateRequest(method="mock", job_type="text_memory_mining")
    )

    with store.connect() as conn:
        jobs = conn.execute("SELECT job_id, input_artifact_ids_json FROM jobs").fetchall()

    assert [(job["job_id"], json.loads(job["input_artifact_ids_json"])) for job in jobs] == [
        (empty_input_job.job_id, [])
    ]


def test_job_failure_records_error(tmp_path):
    store = EvolutionStore(db_path=tmp_path / "evolution.db", artifact_root=tmp_path / "artifacts")
    store.initialize()
    job = store.create_job(JobCreateRequest(method="mock", job_type="text_memory_mining"))
    claim = store.claim_job(
        WorkerClaimRequest(worker_id="worker_1", capabilities=["text_memory_mining"])
    )
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
    claim = store.claim_job(
        WorkerClaimRequest(worker_id="worker_1", capabilities=["text_memory_mining"])
    )
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
        WorkerHeartbeatRequest(
            lease_id=claim.job.lease_id, progress=0.25, message="still running"
        ),
    )

    with store.connect() as conn:
        row = conn.execute(
            "SELECT state, lease_expires_at FROM jobs WHERE job_id = ?",
            (job.job_id,),
        ).fetchone()
    assert result["state"] == "running"
    assert row["state"] == "running"
    assert _parse_utc(row["lease_expires_at"]) > _parse_utc(original_expires_at)


def test_claim_persists_duration_and_heartbeat_renews_exact_short_lease(
    tmp_path,
    monkeypatch,
):
    fixed_now = datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC)

    class FixedDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return fixed_now if tz is not None else fixed_now.replace(tzinfo=None)

    monkeypatch.setattr(store_module, "datetime", FixedDateTime)
    store = EvolutionStore(
        db_path=tmp_path / "evolution.db",
        artifact_root=tmp_path / "artifacts",
    )
    store.initialize()
    job = store.create_job(
        JobCreateRequest(method="mock", job_type="text_memory_mining")
    )
    claim = store.claim_job(
        WorkerClaimRequest(
            worker_id="worker_1",
            capabilities=["text_memory_mining"],
            lease_seconds=2,
        )
    )
    assert claim.job is not None

    fixed_now += timedelta(seconds=1)
    result = store.heartbeat_job(
        job.job_id,
        WorkerHeartbeatRequest(lease_id=claim.job.lease_id),
    )

    with store.connect() as conn:
        row = conn.execute(
            """
            SELECT lease_duration_seconds, lease_expires_at
            FROM jobs WHERE job_id = ?
            """,
            (job.job_id,),
        ).fetchone()
    assert row["lease_duration_seconds"] == 2
    assert _parse_utc(row["lease_expires_at"]) == fixed_now + timedelta(seconds=2)
    assert _parse_utc(str(result["lease_expires_at"])) == fixed_now + timedelta(seconds=2)


def test_heartbeat_backfills_legacy_active_null_lease_duration(tmp_path, monkeypatch):
    fixed_now = datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC)

    class FixedDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return fixed_now if tz is not None else fixed_now.replace(tzinfo=None)

    monkeypatch.setattr(store_module, "datetime", FixedDateTime)
    store = EvolutionStore(
        db_path=tmp_path / "evolution.db",
        artifact_root=tmp_path / "artifacts",
    )
    store.initialize()
    job = store.create_job(
        JobCreateRequest(method="mock", job_type="text_memory_mining")
    )
    claim = store.claim_job(
        WorkerClaimRequest(
            worker_id="worker_1",
            capabilities=["text_memory_mining"],
            lease_seconds=7,
        )
    )
    assert claim.job is not None
    with store.connect() as conn:
        conn.execute(
            """
            UPDATE jobs
            SET lease_duration_seconds = NULL, updated_at = ?, lease_expires_at = ?
            WHERE job_id = ?
            """,
            (
                fixed_now.isoformat().replace("+00:00", "Z"),
                (fixed_now + timedelta(seconds=7)).isoformat().replace("+00:00", "Z"),
                job.job_id,
            ),
        )
        conn.commit()

    store = EvolutionStore(
        db_path=tmp_path / "evolution.db",
        artifact_root=tmp_path / "artifacts",
    )
    store.initialize()
    with store.connect() as conn:
        migrated_duration = conn.execute(
            "SELECT lease_duration_seconds FROM jobs WHERE job_id = ?",
            (job.job_id,),
        ).fetchone()["lease_duration_seconds"]
    assert migrated_duration == 7

    fixed_now += timedelta(seconds=2)
    store.heartbeat_job(
        job.job_id,
        WorkerHeartbeatRequest(lease_id=claim.job.lease_id),
    )

    with store.connect() as conn:
        row = conn.execute(
            """
            SELECT lease_duration_seconds, lease_expires_at
            FROM jobs WHERE job_id = ?
            """,
            (job.job_id,),
        ).fetchone()
    assert row["lease_duration_seconds"] == 7
    assert _parse_utc(row["lease_expires_at"]) == fixed_now + timedelta(seconds=7)


def test_heartbeat_does_not_shorten_long_active_lease(tmp_path):
    store = EvolutionStore(db_path=tmp_path / "evolution.db", artifact_root=tmp_path / "artifacts")
    store.initialize()
    job = store.create_job(JobCreateRequest(method="mock", job_type="text_memory_mining"))
    claim = store.claim_job(
        WorkerClaimRequest(
            worker_id="worker_1",
            capabilities=["text_memory_mining"],
            lease_seconds=24 * 60 * 60,
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
        WorkerHeartbeatRequest(
            lease_id=claim.job.lease_id, progress=0.25, message="still running"
        ),
    )

    with store.connect() as conn:
        renewed_expires_at = conn.execute(
            "SELECT lease_expires_at FROM jobs WHERE job_id = ?",
            (job.job_id,),
        ).fetchone()["lease_expires_at"]
    assert result["state"] == "running"
    assert _parse_utc(renewed_expires_at) >= _parse_utc(original_expires_at)


def test_complete_job_invalid_artifact_marks_failed_and_cleans_registered_artifacts(tmp_path):
    store = EvolutionStore(db_path=tmp_path / "evolution.db", artifact_root=tmp_path / "artifacts")
    store.initialize()
    job = store.create_job(JobCreateRequest(method="mock", job_type="text_memory_mining"))
    claim = store.claim_job(
        WorkerClaimRequest(worker_id="worker_1", capabilities=["text_memory_mining"])
    )
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


def test_complete_job_rejects_legacy_missing_input_artifact_and_cleans_outputs(tmp_path):
    store = EvolutionStore(db_path=tmp_path / "evolution.db", artifact_root=tmp_path / "artifacts")
    store.initialize()
    job_id = "job_legacy_missing_input"
    missing_artifact_id = "art_missing_legacy"
    now = store_module.utc_now_iso()
    with store.connect() as conn:
        conn.execute(
            """
            INSERT INTO jobs (
                job_id, job_type, method, state, priority, created_at,
                updated_at, input_artifact_ids_json, config_json, attempt_count
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                job_id,
                "text_memory_mining",
                "mock",
                str(JobState.PENDING),
                100,
                now,
                now,
                json.dumps([missing_artifact_id]),
                "{}",
                0,
            ),
        )
        conn.commit()
    claim = store.claim_job(
        WorkerClaimRequest(worker_id="worker_1", capabilities=["text_memory_mining"])
    )
    assert claim.job is not None
    assert claim.job.job_id == job_id
    assert claim.job.input_artifacts == []

    with pytest.raises(ValueError, match=rf"unknown input artifact_id.*{missing_artifact_id}"):
        store.complete_job(
            job_id,
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
            "SELECT state, error, lease_id, lease_expires_at FROM jobs WHERE job_id = ?",
            (job_id,),
        ).fetchone()
        artifact_count = conn.execute("SELECT COUNT(*) FROM artifacts").fetchone()[0]
        lineage_count = conn.execute("SELECT COUNT(*) FROM artifact_lineage").fetchone()[0]

    assert job_row["state"] == "failed"
    assert missing_artifact_id in job_row["error"]
    assert job_row["lease_id"] is None
    assert job_row["lease_expires_at"] is None
    assert artifact_count == 0
    assert lineage_count == 0
    assert not list((tmp_path / "artifacts" / "artifacts" / "text_memory").glob("*/manifest.json"))


def test_complete_job_final_update_failure_marks_failed_and_cleans_artifacts(tmp_path):
    store = EvolutionStore(db_path=tmp_path / "evolution.db", artifact_root=tmp_path / "artifacts")
    store.initialize()
    dataset = store.register_artifact(
        ArtifactRegisterRequest(
            type=ArtifactType.DATASET,
            name="dataset",
            uri="file:///tmp/dataset.json",
        )
    )
    job = store.create_job(
        JobCreateRequest(
            method="mock",
            job_type="text_memory_mining",
            input_artifact_ids=[dataset.artifact_id],
        )
    )
    claim = store.claim_job(
        WorkerClaimRequest(worker_id="worker_1", capabilities=["text_memory_mining"])
    )
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
        lineage_count = conn.execute(
            "SELECT COUNT(*) FROM artifact_lineage WHERE parent_artifact_id = ?",
            (dataset.artifact_id,),
        ).fetchone()[0]

    assert job_row["state"] == "failed"
    assert "forced job succeed failure" in job_row["error"]
    assert artifact_count == 1
    assert lineage_count == 0
    assert not list((tmp_path / "artifacts" / "artifacts" / "text_memory").glob("*/manifest.json"))


def test_complete_job_keeps_outputs_staged_until_success_is_committed(
    tmp_path,
    monkeypatch,
):
    store = EvolutionStore(
        db_path=tmp_path / "evolution.db",
        artifact_root=tmp_path / "artifacts",
    )
    store.initialize()
    job = store.create_job(
        JobCreateRequest(method="mock", job_type="text_memory_mining")
    )
    claim = store.claim_job(
        WorkerClaimRequest(worker_id="worker_1", capabilities=["text_memory_mining"])
    )
    assert claim.job is not None

    staged_artifact_ids: list[str] = []
    original_register_artifact = store._register_artifact

    def observe_staged_artifact(
        request: ArtifactRegisterRequest,
        *,
        initial_state: ArtifactState,
        staging_job_id: str | None = None,
    ):
        assert initial_state is ArtifactState.STAGED
        assert staging_job_id == job.job_id
        artifact = original_register_artifact(
            request,
            initial_state=initial_state,
            staging_job_id=staging_job_id,
        )
        staged_artifact_ids.append(artifact.artifact_id)
        with pytest.raises(ValueError, match="unknown artifact"):
            store.get_artifact(artifact.artifact_id)
        with store.connect() as conn:
            row = conn.execute(
                "SELECT state FROM artifacts WHERE artifact_id = ?",
                (artifact.artifact_id,),
            ).fetchone()
        assert row["state"] == "staged"
        return artifact

    monkeypatch.setattr(store, "_register_artifact", observe_staged_artifact)

    completed = store.complete_job(
        job.job_id,
        WorkerCompleteRequest(
            lease_id=claim.job.lease_id,
            artifacts=[
                ArtifactRegisterRequest(
                    type=ArtifactType.TEXT_MEMORY,
                    name="published",
                    uri="file:///tmp/published.md",
                )
            ],
        ),
    )

    assert completed["artifact_ids"] == staged_artifact_ids
    published = store.get_artifact(staged_artifact_ids[0])
    assert published.state is ArtifactState.ACTIVE
    with store.connect() as conn:
        row = conn.execute(
            "SELECT state FROM jobs WHERE job_id = ?",
            (job.job_id,),
        ).fetchone()
        artifact_row = conn.execute(
            "SELECT state, staging_job_id FROM artifacts WHERE artifact_id = ?",
            (staged_artifact_ids[0],),
        ).fetchone()
    assert row["state"] == "succeeded"
    assert artifact_row["state"] == "active"
    assert artifact_row["staging_job_id"] is None


def test_restart_reclaims_expired_job_staged_outputs(tmp_path):
    db_path = tmp_path / "evolution.db"
    artifact_root = tmp_path / "artifacts"
    store = EvolutionStore(db_path=db_path, artifact_root=artifact_root)
    store.initialize()
    job = store.create_job(
        JobCreateRequest(method="mock", job_type="text_memory_mining")
    )
    claim = store.claim_job(
        WorkerClaimRequest(worker_id="worker-1", capabilities=["text_memory_mining"])
    )
    assert claim.job is not None
    staged = store._register_artifact(
        ArtifactRegisterRequest(
            type=ArtifactType.TEXT_MEMORY,
            name="crash-window",
            uri="file:///tmp/crash-window.md",
            promoted=True,
        ),
        initial_state=ArtifactState.STAGED,
        staging_job_id=job.job_id,
    )
    with store.connect() as conn:
        manifest_path = Path(
            conn.execute(
                "SELECT manifest_path FROM artifacts WHERE artifact_id = ?",
                (staged.artifact_id,),
            ).fetchone()["manifest_path"]
        )
        conn.execute(
            "UPDATE jobs SET state = ?, lease_expires_at = ? WHERE job_id = ?",
            (
                str(JobState.RUNNING),
                (datetime.now(UTC) - timedelta(seconds=1)).isoformat(),
                job.job_id,
            ),
        )
        conn.commit()
    assert manifest_path.is_file()

    restarted = EvolutionStore(db_path=db_path, artifact_root=artifact_root)
    restarted.initialize()

    with restarted.connect() as conn:
        job_row = conn.execute(
            "SELECT state FROM jobs WHERE job_id = ?",
            (job.job_id,),
        ).fetchone()
        staged_count = conn.execute(
            "SELECT COUNT(*) FROM artifacts WHERE staging_job_id = ?",
            (job.job_id,),
        ).fetchone()[0]
    assert job_row["state"] == "pending"
    assert staged_count == 0
    assert not manifest_path.exists()
    reclaimed = restarted.claim_job(
        WorkerClaimRequest(worker_id="worker-2", capabilities=["text_memory_mining"])
    )
    assert reclaimed.job is not None
    assert reclaimed.job.job_id == job.job_id


def test_restart_reclaims_manifest_left_after_staged_db_delete(tmp_path):
    db_path = tmp_path / "evolution.db"
    artifact_root = tmp_path / "artifacts"
    store = EvolutionStore(db_path=db_path, artifact_root=artifact_root)
    store.initialize()
    job = store.create_job(
        JobCreateRequest(method="mock", job_type="text_memory_mining")
    )
    staged = store._register_artifact(
        ArtifactRegisterRequest(
            type=ArtifactType.TEXT_MEMORY,
            name="db-delete-unlink-window",
            uri="file:///tmp/db-delete-unlink-window.md",
        ),
        initial_state=ArtifactState.STAGED,
        staging_job_id=job.job_id,
    )
    with store.connect() as conn:
        manifest_path = Path(
            conn.execute(
                "SELECT manifest_path FROM artifacts WHERE artifact_id = ?",
                (staged.artifact_id,),
            ).fetchone()["manifest_path"]
        )
        conn.execute("DELETE FROM artifacts WHERE artifact_id = ?", (staged.artifact_id,))
        conn.commit()
    assert manifest_path.is_file()

    EvolutionStore(db_path=db_path, artifact_root=artifact_root).initialize()

    assert not manifest_path.exists()
    assert not manifest_path.parent.exists()


def test_restart_reclaims_malformed_managed_orphan_manifest(tmp_path):
    artifact_root = tmp_path / "artifacts"
    store = EvolutionStore(
        db_path=tmp_path / "evolution.db",
        artifact_root=artifact_root,
    )
    store.initialize()
    orphan = (
        artifact_root
        / "artifacts"
        / "text_memory"
        / "art_malformed_orphan"
        / "manifest.json"
    )
    orphan.parent.mkdir(parents=True)
    orphan.write_text("{partial", encoding="utf-8")

    EvolutionStore(
        db_path=tmp_path / "evolution.db",
        artifact_root=artifact_root,
    ).initialize()

    assert not orphan.exists()
    assert not orphan.parent.exists()


def test_fail_job_never_unlinks_manifest_path_outside_artifact_root(tmp_path):
    store = EvolutionStore(
        db_path=tmp_path / "evolution.db",
        artifact_root=tmp_path / "artifacts",
    )
    store.initialize()
    job = store.create_job(
        JobCreateRequest(method="mock", job_type="text_memory_mining")
    )
    claim = store.claim_job(
        WorkerClaimRequest(worker_id="worker-1", capabilities=["text_memory_mining"])
    )
    assert claim.job is not None
    staged = store._register_artifact(
        ArtifactRegisterRequest(
            type=ArtifactType.TEXT_MEMORY,
            name="unsafe-manifest-path",
            uri="file:///tmp/unsafe-manifest-path.md",
        ),
        initial_state=ArtifactState.STAGED,
        staging_job_id=job.job_id,
    )
    external = tmp_path / "must-not-delete.json"
    external.write_text("keep", encoding="utf-8")
    with store.connect() as conn:
        conn.execute(
            "UPDATE artifacts SET manifest_path = ? WHERE artifact_id = ?",
            (str(external), staged.artifact_id),
        )
        conn.commit()

    store.fail_job(
        job.job_id,
        WorkerFailRequest(
            lease_id=claim.job.lease_id,
            error="failure with tampered manifest path",
            retryable=False,
        ),
    )

    assert external.read_text(encoding="utf-8") == "keep"


def test_restart_orphan_scan_does_not_follow_external_symlink(tmp_path):
    db_path = tmp_path / "evolution.db"
    artifact_root = tmp_path / "artifacts"
    store = EvolutionStore(db_path=db_path, artifact_root=artifact_root)
    store.initialize()
    external_artifact_dir = tmp_path / "external" / "art_external"
    external_artifact_dir.mkdir(parents=True)
    external_manifest = external_artifact_dir / "manifest.json"
    external_manifest.write_text(
        json.dumps(
            {
                "artifact_id": "art_external",
                "type": "text_memory",
                "name": "external",
                "uri": "file:///tmp/external.md",
                "manifest": {},
                "lineage": {},
                "compatibility": {},
                "scores": {},
                "tags": [],
                "promoted": False,
            }
        ),
        encoding="utf-8",
    )
    linked_dir = artifact_root / "artifacts" / "text_memory" / "art_external"
    linked_dir.symlink_to(external_artifact_dir, target_is_directory=True)

    EvolutionStore(db_path=db_path, artifact_root=artifact_root).initialize()

    assert external_manifest.is_file()
    assert linked_dir.is_symlink()


def test_restart_preserves_live_staging_until_job_fails(tmp_path):
    db_path = tmp_path / "evolution.db"
    artifact_root = tmp_path / "artifacts"
    store = EvolutionStore(db_path=db_path, artifact_root=artifact_root)
    store.initialize()
    job = store.create_job(
        JobCreateRequest(method="mock", job_type="text_memory_mining")
    )
    claim = store.claim_job(
        WorkerClaimRequest(worker_id="worker-1", capabilities=["text_memory_mining"])
    )
    assert claim.job is not None
    staged = store._register_artifact(
        ArtifactRegisterRequest(
            type=ArtifactType.TEXT_MEMORY,
            name="live-stage",
            uri="file:///tmp/live-stage.md",
        ),
        initial_state=ArtifactState.STAGED,
        staging_job_id=job.job_id,
    )

    restarted = EvolutionStore(db_path=db_path, artifact_root=artifact_root)
    restarted.initialize()
    with restarted.connect() as conn:
        staged_row = conn.execute(
            "SELECT manifest_path FROM artifacts WHERE artifact_id = ?",
            (staged.artifact_id,),
        ).fetchone()
    assert staged_row is not None
    manifest_path = Path(staged_row["manifest_path"])
    assert manifest_path.is_file()

    restarted.fail_job(
        job.job_id,
        WorkerFailRequest(
            lease_id=claim.job.lease_id,
            error="simulated worker failure",
            retryable=False,
        ),
    )

    with restarted.connect() as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM artifacts WHERE artifact_id = ?",
            (staged.artifact_id,),
        ).fetchone()[0] == 0
    assert not manifest_path.exists()


def test_complete_job_reclaims_staging_from_an_interrupted_attempt(tmp_path):
    store = EvolutionStore(
        db_path=tmp_path / "evolution.db",
        artifact_root=tmp_path / "artifacts",
    )
    store.initialize()
    job = store.create_job(
        JobCreateRequest(method="mock", job_type="text_memory_mining")
    )
    claim = store.claim_job(
        WorkerClaimRequest(worker_id="worker-1", capabilities=["text_memory_mining"])
    )
    assert claim.job is not None
    stale = store._register_artifact(
        ArtifactRegisterRequest(
            type=ArtifactType.TEXT_MEMORY,
            name="interrupted-attempt",
            uri="file:///tmp/interrupted-attempt.md",
        ),
        initial_state=ArtifactState.STAGED,
        staging_job_id=job.job_id,
    )
    with store.connect() as conn:
        stale_manifest_path = Path(
            conn.execute(
                "SELECT manifest_path FROM artifacts WHERE artifact_id = ?",
                (stale.artifact_id,),
            ).fetchone()["manifest_path"]
        )

    completed = store.complete_job(
        job.job_id,
        WorkerCompleteRequest(
            lease_id=claim.job.lease_id,
            artifacts=[
                ArtifactRegisterRequest(
                    type=ArtifactType.TEXT_MEMORY,
                    name="retry-output",
                    uri="file:///tmp/retry-output.md",
                )
            ],
        ),
    )

    with store.connect() as conn:
        stale_count = conn.execute(
            "SELECT COUNT(*) FROM artifacts WHERE artifact_id = ?",
            (stale.artifact_id,),
        ).fetchone()[0]
        published_row = conn.execute(
            "SELECT state, staging_job_id FROM artifacts WHERE artifact_id = ?",
            (completed["artifact_ids"][0],),
        ).fetchone()
    assert stale_count == 0
    assert not stale_manifest_path.exists()
    assert published_row["state"] == "active"
    assert published_row["staging_job_id"] is None


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

    original_register_artifact = store._register_artifact

    def expire_lease_after_register(
        request: ArtifactRegisterRequest,
        *,
        initial_state,
        staging_job_id=None,
    ):
        artifact = original_register_artifact(
            request,
            initial_state=initial_state,
            staging_job_id=staging_job_id,
        )
        expired_at = (
            (datetime.now(UTC) - timedelta(seconds=1))
            .isoformat()
            .replace(
                "+00:00",
                "Z",
            )
        )
        with store.connect() as conn:
            conn.execute(
                "UPDATE jobs SET state = ?, lease_expires_at = ? WHERE job_id = ?",
                (str(JobState.RUNNING), expired_at, job.job_id),
            )
            conn.commit()
        return artifact

    monkeypatch.setattr(store, "_register_artifact", expire_lease_after_register)

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
