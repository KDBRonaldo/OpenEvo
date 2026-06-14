from __future__ import annotations

from datetime import datetime, timezone
import json
import math
from pathlib import Path
import sqlite3

import pytest

import polar_evolution.store as store_module
from polar_evolution.models import ArtifactRegisterRequest, ArtifactState, ArtifactType
from polar_evolution.store import EvolutionStore


def test_register_artifact_persists_manifest(tmp_path):
    store = EvolutionStore(db_path=tmp_path / "evolution.db", artifact_root=tmp_path / "artifacts")
    store.initialize()

    artifact = store.register_artifact(
        ArtifactRegisterRequest(
            type=ArtifactType.TEXT_MEMORY,
            name="calculator memory",
            uri="file:///tmp/memory.md",
            manifest={"content_path": "memory.md"},
            compatibility={"task_tags": ["calculator"]},
            scores={"quality": 0.9},
            tags=["calculator"],
            promoted=True,
        )
    )

    assert artifact.artifact_id.startswith("art_")
    assert artifact.type == ArtifactType.TEXT_MEMORY
    assert artifact.version == 1
    assert artifact.state == ArtifactState.ACTIVE
    assert artifact.promoted is True
    assert artifact.compatibility["task_tags"] == ["calculator"]

    with store.connect() as conn:
        row = conn.execute(
            "SELECT * FROM artifacts WHERE artifact_id = ?",
            (artifact.artifact_id,),
        ).fetchone()

    assert row is not None
    assert row["type"] == "text_memory"
    assert row["name"] == "calculator memory"
    assert row["version"] == 1
    assert row["state"] == "active"
    assert row["uri"] == "file:///tmp/memory.md"
    assert json.loads(row["lineage_json"]) == {}
    assert json.loads(row["compatibility_json"]) == {"task_tags": ["calculator"]}
    assert json.loads(row["scores_json"]) == {"quality": 0.9}
    assert json.loads(row["tags_json"]) == ["calculator"]
    assert row["promoted"] == 1

    manifest_path = Path(row["manifest_path"])
    assert manifest_path == (
        tmp_path
        / "artifacts"
        / "artifacts"
        / "text_memory"
        / artifact.artifact_id
        / "manifest.json"
    ).resolve()
    assert json.loads(manifest_path.read_text(encoding="utf-8")) == {
        "artifact_id": artifact.artifact_id,
        "type": "text_memory",
        "name": "calculator memory",
        "uri": "file:///tmp/memory.md",
        "manifest": {"content_path": "memory.md"},
        "lineage": {},
        "compatibility": {"task_tags": ["calculator"]},
        "scores": {"quality": 0.9},
        "tags": ["calculator"],
        "promoted": True,
    }


def test_register_artifact_normalizes_nested_json_metadata(tmp_path):
    store = EvolutionStore(db_path=tmp_path / "evolution.db", artifact_root=tmp_path / "artifacts")
    store.initialize()
    observed_at = datetime(2026, 6, 14, 12, 30, 45, tzinfo=timezone.utc)

    artifact = store.register_artifact(
        ArtifactRegisterRequest(
            type=ArtifactType.TEXT_MEMORY,
            name="dated memory",
            uri="file:///tmp/dated-memory.md",
            manifest={"records": [{"observed_at": observed_at}]},
            lineage={"source": {"created_at": observed_at}},
            compatibility={"window": {"after": observed_at}},
        )
    )

    with store.connect() as conn:
        row = conn.execute(
            "SELECT * FROM artifacts WHERE artifact_id = ?",
            (artifact.artifact_id,),
        ).fetchone()

    assert row is not None
    assert json.loads(row["lineage_json"]) == {
        "source": {"created_at": "2026-06-14T12:30:45Z"}
    }
    assert json.loads(row["compatibility_json"]) == {
        "window": {"after": "2026-06-14T12:30:45Z"}
    }

    manifest_path = Path(row["manifest_path"])
    assert json.loads(manifest_path.read_text(encoding="utf-8"))["manifest"] == {
        "records": [{"observed_at": "2026-06-14T12:30:45Z"}]
    }


