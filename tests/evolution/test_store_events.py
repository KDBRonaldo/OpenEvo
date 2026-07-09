from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3

import pytest

from openevo.evolution.files import ArtifactFileStore
from openevo.evolution.models import EventIngestRequest
from openevo.evolution.store import EvolutionStore


def test_store_initializes_schema(tmp_path):
    db_path = tmp_path / "evolution.db"
    store = EvolutionStore(db_path=db_path, artifact_root=tmp_path / "artifacts")
    store.initialize()

    with sqlite3.connect(db_path) as conn:
        tables = {
            row[0]
            for row in conn.execute(
                "select name from sqlite_master where type = 'table'"
            ).fetchall()
        }

    assert {
        "events",
        "datasets",
        "dataset_events",
        "jobs",
        "artifacts",
        "artifact_lineage",
        "contexts",
    }.issubset(tables)


def test_store_initializes_artifact_directories(tmp_path):
    artifact_root = tmp_path / "artifacts"
    store = EvolutionStore(db_path=tmp_path / "evolution.db", artifact_root=artifact_root)
    store.initialize()

    for relative in (
        "events",
        "datasets",
        "artifacts/text_memory",
        "artifacts/skills",
        "artifacts/parametric_memory",
        "artifacts/datasets",
        "artifacts/reports",
        "artifacts/contexts",
        "contexts",
    ):
        assert (artifact_root / relative).is_dir()


def test_events_schema_has_source_event_uniqueness(tmp_path):
    db_path = tmp_path / "evolution.db"
    store = EvolutionStore(db_path=db_path, artifact_root=tmp_path / "artifacts")
    store.initialize()

    event_values = {
        "event_id": "event-1",
        "source": "harness",
        "event_type": "rollout",
        "source_event_id": "source-event-1",
        "created_at": "2026-06-14T00:00:00Z",
        "ingested_at": "2026-06-14T00:00:01Z",
        "payload_path": "events/event-1.json",
    }

    with sqlite3.connect(db_path) as conn:
        columns = {
            row[1]: row[2]
            for row in conn.execute("pragma table_info(events)").fetchall()
        }
        assert columns["event_id"] == "TEXT"
        assert columns["source"] == "TEXT"
        assert columns["event_type"] == "TEXT"
        assert columns["source_event_id"] == "TEXT"
        assert columns["payload_path"] == "TEXT"

        conn.execute(
            """
            INSERT INTO events (
                event_id,
                source,
                event_type,
                source_event_id,
                created_at,
                ingested_at,
                payload_path
            )
            VALUES (
                :event_id,
                :source,
                :event_type,
                :source_event_id,
                :created_at,
                :ingested_at,
                :payload_path
            )
            """,
            event_values,
        )

        duplicate_values = event_values | {"event_id": "event-2"}
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                """
                INSERT INTO events (
                    event_id,
                    source,
                    event_type,
                    source_event_id,
                    created_at,
                    ingested_at,
                    payload_path
                )
                VALUES (
                    :event_id,
                    :source,
                    :event_type,
                    :source_event_id,
                    :created_at,
                    :ingested_at,
                    :payload_path
                )
                """,
                duplicate_values,
            )


def test_initialize_preserves_non_openevo_session_event_identity(tmp_path):
    store = EvolutionStore(db_path=tmp_path / "evolution.db", artifact_root=tmp_path / "artifacts")
    store.initialize()
    non_openevo_event_type = "pol" + "ar.session_completed"
    event = store.ingest_event(
        EventIngestRequest(
            source="openevo",
            event_type=non_openevo_event_type,
            source_event_id="session:non-openevo",
            task_id="task_1",
            session_id="non-openevo",
            status="COMPLETED",
            payload={"session_result": {"trajectory": {"traces": [{"reward": 1.0}]}}},
        )
    )

    store.initialize()

    with store.connect() as conn:
        row = conn.execute(
            "SELECT source, event_type FROM events WHERE event_id = ?",
            (event.event_id,),
        ).fetchone()

    assert dict(row) == {
        "source": "openevo",
        "event_type": non_openevo_event_type,
    }


