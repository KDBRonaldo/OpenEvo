from __future__ import annotations

from datetime import datetime, timezone
import json
import math
from pathlib import Path
import sqlite3

import pytest

import openevo.evolution.store as store_module
from openevo.evolution.models import (
    ArtifactRegisterRequest,
    ArtifactState,
    ArtifactType,
    ContextResolveRequest,
)
from openevo.evolution.store import EvolutionStore


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
    assert (
        manifest_path
        == (
            tmp_path
            / "artifacts"
            / "artifacts"
            / "text_memory"
            / artifact.artifact_id
            / "manifest.json"
        ).resolve()
    )
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


def test_update_artifact_promotion_controls_context_selection(tmp_path):
    store = EvolutionStore(db_path=tmp_path / "evolution.db", artifact_root=tmp_path / "artifacts")
    store.initialize()
    memory_path = tmp_path / "artifacts" / "payloads" / "memory.md"
    memory_path.parent.mkdir(parents=True)
    memory_path.write_text("remember this", encoding="utf-8")

    artifact = store.register_artifact(
        ArtifactRegisterRequest(
            type=ArtifactType.TEXT_MEMORY,
            name="pending memory",
            uri=memory_path.as_uri(),
            manifest={"content_path": "memory.md"},
            compatibility={"task_tags": ["calculator"]},
            promoted=False,
        )
    )

    missing_context = store.resolve_context(
        ContextResolveRequest(
            task_id="calculator",
            instruction="Solve.",
            metadata={"task_tags": ["calculator"]},
        )
    )
    assert missing_context.memory["artifact_ids"] == []

    promoted = store.update_artifact_promotion(artifact.artifact_id, promoted=True)

    assert promoted.promoted is True
    matching_context = store.resolve_context(
        ContextResolveRequest(
            task_id="calculator",
            instruction="Solve.",
            metadata={"task_tags": ["calculator"]},
        )
    )
    assert matching_context.memory["artifact_ids"] == [artifact.artifact_id]

    with store.connect() as conn:
        row = conn.execute(
            "SELECT promoted, manifest_path FROM artifacts WHERE artifact_id = ?",
            (artifact.artifact_id,),
        ).fetchone()
    assert row["promoted"] == 1
    manifest = json.loads(Path(row["manifest_path"]).read_text(encoding="utf-8"))
    assert manifest["promoted"] is True


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
    assert json.loads(row["lineage_json"]) == {"source": {"created_at": "2026-06-14T12:30:45Z"}}
    assert json.loads(row["compatibility_json"]) == {"window": {"after": "2026-06-14T12:30:45Z"}}

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
        tmp_path / "artifacts" / "artifacts" / "text_memory" / "art_forced" / "manifest.json"
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


def test_register_agent_system_normalizes_target_path(tmp_path):
    store = EvolutionStore(db_path=tmp_path / "evolution.db", artifact_root=tmp_path / "artifacts")
    store.initialize()
    agent_system_file = tmp_path / "repo.md"
    agent_system_file.write_text("Use repository conventions.", encoding="utf-8")

    artifact = store.register_artifact(
        ArtifactRegisterRequest(
            type=ArtifactType.AGENT_SYSTEM,
            name="openhands repo instructions",
            uri=agent_system_file.as_uri(),
            manifest={"target_path": "./.openhands/microagents/repo.md"},
            promoted=True,
        )
    )

    assert artifact.manifest["target_path"] == ".openhands/microagents/repo.md"
    manifest_path = (
        tmp_path
        / "artifacts"
        / "artifacts"
        / "agent_system"
        / artifact.artifact_id
        / "manifest.json"
    )
    stored = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert stored["manifest"]["target_path"] == ".openhands/microagents/repo.md"


@pytest.mark.parametrize(
    "target_path",
    ["", "../AGENTS.md", "nested/../AGENTS.md", "/tmp/AGENTS.md"],
)
def test_register_agent_system_rejects_unsafe_target_path(tmp_path, target_path):
    store = EvolutionStore(db_path=tmp_path / "evolution.db", artifact_root=tmp_path / "artifacts")
    store.initialize()
    agent_system_file = tmp_path / "AGENTS.md"
    agent_system_file.write_text("Use repository conventions.", encoding="utf-8")

    with pytest.raises(ValueError, match="target_path"):
        store.register_artifact(
            ArtifactRegisterRequest(
                type=ArtifactType.AGENT_SYSTEM,
                name="unsafe instructions",
                uri=agent_system_file.as_uri(),
                manifest={"target_path": target_path},
                promoted=True,
            )
        )

    with store.connect() as conn:
        artifact_count = conn.execute("SELECT COUNT(*) FROM artifacts").fetchone()[0]

    assert artifact_count == 0


