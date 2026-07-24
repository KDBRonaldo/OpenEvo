"""Production preparation of one immutable v2 science successor.

This module consumes only the captured Attempt evidence and private Daemon
authorities.  It never derives successor state from a v1 project/run record and
never exposes an Evolution or workspace host path through the v2 contract.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
import math
import threading
from typing import Any, Iterator, Protocol

from openevo.backend.contracts.v2 import models as m2
from openevo.backend.contracts.v2.store import ProjectRecordV2
from openevo.backend.science_execution_v2 import (
    ScienceAttemptExecutionRecordV2,
    compile_science_evolution_experiment_v2,
)
from openevo.backend.science_successor import (
    AcceptedWorkspaceResultV2,
    ScienceMethodOutputV2,
    ScienceSuccessorPreparationContextV2,
    SealedTranscriptDatasetV2,
    SuccessorMaterializationV2,
    ValidatedScienceOutputsV2,
)
from openevo.backend.service_supervisor import (
    ServiceExecutionMode,
    ServiceRunBinding,
)
from openevo.backend.workspace_handoff_v2 import (
    WorkspaceHandoffStoreV2,
    WorkspaceResultReceiptV2,
)
from openevo.evolution.context_materialization import MaterializedContext
from openevo.evolution.context_projection import ContextProjectionResolveRequest
from openevo.evolution.framework import canonical_digest
from openevo.evolution.framework.builtins import (
    VerifiedExecutableRegistry,
    require_verified_executable_registry,
)
from openevo.evolution.framework.handlers import RuntimeDestinationRoots
from openevo.evolution.framework.profiles import execution_profile_for_release_mode
from openevo.evolution.models import (
    ArtifactResponse,
    ArtifactState,
    ArtifactType,
    DatasetCreateResponse,
    JobCreateResponse,
    JobState,
)
from openevo.evolution.planned_jobs import PlanBoundJobCreateRequest
from openevo.experiments.clients import EvolutionClientProtocol, EvolutionHttpClient
from openevo.experiments.compiler import (
    CompiledEvolutionMethodSpec,
    CompiledExperiment,
)
from openevo.projects.science.compiler import MANAGED_RUNTIME_IMAGES
from openevo.runtime.managed import MANAGED_WORKSPACE


_CONTEXT_ARTIFACT_TYPES = (
    "dataset",
    "text_memory",
    "parametric_memory",
    "skill_bundle",
    "agent_system",
)


class ScienceSuccessorPreparationV2Error(RuntimeError):
    pass


class _ProjectCatalogV2(Protocol):
    def get_project(self, project_id: str) -> ProjectRecordV2: ...


class _ScienceLedgerV2(Protocol):
    def get_attempt_execution(
        self,
        task_id: str,
        attempt_id: str,
    ) -> ScienceAttemptExecutionRecordV2: ...

    def get_captured_session_result(self, task_id: str, attempt_id: str): ...

    def prior_dataset_artifact_ids_for_head(
        self,
        project_head_id: str,
    ) -> tuple[str, ...]: ...

    def successor_commit_for_project_head(self, project_head_id: str): ...


class _WorkspaceStoreV2(Protocol):
    def create_upload(
        self,
        project_id: str,
        request: m2.WorkspaceUploadCreateV2,
        *,
        idempotency_key: str,
        now: datetime,
    ) -> tuple[m2.WorkspaceUploadSessionV2, bool]: ...

    def get_upload(
        self,
        project_id: str,
        upload_id: str,
    ) -> m2.WorkspaceUploadSessionV2: ...

    def put_chunk(
        self,
        project_id: str,
        upload_id: str,
        *,
        chunk_index: int,
        chunk: bytes,
        chunk_sha256: str,
        chunk_byte_size: int,
        if_match: str,
        idempotency_key: str,
        now: datetime,
    ) -> tuple[m2.WorkspaceUploadSessionV2, bool]: ...

    def finalize_upload(
        self,
        project_id: str,
        upload_id: str,
        request: m2.WorkspaceUploadFinalizeV2,
        *,
        if_match: str,
        idempotency_key: str,
        now: datetime,
    ) -> tuple[m2.WorkspaceUploadSessionV2, bool]: ...


class _ServiceOwnerV2(Protocol):
    def ensure_run_binding(
        self,
        execution_mode: ServiceExecutionMode,
        *,
        model_ref: str | None = None,
        codex_model: str | None = None,
        runtime_image: str | None = None,
        total_timeout: float | None = None,
    ) -> tuple[object, object | None]: ...


class ProductionScienceSuccessorPreparerV2:
    """Seal, evolve, materialize, and capture one complete successor."""

    def __init__(
        self,
        *,
        catalog: _ProjectCatalogV2,
        ledger: _ScienceLedgerV2,
        workspaces: _WorkspaceStoreV2,
        workspace_handoffs: WorkspaceHandoffStoreV2,
        services: _ServiceOwnerV2,
        executable_registry: VerifiedExecutableRegistry,
        evolution_factory: (Callable[[ServiceRunBinding], EvolutionClientProtocol] | None) = None,
        clock: Callable[[], datetime] | None = None,
        poll_interval_seconds: float = 1.0,
        max_poll_attempts: int = 7200,
    ) -> None:
        if (
            isinstance(poll_interval_seconds, bool)
            or not isinstance(poll_interval_seconds, int | float)
            or not math.isfinite(poll_interval_seconds)
            or poll_interval_seconds < 0
            or isinstance(max_poll_attempts, bool)
            or not isinstance(max_poll_attempts, int)
            or max_poll_attempts < 1
        ):
            raise ValueError("v2 successor polling configuration is invalid")
        self._catalog = catalog
        self._ledger = ledger
        self._workspaces = workspaces
        self._handoffs = workspace_handoffs
        self._services = services
        self._registry = require_verified_executable_registry(executable_registry)
        self._evolution_factory = evolution_factory or (
            lambda binding: EvolutionHttpClient(
                binding.evolution_backend_url,
                headers=binding.request_headers(),
            )
        )
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._poll_interval = float(poll_interval_seconds)
        self._max_poll_attempts = max_poll_attempts
        self._stop = threading.Event()

    def request_stop(self) -> None:
        """Interrupt bounded polling during Daemon shutdown."""

        self._stop.set()

    def _require_running(self) -> None:
        if self._stop.is_set():
            raise ScienceSuccessorPreparationV2Error("successor preparation is stopping")

    def seal_dataset(
        self,
        context: ScienceSuccessorPreparationContextV2,
    ) -> SealedTranscriptDatasetV2:
        self._require_running()
        record, result, project = self._authority(context)
        assert record.evidence is not None
        with self._evolution(context, record, project) as (_binding, client):
            raw = client.create_dataset(
                {
                    "name": (
                        f"{context.task.project_id}:{context.task.task_id}:"
                        f"{context.accepted_attempt.attempt_id}"
                    ),
                    "purpose": "openevo_science_successor_v2",
                    "query": {
                        "event_types": ["openevo.session_completed"],
                        "status": ["COMPLETED"],
                        "policy_version": record.evidence.policy_version,
                    },
                    "limits": {
                        "max_events": 1,
                        "max_traces": record.evidence.transcript_record_count,
                    },
                }
            )
            dataset = DatasetCreateResponse.model_validate(raw)
            if (
                dataset.event_count != 1
                or dataset.trace_count != record.evidence.transcript_record_count
                or dataset.trace_count != len(result.trajectory.traces)
            ):
                raise ScienceSuccessorPreparationV2Error(
                    "sealed transcript dataset does not exactly cover the captured result"
                )
            artifact = ArtifactResponse.model_validate(client.get_artifact(dataset.artifact_id))
        self._require_running()
        manifest = artifact.manifest
        if (
            artifact.artifact_id != dataset.artifact_id
            or artifact.type is not ArtifactType.DATASET
            or artifact.state is not ArtifactState.ACTIVE
            or artifact.promoted is not True
            or manifest.get("dataset_id") != dataset.dataset_id
            or manifest.get("event_count") != 1
            or manifest.get("trace_count") != dataset.trace_count
        ):
            raise ScienceSuccessorPreparationV2Error(
                "dataset artifact differs from the sealed transcript authority"
            )
        return SealedTranscriptDatasetV2(
            dataset_id=dataset.dataset_id,
            artifact_id=dataset.artifact_id,
            manifest_sha256=canonical_digest(manifest),
            record_count=dataset.trace_count,
            task_id=context.task.task_id,
            task_admission_id=context.task.admission.task_admission_id,
            accepted_attempt_id=context.accepted_attempt.attempt_id,
            capture_mode="transcript",
            token_level_metrics_available=False,
            sealed=True,
        )

    def run_methods(
        self,
        context: ScienceSuccessorPreparationContextV2,
        dataset: SealedTranscriptDatasetV2,
    ) -> tuple[ScienceMethodOutputV2, ...]:
        self._require_running()
        record, _result, project = self._authority(context)
        if (
            dataset.task_id != context.task.task_id
            or dataset.accepted_attempt_id != context.accepted_attempt.attempt_id
        ):
            raise ScienceSuccessorPreparationV2Error(
                "successor method dataset has different immutable ownership"
            )
        with self._evolution(context, record, project) as (binding, client):
            compiled, methods = self._compile_methods(
                context,
                project=project,
                binding=binding,
                record=record,
            )
            prior_context = self._prior_context_artifacts(context, client)
            compiled_task = compiled.tasks[0]
            legacy_payloads = compiled_task.evolution_job_payloads_for_round(
                0,
                methods,
                dataset_artifact_id=dataset.artifact_id,
                context_artifact_ids=prior_context,
            )
            outputs = tuple(
                self._run_one_method(
                    client,
                    spec=spec,
                    legacy_payload=legacy_payload,
                )
                for spec, legacy_payload in zip(
                    methods,
                    legacy_payloads,
                    strict=True,
                )
            )
        return outputs

    def validate_outputs(
        self,
        context: ScienceSuccessorPreparationContextV2,
        dataset: SealedTranscriptDatasetV2,
        outputs: tuple[ScienceMethodOutputV2, ...],
    ) -> ValidatedScienceOutputsV2:
        self._require_running()
        expected = tuple(
            (item.target_id, item.method_id, item.output_artifact_type)
            for item in context.plan.enabled_methods
        )
        actual = tuple((item.target_id, item.method_id, item.artifact_type) for item in outputs)
        if actual != expected:
            raise ScienceSuccessorPreparationV2Error(
                "method outputs do not exactly cover the successor plan"
            )
        manifest = {
            "artifacts": [item.model_dump(mode="json") for item in outputs],
            "dataset": dataset.model_dump(mode="json"),
            "evolution_revision_contract_version": "2",
            "predecessor_project_head_id": (
                context.task.admission.predecessor_project_head.project_head_id
            ),
            "project_id": context.task.project_id,
            "successor_transition_id": (context.transition.transition.successor_transition_id),
        }
        digest = canonical_digest(manifest)
        revision = m2.EvolutionRevisionRefV2(
            evolution_revision_id=f"evolution-{digest}",
            project_id=context.task.project_id,
            manifest_sha256=digest,
            artifact_count=len(outputs),
        )
        return ValidatedScienceOutputsV2(
            project_id=context.task.project_id,
            successor_transition_id=(context.transition.transition.successor_transition_id),
            predecessor_project_head_id=(
                context.task.admission.predecessor_project_head.project_head_id
            ),
            dataset=dataset,
            outputs=outputs,
            evolution_revision=revision,
        )

    def materialize_context(
        self,
        context: ScienceSuccessorPreparationContextV2,
        validated: ValidatedScienceOutputsV2,
    ) -> SuccessorMaterializationV2:
        self._require_running()
        record, _result, project = self._authority(context)
        assert record.evidence is not None
        output_ids = tuple(item.artifact_id for item in validated.outputs)
        with self._evolution(context, record, project) as (_binding, client):
            request = ContextProjectionResolveRequest(
                task_id=context.task.task_id,
                instruction=project.config.task.objective,
                successor_transition_id=(context.transition.transition.successor_transition_id),
                predecessor_project_head_id=(
                    context.task.admission.predecessor_project_head.project_head_id
                ),
                agent={
                    "harness": "codex",
                    "settings": {"auth_mode": "subscription"},
                },
                base_model=project.config.execution.codex_model,
                policy_version=record.evidence.policy_version,
                rollout_step=0,
                metadata={
                    "task_tags": [
                        "openevo_run_task:"
                        f"{context.accepted_attempt.attempt_id}:"
                        f"{context.task.task_id}"
                    ],
                    "evolution": {"context_artifact_ids": output_ids},
                },
                execution_profile=execution_profile_for_release_mode(
                    project.config.execution.mode
                ),
                destination_roots=RuntimeDestinationRoots(
                    target_data="/openevo/session/evolution",
                    harness_skills="/openevo/session/evolution/skills",
                    harness_instruction=MANAGED_WORKSPACE,
                ),
            )
            raw_materialized = client.create_materialized_context(request.model_dump(mode="json"))
            materialized = MaterializedContext.model_validate_json(
                json.dumps(
                    raw_materialized,
                    ensure_ascii=True,
                    allow_nan=False,
                    sort_keys=True,
                    separators=(",", ":"),
                )
            )
        self._require_running()
        if (
            materialized.registry_digest != context.task.admission.registry_sha256
            or materialized.successor_transition_id
            != context.transition.transition.successor_transition_id
            or materialized.predecessor_project_head_id
            != context.task.admission.predecessor_project_head.project_head_id
            or materialized.selection.artifact_ids != output_ids
        ):
            raise ScienceSuccessorPreparationV2Error(
                "materialized context differs from validated successor outputs"
            )
        materialized_sha256 = canonical_digest(materialized)
        predecessor_runtime = (
            context.task.admission.predecessor_project_head.runtime_context_snapshot
        )
        runtime_manifest = {
            "evolution_revision": validated.evolution_revision.model_dump(mode="json"),
            "materialized_context_id": materialized.context_id,
            "materialized_context_manifest_sha256": materialized_sha256,
            "project_id": context.task.project_id,
            "registry_sha256": materialized.registry_digest,
            "runtime_context_contract_version": "2",
            "runtime_contract_sha256": predecessor_runtime.runtime_contract_sha256,
            "selected_artifact_ids": list(output_ids),
            "successor_transition_id": (context.transition.transition.successor_transition_id),
        }
        runtime_sha256 = canonical_digest(runtime_manifest)
        runtime = m2.RuntimeContextSnapshotRefV2(
            runtime_context_snapshot_id=f"runtime-context-{runtime_sha256}",
            project_id=context.task.project_id,
            evolution_revision_id=validated.evolution_revision.evolution_revision_id,
            evolution_revision_manifest_sha256=(validated.evolution_revision.manifest_sha256),
            registry_sha256=materialized.registry_digest,
            runtime_contract_sha256=predecessor_runtime.runtime_contract_sha256,
            manifest_sha256=runtime_sha256,
        )
        return SuccessorMaterializationV2(
            project_id=context.task.project_id,
            successor_transition_id=(context.transition.transition.successor_transition_id),
            predecessor_project_head_id=(
                context.task.admission.predecessor_project_head.project_head_id
            ),
            materialized_context_id=materialized.context_id,
            materialized_context_manifest_sha256=materialized_sha256,
            runtime_context_snapshot=runtime,
        )

    def capture_workspace_result(
        self,
        context: ScienceSuccessorPreparationContextV2,
    ) -> AcceptedWorkspaceResultV2:
        self._require_running()
        record, result, project = self._authority(context)
        if result.workspace_result is None or record.evidence is None:
            raise ScienceSuccessorPreparationV2Error("captured Attempt has no workspace result")
        receipt = WorkspaceResultReceiptV2.model_validate(
            result.workspace_result.model_dump(mode="python")
        )
        authoritative = self._handoffs.get_result(receipt.handoff_id)
        if (
            authoritative != receipt
            or receipt.result_manifest_sha256 != record.evidence.workspace_result_manifest_sha256
            or receipt.output_archive.content_sha256 != record.evidence.workspace_archive_sha256
        ):
            raise ScienceSuccessorPreparationV2Error(
                "workspace result differs from captured execution evidence"
            )
        predecessor = context.task.admission.predecessor_project_head
        archive = receipt.output_archive
        chunk_size = min(m2.MAX_WORKSPACE_CHUNK_BYTES, archive.byte_size)
        chunk_count = (archive.byte_size + chunk_size - 1) // chunk_size
        key_seed = receipt.result_manifest_sha256[:32]
        session, _ = self._workspaces.create_upload(
            context.task.project_id,
            m2.WorkspaceUploadCreateV2(
                expected_project_head_id=predecessor.project_head_id,
                expected_project_head_manifest_sha256=predecessor.manifest_sha256,
                expected_project_config_sha256=project.project_config_sha256,
                archive=archive,
                chunk_byte_size=chunk_size,
                chunk_count=chunk_count,
            ),
            idempotency_key=f"successor-workspace-{key_seed}",
            now=self._clock(),
        )
        session = self._workspaces.get_upload(
            context.task.project_id,
            session.upload_id,
        )
        if session.state != "finalized":
            with self._handoffs.open_result(receipt) as stream:
                stream.seek(session.accepted_byte_size)
                while session.next_chunk_index < session.chunk_count:
                    self._require_running()
                    expected_size = min(
                        session.chunk_byte_size,
                        archive.byte_size - session.accepted_byte_size,
                    )
                    chunk = stream.read(expected_size)
                    if len(chunk) != expected_size:
                        raise ScienceSuccessorPreparationV2Error(
                            "workspace result ended before its declared byte size"
                        )
                    session, _ = self._workspaces.put_chunk(
                        context.task.project_id,
                        session.upload_id,
                        chunk_index=session.next_chunk_index,
                        chunk=chunk,
                        chunk_sha256=hashlib.sha256(chunk).hexdigest(),
                        chunk_byte_size=len(chunk),
                        if_match=session.etag,
                        idempotency_key=(
                            f"successor-workspace-{key_seed}-chunk-{session.next_chunk_index}"
                        ),
                        now=self._clock(),
                    )
                if stream.read(1):
                    raise ScienceSuccessorPreparationV2Error(
                        "workspace result exceeds its declared byte size"
                    )
            session, _ = self._workspaces.finalize_upload(
                context.task.project_id,
                session.upload_id,
                m2.WorkspaceUploadFinalizeV2(expected_content_sha256=archive.content_sha256),
                if_match=session.etag,
                idempotency_key=f"successor-workspace-{key_seed}-finalize",
                now=self._clock(),
            )
        snapshot = session.workspace_snapshot
        if (
            session.state != "finalized"
            or snapshot is None
            or snapshot.project_id != context.task.project_id
            or snapshot.entry_count != archive.entry_count
            or snapshot.byte_size != archive.extracted_byte_size
        ):
            raise ScienceSuccessorPreparationV2Error(
                "workspace snapshot differs from the accepted result archive"
            )
        self._handoffs.mark_consumed(receipt)
        return AcceptedWorkspaceResultV2(
            project_id=context.task.project_id,
            task_id=context.task.task_id,
            accepted_attempt_id=context.accepted_attempt.attempt_id,
            workspace_snapshot=snapshot,
        )

    def _authority(
        self,
        context: ScienceSuccessorPreparationContextV2,
    ) -> tuple[ScienceAttemptExecutionRecordV2, Any, ProjectRecordV2]:
        if type(context) is not ScienceSuccessorPreparationContextV2:
            raise TypeError("v2 successor preparation requires its exact context")
        record = self._ledger.get_attempt_execution(
            context.task.task_id,
            context.accepted_attempt.attempt_id,
        )
        result = self._ledger.get_captured_session_result(
            context.task.task_id,
            context.accepted_attempt.attempt_id,
        )
        project = self._catalog.get_project(context.task.project_id)
        if (
            type(project) is not ProjectRecordV2
            or record.state != "captured"
            or record.receipt is None
            or record.evidence is None
            or record.successor_plan != context.plan
            or project.project_config_sha256 != context.task.admission.project_config_sha256
            or m2.project_config_sha256_for(project.config) != project.project_config_sha256
        ):
            raise ScienceSuccessorPreparationV2Error(
                "successor preparation authority is incomplete or changed"
            )
        return record, result, project

    @contextmanager
    def _evolution(
        self,
        context: ScienceSuccessorPreparationContextV2,
        record: ScienceAttemptExecutionRecordV2,
        project: ProjectRecordV2,
    ) -> Iterator[tuple[ServiceRunBinding, EvolutionClientProtocol]]:
        self._require_running()
        assert record.receipt is not None
        snapshot, lease = self._services.ensure_run_binding(
            ServiceExecutionMode.CODEX_SUBSCRIPTION_TRANSCRIPT,
            codex_model=project.config.execution.codex_model,
            runtime_image=MANAGED_RUNTIME_IMAGES["managed_science"],
        )
        binding = getattr(lease, "binding", None)
        if (
            lease is None
            or type(binding) is not ServiceRunBinding
            or getattr(snapshot, "run_ready", False) is not True
            or binding.registry_digest != context.task.admission.registry_sha256
            or binding.registry_digest != record.receipt.registry_sha256
            or binding.framework_lock_digest != record.receipt.framework_lock_sha256
            or binding.runtime_identity_digest != record.receipt.runtime_identity_sha256
        ):
            if lease is not None:
                close = getattr(lease, "close", None)
                if callable(close):
                    close()
            raise ScienceSuccessorPreparationV2Error(
                "successor service authority changed after Attempt execution"
            )
        self._require_running()
        client = self._evolution_factory(binding)
        try:
            yield binding, client
        finally:
            close = getattr(client, "close", None)
            if callable(close):
                close()
            lease.close()

    def _compile_methods(
        self,
        context: ScienceSuccessorPreparationContextV2,
        *,
        project: ProjectRecordV2,
        binding: ServiceRunBinding,
        record: ScienceAttemptExecutionRecordV2,
    ) -> tuple[CompiledExperiment, tuple[CompiledEvolutionMethodSpec, ...]]:
        assert record.evidence is not None
        prior_dataset_ids = self._ledger.prior_dataset_artifact_ids_for_head(
            context.task.admission.predecessor_project_head.project_head_id
        )
        compiled = compile_science_evolution_experiment_v2(
            task=context.task,
            attempt=context.accepted_attempt,
            project=project,
            binding=binding,
            registry=self._registry,
            prior_dataset_artifact_ids=prior_dataset_ids,
        )
        methods = tuple(
            sorted(
                compiled.evolution_methods_for_round(
                    0,
                    prior_dataset_artifact_ids=prior_dataset_ids,
                    task_id=context.task.task_id,
                ),
                key=lambda item: item.target_id,
            )
        )
        plan = compiled.evolution_plan_for_round(
            0,
            prior_dataset_artifact_ids=prior_dataset_ids,
            task_id=context.task.task_id,
        )
        expected_methods = tuple(
            (item.target_id, item.method, item.artifact_type) for item in methods
        )
        saved_methods = tuple(
            (item.target_id, item.method_id, item.output_artifact_type)
            for item in context.plan.enabled_methods
        )
        if (
            expected_methods != saved_methods
            or compiled.tasks[0].policy_version_for_round(0) != record.evidence.policy_version
            or (
                plan.registry_snapshot_digest != methods[0].registry_snapshot_digest
                if methods
                else plan.selections != ()
            )
        ):
            raise ScienceSuccessorPreparationV2Error(
                "successor method compilation changed after terminal capture"
            )
        return compiled, methods

    def _prior_context_artifacts(
        self,
        context: ScienceSuccessorPreparationContextV2,
        client: EvolutionClientProtocol,
    ) -> dict[str, list[str]]:
        result = {key: [] for key in _CONTEXT_ARTIFACT_TYPES}
        predecessor_id = context.task.admission.predecessor_project_head.project_head_id
        result["dataset"] = list(self._ledger.prior_dataset_artifact_ids_for_head(predecessor_id))
        commit = self._ledger.successor_commit_for_project_head(predecessor_id)
        if commit is None:
            return result
        for artifact_id in commit.manifest.method_artifact_ids:
            artifact = ArtifactResponse.model_validate(client.get_artifact(artifact_id))
            artifact_type = str(artifact.type)
            if (
                artifact.artifact_id != artifact_id
                or artifact.state is not ArtifactState.ACTIVE
                or artifact.promoted is not True
                or artifact_type not in result
                or artifact_type == "dataset"
            ):
                raise ScienceSuccessorPreparationV2Error(
                    "predecessor context contains a non-target artifact"
                )
            result[artifact_type].append(artifact.artifact_id)
        for values in result.values():
            values.sort()
        return result

    def _run_one_method(
        self,
        client: EvolutionClientProtocol,
        *,
        spec: CompiledEvolutionMethodSpec,
        legacy_payload: dict[str, Any],
    ) -> ScienceMethodOutputV2:
        self._require_running()
        request = _plan_bound_request(spec, legacy_payload)
        created = JobCreateResponse.model_validate(
            client.create_plan_bound_job(request.model_dump(mode="json"))
        )
        terminal: Mapping[str, Any] | None = None
        for poll_index in range(self._max_poll_attempts):
            if poll_index and self._stop.wait(self._poll_interval):
                self._require_running()
            self._require_running()
            observed = client.get_internal_job_result(created.job_id)
            if not isinstance(observed, Mapping):
                raise ScienceSuccessorPreparationV2Error(
                    "managed evolution job returned a non-object result"
                )
            state = observed.get("state")
            if state == JobState.SUCCEEDED.value:
                terminal = observed
                break
            if state in {
                JobState.FAILED.value,
                JobState.CANCELLED.value,
                JobState.EXPIRED.value,
            }:
                raise ScienceSuccessorPreparationV2Error(
                    "managed evolution method did not succeed"
                )
        if terminal is None:
            raise ScienceSuccessorPreparationV2Error(
                "managed evolution method did not reach a terminal result"
            )
        if terminal.get("job_id") != created.job_id or terminal.get("error") is not None:
            raise ScienceSuccessorPreparationV2Error(
                "managed evolution terminal identity is invalid"
            )
        artifact_ids = terminal.get("artifact_ids")
        raw_outputs = terminal.get("outputs")
        if (
            not isinstance(artifact_ids, list)
            or not isinstance(raw_outputs, list)
            or artifact_ids
            != [item.get("artifact_id") for item in raw_outputs if isinstance(item, dict)]
            or len(artifact_ids) != len(set(artifact_ids))
        ):
            raise ScienceSuccessorPreparationV2Error(
                "managed evolution output inventory is inconsistent"
            )
        allowed_types = set(self._registry.snapshot.methods[spec.method].output_artifact_types)
        selected = [
            item
            for item in raw_outputs
            if isinstance(item, dict) and item.get("type") == spec.artifact_type
        ]
        if len(selected) != 1 or any(
            not isinstance(item, dict) or item.get("type") not in allowed_types
            for item in raw_outputs
        ):
            raise ScienceSuccessorPreparationV2Error(
                "managed evolution outputs differ from the verified descriptor"
            )
        output = selected[0]
        digest = output.get("payload_manifest_digest")
        byte_size = output.get("payload_byte_size")
        artifact_id = output.get("artifact_id")
        if (
            not isinstance(artifact_id, str)
            or not isinstance(digest, str)
            or len(digest) != 64
            or any(character not in "0123456789abcdef" for character in digest)
            or not isinstance(byte_size, int)
            or isinstance(byte_size, bool)
            or not 0 <= byte_size <= m2.MAX_SNAPSHOT_BYTES
            or output.get("promoted") is not True
        ):
            raise ScienceSuccessorPreparationV2Error(
                "managed evolution target output evidence is invalid"
            )
        authoritative = ArtifactResponse.model_validate(client.get_artifact(artifact_id))
        if (
            authoritative.artifact_id != artifact_id
            or str(authoritative.type) != spec.artifact_type
            or authoritative.state is not ArtifactState.ACTIVE
            or authoritative.promoted is not True
            or authoritative.manifest != output.get("manifest")
        ):
            raise ScienceSuccessorPreparationV2Error(
                "managed evolution artifact changed after job completion"
            )
        return ScienceMethodOutputV2(
            target_id=spec.target_id,
            method_id=spec.method,
            artifact_id=artifact_id,
            artifact_type=spec.artifact_type,
            manifest_sha256=digest,
            byte_size=byte_size,
            execution_boundary="outside_inference",
        )


def _plan_bound_request(
    spec: CompiledEvolutionMethodSpec,
    legacy_payload: Mapping[str, Any],
) -> PlanBoundJobCreateRequest:
    config = legacy_payload.get("config")
    bindings = legacy_payload.get("input_bindings")
    if not isinstance(config, dict) or not isinstance(bindings, list):
        raise ScienceSuccessorPreparationV2Error("compiled evolution job payload is incomplete")
    user_config = spec.selection.config()
    if any(config.get(key) != value for key, value in user_config.items()):
        raise ScienceSuccessorPreparationV2Error(
            "compiled evolution method config changed after normalization"
        )
    core_config = {key: value for key, value in config.items() if key not in user_config}
    core_config["promoted"] = True
    return PlanBoundJobCreateRequest(
        plan=spec.plan,
        target_id=spec.target_id,
        job_type=spec.method,
        input_bindings=tuple(bindings),
        core_config=core_config,
        priority=100,
    )


__all__ = [
    "ProductionScienceSuccessorPreparerV2",
    "ScienceSuccessorPreparationV2Error",
]
