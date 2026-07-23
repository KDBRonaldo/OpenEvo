from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import sqlite3
import threading
import tracemalloc
from pathlib import Path
from typing import Iterator

import pytest
from pydantic import ValidationError

from openevo.evolution import store as store_module
from openevo.evolution.context_materialization import MaterializedContext
from openevo.evolution.context_projection import ContextProjectionResolveRequest
from openevo.evolution.framework import (
    EvolutionExecutionProfile,
    RuntimeDestinationRoots,
    canonical_digest,
    canonical_json,
)
from openevo.evolution.revisions import (
    AdmissionStatus,
    AtomicSuccessorCommitV2,
    AtomicSuccessorManifestV2,
    ContentAddressedSnapshotRef,
    ExecutionModelIdentity,
    ExecutionRuntimeIdentity,
    ExecutionServingIdentity,
    ExecutionSnapshotV1,
    ExecutionTaskNetworkPolicy,
    ModelIdentitySource,
    RevisionCapacityError,
    RevisionConflictError,
    RevisionContextIdentity,
    RevisionIntegrityError,
    RevisionManifestV1,
    TaskAdmissionConflictError,
    TaskAdmissionIntent,
    TaskAdmissionRecord,
    TaskAdmissionRequest,
    TaskExecutionEnvelopeV1,
    VerifiedExecutionSnapshot,
    admission_id_for_request,
    atomic_successor_manifest_sha256,
    bind_task_admission,
    content_addressed_snapshot_ref,
    execution_snapshot_id_for_snapshot,
    execution_task_network_policy_digest,
    revision_id_for_manifest,
)
from openevo.evolution.store import EvolutionStore
from tests.framework_testkit import verified_builtin_registry
from tests.revision_testkit import verified_execution_snapshot_for_test


def _atomic_successor_manifest(**changes: object) -> AtomicSuccessorManifestV2:
    payload: dict[str, object] = {
        "project_id": "project",
        "successor_transition_id": "successor-1",
        "task_id": "task-1",
        "task_admission_id": "admission-1",
        "admission_sha256": "1" * 64,
        "accepted_attempt_id": "attempt-1",
        "predecessor_project_head_id": "head-0",
        "predecessor_generation": 0,
        "predecessor_manifest_sha256": "2" * 64,
        "successor_project_head_id": "head-1",
        "successor_generation": 1,
        "successor_manifest_sha256": "3" * 64,
        "workspace_snapshot_id": "workspace-1",
        "workspace_manifest_sha256": "4" * 64,
        "evolution_revision_id": "evolution-1",
        "evolution_revision_manifest_sha256": "5" * 64,
        "runtime_context_snapshot_id": "runtime-context-1",
        "runtime_context_manifest_sha256": "6" * 64,
        "effective_execution_snapshot_id": "execution-1",
        "effective_execution_snapshot_sha256": "7" * 64,
        "registry_sha256": "8" * 64,
        "normalized_evolution_intent_sha256": "9" * 64,
        "dataset_id": "dataset-1",
        "dataset_artifact_id": "artifact-dataset",
        "dataset_manifest_sha256": "a" * 64,
        "materialized_context_id": "materialized-context-1",
        "materialized_context_manifest_sha256": "b" * 64,
        "method_artifact_ids": ("artifact-memory",),
    }
    payload.update(changes)
    return AtomicSuccessorManifestV2.model_validate(payload)


def test_atomic_successor_receipt_is_adjacent_closed_and_content_addressed() -> None:
    manifest = _atomic_successor_manifest()
    digest = atomic_successor_manifest_sha256(manifest)

    assert AtomicSuccessorCommitV2(
        manifest_sha256=digest,
        manifest=manifest,
    ).manifest == manifest
    with pytest.raises(ValidationError, match="adjacent"):
        _atomic_successor_manifest(successor_generation=2)
    with pytest.raises(ValidationError, match="digest"):
        AtomicSuccessorCommitV2(
            manifest_sha256="f" * 64,
            manifest=manifest,
        )
    with pytest.raises(ValidationError, match="extra"):
        AtomicSuccessorManifestV2.model_validate(
            {**manifest.model_dump(mode="python"), "host_path": "/private/state"}
        )


def _projection_request(*, subscription: bool = False) -> ContextProjectionResolveRequest:
    return ContextProjectionResolveRequest(
        task_id="task-revision-fixture",
        instruction="Continue the scientific task.",
        agent={"harness": "codex"},
        base_model="codex" if subscription else "Qwen/Qwen3-Coder-30B-A3B-Instruct",
        execution_profile=EvolutionExecutionProfile(
            execution_mode="subscription" if subscription else "self_deployed",
            capture_mode="transcript" if subscription else "token_level",
            harness_id="codex",
        ),
        destination_roots=RuntimeDestinationRoots(
            target_data="/openevo/session/evolution",
            harness_skills="/openevo/session/evolution/skills",
            harness_instruction="/workspace/repository",
        ),
    )


def _ref(kind: str, marker: str) -> ContentAddressedSnapshotRef:
    return content_addressed_snapshot_ref(kind, canonical_digest({"marker": marker}))


def _execution_snapshot(
    *,
    subscription: bool = False,
    model_revision: str = "model-commit-0123456789abcdef",
    token_limit: int = 32_768,
) -> ExecutionSnapshotV1:
    network_policy_id = "openevo.task-network.v1"
    return ExecutionSnapshotV1(
        execution_mode="subscription" if subscription else "self_deployed",
        capture_mode="transcript" if subscription else "token_level",
        token_level_metrics_available=not subscription,
        model=ExecutionModelIdentity(
            source=(
                ModelIdentitySource.SUBSCRIPTION
                if subscription
                else ModelIdentitySource.HUGGING_FACE
            ),
            model_id="codex" if subscription else "Qwen/Qwen3-Coder-30B-A3B-Instruct",
            model_revision="subscription-managed" if subscription else model_revision,
            token_limit=token_limit,
        ),
        runtime=ExecutionRuntimeIdentity(
            kind="subscription_client" if subscription else "container",
            harness_id="codex",
            harness_version="0.144.1" if subscription else "test-harness-v1",
            image_digest="c" * 64,
            policy_id=(
                "openevo.codex-subscription-credential-isolation.v1"
                if subscription
                else "openevo.self-deployed-test-policy.v1"
            ),
            policy_digest="d" * 64,
            snapshot=_ref("runtime", "subscription" if subscription else "science-image"),
        ),
        serving=ExecutionServingIdentity(
            kind="subscription" if subscription else "managed_deployment",
            deployment_id="codex-subscription" if subscription else "vllm-primary",
            snapshot=_ref("deployment", "subscription" if subscription else "vllm-primary"),
        ),
        task_network=ExecutionTaskNetworkPolicy(
            policy_id=network_policy_id,
            allow_internet=True,
            policy_digest=execution_task_network_policy_digest(
                policy_id=network_policy_id,
                allow_internet=True,
            ),
        ),
    )


def _initialized_store(
    tmp_path: Path,
    *,
    subscription: bool = False,
) -> tuple[EvolutionStore, MaterializedContext, ExecutionSnapshotV1]:
    store = EvolutionStore(
        db_path=tmp_path / "evolution.db",
        artifact_root=tmp_path / "managed",
        executable_registry=verified_builtin_registry(tmp_path / "registry"),
    )
    store.initialize()
    context = store.resolve_materialized_context(_projection_request(subscription=subscription))
    snapshot = _execution_snapshot(subscription=subscription)
    store.register_execution_snapshot(verified_execution_snapshot_for_test(snapshot))
    return store, context, snapshot