def test_context_resolver_selects_memory_skill_and_adapter(tmp_path):
    store = EvolutionStore(db_path=tmp_path / "evolution.db", artifact_root=tmp_path / "artifacts")
    store.initialize()
    payload_root = tmp_path / "artifacts" / "payloads"
    skill_dir = payload_root / "skills" / "parser"
    incompatible_skill_dir = payload_root / "skills" / "sorting"
    adapter_dir = payload_root / "adapters" / "parser"
    skill_dir.mkdir(parents=True)
    incompatible_skill_dir.mkdir(parents=True)
    adapter_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text("Use parser tools.", encoding="utf-8")
    (incompatible_skill_dir / "SKILL.md").write_text("Use sorting tools.", encoding="utf-8")
    (adapter_dir / "adapter_config.json").write_text("{}", encoding="utf-8")
    memory_file = payload_root / "memory.md"
    memory_file.write_text("Use recursive descent for parser tasks.", encoding="utf-8")
    memory = store.register_artifact(
        ArtifactRegisterRequest(
            type=ArtifactType.TEXT_MEMORY,
            name="parser memory",
            uri=memory_file.as_uri(),
            compatibility={"task_tags": ["calculator"], "agent_harness": ["codex"]},
            scores={"quality": 0.9},
            tags=["calculator"],
            promoted=True,
        )
    )
    skill = store.register_artifact(
        ArtifactRegisterRequest(
            type=ArtifactType.SKILL_BUNDLE,
            name="parser skill",
            uri=skill_dir.as_uri(),
            compatibility={"task_tags": ["calculator"]},
            scores={"quality": 0.8},
            tags=["calculator"],
            promoted=True,
        )
    )
    agent_system_file = payload_root / "AGENTS.md"
    agent_system_file.write_text(
        "Always inspect the repository conventions before changing code.",
        encoding="utf-8",
    )
    agent_system = store.register_artifact(
        ArtifactRegisterRequest(
            type=ArtifactType.AGENT_SYSTEM,
            name="codex agent instructions",
            uri=agent_system_file.as_uri(),
            manifest={"target_path": "AGENTS.md"},
            compatibility={"task_tags": ["calculator"], "agent_harness": ["codex"]},
            scores={"quality": 0.85},
            tags=["calculator"],
            promoted=True,
        )
    )
    adapter = store.register_artifact(
        ArtifactRegisterRequest(
            type=ArtifactType.PARAMETRIC_MEMORY,
            name="Qwen parser LoRA adapter",
            uri=adapter_dir.as_uri(),
            manifest={
                "adapter_id": "parser-memory",
                "adapter_format": "lora",
                "base_model": "Qwen/Qwen3.6-27B",
            },
            compatibility={"base_model": "Qwen/Qwen3.6-27B", "task_tags": ["calculator"]},
            scores={"heldout_reward_delta": 0.1},
            tags=["calculator"],
            promoted=True,
        )
    )
    inactive_memory = store.register_artifact(
        ArtifactRegisterRequest(
            type=ArtifactType.TEXT_MEMORY,
            name="draft parser memory",
            uri=memory_file.as_uri(),
            compatibility={"task_tags": ["calculator"]},
            scores={"quality": 1.0},
            tags=["calculator"],
            promoted=False,
        )
    )
    incompatible_skill = store.register_artifact(
        ArtifactRegisterRequest(
            type=ArtifactType.SKILL_BUNDLE,
            name="sorting skill",
            uri=incompatible_skill_dir.as_uri(),
            compatibility={"task_tags": ["sorting"]},
            scores={"quality": 1.0},
            tags=["sorting"],
            promoted=True,
        )
    )

    context = store.resolve_context(
        ContextResolveRequest(
            task_id="task_1",
            instruction="fix calculator parser",
            agent={"harness": "codex"},
            base_model="Qwen/Qwen3.6-27B",
            metadata={"task_tags": ["calculator"]},
        )
    )

    assert context.context_id.startswith("ctx_")
    assert memory.artifact_id in context.memory["artifact_ids"]
    assert inactive_memory.artifact_id not in context.memory["artifact_ids"]
    assert "recursive descent" in context.memory["rendered_text"]
    assert context.skills[0]["artifact_id"] == skill.artifact_id
    assert incompatible_skill.artifact_id not in {item["artifact_id"] for item in context.skills}
    assert context.agent_system["artifact_ids"] == [agent_system.artifact_id]
    assert "repository conventions" in context.agent_system["rendered_text"]
    assert context.agent_system["target_path"] == "AGENTS.md"
    assert context.adapter_merge_spec.adapters[0]["artifact_id"] == adapter.artifact_id
    assert context.adapter_merge_spec.adapters[0]["adapter_id"] == "parser-memory"
    assert inactive_memory.artifact_id not in context.selection["artifact_ids"]
    assert incompatible_skill.artifact_id not in context.selection["artifact_ids"]

    with store.connect() as conn:
        row = conn.execute(
            "SELECT * FROM contexts WHERE context_id = ?",
            (context.context_id,),
        ).fetchone()

    assert row is not None
    assert json.loads(row["selected_artifact_ids_json"]) == context.selection["artifact_ids"]
    assert json.loads(row["response_json"])["context_id"] == context.context_id
    snapshot_path = tmp_path / "artifacts" / "contexts" / f"{context.context_id}.json"
    snapshot = json.loads(snapshot_path.read_text(encoding="utf-8"))
    assert snapshot["request"]["task_id"] == "task_1"
    assert snapshot["response"]["selection"]["artifact_ids"] == context.selection["artifact_ids"]


