from __future__ import annotations

import os
from pathlib import Path
import sqlite3

import pytest

from openevo.evolution import store as store_module
from openevo.evolution.context_projection import ContextProjectionResolveRequest
from openevo.evolution.context_snapshot_recovery import write_context_snapshot
from openevo.evolution.files import ArtifactFileStore
from openevo.evolution.framework import (
    EvolutionExecutionProfile,
    RuntimeDestinationRoots,
)
from openevo.evolution.models import ArtifactRegisterRequest, ArtifactType
from openevo.evolution.store import EvolutionStore
from tests.framework_testkit import verified_builtin_registry


def _request() -> ContextProjectionResolveRequest:
    return ContextProjectionResolveRequest(
        task_id="task-store-recovery",
        instruction="Continue the task.",
        agent={"harness": "codex"},
        metadata={"task_tags": ["parser"]},
        execution_profile=EvolutionExecutionProfile(
            execution_mode="self_deployed",
            capture_mode="transcript",
            harness_id="codex",
        ),
        destination_roots=RuntimeDestinationRoots(
            target_data="/openevo/session/evolution",
            harness_skills="/openevo/session/evolution/skills",
            harness_instruction="/workspace/repository",
        ),
    )


def _materialized_store(tmp_path: Path) -> tuple[EvolutionStore, object]:
    artifact_root = tmp_path / "managed"
    registry = verified_builtin_registry(tmp_path / "registry")
    store = EvolutionStore(
        db_path=tmp_path / "evolution.db",
        artifact_root=artifact_root,
        executable_registry=registry,
    )
    store.initialize()
    payload = artifact_root / "payloads" / "memory.md"
    payload.parent.mkdir()
    payload.write_text("Use the verified parser memory.", encoding="utf-8")
    store.register_artifact(
        ArtifactRegisterRequest(
            type=ArtifactType.TEXT_MEMORY,
            name="parser memory",
            uri=payload.as_uri(),
            manifest={"content_path": "memory.md"},
            compatibility={"task_tags": ["parser"]},
            scores={"quality": 0.8},
            promoted=True,
        )
    )
    return store, registry


def test_forged_store_identity_table_cannot_claim_managed_recovery_state(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "forged.db"
    artifact_root = tmp_path / "managed"
    files = ArtifactFileStore(artifact_root)
    files.initialize()
    sentinel = artifact_root / "context_materializations" / "ctx-existing"
    sentinel.mkdir()
    (sentinel / "keep.txt").write_text("keep", encoding="utf-8")
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            "CREATE TABLE store_identity ("
            "singleton INTEGER, store_id TEXT, artifact_root TEXT, binding_state TEXT)"
        )
        connection.execute(
            "INSERT INTO store_identity VALUES (1, ?, ?, 'pending')",
            ("store_0000000000000000", str(artifact_root.resolve())),
        )

    with pytest.raises(ValueError, match="identity|schema|managed state"):
        EvolutionStore(db_path=db_path, artifact_root=artifact_root).initialize()

    assert (sentinel / "keep.txt").read_text(encoding="utf-8") == "keep"
    assert not (artifact_root / ".openevo-store.json").exists()
    assert not (artifact_root / "context_materializations" / ".openevo-store.json").exists()


def test_startup_rejects_corrupted_referenced_materialization(tmp_path: Path) -> None:
    store, registry = _materialized_store(tmp_path)
    context = store.resolve_materialized_context(_request())
    bundle = store.files.context_materialization_dir(context.context_id)
    (bundle / "manifest.json").write_text("{}", encoding="utf-8")

    with pytest.raises(ValueError, match="materialized|manifest|context"):
        EvolutionStore(
            db_path=store.db_path,
            artifact_root=store.files.root,
            executable_registry=registry,
        ).initialize()

    assert bundle.is_dir()


def test_recovery_identity_drift_rolls_back_before_orphan_cleanup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db_path = tmp_path / "evolution.db"
    artifact_root = tmp_path / "managed"
    EvolutionStore(db_path=db_path, artifact_root=artifact_root).initialize()
    orphan = artifact_root / "context_materializations" / "ctx-orphan"
    orphan.mkdir()
    sentinel = orphan / "keep.txt"
    sentinel.write_text("keep", encoding="utf-8")

    recovering = EvolutionStore(db_path=db_path, artifact_root=artifact_root)
    original_ensure_schema = recovering._ensure_schema

    def mutate_identity(connection: sqlite3.Connection) -> None:
        original_ensure_schema(connection)
        connection.execute(
            "UPDATE store_identity SET store_id = ? WHERE singleton = 1",
            ("store_0000000000000000",),
        )

    monkeypatch.setattr(recovering, "_ensure_schema", mutate_identity)
    with pytest.raises(ValueError, match="identity"):
        recovering.initialize()

    assert sentinel.read_text(encoding="utf-8") == "keep"
    with sqlite3.connect(db_path) as connection:
        row = connection.execute(
            "SELECT store_id, binding_state FROM store_identity WHERE singleton = 1"
        ).fetchone()
    assert row is not None
    assert row[0] != "store_0000000000000000"
    assert row[1] == "bound"