def _manifest(
    context: MaterializedContext,
    snapshot: ExecutionSnapshotV1,
    *,
    stream_id: str = "science-project",
    generation: int = 0,
    predecessor_revision_id: str | None = None,
    project_marker: str = "project-v1",
    workspace_marker: str = "workspace-v1",
) -> RevisionManifestV1:
    return RevisionManifestV1(
        stream_id=stream_id,
        generation=generation,
        predecessor_revision_id=predecessor_revision_id,
        project_snapshot=_ref("project", project_marker),
        workspace_snapshot=_ref("workspace", workspace_marker),
        context=RevisionContextIdentity(
            context_id=context.context_id,
            manifest_digest=canonical_digest(context),
            registry_digest=context.registry_digest,
            request_digest=context.request_digest,
            artifact_ids=context.selection.artifact_ids,
        ),
        execution_snapshot_id=execution_snapshot_id_for_snapshot(snapshot),
        execution_snapshot_digest=canonical_digest(snapshot),
        execution_snapshot=snapshot,
        adapters=context.adapter_merge_spec.adapters,
    )


def _intent(
    *,
    stream_id: str = "science-project",
    task_id: str = "task-1",
    generation: int = 0,
    idempotency_key: str = "request-1",
) -> TaskAdmissionIntent:
    return TaskAdmissionIntent(
        stream_id=stream_id,
        task_id=task_id,
        required_generation=generation,
        idempotency_key=idempotency_key,
    )


def _envelope(
    context: MaterializedContext,
    snapshot: ExecutionSnapshotV1,
    *,
    project_id: str = "science-project",
    task_id: str = "task-1",
    project_marker: str = "project-v1",
    workspace_marker: str = "workspace-v1",
    task_marker: str = "task-v1",
    context_id: str | None = None,
    context_artifact_ids: tuple[str, ...] | None = None,
) -> TaskExecutionEnvelopeV1:
    return TaskExecutionEnvelopeV1(
        project_id=project_id,
        project_snapshot=_ref("project", project_marker),
        workspace_snapshot=_ref("workspace", workspace_marker),
        task_id=task_id,
        task_snapshot=_ref("task", task_marker),
        execution_mode=snapshot.execution_mode,
        capture_mode=snapshot.capture_mode,
        execution_snapshot_id=execution_snapshot_id_for_snapshot(snapshot),
        context_id=context.context_id if context_id is None else context_id,
        context_artifact_ids=(
            context.selection.artifact_ids
            if context_artifact_ids is None
            else context_artifact_ids
        ),
        artifact_families=("text_memory", "skill_bundle", "agent_system"),
        method_ids=(
            "text_memory_expel_reflector",
            "skill_bundle_reflector",
            "agent_system_gepa_reflector",
        ),
    )


def _admit(
    store: EvolutionStore,
    context: MaterializedContext,
    snapshot: ExecutionSnapshotV1,
    intent: TaskAdmissionIntent | None = None,
    envelope: TaskExecutionEnvelopeV1 | None = None,
) -> TaskAdmissionRecord:
    intent = intent or _intent()
    return store.admit_task(
        intent,
        envelope or _envelope(context, snapshot, task_id=intent.task_id),
    )


def _restart(tmp_path: Path) -> EvolutionStore:
    store = EvolutionStore(
        db_path=tmp_path / "evolution.db",
        artifact_root=tmp_path / "managed",
        executable_registry=verified_builtin_registry(tmp_path / "restart-registry"),
    )
    store.initialize()
    return store


def test_snapshot_refs_and_envelope_are_closed_and_secret_free() -> None:
    context = MaterializedContext.model_construct(
        context_id="ctx-fixture",
        request_digest="a" * 64,
        registry_digest="b" * 64,
        selection=type("Selection", (), {"artifact_ids": ()})(),
    )
    snapshot = _execution_snapshot()
    payload = _envelope(context, snapshot).model_dump(mode="json")

    for field in ("runtime", "model", "env", "instruction", "setup_command", "GITHUB_PAT"):
        with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
            TaskExecutionEnvelopeV1.model_validate({**payload, field: "secret"})
    with pytest.raises(ValidationError, match="snapshot ID"):
        ContentAddressedSnapshotRef(
            kind="workspace",
            snapshot_id="workspace-snapshot-caller-name",
            content_digest="a" * 64,
        )


def test_token_limit_is_identity_data_not_a_secret_heuristic() -> None:
    first = _execution_snapshot(token_limit=32_768)
    second = _execution_snapshot(token_limit=65_536)

    assert canonical_digest(first) != canonical_digest(second)
    assert execution_snapshot_id_for_snapshot(first) != execution_snapshot_id_for_snapshot(second)
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        ExecutionSnapshotV1.model_validate(
            first.model_dump(mode="json") | {"GITHUB_PAT": "must-not-enter-contract"}
        )


def test_execution_snapshot_independently_validates_metrics_network_and_no_endpoint() -> None:
    subscription = _execution_snapshot(subscription=True).model_dump(mode="json")
    subscription["token_level_metrics_available"] = True
    with pytest.raises(ValidationError, match="token-level metrics"):
        ExecutionSnapshotV1.model_validate(subscription)

    network = _execution_snapshot().task_network.model_dump(mode="json")
    network["policy_digest"] = "a" * 64
    with pytest.raises(ValidationError, match="policy digest is inconsistent"):
        ExecutionTaskNetworkPolicy.model_validate(network)

    serving = _execution_snapshot(subscription=True).serving.model_dump(mode="json")
    serving["endpoint"] = "https://example.invalid/v1"
    with pytest.raises(ValidationError):
        ExecutionServingIdentity.model_validate(serving)


@pytest.mark.parametrize("invalid", [True, "0", 0.0])
def test_revision_and_admission_integers_do_not_coerce(invalid: object) -> None:
    intent = _intent().model_dump(mode="json")
    intent["required_generation"] = invalid
    with pytest.raises(ValidationError, match="without coercion"):
        TaskAdmissionIntent.model_validate(intent)

    snapshot = _execution_snapshot().model_dump(mode="json")
    snapshot["model"]["token_limit"] = invalid
    with pytest.raises(ValidationError, match="without coercion"):
        ExecutionSnapshotV1.model_validate(snapshot)


@pytest.mark.parametrize(
    ("source", "model_id", "model_revision"),
    [
        ("hugging_face", "/home/user/private-model", "0123456789abcdef"),
        ("managed_snapshot", "file:///srv/model", "snapshot-one"),
        ("subscription", "https://user:token@example.invalid/model", "managed"),
        ("managed_snapshot", "snapshot-one", "../private-model"),
        ("hugging_face", "Qwen/model", "revision?token=secret"),
    ],
)
def test_model_identity_never_persists_a_path_or_uri(
    source: str,
    model_id: str,
    model_revision: str,
) -> None:
    with pytest.raises(ValidationError, match="path or URI"):
        ExecutionModelIdentity(
            source=source,
            model_id=model_id,
            model_revision=model_revision,
            token_limit=1,
        )


def test_execution_snapshot_registration_is_content_addressed_and_idempotent(
    tmp_path: Path,
) -> None:
    store, _context, snapshot = _initialized_store(tmp_path)
    verified = verified_execution_snapshot_for_test(snapshot)
    first = store.register_execution_snapshot(verified)
    second = store.register_execution_snapshot(verified)

    assert first == second
    assert first.execution_snapshot_id == execution_snapshot_id_for_snapshot(snapshot)
    assert first.snapshot_digest == canonical_digest(snapshot)
    assert first.snapshot == snapshot
    assert first.producer_id == "repo-testkit"


def test_execution_snapshot_registration_rejects_raw_caller_observations() -> None:
    snapshot = _execution_snapshot()

    with pytest.raises(TypeError, match="verified producer"):
        VerifiedExecutionSnapshot(snapshot=snapshot, producer_id="caller")


