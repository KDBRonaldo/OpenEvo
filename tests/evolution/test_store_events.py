from __future__ import annotations

import json
import sqlite3

import pytest

from polar_evolution.files import ArtifactFileStore
from polar_evolution.store import EvolutionStore


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
