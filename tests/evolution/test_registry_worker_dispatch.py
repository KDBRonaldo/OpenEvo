from __future__ import annotations

import json
from pathlib import Path
from threading import Event
import time

import pytest

from openevo.evolution import methods as methods_module
from openevo.evolution import worker as worker_module
from openevo.evolution.framework import (
    EvolutionExecutionProfile,
    EvolutionFrameworkRegistry,
    EvolutionTargetSelection,
    MethodExecutionEnvelope,
    MethodExecutionServices,
    MethodInvocationABI,
    build_execution_envelope,
    canonical_digest,
    canonical_json,
)
from openevo.evolution.framework.builtins import (
    ImplementationDistributionIdentity,
    build_builtin_registry,
)
from openevo.evolution.models import (
    ArtifactRegisterRequest,
    ArtifactType,
    WorkerClaimRequest,
    WorkerCompleteRequest,
    WorkerFailRequest,
    WorkerHeartbeatRequest,
)
from openevo.evolution.planned_jobs import (
    PlanBoundJobCreateRequest,
    PlannedInputBinding,
)
from openevo.evolution.store import EvolutionStore
from openevo.evolution.worker import run_once


def _snapshot():
    return build_builtin_registry(
        ImplementationDistributionIdentity(
            distribution="openevo-test",
            distribution_version="1.0.0",
            distribution_digest="b" * 64,
        )
    )


class _FakeExecutableRegistry:
    def __init__(self, snapshot, method_handles) -> None:
        self.snapshot = snapshot
        self.method_handles = method_handles


def _registry(
    *,
    skill_handle=None,
    method_handles: dict[str, object] | None = None,
) -> _FakeExecutableRegistry:
    snapshot = _snapshot()
    handles = dict(methods_module.METHOD_REGISTRY)
    if skill_handle is not None:
        handles["skill_bundle"] = skill_handle
    handles.update(method_handles or {})
    return _FakeExecutableRegistry(
        snapshot=snapshot,
        method_handles=handles,
    )


def _context_registry(skill_handle) -> _FakeExecutableRegistry:
    source = _snapshot()
    builder = EvolutionFrameworkRegistry()
    for view in (source.targets, source.target_handlers, source.methods):
        for descriptor_id in view:
            descriptor = view[descriptor_id]
            if descriptor_id == "skill_bundle" and view is source.methods:
                payload = descriptor.model_dump(mode="python")
                payload["invocation_abi"] = MethodInvocationABI.METHOD_CONTEXT_V1
                descriptor = type(descriptor).model_validate(payload)
            builder.register(descriptor)
    snapshot = builder.freeze()
    handles = dict(methods_module.METHOD_REGISTRY)
    handles["skill_bundle"] = skill_handle
    return _FakeExecutableRegistry(
        snapshot=snapshot,
        method_handles=handles,
    )


def _store(tmp_path: Path) -> EvolutionStore:
    store = EvolutionStore(
        db_path=tmp_path / "evolution.db",
        artifact_root=tmp_path / "artifacts",
    )
    store.initialize()
    return store


def _create_skill_job(
    store: EvolutionStore,
    *,
    registry: _FakeExecutableRegistry,
    plan_id: str = "plan-skill-dispatch",
    priority: int = 100,
):
    dataset = store.register_artifact(
        ArtifactRegisterRequest(
            type=ArtifactType.DATASET,
            name="dataset",
            uri="file:///tmp/dataset",
            promoted=True,
        )
    )
    plan = registry.snapshot.compile_plan(
        plan_id=plan_id,
        selections=(
            EvolutionTargetSelection(
                target_id="skill_bundle",
                enabled=True,
                method_id="skill_bundle",
                config={"skill_markdown": "# Verified skill\n"},
            ),
        ),
        profile=EvolutionExecutionProfile(
            execution_mode="self_deployed",
            capture_mode="transcript",
            harness_id="codex",
        ),
    )
    request = PlanBoundJobCreateRequest(
        plan=plan,
        target_id="skill_bundle",
        job_type="openevo:run:task:round-0:skill_bundle",
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
        core_config={
            "name": "verified-skill",
            "promoted": True,
            "lineage": {"input_artifact_ids": [dataset.artifact_id]},
        },
        priority=priority,
    )
    created = store.create_plan_bound_job(request, snapshot=registry.snapshot)
    return request, created