def test_store_rejects_unsealed_execution_snapshot(tmp_path: Path) -> None:
    store = EvolutionStore(
        db_path=tmp_path / "evolution.db",
        artifact_root=tmp_path / "managed",
        executable_registry=verified_builtin_registry(tmp_path / "registry"),
    )
    store.initialize()

    with pytest.raises(TypeError, match="verified producer"):
        store.register_execution_snapshot(_execution_snapshot())


def test_genesis_requires_a_registered_exact_execution_snapshot(tmp_path: Path) -> None:
    store, context, registered = _initialized_store(tmp_path)
    unregistered = _execution_snapshot(model_revision="different-model-revision")

    with pytest.raises(RevisionIntegrityError, match="execution snapshot"):
        store.create_genesis_revision(_manifest(context, unregistered))
    created = store.create_genesis_revision(_manifest(context, registered))
    assert created == store.create_genesis_revision(_manifest(context, registered))


def test_revision_rejects_a_caller_supplied_execution_snapshot_digest(
    tmp_path: Path,
) -> None:
    _store, context, snapshot = _initialized_store(tmp_path)
    payload = _manifest(context, snapshot).model_dump(mode="json")
    payload["execution_snapshot_digest"] = "a" * 64

    with pytest.raises(ValidationError, match="execution snapshot identity"):
        RevisionManifestV1.model_validate(payload)


def test_execution_snapshot_row_tamper_is_rejected_on_restart(tmp_path: Path) -> None:
    store, context, snapshot = _initialized_store(tmp_path)
    store.create_genesis_revision(_manifest(context, snapshot))
    with sqlite3.connect(tmp_path / "evolution.db") as connection:
        connection.execute(
            "UPDATE execution_snapshots SET snapshot_json = '{}' WHERE execution_snapshot_id = ?",
            (execution_snapshot_id_for_snapshot(snapshot),),
        )
        connection.commit()

    with pytest.raises(RevisionIntegrityError, match="execution snapshot"):
        _restart(tmp_path)


def test_execution_snapshot_digest_column_tamper_is_rejected_on_restart(
    tmp_path: Path,
) -> None:
    store, context, snapshot = _initialized_store(tmp_path)
    store.create_genesis_revision(_manifest(context, snapshot))
    with sqlite3.connect(tmp_path / "evolution.db") as connection:
        connection.execute(
            "UPDATE execution_snapshots SET snapshot_digest = ? WHERE execution_snapshot_id = ?",
            ("a" * 64, execution_snapshot_id_for_snapshot(snapshot)),
        )
        connection.commit()

    with pytest.raises(RevisionIntegrityError, match="execution snapshot"):
        _restart(tmp_path)


def test_admission_transaction_rechecks_registered_execution_snapshot(tmp_path: Path) -> None:
    store, context, snapshot = _initialized_store(tmp_path)
    store.create_genesis_revision(_manifest(context, snapshot))
    with store.connect() as connection:
        connection.execute(
            "UPDATE execution_snapshots SET snapshot_json = '{}' WHERE execution_snapshot_id = ?",
            (execution_snapshot_id_for_snapshot(snapshot),),
        )
        connection.commit()

    with pytest.raises(RevisionIntegrityError, match="execution snapshot"):
        _admit(store, context, snapshot)


def test_genesis_binds_exact_materialization_execution_and_project(tmp_path: Path) -> None:
    store, context, snapshot = _initialized_store(tmp_path)
    manifest = _manifest(context, snapshot)
    created = store.create_genesis_revision(manifest)

    assert created.revision_id == revision_id_for_manifest(manifest)
    assert created.manifest == manifest
    with pytest.raises(RevisionConflictError, match="stream"):
        store.create_genesis_revision(
            _manifest(context, snapshot, project_marker="different-project-snapshot")
        )


@pytest.mark.parametrize(
    ("change", "error"),
    [
        ({"project_id": "other-project"}, "project"),
        ({"project_marker": "other-project"}, "project snapshot"),
        ({"workspace_marker": "other-workspace"}, "workspace snapshot"),
        ({"context_id": "ctx-other"}, "context"),
        ({"context_artifact_ids": ("art-other",)}, "artifact"),
    ],
)
def test_admission_rejects_cross_project_and_context_identity(
    tmp_path: Path,
    change: dict[str, object],
    error: str,
) -> None:
    store, context, snapshot = _initialized_store(tmp_path)
    store.create_genesis_revision(_manifest(context, snapshot))
    envelope = _envelope(context, snapshot, **change)

    with pytest.raises(TaskAdmissionConflictError, match=error):
        _admit(store, context, snapshot, envelope=envelope)


def test_admission_rejects_cross_mode_and_model_snapshot(tmp_path: Path) -> None:
    store, context, snapshot = _initialized_store(tmp_path)
    store.create_genesis_revision(_manifest(context, snapshot))
    subscription = _execution_snapshot(subscription=True)
    store.register_execution_snapshot(verified_execution_snapshot_for_test(subscription))

    with pytest.raises(TaskAdmissionConflictError, match="execution snapshot"):
        _admit(store, context, snapshot, envelope=_envelope(context, subscription))
    payload = _envelope(context, snapshot).model_dump(mode="json")
    payload["execution_mode"] = "subscription"
    payload["capture_mode"] = "transcript"
    with pytest.raises(TaskAdmissionConflictError, match="execution mode"):
        _admit(
            store,
            context,
            snapshot,
            envelope=TaskExecutionEnvelopeV1.model_validate(payload),
        )


def test_admission_request_persists_only_closed_identity_fields(tmp_path: Path) -> None:
    store, context, snapshot = _initialized_store(tmp_path)
    revision = store.create_genesis_revision(_manifest(context, snapshot))
    envelope = _envelope(context, snapshot)
    admitted = _admit(store, context, snapshot, envelope=envelope)

    with store.connect() as connection:
        persisted = connection.execute(
            "SELECT request_json FROM task_admissions WHERE admission_id = ?",
            (admitted.admission_id,),
        ).fetchone()[0]
    assert admitted.pinned_revision_id == revision.revision_id
    assert admitted.request.task_envelope_digest == canonical_digest(envelope)
    assert admitted.request.execution_envelope() == envelope
    assert admitted.request.execution_snapshot_id == execution_snapshot_id_for_snapshot(snapshot)
    assert "instruction" not in persisted
    assert "runtime" not in persisted
    assert "credential" not in persisted


def test_subscription_requires_transcript_and_cannot_use_adapters(tmp_path: Path) -> None:
    store, context, snapshot = _initialized_store(tmp_path, subscription=True)
    revision = store.create_genesis_revision(
        _manifest(context, snapshot, stream_id="subscription-project")
    )
    intent = _intent(stream_id="subscription-project", task_id="subscription-task")
    envelope = _envelope(
        context,
        snapshot,
        project_id="subscription-project",
        task_id="subscription-task",
    )

    admitted = _admit(store, context, snapshot, intent, envelope)
    assert admitted.pinned_revision_id == revision.revision_id

    payload = snapshot.model_dump(mode="json")
    payload["capture_mode"] = "token_level"
    with pytest.raises(ValidationError, match="transcript"):
        ExecutionSnapshotV1.model_validate(payload)


def test_current_generation_admission_is_idempotent_and_concurrent(tmp_path: Path) -> None:
    store, context, snapshot = _initialized_store(tmp_path)
    revision = store.create_genesis_revision(_manifest(context, snapshot))
    intent = _intent()
    barrier = threading.Barrier(3)
    results: list[TaskAdmissionRecord] = []
    errors: list[BaseException] = []

    def admit() -> None:
        barrier.wait()
        try:
            results.append(_admit(store, context, snapshot, intent))
        except BaseException as exc:  # pragma: no cover - asserted below
            errors.append(exc)

    threads = [threading.Thread(target=admit) for _ in range(2)]
    for thread in threads:
        thread.start()
    barrier.wait()
    for thread in threads:
        thread.join(timeout=10)

    assert errors == []
    assert len(results) == 2 and results[0] == results[1]
    assert results[0].pinned_revision_id == revision.revision_id
    assert store.active_revision_lease_count(revision.revision_id) == 1


