from __future__ import annotations

import os
from pathlib import Path
import sqlite3
import threading

import pytest

from openevo.evolution import store as store_module
from openevo.evolution.context_projection import ContextProjectionResolveRequest
from openevo.evolution.framework import (
    EvolutionExecutionProfile,
    RuntimeDestinationRoots,
)
from openevo.evolution.models import ArtifactRegisterRequest, ArtifactType
from openevo.evolution.store import EvolutionStore
from tests.framework_testkit import verified_builtin_registry


def _request() -> ContextProjectionResolveRequest:
    return ContextProjectionResolveRequest(
        task_id="task-materialization-integrity",
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


@pytest.fixture
def materialization_store(tmp_path: Path) -> EvolutionStore:
    artifact_root = tmp_path / "managed"
    store = EvolutionStore(
        db_path=tmp_path / "evolution.db",
        artifact_root=artifact_root,
        executable_registry=verified_builtin_registry(tmp_path / "registry"),
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
    return store


def _tamper_database_store_id(store: EvolutionStore) -> None:
    with sqlite3.connect(store.db_path) as connection:
        current = connection.execute(
            "SELECT store_id FROM store_identity WHERE singleton = 1"
        ).fetchone()
        assert current is not None
        replacement = (
            "store_0000000000000000"
            if current[0] != "store_0000000000000000"
            else "store_1111111111111111"
        )
        updated = connection.execute(
            "UPDATE store_identity SET store_id = ? WHERE singleton = 1",
            (replacement,),
        )
        assert updated.rowcount == 1


def _context_row_counts(store: EvolutionStore, context_id: str) -> tuple[int, int]:
    with sqlite3.connect(store.db_path) as connection:
        context_count = connection.execute(
            "SELECT COUNT(*) FROM contexts WHERE context_id = ?",
            (context_id,),
        ).fetchone()[0]
        materialization_count = connection.execute(
            "SELECT COUNT(*) FROM context_materializations WHERE context_id = ?",
            (context_id,),
        ).fetchone()[0]
    return int(context_count), int(materialization_count)


def test_store_materialization_root_lock_is_same_thread_reentrant(
    tmp_path: Path,
) -> None:
    store = EvolutionStore(
        db_path=tmp_path / "evolution.db",
        artifact_root=tmp_path / "managed",
    )
    store.initialize()
    completed = threading.Event()
    descriptors: list[int] = []
    failures: list[BaseException] = []

    def enter_nested_lock() -> None:
        try:
            with store._locked_context_materialization_root() as outer_descriptor:
                outer_identity = os.fstat(outer_descriptor)
                with store._locked_context_materialization_root() as inner_descriptor:
                    descriptors.extend((outer_descriptor, inner_descriptor))
                    assert os.path.samestat(outer_identity, os.fstat(inner_descriptor))
                assert os.path.samestat(outer_identity, os.fstat(outer_descriptor))
        except BaseException as exc:
            failures.append(exc)
        finally:
            completed.set()

    thread = threading.Thread(target=enter_nested_lock, daemon=True)
    thread.start()

    assert completed.wait(timeout=5), "nested store materialization lock deadlocked"
    thread.join(timeout=1)
    assert not thread.is_alive()
    if failures:
        raise failures[0]
    assert len(descriptors) == 2
    assert descriptors[0] == descriptors[1]


def test_open_materialized_blob_rejects_tampered_database_store_identity(
    materialization_store: EvolutionStore,
) -> None:
    context = materialization_store.resolve_materialized_context(_request())
    blob = context.blobs[0]
    _tamper_database_store_id(materialization_store)
    opened = False

    with pytest.raises(ValueError, match="identity"):
        with materialization_store.open_materialized_blob(context.context_id, blob.blob_id):
            opened = True

    assert not opened


def test_resolve_rejects_tampered_database_identity_without_publication(
    materialization_store: EvolutionStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    context_id = "ctx-tampered-store-identity"
    monkeypatch.setattr(store_module, "new_id", lambda _prefix: context_id)
    _tamper_database_store_id(materialization_store)

    with pytest.raises(ValueError, match="identity"):
        materialization_store.resolve_materialized_context(_request())

    assert _context_row_counts(materialization_store, context_id) == (0, 0)
    assert not materialization_store.files.context_materialization_dir(context_id).exists()


def test_precommit_identity_recheck_discards_published_bundle(
    materialization_store: EvolutionStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    context_id = "ctx-precommit-identity-drift"
    monkeypatch.setattr(store_module, "new_id", lambda _prefix: context_id)
    original_verify = materialization_store._verify_bound_store_identity
    publication_observed = False
    identity_changed_after_initial_check = False

    def verify_then_change_identity(connection: sqlite3.Connection) -> None:
        nonlocal publication_observed, identity_changed_after_initial_check
        original_verify(connection)
        bundle = materialization_store.files.context_materialization_dir(context_id)
        if not bundle.is_dir() or identity_changed_after_initial_check:
            return
        publication_observed = True
        current = connection.execute(
            "SELECT store_id FROM store_identity WHERE singleton = 1"
        ).fetchone()
        assert current is not None
        replacement = (
            "store_0000000000000000"
            if current[0] != "store_0000000000000000"
            else "store_1111111111111111"
        )
        connection.execute(
            "UPDATE store_identity SET store_id = ? WHERE singleton = 1",
            (replacement,),
        )
        identity_changed_after_initial_check = True

    monkeypatch.setattr(
        materialization_store,
        "_verify_bound_store_identity",
        verify_then_change_identity,
    )

    with pytest.raises(ValueError, match="identity"):
        materialization_store.resolve_materialized_context(_request())

    assert publication_observed
    assert identity_changed_after_initial_check
    assert _context_row_counts(materialization_store, context_id) == (0, 0)
    assert not materialization_store.files.context_materialization_dir(context_id).exists()


def test_precommit_rejects_replaced_publication_without_deleting_replacement(
    materialization_store: EvolutionStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    context_id = "ctx-precommit-publication-replacement"
    monkeypatch.setattr(store_module, "new_id", lambda _prefix: context_id)
    materializer = materialization_store._context_materializer
    assert materializer is not None
    original_materialize = materializer.materialize_for_publication
    materialization_root = materialization_store.files.root / "context_materializations"
    moved = materialization_root / "moved-original-publication"
    replacement = materialization_root / context_id

    def replace_after_publication(*args, **kwargs):
        receipt = original_materialize(*args, **kwargs)
        replacement.rename(moved)
        replacement.mkdir()
        (replacement / "sentinel.txt").write_text("replacement", encoding="utf-8")
        return receipt

    monkeypatch.setattr(
        materializer,
        "materialize_for_publication",
        replace_after_publication,
    )

    with pytest.raises(ValueError, match="publication|context|identity"):
        materialization_store.resolve_materialized_context(_request())

    assert _context_row_counts(materialization_store, context_id) == (0, 0)
    assert (replacement / "sentinel.txt").read_text(encoding="utf-8") == "replacement"
    assert (moved / "manifest.json").is_file()


def test_postcommit_verification_failure_does_not_discard_committed_publication(
    materialization_store: EvolutionStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    context_id = "ctx-postcommit-verification-failure"
    monkeypatch.setattr(store_module, "new_id", lambda _prefix: context_id)
    original_verify = materialization_store._verify_bound_artifact_root_descriptor
    injected = False

    def fail_after_context_commit(descriptor: int) -> None:
        nonlocal injected
        original_verify(descriptor)
        if injected:
            return
        with sqlite3.connect(materialization_store.db_path) as connection:
            committed = connection.execute(
                "SELECT 1 FROM context_materializations WHERE context_id = ?",
                (context_id,),
            ).fetchone()
        if committed is not None:
            injected = True
            raise RuntimeError("injected postcommit root verification failure")

    monkeypatch.setattr(
        materialization_store,
        "_verify_bound_artifact_root_descriptor",
        fail_after_context_commit,
    )

    with pytest.raises(RuntimeError, match="postcommit root verification failure"):
        materialization_store.resolve_materialized_context(_request())

    assert injected
    assert _context_row_counts(materialization_store, context_id) == (1, 1)
    bundle = materialization_store.files.context_materialization_dir(context_id)
    assert (bundle / "manifest.json").is_file()
    assert not list(bundle.parent.glob(".openevo-quarantine-*"))

    monkeypatch.setattr(
        materialization_store,
        "_verify_bound_artifact_root_descriptor",
        original_verify,
    )
    materialization_store.initialize()
    assert (bundle / "manifest.json").is_file()


def test_root_replaced_after_publication_callback_prevents_database_commit(
    materialization_store: EvolutionStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    context_id = "ctx-final-root-replacement"
    monkeypatch.setattr(store_module, "new_id", lambda _prefix: context_id)
    materializer = materialization_store._context_materializer
    assert materializer is not None
    original_verify = materializer.verify_publication
    root = materialization_store.files.root / "context_materializations"
    moved = materialization_store.files.root / "context_materializations-moved"
    replacement_sentinel = root / "replacement.txt"

    def verify_then_replace(*args, **kwargs) -> None:
        original_verify(*args, **kwargs)
        root.rename(moved)
        root.mkdir(mode=0o700)
        replacement_sentinel.write_text("replacement", encoding="utf-8")

    monkeypatch.setattr(materializer, "verify_publication", verify_then_replace)

    with pytest.raises(ValueError, match="root|binding|identity"):
        materialization_store.resolve_materialized_context(_request())

    assert _context_row_counts(materialization_store, context_id) == (0, 0)
    assert replacement_sentinel.read_text(encoding="utf-8") == "replacement"
    assert not (root / context_id).exists()
    assert (moved / context_id / "manifest.json").is_file()