def test_initialize_keeps_distinct_non_openevo_and_openevo_session_events(tmp_path):
    store = EvolutionStore(db_path=tmp_path / "evolution.db", artifact_root=tmp_path / "artifacts")
    store.initialize()
    non_openevo_event_type = "pol" + "ar.session_completed"
    non_openevo = store.ingest_event(
        EventIngestRequest(
            source="openevo",
            event_type=non_openevo_event_type,
            source_event_id="session:shared",
            task_id="task_1",
            session_id="shared",
            status="COMPLETED",
            payload={"session_result": {"trajectory": {"traces": [{"reward": 1.0}]}}},
        )
    )
    canonical = store.ingest_event(
        EventIngestRequest(
            source="openevo",
            event_type="openevo.session_completed",
            source_event_id="session:shared",
            task_id="task_1",
            session_id="shared",
            status="COMPLETED",
            payload={"session_result": {"trajectory": {"traces": [{"reward": 1.0}]}}},
        )
    )
    with store.connect() as conn:
        conn.execute(
            "INSERT INTO dataset_events (dataset_id, event_id) VALUES (?, ?)",
            ("ds_existing", non_openevo.event_id),
        )
        conn.commit()

    store.initialize()

    with store.connect() as conn:
        rows = conn.execute(
            """
            SELECT event_id, source, event_type
            FROM events
            WHERE source_event_id = ?
            ORDER BY event_type
            """,
            ("session:shared",),
        ).fetchall()
        dataset_links = conn.execute(
            "SELECT event_id FROM dataset_events WHERE dataset_id = ?",
            ("ds_existing",),
        ).fetchall()

    assert [dict(row) for row in rows] == [
        {
            "event_id": canonical.event_id,
            "source": "openevo",
            "event_type": "openevo.session_completed",
        },
        {
            "event_id": non_openevo.event_id,
            "source": "openevo",
            "event_type": non_openevo_event_type,
        }
    ]
    assert [row["event_id"] for row in dataset_links] == [non_openevo.event_id]


def test_ingest_event_preserves_non_openevo_session_event_identity(tmp_path):
    store = EvolutionStore(db_path=tmp_path / "evolution.db", artifact_root=tmp_path / "artifacts")
    store.initialize()
    non_openevo_event_type = "pol" + "ar.session_completed"

    response = store.ingest_event(
        EventIngestRequest(
            source="openevo",
            event_type=non_openevo_event_type,
            source_event_id="session:non-openevo-live",
            task_id="task_1",
            session_id="non-openevo-live",
            status="COMPLETED",
            payload={"session_result": {"trajectory": {"traces": [{"reward": 1.0}]}}},
        )
    )

    with store.connect() as conn:
        row = conn.execute(
            "SELECT source, event_type, payload_path FROM events WHERE event_id = ?",
            (response.event_id,),
        ).fetchone()

    assert row["source"] == "openevo"
    assert row["event_type"] == non_openevo_event_type
    payload = json.loads(Path(row["payload_path"]).read_text(encoding="utf-8"))
    assert payload["source"] == "openevo"
    assert payload["event_type"] == non_openevo_event_type


def test_store_connection_rows_support_column_names(tmp_path):
    store = EvolutionStore(db_path=tmp_path / "evolution.db", artifact_root=tmp_path / "artifacts")
    store.initialize()

    with store.connect() as conn:
        row = conn.execute("select 'event-1' as event_id").fetchone()

    assert row["event_id"] == "event-1"


def test_safe_path_rejects_traversal_outside_root(tmp_path):
    files = ArtifactFileStore(tmp_path / "artifacts")

    with pytest.raises(ValueError, match="path escapes artifact root"):
        files.safe_path("..", "outside.json")