def _activate_successor(
    store: EvolutionStore,
    context: MaterializedContext,
    snapshot: ExecutionSnapshotV1,
    predecessor_id: str,
    *,
    generation: int = 1,
) -> RevisionManifestV1:
    manifest = _manifest(
        context,
        snapshot,
        generation=generation,
        predecessor_revision_id=predecessor_id,
    )
    return store.activate_successor_revision(manifest).manifest


def test_successor_activation_is_atomic_idempotent_and_restart_safe(
    tmp_path: Path,
) -> None:
    store, context, snapshot = _initialized_store(tmp_path)
    genesis = store.create_genesis_revision(_manifest(context, snapshot))
    manifest = _manifest(
        context,
        snapshot,
        generation=1,
        predecessor_revision_id=genesis.revision_id,
        project_marker="project-v2",
        workspace_marker="workspace-v2",
    )

    activated = store.activate_successor_revision(manifest)
    assert activated.active is True
    assert activated.manifest == manifest
    assert store.activate_successor_revision(manifest) == activated
    assert store.get_active_revision(manifest.stream_id) == activated

    restarted = _restart(tmp_path)
    assert restarted.get_active_revision(manifest.stream_id) == activated
    assert restarted.activate_successor_revision(manifest) == activated


def test_successor_activation_preserves_existing_task_pin(tmp_path: Path) -> None:
    store, context, snapshot = _initialized_store(tmp_path)
    genesis = store.create_genesis_revision(_manifest(context, snapshot))
    admitted = _admit(store, context, snapshot, _intent())
    successor = _manifest(
        context,
        snapshot,
        generation=1,
        predecessor_revision_id=genesis.revision_id,
    )

    activated = store.activate_successor_revision(successor)

    assert activated.revision_id != genesis.revision_id
    assert store.get_task_admission(admitted.admission_id) == admitted
    assert admitted.pinned_revision_id == genesis.revision_id
    assert store.active_revision_lease_count(genesis.revision_id) == 1


def test_successor_activation_rejects_queued_request_identity_mismatch(
    tmp_path: Path,
) -> None:
    store, context, snapshot = _initialized_store(tmp_path)
    genesis = store.create_genesis_revision(_manifest(context, snapshot))
    queued = _admit(
        store,
        context,
        snapshot,
        _intent(task_id="task-next", generation=1, idempotency_key="next"),
        _envelope(context, snapshot, task_id="task-next"),
    )
    mismatched = _manifest(
        context,
        snapshot,
        generation=1,
        predecessor_revision_id=genesis.revision_id,
        workspace_marker="different-workspace",
    )

    with pytest.raises(RevisionConflictError, match="queued task admission"):
        store.activate_successor_revision(mismatched)

    assert store.get_active_revision(mismatched.stream_id) == genesis
    assert store.get_task_admission(queued.admission_id) == queued
    restarted = _restart(tmp_path)
    assert restarted.get_active_revision(mismatched.stream_id) == genesis
    assert restarted.get_task_admission(queued.admission_id) == queued


def test_unpinned_active_generation_queue_blocks_next_successor(
    tmp_path: Path,
) -> None:
    store, context, snapshot = _initialized_store(tmp_path)
    genesis = store.create_genesis_revision(_manifest(context, snapshot))
    intent = _intent(task_id="task-next", generation=1, idempotency_key="next")
    envelope = _envelope(context, snapshot, task_id="task-next")
    queued = _admit(store, context, snapshot, intent, envelope)
    first_manifest = _manifest(
        context,
        snapshot,
        generation=1,
        predecessor_revision_id=genesis.revision_id,
    )
    first = store.activate_successor_revision(first_manifest)
    second_manifest = _manifest(
        context,
        snapshot,
        generation=2,
        predecessor_revision_id=first.revision_id,
    )

    assert store.activate_successor_revision(first_manifest) == first
    with pytest.raises(RevisionConflictError, match="must be pinned or cancelled"):
        store.activate_successor_revision(second_manifest)

    restarted = _restart(tmp_path)
    assert restarted.get_task_admission(queued.admission_id) == queued
    admitted = _admit(restarted, context, snapshot, intent, envelope)
    assert admitted.status is AdmissionStatus.ADMITTED
    assert admitted.pinned_revision_id == first.revision_id
    second = restarted.activate_successor_revision(second_manifest)
    assert second.manifest == second_manifest


def test_queued_retry_and_next_activation_serialize_without_invalid_head(
    tmp_path: Path,
) -> None:
    store, context, snapshot = _initialized_store(tmp_path)
    genesis = store.create_genesis_revision(_manifest(context, snapshot))
    intent = _intent(task_id="task-next", generation=1, idempotency_key="next")
    envelope = _envelope(context, snapshot, task_id="task-next")
    _admit(store, context, snapshot, intent, envelope)
    first = store.activate_successor_revision(
        _manifest(
            context,
            snapshot,
            generation=1,
            predecessor_revision_id=genesis.revision_id,
        )
    )
    second_manifest = _manifest(
        context,
        snapshot,
        generation=2,
        predecessor_revision_id=first.revision_id,
    )
    barrier = threading.Barrier(3)
    admissions: list[TaskAdmissionRecord] = []
    activations = []
    activation_errors: list[BaseException] = []

    def retry_admission() -> None:
        barrier.wait()
        admissions.append(_admit(store, context, snapshot, intent, envelope))

    def activate_next() -> None:
        barrier.wait()
        try:
            activations.append(store.activate_successor_revision(second_manifest))
        except BaseException as exc:  # pragma: no cover - asserted below
            activation_errors.append(exc)

    threads = [
        threading.Thread(target=retry_admission),
        threading.Thread(target=activate_next),
    ]
    for thread in threads:
        thread.start()
    barrier.wait()
    for thread in threads:
        thread.join(timeout=10)

    assert len(admissions) == 1
    assert admissions[0].status is AdmissionStatus.ADMITTED
    assert admissions[0].pinned_revision_id == first.revision_id
    assert len(activations) + len(activation_errors) == 1
    if activation_errors:
        assert isinstance(activation_errors[0], RevisionConflictError)
        activations.append(store.activate_successor_revision(second_manifest))
    assert activations[0].manifest == second_manifest
    assert _restart(tmp_path).get_active_revision(second_manifest.stream_id) == activations[0]


def test_successor_activation_rejects_stale_fork_and_generation_gap(
    tmp_path: Path,
) -> None:
    store, context, snapshot = _initialized_store(tmp_path)
    genesis = store.create_genesis_revision(_manifest(context, snapshot))

    with pytest.raises(RevisionConflictError, match="generation|successor"):
        store.activate_successor_revision(
            _manifest(
                context,
                snapshot,
                generation=2,
                predecessor_revision_id=genesis.revision_id,
            )
        )

    first = store.activate_successor_revision(
        _manifest(
            context,
            snapshot,
            generation=1,
            predecessor_revision_id=genesis.revision_id,
        )
    )
    with pytest.raises(RevisionConflictError, match="predecessor|successor"):
        store.activate_successor_revision(
            _manifest(
                context,
                snapshot,
                generation=1,
                predecessor_revision_id=genesis.revision_id,
                workspace_marker="competing-workspace",
            )
        )

    second_manifest = _manifest(
        context,
        snapshot,
        generation=2,
        predecessor_revision_id=first.revision_id,
    )
    second = store.activate_successor_revision(second_manifest)
    historical_retry = store.activate_successor_revision(first.manifest)
    assert historical_retry.revision_id == first.revision_id
    assert historical_retry.active is False
    assert store.get_active_revision(second_manifest.stream_id) == second


