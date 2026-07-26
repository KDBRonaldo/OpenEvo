from __future__ import annotations

import hashlib
import json
import multiprocessing
import os
from pathlib import Path
import shutil
import sqlite3
import stat
import threading

import pytest

from openevo.evolution import store as store_module
from openevo.evolution.context_projection import (
    MAX_ARTIFACT_ROUTING_JSON_BYTES,
    ContextProjectionResolveRequest,
)
from openevo.evolution.framework import (
    EvolutionTargetSelection,
    EvolutionExecutionProfile,
    RuntimeDestinationRoots,
    TargetConsumptionLimits,
    canonical_json,
)
from openevo.evolution.models import (
    ArtifactRegisterRequest,
    ArtifactType,
    ContextResolveRequest,
    WorkerClaimRequest,
    WorkerCompleteRequest,
)
from openevo.evolution.planned_jobs import (
    PlanBoundJobCreateRequest,
    PlannedInputBinding,
)
from openevo.evolution.server import create_app
from openevo.evolution.store import EvolutionStore
from tests.framework_testkit import verified_builtin_registry


def _initialize_store_in_process(
    db_path: str,
    artifact_root: str,
    result_queue,
) -> None:
    try:
        EvolutionStore(db_path=db_path, artifact_root=artifact_root).initialize()
    except BaseException as exc:
        result_queue.put(("error", str(exc)))
    else:
        result_queue.put(("ok", ""))


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
        store.files.context_snapshot_path(response.context_id).read_text(encoding="utf-8")
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
    assert store.get_artifact(artifact.artifact_id).manifest == {"content_path": "tampered.md"}
    repeated = store.resolve_context_projections(_request())
    assert repeated.selection.artifact_ids == (artifact.artifact_id,)
    assert repeated.projections[0].instructions[0].text == ("Use the verified parser memory.")


def test_store_materializes_and_persists_registry_bound_context(tmp_path: Path) -> None:
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

    response = store.resolve_materialized_context(_request())

    assert response.registry_digest == registry.snapshot.registry_digest
    assert response.selection.artifact_ids == (artifact.artifact_id,)
    assert response.instruction == (
        "Use the following long-term memory for this task:\nUse the verified parser memory."
    )
    assert len(response.blobs) == 1
    assert response.blobs[0].destination_relative_path == "memory.md"
    assert {item.name: item.value for item in response.environment} == {
        "OPENEVO_MEMORY_FILE": "/openevo/session/evolution/memory.md"
    }
    with store.open_materialized_blob(
        response.context_id,
        response.blobs[0].blob_id,
    ) as lease:
        assert lease.stream.read().decode("utf-8") == "Use the verified parser memory."

    with store.connect() as connection:
        context_row = connection.execute(
            "SELECT response_json FROM contexts WHERE context_id = ?",
            (response.context_id,),
        ).fetchone()
        materialization_row = connection.execute(
            "SELECT * FROM context_materializations WHERE context_id = ?",
            (response.context_id,),
        ).fetchone()
    assert json.loads(context_row["response_json"]) == response.model_dump(mode="json")
    assert materialization_row["registry_digest"] == registry.snapshot.registry_digest
    assert materialization_row["request_digest"] == response.request_digest
    assert materialization_row["manifest_json"] == canonical_json(response)
    stored_manifest = json.loads(materialization_row["manifest_json"])
    assert stored_manifest == response.model_dump(mode="json")
    encoded = json.dumps(stored_manifest, sort_keys=True)
    assert str(artifact_root) not in encoded
    assert "file://" not in encoded
    assert "payload_handle" not in encoded

    with store.connect() as connection:
        connection.execute(
            "UPDATE context_materializations SET manifest_json = ? WHERE context_id = ?",
            (json.dumps(stored_manifest, sort_keys=True), response.context_id),
        )
        connection.commit()
    with pytest.raises(ValueError, match="not canonical"):
        with store.open_materialized_blob(
            response.context_id,
            response.blobs[0].blob_id,
        ):
            pass


