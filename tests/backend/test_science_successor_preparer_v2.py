from __future__ import annotations

import pytest

from openevo.backend.contracts.v2.models import (
    EffectiveExecutionSnapshotRefV2,
    TaskSubmitRequestV2,
    project_config_sha256_for,
)
from openevo.backend.contracts.v2.store import ProjectRecordV2
from openevo.backend.run_admission import (
    EffectiveExecutionSettings,
    resolve_genesis_execution_snapshot,
)
from openevo.backend.science_execution_v2 import ScienceAttemptExecutorV2
from openevo.backend.science_run_owner import CoreScienceTaskOwnerV2
from openevo.backend.science_run_store import ScienceProjectAdmissionAuthorityV2
from openevo.backend.science_successor_preparer_v2 import (
    ProductionScienceSuccessorPreparerV2,
    ScienceSuccessorPreparationV2Error,
)
from openevo.backend.workspace_handoff_v2 import WorkspaceHandoffStoreV2
from openevo.backend.workspace_store_v2 import WorkspaceStoreV2
from openevo.evolution.context_materialization import MaterializedContext
from openevo.evolution.context_projection import ContextProjectionResolveRequest
from openevo.evolution.framework import canonical_digest
from openevo.evolution.models import ArtifactResponse
from openevo.evolution.planned_jobs import PlanBoundJobCreateRequest
from tests.backend.test_science_execution_v2 import (
    _Catalog,
    _Clock,
    _Rollout,
    _Services,
    _head,
    _project_config,
    _service_binding,
    _wait_task_state,
)
from tests.framework_testkit import verified_builtin_registry


class _Evolution:
    def __init__(self, registry_sha256: str) -> None:
        self.registry_sha256 = registry_sha256
        self.artifacts: dict[str, dict] = {}
        self.jobs: dict[str, dict] = {}
        self.materialized: MaterializedContext | None = None
        self.closed_count = 0

    def close(self) -> None:
        self.closed_count += 1

    def create_dataset(self, payload: dict) -> dict:
        assert payload["query"]["event_types"] == ["openevo.session_completed"]
        artifact_id = "artifact-dataset-successor"
        manifest = {
            "dataset_id": "dataset-successor",
            "event_count": 1,
            "trace_count": 1,
        }
        self.artifacts[artifact_id] = ArtifactResponse(
            artifact_id=artifact_id,
            type="dataset",
            name="Captured transcript",
            version=1,
            state="active",
            uri="file:///opaque/dataset.json",
            manifest=manifest,
            compatibility={"purpose": "openevo_science_successor_v2"},
            scores={},
            tags=[],
            promoted=True,
        ).model_dump(mode="json")
        return {
            "dataset_id": "dataset-successor",
            "artifact_id": artifact_id,
            "event_count": 1,
            "trace_count": 1,
        }

    def create_plan_bound_job(self, payload: dict) -> dict:
        request = PlanBoundJobCreateRequest.model_validate(payload)
        assert request.core_config["promoted"] is True
        selection = request.selection()
        artifact_id = f"artifact-{request.target_id}-successor"
        manifest = {
            "content_path": "memory.md",
            "target_id": request.target_id,
        }
        artifact = ArtifactResponse(
            artifact_id=artifact_id,
            type="text_memory",
            name="Verified successor memory",
            version=1,
            state="active",
            uri="file:///opaque/memory.md",
            manifest=manifest,
            compatibility={"agent_harness": ["codex"]},
            scores={"quality": 1.0},
            tags=[],
            promoted=True,
        ).model_dump(mode="json")
        self.artifacts[artifact_id] = artifact
        job_id = f"job-{request.target_id}-successor"
        output = {
            "artifact_id": artifact_id,
            "type": "text_memory",
            "name": artifact["name"],
            "manifest": manifest,
            "lineage": {"plan_id": request.plan.plan_id},
            "compatibility": artifact["compatibility"],
            "scores": artifact["scores"],
            "promoted": True,
            "created_at": "2026-07-23T03:00:00Z",
            "payload_manifest_digest": canonical_digest({"memory.md": "Use the accepted result."}),
            "payload_byte_size": len("Use the accepted result.".encode()),
            "payload_file_count": 1,
        }
        self.jobs[job_id] = {
            "artifact_ids": [artifact_id],
            "error": None,
            "job_id": job_id,
            "state": "succeeded",
            "outputs": [output],
        }
        assert selection.method_id == "text_memory_reflector"
        return {"job_id": job_id, "state": "pending"}

    def get_internal_job_result(self, job_id: str) -> dict:
        return self.jobs[job_id]

    def get_artifact(self, artifact_id: str) -> dict:
        return self.artifacts[artifact_id]

    def create_materialized_context(self, payload: dict) -> dict:
        request = ContextProjectionResolveRequest.model_validate(payload)
        assert request.metadata.evolution is not None
        artifact_ids = request.metadata.evolution.context_artifact_ids
        assert artifact_ids is not None
        self.materialized = MaterializedContext(
            context_id="ctx-successor-v2",
            request_digest=canonical_digest(request),
            registry_digest=self.registry_sha256,
            successor_transition_id=request.successor_transition_id,
            predecessor_project_head_id=request.predecessor_project_head_id,
            base_model=request.base_model,
            projections=(),
            selection={
                "artifact_ids": artifact_ids,
                "skipped_artifacts": (),
                "reasons": ("explicit_artifact_ids",),
            },
            blobs=(),
            environment=(),
            instruction=(
                "Use the following long-term memory for this task:\nUse the accepted result."
            ),
            adapter_merge_spec={
                "base_model": request.base_model,
                "merge_mode": "reference_only",
                "adapters": (),
            },
        )
        return self.materialized.model_dump(mode="json")