def test_context_resolver_honors_explicit_context_artifact_ids(tmp_path):
    store = EvolutionStore(db_path=tmp_path / "evolution.db", artifact_root=tmp_path / "artifacts")
    store.initialize()
    payload_root = tmp_path / "artifacts" / "payloads"
    payload_root.mkdir(parents=True)
    old_memory_file = payload_root / "old-memory.md"
    old_memory_file.write_text("Old round advice.", encoding="utf-8")
    old_memory = store.register_artifact(
        ArtifactRegisterRequest(
            type=ArtifactType.TEXT_MEMORY,
            name="old memory",
            uri=old_memory_file.as_uri(),
            compatibility={"task_tags": ["openevo_run_task:run-1:task-a"]},
            scores={"quality": 0.9},
            promoted=True,
        )
    )
    latest_memory_file = payload_root / "latest-memory.md"
    latest_memory_file.write_text("Latest round advice.", encoding="utf-8")
    latest_memory = store.register_artifact(
        ArtifactRegisterRequest(
            type=ArtifactType.TEXT_MEMORY,
            name="latest memory",
            uri=latest_memory_file.as_uri(),
            compatibility={"task_tags": ["openevo_run_task:run-1:task-a"]},
            scores={"quality": 0.8},
            promoted=True,
        )
    )
    adapter_dir = payload_root / "adapter"
    adapter_dir.mkdir()
    (adapter_dir / "adapter_config.json").write_text("{}", encoding="utf-8")
    adapter = store.register_artifact(
        ArtifactRegisterRequest(
            type=ArtifactType.PARAMETRIC_MEMORY,
            name="parser adapter",
            uri=adapter_dir.as_uri(),
            manifest={"adapter_id": "parser-memory", "adapter_format": "lora"},
            compatibility={
                "base_model": ["gpt-5.1-codex-mini"],
                "task_tags": ["openevo_run_task:run-1:task-a"],
            },
            scores={"quality": 0.7},
            promoted=True,
        )
    )

    context = store.resolve_context(
        ContextResolveRequest(
            task_id="task-a",
            instruction="continue task",
            base_model="gpt-5.1-codex-mini",
            metadata={
                "task_tags": ["openevo_run_task:run-1:task-a"],
                "evolution": {"context_artifact_ids": [latest_memory.artifact_id]},
            },
        )
    )

    assert context.memory["artifact_ids"] == [latest_memory.artifact_id]
    assert "Latest round advice" in context.memory["rendered_text"]
    assert old_memory.artifact_id not in context.selection["artifact_ids"]
    assert context.adapter_merge_spec.adapters == []
    assert adapter.artifact_id not in context.selection["artifact_ids"]

    generic_context = store.resolve_context(
        ContextResolveRequest(
            task_id="task-a",
            instruction="continue task",
            base_model="gpt-5.1-codex-mini",
            metadata={"task_tags": ["openevo_run_task:run-1:task-a"]},
        )
    )
    assert generic_context.memory["artifact_ids"] == [
        old_memory.artifact_id,
        latest_memory.artifact_id,
    ]

    ordered_context = store.resolve_context(
        ContextResolveRequest(
            task_id="task-a",
            instruction="continue task",
            base_model="gpt-5.1-codex-mini",
            metadata={
                "task_tags": ["openevo_run_task:run-1:task-a"],
                "evolution": {
                    "context_artifact_ids": [
                        latest_memory.artifact_id,
                        old_memory.artifact_id,
                    ]
                },
            },
        )
    )
    assert ordered_context.selection["artifact_ids"] == [
        latest_memory.artifact_id,
        old_memory.artifact_id,
    ]
    assert ordered_context.memory["artifact_ids"] == [
        latest_memory.artifact_id,
        old_memory.artifact_id,
    ]
    assert ordered_context.memory["rendered_text"] == ("Latest round advice.\n\nOld round advice.")
    assert store.get_context_runtime_authority(ordered_context.context_id) == ordered_context
    with store.connect() as conn:
        conn.execute(
            "UPDATE contexts SET selected_artifact_ids_json = ? WHERE context_id = ?",
            (json.dumps([old_memory.artifact_id]), ordered_context.context_id),
        )
        conn.commit()
    with pytest.raises(ValueError, match="inconsistent"):
        store.get_context_runtime_authority(ordered_context.context_id)

    for invalid_ids, message in (
        ([latest_memory.artifact_id, latest_memory.artifact_id], "duplicates"),
        ([latest_memory.artifact_id, "artifact-not-in-revision"], "unavailable"),
    ):
        with pytest.raises(ValueError, match=message):
            store.resolve_context(
                ContextResolveRequest(
                    task_id="task-a",
                    instruction="continue task",
                    base_model="gpt-5.1-codex-mini",
                    metadata={
                        "task_tags": ["openevo_run_task:run-1:task-a"],
                        "evolution": {"context_artifact_ids": invalid_ids},
                    },
                )
            )

    adapter_context = store.resolve_context(
        ContextResolveRequest(
            task_id="task-a",
            instruction="continue task",
            base_model="gpt-5.1-codex-mini",
            metadata={
                "task_tags": ["openevo_run_task:run-1:task-a"],
                "evolution": {
                    "context_artifact_ids": [
                        latest_memory.artifact_id,
                        adapter.artifact_id,
                    ]
                },
            },
        )
    )

    assert adapter_context.memory["artifact_ids"] == [latest_memory.artifact_id]
    assert adapter_context.adapter_merge_spec.adapters[0]["artifact_id"] == adapter.artifact_id