def test_register_artifact_rejects_non_finite_scores_without_writes(tmp_path):
    store = EvolutionStore(db_path=tmp_path / "evolution.db", artifact_root=tmp_path / "artifacts")
    store.initialize()

    with pytest.raises(ValueError, match="non-finite float"):
        store.register_artifact(
            ArtifactRegisterRequest(
                type=ArtifactType.TEXT_MEMORY,
                name="invalid score",
                uri="file:///tmp/invalid.md",
                scores={"quality": math.nan},
            )
        )

    with store.connect() as conn:
        artifact_count = conn.execute("SELECT COUNT(*) FROM artifacts").fetchone()[0]

    assert artifact_count == 0
    assert not list((tmp_path / "artifacts" / "artifacts" / "text_memory").glob("*/manifest.json"))


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("manifest", {"weights": [1.0, math.nan]}),
        ("lineage", {"parent": {"score": math.inf}}),
        ("compatibility", {"bounds": {"max": -math.inf}}),
    ],
)
def test_register_artifact_rejects_non_finite_metadata_without_writes(
    tmp_path,
    field,
    value,
):
    store = EvolutionStore(db_path=tmp_path / "evolution.db", artifact_root=tmp_path / "artifacts")
    store.initialize()
    request_data = {
        "type": ArtifactType.TEXT_MEMORY,
        "name": "invalid metadata",
        "uri": "file:///tmp/invalid-metadata.md",
        field: value,
    }

    with pytest.raises(ValueError, match=f"non-finite float at {field}"):
        store.register_artifact(ArtifactRegisterRequest(**request_data))

    with store.connect() as conn:
        artifact_count = conn.execute("SELECT COUNT(*) FROM artifacts").fetchone()[0]

    assert artifact_count == 0
    assert not list((tmp_path / "artifacts" / "artifacts" / "text_memory").glob("*/manifest.json"))


def test_register_artifact_cleans_up_manifest_on_db_failure(tmp_path, monkeypatch):
    store = EvolutionStore(db_path=tmp_path / "evolution.db", artifact_root=tmp_path / "artifacts")
    store.initialize()
    monkeypatch.setattr(store_module, "new_id", lambda prefix: f"{prefix}_forced")
    manifest_path = (
        tmp_path
        / "artifacts"
        / "artifacts"
        / "text_memory"
        / "art_forced"
        / "manifest.json"
    ).resolve()

    with store.connect() as conn:
        conn.execute(
            """
            CREATE TRIGGER artifacts_insert_failure
            BEFORE INSERT ON artifacts
            BEGIN
                SELECT RAISE(ABORT, 'forced artifact insert failure');
            END;
            """
        )
        conn.commit()

    with pytest.raises(sqlite3.DatabaseError, match="forced artifact insert failure"):
        store.register_artifact(
            ArtifactRegisterRequest(
                type=ArtifactType.TEXT_MEMORY,
                name="db failure",
                uri="file:///tmp/db-failure.md",
                manifest={"content_path": "db-failure.md"},
            )
        )

    with store.connect() as conn:
        artifact_count = conn.execute("SELECT COUNT(*) FROM artifacts").fetchone()[0]

    assert artifact_count == 0
    assert not manifest_path.exists()


def test_register_artifact_retries_collision_without_touching_existing_manifest(
    tmp_path,
    monkeypatch,
):
    store = EvolutionStore(db_path=tmp_path / "evolution.db", artifact_root=tmp_path / "artifacts")
    store.initialize()
    artifact_ids = iter(["art_collision", "art_collision", "art_retry"])
    monkeypatch.setattr(store_module, "new_id", lambda prefix: next(artifact_ids))

    first_artifact = store.register_artifact(
        ArtifactRegisterRequest(
            type=ArtifactType.TEXT_MEMORY,
            name="first memory",
            uri="file:///tmp/first.md",
            manifest={"content_path": "first.md"},
        )
    )
    first_manifest_path = (
        tmp_path
        / "artifacts"
        / "artifacts"
        / "text_memory"
        / first_artifact.artifact_id
        / "manifest.json"
    ).resolve()
    first_manifest_before = first_manifest_path.read_text(encoding="utf-8")

    second_artifact = store.register_artifact(
        ArtifactRegisterRequest(
            type=ArtifactType.TEXT_MEMORY,
            name="second memory",
            uri="file:///tmp/second.md",
            manifest={"content_path": "second.md"},
        )
    )

    with store.connect() as conn:
        rows = conn.execute(
            "SELECT artifact_id, name, manifest_path FROM artifacts ORDER BY artifact_id"
        ).fetchall()

    assert first_artifact.artifact_id == "art_collision"
    assert second_artifact.artifact_id == "art_retry"
    assert [row["artifact_id"] for row in rows] == ["art_collision", "art_retry"]
    assert first_manifest_path.read_text(encoding="utf-8") == first_manifest_before
    assert json.loads(first_manifest_before)["manifest"] == {"content_path": "first.md"}
    second_manifest_path = (
        tmp_path
        / "artifacts"
        / "artifacts"
        / "text_memory"
        / second_artifact.artifact_id
        / "manifest.json"
    ).resolve()
    assert second_manifest_path.exists()
    assert json.loads(second_manifest_path.read_text(encoding="utf-8"))["manifest"] == {
        "content_path": "second.md"
    }