def test_concurrent_successor_activation_has_one_authoritative_result(
    tmp_path: Path,
) -> None:
    store, context, snapshot = _initialized_store(tmp_path)
    genesis = store.create_genesis_revision(_manifest(context, snapshot))
    _admit(
        store,
        context,
        snapshot,
        _intent(task_id="task-next", generation=1, idempotency_key="next"),
        _envelope(context, snapshot, task_id="task-next"),
    )
    manifest = _manifest(
        context,
        snapshot,
        generation=1,
        predecessor_revision_id=genesis.revision_id,
    )
    barrier = threading.Barrier(3)
    results = []
    errors: list[BaseException] = []

    def activate() -> None:
        barrier.wait()
        try:
            results.append(store.activate_successor_revision(manifest))
        except BaseException as exc:  # pragma: no cover - asserted below
            errors.append(exc)

    threads = [threading.Thread(target=activate) for _ in range(2)]
    for thread in threads:
        thread.start()
    barrier.wait()
    for thread in threads:
        thread.join(timeout=10)

    assert errors == []
    assert len(results) == 2 and results[0] == results[1]
    assert store.get_active_revision(manifest.stream_id) == results[0]


def test_competing_concurrent_successors_cannot_fork_stream(tmp_path: Path) -> None:
    store, context, snapshot = _initialized_store(tmp_path)
    genesis = store.create_genesis_revision(_manifest(context, snapshot))
    _admit(
        store,
        context,
        snapshot,
        _intent(task_id="task-next", generation=1, idempotency_key="next"),
        _envelope(context, snapshot, task_id="task-next"),
    )
    manifests = [
        _manifest(
            context,
            snapshot,
            generation=1,
            predecessor_revision_id=genesis.revision_id,
            workspace_marker=marker,
        )
        for marker in ("workspace-v1", "workspace-successor-b")
    ]
    barrier = threading.Barrier(3)
    results = []
    errors: list[BaseException] = []

    def activate(manifest: RevisionManifestV1) -> None:
        barrier.wait()
        try:
            results.append(store.activate_successor_revision(manifest))
        except BaseException as exc:  # pragma: no cover - asserted below
            errors.append(exc)

    threads = [
        threading.Thread(target=activate, args=(manifest,)) for manifest in manifests
    ]
    for thread in threads:
        thread.start()
    barrier.wait()
    for thread in threads:
        thread.join(timeout=10)

    assert len(results) == 1
    assert len(errors) == 1 and isinstance(errors[0], RevisionConflictError)
    assert store.get_active_revision(genesis.manifest.stream_id) == results[0]


def test_successor_activation_revalidates_context_and_execution_snapshot(
    tmp_path: Path,
) -> None:
    store, context, snapshot = _initialized_store(tmp_path)
    genesis = store.create_genesis_revision(_manifest(context, snapshot))
    valid = _manifest(
        context,
        snapshot,
        generation=1,
        predecessor_revision_id=genesis.revision_id,
    )
    invalid_context = valid.model_copy(
        update={
            "context": valid.context.model_copy(
                update={"manifest_digest": "0" * 64}
            )
        }
    )
    unregistered_snapshot = _execution_snapshot(token_limit=16_384)
    invalid_execution = _manifest(
        context,
        unregistered_snapshot,
        generation=1,
        predecessor_revision_id=genesis.revision_id,
    )

    with pytest.raises(RevisionIntegrityError, match="materialized context"):
        store.activate_successor_revision(invalid_context)
    with pytest.raises(RevisionIntegrityError, match="execution snapshot"):
        store.activate_successor_revision(invalid_execution)
    assert store.get_active_revision(valid.stream_id) == genesis