def test_context_resolver_skips_legacy_agent_system_with_unsafe_target_path(tmp_path):
    store = EvolutionStore(db_path=tmp_path / "evolution.db", artifact_root=tmp_path / "artifacts")
    store.initialize()
    agent_system_file = tmp_path / "AGENTS.md"
    agent_system_file.write_text("Use repository conventions.", encoding="utf-8")
    agent_system = store.register_artifact(
        ArtifactRegisterRequest(
            type=ArtifactType.AGENT_SYSTEM,
            name="legacy unsafe instructions",
            uri=agent_system_file.as_uri(),
            manifest={"target_path": "AGENTS.md"},
            compatibility={"agent_harness": ["codex"]},
            scores={"quality": 0.9},
            promoted=True,
        )
    )
    with store.connect() as conn:
        row = conn.execute(
            "SELECT manifest_path FROM artifacts WHERE artifact_id = ?",
            (agent_system.artifact_id,),
        ).fetchone()
    manifest_path = Path(row["manifest_path"])
    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    payload["manifest"]["target_path"] = "pyproject.toml"
    manifest_path.write_text(json.dumps(payload), encoding="utf-8")

    context = store.resolve_context(
        ContextResolveRequest(
            task_id="task_unsafe_agent_system",
            instruction="fix parser",
            agent={"harness": "codex"},
        )
    )

    assert context.agent_system["artifact_ids"] == []
    assert agent_system.artifact_id not in context.selection["artifact_ids"]


