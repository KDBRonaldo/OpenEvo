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
import re
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
    ScienceSuccessorCleanupContextV2,
    ScienceSuccessorCleanupReceiptV2,
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
from openevo.evolution.revisions import (
    AtomicEvolutionAbandonManifestV2,
    AtomicSuccessorManifestV2,
    SuccessorArtifactContributionV2,
)
from openevo.experiments.clients import (
    EvolutionClientProtocol,
    EvolutionHttpClient,
)
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
    def __init__(
        self,
        message: str,
        *,
        retryable: bool = False,
    ) -> None:
        super().__init__(message)
        self.retryable = retryable


def _successor_dataset_idempotency_key(
    context: ScienceSuccessorPreparationContextV2,
    record: ScienceAttemptExecutionRecordV2,
) -> str:
    if record.receipt is None or record.evidence is None:
        raise ScienceSuccessorPreparationV2Error(
            "captured Attempt is missing dataset source evidence"
        )
    identity = canonical_digest(
        {
            "schema_version": "1",
            "project_id": context.task.project_id,
            "task_id": context.task.task_id,
            "task_admission_id": (context.task.admission.task_admission_id),
            "accepted_attempt_id": context.accepted_attempt.attempt_id,
            "rollout_task_id": record.receipt.rollout_task_id,
            "session_id": record.receipt.session_id,
            "session_result_sha256": (record.evidence.session_result_sha256),
        }
    )
    return f"science-successor-dataset-{identity}"


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
            raise ScienceSuccessorPreparationV2Error(
                "successor preparation is stopping",
                retryable=True,
            )

    def seal_dataset(
        self,
        context: ScienceSuccessorPreparationContextV2,
    ) -> SealedTranscriptDatasetV2:
        self._require_running()
        record, result, project = self._authority(context)
        assert record.evidence is not None
        assert record.receipt is not None
        dataset_name = (
            f"{context.task.project_id}:{context.task.task_id}:"
            f"{context.accepted_attempt.attempt_id}"
        )
        idempotency_key = _successor_dataset_idempotency_key(
            context,
            record,
        )
        with self._evolution(context, record, project) as (_binding, client):
            raw = client.create_dataset(
                {
                    "idempotency_key": idempotency_key,
                    "name": dataset_name,
                    "purpose": "openevo_science_successor_v2",
                    "query": {
                        "source": "openevo",
                        "event_types": ["openevo.session_completed"],
                        "status": ["COMPLETED"],
                        "policy_version": record.evidence.policy_version,
                        "source_event_id": (f"session:{record.receipt.session_id}"),
                        "task_id": record.receipt.rollout_task_id,
                        "session_id": record.receipt.session_id,
                    },
                    "limits": {
                        "max_events": 1,
                        "max_traces": record.evidence.transcript_record_count,
                    },
                }
            )
            dataset = DatasetCreateResponse.model_validate(raw)
            artifact = ArtifactResponse.model_validate(client.get_artifact(dataset.artifact_id))
        self._require_running()
        return self._sealed_dataset_receipt(
            context,
            dataset=dataset,
            artifact=artifact,
            expected_name=dataset_name,
            expected_idempotency_key=idempotency_key,
            expected_record=record,
            expected_record_count=record.evidence.transcript_record_count,
            captured_trace_count=len(result.trajectory.traces),
        )

    def recover_dataset(
        self,
        context: ScienceSuccessorPreparationContextV2,
        *,
        dataset_id: str,
        manifest_sha256: str,
    ) -> SealedTranscriptDatasetV2:
        self._require_running()
        record, result, project = self._authority(context)
        assert record.evidence is not None
        dataset_name = (
            f"{context.task.project_id}:{context.task.task_id}:"
            f"{context.accepted_attempt.attempt_id}"
        )
        idempotency_key = _successor_dataset_idempotency_key(
            context,
            record,
        )
        with self._evolution(context, record, project) as (_binding, client):
            dataset = DatasetCreateResponse.model_validate(client.get_dataset(dataset_id))
            artifact = ArtifactResponse.model_validate(client.get_artifact(dataset.artifact_id))
        self._require_running()
        receipt = self._sealed_dataset_receipt(
            context,
            dataset=dataset,
            artifact=artifact,
            expected_name=dataset_name,
            expected_idempotency_key=idempotency_key,
            expected_record=record,
            expected_record_count=record.evidence.transcript_record_count,
            captured_trace_count=len(result.trajectory.traces),
        )
        if receipt.dataset_id != dataset_id or receipt.manifest_sha256 != manifest_sha256:
            raise ScienceSuccessorPreparationV2Error(
                "recovered transcript dataset differs from the transition journal"
            )
        return receipt

    def _sealed_dataset_receipt(
        self,
        context: ScienceSuccessorPreparationContextV2,
        *,
        dataset: DatasetCreateResponse,
        artifact: ArtifactResponse,
        expected_name: str,
        expected_idempotency_key: str,
        expected_record: ScienceAttemptExecutionRecordV2,
        expected_record_count: int,
        captured_trace_count: int,
    ) -> SealedTranscriptDatasetV2:
        assert expected_record.receipt is not None
        assert expected_record.evidence is not None
        receipt = expected_record.receipt
        evidence = expected_record.evidence
        manifest = artifact.manifest
        event_ids = manifest.get("event_ids")
        source_event_evidence = manifest.get("source_event_evidence")
        expected_query = {
            "source": "openevo",
            "event_types": ["openevo.session_completed"],
            "status": ["COMPLETED"],
            "reward_min": None,
            "policy_version": evidence.policy_version,
            "task_tags": [],
            "source_event_id": f"session:{receipt.session_id}",
            "task_id": receipt.rollout_task_id,
            "session_id": receipt.session_id,
        }
        expected_query = {key: value for key, value in expected_query.items() if value is not None}
        if (
            dataset.event_count != 1
            or dataset.trace_count != expected_record_count
            or dataset.trace_count != captured_trace_count
            or artifact.artifact_id != dataset.artifact_id
            or artifact.type is not ArtifactType.DATASET
            or artifact.state is not ArtifactState.ACTIVE
            or artifact.promoted is not True
            or artifact.name != expected_name
            or artifact.compatibility != {"purpose": "openevo_science_successor_v2"}
            or manifest.get("dataset_id") != dataset.dataset_id
            or manifest.get("name") != expected_name
            or manifest.get("purpose") != "openevo_science_successor_v2"
            or manifest.get("query") != expected_query
            or manifest.get("limits")
            != {
                "max_events": 1,
                "max_traces": expected_record_count,
            }
            or not isinstance(event_ids, list)
            or len(event_ids) != 1
            or not isinstance(event_ids[0], str)
            or not event_ids[0]
            or manifest.get("event_count") != 1
            or manifest.get("trace_count") != dataset.trace_count
            or manifest.get("records_path") != "records.jsonl"
            or not isinstance(manifest.get("records_uri"), str)
            or not manifest["records_uri"]
            or type(manifest.get("records_byte_size")) is not int
            or manifest["records_byte_size"] < 0
            or not isinstance(manifest.get("records_sha256"), str)
            or re.fullmatch(
                r"[0-9a-f]{64}",
                manifest["records_sha256"],
            )
            is None
            or manifest.get("create_identity") != expected_idempotency_key
            or source_event_evidence
            != {
                "event_id": event_ids[0],
                "source": "openevo",
                "event_type": "openevo.session_completed",
                "source_event_id": f"session:{receipt.session_id}",
                "task_id": receipt.rollout_task_id,
                "session_id": receipt.session_id,
                "session_result_sha256": evidence.session_result_sha256,
            }
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
            (
                prior_context,
                _prior_composition,
                prior_owner_by_target,
            ) = self._prior_context_artifacts(context, client)
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
                    transition_attempt_id=(context.transition_attempt.transition_attempt_id),
                    transition_attempt_ordinal=(context.transition_attempt.ordinal),
                    successor_transition_id=(
                        context.transition.transition.successor_transition_id
                    ),
                    predecessor_successor_transition_id=(
                        prior_owner_by_target.get(spec.target_id)
                    ),
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
        record, _result, project = self._authority(context)
        expected = tuple(
            (item.target_id, item.method_id, item.output_artifact_type)
            for item in context.plan.enabled_methods
        )
        actual = tuple((item.target_id, item.method_id, item.artifact_type) for item in outputs)
        if actual != expected:
            raise ScienceSuccessorPreparationV2Error(
                "method outputs do not exactly cover the successor plan"
            )
        with self._evolution(
            context,
            record,
            project,
        ) as (_binding, client):
            (
                _prior_context,
                inherited,
                _prior_owner_by_target,
            ) = self._prior_context_artifacts(context, client)
        composition_by_target = {item.target_id: item for item in inherited}
        transition_id = context.transition.transition.successor_transition_id
        for output in outputs:
            composition_by_target[output.target_id] = SuccessorArtifactContributionV2(
                target_id=output.target_id,
                artifact_id=output.artifact_id,
                artifact_type=output.artifact_type,
                owner_successor_transition_id=transition_id,
                origin="produced",
            )
        composition = tuple(
            composition_by_target[target_id] for target_id in sorted(composition_by_target)
        )
        if not context.plan.enabled_methods:
            predecessor_revision = (
                context.task.admission.predecessor_project_head.evolution_revision
            )
            return ValidatedScienceOutputsV2(
                project_id=context.task.project_id,
                successor_transition_id=transition_id,
                predecessor_project_head_id=(
                    context.task.admission.predecessor_project_head.project_head_id
                ),
                dataset=dataset,
                outputs=outputs,
                composition=composition,
                evolution_revision=predecessor_revision,
            )
        manifest = {
            "artifacts": [item.model_dump(mode="json") for item in composition],
            "dataset": dataset.model_dump(mode="json"),
            "evolution_revision_contract_version": "2",
            "method_outputs": [item.model_dump(mode="json") for item in outputs],
            "predecessor_evolution_revision": (
                context.task.admission.predecessor_project_head.evolution_revision.model_dump(
                    mode="json"
                )
            ),
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
            artifact_count=len(composition),
        )
        return ValidatedScienceOutputsV2(
            project_id=context.task.project_id,
            successor_transition_id=(context.transition.transition.successor_transition_id),
            predecessor_project_head_id=(
                context.task.admission.predecessor_project_head.project_head_id
            ),
            dataset=dataset,
            outputs=outputs,
            composition=composition,
            evolution_revision=revision,
        )

    def materialize_context(
        self,
        context: ScienceSuccessorPreparationContextV2,
        validated: ValidatedScienceOutputsV2,
    ) -> SuccessorMaterializationV2:
        self._require_running()
        if not context.plan.enabled_methods:
            return self._inherited_materialization(
                context,
                validated,
            )
        record, _result, project = self._authority(context)
        assert record.evidence is not None
        output_ids = tuple(item.artifact_id for item in validated.composition)
        owner_transition_ids = tuple(
            item.owner_successor_transition_id for item in validated.composition
        )
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
                    "settings": {
                        "auth_mode": (
                            "subscription"
                            if project.config.execution.mode == "codex_subscription_transcript"
                            else "proxy"
                        )
                    },
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
                    "evolution": {
                        "context_artifact_ids": output_ids,
                        "context_artifact_owner_transition_ids": (owner_transition_ids),
                    },
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

    def _inherited_materialization(
        self,
        context: ScienceSuccessorPreparationContextV2,
        validated: ValidatedScienceOutputsV2,
    ) -> SuccessorMaterializationV2:
        predecessor = context.task.admission.predecessor_project_head
        artifact_ids = tuple(item.artifact_id for item in validated.composition)
        if (
            validated.outputs
            or any(item.origin != "inherited" for item in validated.composition)
            or validated.evolution_revision != predecessor.evolution_revision
            or len(artifact_ids) != predecessor.evolution_revision.artifact_count
        ):
            raise ScienceSuccessorPreparationV2Error(
                "no-evolution successor changed predecessor evolution authority"
            )
        commit = self._ledger.successor_commit_for_project_head(predecessor.project_head_id)
        runtime_source: str
        source_transition_id: str | None
        source_predecessor_id: str | None
        materialized_context_id: str | None
        materialized_manifest_sha256: str | None
        if commit is None:
            if (
                predecessor.generation != 0
                or predecessor.evolution_revision.artifact_count != 0
                or artifact_ids
            ):
                raise ScienceSuccessorPreparationV2Error(
                    "no-evolution successor lacks exact predecessor receipt"
                )
            runtime_source = "empty_inherited"
            source_transition_id = None
            source_predecessor_id = None
            materialized_context_id = None
            materialized_manifest_sha256 = None
        else:
            manifest = commit.manifest
            if manifest.method_artifact_ids != artifact_ids or (
                manifest.artifacts
                and tuple(
                    (
                        item.target_id,
                        item.artifact_id,
                        item.artifact_type,
                        item.owner_successor_transition_id,
                    )
                    for item in manifest.artifacts
                )
                != tuple(
                    (
                        item.target_id,
                        item.artifact_id,
                        item.artifact_type,
                        item.owner_successor_transition_id,
                    )
                    for item in validated.composition
                )
            ):
                raise ScienceSuccessorPreparationV2Error(
                    "no-evolution successor artifact receipt changed"
                )
            if type(manifest) is AtomicSuccessorManifestV2:
                if manifest.runtime_context_source == "materialized_new":
                    runtime_source = "materialized_inherited"
                    source_transition_id = manifest.successor_transition_id
                    source_predecessor_id = manifest.predecessor_project_head_id
                    materialized_context_id = manifest.materialized_context_id
                    materialized_manifest_sha256 = manifest.materialized_context_manifest_sha256
                else:
                    runtime_source = manifest.runtime_context_source
                    source_transition_id = manifest.materialized_source_successor_transition_id
                    source_predecessor_id = (
                        manifest.materialized_source_predecessor_project_head_id
                    )
                    materialized_context_id = manifest.materialized_context_id
                    materialized_manifest_sha256 = manifest.materialized_context_manifest_sha256
            elif type(manifest) is AtomicEvolutionAbandonManifestV2:
                runtime_source = manifest.runtime_context_source
                source_transition_id = manifest.materialized_source_successor_transition_id
                source_predecessor_id = manifest.materialized_source_predecessor_project_head_id
                materialized_context_id = manifest.materialized_context_id
                materialized_manifest_sha256 = manifest.materialized_context_manifest_sha256
            else:  # pragma: no cover - closed receipt union
                raise ScienceSuccessorPreparationV2Error(
                    "no-evolution successor receipt type is unsupported"
                )
        return SuccessorMaterializationV2(
            project_id=context.task.project_id,
            successor_transition_id=(context.transition.transition.successor_transition_id),
            predecessor_project_head_id=(predecessor.project_head_id),
            runtime_context_source=runtime_source,
            materialized_source_successor_transition_id=(source_transition_id),
            materialized_source_predecessor_project_head_id=(source_predecessor_id),
            materialized_context_id=materialized_context_id,
            materialized_context_manifest_sha256=(materialized_manifest_sha256),
            runtime_context_snapshot=(predecessor.runtime_context_snapshot),
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

    def discard_transition_outputs(
        self,
        context: ScienceSuccessorCleanupContextV2,
    ) -> ScienceSuccessorCleanupReceiptV2:
        """Discard non-active outputs after the Core has committed abandon."""

        self._require_running()
        record, _result, project = self._cleanup_authority(context)
        transition_id = context.transition.transition.successor_transition_id
        with self._evolution(context, record, project) as (_binding, client):
            raw = client.discard_successor_transition_outputs(transition_id)
        self._require_running()
        discarded = raw.get("discarded_artifact_ids") if type(raw) is dict else None
        discarded_contexts = (
            raw.get("discarded_materialized_context_ids") if type(raw) is dict else None
        )
        if (
            type(raw) is not dict
            or set(raw)
            != {
                "successor_transition_id",
                "discarded_artifact_ids",
                "discarded_materialized_context_ids",
            }
            or raw.get("successor_transition_id") != transition_id
            or not isinstance(discarded, list)
            or not isinstance(discarded_contexts, list)
            or any(
                not isinstance(artifact_id, str) or not artifact_id for artifact_id in discarded
            )
            or any(
                not isinstance(context_id, str) or not context_id
                for context_id in discarded_contexts
            )
            or len(discarded) != len(set(discarded))
            or len(discarded_contexts) != len(set(discarded_contexts))
        ):
            raise ScienceSuccessorPreparationV2Error(
                "discard receipt differs from the requested transition"
            )
        try:
            return ScienceSuccessorCleanupReceiptV2(
                successor_transition_id=transition_id,
                discarded_artifact_ids=tuple(discarded),
                discarded_materialized_context_ids=tuple(discarded_contexts),
            )
        except ValueError as exc:
            raise ScienceSuccessorPreparationV2Error(
                "discard receipt differs from the requested transition"
            ) from exc

    def _authority(
        self,
        context: ScienceSuccessorPreparationContextV2,
    ) -> tuple[ScienceAttemptExecutionRecordV2, Any, ProjectRecordV2]:
        if type(context) is not ScienceSuccessorPreparationContextV2:
            raise TypeError("v2 successor preparation requires its exact context")
        return self._validated_authority(context)

    def _cleanup_authority(
        self,
        context: ScienceSuccessorCleanupContextV2,
    ) -> tuple[ScienceAttemptExecutionRecordV2, Any, ProjectRecordV2]:
        if type(context) is not ScienceSuccessorCleanupContextV2:
            raise TypeError("v2 successor cleanup requires its exact context")
        return self._validated_authority(context)

    def _validated_authority(
        self,
        context: ScienceSuccessorPreparationContextV2 | ScienceSuccessorCleanupContextV2,
    ) -> tuple[ScienceAttemptExecutionRecordV2, Any, ProjectRecordV2]:
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
        context: ScienceSuccessorPreparationContextV2 | ScienceSuccessorCleanupContextV2,
        record: ScienceAttemptExecutionRecordV2,
        project: ProjectRecordV2,
    ) -> Iterator[tuple[ServiceRunBinding, EvolutionClientProtocol]]:
        self._require_running()
        assert record.receipt is not None
        if project.config.execution.mode == "self-deployed":
            snapshot, lease = self._services.ensure_run_binding(
                ServiceExecutionMode.SELF_DEPLOYED,
                model_ref=project.config.execution.model_profile_id,
                runtime_image=MANAGED_RUNTIME_IMAGES["managed_science"],
                total_timeout=7200.0,
            )
        else:
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
    ) -> tuple[
        dict[str, list[str]],
        tuple[SuccessorArtifactContributionV2, ...],
        dict[str, str],
    ]:
        result = {key: [] for key in _CONTEXT_ARTIFACT_TYPES}
        predecessor_id = context.task.admission.predecessor_project_head.project_head_id
        result["dataset"] = list(self._ledger.prior_dataset_artifact_ids_for_head(predecessor_id))
        commit = self._ledger.successor_commit_for_project_head(predecessor_id)
        if commit is None:
            return result, (), {}
        manifest = commit.manifest
        if type(manifest) not in {
            AtomicSuccessorManifestV2,
            AtomicEvolutionAbandonManifestV2,
        }:  # pragma: no cover - closed receipt union
            raise ScienceSuccessorPreparationV2Error(
                "predecessor context has an unsupported commit receipt"
            )
        stored_contributions = manifest.artifacts
        if (
            stored_contributions
            and tuple(item.artifact_id for item in stored_contributions)
            != manifest.method_artifact_ids
        ):
            raise ScienceSuccessorPreparationV2Error(
                "predecessor artifact composition differs from its receipt"
            )
        if type(manifest) is AtomicSuccessorManifestV2:
            legacy_owner = manifest.successor_transition_id
        else:
            legacy_owner = manifest.materialized_source_successor_transition_id
        contributions: list[SuccessorArtifactContributionV2] = []
        owner_by_target: dict[str, str] = {}
        for index, artifact_id in enumerate(manifest.method_artifact_ids):
            stored = None if not stored_contributions else stored_contributions[index]
            owner_transition_id = (
                legacy_owner if stored is None else stored.owner_successor_transition_id
            )
            if owner_transition_id is None:
                raise ScienceSuccessorPreparationV2Error(
                    "predecessor context has no artifact owner transition"
                )
            artifact = ArtifactResponse.model_validate(
                client.get_internal_successor_artifact(
                    owner_transition_id,
                    artifact_id,
                )
            )
            artifact_type = str(artifact.type)
            if stored is None:
                candidate_targets = tuple(
                    target.id
                    for target in self._registry.snapshot.targets.values()
                    if target.artifact_type == artifact_type
                )
                if len(candidate_targets) != 1:
                    raise ScienceSuccessorPreparationV2Error(
                        "legacy predecessor artifact has ambiguous target ownership"
                    )
                target_id = candidate_targets[0]
            else:
                target_id = stored.target_id
                target = self._registry.snapshot.targets.get(target_id)
                if (
                    stored.artifact_id != artifact_id
                    or stored.artifact_type != artifact_type
                    or target is None
                    or target.artifact_type != artifact_type
                ):
                    raise ScienceSuccessorPreparationV2Error(
                        "predecessor artifact differs from its typed contribution"
                    )
            if (
                artifact.artifact_id != artifact_id
                or artifact.state
                not in {
                    ArtifactState.ACTIVE,
                    ArtifactState.SEALED,
                }
                or artifact.promoted is not True
                or artifact_type not in result
                or artifact_type == "dataset"
                or target_id in owner_by_target
            ):
                raise ScienceSuccessorPreparationV2Error(
                    "predecessor context contains a non-target artifact"
                )
            result[artifact_type].append(artifact.artifact_id)
            owner_by_target[target_id] = owner_transition_id
            contributions.append(
                SuccessorArtifactContributionV2(
                    target_id=target_id,
                    artifact_id=artifact_id,
                    artifact_type=artifact_type,
                    owner_successor_transition_id=(owner_transition_id),
                    origin="inherited",
                )
            )
        for values in result.values():
            values.sort()
        ordered = tuple(
            sorted(
                contributions,
                key=lambda item: item.target_id,
            )
        )
        if (
            len(ordered)
            != context.task.admission.predecessor_project_head.evolution_revision.artifact_count
        ):
            raise ScienceSuccessorPreparationV2Error(
                "predecessor artifact composition is incomplete"
            )
        return result, ordered, owner_by_target

    def _run_one_method(
        self,
        client: EvolutionClientProtocol,
        *,
        spec: CompiledEvolutionMethodSpec,
        legacy_payload: dict[str, Any],
        transition_attempt_id: str,
        transition_attempt_ordinal: int,
        successor_transition_id: str,
        predecessor_successor_transition_id: str | None,
    ) -> ScienceMethodOutputV2:
        self._require_running()
        request = _plan_bound_request(
            spec,
            legacy_payload,
            successor_transition_id=successor_transition_id,
            predecessor_successor_transition_id=(predecessor_successor_transition_id),
        )
        created = JobCreateResponse.model_validate(
            client.create_plan_bound_job(request.model_dump(mode="json"))
        )
        if created.state in {
            JobState.FAILED,
            JobState.CANCELLED,
            JobState.EXPIRED,
        }:
            observed = client.get_internal_job_result(created.job_id)
            if (
                not isinstance(observed, Mapping)
                or observed.get("job_id") != created.job_id
                or observed.get("state") != created.state.value
                or type(observed.get("retryable")) is not bool
            ):
                raise ScienceSuccessorPreparationV2Error(
                    "initial managed evolution terminal authority is invalid"
                )
            if observed["retryable"] is not True:
                raise ScienceSuccessorPreparationV2Error(
                    "initial managed evolution job failed deterministically",
                    retryable=False,
                )
            if transition_attempt_ordinal < 2:
                raise ScienceSuccessorPreparationV2Error(
                    "initial managed evolution job was already terminal",
                    retryable=True,
                )
            created = JobCreateResponse.model_validate(
                client.retry_plan_bound_job(
                    created.job_id,
                    {
                        "retry_request_id": transition_attempt_id,
                        "plan_id": request.plan.plan_id,
                        "target_id": request.target_id,
                    },
                )
            )
            if created.state in {
                JobState.FAILED,
                JobState.CANCELLED,
                JobState.EXPIRED,
            }:
                raise ScienceSuccessorPreparationV2Error(
                    "managed evolution job retry did not requeue",
                    retryable=True,
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
                retryable = observed.get("retryable")
                if type(retryable) is not bool:
                    raise ScienceSuccessorPreparationV2Error(
                        "managed evolution failure classification is invalid"
                    )
                raise ScienceSuccessorPreparationV2Error(
                    "managed evolution method did not succeed",
                    retryable=retryable,
                )
        if terminal is None:
            raise ScienceSuccessorPreparationV2Error(
                "managed evolution method did not reach a terminal result",
                retryable=True,
            )
        if (
            terminal.get("job_id") != created.job_id
            or terminal.get("error") is not None
            or terminal.get("successor_transition_id") != successor_transition_id
        ):
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
    *,
    successor_transition_id: str,
    predecessor_successor_transition_id: str | None,
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
        successor_transition_id=successor_transition_id,
        predecessor_successor_transition_id=(predecessor_successor_transition_id),
        core_config=core_config,
        priority=100,
    )


__all__ = [
    "ProductionScienceSuccessorPreparerV2",
    "ScienceSuccessorPreparationV2Error",
]