def test_successor_activation_rolls_back_capacity_failure_and_exact_retry_precedes_it(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store, context, snapshot = _initialized_store(tmp_path)
    genesis = store.create_genesis_revision(_manifest(context, snapshot))
    manifest = _manifest(
        context,
        snapshot,
        generation=1,
        predecessor_revision_id=genesis.revision_id,
    )

    monkeypatch.setattr(store_module, "MAX_REVISION_STREAM_RECOVERY_BYTES", 0)
    with pytest.raises(RevisionCapacityError, match="stream ledger"):
        store.activate_successor_revision(manifest)
    assert store.get_active_revision(manifest.stream_id) == genesis
    with store.connect() as connection:
        row = connection.execute(
            "SELECT 1 FROM revisions WHERE revision_id = ?",
            (revision_id_for_manifest(manifest),),
        ).fetchone()
    assert row is None

    monkeypatch.setattr(
        store_module,
        "MAX_REVISION_STREAM_RECOVERY_BYTES",
        4 * 1024 * 1024,
    )
    activated = store.activate_successor_revision(manifest)
    monkeypatch.setattr(store_module, "MAX_REVISION_RECOVERY_ROWS", 0)
    monkeypatch.setattr(store_module, "MAX_REVISION_STREAM_RECOVERY_BYTES", 0)
    assert store.activate_successor_revision(manifest) == activated


def test_queued_active_generation_survives_restart_and_retry_pins_atomically(
    tmp_path: Path,
) -> None:
    store, context, snapshot = _initialized_store(tmp_path)
    genesis = store.create_genesis_revision(_manifest(context, snapshot))
    intent = _intent(task_id="task-next", generation=1, idempotency_key="request-next")
    envelope = _envelope(context, snapshot, task_id="task-next")
    queued = _admit(store, context, snapshot, intent, envelope)
    assert queued.status is AdmissionStatus.QUEUED

    successor = _activate_successor(store, context, snapshot, genesis.revision_id)
    restarted = _restart(tmp_path)
    still_queued = restarted.get_task_admission(queued.admission_id)
    assert still_queued.status is AdmissionStatus.QUEUED

    admitted = _admit(restarted, context, snapshot, intent, envelope)
    assert admitted.status is AdmissionStatus.ADMITTED
    assert admitted.pinned_revision_id == revision_id_for_manifest(successor)


def test_queued_recovery_still_rejects_older_or_gap_generation(tmp_path: Path) -> None:
    store, context, snapshot = _initialized_store(tmp_path)
    genesis = store.create_genesis_revision(_manifest(context, snapshot))
    queued = _admit(
        store,
        context,
        snapshot,
        _intent(task_id="task-next", generation=1, idempotency_key="next"),
    )
    _activate_successor(store, context, snapshot, genesis.revision_id)
    with sqlite3.connect(tmp_path / "evolution.db") as connection:
        connection.execute("PRAGMA ignore_check_constraints = ON")
        connection.execute(
            "UPDATE task_admissions SET required_generation = 0 WHERE admission_id = ?",
            (queued.admission_id,),
        )
        connection.commit()
    with pytest.raises(RevisionIntegrityError, match="admission"):
        _restart(tmp_path)


def test_genesis_exact_retry_revalidates_active_stream_closure(tmp_path: Path) -> None:
    store, context, snapshot = _initialized_store(tmp_path)
    manifest = _manifest(context, snapshot)
    store.create_genesis_revision(manifest)
    with store.connect() as connection:
        connection.execute("PRAGMA ignore_check_constraints = ON")
        connection.execute(
            "UPDATE revision_streams SET active_generation = 1 WHERE stream_id = ?",
            (manifest.stream_id,),
        )
        connection.commit()

    with pytest.raises(RevisionIntegrityError, match="active|stream"):
        store.create_genesis_revision(manifest)


@pytest.mark.parametrize(
    "dependency",
    ["stream_generation", "materialization", "execution_snapshot"],
)
def test_get_active_revision_revalidates_full_live_closure(
    tmp_path: Path,
    dependency: str,
) -> None:
    store, context, snapshot = _initialized_store(tmp_path)
    manifest = _manifest(context, snapshot)
    store.create_genesis_revision(manifest)
    with store.connect() as connection:
        if dependency == "stream_generation":
            connection.execute("PRAGMA ignore_check_constraints = ON")
            connection.execute(
                "UPDATE revision_streams SET active_generation = 1 WHERE stream_id = ?",
                (manifest.stream_id,),
            )
        elif dependency == "materialization":
            connection.execute(
                "UPDATE context_materializations SET manifest_json = '{}' WHERE context_id = ?",
                (context.context_id,),
            )
        elif dependency == "execution_snapshot":
            connection.execute(
                "UPDATE execution_snapshots SET snapshot_json = '{}' "
                "WHERE execution_snapshot_id = ?",
                (execution_snapshot_id_for_snapshot(snapshot),),
            )
        else:  # pragma: no cover - parametrization is closed
            raise AssertionError(dependency)
        connection.commit()

    with pytest.raises(
        RevisionIntegrityError,
        match="stream|revision|materialized|snapshot",
    ):
        store.get_active_revision(manifest.stream_id)


def test_queued_retry_revalidates_execution_snapshot_closure(tmp_path: Path) -> None:
    store, context, snapshot = _initialized_store(tmp_path)
    store.create_genesis_revision(_manifest(context, snapshot))
    intent = _intent(task_id="task-next", generation=1, idempotency_key="next")
    queued = _admit(store, context, snapshot, intent)
    with store.connect() as connection:
        connection.execute(
            "UPDATE execution_snapshots SET snapshot_json = '{}' WHERE execution_snapshot_id = ?",
            (execution_snapshot_id_for_snapshot(snapshot),),
        )
        connection.commit()

    with pytest.raises(RevisionIntegrityError, match="execution snapshot"):
        _admit(store, context, snapshot, intent)
    with pytest.raises(RevisionIntegrityError, match="execution snapshot"):
        store.get_task_admission(queued.admission_id)
    with store.connect() as connection:
        row = connection.execute(
            "SELECT status FROM task_admissions WHERE admission_id = ?",
            (queued.admission_id,),
        ).fetchone()
    assert row is not None
    assert row["status"] == str(AdmissionStatus.QUEUED)


def _tamper_admission_dependency(
    store: EvolutionStore,
    admitted: TaskAdmissionRecord,
    dependency: str,
) -> str:
    admission_id = admitted.admission_id
    with store.connect() as connection:
        if dependency == "stream":
            connection.execute("PRAGMA ignore_check_constraints = ON")
            connection.execute(
                "UPDATE revision_streams SET active_generation = 1 WHERE stream_id = ?",
                (admitted.request.stream_id,),
            )
        elif dependency == "revision":
            connection.execute(
                "UPDATE revisions SET manifest_json = '{}' WHERE revision_id = ?",
                (admitted.pinned_revision_id,),
            )
        elif dependency == "materialization":
            connection.execute(
                "UPDATE context_materializations SET manifest_json = '{}' WHERE context_id = ?",
                (admitted.request.context_id,),
            )
        elif dependency == "execution_snapshot":
            connection.execute(
                "UPDATE execution_snapshots SET snapshot_json = '{}' "
                "WHERE execution_snapshot_id = ?",
                (admitted.request.execution_snapshot_id,),
            )
        elif dependency == "envelope":
            request = TaskAdmissionRequest.model_validate_json(
                connection.execute(
                    "SELECT request_json FROM task_admissions WHERE admission_id = ?",
                    (admission_id,),
                ).fetchone()[0]
            ).model_copy(update={"workspace_snapshot": _ref("workspace", "tampered")})
            request = request.model_copy(
                update={"task_envelope_digest": canonical_digest(request.execution_envelope())}
            )
            request = TaskAdmissionRequest.model_validate(request.model_dump(mode="python"))
            request_digest = canonical_digest(request)
            replacement_id = admission_id_for_request(request)
            connection.execute(
                "UPDATE task_admissions SET admission_id = ?, request_digest = ?, "
                "request_json = ? WHERE admission_id = ?",
                (replacement_id, request_digest, canonical_json(request), admission_id),
            )
            admission_id = replacement_id
        else:  # pragma: no cover - test helper is closed by parametrization
            raise AssertionError(dependency)
        connection.commit()
    return admission_id


@pytest.mark.parametrize(
    "dependency",
    ["revision", "materialization", "execution_snapshot"],
)
def test_get_task_admission_revalidates_live_dependencies(
    tmp_path: Path,
    dependency: str,
) -> None:
    store, context, snapshot = _initialized_store(tmp_path)
    store.create_genesis_revision(_manifest(context, snapshot))
    admitted = _admit(store, context, snapshot)
    _tamper_admission_dependency(store, admitted, dependency)

    with pytest.raises(
        RevisionIntegrityError,
        match="admission|revision|materialized|snapshot",
    ):
        store.get_task_admission(admitted.admission_id)


@pytest.mark.parametrize(
    "dependency",
    ["stream", "revision", "materialization", "execution_snapshot", "envelope"],
)
def test_finish_revalidates_full_authoritative_closure_before_transition(
    tmp_path: Path,
    dependency: str,
) -> None:
    store, context, snapshot = _initialized_store(tmp_path)
    store.create_genesis_revision(_manifest(context, snapshot))
    admitted = _admit(store, context, snapshot)
    admission_id = _tamper_admission_dependency(store, admitted, dependency)

    with pytest.raises(
        RevisionIntegrityError, match="admission|stream|revision|materialized|snapshot"
    ):
        store.finish_task_admission(admission_id, AdmissionStatus.COMPLETED)
    with store.connect() as connection:
        assert (
            connection.execute(
                "SELECT status FROM task_admissions WHERE admission_id = ?",
                (admission_id,),
            ).fetchone()[0]
            == "admitted"
        )


def test_terminal_retry_revalidates_full_authoritative_closure(tmp_path: Path) -> None:
    store, context, snapshot = _initialized_store(tmp_path)
    store.create_genesis_revision(_manifest(context, snapshot))
    admitted = _admit(store, context, snapshot)
    completed = store.finish_task_admission(
        admitted.admission_id,
        AdmissionStatus.COMPLETED,
    )
    _tamper_admission_dependency(store, completed, "materialization")

    with pytest.raises(RevisionIntegrityError, match="materialized"):
        store.finish_task_admission(completed.admission_id, AdmissionStatus.COMPLETED)


def test_terminal_state_and_timestamp_contract_remain_immutable(tmp_path: Path) -> None:
    store, context, snapshot = _initialized_store(tmp_path)
    revision = store.create_genesis_revision(_manifest(context, snapshot))
    admitted = _admit(store, context, snapshot)
    completed = store.finish_task_admission(admitted.admission_id, AdmissionStatus.COMPLETED)

    assert (
        store.finish_task_admission(admitted.admission_id, AdmissionStatus.COMPLETED) == completed
    )
    assert completed.pinned_revision_id == revision.revision_id
    with pytest.raises(TaskAdmissionConflictError, match="terminal"):
        store.finish_task_admission(admitted.admission_id, AdmissionStatus.FAILED)

    request = bind_task_admission(
        _intent(task_id="timestamp"), _envelope(context, snapshot, task_id="timestamp")
    )
    created = datetime.now(timezone.utc)
    with pytest.raises(ValidationError, match="timestamps"):
        TaskAdmissionRecord(
            admission_id=f"adm-{canonical_digest(request)}",
            request_digest=canonical_digest(request),
            request=request,
            status="completed",
            pinned_revision_id="rev-" + "a" * 64,
            created_at=created,
            finished_at=created + timedelta(seconds=2),
            updated_at=created + timedelta(seconds=1),
        )


_TASK_ADMISSION_TEXT_COLUMNS = (
    "admission_id",
    "stream_id",
    "task_id",
    "idempotency_key",
    "request_digest",
    "request_json",
    "status",
    "reason",
    "pinned_revision_id",
    "created_at",
    "updated_at",
    "finished_at",
)


def _sqlite_text_bytes(values: tuple[object, ...]) -> int:
    return sum(
        len(value.encode("utf-8"))
        if isinstance(value, str)
        else len(value)
        if isinstance(value, bytes)
        else 0
        for value in values
    )


@pytest.mark.parametrize(
    "transition",
    [
        "queued_to_admitted",
        "admitted_to_completed",
        "admitted_to_failed",
        "admitted_to_cancelled",
        "queued_to_cancelled",
    ],
)
def test_task_admission_transition_enforces_exact_byte_delta_capacity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    transition: str,
) -> None:
    store, context, snapshot = _initialized_store(tmp_path)
    genesis = store.create_genesis_revision(_manifest(context, snapshot))
    transition_now = "2030-01-01T00:00:00.123456Z"

    if transition.startswith("queued"):
        intent = _intent(
            task_id=f"task-{transition}",
            generation=1,
            idempotency_key=f"request-{transition}",
        )
        envelope = _envelope(context, snapshot, task_id=intent.task_id)
        initial = _admit(store, context, snapshot, intent, envelope)
    else:
        intent = _intent(
            task_id=f"task-{transition}",
            idempotency_key=f"request-{transition}",
        )
        envelope = _envelope(context, snapshot, task_id=intent.task_id)
        initial = _admit(store, context, snapshot, intent, envelope)

    if transition == "queued_to_admitted":
        successor = _activate_successor(
            store,
            context,
            snapshot,
            genesis.revision_id,
        )
        target_status = AdmissionStatus.ADMITTED
        target_pin = revision_id_for_manifest(successor)
        target_finished_at = None

        def apply_transition() -> TaskAdmissionRecord:
            return _admit(store, context, snapshot, intent, envelope)

    else:
        target_status = AdmissionStatus(transition.rsplit("_to_", 1)[1])
        target_pin = initial.pinned_revision_id
        target_finished_at = transition_now

        def apply_transition() -> TaskAdmissionRecord:
            return store.finish_task_admission(initial.admission_id, target_status)

    monkeypatch.setattr(store_module, "utc_now_iso", lambda: transition_now)
    with store.connect() as connection:
        row = connection.execute(
            "SELECT * FROM task_admissions WHERE admission_id = ?",
            (initial.admission_id,),
        ).fetchone()
        assert row is not None
        _rows, used_bytes = store_module._ledger_payload_usage(
            connection,
            "task_admissions",
            _TASK_ADMISSION_TEXT_COLUMNS,
        )
    old_values = tuple(row[column] for column in _TASK_ADMISSION_TEXT_COLUMNS)
    overrides = {
        "status": str(target_status),
        "reason": None,
        "pinned_revision_id": target_pin,
        "updated_at": transition_now,
        "finished_at": target_finished_at,
    }
    new_values = tuple(
        overrides[column] if column in overrides else row[column]
        for column in _TASK_ADMISSION_TEXT_COLUMNS
    )
    expected_total = used_bytes - _sqlite_text_bytes(old_values) + _sqlite_text_bytes(new_values)
    assert expected_total > used_bytes

    monkeypatch.setattr(
        store_module,
        "MAX_TASK_ADMISSION_RECOVERY_BYTES",
        expected_total - 1,
    )
    with pytest.raises(RevisionCapacityError, match="byte capacity"):
        apply_transition()
    assert store.get_task_admission(initial.admission_id) == initial

    monkeypatch.setattr(
        store_module,
        "MAX_TASK_ADMISSION_RECOVERY_BYTES",
        expected_total,
    )
    transitioned = apply_transition()
    assert transitioned.status is target_status

    monkeypatch.setattr(store_module, "MAX_TASK_ADMISSION_RECOVERY_BYTES", 0)
    assert apply_transition() == transitioned

    monkeypatch.setattr(
        store_module,
        "MAX_TASK_ADMISSION_RECOVERY_BYTES",
        expected_total,
    )
    restarted = _restart(tmp_path)
    assert restarted.get_task_admission(initial.admission_id) == transitioned