def test_restart_accepts_materialization_from_before_optional_owner_transition_metadata(
    tmp_path: Path,
) -> None:
    registry = verified_builtin_registry(tmp_path / "registry")
    artifact_root = tmp_path / "managed"
    db_path = tmp_path / "evolution.db"
    store = EvolutionStore(
        db_path=db_path,
        artifact_root=artifact_root,
        executable_registry=registry,
    )
    store.initialize()
    request_payload = _request().model_dump(mode="json")
    request_payload["metadata"]["evolution"] = {"context_artifact_ids": []}
    request = ContextProjectionResolveRequest.model_validate(request_payload)
    materialized = store.resolve_materialized_context(request)

    with store.connect() as connection:
        row = connection.execute(
            """
            SELECT contexts.request_json, contexts.response_json,
                   context_materializations.manifest_json
            FROM context_materializations
            JOIN contexts USING (context_id)
            WHERE context_id = ?
            """,
            (materialized.context_id,),
        ).fetchone()
        assert row is not None
        legacy_request = json.loads(row["request_json"])
        legacy_request["metadata"]["evolution"].pop(
            "context_artifact_owner_transition_ids",
            None,
        )
        legacy_request_json = canonical_json(legacy_request)
        legacy_request_digest = hashlib.sha256(
            legacy_request_json.encode("utf-8")
        ).hexdigest()
        legacy_response = json.loads(row["response_json"])
        legacy_response["request_digest"] = legacy_request_digest
        legacy_response_json = canonical_json(legacy_response)
        legacy_manifest = json.loads(row["manifest_json"])
        legacy_manifest["request_digest"] = legacy_request_digest
        legacy_manifest_json = canonical_json(legacy_manifest)
        connection.execute(
            """
            UPDATE contexts
            SET request_json = ?, response_json = ?
            WHERE context_id = ?
            """,
            (
                legacy_request_json,
                legacy_response_json,
                materialized.context_id,
            ),
        )
        connection.execute(
            """
            UPDATE context_materializations
            SET request_digest = ?, manifest_json = ?
            WHERE context_id = ?
            """,
            (
                legacy_request_digest,
                legacy_manifest_json,
                materialized.context_id,
            ),
        )
        connection.commit()

    store.files.context_snapshot_path(materialized.context_id).write_bytes(
        store_module._context_snapshot_bytes(
            legacy_request,
            legacy_response,
        )
    )
    (
        store.files.context_materialization_dir(materialized.context_id)
        / "manifest.json"
    ).write_text(legacy_manifest_json, encoding="utf-8")

    restarted = EvolutionStore(
        db_path=db_path,
        artifact_root=artifact_root,
        executable_registry=registry,
    )
    restarted.initialize()

    recovered = restarted.get_materialized_context(materialized.context_id)
    assert recovered.request_digest == legacy_request_digest
    with restarted.connect() as connection:
        recovered_request = json.loads(
            connection.execute(
                "SELECT request_json FROM contexts WHERE context_id = ?",
                (materialized.context_id,),
            ).fetchone()["request_json"]
        )
    assert (
        "context_artifact_owner_transition_ids"
        not in recovered_request["metadata"]["evolution"]
    )