def test_store_startup_tombstones_snapshot_written_before_database_commit(
    tmp_path: Path,
) -> None:
    artifact_root = tmp_path / "managed"
    db_path = tmp_path / "evolution.db"
    EvolutionStore(db_path=db_path, artifact_root=artifact_root).initialize()
    artifact_root_fd = os.open(
        artifact_root,
        os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_DIRECTORY,
    )
    try:
        write_context_snapshot(
            artifact_root_fd,
            "ctx_crashed_before_commit",
            b'{"request":{},"response":{}}',
        )
    finally:
        os.close(artifact_root_fd)

    EvolutionStore(db_path=db_path, artifact_root=artifact_root).initialize()

    contexts = artifact_root / "contexts"
    assert not (contexts / "ctx_crashed_before_commit.json").exists()
    tombstones = list(contexts.glob(".openevo-context-tombstone-*"))
    assert len(tombstones) == 1
    assert tombstones[0].read_bytes() == b""


def test_base_schema_installation_rolls_back_an_interrupted_script(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    artifact_root = tmp_path / "managed"
    db_path = tmp_path / "evolution.db"
    original_schema = store_module.SCHEMA
    monkeypatch.setattr(
        store_module,
        "SCHEMA",
        "CREATE TABLE injected_partial (value TEXT);\nSELECT value FROM missing_injected_table;",
    )

    with pytest.raises(sqlite3.OperationalError, match="missing_injected_table"):
        EvolutionStore(db_path=db_path, artifact_root=artifact_root).initialize()

    with sqlite3.connect(db_path) as connection:
        assert (
            connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'injected_partial'"
            ).fetchone()
            is None
        )
    monkeypatch.setattr(store_module, "SCHEMA", original_schema)

    EvolutionStore(db_path=db_path, artifact_root=artifact_root).initialize()

    with sqlite3.connect(db_path) as connection:
        assert (
            connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'events'"
            ).fetchone()
            is not None
        )


def test_base_schema_and_additive_migrations_share_one_transaction(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    artifact_root = tmp_path / "managed"
    db_path = tmp_path / "evolution.db"
    store = EvolutionStore(db_path=db_path, artifact_root=artifact_root)

    def interrupt_after_base_schema(_connection: sqlite3.Connection) -> None:
        raise RuntimeError("injected interruption before additive migrations")

    monkeypatch.setattr(store, "_ensure_schema", interrupt_after_base_schema)
    with pytest.raises(RuntimeError, match="before additive migrations"):
        store.initialize()

    with sqlite3.connect(db_path) as connection:
        assert (
            connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'events'"
            ).fetchone()
            is None
        )
        assert (
            connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'store_identity'"
            ).fetchone()
            is not None
        )

    monkeypatch.undo()
    EvolutionStore(db_path=db_path, artifact_root=artifact_root).initialize()

    with sqlite3.connect(db_path) as connection:
        assert (
            connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'events'"
            ).fetchone()
            is not None
        )


def test_fresh_database_refuses_existing_context_snapshot_without_mutation(
    tmp_path: Path,
) -> None:
    artifact_root = tmp_path / "managed"
    files = ArtifactFileStore(artifact_root)
    files.initialize()
    snapshot = artifact_root / "contexts" / "ctx_existing.json"
    snapshot_bytes = b'{"request":{},"response":{}}'
    snapshot.write_bytes(snapshot_bytes)
    snapshot.chmod(0o600)
    db_path = tmp_path / "fresh.db"

    with pytest.raises(ValueError, match="fresh evolution database.*managed state"):
        EvolutionStore(db_path=db_path, artifact_root=artifact_root).initialize()

    assert snapshot.read_bytes() == snapshot_bytes
    assert not list((artifact_root / "contexts").glob(".openevo-context-*-*"))
    assert not (artifact_root / ".openevo-store.json").exists()
    assert not (artifact_root / "context_materializations" / ".openevo-store.json").exists()
    with sqlite3.connect(db_path) as connection:
        assert (
            connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'store_identity'"
            ).fetchone()
            is None
        )
