"""Closed evidence for one Core-owned v2 science Attempt execution.

The public v2 Task and Attempt objects intentionally contain no backend command,
host path, credential, or mutable execution details.  This module is the private
receipt boundary used by the Daemon run owner before a result may become the
authoritative input to a successor transition.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import math
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field, model_validator

from openevo.backend.contracts.v2 import models as m2
from openevo.backend.contracts.v2.store import ProjectRecordV2
from openevo.backend.run_admission import (
    EffectiveExecutionSettings,
    resolve_genesis_execution_snapshot,
)
from openevo.backend.runtime_context_binding_v2 import (
    runtime_context_binding_for_head,
)
from openevo.backend.science_successor import ScienceSuccessorPlanV2
from openevo.backend.science_successor import ScienceSuccessorMethodPlanV2
from openevo.backend.service_supervisor import ServiceExecutionMode, ServiceRunBinding
from openevo.backend.workspace_handoff_v2 import (
    WorkspaceHandoffBindingV2,
    WorkspaceHandoffRequestV2,
)
from openevo.evolution.framework import EvolutionPlan, canonical_digest
from openevo.evolution.revisions import AtomicSuccessorCommitV2
from openevo.evolution.framework.builtins import (
    VerifiedExecutableRegistry,
    require_verified_executable_registry,
)
from openevo.evolution.framework.profiles import execution_profile_for_release_mode
from openevo.experiments.compiler import (
    CompiledEvolutionMethodSpec,
    CompiledExperiment,
    compile_experiment,
)
from openevo.experiments.clients import RolloutClientProtocol, RolloutHttpClient
from openevo.experiments.models import ExperimentConfig
from openevo.harness.models import AgentSpec
from openevo.internal_auth import (
    GenerationBoundRunAdmissionCheck,
    RunAdmissionOperation,
)
from openevo.rollout.models import (
    CanonicalTaskRequest,
    SessionResult,
    SessionStatus,
    TaskRequest,
    TaskStatus,
    canonicalize_task_request,
)
from openevo.runtime.codex_isolation import (
    CODEX_SUBSCRIPTION_CONTRACT_KEY,
    codex_subscription_contract,
)
from openevo.runtime.managed import (
    MANAGED_HOME,
    MANAGED_PATH,
    MANAGED_RUNTIME_IMAGES,
    MANAGED_SUBSCRIPTION_PREPARE_COMMAND,
    MANAGED_WORKSPACE,
    require_immutable_managed_runtime_image,
)
from openevo.runtime.models import PrepareAction, RuntimeSpec
from openevo.trajectory.models import StrategySpec


_ID_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$"
_SHA256_PATTERN = r"^[0-9a-f]{64}$"
_ERROR_CODE_PATTERN = r"^[a-z][a-z0-9_]{0,127}$"
MAX_CAPTURED_SESSION_RESULT_BYTES = 64 * 1024 * 1024
_MAX_CAPTURED_RESULT_NODES = 1_000_000
_MAX_CAPTURED_RESULT_DEPTH = 64
_MANAGED_PROXY_CODEX_HOME = f"{MANAGED_HOME}/.codex"


class ScienceAttemptExecutionV2Error(RuntimeError):
    def __init__(self, code: str, *, retryable: bool) -> None:
        if (
            not isinstance(code, str)
            or not code
            or len(code) > 128
            or any(character not in "abcdefghijklmnopqrstuvwxyz0123456789_" for character in code)
        ):
            raise ValueError("science Attempt execution error code is invalid")
        super().__init__(code)
        self.code = code
        self.retryable = retryable


class ScienceAttemptCancelledV2(ScienceAttemptExecutionV2Error):
    def __init__(self) -> None:
        super().__init__("attempt_cancelled", retryable=True)


class _ProjectCatalogV2(Protocol):
    def get_project(self, project_id: str) -> ProjectRecordV2: ...


class _WorkspaceAuthorityV2(Protocol):
    def get_snapshot(self, workspace_snapshot_id: str) -> m2.WorkspaceSnapshotRefV2: ...

    def snapshot_path(self, snapshot: m2.WorkspaceSnapshotRefV2) -> Path: ...

    def archive_declaration(
        self,
        snapshot: m2.WorkspaceSnapshotRefV2,
    ) -> m2.WorkspaceArchiveDeclarationV2: ...


class _WorkspaceHandoffAuthorityV2(Protocol):
    def reserve(
        self,
        request: WorkspaceHandoffRequestV2,
        source_workspace: Path | str,
        *,
        now: datetime,
    ) -> WorkspaceHandoffBindingV2: ...


class _AttemptLedgerV2(Protocol):
    def successor_commit_for_project_head(
        self,
        project_head_id: str,
    ) -> AtomicSuccessorCommitV2 | None: ...

    def register_attempt_run_admission(
        self,
        *,
        task_id: str,
        attempt_id: str,
        check: object,
        allow_create: bool,
    ) -> bool: ...

    def mark_attempt_running(
        self,
        *,
        task_id: str,
        attempt_id: str,
        now: datetime,
    ) -> ScienceAttemptExecutionRecordV2: ...

    def record_terminal_attempt(
        self,
        *,
        task_id: str,
        attempt_id: str,
        receipt: ScienceAttemptExecutionReceiptV2,
        evidence: ScienceAttemptExecutionEvidenceV2,
        successor_plan: ScienceSuccessorPlanV2,
        terminal_result: SessionResult,
        now: datetime,
    ) -> ScienceAttemptExecutionRecordV2: ...


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


class _CancellationSignal(Protocol):
    def is_set(self) -> bool: ...

    def wait(self, timeout: float | None = None) -> bool: ...


@dataclass(frozen=True, slots=True)
class ExecutedScienceAttemptV2:
    compiled: CompiledScienceAttemptV2
    session_result: SessionResult
    evidence: ScienceAttemptExecutionEvidenceV2
    receipt: ScienceAttemptExecutionReceiptV2
    record: ScienceAttemptExecutionRecordV2


@dataclass(frozen=True, slots=True)
class CompiledScienceAttemptV2:
    """Exact private request and successor method plan for one immutable Attempt."""

    task: m2.TaskV2
    attempt: m2.AttemptRefV2
    project_config: m2.ScienceProjectConfigV2
    policy_version: str
    rollout: CanonicalTaskRequest
    evolution_plan: EvolutionPlan
    evolution_methods: tuple[CompiledEvolutionMethodSpec, ...]
    successor_plan: ScienceSuccessorPlanV2


def compile_science_attempt_v2(
    *,
    task: m2.TaskV2,
    attempt: m2.AttemptRefV2,
    project: ProjectRecordV2,
    binding: ServiceRunBinding,
    workspace_handoff: WorkspaceHandoffBindingV2,
    executable_registry: VerifiedExecutableRegistry,
    prior_dataset_artifact_ids: Sequence[str] = (),
    predecessor_successor_commit: AtomicSuccessorCommitV2 | None = None,
) -> CompiledScienceAttemptV2:
    """Compile saved v2 authority directly into one managed rollout request."""

    task = m2.TaskV2.model_validate(task.model_dump(mode="python"))
    attempt = m2.AttemptRefV2.model_validate(attempt.model_dump(mode="python"))
    if type(project) is not ProjectRecordV2:
        raise TypeError("science execution requires the exact v2 project record")
    project = ProjectRecordV2(
        project_id=project.project_id,
        display_name=project.display_name,
        config=project.config,
        project_config_sha256=project.project_config_sha256,
        created_at=project.created_at,
        updated_at=project.updated_at,
        resource_version=project.resource_version,
    )
    if type(binding) is not ServiceRunBinding:
        raise TypeError("science execution requires an exact service run binding")
    if type(workspace_handoff) is not WorkspaceHandoffBindingV2:
        raise TypeError("science execution requires an exact workspace handoff binding")
    workspace_handoff = WorkspaceHandoffBindingV2.model_validate(
        workspace_handoff.model_dump(mode="python")
    )
    registry = require_verified_executable_registry(executable_registry)
    config = project.config
    subscription = config.execution.mode == "codex_subscription_transcript"
    expected_service_mode = (
        ServiceExecutionMode.CODEX_SUBSCRIPTION_TRANSCRIPT
        if subscription
        else ServiceExecutionMode.SELF_DEPLOYED
    )
    admission = task.admission
    predecessor = admission.predecessor_project_head
    if (
        task.project_id != project.project_id
        or project.project_config_sha256 != admission.project_config_sha256
        or m2.project_config_sha256_for(config) != admission.project_config_sha256
        or attempt not in task.attempts
        or attempt.task_id != task.task_id
        or attempt.task_admission_id != admission.task_admission_id
        or attempt.admission_sha256 != admission.admission_sha256
        or attempt.predecessor_project_head_id != predecessor.project_head_id
        or workspace_handoff.task_id != _rollout_task_id(attempt)
        or workspace_handoff.attempt_id != attempt.attempt_id
        or workspace_handoff.task_admission_id != admission.task_admission_id
        or workspace_handoff.admission_sha256 != admission.admission_sha256
        or workspace_handoff.project_id != task.project_id
        or workspace_handoff.input_workspace_snapshot != admission.workspace_snapshot
        or binding.execution_mode is not expected_service_mode
        or binding.codex_model != config.execution.codex_model
        or binding.registry_digest != admission.registry_sha256
        or workspace_handoff.service_generation_sha256 != binding.generation_digest
        or workspace_handoff.registry_sha256 != binding.registry_digest
        or workspace_handoff.framework_lock_sha256 != binding.framework_lock_digest
    ):
        raise ValueError("science Attempt compilation differs from immutable authority")
    runtime_release = require_immutable_managed_runtime_image(
        profile="managed_science",
        image=binding.runtime_image_immutable_reference,
    )
    if runtime_release.image != binding.runtime_image:
        raise ValueError("science Attempt runtime release changed")
    prior_dataset_ids = _canonical_prior_dataset_ids(prior_dataset_artifact_ids)
    experiment = compile_science_evolution_experiment_v2(
        task=task,
        attempt=attempt,
        project=project,
        binding=binding,
        registry=registry,
        prior_dataset_artifact_ids=prior_dataset_ids,
    )
    methods = tuple(
        experiment.evolution_methods_for_round(
            0,
            prior_dataset_artifact_ids=prior_dataset_ids,
            task_id=task.task_id,
        )
    )
    plan = experiment.evolution_plan_for_round(
        0,
        prior_dataset_artifact_ids=prior_dataset_ids,
        task_id=task.task_id,
    )
    successor_plan = ScienceSuccessorPlanV2(
        project_id=task.project_id,
        task_id=task.task_id,
        task_admission_id=admission.task_admission_id,
        admission_sha256=admission.admission_sha256,
        accepted_attempt_id=attempt.attempt_id,
        predecessor_project_head_id=predecessor.project_head_id,
        normalized_evolution_intent_sha256=(admission.normalized_evolution_intent_sha256),
        enabled_methods=tuple(
            sorted(
                (
                    ScienceSuccessorMethodPlanV2(
                        target_id=method.target_id,
                        method_id=method.method,
                        output_artifact_type=method.artifact_type,
                    )
                    for method in methods
                ),
                key=lambda item: item.target_id,
            )
        ),
    )
    if len(experiment.tasks) != 1:
        raise ValueError("science Attempt compilation produced multiple rollout tasks")
    policy_version = experiment.tasks[0].policy_version_for_round(0)
    request = TaskRequest(
        task_id=_rollout_task_id(attempt),
        instruction=config.task.objective,
        num_samples=1,
        timeout_seconds=7200.0,
        runtime=RuntimeSpec(
            backend="docker",
            profile="managed_science",
            container_user="host",
            image=binding.runtime_image_immutable_reference,
            prepare=[
                PrepareAction(
                    type="exec",
                    command=MANAGED_SUBSCRIPTION_PREPARE_COMMAND,
                )
            ],
            env={
                "HOME": MANAGED_HOME,
                "PATH": MANAGED_PATH,
                **(
                    {}
                    if subscription
                    else {
                        "CODEX_HOME": _MANAGED_PROXY_CODEX_HOME,
                        "OPENEVO_MANAGED_HF_MODEL": config.execution.codex_model,
                    }
                ),
            },
            network="host",
            workdir=MANAGED_WORKSPACE,
            allow_internet=config.execution.task_network_allow_internet,
        ),
        agent=AgentSpec(
            harness="codex",
            model_name=config.execution.codex_model,
            settings={
                "auth_mode": "subscription" if subscription else "proxy",
                "capture_mode": "transcript",
                **(
                    {"reasoning_effort": config.execution.reasoning_effort}
                    if subscription and config.execution.reasoning_effort is not None
                    else {}
                ),
                **(
                    {CODEX_SUBSCRIPTION_CONTRACT_KEY: (codex_subscription_contract())}
                    if subscription
                    else {}
                ),
            },
        ),
        builder=StrategySpec(strategy="agent_transcript"),
        metadata={
            "policy_version": policy_version,
            "rollout_step": 0,
            "agent": {
                "harness": "codex",
                "model_name": config.execution.codex_model,
            },
            "openevo": {
                "admission_sha256": admission.admission_sha256,
                "attempt_id": attempt.attempt_id,
                "evolution_revision_id": (predecessor.evolution_revision.evolution_revision_id),
                "project_head_id": predecessor.project_head_id,
                "project_id": task.project_id,
                "runtime_context_snapshot_id": (
                    predecessor.runtime_context_snapshot.runtime_context_snapshot_id
                ),
                "task_admission_id": admission.task_admission_id,
            },
        },
        workspace_handoff=workspace_handoff,
        runtime_context_binding=runtime_context_binding_for_head(
            project_head=predecessor,
            service_generation_sha256=binding.generation_digest,
            framework_lock_sha256=binding.framework_lock_digest,
            successor_commit=predecessor_successor_commit,
        ),
    )
    return CompiledScienceAttemptV2(
        task=task,
        attempt=attempt,
        project_config=config,
        policy_version=policy_version,
        rollout=canonicalize_task_request(request),
        evolution_plan=plan,
        evolution_methods=methods,
        successor_plan=successor_plan,
    )


def compile_science_evolution_experiment_v2(
    *,
    task: m2.TaskV2,
    attempt: m2.AttemptRefV2,
    project: ProjectRecordV2,
    binding: ServiceRunBinding,
    registry: VerifiedExecutableRegistry,
    prior_dataset_artifact_ids: tuple[str, ...],
) -> CompiledExperiment:
    del prior_dataset_artifact_ids
    config = project.config
    subscription = config.execution.mode == "codex_subscription_transcript"
    experiment = ExperimentConfig.model_validate(
        {
            "version": 1,
            "experiment": {"name": project.project_id},
            "agent": {
                "preset": "codex",
                "model": config.execution.codex_model,
                "auth": "subscription" if subscription else "proxy",
                "provider": "codex_cli",
                "settings": {
                    "auth_mode": "subscription" if subscription else "proxy",
                    "capture_mode": "transcript",
                    **(
                        {"reasoning_effort": config.execution.reasoning_effort}
                        if subscription and config.execution.reasoning_effort is not None
                        else {}
                    ),
                },
                "env": ({} if subscription else {"CODEX_HOME": _MANAGED_PROXY_CODEX_HOME}),
            },
            "tasks": [
                {
                    "id": task.task_id,
                    "instruction": config.task.objective,
                    "metadata": {},
                }
            ],
            "runtime": {
                "kind": "docker",
                "profile": "managed_science",
                "container_user": "host",
                "workdir": MANAGED_WORKSPACE,
                "image": binding.runtime_image_immutable_reference,
                "env": {
                    "HOME": MANAGED_HOME,
                    "PATH": MANAGED_PATH,
                    **(
                        {}
                        if subscription
                        else {
                            "CODEX_HOME": _MANAGED_PROXY_CODEX_HOME,
                            "OPENEVO_MANAGED_HF_MODEL": config.execution.codex_model,
                        }
                    ),
                },
                "prepare": [
                    {
                        "type": "exec",
                        "command": MANAGED_SUBSCRIPTION_PREPARE_COMMAND,
                    }
                ],
            },
            "rollout": {"url": binding.rollout_url},
            "evolution": {
                "backend_url": binding.evolution_backend_url,
                "rounds": 1,
                "targets": config.evolution.targets,
            },
        }
    )
    return compile_experiment(
        experiment,
        task_ids=[task.task_id],
        rounds_override=1,
        run_id=attempt.attempt_id,
        registry_snapshot=registry.snapshot,
        execution_profile=execution_profile_for_release_mode(config.execution.mode),
    )


def _rollout_task_id(attempt: m2.AttemptRefV2) -> str:
    return f"rollout-{attempt.attempt_id}"


def _canonical_prior_dataset_ids(values: Sequence[str]) -> tuple[str, ...]:
    if isinstance(values, str | bytes):
        raise TypeError("prior dataset artifact IDs must be a sequence")
    normalized = tuple(values)
    if (
        len(normalized) > 128
        or any(
            not isinstance(item, str) or not item or len(item.encode("utf-8")) > 256
            for item in normalized
        )
        or len(normalized) != len(set(normalized))
        or normalized != tuple(sorted(normalized))
    ):
        raise ValueError("prior dataset artifact IDs are not canonical")
    return normalized


class ScienceAttemptExecutorV2:
    """Execute one immutable v2 Attempt against one leased service generation."""

    def __init__(
        self,
        *,
        catalog: _ProjectCatalogV2,
        workspaces: _WorkspaceAuthorityV2,
        workspace_handoffs: _WorkspaceHandoffAuthorityV2,
        ledger: _AttemptLedgerV2,
        services: _ServiceOwnerV2,
        executable_registry: VerifiedExecutableRegistry,
        rollout_factory: Callable[[ServiceRunBinding], RolloutClientProtocol] | None = None,
        prior_dataset_artifact_ids: (Callable[[m2.ProjectHeadRefV2], Sequence[str]] | None) = None,
        clock: Callable[[], datetime] | None = None,
        poll_interval_seconds: float = 1.0,
        max_poll_attempts: int = 7200,
    ) -> None:
        if poll_interval_seconds < 0 or max_poll_attempts < 1:
            raise ValueError("v2 science execution polling configuration is invalid")
        for value, method, label in (
            (catalog, "get_project", "project catalog"),
            (workspaces, "get_snapshot", "workspace authority"),
            (workspace_handoffs, "reserve", "workspace handoff authority"),
            (ledger, "record_terminal_attempt", "Attempt ledger"),
            (
                ledger,
                "successor_commit_for_project_head",
                "successor receipt ledger",
            ),
            (services, "ensure_run_binding", "service owner"),
        ):
            if not callable(getattr(value, method, None)):
                raise TypeError(f"science executor requires a {label}")
        self._catalog = catalog
        self._workspaces = workspaces
        self._workspace_handoffs = workspace_handoffs
        self._ledger = ledger
        self._services = services
        self._registry = require_verified_executable_registry(executable_registry)
        self._rollout_factory = rollout_factory or (
            lambda binding: RolloutHttpClient(
                binding.rollout_url,
                headers=binding.request_headers(),
            )
        )
        self._prior_dataset_artifact_ids = prior_dataset_artifact_ids or (lambda _head: ())
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._poll_interval = poll_interval_seconds
        self._max_poll_attempts = max_poll_attempts

    def execute(
        self,
        *,
        task: m2.TaskV2,
        attempt: m2.AttemptRefV2,
        cancellation: _CancellationSignal,
    ) -> ExecutedScienceAttemptV2:
        task = m2.TaskV2.model_validate(task.model_dump(mode="python"))
        attempt = m2.AttemptRefV2.model_validate(attempt.model_dump(mode="python"))
        if not callable(getattr(cancellation, "is_set", None)) or not callable(
            getattr(cancellation, "wait", None)
        ):
            raise TypeError("science executor requires a cancellation signal")
        if cancellation.is_set():
            raise ScienceAttemptCancelledV2()
        project = self._catalog.get_project(task.project_id)
        if type(project) is not ProjectRecordV2:
            raise ScienceAttemptExecutionV2Error(
                "project_authority_unavailable",
                retryable=True,
            )
        config = project.config
        service_lease = None
        rollout: RolloutClientProtocol | None = None
        submitted = False
        terminal_received = False
        primary_error: BaseException | None = None
        try:
            if config.execution.mode == "self-deployed":
                snapshot, service_lease = self._services.ensure_run_binding(
                    ServiceExecutionMode.SELF_DEPLOYED,
                    model_ref=config.execution.model_profile_id,
                    runtime_image=MANAGED_RUNTIME_IMAGES["managed_science"],
                    total_timeout=7200.0,
                )
            else:
                snapshot, service_lease = self._services.ensure_run_binding(
                    ServiceExecutionMode.CODEX_SUBSCRIPTION_TRANSCRIPT,
                    codex_model=config.execution.codex_model,
                    runtime_image=MANAGED_RUNTIME_IMAGES["managed_science"],
                )
            binding = getattr(service_lease, "binding", None)
            if (
                service_lease is None
                or type(binding) is not ServiceRunBinding
                or getattr(snapshot, "run_ready", False) is not True
                or getattr(snapshot, "generation_digest", None) != binding.generation_digest
                or getattr(snapshot, "runtime_identity_digest", None)
                != binding.runtime_identity_digest
            ):
                raise ScienceAttemptExecutionV2Error(
                    "service_generation_unavailable",
                    retryable=True,
                )
            self._verify_effective_execution(task, project, binding)
            authoritative_snapshot = self._workspaces.get_snapshot(
                task.admission.workspace_snapshot.workspace_snapshot_id
            )
            if authoritative_snapshot != task.admission.workspace_snapshot:
                raise ScienceAttemptExecutionV2Error(
                    "workspace_snapshot_changed",
                    retryable=False,
                )
            workspace_path = self._workspaces.snapshot_path(authoritative_snapshot)
            archive = self._workspaces.archive_declaration(authoritative_snapshot)
            handoff = self._workspace_handoffs.reserve(
                WorkspaceHandoffRequestV2(
                    task_id=_rollout_task_id(attempt),
                    attempt_id=attempt.attempt_id,
                    task_admission_id=task.admission.task_admission_id,
                    admission_sha256=task.admission.admission_sha256,
                    project_id=task.project_id,
                    input_workspace_snapshot=authoritative_snapshot,
                    input_archive=archive,
                    service_generation_sha256=binding.generation_digest,
                    registry_sha256=binding.registry_digest,
                    framework_lock_sha256=binding.framework_lock_digest,
                ),
                workspace_path,
                now=self._clock(),
            )
            compiled = compile_science_attempt_v2(
                task=task,
                attempt=attempt,
                project=project,
                binding=binding,
                workspace_handoff=handoff,
                executable_registry=self._registry,
                prior_dataset_artifact_ids=self._prior_dataset_artifact_ids(
                    task.admission.predecessor_project_head
                ),
                predecessor_successor_commit=(
                    self._ledger.successor_commit_for_project_head(
                        task.admission.predecessor_project_head.project_head_id
                    )
                ),
            )
            check = GenerationBoundRunAdmissionCheck(
                operation=RunAdmissionOperation.ROLLOUT_TASK_SUBMIT,
                generation_digest=binding.generation_digest,
                registry_digest=binding.registry_digest,
                framework_lock_digest=binding.framework_lock_digest,
                payload_sha256=compiled.rollout.payload_sha256,
                task_id=compiled.rollout.request.task_id,
                session_id=None,
            )
            if not self._ledger.register_attempt_run_admission(
                task_id=task.task_id,
                attempt_id=attempt.attempt_id,
                check=check,
                allow_create=True,
            ):
                raise ScienceAttemptExecutionV2Error(
                    "run_admission_changed",
                    retryable=False,
                )
            if cancellation.is_set():
                raise ScienceAttemptCancelledV2()
            self._ledger.mark_attempt_running(
                task_id=task.task_id,
                attempt_id=attempt.attempt_id,
                now=self._clock(),
            )
            rollout = self._rollout_factory(binding)
            submitted_task_id = rollout.submit_task(compiled.rollout.payload)
            submitted = True
            if submitted_task_id != compiled.rollout.request.task_id:
                raise ScienceAttemptExecutionV2Error(
                    "rollout_task_identity_changed",
                    retryable=False,
                )
            terminal_status = self._wait_for_terminal_status(
                rollout,
                compiled=compiled,
                cancellation=cancellation,
            )
            terminal_received = True
            session_result = self._validate_terminal_result(
                terminal_status,
                compiled=compiled,
            )
            evidence, receipt = _execution_terminal_bundle(
                compiled=compiled,
                binding=binding,
                result=session_result,
                completed_at=self._clock(),
            )
            record = self._ledger.record_terminal_attempt(
                task_id=task.task_id,
                attempt_id=attempt.attempt_id,
                receipt=receipt,
                evidence=evidence,
                successor_plan=compiled.successor_plan,
                terminal_result=session_result,
                now=self._clock(),
            )
            return ExecutedScienceAttemptV2(
                compiled=compiled,
                session_result=session_result,
                evidence=evidence,
                receipt=receipt,
                record=record,
            )
        except BaseException as exc:
            primary_error = exc
            if submitted and not terminal_received and rollout is not None:
                try:
                    _cancel_rollout_task(rollout, _rollout_task_id(attempt))
                except Exception as cancellation_error:
                    exc.add_note(
                        f"rollout cancellation also failed: {type(cancellation_error).__name__}"
                    )
            raise
        finally:
            cleanup_errors: list[Exception] = []
            if rollout is not None:
                close = getattr(rollout, "close", None)
                if callable(close):
                    try:
                        close()
                    except Exception as cleanup_error:
                        cleanup_errors.append(cleanup_error)
            if service_lease is not None:
                close = getattr(service_lease, "close", None)
                if callable(close):
                    try:
                        close()
                    except Exception as cleanup_error:
                        cleanup_errors.append(cleanup_error)
            if cleanup_errors:
                if primary_error is not None:
                    for cleanup_error in cleanup_errors:
                        primary_error.add_note(
                            f"attempt cleanup also failed: {type(cleanup_error).__name__}"
                        )
                else:
                    raise cleanup_errors[0]

    def _verify_effective_execution(
        self,
        task: m2.TaskV2,
        project: ProjectRecordV2,
        binding: ServiceRunBinding,
    ) -> None:
        config = project.config
        verified = resolve_genesis_execution_snapshot(
            settings=EffectiveExecutionSettings(
                execution_mode=config.execution.mode,
                capture_mode=config.execution.capture_mode,
                harness_id=config.execution.harness_id,
                model_ref=config.execution.codex_model,
                token_limit=config.execution.token_limit,
                task_network_allow_internet=(config.execution.task_network_allow_internet),
            ),
            service_binding=binding,
        )
        expected = task.admission.predecessor_project_head.effective_execution_snapshot
        digest = canonical_digest(verified.snapshot)
        if (
            project.project_config_sha256 != task.admission.project_config_sha256
            or m2.project_config_sha256_for(config) != task.admission.project_config_sha256
            or verified.producer_id != expected.producer_id
            or digest != expected.snapshot_sha256
            or expected.effective_execution_snapshot_id != f"exec-{digest}"
            or expected.execution_mode != config.execution.mode
            or expected.capture_mode != config.execution.capture_mode
            or expected.token_level_metrics_available
            != config.execution.token_level_metrics_available
            or binding.registry_digest != task.admission.registry_sha256
        ):
            raise ScienceAttemptExecutionV2Error(
                "effective_execution_snapshot_changed",
                retryable=False,
            )

    def _wait_for_terminal_status(
        self,
        rollout: RolloutClientProtocol,
        *,
        compiled: CompiledScienceAttemptV2,
        cancellation: _CancellationSignal,
    ) -> TaskStatus:
        task_id = compiled.rollout.request.task_id
        for poll_index in range(self._max_poll_attempts):
            if poll_index and cancellation.wait(self._poll_interval):
                raise ScienceAttemptCancelledV2()
            if cancellation.is_set():
                raise ScienceAttemptCancelledV2()
            raw = rollout.get_task(task_id)
            try:
                status = TaskStatus.model_validate(raw)
            except (TypeError, ValueError) as exc:
                raise ScienceAttemptExecutionV2Error(
                    "rollout_status_invalid",
                    retryable=True,
                ) from exc
            if status.task_id != task_id:
                raise ScienceAttemptExecutionV2Error(
                    "rollout_task_identity_changed",
                    retryable=False,
                )
            if status.status == "running":
                continue
            return status
        raise ScienceAttemptExecutionV2Error("rollout_poll_timeout", retryable=True)

    @staticmethod
    def _validate_terminal_result(
        status: TaskStatus,
        *,
        compiled: CompiledScienceAttemptV2,
    ) -> SessionResult:
        if (
            status.status != "completed"
            or status.total_sessions != 1
            or status.completed_sessions != 1
            or len(status.results) != 1
        ):
            raise ScienceAttemptExecutionV2Error(
                "rollout_task_failed",
                retryable=True,
            )
        candidate = status.results[0]
        if (
            candidate.status is not SessionStatus.COMPLETED
            or candidate.trajectory.status != "COMPLETED"
            or candidate.error is not None
            or candidate.trajectory.error is not None
        ):
            raise ScienceAttemptExecutionV2Error(
                "rollout_task_failed",
                retryable=True,
            )
        try:
            result = canonical_subscription_session_result(candidate)
        except (TypeError, ValueError) as exc:
            raise ScienceAttemptExecutionV2Error(
                "rollout_terminal_evidence_invalid",
                retryable=False,
            ) from exc
        if (
            result.task_id != compiled.rollout.request.task_id
            or result.metadata.get("policy_version") != compiled.policy_version
        ):
            raise ScienceAttemptExecutionV2Error(
                "rollout_terminal_evidence_changed",
                retryable=False,
            )
        return result


def _execution_terminal_bundle(
    *,
    compiled: CompiledScienceAttemptV2,
    binding: ServiceRunBinding,
    result: SessionResult,
    completed_at: datetime,
) -> tuple[ScienceAttemptExecutionEvidenceV2, ScienceAttemptExecutionReceiptV2]:
    result = canonical_subscription_session_result(result)
    _validate_runtime_context_terminal_metadata(compiled, result)
    workspace = result.workspace_result
    assert workspace is not None
    handoff = compiled.rollout.request.workspace_handoff
    if handoff is None:
        raise ScienceAttemptExecutionV2Error(
            "workspace_handoff_missing",
            retryable=False,
        )
    task = compiled.task
    attempt = compiled.attempt
    admission = task.admission
    predecessor = admission.predecessor_project_head
    if (
        workspace.handoff_id != handoff.handoff_id
        or workspace.attempt_id != attempt.attempt_id
        or workspace.task_admission_id != admission.task_admission_id
        or workspace.admission_sha256 != admission.admission_sha256
        or workspace.project_id != task.project_id
        or workspace.service_generation_sha256 != binding.generation_digest
        or workspace.registry_sha256 != binding.registry_digest
        or workspace.framework_lock_sha256 != binding.framework_lock_digest
    ):
        raise ScienceAttemptExecutionV2Error(
            "workspace_result_authority_changed",
            retryable=False,
        )
    result_sha256 = science_session_result_sha256(result)
    evidence = ScienceAttemptExecutionEvidenceV2(
        task_id=task.task_id,
        attempt_id=attempt.attempt_id,
        rollout_task_id=result.task_id,
        policy_version=compiled.policy_version,
        session_id=result.session_id,
        session_status="COMPLETED",
        session_result_sha256=result_sha256,
        workspace_handoff_id=workspace.handoff_id,
        workspace_result_manifest_sha256=workspace.result_manifest_sha256,
        workspace_archive_sha256=workspace.output_archive.content_sha256,
        workspace_archive_byte_size=workspace.output_archive.byte_size,
        workspace_entry_count=workspace.output_archive.entry_count,
        workspace_extracted_byte_size=workspace.output_archive.extracted_byte_size,
        capture_mode="transcript",
        token_level_metrics_available=False,
        transcript_record_count=len(result.trajectory.traces),
    )
    provisional = ScienceAttemptExecutionReceiptV2.model_construct(
        execution_receipt_contract_version="2",
        task_id=task.task_id,
        attempt_id=attempt.attempt_id,
        task_admission_id=admission.task_admission_id,
        admission_sha256=admission.admission_sha256,
        project_id=task.project_id,
        predecessor_project_head_id=predecessor.project_head_id,
        predecessor_project_head_manifest_sha256=predecessor.manifest_sha256,
        workspace_snapshot_id=admission.workspace_snapshot.workspace_snapshot_id,
        workspace_manifest_sha256=admission.workspace_snapshot.manifest_sha256,
        evolution_revision_id=predecessor.evolution_revision.evolution_revision_id,
        evolution_revision_manifest_sha256=predecessor.evolution_revision.manifest_sha256,
        runtime_context_snapshot_id=(
            predecessor.runtime_context_snapshot.runtime_context_snapshot_id
        ),
        runtime_context_manifest_sha256=predecessor.runtime_context_snapshot.manifest_sha256,
        effective_execution_snapshot_id=(
            predecessor.effective_execution_snapshot.effective_execution_snapshot_id
        ),
        effective_execution_snapshot_sha256=(
            predecessor.effective_execution_snapshot.snapshot_sha256
        ),
        registry_sha256=binding.registry_digest,
        service_generation_sha256=binding.generation_digest,
        framework_lock_sha256=binding.framework_lock_digest,
        runtime_identity_sha256=binding.runtime_identity_digest,
        harness_id="codex",
        capture_mode="transcript",
        token_level_metrics_available=False,
        model_ref=compiled.project_config.execution.codex_model,
        task_network_allow_internet=(
            compiled.project_config.execution.task_network_allow_internet
        ),
        rollout_task_id=result.task_id,
        rollout_payload_sha256=compiled.rollout.payload_sha256,
        session_id=result.session_id,
        session_result_sha256=result_sha256,
        workspace_handoff_id=workspace.handoff_id,
        workspace_result_manifest_sha256=workspace.result_manifest_sha256,
        terminal_status="COMPLETED",
        completed_at=_timestamp(completed_at),
        receipt_sha256="0" * 64,
    )
    receipt = ScienceAttemptExecutionReceiptV2.model_validate(
        {
            **provisional.model_dump(mode="python"),
            "receipt_sha256": science_attempt_execution_receipt_sha256(provisional),
        }
    )
    return evidence, receipt


def _validate_runtime_context_terminal_metadata(
    compiled: CompiledScienceAttemptV2,
    result: SessionResult,
) -> None:
    binding = compiled.rollout.request.runtime_context_binding
    metadata = result.metadata
    openevo = metadata.get("openevo")
    evolution = metadata.get("evolution")
    if binding is None or not isinstance(openevo, dict) or not isinstance(evolution, dict):
        raise ScienceAttemptExecutionV2Error(
            "runtime_context_injection_unproven",
            retryable=False,
        )
    head = binding.project_head
    expected_evolution = {
        "context_id": binding.materialized_context_id,
        "context_injected": binding.source in {"materialized_successor", "materialized_inherited"},
        "context_source": binding.source,
        "runtime_context_snapshot_id": (head.runtime_context_snapshot.runtime_context_snapshot_id),
    }
    if (
        any(evolution.get(key) != value for key, value in expected_evolution.items())
        or openevo.get("project_id") != head.project_id
        or openevo.get("project_head_id") != head.project_head_id
        or openevo.get("evolution_revision_id") != head.evolution_revision.evolution_revision_id
        or openevo.get("runtime_context_snapshot_id")
        != head.runtime_context_snapshot.runtime_context_snapshot_id
    ):
        raise ScienceAttemptExecutionV2Error(
            "runtime_context_injection_unproven",
            retryable=False,
        )
    receipt = evolution.get("runtime_injection_receipt")
    if binding.source in {"empty_genesis", "empty_inherited"}:
        if receipt is not None:
            raise ScienceAttemptExecutionV2Error(
                "runtime_context_injection_unproven",
                retryable=False,
            )
        return
    artifacts = receipt.get("artifacts") if isinstance(receipt, dict) else None
    files = receipt.get("files") if isinstance(receipt, dict) else None
    artifact_ids = (
        [item.get("artifact_id") for item in artifacts if isinstance(item, dict)]
        if isinstance(artifacts, list)
        else None
    )
    if (
        not isinstance(receipt, dict)
        or receipt.get("schema_version") != "4"
        or receipt.get("context_id") != binding.materialized_context_id
        or receipt.get("context_manifest_sha256") != binding.materialized_context_manifest_sha256
        or receipt.get("revision_id") != head.evolution_revision.evolution_revision_id
        or receipt.get("runtime_context_snapshot_id")
        != head.runtime_context_snapshot.runtime_context_snapshot_id
        or receipt.get("project_head_id") != head.project_head_id
        or not _is_sha256(receipt.get("instruction_sha256"))
        or not _is_sha256(receipt.get("runtime_tree_sha256"))
        or not isinstance(files, list)
        or artifact_ids != list(binding.selected_artifact_ids)
    ):
        raise ScienceAttemptExecutionV2Error(
            "runtime_context_injection_unproven",
            retryable=False,
        )


def _is_sha256(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )


def _cancel_rollout_task(client: RolloutClientProtocol, task_id: str) -> None:
    result = client.cancel_task(task_id)
    if result.get("task_id") != task_id or result.get("status") != "cancelled":
        raise ScienceAttemptExecutionV2Error(
            "rollout_cancellation_unproven",
            retryable=True,
        )


def _timestamp(value: datetime) -> str:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise ValueError("science execution clock must be timezone-aware")
    return (
        value.astimezone(timezone.utc)
        .isoformat(timespec="microseconds")
        .replace(
            "+00:00",
            "Z",
        )
    )


class _ScienceExecutionModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        strict=True,
        validate_default=True,
    )


class ScienceAttemptExecutionEvidenceV2(_ScienceExecutionModel):
    """Sanitized terminal evidence produced by the managed execution graph."""

    execution_evidence_contract_version: Literal["2"] = "2"
    task_id: str = Field(pattern=_ID_PATTERN)
    attempt_id: str = Field(pattern=_ID_PATTERN)
    rollout_task_id: str = Field(pattern=_ID_PATTERN)
    policy_version: str = Field(min_length=1, max_length=512)
    session_id: str = Field(pattern=_ID_PATTERN)
    session_status: Literal["COMPLETED"]
    session_result_sha256: str = Field(pattern=_SHA256_PATTERN)
    workspace_handoff_id: str = Field(pattern=_ID_PATTERN)
    workspace_result_manifest_sha256: str = Field(pattern=_SHA256_PATTERN)
    workspace_archive_sha256: str = Field(pattern=_SHA256_PATTERN)
    workspace_archive_byte_size: int = Field(ge=0, le=m2.MAX_SNAPSHOT_BYTES)
    workspace_entry_count: int = Field(ge=0, le=m2.MAX_SNAPSHOT_ENTRIES)
    workspace_extracted_byte_size: int = Field(ge=0, le=m2.MAX_SNAPSHOT_BYTES)
    capture_mode: Literal["transcript"]
    token_level_metrics_available: Literal[False]
    transcript_record_count: int = Field(ge=1, le=10_000_000)


class ScienceAttemptExecutionReceiptV2(_ScienceExecutionModel):
    """Content-addressed proof that execution used every immutable admission pin."""

    execution_receipt_contract_version: Literal["2"] = "2"
    task_id: str = Field(pattern=_ID_PATTERN)
    attempt_id: str = Field(pattern=_ID_PATTERN)
    task_admission_id: str = Field(pattern=_ID_PATTERN)
    admission_sha256: str = Field(pattern=_SHA256_PATTERN)
    project_id: str = Field(pattern=_ID_PATTERN)
    predecessor_project_head_id: str = Field(pattern=_ID_PATTERN)
    predecessor_project_head_manifest_sha256: str = Field(pattern=_SHA256_PATTERN)
    workspace_snapshot_id: str = Field(pattern=_ID_PATTERN)
    workspace_manifest_sha256: str = Field(pattern=_SHA256_PATTERN)
    evolution_revision_id: str = Field(pattern=_ID_PATTERN)
    evolution_revision_manifest_sha256: str = Field(pattern=_SHA256_PATTERN)
    runtime_context_snapshot_id: str = Field(pattern=_ID_PATTERN)
    runtime_context_manifest_sha256: str = Field(pattern=_SHA256_PATTERN)
    effective_execution_snapshot_id: str = Field(pattern=_ID_PATTERN)
    effective_execution_snapshot_sha256: str = Field(pattern=_SHA256_PATTERN)
    registry_sha256: str = Field(pattern=_SHA256_PATTERN)
    service_generation_sha256: str = Field(pattern=_SHA256_PATTERN)
    framework_lock_sha256: str = Field(pattern=_SHA256_PATTERN)
    runtime_identity_sha256: str = Field(pattern=_SHA256_PATTERN)
    harness_id: Literal["codex"]
    capture_mode: Literal["transcript"]
    token_level_metrics_available: Literal[False]
    model_ref: str = Field(min_length=1, max_length=256)
    task_network_allow_internet: bool
    rollout_task_id: str = Field(pattern=_ID_PATTERN)
    rollout_payload_sha256: str = Field(pattern=_SHA256_PATTERN)
    session_id: str = Field(pattern=_ID_PATTERN)
    session_result_sha256: str = Field(pattern=_SHA256_PATTERN)
    workspace_handoff_id: str = Field(pattern=_ID_PATTERN)
    workspace_result_manifest_sha256: str = Field(pattern=_SHA256_PATTERN)
    terminal_status: Literal["COMPLETED"]
    completed_at: m2.UtcTimestamp
    receipt_sha256: str = Field(pattern=_SHA256_PATTERN)

    @model_validator(mode="after")
    def _content_addressed(self) -> ScienceAttemptExecutionReceiptV2:
        if self.receipt_sha256 != science_attempt_execution_receipt_sha256(self):
            raise ValueError("science Attempt execution receipt digest is invalid")
        return self


ScienceAttemptExecutionStateV2 = Literal[
    "preparing",
    "running",
    "cancelling",
    "captured",
    "failed",
    "cancelled",
]


class ScienceAttemptExecutionRecordV2(_ScienceExecutionModel):
    """Durable private lifecycle for one immutable public Attempt reference."""

    execution_record_contract_version: Literal["2"] = "2"
    task_id: str = Field(pattern=_ID_PATTERN)
    attempt_id: str = Field(pattern=_ID_PATTERN)
    state: ScienceAttemptExecutionStateV2
    receipt: ScienceAttemptExecutionReceiptV2 | None = None
    evidence: ScienceAttemptExecutionEvidenceV2 | None = None
    successor_plan: ScienceSuccessorPlanV2 | None = None
    error_code: str | None = Field(default=None, pattern=_ERROR_CODE_PATTERN)
    created_at: m2.UtcTimestamp
    updated_at: m2.UtcTimestamp

    @model_validator(mode="after")
    def _closed_terminal_shape(self) -> ScienceAttemptExecutionRecordV2:
        bundle = (self.receipt, self.evidence, self.successor_plan)
        if self.state == "captured":
            if any(value is None for value in bundle) or self.error_code is not None:
                raise ValueError("captured Attempt execution requires exact terminal evidence")
            assert self.receipt is not None
            assert self.evidence is not None
            assert self.successor_plan is not None
            if (
                self.receipt.task_id != self.task_id
                or self.receipt.attempt_id != self.attempt_id
                or self.evidence.task_id != self.task_id
                or self.evidence.attempt_id != self.attempt_id
                or self.successor_plan.task_id != self.task_id
                or self.successor_plan.accepted_attempt_id != self.attempt_id
                or self.receipt.rollout_task_id != self.evidence.rollout_task_id
                or self.receipt.session_id != self.evidence.session_id
                or self.receipt.session_result_sha256 != self.evidence.session_result_sha256
                or self.receipt.workspace_handoff_id != self.evidence.workspace_handoff_id
                or self.receipt.workspace_result_manifest_sha256
                != self.evidence.workspace_result_manifest_sha256
                or self.receipt.terminal_status != self.evidence.session_status
            ):
                raise ValueError("captured Attempt execution evidence is inconsistent")
        elif any(value is not None for value in bundle):
            raise ValueError("non-captured Attempt execution cannot contain result evidence")

        if self.state == "failed":
            if self.error_code is None:
                raise ValueError("failed Attempt execution requires an error code")
        elif self.error_code is not None:
            raise ValueError("only failed Attempt execution may contain an error code")
        return self


def science_attempt_execution_receipt_sha256(
    receipt: ScienceAttemptExecutionReceiptV2,
) -> str:
    """Return the canonical digest while excluding the self-address field."""

    if not isinstance(receipt, ScienceAttemptExecutionReceiptV2):
        raise TypeError("science Attempt receipt digest requires its exact model")
    payload = json.dumps(
        receipt.model_dump(mode="json", exclude={"receipt_sha256"}),
        ensure_ascii=True,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def canonical_subscription_session_result(result: SessionResult) -> SessionResult:
    """Validate the one terminal transcript result accepted by the v2 run owner."""

    if type(result) is not SessionResult:
        raise TypeError("science execution requires the exact SessionResult model")
    result = SessionResult.model_validate(result.model_dump(mode="python"))
    workspace = result.workspace_result
    trajectory = result.trajectory
    if (
        result.status is not SessionStatus.COMPLETED
        or trajectory.status != "COMPLETED"
        or result.error is not None
        or trajectory.error is not None
        or workspace is None
        or workspace.task_id != result.task_id
        or workspace.session_id != result.session_id
    ):
        raise ValueError("science execution did not produce one complete workspace result")
    if not trajectory.traces:
        raise ValueError("subscription science execution produced no transcript records")
    if (
        trajectory.metadata.get("capture_mode") != "transcript"
        or trajectory.metadata.get("token_level_metrics_available") is not False
    ):
        raise ValueError("subscription trajectory lacks exact transcript capture authority")
    for trace in trajectory.traces:
        if (
            not trace.prompt_messages
            or not trace.response_messages
            or trace.prompt_ids
            or trace.response_ids
            or trace.loss_mask
            or trace.response_logprobs is not None
            or trace.metadata.get("capture_mode") != "transcript"
            or trace.metadata.get("token_level_metrics_available") is not False
        ):
            raise ValueError("subscription transcript record contains invalid token authority")
    payload = result.model_dump(
        mode="json",
        exclude_defaults=False,
        exclude_none=False,
        exclude_unset=False,
    )
    _validate_captured_result_value(payload)
    encoded = _canonical_json_bytes(payload)
    if len(encoded) > MAX_CAPTURED_SESSION_RESULT_BYTES:
        raise ValueError("captured subscription result exceeds its byte budget")
    return result


def science_session_result_bytes(result: SessionResult) -> bytes:
    result = canonical_subscription_session_result(result)
    return _canonical_json_bytes(
        result.model_dump(
            mode="json",
            exclude_defaults=False,
            exclude_none=False,
            exclude_unset=False,
        )
    )


def science_session_result_sha256(result: SessionResult) -> str:
    return hashlib.sha256(science_session_result_bytes(result)).hexdigest()


def _canonical_json_bytes(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=True,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _validate_captured_result_value(value: object) -> None:
    remaining = _MAX_CAPTURED_RESULT_NODES
    stack: list[tuple[object, int]] = [(value, 0)]
    while stack:
        current, depth = stack.pop()
        remaining -= 1
        if remaining < 0:
            raise ValueError("captured subscription result exceeds its node budget")
        if depth > _MAX_CAPTURED_RESULT_DEPTH:
            raise ValueError("captured subscription result exceeds its depth budget")
        if current is None or type(current) in {bool, int, str}:
            continue
        if type(current) is float:
            if not math.isfinite(current):
                raise ValueError("captured subscription result contains a non-finite number")
            continue
        if type(current) is list:
            stack.extend((item, depth + 1) for item in current)
            continue
        if type(current) is dict:
            for key, item in current.items():
                if type(key) is not str:
                    raise ValueError("captured subscription result has a non-text key")
                stack.append((item, depth + 1))
            continue
        raise ValueError("captured subscription result contains a non-JSON value")


__all__ = [
    "CompiledScienceAttemptV2",
    "ExecutedScienceAttemptV2",
    "ScienceAttemptCancelledV2",
    "ScienceAttemptExecutionV2Error",
    "ScienceAttemptExecutionEvidenceV2",
    "ScienceAttemptExecutorV2",
    "ScienceAttemptExecutionReceiptV2",
    "ScienceAttemptExecutionRecordV2",
    "ScienceAttemptExecutionStateV2",
    "MAX_CAPTURED_SESSION_RESULT_BYTES",
    "canonical_subscription_session_result",
    "compile_science_attempt_v2",
    "compile_science_evolution_experiment_v2",
    "science_attempt_execution_receipt_sha256",
    "science_session_result_bytes",
    "science_session_result_sha256",
]