def test_successor_materialization_privately_consumes_only_exact_sealed_outputs(
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
    dataset = store.register_artifact(
        ArtifactRegisterRequest(
            type=ArtifactType.DATASET,
            name="transition dataset",
            uri=(artifact_root / "dataset.json").as_uri(),
            promoted=True,
        )
    )
    plan = registry.snapshot.compile_plan(
        plan_id="successor-plan-1",
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
        profile=_request().execution_profile,
    )
    create = PlanBoundJobCreateRequest(
        plan=plan,
        target_id="skill_bundle",
        job_type="skill_bundle_reflector",
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
        successor_transition_id="successor-transition-1",
        core_config={"promoted": True},
    )
    job = store.create_plan_bound_job(
        create,
        snapshot=registry.snapshot,
    )
    selection = plan.selections[0]
    claim = store.claim_job(
        WorkerClaimRequest(
            worker_id="verified-worker",
            capabilities=[create.job_type],
            method_capabilities=[selection.method_id],
            method_identity_capabilities={
                selection.method_id: (
                    selection.method_identity_digest
                )
            },
        )
    )
    assert claim.job is not None
    skill = artifact_root / "payloads" / "skill"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text(
        "# Exact sealed skill\n",
        encoding="utf-8",
    )
    completed = store.complete_job(
        job.job_id,
        WorkerCompleteRequest(
            lease_id=claim.job.lease_id,
            artifacts=[
                ArtifactRegisterRequest(
                    type=ArtifactType.SKILL_BUNDLE,
                    name="exact sealed skill",
                    uri=skill.as_uri(),
                    manifest={},
                    compatibility={"agent_harness": ["codex"]},
                    promoted=True,
                )
            ],
        ),
    )
    artifact_id = completed["artifact_ids"][0]
    with pytest.raises(ValueError, match="unknown artifact"):
        store.get_artifact(artifact_id)
    generic = store.resolve_materialized_context(_request())
    assert artifact_id not in generic.selection.artifact_ids

    request_payload = _request().model_dump(mode="json")
    request_payload.update(
        {
            "successor_transition_id": "successor-transition-1",
            "predecessor_project_head_id": "project-head-genesis",
        }
    )
    request_payload["metadata"]["evolution"] = {
        "context_artifact_ids": [artifact_id]
    }
    exact_request = ContextProjectionResolveRequest.model_validate(
        request_payload
    )
    exact = store.resolve_materialized_context(exact_request)
    assert exact.successor_transition_id == "successor-transition-1"
    assert exact.selection.artifact_ids == (artifact_id,)

    wrong_payload = exact_request.model_dump(mode="json")
    wrong_payload["successor_transition_id"] = (
        "different-successor-transition"
    )
    with pytest.raises(ValueError, match="unavailable"):
        store.resolve_materialized_context(
            ContextProjectionResolveRequest.model_validate(
                wrong_payload
            )
        )
    expected_receipt = {
        "successor_transition_id": "successor-transition-1",
        "discarded_artifact_ids": [artifact_id],
        "discarded_materialized_context_ids": [exact.context_id],
    }
    materializer = store._context_materializer
    assert materializer is not None
    original_discard = materializer.discard_persisted

    def _interrupt_postcommit(*_args, **_kwargs):
        raise RuntimeError("simulated postcommit cleanup interruption")

    monkeypatch.setattr(
        materializer,
        "discard_persisted",
        _interrupt_postcommit,
    )
    with pytest.raises(
        RuntimeError,
        match="postcommit cleanup interruption",
    ):
        store.discard_successor_transition_outputs(
            "successor-transition-1"
        )
    with store.connect() as connection:
        durable_receipt = connection.execute(
            "SELECT receipt_json FROM "
            "successor_transition_discards "
            "WHERE successor_transition_id = ?",
            ("successor-transition-1",),
        ).fetchone()
        assert connection.execute(
            "SELECT 1 FROM artifacts WHERE artifact_id = ?",
            (artifact_id,),
        ).fetchone() is None
        assert connection.execute(
            "SELECT 1 FROM context_materializations "
            "WHERE context_id = ?",
            (exact.context_id,),
        ).fetchone() is None
    assert durable_receipt is not None
    assert json.loads(durable_receipt["receipt_json"]) == (
        expected_receipt
    )
    monkeypatch.setattr(
        materializer,
        "discard_persisted",
        original_discard,
    )

    receipt = store.discard_successor_transition_outputs(
        "successor-transition-1"
    )
    assert receipt == expected_receipt
    with pytest.raises(ValueError, match="not persisted"):
        store.get_materialized_context(exact.context_id)
    assert store.get_materialized_context(generic.context_id) == generic
    assert not store.files.context_snapshot_path(exact.context_id).exists()
    with pytest.raises(ValueError, match="discarded"):
        store.resolve_materialized_context(exact_request)
    assert store.discard_successor_transition_outputs(
        "successor-transition-1"
    ) == receipt


def test_store_materializes_all_builtin_carriers_through_verified_registry(
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
    payload_root = artifact_root / "payloads"
    payload_root.mkdir()

    memory_path = payload_root / "memory.md"
    memory_path.write_text(
        "\nRemember the verified parser invariant.\n\n",
        encoding="utf-8",
    )
    memory = store.register_artifact(
        ArtifactRegisterRequest(
            type=ArtifactType.TEXT_MEMORY,
            name="parser-memory",
            uri=memory_path.as_uri(),
            manifest={"content_path": memory_path.name},
            compatibility={"task_tags": ["parser"]},
            scores={"quality": 0.8},
            promoted=True,
        )
    )

    skill_path = payload_root / "parser-skill"
    skill_path.mkdir()
    (skill_path / "SKILL.md").write_text("# Parse safely\n", encoding="utf-8")
    (skill_path / "reference.txt").write_text("parser reference", encoding="utf-8")
    skill = store.register_artifact(
        ArtifactRegisterRequest(
            type=ArtifactType.SKILL_BUNDLE,
            name="parser-skill",
            uri=skill_path.as_uri(),
            compatibility={"task_tags": ["parser"]},
            scores={"quality": 0.7},
            promoted=True,
        )
    )

    agent_path = payload_root / "agent-system" / "AGENTS.md"
    agent_path.parent.mkdir()
    agent_path.write_text(
        "\n\nApply the evolved parser procedure.\n",
        encoding="utf-8",
    )
    agent_system = store.register_artifact(
        ArtifactRegisterRequest(
            type=ArtifactType.AGENT_SYSTEM,
            name="parser-agent-system",
            uri=agent_path.as_uri(),
            manifest={"content_path": "AGENTS.md", "target_path": "AGENTS.md"},
            compatibility={"task_tags": ["parser"]},
            scores={"quality": 0.9},
            promoted=True,
        )
    )

    adapter_bytes = b"verified-adapter-weights"
    adapter_path = payload_root / "parser-adapter"
    adapter_path.mkdir()
    (adapter_path / "adapter.bin").write_bytes(adapter_bytes)
    adapter = store.register_artifact(
        ArtifactRegisterRequest(
            type=ArtifactType.PARAMETRIC_MEMORY,
            name="parser-adapter",
            uri=adapter_path.as_uri(),
            manifest={
                "adapter_id": "parser-adapter",
                "adapter_format": "lora",
                "base_model": "model-a",
            },
            compatibility={"task_tags": ["parser"], "base_model": ["model-a"]},
            scores={"quality": 0.6},
            promoted=True,
        )
    )

    request_payload = _request().model_dump(mode="json")
    request_payload["base_model"] = "model-a"
    request_payload["execution_profile"]["runtime_capabilities"] = ["adapter_serving"]
    response = store.resolve_materialized_context(
        ContextProjectionResolveRequest.model_validate(request_payload)
    )

    assert response.selection.artifact_ids == (
        agent_system.artifact_id,
        memory.artifact_id,
        skill.artifact_id,
        adapter.artifact_id,
    )
    assert response.instruction == (
        "Use the following evolved agent system instructions for this task:\n"
        "Apply the evolved parser procedure.\n\n"
        "Use the following long-term memory for this task:\n"
        "Remember the verified parser invariant."
    )
    blobs_by_destination = {
        (item.destination_scope.value, item.destination_relative_path): item
        for item in response.blobs
    }
    assert set(blobs_by_destination) == {
        ("target_data", "agent_system.md"),
        ("harness_instruction", "AGENTS.md"),
        ("target_data", "memory.md"),
        ("harness_skills", f"{skill.artifact_id}/SKILL.md"),
        ("harness_skills", f"{skill.artifact_id}/reference.txt"),
    }
    skill_blob = blobs_by_destination[("harness_skills", f"{skill.artifact_id}/SKILL.md")]
    with store.open_materialized_blob(response.context_id, skill_blob.blob_id) as lease:
        assert lease.stream.read().decode("utf-8") == "# Parse safely\n"
    memory_blob = blobs_by_destination[("target_data", "memory.md")]
    with store.open_materialized_blob(response.context_id, memory_blob.blob_id) as lease:
        assert (
            lease.stream.read().decode("utf-8") == "\nRemember the verified parser invariant.\n\n"
        )
    agent_blob = blobs_by_destination[("target_data", "agent_system.md")]
    with store.open_materialized_blob(response.context_id, agent_blob.blob_id) as lease:
        assert lease.stream.read().decode("utf-8") == "\n\nApply the evolved parser procedure.\n"
    assert {item.name: item.value for item in response.environment} == {
        "OPENEVO_AGENT_SYSTEM_FILE": "/openevo/session/evolution/agent_system.md",
        "OPENEVO_AGENT_SYSTEM_TARGET": "/workspace/repository/AGENTS.md",
        "OPENEVO_AGENT_SYSTEM_TARGETS": '["/workspace/repository/AGENTS.md"]',
        "OPENEVO_AGENTS_MD": "/workspace/repository/AGENTS.md",
        "OPENEVO_MEMORY_FILE": "/openevo/session/evolution/memory.md",
        "OPENEVO_SKILLS_DIR": "/openevo/session/evolution/skills",
    }
    assert response.adapter_merge_spec.merge_mode == "runtime_lora"
    assert response.adapter_merge_spec.base_model == "model-a"
    assert len(response.adapter_merge_spec.adapters) == 1
    materialized_adapter = response.adapter_merge_spec.adapters[0]
    assert materialized_adapter.source_artifact_id == adapter.artifact_id
    assert materialized_adapter.adapter_id == "parser-adapter"
    assert materialized_adapter.adapter_format == "lora"
    assert materialized_adapter.base_model == "model-a"
    assert materialized_adapter.source_size_bytes == len(adapter_bytes)
    assert len(materialized_adapter.source_payload_digest) == 64


def test_store_blob_reader_rejects_self_signed_disk_manifest(tmp_path: Path) -> None:
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
    payload.write_text("trusted", encoding="utf-8")
    store.register_artifact(
        ArtifactRegisterRequest(
            type=ArtifactType.TEXT_MEMORY,
            name="memory",
            uri=payload.as_uri(),
            manifest={"content_path": "memory.md"},
            promoted=True,
        )
    )
    response = store.resolve_materialized_context(_request())
    blob = response.blobs[0]
    forged = b"forged"
    blob_path = (
        store.files.context_materialization_dir(response.context_id) / "blobs" / blob.blob_id
    )
    blob_path.write_bytes(forged)
    forged_blob = blob.model_copy(
        update={
            "size_bytes": len(forged),
            "sha256": hashlib.sha256(forged).hexdigest(),
        }
    )
    manifest_path = store.files.context_materialization_dir(response.context_id) / "manifest.json"
    manifest_path.write_text(
        json.dumps(
            response.model_copy(update={"blobs": (forged_blob,)}).model_dump(mode="json"),
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="persisted manifest"):
        with store.open_materialized_blob(response.context_id, blob.blob_id):
            pass


def test_store_blob_reader_rejects_replaced_materialization_root(tmp_path: Path) -> None:
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
    payload.write_text("trusted", encoding="utf-8")
    store.register_artifact(
        ArtifactRegisterRequest(
            type=ArtifactType.TEXT_MEMORY,
            name="memory",
            uri=payload.as_uri(),
            manifest={"content_path": "memory.md"},
            promoted=True,
        )
    )
    response = store.resolve_materialized_context(_request())
    root = artifact_root / "context_materializations"
    original = tmp_path / "original-context-materializations"
    os.replace(root, original)
    shutil.copytree(original, root)
    root.chmod(0o777)

    with pytest.raises(ValueError, match="root identity|not private|binding changed"):
        with store.open_materialized_blob(
            response.context_id,
            response.blobs[0].blob_id,
        ):
            pass


def test_store_projection_requires_executable_registry(tmp_path: Path) -> None:
    store = EvolutionStore(
        db_path=tmp_path / "evolution.db",
        artifact_root=tmp_path / "managed",
    )
    store.initialize()

    with pytest.raises(ValueError, match="verified executable registry"):
        store.resolve_context_projections(_request())

    with pytest.raises(ValueError, match="verified executable registry"):
        store.resolve_materialized_context(_request())


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
    identifiers = iter(("ctx-db-collision", "ctx-path-collision", "ctx-after-collisions"))
    monkeypatch.setattr(store_module, "new_id", lambda _prefix: next(identifiers))

    response = store.resolve_context_projections(_request())

    assert response.context_id == "ctx-after-collisions"
    assert stale_snapshot.read_text(encoding="utf-8") == "stale"


def test_materialized_context_retries_collisions_and_removes_unpersisted_bundle(
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
    identifiers = iter(("ctx-db-collision", "ctx-after-collision"))
    monkeypatch.setattr(store_module, "new_id", lambda _prefix: next(identifiers))

    response = store.resolve_materialized_context(_request())

    assert response.context_id == "ctx-after-collision"
    assert not store.files.context_materialization_dir("ctx-db-collision").exists()
    assert store.files.context_materialization_dir(response.context_id).is_dir()


def test_materialized_context_rolls_back_bundle_and_database_on_snapshot_failure(
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
    monkeypatch.setattr(store_module, "new_id", lambda _prefix: "ctx-write-failure")

    def fail_write(*_args, **_kwargs):
        raise OSError("injected materialized context snapshot failure")

    monkeypatch.setattr(store_module, "write_context_snapshot", fail_write)
    with pytest.raises(OSError, match="snapshot failure"):
        store.resolve_materialized_context(_request())

    assert not store.files.context_materialization_dir("ctx-write-failure").exists()
    with store.connect() as connection:
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM contexts WHERE context_id = ?",
                ("ctx-write-failure",),
            ).fetchone()[0]
            == 0
        )
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM context_materializations WHERE context_id = ?",
                ("ctx-write-failure",),
            ).fetchone()[0]
            == 0
        )


def test_initialize_reconciles_orphan_materializations_without_following_symlinks(
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
    referenced = store.resolve_materialized_context(_request())
    root = artifact_root / "context_materializations"
    orphan = root / "ctx-orphan"
    orphan.mkdir()
    (orphan / "partial").write_text("orphan", encoding="utf-8")
    temporary = root / ".ctx-crashed.random"
    temporary.mkdir()
    (temporary / "partial").write_text("temporary", encoding="utf-8")
    outside = tmp_path / "outside"
    outside.mkdir()
    sentinel = outside / "keep.txt"
    sentinel.write_text("keep", encoding="utf-8")
    link = root / "ctx-orphan-link"
    link.symlink_to(outside, target_is_directory=True)

    restarted = EvolutionStore(
        db_path=tmp_path / "evolution.db",
        artifact_root=artifact_root,
        executable_registry=registry,
    )
    restarted.initialize()

    assert restarted.files.context_materialization_dir(referenced.context_id).is_dir()
    assert not orphan.exists()
    assert not temporary.exists()
    assert not link.exists()
    assert sentinel.read_text(encoding="utf-8") == "keep"


def test_orphan_cleanup_does_not_delete_a_referenced_replacement(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry = verified_builtin_registry(tmp_path / "registry")
    artifact_root = tmp_path / "managed"
    db_path = tmp_path / "evolution.db"
    store = EvolutionStore(
        db_path=db_path,
        artifact_root=artifact_root,
        executable_registry=registry,
    )
    store.initialize()
    referenced = store.resolve_materialized_context(_request())
    root = artifact_root / "context_materializations"
    bundle = root / referenced.context_id
    orphan = root / "ctx-orphan-race"
    orphan.mkdir()
    (orphan / "partial").write_text("orphan", encoding="utf-8")
    original_remove = store._remove_orphan_context_materializations

    def replace_before_cleanup(candidates, root_descriptor: int) -> None:
        shutil.rmtree(orphan)
        bundle.rename(orphan)
        original_remove(candidates, root_descriptor)

    monkeypatch.setattr(
        store,
        "_remove_orphan_context_materializations",
        replace_before_cleanup,
    )
    with pytest.raises(ValueError, match="changed|identity|preserved"):
        store.initialize()

    preserved = list(root.glob(".openevo-preserved-*"))
    assert len(preserved) == 1
    assert (preserved[0] / "manifest.json").is_file()
    with store.connect() as connection:
        assert connection.execute(
            "SELECT 1 FROM context_materializations WHERE context_id = ?",
            (referenced.context_id,),
        ).fetchone()
    monkeypatch.undo()
    with pytest.raises(ValueError, match="preserved materialization"):
        EvolutionStore(
            db_path=db_path,
            artifact_root=artifact_root,
            executable_registry=registry,
        ).initialize()


def test_initialize_waits_for_materialization_publication_and_database_commit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry = verified_builtin_registry(tmp_path / "registry")
    artifact_root = tmp_path / "managed"
    db_path = tmp_path / "evolution.db"
    store = EvolutionStore(
        db_path=db_path,
        artifact_root=artifact_root,
        executable_registry=registry,
    )
    store.initialize()
    materializer = store._context_materializer
    assert materializer is not None
    original_materialize = materializer.materialize_for_publication
    materialization_published = threading.Event()
    allow_database_commit = threading.Event()

    def pause_after_publish(*args, **kwargs):
        result = original_materialize(*args, **kwargs)
        materialization_published.set()
        if not allow_database_commit.wait(timeout=5):
            raise RuntimeError("timed out waiting to continue context persistence")
        return result

    monkeypatch.setattr(
        materializer,
        "materialize_for_publication",
        pause_after_publish,
    )
    resolved: list[object] = []
    failures: list[BaseException] = []

    def resolve() -> None:
        try:
            resolved.append(store.resolve_materialized_context(_request()))
        except BaseException as exc:
            failures.append(exc)

    resolve_thread = threading.Thread(target=resolve)
    resolve_thread.start()
    assert materialization_published.wait(timeout=5)

    restarted = EvolutionStore(
        db_path=db_path,
        artifact_root=artifact_root,
        executable_registry=registry,
    )
    initialize_finished = threading.Event()

    def initialize() -> None:
        try:
            restarted.initialize()
        except BaseException as exc:
            failures.append(exc)
        finally:
            initialize_finished.set()

    initialize_thread = threading.Thread(target=initialize)
    initialize_thread.start()
    assert not initialize_finished.wait(timeout=0.2)

    allow_database_commit.set()
    resolve_thread.join(timeout=5)
    initialize_thread.join(timeout=5)
    assert not resolve_thread.is_alive()
    assert not initialize_thread.is_alive()
    assert failures == []
    assert len(resolved) == 1
    response = resolved[0]
    assert hasattr(response, "context_id")
    assert restarted.files.context_materialization_dir(response.context_id).is_dir()


def test_store_identity_prevents_other_database_from_cleaning_shared_root(
    tmp_path: Path,
) -> None:
    registry = verified_builtin_registry(tmp_path / "registry")
    artifact_root = tmp_path / "managed"
    primary_db = tmp_path / "primary.db"
    store = EvolutionStore(
        db_path=primary_db,
        artifact_root=artifact_root,
        executable_registry=registry,
    )
    store.initialize()
    context = store.resolve_materialized_context(_request())
    bundle = store.files.context_materialization_dir(context.context_id)
    assert bundle.is_dir()

    process_context = multiprocessing.get_context("spawn")
    result_queue = process_context.Queue()
    process = process_context.Process(
        target=_initialize_store_in_process,
        args=(
            str(tmp_path / "other.db"),
            str(artifact_root),
            result_queue,
        ),
    )
    process.start()
    process.join(timeout=15)
    if process.is_alive():
        process.terminate()
        process.join(timeout=5)
        pytest.fail("secondary store initialization did not terminate")

    assert process.exitcode == 0
    status, message = result_queue.get(timeout=5)
    assert status == "error"
    assert "artifact root" in message and "database" in message
    assert bundle.is_dir()
    restarted = EvolutionStore(
        db_path=primary_db,
        artifact_root=artifact_root,
        executable_registry=registry,
    )
    restarted.initialize()
    assert bundle.is_dir()


def test_rejected_shared_root_does_not_poison_secondary_database(tmp_path: Path) -> None:
    shared_root = tmp_path / "shared-root"
    EvolutionStore(
        db_path=tmp_path / "primary.db",
        artifact_root=shared_root,
    ).initialize()
    secondary_db = tmp_path / "secondary.db"

    with pytest.raises(ValueError, match="already bound"):
        EvolutionStore(
            db_path=secondary_db,
            artifact_root=shared_root,
        ).initialize()

    own_root = tmp_path / "secondary-root"
    EvolutionStore(
        db_path=secondary_db,
        artifact_root=own_root,
    ).initialize()
    assert (own_root / ".openevo-store.json").is_file()


def test_fresh_database_rejects_unclaimed_managed_materialization_state(
    tmp_path: Path,
) -> None:
    artifact_root = tmp_path / "managed"
    victim = artifact_root / "context_materializations" / "ctx-victim"
    victim.mkdir(parents=True)
    sentinel = victim / "keep.txt"
    sentinel.write_text("keep", encoding="utf-8")
    db_path = tmp_path / "fresh.db"

    with pytest.raises(ValueError, match="managed|unclaimed|non-empty"):
        EvolutionStore(db_path=db_path, artifact_root=artifact_root).initialize()

    assert sentinel.read_text(encoding="utf-8") == "keep"
    with sqlite3.connect(db_path) as connection:
        table = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'store_identity'"
        ).fetchone()
    assert table is None


def test_initialize_enforces_private_materialization_root_mode(tmp_path: Path) -> None:
    artifact_root = tmp_path / "managed"
    materialization_root = artifact_root / "context_materializations"
    materialization_root.mkdir(parents=True, mode=0o777)
    materialization_root.chmod(0o777)

    EvolutionStore(
        db_path=tmp_path / "evolution.db",
        artifact_root=artifact_root,
    ).initialize()

    assert stat.S_IMODE(materialization_root.stat().st_mode) == 0o700


def test_store_identity_schema_and_pending_row_are_created_atomically(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db_path = tmp_path / "evolution.db"
    artifact_root = tmp_path / "managed"

    def fail_after_schema(_connection: sqlite3.Connection) -> None:
        raise RuntimeError("injected identity bootstrap failure")

    with monkeypatch.context() as patch:
        patch.setattr(
            store_module,
            "_after_store_identity_schema_created",
            fail_after_schema,
        )
        with pytest.raises(RuntimeError, match="injected identity bootstrap failure"):
            EvolutionStore(
                db_path=db_path,
                artifact_root=artifact_root,
            ).initialize()

    with sqlite3.connect(db_path) as connection:
        table = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'store_identity'"
        ).fetchone()
    assert table is None

    EvolutionStore(db_path=db_path, artifact_root=artifact_root).initialize()
    with sqlite3.connect(db_path) as connection:
        row = connection.execute(
            "SELECT binding_state FROM store_identity WHERE singleton = 1"
        ).fetchone()
    assert row == ("bound",)


def test_store_identity_rejects_root_drift_and_symlink_marker(tmp_path: Path) -> None:
    db_path = tmp_path / "evolution.db"
    artifact_root = tmp_path / "managed"
    store = EvolutionStore(db_path=db_path, artifact_root=artifact_root)
    store.initialize()

    moved = EvolutionStore(
        db_path=db_path,
        artifact_root=tmp_path / "different-root",
    )
    with pytest.raises(ValueError, match="different artifact root"):
        moved.initialize()

    marker = artifact_root / ".openevo-store.json"
    marker.unlink()
    outside = tmp_path / "outside-identity.json"
    outside.write_text('{"store_id":"untrusted"}', encoding="utf-8")
    marker.symlink_to(outside)
    with pytest.raises(ValueError, match="identity.*opened safely"):
        EvolutionStore(db_path=db_path, artifact_root=artifact_root).initialize()
    assert outside.read_text(encoding="utf-8") == '{"store_id":"untrusted"}'


def test_missing_bound_store_marker_fails_before_secondary_database_cleanup(
    tmp_path: Path,
) -> None:
    registry = verified_builtin_registry(tmp_path / "registry")
    artifact_root = tmp_path / "managed"
    primary_db = tmp_path / "primary.db"
    primary = EvolutionStore(
        db_path=primary_db,
        artifact_root=artifact_root,
        executable_registry=registry,
    )
    primary.initialize()
    context = primary.resolve_materialized_context(_request())
    bundle = primary.files.context_materialization_dir(context.context_id)
    (artifact_root / ".openevo-store.json").unlink()

    with pytest.raises(
        ValueError,
        match="identity marker is missing|different evolution database",
    ):
        EvolutionStore(
            db_path=tmp_path / "secondary.db",
            artifact_root=artifact_root,
        ).initialize()
    assert bundle.is_dir()
    with pytest.raises(ValueError, match="identity marker is missing"):
        EvolutionStore(
            db_path=primary_db,
            artifact_root=artifact_root,
            executable_registry=registry,
        ).initialize()
    assert bundle.is_dir()


def test_pending_store_binding_recovers_missing_marker(tmp_path: Path) -> None:
    db_path = tmp_path / "evolution.db"
    artifact_root = tmp_path / "managed"
    store = EvolutionStore(db_path=db_path, artifact_root=artifact_root)
    store.initialize()
    with store.connect() as connection:
        connection.execute(
            "UPDATE store_identity SET binding_state = 'pending' WHERE singleton = 1"
        )
        connection.commit()
    marker = artifact_root / ".openevo-store.json"
    marker.unlink()

    EvolutionStore(db_path=db_path, artifact_root=artifact_root).initialize()

    assert marker.is_file()
    with store.connect() as connection:
        row = connection.execute(
            "SELECT binding_state FROM store_identity WHERE singleton = 1"
        ).fetchone()
    assert row["binding_state"] == "bound"


def test_store_identity_read_rejects_marker_path_replacement(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db_path = tmp_path / "evolution.db"
    artifact_root = tmp_path / "managed"
    store = EvolutionStore(db_path=db_path, artifact_root=artifact_root)
    store.initialize()
    marker = artifact_root / ".openevo-store.json"
    backup = artifact_root / ".openevo-store.backup.json"
    replacement = artifact_root / ".openevo-store.replacement.json"
    payload = json.loads(marker.read_text(encoding="utf-8"))
    payload["store_id"] = "store_0000000000000000"
    replacement.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")
    original_read = store_module.os.read
    replaced = False

    def replace_marker_after_read(descriptor: int, maximum: int) -> bytes:
        nonlocal replaced
        data = original_read(descriptor, maximum)
        if data and not replaced:
            replaced = True
            marker.rename(backup)
            replacement.rename(marker)
        return data

    monkeypatch.setattr(store_module.os, "read", replace_marker_after_read)
    with pytest.raises(ValueError, match="identity changed while being read"):
        EvolutionStore(db_path=db_path, artifact_root=artifact_root).initialize()


def test_resolve_rejects_materialization_root_rebind_after_lock(
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
    root = artifact_root / "context_materializations"
    moved = artifact_root / "context_materializations-locked"
    replacement = artifact_root / "context_materializations-replacement"
    materializer = store._context_materializer
    assert materializer is not None
    original_materialize = materializer.materialize_for_publication

    def replace_root_then_materialize(*args, **kwargs):
        root.rename(moved)
        replacement.mkdir(mode=0o700)
        shutil.copyfile(
            moved / ".openevo-store.json",
            replacement / ".openevo-store.json",
        )
        replacement.rename(root)
        return original_materialize(*args, **kwargs)

    monkeypatch.setattr(
        materializer,
        "materialize_for_publication",
        replace_root_then_materialize,
    )
    try:
        with pytest.raises(ValueError, match="materialization root|identity|binding"):
            store.resolve_materialized_context(_request())
    finally:
        if root.exists():
            root.rename(replacement)
        if moved.exists():
            moved.rename(root)

    with store.connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM contexts").fetchone()[0] == 0


def test_exact_identityless_current_schema_rebinds_existing_materialization(
    tmp_path: Path,
) -> None:
    registry = verified_builtin_registry(tmp_path / "registry")
    db_path = tmp_path / "evolution.db"
    artifact_root = tmp_path / "managed"
    store = EvolutionStore(
        db_path=db_path,
        artifact_root=artifact_root,
        executable_registry=registry,
    )
    store.initialize()
    context = store.resolve_materialized_context(_request())
    bundle = store.files.context_materialization_dir(context.context_id)
    with store.connect() as connection:
        connection.execute("DROP TABLE store_identity")
        connection.commit()
    (artifact_root / ".openevo-store.json").unlink()
    (artifact_root / "context_materializations" / ".openevo-store.json").unlink()

    recovered = EvolutionStore(
        db_path=db_path,
        artifact_root=artifact_root,
        executable_registry=registry,
    )
    recovered.initialize()

    assert bundle.is_dir()
    assert (artifact_root / ".openevo-store.json").is_file()
    assert (artifact_root / "context_materializations" / ".openevo-store.json").is_file()
    with sqlite3.connect(db_path) as connection:
        assert (
            connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'store_identity'"
            ).fetchone()
            is not None
        )


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
    original_write = store_module.write_context_snapshot

    def fail_write(*_args, **_kwargs):
        raise OSError("injected context snapshot failure")

    monkeypatch.setattr(store_module, "new_id", lambda _prefix: "ctx-write-failure")
    monkeypatch.setattr(store_module, "write_context_snapshot", fail_write)
    with pytest.raises(OSError, match="snapshot failure"):
        store.resolve_context_projections(_request())
    with store.connect() as connection:
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM contexts WHERE context_id = ?",
                ("ctx-write-failure",),
            ).fetchone()[0]
            == 0
        )

    monkeypatch.setattr(store_module, "write_context_snapshot", original_write)
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
    request_payload["metadata"]["evolution"] = {"context_artifact_ids": [artifacts[0].artifact_id]}

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
        store.files.context_snapshot_path(response.context_id).read_text(encoding="utf-8")
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
    compatibility: dict[str, object] = {"padding": "x" * MAX_ARTIFACT_ROUTING_JSON_BYTES}
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
    invalid_row = next(row for row in rows if row["artifact_id"] == artifacts[0].artifact_id)
    assert invalid_row["projection_skip_reason"] == "metadata_policy_rejected"
    if column == "compatibility_json":
        assert invalid_row["compatibility_json"] is None
    else:
        assert json.loads(invalid_row["compatibility_json"]) == {"task_tags": ["parser"]}
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
    assert response.selection.skipped_artifacts[0].reason == ("metadata_policy_rejected")


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
    assert response.selection.skipped_artifacts[0].reason == ("unbound_legacy_metadata")


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
    assert store.get_artifact(artifact_id).manifest == {"content_path": "legacy.md"}
    response = store.resolve_context_projections(_request())
    assert response.selection.artifact_ids == ()
    assert response.selection.skipped_artifact_ids == (artifact_id,)
    assert response.selection.skipped_artifacts[0].reason == ("unbound_legacy_metadata")