def test_write_json_writes_sorted_indented_utf8(tmp_path):
    files = ArtifactFileStore(tmp_path / "artifacts")
    path = files.safe_path("events", "event-1.json")

    written_path = files.write_json(path, {"z": "snowman \u2603", "a": 1})

    assert written_path == path.resolve()
    assert path.read_text(encoding="utf-8") == json.dumps(
        {"a": 1, "z": "snowman \u2603"},
        indent=2,
        sort_keys=True,
    )


def test_write_json_rejects_paths_outside_root(tmp_path):
    files = ArtifactFileStore(tmp_path / "artifacts")

    with pytest.raises(ValueError, match="path escapes artifact root"):
        files.write_json(tmp_path / "outside.json", {"a": 1})


def test_artifact_manifest_path_maps_known_artifact_aliases(tmp_path):
    files = ArtifactFileStore(tmp_path / "artifacts")

    assert files.artifact_manifest_path("skill_bundle", "skill-1") == (
        files.root / "artifacts" / "skills" / "skill-1" / "manifest.json"
    )
    assert files.artifact_manifest_path("report", "report-1") == (
        files.root / "artifacts" / "reports" / "report-1" / "manifest.json"
    )
    assert files.artifact_manifest_path("dataset", "dataset-artifact-1") == (
        files.root / "artifacts" / "datasets" / "dataset-artifact-1" / "manifest.json"
    )
    assert files.dataset_manifest_path("dataset-1") == (
        files.root / "datasets" / "dataset-1" / "manifest.json"
    )


@pytest.mark.parametrize("artifact_type", ["", "unknown"])
def test_artifact_manifest_path_rejects_empty_or_unknown_type(tmp_path, artifact_type):
    files = ArtifactFileStore(tmp_path / "artifacts")

    with pytest.raises(ValueError, match="unknown artifact type"):
        files.artifact_manifest_path(artifact_type, "artifact-1")


def test_ingest_event_is_idempotent(tmp_path):
    db_path = tmp_path / "evolution.db"
    store = EvolutionStore(db_path=db_path, artifact_root=tmp_path / "artifacts")
    store.initialize()
    created_at = datetime(2026, 1, 2, 3, 4, 5, tzinfo=timezone.utc)
    request = EventIngestRequest(
        source="openevo",
        event_type="openevo.session_completed",
        source_event_id="session:abc",
        created_at=created_at,
        task_id="task_1",
        session_id="abc",
        agent={"harness": "codex", "model_name": "gpt-5.4"},
        base_model="Qwen/Qwen3.6-27B",
        reward=1.0,
        status="COMPLETED",
        payload={"session_result": {"session_id": "abc"}},
    )

    first = store.ingest_event(request)
    second = store.ingest_event(request)

    assert first.event_id == second.event_id
    assert first.ingested is True
    assert first.duplicate is False
    assert second.ingested is False
    assert second.duplicate is True

    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute("SELECT * FROM events").fetchall()

    assert len(rows) == 1
    row = rows[0]
    assert row["event_id"] == first.event_id
    assert row["created_at"] == "2026-01-02T03:04:05Z"
    assert row["agent_harness"] == "codex"
    assert row["agent_model"] == "gpt-5.4"

    payload_path = Path(row["payload_path"])
    assert payload_path.exists()
    assert json.loads(payload_path.read_text(encoding="utf-8")) == request.model_dump(mode="json")


def test_ingest_event_normalizes_non_string_agent_metadata(tmp_path):
    db_path = tmp_path / "evolution.db"
    store = EvolutionStore(db_path=db_path, artifact_root=tmp_path / "artifacts")
    store.initialize()
    request = EventIngestRequest(
        source="openevo",
        event_type="openevo.session_completed",
        source_event_id="session:abc",
        agent={
            "harness": {"name": "codex", "version": 2},
            "model_name": ["gpt-5.4", "fallback"],
        },
    )

    response = store.ingest_event(request)

    assert response.ingested is True
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT agent_harness, agent_model FROM events WHERE event_id = ?",
            (response.event_id,),
        ).fetchone()

    assert row["agent_harness"] == '{"name": "codex", "version": 2}'
    assert row["agent_model"] == '["gpt-5.4", "fallback"]'
