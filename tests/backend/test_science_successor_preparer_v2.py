from __future__ import annotations

from contextlib import contextmanager
from types import SimpleNamespace

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
from openevo.backend.science_execution_v2 import (
    ScienceAttemptExecutorV2,
    science_session_result_sha256,
)
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
from openevo.evolution.models import (
    ArtifactResponse,
    DatasetCreateRequest,
)
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
        self.datasets: dict[str, dict] = {}
        self.dataset_requests: list[DatasetCreateRequest] = []
        self.jobs: dict[str, dict] = {}
        self.artifact_owners: dict[str, str] = {}
        self.target_run_counts: dict[str, int] = {}
        self.materialized: MaterializedContext | None = None
        self.materialized_count = 0
        self.discard_response: dict | None = None
        self.discarded_transition_ids: list[str] = []
        self.closed_count = 0
        self.rollout = None

    def close(self) -> None:
        self.closed_count += 1

    def create_dataset(self, payload: dict) -> dict:
        request = DatasetCreateRequest.model_validate(payload)
        assert request.idempotency_key is not None
        prior = next(
            (
                dataset
                for prior_request, dataset in zip(
                    self.dataset_requests,
                    self.datasets.values(),
                    strict=True,
                )
                if prior_request.idempotency_key == request.idempotency_key
            ),
            None,
        )
        if prior is not None:
            assert request == next(
                item
                for item in self.dataset_requests
                if item.idempotency_key == request.idempotency_key
            )
            return prior
        assert self.rollout is not None and self.rollout.result is not None
        result = self.rollout.result
        assert request.query.source == "openevo"
        assert request.query.event_types == ["openevo.session_completed"]
        assert request.query.status == ["COMPLETED"]
        assert request.query.source_event_id == f"session:{result.session_id}"
        assert request.query.task_id == result.task_id
        assert request.query.session_id == result.session_id
        ordinal = len(self.dataset_requests) + 1
        dataset_id = f"dataset-successor-{ordinal}"
        artifact_id = f"artifact-dataset-successor-{ordinal}"
        event_id = f"event-dataset-successor-{ordinal}"
        normalized = request.model_dump(mode="json", exclude_none=True)
        manifest = {
            "dataset_id": dataset_id,
            "name": request.name,
            "purpose": request.purpose,
            "query": normalized["query"],
            "limits": normalized["limits"],
            "event_ids": [event_id],
            "event_count": 1,
            "trace_count": 1,
            "records_path": "records.jsonl",
            "records_uri": f"file:///opaque/{dataset_id}/records.jsonl",
            "records_byte_size": 1,
            "records_sha256": "a" * 64,
            "create_identity": request.idempotency_key,
            "source_event_evidence": {
                "event_id": event_id,
                "source": "openevo",
                "event_type": "openevo.session_completed",
                "source_event_id": f"session:{result.session_id}",
                "task_id": result.task_id,
                "session_id": result.session_id,
                "session_result_sha256": science_session_result_sha256(result),
            },
        }
        self.artifacts[artifact_id] = ArtifactResponse(
            artifact_id=artifact_id,
            type="dataset",
            name=request.name,
            version=1,
            state="active",
            uri="file:///opaque/dataset.json",
            manifest=manifest,
            compatibility={"purpose": "openevo_science_successor_v2"},
            scores={},
            tags=[],
            promoted=True,
        ).model_dump(mode="json")
        response = {
            "dataset_id": dataset_id,
            "artifact_id": artifact_id,
            "event_count": 1,
            "trace_count": 1,
        }
        self.dataset_requests.append(request)
        self.datasets[dataset_id] = response
        return response

    def get_dataset(self, dataset_id: str) -> dict:
        return self.datasets[dataset_id]

    def create_plan_bound_job(self, payload: dict) -> dict:
        request = PlanBoundJobCreateRequest.model_validate(payload)
        assert request.core_config["promoted"] is True
        assert request.successor_transition_id is not None
        selection = request.selection()
        ordinal = self.target_run_counts.get(request.target_id, 0) + 1
        self.target_run_counts[request.target_id] = ordinal
        suffix = "" if ordinal == 1 else f"-{ordinal}"
        artifact_id = (
            f"artifact-{request.target_id}-successor{suffix}"
        )
        manifest = {
            "content_path": "memory.md",
            "target_id": request.target_id,
        }
        artifact = ArtifactResponse(
            artifact_id=artifact_id,
            type=request.target_id,
            name=f"Verified successor {request.target_id}",
            version=1,
            state="sealed",
            uri=f"file:///opaque/{request.target_id}",
            manifest=manifest,
            compatibility={"agent_harness": ["codex"]},
            scores={"quality": 1.0},
            tags=[],
            promoted=True,
        ).model_dump(mode="json")
        self.artifacts[artifact_id] = artifact
        self.artifact_owners[artifact_id] = (
            request.successor_transition_id
        )
        job_id = f"job-{request.target_id}-successor{suffix}"
        output = {
            "artifact_id": artifact_id,
            "type": request.target_id,
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
            "retryable": None,
            "state": "succeeded",
            "successor_transition_id": request.successor_transition_id,
            "outputs": [output],
        }
        expected_methods = {
            "agent_system": {
                "agent_system_reflector",
                "agent_system_history_reflector",
            },
            "skill_bundle": {"skill_bundle_reflector"},
            "text_memory": {
                "text_memory_reflector",
                "text_memory_expel_reflector",
            },
        }
        assert selection.method_id in expected_methods[request.target_id]
        return {"job_id": job_id, "state": "pending"}

    def get_internal_job_result(self, job_id: str) -> dict:
        return self.jobs[job_id]

    def get_artifact(self, artifact_id: str) -> dict:
        artifact = self.artifacts[artifact_id]
        assert artifact["state"] != "sealed"
        return artifact

    def get_internal_successor_artifact(
        self,
        successor_transition_id: str,
        artifact_id: str,
    ) -> dict:
        assert self.artifact_owners[artifact_id] == (
            successor_transition_id
        )
        return self.artifacts[artifact_id]

    def discard_successor_transition_outputs(
        self,
        successor_transition_id: str,
    ) -> dict:
        self.discarded_transition_ids.append(successor_transition_id)
        assert self.discard_response is not None
        return self.discard_response

    def create_materialized_context(self, payload: dict) -> dict:
        request = ContextProjectionResolveRequest.model_validate(payload)
        assert request.metadata.evolution is not None
        artifact_ids = request.metadata.evolution.context_artifact_ids
        owner_transition_ids = (
            request.metadata.evolution.context_artifact_owner_transition_ids
        )
        assert artifact_ids is not None
        assert owner_transition_ids is not None
        assert tuple(
            self.artifact_owners[artifact_id]
            for artifact_id in artifact_ids
        ) == owner_transition_ids
        self.materialized_count += 1
        context_suffix = (
            ""
            if self.materialized_count == 1
            else f"-{self.materialized_count}"
        )
        self.materialized = MaterializedContext(
            context_id=f"ctx-successor-v2{context_suffix}",
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


def test_production_preparer_discards_only_the_exact_transition_outputs(
    tmp_path,
) -> None:
    registry = verified_builtin_registry(tmp_path / "registry")
    evolution = _Evolution(registry.snapshot.registry_digest)
    preparer = ProductionScienceSuccessorPreparerV2(
        catalog=object(),
        ledger=object(),
        workspaces=object(),
        workspace_handoffs=object(),
        services=object(),
        executable_registry=registry,
    )
    context = SimpleNamespace(
        transition=SimpleNamespace(
            transition=SimpleNamespace(
                successor_transition_id="successor-transition-exact"
            )
        )
    )
    record = object()
    project = object()

    @contextmanager
    def _evolution(_context, _record, _project):
        assert _context is context
        assert _record is record
        assert _project is project
        yield object(), evolution

    preparer._cleanup_authority = lambda _context: (
        record,
        object(),
        project,
    )
    preparer._evolution = _evolution

    evolution.discard_response = {
        "successor_transition_id": "successor-transition-exact",
        "discarded_artifact_ids": ["artifact-sealed-1"],
        "discarded_materialized_context_ids": [
            "context-materialized-1"
        ],
    }
    receipt = preparer.discard_transition_outputs(context)
    assert receipt.successor_transition_id == (
        "successor-transition-exact"
    )
    assert receipt.discarded_artifact_ids == (
        "artifact-sealed-1",
    )
    assert receipt.discarded_materialized_context_ids == (
        "context-materialized-1",
    )
    assert evolution.discarded_transition_ids == [
        "successor-transition-exact"
    ]

    evolution.discard_response = {
        "successor_transition_id": "successor-transition-other",
        "discarded_artifact_ids": [],
        "discarded_materialized_context_ids": [],
    }
    with pytest.raises(
        ScienceSuccessorPreparationV2Error,
        match="discard receipt differs from the requested transition",
    ):
        preparer.discard_transition_outputs(context)


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
    base_config = _project_config()
    config_json = base_config.model_dump(mode="json")
    config_json["evolution"]["targets"] = {
        "agent_system": {
            "enabled": True,
            "method": "auto",
            "config": {},
        },
        "skill_bundle": {
            "enabled": True,
            "method": "skill_bundle_reflector",
            "config": {},
        },
        "text_memory": {
            "enabled": True,
            "method": "text_memory_expel_reflector",
            "config": {},
        },
    }
    config = type(base_config).model_validate(config_json)
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
    evolution.rollout = rollout
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
        assert successor.evolution_revision.artifact_count == 3
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
        assert commit.manifest.dataset_artifact_id == (
            "artifact-dataset-successor-1"
        )
        assert commit.manifest.method_artifact_ids == (
            "artifact-agent_system-successor",
            "artifact-skill_bundle-successor",
            "artifact-text_memory-successor",
        )
        assert commit.manifest.materialized_context_id == "ctx-successor-v2"

        next_authority = owner.project_admission_authority(project_id)
        partial_config_json = config.model_dump(mode="json")
        partial_config_json["evolution"]["targets"] = {
            "agent_system": {
                "enabled": False,
                "method": "auto",
                "config": {},
            },
            "skill_bundle": {
                "enabled": False,
                "method": "skill_bundle_reflector",
                "config": {},
            },
            "text_memory": {
                "enabled": True,
                "method": "text_memory_expel_reflector",
                "config": {},
            },
        }
        partial_config = type(config).model_validate(
            partial_config_json
        )
        partial_project = ProjectRecordV2(
            project_id=project_id,
            display_name=project.display_name,
            config=partial_config,
            project_config_sha256=project_config_sha256_for(
                partial_config
            ),
            created_at=project.created_at,
            updated_at="2026-07-23T02:00:01.000000Z",
            resource_version=2,
        )
        catalog.project = partial_project
        desired_next_authority = ScienceProjectAdmissionAuthorityV2(
            project_id=project_id,
            active_project_head=successor,
            project_config_sha256=(
                partial_project.project_config_sha256
            ),
            workspace_snapshot=next_authority.workspace_snapshot,
            normalized_evolution_intent_sha256=canonical_digest(
                partial_config.evolution
            ),
        )
        owner.begin_project_admission_authority_rebind(next_authority)
        owner.finish_project_admission_authority_rebind(
            desired_next_authority,
        )
        next_authority = owner.release_project_admission_authority_rebind(
            desired_next_authority,
        )
        second = owner.invoke(
            "submitCoreTaskV2",
            {
                "request": TaskSubmitRequestV2(
                    project_id=project_id,
                    expected_project_admission_etag=next_authority.project_etag,
                    expected_project_head_id=successor.project_head_id,
                    expected_project_head_manifest_sha256=successor.manifest_sha256,
                    expected_project_config_sha256=(
                        partial_project.project_config_sha256
                    ),
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
        second_successor = owner.active_project_head(project_id)
        assert second_successor.generation == 2
        assert second_successor.evolution_revision.artifact_count == 3
        second_transition = owner.get_successor_transition_for_task(
            second.task_id
        )
        second_commit = owner.successor_commit(
            second_transition.transition.successor_transition_id
        )
        assert second_commit is not None
        assert second_commit.manifest.method_artifact_ids == (
            "artifact-agent_system-successor",
            "artifact-skill_bundle-successor",
            "artifact-text_memory-successor-2",
        )
        assert evolution.materialized is not None
        assert evolution.materialized.selection.artifact_ids == (
            second_commit.manifest.method_artifact_ids
        )
        assert len(evolution.jobs) == 4

        no_evolution_json = partial_config.model_dump(mode="json")
        for target in no_evolution_json["evolution"]["targets"].values():
            target["enabled"] = False
        no_evolution_config = type(config).model_validate(
            no_evolution_json
        )
        no_evolution_project = ProjectRecordV2(
            project_id=project_id,
            display_name=project.display_name,
            config=no_evolution_config,
            project_config_sha256=project_config_sha256_for(
                no_evolution_config
            ),
            created_at=project.created_at,
            updated_at="2026-07-23T02:00:02.000000Z",
            resource_version=3,
        )
        catalog.project = no_evolution_project
        current_authority = owner.project_admission_authority(project_id)
        desired_third_authority = ScienceProjectAdmissionAuthorityV2(
            project_id=project_id,
            active_project_head=second_successor,
            project_config_sha256=(
                no_evolution_project.project_config_sha256
            ),
            workspace_snapshot=current_authority.workspace_snapshot,
            normalized_evolution_intent_sha256=canonical_digest(
                no_evolution_config.evolution
            ),
        )
        owner.begin_project_admission_authority_rebind(current_authority)
        owner.finish_project_admission_authority_rebind(
            desired_third_authority,
        )
        third_authority = owner.release_project_admission_authority_rebind(
            desired_third_authority,
        )
        prior_materialized_count = evolution.materialized_count
        third = owner.invoke(
            "submitCoreTaskV2",
            {
                "request": TaskSubmitRequestV2(
                    project_id=project_id,
                    expected_project_admission_etag=(
                        third_authority.project_etag
                    ),
                    expected_project_head_id=(
                        second_successor.project_head_id
                    ),
                    expected_project_head_manifest_sha256=(
                        second_successor.manifest_sha256
                    ),
                    expected_project_config_sha256=(
                        no_evolution_project.project_config_sha256
                    ),
                ),
                "idempotency_key": (
                    "production-successor-session-3-no-evolution"
                ),
            },
        )
        _wait_task_state(owner, third.task_id, "completed")
        third_successor = owner.active_project_head(project_id)
        assert third_successor.generation == 3
        assert third_successor.evolution_revision == (
            second_successor.evolution_revision
        )
        assert third_successor.runtime_context_snapshot == (
            second_successor.runtime_context_snapshot
        )
        third_transition = owner.get_successor_transition_for_task(
            third.task_id
        )
        third_commit = owner.successor_commit(
            third_transition.transition.successor_transition_id
        )
        assert third_commit is not None
        assert third_commit.manifest.method_artifact_ids == (
            second_commit.manifest.method_artifact_ids
        )
        assert evolution.materialized_count == prior_materialized_count
        assert len(evolution.jobs) == 4
        assert len(rollout.requests) == 3
        assert len(evolution.dataset_requests) == 3
        assert len(
            {
                request.idempotency_key
                for request in evolution.dataset_requests
            }
        ) == 3
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