def _claim_skill_job(
    store: EvolutionStore,
    request: PlanBoundJobCreateRequest,
):
    selection = request.selection()
    response = store.claim_job(
        WorkerClaimRequest(
            worker_id="verified-worker",
            capabilities=[request.job_type],
            method_capabilities=[selection.method_id],
            method_identity_capabilities={
                selection.method_id: selection.method_identity_digest
            },
        )
    )
    assert response.job is not None
    return response.job


class _StoreWorkerClient:
    def __init__(self, store: EvolutionStore) -> None:
        self.store = store
        self.failed: list[dict[str, object]] = []
        self.heartbeat_messages: list[str | None] = []

    def claim(
        self,
        worker_id: str,
        capabilities: list[str],
        *,
        lease_seconds: int | None = None,
        method_capabilities: list[str] | None = None,
        method_identity_capabilities: dict[str, str] | None = None,
    ):
        request = WorkerClaimRequest(
            worker_id=worker_id,
            capabilities=capabilities,
            method_capabilities=method_capabilities,
            method_identity_capabilities=method_identity_capabilities,
            lease_seconds=lease_seconds or 600,
        )
        claimed = self.store.claim_job(request).job
        return None if claimed is None else claimed.model_dump(mode="json")

    def heartbeat(
        self,
        job_id: str,
        lease_id: str,
        *,
        progress: float | None = None,
        message: str | None = None,
    ):
        self.heartbeat_messages.append(message)
        return self.store.heartbeat_job(
            job_id,
            WorkerHeartbeatRequest(
                lease_id=lease_id,
                progress=progress,
                message=message,
            ),
        )

    def complete(
        self,
        job_id: str,
        lease_id: str,
        artifacts: list[dict],
        *,
        report: dict | None = None,
    ):
        return self.store.complete_job(
            job_id,
            WorkerCompleteRequest(
                lease_id=lease_id,
                artifacts=artifacts,
                report=report or {},
            ),
        )

    def fail(
        self,
        job_id: str,
        lease_id: str,
        error: str,
        *,
        retryable: bool = True,
    ):
        result = self.store.fail_job(
            job_id,
            WorkerFailRequest(
                lease_id=lease_id,
                error=error,
                retryable=retryable,
            ),
        )
        self.failed.append(result)
        return result


def test_plan_bound_worker_dispatches_verified_handle_not_legacy_global(
    tmp_path: Path,
    monkeypatch,
) -> None:
    registry = _registry()
    store = _store(tmp_path)
    request, created = _create_skill_job(store, registry=registry)
    monkeypatch.setitem(
        methods_module.METHOD_REGISTRY,
        "skill_bundle",
        lambda job, artifact_root: (_ for _ in ()).throw(
            AssertionError("legacy global registry must not dispatch a planned job")
        ),
    )

    client = _StoreWorkerClient(store)
    claimed = run_once(
        client,
        worker_id="verified-worker",
        capabilities=[request.job_type],
        artifact_root=tmp_path / "artifacts",
        executable_registry=registry,
    )

    assert claimed is True
    assert client.failed == []
    with store.connect() as connection:
        row = connection.execute(
            "SELECT state FROM jobs WHERE job_id = ?", (created.job_id,)
        ).fetchone()
        artifact_row = connection.execute(
            "SELECT lineage_json FROM artifacts WHERE type = 'skill_bundle' "
            "ORDER BY created_at DESC LIMIT 1"
        ).fetchone()
    assert row["state"] == "succeeded"
    trusted_lineage = json.loads(artifact_row["lineage_json"])["openevo_execution"]
    assert trusted_lineage["job_id"] == created.job_id
    assert trusted_lineage["plan_id"] == request.plan.plan_id
    assert trusted_lineage["method_id"] == "skill_bundle"
    assert [binding["binding_id"] for binding in trusted_lineage["input_bindings"]] == [
        "current_dataset",
        "prior_target_artifacts",
    ]