def test_context_resolver_skips_unreadable_text_memory(tmp_path):
    store = EvolutionStore(db_path=tmp_path / "evolution.db", artifact_root=tmp_path / "artifacts")
    store.initialize()
    missing_memory = store.register_artifact(
        ArtifactRegisterRequest(
            type=ArtifactType.TEXT_MEMORY,
            name="missing memory",
            uri=(tmp_path / "missing.md").as_uri(),
            compatibility={"task_tags": ["calculator"]},
            scores={"quality": 1.0},
            tags=["calculator"],
            promoted=True,
        )
    )

    context = store.resolve_context(
        ContextResolveRequest(
            task_id="task_missing_memory",
            instruction="fix calculator parser",
            metadata={"task_tags": ["calculator"]},
        )
    )

    assert context.memory["artifact_ids"] == []
    assert context.memory["rendered_text"] == ""
    assert missing_memory.artifact_id not in context.selection["artifact_ids"]


def test_context_resolver_requires_declared_base_model_and_harness(tmp_path):
    store = EvolutionStore(db_path=tmp_path / "evolution.db", artifact_root=tmp_path / "artifacts")
    store.initialize()
    payload_root = tmp_path / "artifacts" / "payloads"
    adapter_dir = payload_root / "adapters" / "qwen-parser"
    adapter_dir.mkdir(parents=True)
    (adapter_dir / "adapter_config.json").write_text("{}", encoding="utf-8")
    memory_file = payload_root / "harness-memory.md"
    memory_file.write_text("Use codex-specific parser heuristics.", encoding="utf-8")
    harness_memory = store.register_artifact(
        ArtifactRegisterRequest(
            type=ArtifactType.TEXT_MEMORY,
            name="codex memory",
            uri=memory_file.as_uri(),
            compatibility={"task_tags": ["calculator"], "agent_harness": "codex"},
            scores={"quality": 0.7},
            tags=["calculator"],
            promoted=True,
        )
    )
    adapter = store.register_artifact(
        ArtifactRegisterRequest(
            type=ArtifactType.PARAMETRIC_MEMORY,
            name="qwen parser lora",
            uri=adapter_dir.as_uri(),
            manifest={"adapter_format": "lora", "base_model": "Qwen/Qwen3.6-27B"},
            compatibility={"base_model": "Qwen/Qwen3.6-27B", "task_tags": ["calculator"]},
            scores={"heldout_reward_delta": 0.2},
            tags=["calculator"],
            promoted=True,
        )
    )

    missing_context = store.resolve_context(
        ContextResolveRequest(
            task_id="task_missing_constraints",
            instruction="fix calculator parser",
            metadata={"task_tags": ["calculator"]},
        )
    )
    mismatched_context = store.resolve_context(
        ContextResolveRequest(
            task_id="task_mismatched_constraints",
            instruction="fix calculator parser",
            agent={"harness": "other"},
            base_model="other/model",
            metadata={"task_tags": ["calculator"]},
        )
    )
    matching_context = store.resolve_context(
        ContextResolveRequest(
            task_id="task_matching_constraints",
            instruction="fix calculator parser",
            agent={"harness": "codex"},
            base_model="Qwen/Qwen3.6-27B",
            metadata={"task_tags": ["calculator"]},
        )
    )

    assert harness_memory.artifact_id not in missing_context.memory["artifact_ids"]
    assert adapter.artifact_id not in missing_context.selection["artifact_ids"]
    assert missing_context.adapter_merge_spec.adapters == []
    assert missing_context.adapter_merge_spec.merge_mode == "reference_only"

    assert harness_memory.artifact_id not in mismatched_context.memory["artifact_ids"]
    assert adapter.artifact_id not in mismatched_context.selection["artifact_ids"]
    assert mismatched_context.adapter_merge_spec.adapters == []

    assert harness_memory.artifact_id in matching_context.memory["artifact_ids"]
    assert adapter.artifact_id in matching_context.selection["artifact_ids"]
    assert matching_context.adapter_merge_spec.adapters[0]["artifact_id"] == adapter.artifact_id

    subscription_context = store.resolve_context(
        ContextResolveRequest(
            task_id="task_subscription_constraints",
            instruction="fix calculator parser",
            agent={"settings": {"auth_mode": "subscription"}},
            base_model="Qwen/Qwen3.6-27B",
            metadata={"task_tags": ["calculator"]},
        )
    )

    assert adapter.artifact_id not in subscription_context.selection["artifact_ids"]
    assert subscription_context.adapter_merge_spec.adapters == []
    assert subscription_context.adapter_merge_spec.merge_mode == "reference_only"

    unknown_subscription_alias_context = store.resolve_context(
        ContextResolveRequest(
            task_id="task_legacy_unknown_subscription_alias",
            instruction="fix calculator parser",
            agent={"settings": {"auth_mode": "claude_subscription"}},
            base_model="Qwen/Qwen3.6-27B",
            metadata={"task_tags": ["calculator"]},
        )
    )

    assert adapter.artifact_id in unknown_subscription_alias_context.selection["artifact_ids"]
    assert (
        unknown_subscription_alias_context.adapter_merge_spec.adapters[0]["artifact_id"]
        == adapter.artifact_id
    )