def test_unpinned_cancelled_remains_historical_after_arbitrary_head_advance(
    tmp_path: Path,
) -> None:
    store, context, snapshot = _initialized_store(tmp_path)
    genesis = store.create_genesis_revision(_manifest(context, snapshot))

    pinned_intent = _intent(task_id="task-pinned", idempotency_key="request-pinned")
    pinned_envelope = _envelope(context, snapshot, task_id=pinned_intent.task_id)
    pinned = store.finish_task_admission(
        _admit(store, context, snapshot, pinned_intent, pinned_envelope).admission_id,
        AdmissionStatus.CANCELLED,
    )
    assert pinned.pinned_revision_id == genesis.revision_id

    unpinned_intent = _intent(
        task_id="task-unpinned",
        generation=1,
        idempotency_key="request-unpinned",
    )
    unpinned_envelope = _envelope(context, snapshot, task_id=unpinned_intent.task_id)
    unpinned = store.finish_task_admission(
        _admit(store, context, snapshot, unpinned_intent, unpinned_envelope).admission_id,
        AdmissionStatus.CANCELLED,
    )
    assert unpinned.pinned_revision_id is None

    first = _activate_successor(
        store,
        context,
        snapshot,
        genesis.revision_id,
    )
    _activate_successor(
        store,
        context,
        snapshot,
        revision_id_for_manifest(first),
        generation=2,
    )

    restarted = _restart(tmp_path)
    assert restarted.get_task_admission(unpinned.admission_id) == unpinned
    assert (
        _admit(
            restarted,
            context,
            snapshot,
            unpinned_intent,
            unpinned_envelope,
        )
        == unpinned
    )
    assert (
        restarted.finish_task_admission(
            unpinned.admission_id,
            AdmissionStatus.CANCELLED,
        )
        == unpinned
    )
    assert (
        restarted.finish_task_admission(
            pinned.admission_id,
            AdmissionStatus.CANCELLED,
        )
        == pinned
    )