@pytest.mark.parametrize(
    ("method_id", "target_id", "artifact_type", "emits_report"),
    [
        ("text_memory_expel_reflector", "text_memory", "text_memory", False),
        ("skill_bundle_reflector", "skill_bundle", "skill_bundle", False),
        ("agent_system_gepa_reflector", "agent_system", "agent_system", True),
    ],
)
def test_protected_methods_cross_plan_store_verified_worker_and_publication(
    tmp_path: Path,
    method_id: str,
    target_id: str,
    artifact_type: str,
    emits_report: bool,
) -> None:
    calls: list[str] = []

    def protected_test_handle(job, artifact_root):
        del artifact_root
        calls.append(job.job_id)
        outputs = [
            ArtifactRegisterRequest(
                type=artifact_type,
                name=f"{target_id}-candidate",
                uri=f"file:///tmp/{target_id}-candidate",
                promoted=True,
            )
        ]
        if emits_report:
            outputs.append(
                ArtifactRegisterRequest(
                    type=ArtifactType.REPORT,
                    name="agent-system-gepa-report",
                    uri="file:///tmp/agent-system-gepa-report.json",
                    promoted=True,
                )
            )
        return outputs

    registry = _registry(method_handles={method_id: protected_test_handle})
    store = _store(tmp_path)
    dataset = store.register_artifact(
        ArtifactRegisterRequest(
            type=ArtifactType.DATASET,
            name="protected-path-dataset",
            uri="file:///tmp/protected-path-dataset",
            promoted=True,
        )
    )
    plan = registry.snapshot.compile_plan(
        plan_id=f"plan-protected-{target_id}",
        selections=(
            EvolutionTargetSelection(
                target_id=target_id,
                enabled=True,
                method_id=method_id,
                config={
                    "reflector_llm": {
                        "model": "reflector-model",
                        "provider": "codex_cli",
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
    descriptor = registry.snapshot.methods[method_id]
    input_bindings = tuple(
        PlannedInputBinding(
            binding_id=binding.binding_id,
            artifact_ids=(dataset.artifact_id,)
            if binding.artifact_type == "dataset"
            else (),
        )
        for binding in descriptor.input_bindings
    )
    request = PlanBoundJobCreateRequest(
        plan=plan,
        target_id=target_id,
        job_type=f"openevo:protected:{method_id}",
        input_bindings=input_bindings,
        core_config={
            "name": f"protected-{target_id}",
            "promoted": True,
            "lineage": {"input_artifact_ids": [dataset.artifact_id]},
        },
    )
    created = store.create_plan_bound_job(request, snapshot=registry.snapshot)

    client = _StoreWorkerClient(store)
    assert run_once(
        client,
        worker_id=f"worker-{target_id}",
        capabilities=[request.job_type],
        artifact_root=tmp_path / "artifacts",
        executable_registry=registry,
    )

    assert calls == [created.job_id]
    assert client.failed == []
    with store.connect() as connection:
        job_row = connection.execute(
            "SELECT state FROM jobs WHERE job_id = ?",
            (created.job_id,),
        ).fetchone()
        artifact_rows = connection.execute(
            "SELECT type, state, staging_job_id, lineage_json FROM artifacts "
            "WHERE artifact_id != ? ORDER BY type",
            (dataset.artifact_id,),
        ).fetchall()
    assert job_row["state"] == "succeeded"
    assert {row["type"] for row in artifact_rows} == {
        artifact_type,
        *({"report"} if emits_report else set()),
    }
    target_rows = [row for row in artifact_rows if row["type"] == artifact_type]
    assert len(target_rows) == 1
    assert all(row["state"] == "active" for row in artifact_rows)
    assert all(row["staging_job_id"] is None for row in artifact_rows)
    for row in artifact_rows:
        execution = json.loads(row["lineage_json"])["openevo_execution"]
        assert execution["job_id"] == created.job_id
        assert execution["target_id"] == target_id
        assert execution["method_id"] == method_id
        assert execution["method_identity_digest"] == request.selection().method_identity_digest


def test_plan_identity_tamper_fails_before_verified_method_is_called(tmp_path: Path) -> None:
    calls: list[str] = []

    def skill_handle(job, artifact_root):
        del artifact_root
        calls.append(job.job_id)
        return []

    registry = _registry(skill_handle=skill_handle)
    store = _store(tmp_path)
    request, created = _create_skill_job(store, registry=registry)
    with store.connect() as connection:
        connection.execute(
            "UPDATE jobs SET method_identity_digest = ? WHERE job_id = ?",
            ("f" * 64, created.job_id),
        )
        connection.commit()

    client = _StoreWorkerClient(store)
    assert not run_once(
        client,
        worker_id="verified-worker",
        capabilities=[request.job_type],
        artifact_root=tmp_path / "artifacts",
        executable_registry=registry,
    )

    assert calls == []
    assert client.failed == []
    with store.connect() as connection:
        row = connection.execute(
            "SELECT state, error, lease_id FROM jobs WHERE job_id = ?",
            (created.job_id,),
        ).fetchone()
    assert row["state"] == "failed"
    assert row["error"] == "plan-bound job contract validation failed"
    assert row["lease_id"] is None


def test_plan_bound_worker_renews_real_store_lease_while_method_runs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def blocking_skill_handle(job, artifact_root):
        del job, artifact_root
        time.sleep(0.15)
        return []

    registry = _registry(skill_handle=blocking_skill_handle)
    store = _store(tmp_path)
    request, created = _create_skill_job(store, registry=registry)
    client = _StoreWorkerClient(store)
    monkeypatch.setattr(worker_module, "_heartbeat_interval_seconds", lambda _: 0.02)

    assert run_once(
        client,
        worker_id="verified-worker",
        capabilities=[request.job_type],
        artifact_root=tmp_path / "artifacts",
        lease_seconds=1,
        executable_registry=registry,
    )

    assert client.heartbeat_messages.count("running") >= 2
    with store.connect() as connection:
        row = connection.execute(
            "SELECT state FROM jobs WHERE job_id = ?",
            (created.job_id,),
        ).fetchone()
    assert row["state"] == "succeeded"


def test_plan_bound_worker_cancels_context_method_when_heartbeat_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    method_started = Event()
    method_cancelled = Event()

    def cancellable_skill_handle(context):
        cancellation = context.services.cancellation
        assert cancellation is not None
        method_started.set()
        if not cancellation.wait(1.0):
            raise AssertionError("worker cancellation did not reach the context method")
        method_cancelled.set()
        return []

    class _FailingHeartbeatClient(_StoreWorkerClient):
        def heartbeat(
            self,
            job_id: str,
            lease_id: str,
            *,
            progress: float | None = None,
            message: str | None = None,
        ):
            if message == "running":
                self.heartbeat_messages.append(message)
                raise RuntimeError("worker lease heartbeat was rejected")
            return super().heartbeat(
                job_id,
                lease_id,
                progress=progress,
                message=message,
            )

    registry = _context_registry(cancellable_skill_handle)
    store = _store(tmp_path)
    request, created = _create_skill_job(store, registry=registry)
    client = _FailingHeartbeatClient(store)
    monkeypatch.setattr(worker_module, "_heartbeat_interval_seconds", lambda _: 0.01)

    assert run_once(
        client,
        worker_id="context-worker",
        capabilities=[request.job_type],
        artifact_root=tmp_path / "artifacts",
        lease_seconds=1,
        executable_registry=registry,
        method_services=MethodExecutionServices(harness=object()),
    )

    assert method_started.is_set()
    assert method_cancelled.is_set()
    assert client.heartbeat_messages.count("running") == 1
    with store.connect() as connection:
        row = connection.execute(
            "SELECT state, error FROM jobs WHERE job_id = ?",
            (created.job_id,),
        ).fetchone()
    assert row["state"] == "failed"
    assert row["error"] == "worker lease heartbeat was rejected"


def test_plan_bound_worker_dispatches_context_abi_with_core_services(
    tmp_path: Path,
) -> None:
    contexts = []

    def context_skill_handle(context):
        contexts.append(context)
        return []

    registry = _context_registry(context_skill_handle)
    store = _store(tmp_path)
    request, created = _create_skill_job(store, registry=registry)
    client = _StoreWorkerClient(store)

    assert run_once(
        client,
        worker_id="context-worker",
        capabilities=[request.job_type],
        artifact_root=tmp_path / "artifacts",
        executable_registry=registry,
        method_services=MethodExecutionServices(harness=object()),
    )

    assert len(contexts) == 1
    assert contexts[0].job.job_id == created.job_id
    assert contexts[0].envelope.user_config() == request.plan.selections[0].config()


@pytest.mark.parametrize(
    ("tamper_sql", "parameters"),
    [
        (
            "UPDATE jobs SET config_json = ? WHERE job_id = ?",
            ('{"name":"tampered"}',),
        ),
        (
            "UPDATE evolution_plans SET registry_snapshot_digest = ? WHERE plan_id = ?",
            ("f" * 64,),
        ),
        (
            "UPDATE artifacts SET uri = ? WHERE type = 'dataset'",
            ("file:///tmp/tampered-dataset",),
        ),
    ],
)
def test_plan_bound_dispatch_rejects_persisted_identity_tamper_before_call(
    tmp_path: Path,
    tamper_sql: str,
    parameters: tuple[str, ...],
) -> None:
    calls: list[str] = []

    def skill_handle(job, artifact_root):
        del artifact_root
        calls.append(job.job_id)
        return []

    registry = _registry(skill_handle=skill_handle)
    store = _store(tmp_path)
    request, created = _create_skill_job(store, registry=registry)
    identifier = (
        request.plan.plan_id
        if "evolution_plans" in tamper_sql
        else created.job_id
        if "jobs" in tamper_sql
        else None
    )
    with store.connect() as connection:
        connection.execute(
            tamper_sql,
            (*parameters, identifier) if identifier is not None else parameters,
        )
        connection.commit()

    client = _StoreWorkerClient(store)
    assert not run_once(
        client,
        worker_id="verified-worker",
        capabilities=[request.job_type],
        artifact_root=tmp_path / "artifacts",
        executable_registry=registry,
    )

    assert calls == []
    assert client.failed == []
    with store.connect() as connection:
        row = connection.execute(
            "SELECT state, error, lease_id FROM jobs WHERE job_id = ?",
            (created.job_id,),
        ).fetchone()
    assert row["state"] == "failed"
    assert row["error"] == "plan-bound job contract validation failed"
    assert row["lease_id"] is None


def test_plan_bound_claim_rejects_joint_config_and_envelope_tamper(
    tmp_path: Path,
) -> None:
    registry = _registry()
    store = _store(tmp_path)
    request, created = _create_skill_job(store, registry=registry)
    with store.connect() as connection:
        row = connection.execute(
            "SELECT execution_envelope_json FROM jobs WHERE job_id = ?",
            (created.job_id,),
        ).fetchone()
        original = MethodExecutionEnvelope.model_validate_json(
            row["execution_envelope_json"]
        )
        tampered = build_execution_envelope(
            plan_id=original.plan_id,
            plan_digest=original.plan_digest,
            registry_snapshot_digest=original.registry_snapshot_digest,
            target_id=original.target_id,
            method_id=original.method_id,
            method_identity_digest=original.method_identity_digest,
            user_config={"skill_markdown": "# Tampered skill\n"},
            core_config=original.core_config(),
            input_bindings=original.input_bindings,
            output_artifact_types=original.output_artifact_types,
        )
        connection.execute(
            "UPDATE jobs SET config_json = ?, execution_envelope_json = ? "
            "WHERE job_id = ?",
            (
                json.dumps(tampered.legacy_flat_config(), sort_keys=True),
                tampered.model_dump_json(),
                created.job_id,
            ),
        )
        connection.commit()

    client = _StoreWorkerClient(store)
    assert not run_once(
        client,
        worker_id="verified-worker",
        capabilities=[request.job_type],
        artifact_root=tmp_path / "artifacts",
        executable_registry=registry,
    )

    with store.connect() as connection:
        row = connection.execute(
            "SELECT state, error, lease_id FROM jobs WHERE job_id = ?",
            (created.job_id,),
        ).fetchone()
    assert row["state"] == "failed"
    assert row["error"] == "plan-bound job contract validation failed"
    assert row["lease_id"] is None


def test_invalid_plan_bound_job_is_quarantined_before_next_job_is_claimed(
    tmp_path: Path,
) -> None:
    calls: list[str] = []

    def skill_handle(job, artifact_root):
        del artifact_root
        calls.append(job.job_id)
        return []

    registry = _registry(skill_handle=skill_handle)
    store = _store(tmp_path)
    invalid_request, invalid_job = _create_skill_job(
        store,
        registry=registry,
        plan_id="plan-invalid",
        priority=200,
    )
    valid_request, valid_job = _create_skill_job(
        store,
        registry=registry,
        plan_id="plan-valid",
        priority=100,
    )
    with store.connect() as connection:
        connection.execute(
            "UPDATE jobs SET config_json = ? WHERE job_id = ?",
            ('{"tampered":true}', invalid_job.job_id),
        )
        connection.commit()

    client = _StoreWorkerClient(store)
    assert not run_once(
        client,
        worker_id="verified-worker",
        capabilities=[invalid_request.job_type],
        artifact_root=tmp_path / "artifacts",
        executable_registry=registry,
    )
    assert run_once(
        client,
        worker_id="verified-worker",
        capabilities=[valid_request.job_type],
        artifact_root=tmp_path / "artifacts",
        executable_registry=registry,
    )

    assert calls == [valid_job.job_id]
    with store.connect() as connection:
        rows = {
            row["job_id"]: row
            for row in connection.execute(
                "SELECT job_id, state, error FROM jobs WHERE job_id IN (?, ?)",
                (invalid_job.job_id, valid_job.job_id),
            ).fetchall()
        }
    assert rows[invalid_job.job_id]["state"] == "failed"
    assert rows[valid_job.job_id]["state"] == "succeeded"


@pytest.mark.parametrize(
    ("tamper_sql", "value", "uses_plan_id"),
    [
        ("UPDATE jobs SET target_id = ? WHERE job_id = ?", "text_memory", False),
        (
            "UPDATE jobs SET method_identity_digest = ? WHERE job_id = ?",
            "f" * 64,
            False,
        ),
        (
            "UPDATE evolution_plans SET registry_snapshot_digest = ? WHERE plan_id = ?",
            "f" * 64,
            True,
        ),
    ],
)
def test_complete_revalidates_plan_identity_after_claim(
    tmp_path: Path,
    tamper_sql: str,
    value: str,
    uses_plan_id: bool,
) -> None:
    registry = _registry()
    store = _store(tmp_path)
    request, created = _create_skill_job(store, registry=registry)
    claimed = _claim_skill_job(store, request)
    identifier = request.plan.plan_id if uses_plan_id else created.job_id
    with store.connect() as connection:
        connection.execute(tamper_sql, (value, identifier))
        connection.commit()

    with pytest.raises(ValueError):
        store.complete_job(
            created.job_id,
            WorkerCompleteRequest(
                lease_id=claimed.lease_id,
                artifacts=[
                    ArtifactRegisterRequest(
                        type=ArtifactType.SKILL_BUNDLE,
                        name="must-not-publish",
                        uri="file:///tmp/must-not-publish",
                    )
                ],
            ),
        )

    with store.connect() as connection:
        row = connection.execute(
            "SELECT state, lease_id FROM jobs WHERE job_id = ?",
            (created.job_id,),
        ).fetchone()
        output_count = connection.execute(
            "SELECT COUNT(*) FROM artifacts WHERE type = 'skill_bundle'"
        ).fetchone()[0]
    assert row["state"] == "claimed"
    assert output_count == 0


def test_complete_rejects_joint_output_envelope_and_digest_tamper(
    tmp_path: Path,
) -> None:
    registry = _registry()
    store = _store(tmp_path)
    request, created = _create_skill_job(store, registry=registry)
    claimed = _claim_skill_job(store, request)
    with store.connect() as connection:
        row = connection.execute(
            "SELECT execution_envelope_json FROM jobs WHERE job_id = ?",
            (created.job_id,),
        ).fetchone()
        envelope = MethodExecutionEnvelope.model_validate_json(
            row["execution_envelope_json"]
        ).model_copy(update={"output_artifact_types": ("text_memory",)})
        connection.execute(
            """
            UPDATE jobs
            SET execution_envelope_json = ?, execution_envelope_digest = ?,
                declared_output_artifact_types_json = ?
            WHERE job_id = ?
            """,
            (
                canonical_json(envelope),
                canonical_digest(envelope),
                json.dumps(["text_memory"]),
                created.job_id,
            ),
        )
        connection.commit()

    with pytest.raises(ValueError, match="output artifact types"):
        store.complete_job(
            created.job_id,
            WorkerCompleteRequest(
                lease_id=claimed.lease_id,
                artifacts=[
                    ArtifactRegisterRequest(
                        type=ArtifactType.TEXT_MEMORY,
                        name="forged-output",
                        uri="file:///tmp/forged-output",
                    )
                ],
            ),
        )

    with store.connect() as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM artifacts WHERE type = 'text_memory'"
        ).fetchone()[0] == 0