def test_context_resolver_counts_memory_separators_against_limit(tmp_path):
    store = EvolutionStore(db_path=tmp_path / "evolution.db", artifact_root=tmp_path / "artifacts")
    store.initialize()
    payload_root = tmp_path / "artifacts" / "payloads"
    payload_root.mkdir(parents=True)
    first_file = payload_root / "first-memory.md"
    second_file = payload_root / "second-memory.md"
    first_file.write_text("AAAA", encoding="utf-8")
    second_file.write_text("BBBB", encoding="utf-8")
    first_memory = store.register_artifact(
        ArtifactRegisterRequest(
            type=ArtifactType.TEXT_MEMORY,
            name="first memory",
            uri=first_file.as_uri(),
            compatibility={"task_tags": ["calculator"]},
            scores={"quality": 0.9},
            tags=["calculator"],
            promoted=True,
        )
    )
    second_memory = store.register_artifact(
        ArtifactRegisterRequest(
            type=ArtifactType.TEXT_MEMORY,
            name="second memory",
            uri=second_file.as_uri(),
            compatibility={"task_tags": ["calculator"]},
            scores={"quality": 0.8},
            tags=["calculator"],
            promoted=True,
        )
    )

    context = store.resolve_context(
        ContextResolveRequest(
            task_id="task_memory_budget",
            instruction="fix calculator parser",
            metadata={"task_tags": ["calculator"]},
            limits={"max_memory_chars": 7},
        )
    )

    assert context.memory["rendered_text"] == "AAAA\n\nB"
    assert len(context.memory["rendered_text"]) == 7
    assert context.memory["artifact_ids"] == [first_memory.artifact_id, second_memory.artifact_id]


def test_context_resolver_skips_malformed_stored_compatibility_json(tmp_path):
    store = EvolutionStore(db_path=tmp_path / "evolution.db", artifact_root=tmp_path / "artifacts")
    store.initialize()
    memory_file = tmp_path / "malformed-compatibility-memory.md"
    memory_file.write_text("This should not be selected.", encoding="utf-8")
    memory = store.register_artifact(
        ArtifactRegisterRequest(
            type=ArtifactType.TEXT_MEMORY,
            name="malformed compatibility memory",
            uri=memory_file.as_uri(),
            compatibility={"task_tags": ["calculator"]},
            scores={"quality": 0.9},
            tags=["calculator"],
            promoted=True,
        )
    )
    with store.connect() as conn:
        conn.execute(
            "UPDATE artifacts SET compatibility_json = ? WHERE artifact_id = ?",
            ("{not valid json", memory.artifact_id),
        )
        conn.commit()

    context = store.resolve_context(
        ContextResolveRequest(
            task_id="task_malformed_compatibility",
            instruction="fix calculator parser",
            metadata={"task_tags": ["calculator"]},
        )
    )

    assert context.memory["artifact_ids"] == []
    assert context.memory["rendered_text"] == ""
    assert memory.artifact_id not in context.selection["artifact_ids"]