def test_production_preparer_fails_closed_after_shutdown_is_requested(
    tmp_path,
) -> None:
    registry = verified_builtin_registry(tmp_path / "registry")
    preparer = ProductionScienceSuccessorPreparerV2(
        catalog=object(),
        ledger=object(),
        workspaces=object(),
        workspace_handoffs=object(),
        services=object(),
        executable_registry=registry,
    )

    preparer.request_stop()

    with pytest.raises(
        ScienceSuccessorPreparationV2Error,
        match="successor preparation is stopping",
    ):
        preparer.seal_dataset(object())


def test_production_preparer_commits_complete_workspace_and_context_successor(
    tmp_path,
) -> None:
    clock = _Clock()
    registry = verified_builtin_registry(tmp_path / "registry")
    config = _project_config()
    binding = _service_binding(registry.snapshot.registry_digest)
    project_id = "project-execution"
    workspaces = WorkspaceStoreV2(tmp_path / "workspaces")
    input_workspace = workspaces.ensure_empty_snapshot(project_id)
    verified = resolve_genesis_execution_snapshot(
        settings=EffectiveExecutionSettings(
            execution_mode=config.execution.mode,
            capture_mode=config.execution.capture_mode,
            harness_id=config.execution.harness_id,
            model_ref=config.execution.codex_model,
            token_limit=config.execution.token_limit,
            task_network_allow_internet=config.execution.task_network_allow_internet,
        ),
        service_binding=binding,
    )
    execution_sha256 = canonical_digest(verified.snapshot)
    head = _head(
        project_id,
        registry_sha256=registry.snapshot.registry_digest,
        workspace=input_workspace,
        effective_execution=EffectiveExecutionSnapshotRefV2(
            effective_execution_snapshot_id=f"exec-{execution_sha256}",
            project_id=project_id,
            execution_mode=config.execution.mode,
            capture_mode=config.execution.capture_mode,
            token_level_metrics_available=False,
            producer_id=verified.producer_id,
            snapshot_sha256=execution_sha256,
        ),
    )
    authority = ScienceProjectAdmissionAuthorityV2(
        project_id=project_id,
        active_project_head=head,
        project_config_sha256=project_config_sha256_for(config),
        workspace_snapshot=input_workspace,
        normalized_evolution_intent_sha256=canonical_digest(config.evolution),
    )
    project = ProjectRecordV2(
        project_id=project_id,
        display_name="Production successor project",
        config=config,
        project_config_sha256=project_config_sha256_for(config),
        created_at="2026-07-23T02:00:00.000000Z",
        updated_at="2026-07-23T02:00:00.000000Z",
        resource_version=1,
    )
    catalog = _Catalog(project)
    handoffs = WorkspaceHandoffStoreV2(tmp_path / "workspace-handoffs")
    services = _Services(binding)
    evolution = _Evolution(registry.snapshot.registry_digest)
    rollout = _Rollout(handoffs, binding, tmp_path / "gateway-sessions")
    (tmp_path / "gateway-sessions").mkdir(mode=0o700)

    def executor_factory(ledger):
        return ScienceAttemptExecutorV2(
            catalog=catalog,
            workspaces=workspaces,
            workspace_handoffs=handoffs,
            ledger=ledger,
            services=services,
            executable_registry=registry,
            rollout_factory=lambda _binding: rollout,
            prior_dataset_artifact_ids=lambda project_head: (
                ledger.prior_dataset_artifact_ids_for_head(project_head.project_head_id)
            ),
            clock=clock,
            poll_interval_seconds=0,
            max_poll_attempts=2,
        )

    def successor_factory(ledger):
        return ProductionScienceSuccessorPreparerV2(
            catalog=catalog,
            ledger=ledger,
            workspaces=workspaces,
            workspace_handoffs=handoffs,
            services=services,
            executable_registry=registry,
            evolution_factory=lambda _binding: evolution,
            clock=clock,
            poll_interval_seconds=0,
            max_poll_attempts=2,
        )

    owner = CoreScienceTaskOwnerV2(
        state_root=tmp_path / "owner",
        clock=clock,
        attempt_executor_factory=executor_factory,
        successor_preparer_factory=successor_factory,
    )
    try:
        owner.publish_project_admission_authority(authority)
        task = owner.invoke(
            "submitCoreTaskV2",
            {
                "request": TaskSubmitRequestV2(
                    project_id=project_id,
                    expected_project_admission_etag=authority.project_etag,
                    expected_project_head_id=head.project_head_id,
                    expected_project_head_manifest_sha256=head.manifest_sha256,
                    expected_project_config_sha256=project.project_config_sha256,
                ),
                "idempotency_key": "production-successor",
            },
        )
        try:
            _wait_task_state(owner, task.task_id, "completed")
        except AssertionError as exc:
            current = owner.invoke("getCoreTaskV2", {"task_id": task.task_id})
            transitions = owner.list_successor_transitions(project_id)
            raise AssertionError(f"{exc}; current={current}; transitions={transitions}") from exc

        successor = owner.active_project_head(project_id)
        assert successor.generation == 1
        assert successor.project_head_id == f"project-head-{successor.manifest_sha256}"
        assert successor.predecessor_project_head_id == head.project_head_id
        assert successor.evolution_revision.artifact_count == 1
        assert successor.runtime_context_snapshot.evolution_revision_id == (
            successor.evolution_revision.evolution_revision_id
        )
        assert evolution.materialized is not None
        assert successor.workspace_snapshot != input_workspace
        result_root = workspaces.snapshot_path(successor.workspace_snapshot)
        assert (result_root / "answer.txt").read_text(encoding="utf-8") == ("accepted\n")
        transition = owner.get_successor_transition_for_task(task.task_id)
        commit = owner.successor_commit(transition.transition.successor_transition_id)
        assert commit is not None
        assert commit.manifest.dataset_artifact_id == ("artifact-dataset-successor")
        assert commit.manifest.method_artifact_ids == ("artifact-text_memory-successor",)
        assert commit.manifest.materialized_context_id == "ctx-successor-v2"

        next_authority = owner.project_admission_authority(project_id)
        second = owner.invoke(
            "submitCoreTaskV2",
            {
                "request": TaskSubmitRequestV2(
                    project_id=project_id,
                    expected_project_admission_etag=next_authority.project_etag,
                    expected_project_head_id=successor.project_head_id,
                    expected_project_head_manifest_sha256=successor.manifest_sha256,
                    expected_project_config_sha256=project.project_config_sha256,
                ),
                "idempotency_key": "production-successor-session-2",
            },
        )
        _wait_task_state(owner, second.task_id, "completed")
        assert len(rollout.requests) == 2
        assert rollout.requests[0].runtime_context_binding.source == "empty_genesis"
        second_context = rollout.requests[1].runtime_context_binding
        assert second_context.source == "materialized_successor"
        assert second_context.project_head == successor
        assert second_context.materialized_context_id == (commit.manifest.materialized_context_id)
        assert second_context.selected_artifact_ids == (commit.manifest.method_artifact_ids)
        assert rollout.input_answer_before_run == [None, "accepted\n"]
        assert owner.active_project_head(project_id).generation == 2
    finally:
        owner.close()
        handoffs.close()
        workspaces.close()


def test_internal_materialized_context_transport_has_no_host_path(tmp_path) -> None:
    # This assertion guards the private successor receipt shape independently of
    # the HTTP transport tests in the Evolution suite.
    materialized = MaterializedContext(
        context_id="ctx-transport-v2",
        request_digest="1" * 64,
        registry_digest="2" * 64,
        projections=(),
        selection={"artifact_ids": (), "reasons": ("no_candidates",)},
        blobs=(),
        environment=(),
        instruction="",
        adapter_merge_spec={"merge_mode": "reference_only", "adapters": ()},
    )
    encoded = str(materialized.model_dump(mode="json"))
    assert str(tmp_path) not in encoded
    assert "file://" not in encoded