def test_exact_retry_precedes_row_and_byte_capacity_checks(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store, context, snapshot = _initialized_store(tmp_path)
    revision = store.create_genesis_revision(_manifest(context, snapshot))
    admission = _admit(store, context, snapshot)
    verified = verified_execution_snapshot_for_test(snapshot)
    snapshot_record = store.register_execution_snapshot(verified)

    monkeypatch.setattr(store_module, "MAX_EXECUTION_SNAPSHOT_RECOVERY_ROWS", 1)
    monkeypatch.setattr(store_module, "MAX_REVISION_RECOVERY_ROWS", 1)
    monkeypatch.setattr(store_module, "MAX_REVISION_STREAM_RECOVERY_ROWS", 1)
    monkeypatch.setattr(store_module, "MAX_TASK_ADMISSION_RECOVERY_ROWS", 1)
    monkeypatch.setattr(store_module, "MAX_EXECUTION_SNAPSHOT_RECOVERY_BYTES", 1)
    monkeypatch.setattr(store_module, "MAX_REVISION_RECOVERY_BYTES", 1)
    monkeypatch.setattr(store_module, "MAX_TASK_ADMISSION_RECOVERY_BYTES", 1)

    assert store.register_execution_snapshot(verified) == snapshot_record
    assert store.create_genesis_revision(_manifest(context, snapshot)) == revision
    assert _admit(store, context, snapshot) == admission


def test_write_and_recovery_share_aggregate_byte_capacity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store, context, snapshot = _initialized_store(tmp_path)
    store.create_genesis_revision(_manifest(context, snapshot))
    monkeypatch.setattr(store_module, "MAX_TASK_ADMISSION_RECOVERY_BYTES", 1)

    with pytest.raises(RevisionCapacityError, match="byte capacity"):
        _admit(store, context, snapshot)

    monkeypatch.undo()
    _admit(store, context, snapshot)
    monkeypatch.setattr(store_module, "MAX_TASK_ADMISSION_RECOVERY_BYTES", 1)
    with pytest.raises(RevisionIntegrityError, match="byte limit"):
        _restart(tmp_path)


def test_materialization_write_and_recovery_share_byte_capacity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = EvolutionStore(
        db_path=tmp_path / "evolution.db",
        artifact_root=tmp_path / "managed",
        executable_registry=verified_builtin_registry(tmp_path / "registry"),
    )
    store.initialize()
    monkeypatch.setattr(
        store_module,
        "MAX_CONTEXT_MATERIALIZATION_RECOVERY_BYTES",
        1,
    )

    with pytest.raises(RevisionCapacityError, match="byte capacity"):
        store.resolve_materialized_context(_projection_request())

    monkeypatch.undo()
    store.resolve_materialized_context(_projection_request())
    monkeypatch.setattr(
        store_module,
        "MAX_CONTEXT_MATERIALIZATION_RECOVERY_BYTES",
        1,
    )
    with pytest.raises(RevisionIntegrityError, match="byte limit"):
        _restart(tmp_path)


def test_recovery_rejects_single_row_and_aggregate_overflow_before_json_parse(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store, context, snapshot = _initialized_store(tmp_path)
    store.create_genesis_revision(_manifest(context, snapshot))
    first = _admit(store, context, snapshot)
    _admit(
        store,
        context,
        snapshot,
        _intent(task_id="task-2", idempotency_key="request-2"),
    )
    with store.connect() as connection:
        row_sizes = [
            row[0]
            for row in connection.execute(
                "SELECT length(CAST(request_json AS BLOB)) FROM task_admissions ORDER BY task_id"
            )
        ]

    monkeypatch.setattr(
        store_module,
        "MAX_TASK_ADMISSION_RECOVERY_BYTES",
        sum(row_sizes) - 1,
    )
    monkeypatch.setattr(store_module, "RECOVERY_FETCH_ROWS", 1)
    with pytest.raises(RevisionIntegrityError, match="byte limit"):
        _restart(tmp_path)

    monkeypatch.setattr(store_module, "MAX_TASK_ADMISSION_RECOVERY_BYTES", 10**9)
    monkeypatch.setattr(store_module, "MAX_TASK_ADMISSION_ROW_BYTES", 32)
    with sqlite3.connect(tmp_path / "evolution.db") as connection:
        connection.execute(
            "UPDATE task_admissions SET request_json = ? WHERE admission_id = ?",
            ("{" + "x" * 100 + "}", first.admission_id),
        )
        connection.commit()
    with pytest.raises(RevisionIntegrityError, match="row byte limit"):
        _restart(tmp_path)


def test_context_snapshot_budget_rejects_oversized_sql_text_before_python_allocation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = EvolutionStore(
        db_path=tmp_path / "evolution.db",
        artifact_root=tmp_path / "managed",
    )
    store.initialize()
    oversized = canonical_json({"payload": "x" * (2 * 1024 * 1024)})
    with store.connect() as connection:
        connection.execute(
            "INSERT INTO contexts (context_id, created_at, request_json, response_json, "
            "selected_artifact_ids_json) VALUES (?, ?, ?, ?, ?)",
            ("ctx-oversized", "2027-01-01T00:00:00Z", oversized, "{}", "[]"),
        )
        connection.commit()
    monkeypatch.setattr(store_module, "MAX_CONTEXT_SNAPSHOT_INVENTORY_BYTES", 1)

    tracemalloc.start()
    try:
        with store.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            with pytest.raises(ValueError, match="byte limit"):
                store._expected_context_snapshot_bytes(connection)
            connection.rollback()
        _current, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()

    assert peak < 512 * 1024


def test_b3_budget_rejects_oversized_sql_text_before_python_allocation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store, _context, snapshot = _initialized_store(tmp_path)
    oversized = canonical_json({"payload": "x" * (2 * 1024 * 1024)})
    with store.connect() as connection:
        connection.execute(
            "UPDATE execution_snapshots SET snapshot_json = ? WHERE execution_snapshot_id = ?",
            (oversized, execution_snapshot_id_for_snapshot(snapshot)),
        )
        connection.commit()
    monkeypatch.setattr(store_module, "MAX_EXECUTION_SNAPSHOT_RECOVERY_BYTES", 1)

    tracemalloc.start()
    try:
        with store.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            with pytest.raises(RevisionIntegrityError, match="byte limit"):
                store._verify_execution_snapshots(connection)
            connection.rollback()
        _current, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()

    assert peak < 512 * 1024


def test_store_identity_guard_rejects_oversized_sql_text_before_python_allocation(
    tmp_path: Path,
) -> None:
    store = EvolutionStore(
        db_path=tmp_path / "evolution.db",
        artifact_root=tmp_path / "managed",
    )
    store.initialize()
    oversized = "x" * (2 * 1024 * 1024)
    with store.connect() as connection:
        connection.execute(
            "UPDATE store_identity SET artifact_root = ? WHERE singleton = 1",
            (oversized,),
        )
        connection.commit()

    tracemalloc.start()
    try:
        with store.connect() as connection:
            with pytest.raises(ValueError, match="store identity"):
                store._verify_bound_store_identity(connection)
        _current, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()

    assert peak < 512 * 1024


def test_startup_uses_one_guarded_point_read_per_ledger_row(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store, context, snapshot = _initialized_store(tmp_path)
    store.create_genesis_revision(_manifest(context, snapshot))
    for index in range(4):
        _admit(
            store,
            context,
            snapshot,
            _intent(task_id=f"task-{index}", idempotency_key=f"request-{index}"),
        )

    statements: list[str] = []
    original_connect = EvolutionStore.connect

    @contextmanager
    def traced_connect(self: EvolutionStore) -> Iterator[sqlite3.Connection]:
        with original_connect(self) as connection:
            connection.set_trace_callback(statements.append)
            yield connection

    monkeypatch.setattr(EvolutionStore, "connect", traced_connect)
    _restart(tmp_path)

    expected_reads = {
        "contexts": 2,
        "execution_snapshots": 2,
        "context_materializations": 3,
        "revisions": 2,
        "revision_streams": 2,
        "task_admissions": 5,
    }
    for table, expected in expected_reads.items():
        reads = [
            sql
            for sql in statements
            if sql.lstrip().upper().startswith("SELECT") and f"FROM {table}" in sql
        ]
        assert len(reads) == expected, (table, reads)
        assert all("SELECT *" not in sql.upper() for sql in reads)
        assert all("AS __row_key" in sql or "AS __guard_" in sql for sql in reads)


@pytest.mark.parametrize(
    ("table", "column", "value", "error"),
    [
        ("execution_snapshots", "created_at", "not-a-timestamp", "execution snapshot"),
        ("revisions", "created_at", "not-a-timestamp", "revision"),
        ("revision_streams", "updated_at", "2026-01-01T00:00:00", "stream"),
        ("task_admissions", "created_at", "2026-01-01T00:00:00+01:00", "admission"),
    ],
)
def test_startup_rejects_noncanonical_ledger_timestamps(
    tmp_path: Path,
    table: str,
    column: str,
    value: str,
    error: str,
) -> None:
    store, context, snapshot = _initialized_store(tmp_path)
    store.create_genesis_revision(_manifest(context, snapshot))
    _admit(store, context, snapshot)
    with sqlite3.connect(tmp_path / "evolution.db") as connection:
        connection.execute(f"UPDATE {table} SET {column} = ?", (value,))
        connection.commit()

    with pytest.raises(RevisionIntegrityError, match=error):
        _restart(tmp_path)
