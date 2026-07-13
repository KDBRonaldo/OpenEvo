from __future__ import annotations

import json
from pathlib import Path

import pytest

from openevo.evolution import store as store_module
from openevo.evolution.context_projection import (
    MAX_ARTIFACT_ROUTING_JSON_BYTES,
    ContextProjectionResolveRequest,
)
from openevo.evolution.framework import (
    EvolutionExecutionProfile,
    RuntimeDestinationRoots,
    TargetConsumptionLimits,
)
from openevo.evolution.models import (
    ArtifactRegisterRequest,
    ArtifactType,
    ContextResolveRequest,
)
from openevo.evolution.server import create_app
from openevo.evolution.store import EvolutionStore
from tests.framework_testkit import verified_builtin_registry


def _request() -> ContextProjectionResolveRequest:
    return ContextProjectionResolveRequest(
        task_id="task-store-projection",
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
        target_limits={
            "text_memory": TargetConsumptionLimits(
                max_text_chars=64,
                max_text_bytes=64,
            )
        },
    )


def test_store_persists_versioned_projection_response(tmp_path: Path) -> None:
    registry = verified_builtin_registry(tmp_path / "registry")
    artifact_root = tmp_path / "managed"
    store = EvolutionStore(
        db_path=tmp_path / "evolution.db",
        artifact_root=artifact_root,
        executable_registry=registry,
    )
    store.initialize()
    payload = artifact_root / "payloads" / "memory.md"
    payload.parent.mkdir()
    payload.write_text("Use the verified parser memory.", encoding="utf-8")
    artifact = store.register_artifact(
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

    response = store.resolve_context_projections(_request())

    assert response.registry_digest == registry.snapshot.registry_digest
    assert response.selection.artifact_ids == (artifact.artifact_id,)
    assert response.projections[0].target_id == "text_memory"
    with store.connect() as connection:
        row = connection.execute(
            "SELECT * FROM contexts WHERE context_id = ?",
            (response.context_id,),
        ).fetchone()
        artifact_row = connection.execute(
            "SELECT manifest_json FROM artifacts WHERE artifact_id = ?",
            (artifact.artifact_id,),
        ).fetchone()
    assert row is not None
    assert artifact_row["manifest_json"] == json.dumps(
        {"content_path": "memory.md"},
        sort_keys=True,
        allow_nan=False,
    )
    stored_response = json.loads(row["response_json"])
    assert stored_response == response.model_dump(mode="json")
    assert json.loads(row["selected_artifact_ids_json"]) == [artifact.artifact_id]
    snapshot = json.loads(
        store.files.context_snapshot_path(response.context_id).read_text(
            encoding="utf-8"
        )
    )
    assert snapshot["request"] == _request().model_dump(mode="json")
    assert snapshot["response"] == response.model_dump(mode="json")
    encoded = json.dumps(snapshot, sort_keys=True)
    assert "file://" not in encoded
    assert "payload_handle" not in encoded

    with store.connect() as connection:
        stored = connection.execute(
            "SELECT manifest_path FROM artifacts WHERE artifact_id = ?",
            (artifact.artifact_id,),
        ).fetchone()
    Path(stored["manifest_path"]).write_text(
        json.dumps({"manifest": {"content_path": "tampered.md"}}),
        encoding="utf-8",
    )
    assert store.get_artifact(artifact.artifact_id).manifest == {
        "content_path": "tampered.md"
    }
    repeated = store.resolve_context_projections(_request())
    assert repeated.selection.artifact_ids == (artifact.artifact_id,)
    assert repeated.projections[0].instructions[0].text == (
        "Use the verified parser memory."
    )


def test_store_projection_requires_executable_registry(tmp_path: Path) -> None:
    store = EvolutionStore(
        db_path=tmp_path / "evolution.db",
        artifact_root=tmp_path / "managed",
    )
    store.initialize()

    with pytest.raises(ValueError, match="verified executable registry"):
        store.resolve_context_projections(_request())


def test_evolution_app_retains_full_verified_registry(tmp_path: Path) -> None:
    registry = verified_builtin_registry(tmp_path / "registry")
    app = create_app(
        db_path=tmp_path / "evolution.db",
        artifact_root=tmp_path / "managed",
        executable_registry=registry,
    )

    assert app.state.evolution_registry is registry
    assert app.state.registry_snapshot is registry.snapshot
    assert app.state.store._executable_registry is registry


def test_projection_context_retries_database_and_snapshot_path_collisions(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry = verified_builtin_registry(tmp_path / "registry")
    store = EvolutionStore(
        db_path=tmp_path / "evolution.db",
        artifact_root=tmp_path / "managed",
        executable_registry=registry,
    )
    store.initialize()
    with store.connect() as connection:
        connection.execute(
            """
            INSERT INTO contexts (
                context_id, created_at, request_json, response_json,
                selected_artifact_ids_json
            ) VALUES (?, ?, ?, ?, ?)
            """,
            ("ctx-db-collision", "now", "{}", "{}", "[]"),
        )
        connection.commit()
    stale_snapshot = store.files.context_snapshot_path("ctx-path-collision")
    stale_snapshot.write_text("stale", encoding="utf-8")
    identifiers = iter(
        ("ctx-db-collision", "ctx-path-collision", "ctx-after-collisions")
    )
    monkeypatch.setattr(store_module, "new_id", lambda _prefix: next(identifiers))

    response = store.resolve_context_projections(_request())

    assert response.context_id == "ctx-after-collisions"
    assert stale_snapshot.read_text(encoding="utf-8") == "stale"


def test_shared_context_persistence_rolls_back_write_failure_and_legacy_retries(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry = verified_builtin_registry(tmp_path / "registry")
    store = EvolutionStore(
        db_path=tmp_path / "evolution.db",
        artifact_root=tmp_path / "managed",
        executable_registry=registry,
    )
    store.initialize()
    original_write = store_module._write_json_strict_exclusive

    def fail_write(*_args, **_kwargs):
        raise OSError("injected context snapshot failure")

    monkeypatch.setattr(store_module, "new_id", lambda _prefix: "ctx-write-failure")
    monkeypatch.setattr(store_module, "_write_json_strict_exclusive", fail_write)
    with pytest.raises(OSError, match="snapshot failure"):
        store.resolve_context_projections(_request())
    with store.connect() as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM contexts WHERE context_id = ?",
            ("ctx-write-failure",),
        ).fetchone()[0] == 0

    monkeypatch.setattr(store_module, "_write_json_strict_exclusive", original_write)
    with store.connect() as connection:
        connection.execute(
            """
            INSERT INTO contexts (
                context_id, created_at, request_json, response_json,
                selected_artifact_ids_json
            ) VALUES (?, ?, ?, ?, ?)
            """,
            ("ctx-legacy-collision", "now", "{}", "{}", "[]"),
        )
        connection.commit()
    identifiers = iter(("ctx-legacy-collision", "ctx-legacy-after-collision"))
    monkeypatch.setattr(store_module, "new_id", lambda _prefix: next(identifiers))

    response = store.resolve_context(
        ContextResolveRequest(task_id="legacy-task", instruction="Continue.")
    )

    assert response.context_id == "ctx-legacy-after-collision"


def test_projection_store_rejects_promoted_candidate_overflow_before_loading(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry = verified_builtin_registry(tmp_path / "registry")
    artifact_root = tmp_path / "managed"
    store = EvolutionStore(
        db_path=tmp_path / "evolution.db",
        artifact_root=artifact_root,
        executable_registry=registry,
    )
    store.initialize()
    for index in range(3):
        payload = artifact_root / "payloads" / f"memory-{index}.md"
        payload.parent.mkdir(exist_ok=True)
        payload.write_text(f"memory {index}", encoding="utf-8")
        store.register_artifact(
            ArtifactRegisterRequest(
                type=ArtifactType.TEXT_MEMORY,
                name=f"memory {index}",
                uri=payload.as_uri(),
                manifest={"content_path": payload.name},
                promoted=True,
            )
        )
    monkeypatch.setattr(
        store_module,
        "MAX_CONTEXT_PROJECTION_CANDIDATES",
        2,
        raising=False,
    )

    with pytest.raises(ValueError, match="promoted candidate budget"):
        store.resolve_context_projections(_request())


def test_explicit_artifact_allowlist_is_applied_before_global_candidate_limit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry = verified_builtin_registry(tmp_path / "registry")
    artifact_root = tmp_path / "managed"
    store = EvolutionStore(
        db_path=tmp_path / "evolution.db",
        artifact_root=artifact_root,
        executable_registry=registry,
    )
    store.initialize()
    artifacts = []
    for index in range(2):
        payload = artifact_root / "payloads" / f"memory-{index}.md"
        payload.parent.mkdir(exist_ok=True)
        payload.write_text(f"memory {index}", encoding="utf-8")
        artifacts.append(
            store.register_artifact(
                ArtifactRegisterRequest(
                    type=ArtifactType.TEXT_MEMORY,
                    name=f"memory {index}",
                    uri=payload.as_uri(),
                    manifest={"content_path": payload.name},
                    promoted=True,
                )
            )
        )
    monkeypatch.setattr(
        store_module,
        "MAX_CONTEXT_PROJECTION_CANDIDATES",
        1,
    )
    request_payload = _request().model_dump(mode="json")
    request_payload["metadata"]["evolution"] = {
        "context_artifact_ids": [artifacts[0].artifact_id]
    }

    response = store.resolve_context_projections(
        ContextProjectionResolveRequest.model_validate(request_payload)
    )

    assert response.selection.artifact_ids == (artifacts[0].artifact_id,)


def test_cheap_remote_candidates_do_not_displace_implicit_local_candidates(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry = verified_builtin_registry(tmp_path / "registry")
    artifact_root = tmp_path / "managed"
    store = EvolutionStore(
        db_path=tmp_path / "evolution.db",
        artifact_root=artifact_root,
        executable_registry=registry,
    )
    store.initialize()
    for index in range(2):
        store.register_artifact(
            ArtifactRegisterRequest(
                type=ArtifactType.TEXT_MEMORY,
                name=f"remote memory {index}",
                uri=f"hf://organization/memory-{index}@revision",
                compatibility={"task_tags": ["parser"]},
                scores={"quality": 1.0},
                promoted=True,
            )
        )
    payload = artifact_root / "payloads" / "local.md"
    payload.parent.mkdir()
    payload.write_text("local memory", encoding="utf-8")
    local = store.register_artifact(
        ArtifactRegisterRequest(
            type=ArtifactType.TEXT_MEMORY,
            name="local memory",
            uri=payload.as_uri(),
            manifest={"content_path": payload.name},
            compatibility={"task_tags": ["parser"]},
            scores={"quality": 0.5},
            promoted=True,
        )
    )
    monkeypatch.setattr(
        store_module,
        "MAX_CONTEXT_PROJECTION_CANDIDATES",
        1,
    )

    response = store.resolve_context_projections(_request())

    assert response.selection.artifact_ids == (local.artifact_id,)


def test_store_does_not_persist_skip_for_incompatible_rejected_row(
    tmp_path: Path,
) -> None:
    registry = verified_builtin_registry(tmp_path / "registry")
    store = EvolutionStore(
        db_path=tmp_path / "evolution.db",
        artifact_root=tmp_path / "managed",
        executable_registry=registry,
    )
    store.initialize()
    remote = store.register_artifact(
        ArtifactRegisterRequest(
            type=ArtifactType.TEXT_MEMORY,
            name="other task remote memory",
            uri="hf://organization/private-memory@revision",
            compatibility={"task_tags": ["other-task"]},
            promoted=True,
        )
    )

    response = store.resolve_context_projections(_request())

    assert response.selection.artifact_ids == ()
    assert response.selection.skipped_artifacts == ()
    snapshot = json.loads(
        store.files.context_snapshot_path(response.context_id).read_text(
            encoding="utf-8"
        )
    )
    assert remote.artifact_id not in json.dumps(snapshot, sort_keys=True)


@pytest.mark.parametrize("mode", ["bytes", "depth"])
def test_legacy_registration_preserves_metadata_rejected_by_projection(
    tmp_path: Path,
    mode: str,
) -> None:
    registry = verified_builtin_registry(tmp_path / "registry")
    artifact_root = tmp_path / "managed"
    store = EvolutionStore(
        db_path=tmp_path / "evolution.db",
        artifact_root=artifact_root,
        executable_registry=registry,
    )
    store.initialize()
    compatibility: dict[str, object] = {
        "padding": "x" * MAX_ARTIFACT_ROUTING_JSON_BYTES
    }
    if mode == "depth":
        nested: dict[str, object] = {}
        for _ in range(17):
            nested = {"child": nested}
        compatibility = nested

    payload = artifact_root / "payloads" / f"{mode}.md"
    payload.parent.mkdir()
    payload.write_text("legacy metadata", encoding="utf-8")

    artifact = store.register_artifact(
        ArtifactRegisterRequest(
            type=ArtifactType.TEXT_MEMORY,
            name="legacy metadata",
            uri=payload.as_uri(),
            manifest={"content_path": payload.name},
            compatibility=compatibility,
            promoted=True,
        )
    )

    assert store.get_artifact(artifact.artifact_id).compatibility == compatibility
    if mode == "bytes":
        rows = store._promoted_artifact_rows(
            maximum=1,
            artifact_types={"text_memory"},
        )
        assert rows == [
            {
                "artifact_id": artifact.artifact_id,
                "type": "text_memory",
                "name": artifact.artifact_id,
                "state": "active",
                "created_at": rows[0]["created_at"],
                "uri": "",
                "manifest_json": None,
                "compatibility_json": None,
                "scores_json": "{}",
                "promoted": 1,
                "projection_skip_reason": "metadata_policy_rejected",
            }
        ]
    response = store.resolve_context_projections(_request())
    assert response.selection.artifact_ids == ()
    assert response.selection.skipped_artifacts == ()


@pytest.mark.parametrize(
    ("column", "encoded"),
    [
        ("compatibility_json", "not-json"),
        ("scores_json", "[]"),
    ],
)
def test_invalid_routing_json_is_a_synthetic_skip_not_an_eligible_candidate(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    column: str,
    encoded: str,
) -> None:
    registry = verified_builtin_registry(tmp_path / "registry")
    artifact_root = tmp_path / "managed"
    store = EvolutionStore(
        db_path=tmp_path / "evolution.db",
        artifact_root=artifact_root,
        executable_registry=registry,
    )
    store.initialize()
    artifacts = []
    for name in ("invalid", "valid"):
        payload = artifact_root / "payloads" / f"{name}.md"
        payload.parent.mkdir(exist_ok=True)
        payload.write_text(name, encoding="utf-8")
        artifacts.append(
            store.register_artifact(
                ArtifactRegisterRequest(
                    type=ArtifactType.TEXT_MEMORY,
                    name=name,
                    uri=payload.as_uri(),
                    manifest={"content_path": payload.name},
                    compatibility={"task_tags": ["parser"]},
                    promoted=True,
                )
            )
        )
    with store.connect() as connection:
        connection.execute(
            f"UPDATE artifacts SET {column} = ? WHERE artifact_id = ?",  # noqa: S608
            (encoded, artifacts[0].artifact_id),
        )
        connection.commit()

    rows = store._promoted_artifact_rows(
        maximum=2,
        artifact_types={"text_memory"},
    )
    invalid_row = next(
        row for row in rows if row["artifact_id"] == artifacts[0].artifact_id
    )
    assert invalid_row["projection_skip_reason"] == "metadata_policy_rejected"
    if column == "compatibility_json":
        assert invalid_row["compatibility_json"] is None
    else:
        assert json.loads(invalid_row["compatibility_json"]) == {
            "task_tags": ["parser"]
        }
    assert invalid_row["scores_json"] == "{}"

    monkeypatch.setattr(store_module, "MAX_CONTEXT_PROJECTION_CANDIDATES", 1)
    response = store.resolve_context_projections(_request())
    assert response.selection.artifact_ids == (artifacts[1].artifact_id,)


def test_legacy_registration_preserves_manifest_outside_projection_policy(
    tmp_path: Path,
) -> None:
    registry = verified_builtin_registry(tmp_path / "registry")
    artifact_root = tmp_path / "managed"
    store = EvolutionStore(
        db_path=tmp_path / "evolution.db",
        artifact_root=artifact_root,
        executable_registry=registry,
    )
    store.initialize()
    payload = artifact_root / "payloads" / "surrogate.md"
    payload.parent.mkdir()
    payload.write_text("legacy manifest", encoding="utf-8")
    manifest = {"content_path": payload.name, "legacy_value": "\ud800"}

    artifact = store.register_artifact(
        ArtifactRegisterRequest(
            type=ArtifactType.TEXT_MEMORY,
            name="legacy manifest",
            uri=payload.as_uri(),
            manifest=manifest,
            compatibility={"task_tags": ["parser"]},
            promoted=True,
        )
    )

    assert store.get_artifact(artifact.artifact_id).manifest == manifest
    response = store.resolve_context_projections(_request())
    assert response.selection.artifact_ids == ()
    assert response.selection.skipped_artifact_ids == (artifact.artifact_id,)
    assert response.selection.skipped_artifacts[0].reason == (
        "metadata_policy_rejected"
    )


def test_store_quarantines_migrated_artifact_without_manifest_binding(
    tmp_path: Path,
) -> None:
    registry = verified_builtin_registry(tmp_path / "registry")
    artifact_root = tmp_path / "managed"
    store = EvolutionStore(
        db_path=tmp_path / "evolution.db",
        artifact_root=artifact_root,
        executable_registry=registry,
    )
    store.initialize()
    artifacts = []
    for name, quality in (("legacy", 1.0), ("current", 0.5)):
        payload = artifact_root / "payloads" / f"{name}.md"
        payload.parent.mkdir(exist_ok=True)
        payload.write_text(name, encoding="utf-8")
        artifacts.append(
            store.register_artifact(
                ArtifactRegisterRequest(
                    type=ArtifactType.TEXT_MEMORY,
                    name=name,
                    uri=payload.as_uri(),
                    manifest={"content_path": payload.name},
                    scores={"quality": quality},
                    promoted=True,
                )
            )
        )
    with store.connect() as connection:
        connection.execute(
            "UPDATE artifacts SET manifest_json = '' WHERE artifact_id = ?",
            (artifacts[0].artifact_id,),
        )
        connection.commit()

    response = store.resolve_context_projections(_request())

    assert response.selection.artifact_ids == (artifacts[1].artifact_id,)
    assert response.selection.skipped_artifact_ids == (artifacts[0].artifact_id,)
    assert response.selection.skipped_artifacts[0].reason == (
        "unbound_legacy_metadata"
    )


def test_baseline_artifact_schema_migrates_without_trusting_legacy_manifest(
    tmp_path: Path,
) -> None:
    registry = verified_builtin_registry(tmp_path / "registry")
    artifact_root = tmp_path / "managed"
    store = EvolutionStore(
        db_path=tmp_path / "evolution.db",
        artifact_root=artifact_root,
        executable_registry=registry,
    )
    store.initialize()
    artifact_id = "art_legacy_schema"
    payload = artifact_root / "payloads" / "legacy.md"
    payload.parent.mkdir()
    payload.write_text("legacy memory", encoding="utf-8")
    manifest_path = store.files.artifact_manifest_path("text_memory", artifact_id)
    manifest_path.parent.mkdir(parents=True)
    manifest_path.write_text(
        json.dumps(
            {
                "artifact_id": artifact_id,
                "manifest": {"content_path": payload.name},
            }
        ),
        encoding="utf-8",
    )
    with store.connect() as connection:
        connection.execute("ALTER TABLE artifacts DROP COLUMN manifest_json")
        connection.execute(
            """
            INSERT INTO artifacts (
                artifact_id, type, name, version, state, created_at, uri,
                manifest_path, lineage_json, compatibility_json, scores_json,
                tags_json, promoted, staging_job_id
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                artifact_id,
                "text_memory",
                "legacy memory",
                1,
                "active",
                "2026-01-01T00:00:00+00:00",
                payload.as_uri(),
                str(manifest_path),
                "{}",
                json.dumps({"task_tags": ["parser"]}),
                json.dumps({"quality": 1.0}),
                "[]",
                1,
                None,
            ),
        )
        connection.commit()

    store.initialize()

    with store.connect() as connection:
        row = connection.execute(
            "SELECT manifest_json FROM artifacts WHERE artifact_id = ?",
            (artifact_id,),
        ).fetchone()
    assert row["manifest_json"] is None
    assert store.get_artifact(artifact_id).manifest == {
        "content_path": "legacy.md"
    }
    response = store.resolve_context_projections(_request())
    assert response.selection.artifact_ids == ()
    assert response.selection.skipped_artifact_ids == (artifact_id,)
    assert response.selection.skipped_artifacts[0].reason == (
        "unbound_legacy_metadata"
    )
