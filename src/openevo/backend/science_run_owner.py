"""Durable Core owner for ordinary-user science runs."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
import logging
import math
from pathlib import Path
import secrets
import threading
from typing import Any, Iterator, Protocol, cast

from fastapi.responses import JSONResponse, Response

from openevo.backend.contracts.v1 import models as m
from openevo.backend.contracts.v2 import models as m2
from openevo.backend.contracts.v1.store import (
    CoreControlStoreV1,
    ResourceConflictError,
    ResourceNotFoundError,
)
from openevo.backend.run_control import CoreRunControlError, CoreTaskControlError
from openevo.backend.service_control import CoreServiceControlError
from openevo.backend.science_execution import compile_science_execution
from openevo.backend.science_execution_v2 import (
    ScienceAttemptCancelledV2,
    ScienceAttemptExecutionV2Error,
)
from openevo.backend.science_successor import (
    AcceptedWorkspaceResultV2,
    ScienceMethodOutputV2,
    ScienceSuccessorPlanV2,
    ScienceSuccessorPreparationContextV2,
    SealedTranscriptDatasetV2,
    SuccessorMaterializationV2,
    ValidatedScienceOutputsV2,
)
from openevo.backend.science_run_store import (
    ProjectInFlightCoordinator,
    ScienceAttemptNotFoundV2,
    ScienceEventCursorExpiredV2,
    ScienceProjectInFlight,
    ScienceRunConflict,
    ScienceRunIdempotencyConflict,
    ScienceRunNotFound,
    ScienceRunPreconditionFailed,
    ScienceRunStore,
    ScienceRunStoreError,
    ScienceProjectAdmissionAuthorityV2,
    ScienceTaskConflictV2,
    ScienceTaskETagChangedV2,
    ScienceTaskIdempotencyConflictV2,
    ScienceTaskNotFoundV2,
    ScienceTaskNotReadyV2,
    ScienceTaskPreconditionFailedV2,
    ScienceTaskProjectInFlightV2,
    ScienceTaskStaleSubmissionV2,
    ScienceTaskStoreV2,
    ScienceTaskStoreV2Error,
    ScienceTaskTerminalV2,
    page_items,
)
from openevo.backend.service_supervisor import (
    CoreServiceSupervisor,
    ServiceExecutionMode,
    ServiceGroupSnapshot,
    ServiceRunReadinessCode,
    ServiceRunBinding,
    ServiceRunLease,
)
from openevo.evolution.framework.builtins import VerifiedExecutableRegistry
from openevo.evolution.framework.execution import ResolvedMethodInputBinding
from openevo.evolution.runtime_injection import build_runtime_injection_plan
from openevo.evolution.revisions import (
    AtomicSuccessorCommitV2,
    AtomicSuccessorManifestV2,
    atomic_successor_manifest_sha256,
)
from openevo.experiments import EvolutionHttpClient, RolloutHttpClient
from openevo.experiments.clients import EvolutionClientProtocol, RolloutClientProtocol
from openevo.experiments.runner import _run_core_authoritative_experiment
from openevo.internal_auth import (
    GenerationBoundRunAdmissionCheck,
    RunAdmissionError,
    RunAdmissionOperation,
)
from openevo.projects.science.compiler import MANAGED_RUNTIME_IMAGES
from openevo.rollout.models import canonicalize_task_request


logger = logging.getLogger(__name__)


_TERMINAL = frozenset({m.RunStatus.SUCCEEDED, m.RunStatus.FAILED, m.RunStatus.CANCELLED})
_ACTIVE_FOR_ADMISSION = frozenset(
    {m.RunStatus.PREPARING, m.RunStatus.RUNNING, m.RunStatus.CANCELLING}
)
_CONTEXT_TYPES = (
    "dataset",
    "text_memory",
    "parametric_memory",
    "skill_bundle",
    "agent_system",
)


class _ServiceOwner(Protocol):
    def ensure(
        self,
        execution_mode: ServiceExecutionMode,
        *,
        model_ref: str | None = None,
        codex_model: str | None = None,
        runtime_image: str | None = None,
        total_timeout: float | None = None,
    ) -> ServiceGroupSnapshot: ...

    def ensure_run_binding(
        self,
        execution_mode: ServiceExecutionMode,
        *,
        model_ref: str | None = None,
        codex_model: str | None = None,
        runtime_image: str | None = None,
        total_timeout: float | None = None,
    ) -> tuple[ServiceGroupSnapshot, ServiceRunLease | None]: ...

    def run_binding(self) -> ServiceRunBinding: ...


ExperimentRunner = Callable[..., dict[str, Any]]
RolloutFactory = Callable[[ServiceRunBinding], RolloutClientProtocol]
EvolutionFactory = Callable[[ServiceRunBinding], EvolutionClientProtocol]


class ScienceSuccessorPreparerV2(Protocol):
    """Prepare all evidence before the Core owner atomically advances a head."""

    def request_stop(self) -> None: ...

    def seal_dataset(
        self,
        context: ScienceSuccessorPreparationContextV2,
    ) -> SealedTranscriptDatasetV2: ...

    def run_methods(
        self,
        context: ScienceSuccessorPreparationContextV2,
        dataset: SealedTranscriptDatasetV2,
    ) -> tuple[ScienceMethodOutputV2, ...]: ...

    def validate_outputs(
        self,
        context: ScienceSuccessorPreparationContextV2,
        dataset: SealedTranscriptDatasetV2,
        outputs: tuple[ScienceMethodOutputV2, ...],
    ) -> ValidatedScienceOutputsV2: ...

    def materialize_context(
        self,
        context: ScienceSuccessorPreparationContextV2,
        validated: ValidatedScienceOutputsV2,
    ) -> SuccessorMaterializationV2: ...

    def capture_workspace_result(
        self,
        context: ScienceSuccessorPreparationContextV2,
    ) -> AcceptedWorkspaceResultV2: ...


class ScienceAttemptRunnerV2(Protocol):
    """Execute one already-started immutable v2 Attempt."""

    def execute(
        self,
        *,
        task: m2.TaskV2,
        attempt: m2.AttemptRefV2,
        cancellation: threading.Event,
    ) -> object: ...


class _RunCancelled(RuntimeError):
    pass


class _RunFinalizationConflict(ScienceRunConflict):
    pass


class CoreScienceTaskOwnerV2:
    """Own immutable v2 Task, Attempt execution, and successor publication."""

    def __init__(
        self,
        *,
        state_root: str | Path,
        clock: Callable[[], datetime] | None = None,
        successor_preparer: ScienceSuccessorPreparerV2 | None = None,
        successor_preparer_factory: (
            Callable[[ScienceTaskStoreV2], ScienceSuccessorPreparerV2] | None
        ) = None,
        attempt_executor_factory: (
            Callable[[ScienceTaskStoreV2], ScienceAttemptRunnerV2] | None
        ) = None,
    ) -> None:
        if successor_preparer is not None and successor_preparer_factory is not None:
            raise ValueError("v2 Task owner accepts one successor preparer authority")
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._ledger = ScienceTaskStoreV2(Path(state_root) / "science-tasks-v2")
        self._successor_preparer = (
            successor_preparer
            if successor_preparer_factory is None
            else successor_preparer_factory(self._ledger)
        )
        self._condition = threading.Condition()
        self._lifecycle_lock = threading.RLock()
        self._cancel_events: dict[str, threading.Event] = {}
        self._closed = False
        self._close_complete = False
        self._recovery_complete = False
        self._attempt_executor = (
            None if attempt_executor_factory is None else attempt_executor_factory(self._ledger)
        )
        if self._attempt_executor is not None and not callable(
            getattr(self._attempt_executor, "execute", None)
        ):
            self._ledger.close()
            raise TypeError("v2 Task owner requires an Attempt executor")
        self._ledger.recover_interrupted_attempts(now=self._clock())
        self._recover_interrupted_successor_transitions()
        self._recovery_complete = True
        self._worker: threading.Thread | None = None
        if self._attempt_executor is not None:
            self._worker = threading.Thread(
                target=self._worker_loop,
                name="openevo-science-task-owner-v2",
                daemon=True,
            )
            self._worker.start()
            with self._condition:
                self._condition.notify_all()

    def invoke(self, operation_id: str, arguments: Mapping[str, object]) -> object:
        handlers: dict[str, Callable[[Mapping[str, object]], object]] = {
            "appendCoreTaskAttemptV2": self._append_attempt,
            "getCoreTaskAdmissionV2": self._get_admission,
            "getCoreTaskAttemptV2": self._get_attempt,
            "getCoreTaskV2": self._get_task,
            "listCoreTaskAttemptsV2": self._list_attempts,
            "listCoreTasksV2": self._list_tasks,
            "submitCoreTaskV2": self._submit_task,
        }
        handler = handlers.get(operation_id)
        if handler is None:
            raise CoreTaskControlError(
                "task_operation_unavailable",
                "The v2 science Task owner does not implement this operation.",
                http_status=503,
                retryable=False,
            )
        try:
            return handler(arguments)
        except CoreTaskControlError:
            raise
        except Exception as exc:
            _raise_v2_owner_error(exc, operation_id=operation_id)

    @property
    def successor_available(self) -> bool:
        return self._successor_preparer is not None

    @property
    def execution_available(self) -> bool:
        return self._attempt_executor is not None

    @property
    def production_ready(self) -> bool:
        return (
            self._recovery_complete
            and self._attempt_executor is not None
            and self._successor_preparer is not None
        )

    def publish_project_admission_authority(
        self,
        authority: ScienceProjectAdmissionAuthorityV2,
        *,
        expected_project_head_id: str | None = None,
    ) -> ScienceProjectAdmissionAuthorityV2:
        try:
            return self._ledger.publish_project_admission_authority(
                authority,
                expected_project_head_id=expected_project_head_id,
            )
        except Exception as exc:
            _raise_v2_owner_error(
                exc,
                operation_id="publishCoreProjectAdmissionAuthorityV2",
            )

    def project_admission_authority(
        self,
        project_id: str,
    ) -> ScienceProjectAdmissionAuthorityV2:
        try:
            return self._ledger.project_admission_authority(project_id)
        except Exception as exc:
            _raise_v2_owner_error(
                exc,
                operation_id="getCoreProjectAdmissionAuthorityV2",
            )

    def active_project_head(self, project_id: str) -> m2.ProjectHeadRefV2:
        try:
            return self._ledger.active_project_head(project_id)
        except Exception as exc:
            _raise_v2_owner_error(exc, operation_id="getCoreActiveProjectHeadV2")

    def list_project_heads(self, project_id: str) -> list[m2.ProjectHeadRefV2]:
        try:
            return self._ledger.list_project_heads(project_id)
        except Exception as exc:
            _raise_v2_owner_error(exc, operation_id="listCoreProjectHeadsV2")

    def get_project_head(self, project_head_id: str) -> m2.ProjectHeadRefV2:
        try:
            return self._ledger.get_project_head(project_head_id)
        except Exception as exc:
            _raise_v2_owner_error(exc, operation_id="getCoreProjectHeadV2")

    def get_successor_transition(
        self,
        successor_transition_id: str,
    ) -> m2.SuccessorTransitionV2:
        try:
            return self._ledger.get_successor_transition(successor_transition_id)
        except Exception as exc:
            _raise_v2_owner_error(exc, operation_id="getCoreSuccessorTransitionV2")

    def list_successor_transitions(
        self,
        project_id: str,
    ) -> list[m2.SuccessorTransitionV2]:
        try:
            return self._ledger.list_successor_transitions(project_id)
        except Exception as exc:
            _raise_v2_owner_error(exc, operation_id="listCoreSuccessorTransitionsV2")

    def get_successor_transition_for_task(
        self,
        task_id: str,
    ) -> m2.SuccessorTransitionV2:
        try:
            return self._ledger.get_successor_transition_for_task(task_id)
        except Exception as exc:
            _raise_v2_owner_error(exc, operation_id="getCoreTaskSuccessorTransitionV2")

    def successor_commit(
        self,
        successor_transition_id: str,
    ) -> AtomicSuccessorCommitV2 | None:
        try:
            return self._ledger.successor_commit(successor_transition_id)
        except Exception as exc:
            _raise_v2_owner_error(exc, operation_id="getCoreSuccessorCommitV2")

    def run_successor_transition(
        self,
        task_id: str,
        *,
        accepted_attempt_id: str,
        plan: ScienceSuccessorPlanV2,
    ) -> m2.SuccessorTransitionV2:
        preparer = self._successor_preparer
        if preparer is None:
            raise CoreTaskControlError(
                "successor_preparer_unavailable",
                "Core has no verified science successor preparer.",
                http_status=503,
                retryable=False,
            )
        try:
            transition = self._ledger.start_successor_transition(
                task_id=task_id,
                accepted_attempt_id=accepted_attempt_id,
                plan=plan,
                now=self._clock(),
            )
        except Exception as exc:
            _raise_v2_owner_error(exc, operation_id="startCoreSuccessorTransitionV2")

        transition_id = transition.transition.successor_transition_id
        try:
            context = self._advance_successor_phase(
                transition_id,
                state="sealing_dataset",
                plan=plan,
            )
            dataset = _validate_sealed_successor_dataset(
                preparer.seal_dataset(context),
                context=context,
            )
            self._ledger.record_dataset_sealed(
                transition_id,
                dataset_id=dataset.dataset_id,
                dataset_sha256=dataset.manifest_sha256,
                now=self._clock(),
            )

            context = self._advance_successor_phase(
                transition_id,
                state="running_methods",
                plan=plan,
            )
            outputs = _validate_successor_method_outputs(
                preparer.run_methods(context, dataset),
                plan=plan,
            )

            context = self._advance_successor_phase(
                transition_id,
                state="validating",
                plan=plan,
            )
            validated = _validate_successor_outputs_receipt(
                preparer.validate_outputs(context, dataset, outputs),
                context=context,
                dataset=dataset,
                outputs=outputs,
            )

            context = self._advance_successor_phase(
                transition_id,
                state="materializing",
                plan=plan,
            )
            materialized = _validate_successor_materialization_receipt(
                preparer.materialize_context(context, validated),
                context=context,
                validated=validated,
            )
            workspace = _validate_accepted_workspace_result(
                preparer.capture_workspace_result(context),
                context=context,
            )

            context = self._advance_successor_phase(
                transition_id,
                state="committing",
                plan=plan,
            )
            successor = _build_v2_successor_project_head(
                context=context,
                workspace=workspace,
                validated=validated,
                materialized=materialized,
            )
            manifest = _build_atomic_successor_manifest(
                context=context,
                dataset=dataset,
                outputs=outputs,
                workspace=workspace,
                validated=validated,
                materialized=materialized,
                successor=successor,
            )
            commit = AtomicSuccessorCommitV2(
                manifest_sha256=atomic_successor_manifest_sha256(manifest),
                manifest=manifest,
            )
            return self._ledger.commit_successor_transition(
                transition_id,
                successor=successor,
                commit=commit,
                now=self._clock(),
            )
        except Exception as exc:
            logger.error(
                "v2 science successor transition %s failed during preparation [%s]",
                transition_id,
                type(exc).__name__,
            )
            error = _successor_transition_api_error(
                code="successor_transition_failed",
                message="Core could not prepare and atomically commit the successor state.",
            )
            try:
                self._ledger.fail_successor_transition(
                    transition_id,
                    error=error,
                    now=self._clock(),
                )
            except Exception as persistence_exc:
                raise CoreTaskControlError(
                    "successor_transition_failed",
                    "Core could not preserve the failed successor transition.",
                    http_status=503,
                    retryable=False,
                ) from persistence_exc
            raise CoreTaskControlError(
                "successor_transition_failed",
                "Core preserved the failed successor transition without advancing the project head.",
                http_status=503,
                retryable=False,
            ) from exc

    def _advance_successor_phase(
        self,
        successor_transition_id: str,
        *,
        state: str,
        plan: ScienceSuccessorPlanV2,
    ) -> ScienceSuccessorPreparationContextV2:
        transition = self._ledger.advance_successor_transition(
            successor_transition_id,
            state=state,
            now=self._clock(),
        )
        admission = transition.transition.task_admission
        attempt = transition.transition.accepted_attempt
        if admission is None or attempt is None:
            raise ScienceTaskStoreV2Error(
                "run-result successor transition lost its Task ownership"
            )
        task = self._ledger.get_task(admission.task_id)
        return ScienceSuccessorPreparationContextV2(
            task=task,
            accepted_attempt=attempt,
            transition=transition,
            plan=plan,
        )

    def _recover_interrupted_successor_transitions(self) -> None:
        for transition_id in self._ledger.nonterminal_successor_transition_ids():
            self._ledger.fail_successor_transition(
                transition_id,
                error=_successor_transition_api_error(
                    code="successor_transition_interrupted",
                    message=(
                        "Core restarted before the successor transition committed; "
                        "the predecessor remains active."
                    ),
                ),
                now=self._clock(),
            )

    def close_task(
        self,
        task_id: str,
        request: m2.TaskActionRequestV2,
        *,
        expected_etag: str | None = None,
        allow_closed_recovery: bool = False,
    ) -> m2.TaskV2:
        try:
            return self._ledger.close_task(
                task_id,
                request,
                now=self._clock(),
                expected_etag=expected_etag,
                allow_closed_recovery=allow_closed_recovery,
            )
        except Exception as exc:
            _raise_v2_owner_error(exc, operation_id="closeCoreTaskV2")

    def ownership_counts(self) -> tuple[int, int, int]:
        try:
            return self._ledger.ownership_counts()
        except Exception as exc:
            _raise_v2_owner_error(exc, operation_id="inspectCoreTaskOwnershipV2")

    def list_events(
        self,
        *,
        after_event_id: str | None = None,
    ) -> list[m2.EventEnvelopeV2]:
        try:
            return self._ledger.list_events(after_event_id=after_event_id)
        except Exception as exc:
            _raise_v2_owner_error(exc, operation_id="streamCoreEventsV2")

    def list_task_events(self, task_id: str) -> list[m2.EventEnvelopeV2]:
        try:
            return self._ledger.list_task_events(task_id)
        except Exception as exc:
            _raise_v2_owner_error(exc, operation_id="getCoreTaskTimelineV2")

    async def verify(self, check: GenerationBoundRunAdmissionCheck) -> None:
        """Authorize only service calls derived from an active v2 Attempt."""

        try:
            accepted = self._ledger.verify_attempt_run_admission(check)
        except Exception as exc:
            raise _admission_denied() from exc
        if not accepted:
            raise _admission_denied()

    def cancel_attempt(
        self,
        task_id: str,
        attempt_id: str,
    ) -> m2.TaskV2:
        """Durably request cancellation and wake the one owning executor."""

        try:
            with self._lifecycle_lock:
                record = self._ledger.get_attempt_execution_optional(
                    task_id,
                    attempt_id,
                )
                if record is None:
                    task = self._ledger.get_task(task_id)
                    attempt = self._ledger.get_attempt(task_id, attempt_id)
                    if attempt != task.attempts[-1]:
                        raise ScienceTaskTerminalV2("only the latest v2 Attempt may be cancelled")
                    record = self._ledger.begin_attempt_execution(
                        task_id=task_id,
                        attempt_id=attempt_id,
                        now=self._clock(),
                    )
                self._ledger.request_attempt_cancellation(
                    task_id=task_id,
                    attempt_id=attempt_id,
                    now=self._clock(),
                )
                cancellation = self._cancel_events.get(attempt_id)
                if cancellation is None:
                    self._ledger.finish_attempt_cancelled(
                        task_id=task_id,
                        attempt_id=attempt_id,
                        now=self._clock(),
                    )
                else:
                    cancellation.set()
            with self._condition:
                self._condition.notify_all()
            return self._ledger.get_task(task_id)
        except Exception as exc:
            _raise_v2_owner_error(exc, operation_id="cancelCoreTaskAttemptV2")

    def close(self) -> None:
        with self._lifecycle_lock:
            if self._close_complete:
                return
            first_close = not self._closed
            if first_close:
                self._closed = True
                for cancellation in self._cancel_events.values():
                    cancellation.set()
        if first_close:
            preparer_stop = getattr(self._successor_preparer, "request_stop", None)
            if callable(preparer_stop):
                preparer_stop()
            with self._condition:
                self._condition.notify_all()
        worker = self._worker
        if worker is not None and worker is not threading.current_thread():
            worker.join(timeout=30.0)
            if worker.is_alive():
                raise RuntimeError("v2 science Task owner did not stop before shutdown")
        self._ledger.close()
        with self._lifecycle_lock:
            self._close_complete = True

    def _submit_task(self, arguments: Mapping[str, object]) -> m2.TaskV2:
        _require_v2_argument_keys(arguments, {"request", "idempotency_key"})
        request = _v2_request_model(m2.TaskSubmitRequestV2, arguments["request"])
        task, _replayed = self._ledger.submit_task(
            request=request,
            idempotency_key=_v2_string_argument(
                arguments["idempotency_key"], label="idempotency key"
            ),
            now=self._clock(),
        )
        with self._condition:
            self._condition.notify_all()
        return task

    def _get_task(self, arguments: Mapping[str, object]) -> m2.TaskV2:
        _require_v2_argument_keys(arguments, {"task_id"})
        return self._ledger.get_task(_v2_string_argument(arguments["task_id"], label="task ID"))

    def _list_tasks(self, arguments: Mapping[str, object]) -> list[m2.TaskV2]:
        _require_v2_argument_keys(arguments, set(), optional={"project_id"})
        project_id = arguments.get("project_id")
        return self._ledger.list_tasks(
            project_id=(
                None if project_id is None else _v2_string_argument(project_id, label="project ID")
            )
        )

    def _get_admission(self, arguments: Mapping[str, object]) -> m2.TaskAdmissionRefV2:
        _require_v2_argument_keys(arguments, {"task_id"})
        return self._ledger.get_admission(
            _v2_string_argument(arguments["task_id"], label="task ID")
        )

    def _list_attempts(self, arguments: Mapping[str, object]) -> list[m2.AttemptRefV2]:
        _require_v2_argument_keys(arguments, {"task_id"})
        return self._ledger.list_attempts(
            _v2_string_argument(arguments["task_id"], label="task ID")
        )

    def _append_attempt(self, arguments: Mapping[str, object]) -> m2.AttemptRefV2:
        _require_v2_argument_keys(
            arguments,
            {"task_id", "request", "idempotency_key"},
        )
        request = _v2_request_model(m2.AttemptAppendRequestV2, arguments["request"])
        attempt, _replayed = self._ledger.append_attempt(
            task_id=_v2_string_argument(arguments["task_id"], label="task ID"),
            request=request,
            idempotency_key=_v2_string_argument(
                arguments["idempotency_key"], label="idempotency key"
            ),
            now=self._clock(),
        )
        with self._condition:
            self._condition.notify_all()
        return attempt

    def _get_attempt(self, arguments: Mapping[str, object]) -> m2.AttemptRefV2:
        _require_v2_argument_keys(arguments, {"task_id", "attempt_id"})
        return self._ledger.get_attempt(
            _v2_string_argument(arguments["task_id"], label="task ID"),
            _v2_string_argument(arguments["attempt_id"], label="attempt ID"),
        )

    def _worker_loop(self) -> None:
        while True:
            with self._lifecycle_lock:
                if self._closed:
                    return
            try:
                if self._process_one_captured_successor():
                    continue
                if self._process_one_unstarted_attempt():
                    continue
            except Exception as exc:
                # A durable phase method records its own closed failure. Keep the
                # owner alive so a different project can continue to make progress.
                logger.error(
                    "v2 science owner preserved a worker failure [%s]",
                    type(exc).__name__,
                )
            with self._condition:
                with self._lifecycle_lock:
                    if self._closed:
                        return
                self._condition.wait(timeout=1.0)

    def _process_one_captured_successor(self) -> bool:
        if self._successor_preparer is None:
            return False
        for record in self._ledger.captured_attempt_executions():
            attempt_id = record.attempt_id
            task = self._ledger.get_task(record.task_id)
            if (
                task.state == "waiting_for_successor"
                and task.authoritative_attempt_id == attempt_id
                and task.successor_transition is None
                and record.successor_plan is not None
            ):
                self.run_successor_transition(
                    task.task_id,
                    accepted_attempt_id=attempt_id,
                    plan=record.successor_plan,
                )
                return True
        return False

    def _process_one_unstarted_attempt(self) -> bool:
        executor = self._attempt_executor
        if executor is None:
            return False
        pending = self._ledger.unstarted_attempts()
        if not pending:
            return False
        task, attempt = pending[0]
        cancellation = threading.Event()
        try:
            with self._lifecycle_lock:
                if self._closed:
                    return False
                self._ledger.begin_attempt_execution(
                    task_id=task.task_id,
                    attempt_id=attempt.attempt_id,
                    now=self._clock(),
                )
                self._cancel_events[attempt.attempt_id] = cancellation
            executor.execute(
                task=task,
                attempt=attempt,
                cancellation=cancellation,
            )
        except ScienceAttemptCancelledV2:
            with self._lifecycle_lock:
                if self._closed:
                    self._finish_attempt_failed_if_owned(
                        task,
                        attempt,
                        "daemon_shutdown_during_attempt",
                    )
                else:
                    try:
                        self._ledger.finish_attempt_cancelled(
                            task_id=task.task_id,
                            attempt_id=attempt.attempt_id,
                            now=self._clock(),
                        )
                    except ScienceTaskTerminalV2:
                        pass
        except ScienceAttemptExecutionV2Error as exc:
            self._finish_attempt_failed_if_owned(task, attempt, exc.code)
        except Exception as exc:
            logger.error(
                "v2 science Attempt %s failed inside its executor [%s]",
                attempt.attempt_id,
                type(exc).__name__,
            )
            self._finish_attempt_failed_if_owned(
                task,
                attempt,
                "attempt_execution_internal_error",
            )
        finally:
            with self._lifecycle_lock:
                self._cancel_events.pop(attempt.attempt_id, None)
        return True

    def _finish_attempt_failed_if_owned(
        self,
        task: m2.TaskV2,
        attempt: m2.AttemptRefV2,
        error_code: str,
    ) -> None:
        try:
            self._ledger.finish_attempt_failed(
                task_id=task.task_id,
                attempt_id=attempt.attempt_id,
                error_code=error_code,
                now=self._clock(),
            )
        except ScienceTaskTerminalV2:
            pass


def _validate_sealed_successor_dataset(
    value: SealedTranscriptDatasetV2,
    *,
    context: ScienceSuccessorPreparationContextV2,
) -> SealedTranscriptDatasetV2:
    if type(value) is not SealedTranscriptDatasetV2:
        raise TypeError("successor dataset has the wrong type")
    dataset = SealedTranscriptDatasetV2.model_validate(value.model_dump(mode="python"))
    if (
        dataset.task_id != context.task.task_id
        or dataset.task_admission_id != context.task.admission.task_admission_id
        or dataset.accepted_attempt_id != context.accepted_attempt.attempt_id
    ):
        raise ValueError("sealed successor dataset has different Task ownership")
    return dataset


def _validate_successor_method_outputs(
    value: tuple[ScienceMethodOutputV2, ...],
    *,
    plan: ScienceSuccessorPlanV2,
) -> tuple[ScienceMethodOutputV2, ...]:
    if type(value) is not tuple or any(type(item) is not ScienceMethodOutputV2 for item in value):
        raise TypeError("successor method outputs have the wrong type")
    outputs = tuple(
        ScienceMethodOutputV2.model_validate(item.model_dump(mode="python")) for item in value
    )
    expected = tuple(
        (item.target_id, item.method_id, item.output_artifact_type)
        for item in plan.enabled_methods
    )
    actual = tuple((item.target_id, item.method_id, item.artifact_type) for item in outputs)
    if actual != expected:
        raise ValueError("successor method outputs do not exactly cover the enabled method plan")
    if len({item.artifact_id for item in outputs}) != len(outputs):
        raise ValueError("successor method output artifact IDs must be unique")
    return outputs


def _validate_successor_outputs_receipt(
    value: ValidatedScienceOutputsV2,
    *,
    context: ScienceSuccessorPreparationContextV2,
    dataset: SealedTranscriptDatasetV2,
    outputs: tuple[ScienceMethodOutputV2, ...],
) -> ValidatedScienceOutputsV2:
    if type(value) is not ValidatedScienceOutputsV2:
        raise TypeError("validated successor outputs have the wrong type")
    validated = ValidatedScienceOutputsV2.model_validate(value.model_dump(mode="python"))
    if (
        validated.project_id != context.task.project_id
        or validated.successor_transition_id
        != context.transition.transition.successor_transition_id
        or validated.predecessor_project_head_id
        != context.task.admission.predecessor_project_head.project_head_id
        or validated.dataset != dataset
        or validated.outputs != outputs
    ):
        raise ValueError("validated successor outputs do not bind their preparation")
    return validated


def _validate_successor_materialization_receipt(
    value: SuccessorMaterializationV2,
    *,
    context: ScienceSuccessorPreparationContextV2,
    validated: ValidatedScienceOutputsV2,
) -> SuccessorMaterializationV2:
    if type(value) is not SuccessorMaterializationV2:
        raise TypeError("successor materialization has the wrong type")
    materialized = SuccessorMaterializationV2.model_validate(value.model_dump(mode="python"))
    runtime = materialized.runtime_context_snapshot
    evolution = validated.evolution_revision
    if (
        materialized.project_id != context.task.project_id
        or materialized.successor_transition_id
        != context.transition.transition.successor_transition_id
        or materialized.predecessor_project_head_id
        != context.task.admission.predecessor_project_head.project_head_id
        or runtime.evolution_revision_id != evolution.evolution_revision_id
        or runtime.evolution_revision_manifest_sha256 != evolution.manifest_sha256
        or runtime.registry_sha256 != context.task.admission.registry_sha256
    ):
        raise ValueError("successor materialization does not bind validated outputs")
    return materialized


def _validate_accepted_workspace_result(
    value: AcceptedWorkspaceResultV2,
    *,
    context: ScienceSuccessorPreparationContextV2,
) -> AcceptedWorkspaceResultV2:
    if type(value) is not AcceptedWorkspaceResultV2:
        raise TypeError("accepted workspace result has the wrong type")
    workspace = AcceptedWorkspaceResultV2.model_validate(value.model_dump(mode="python"))
    if (
        workspace.project_id != context.task.project_id
        or workspace.task_id != context.task.task_id
        or workspace.accepted_attempt_id != context.accepted_attempt.attempt_id
    ):
        raise ValueError("accepted workspace result has different Task ownership")
    return workspace


def _build_v2_successor_project_head(
    *,
    context: ScienceSuccessorPreparationContextV2,
    workspace: AcceptedWorkspaceResultV2,
    validated: ValidatedScienceOutputsV2,
    materialized: SuccessorMaterializationV2,
) -> m2.ProjectHeadRefV2:
    predecessor = context.task.admission.predecessor_project_head
    composition = {
        "effective_execution_snapshot": predecessor.effective_execution_snapshot.model_dump(
            mode="json"
        ),
        "evolution_revision": validated.evolution_revision.model_dump(mode="json"),
        "generation": predecessor.generation + 1,
        "predecessor_project_head_id": predecessor.project_head_id,
        "project_id": context.task.project_id,
        "registry_sha256": materialized.runtime_context_snapshot.registry_sha256,
        "runtime_context_snapshot": materialized.runtime_context_snapshot.model_dump(mode="json"),
        "successor_transition_id": (context.transition.transition.successor_transition_id),
        "workspace_snapshot": workspace.workspace_snapshot.model_dump(mode="json"),
    }
    payload = json.dumps(
        composition,
        ensure_ascii=True,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    manifest_sha256 = hashlib.sha256(payload).hexdigest()
    return m2.ProjectHeadRefV2(
        project_head_id=f"project-head-{manifest_sha256}",
        project_id=context.task.project_id,
        generation=predecessor.generation + 1,
        predecessor_project_head_id=predecessor.project_head_id,
        workspace_snapshot=workspace.workspace_snapshot,
        evolution_revision=validated.evolution_revision,
        runtime_context_snapshot=materialized.runtime_context_snapshot,
        effective_execution_snapshot=predecessor.effective_execution_snapshot,
        registry_sha256=materialized.runtime_context_snapshot.registry_sha256,
        manifest_sha256=manifest_sha256,
    )


def _build_atomic_successor_manifest(
    *,
    context: ScienceSuccessorPreparationContextV2,
    dataset: SealedTranscriptDatasetV2,
    outputs: tuple[ScienceMethodOutputV2, ...],
    workspace: AcceptedWorkspaceResultV2,
    validated: ValidatedScienceOutputsV2,
    materialized: SuccessorMaterializationV2,
    successor: m2.ProjectHeadRefV2,
) -> AtomicSuccessorManifestV2:
    predecessor = context.task.admission.predecessor_project_head
    execution = successor.effective_execution_snapshot
    return AtomicSuccessorManifestV2(
        project_id=context.task.project_id,
        successor_transition_id=(context.transition.transition.successor_transition_id),
        task_id=context.task.task_id,
        task_admission_id=context.task.admission.task_admission_id,
        admission_sha256=context.task.admission.admission_sha256,
        accepted_attempt_id=context.accepted_attempt.attempt_id,
        predecessor_project_head_id=predecessor.project_head_id,
        predecessor_generation=predecessor.generation,
        predecessor_manifest_sha256=predecessor.manifest_sha256,
        successor_project_head_id=successor.project_head_id,
        successor_generation=successor.generation,
        successor_manifest_sha256=successor.manifest_sha256,
        workspace_snapshot_id=workspace.workspace_snapshot.workspace_snapshot_id,
        workspace_manifest_sha256=workspace.workspace_snapshot.manifest_sha256,
        evolution_revision_id=validated.evolution_revision.evolution_revision_id,
        evolution_revision_manifest_sha256=(validated.evolution_revision.manifest_sha256),
        runtime_context_snapshot_id=(
            materialized.runtime_context_snapshot.runtime_context_snapshot_id
        ),
        runtime_context_manifest_sha256=(materialized.runtime_context_snapshot.manifest_sha256),
        effective_execution_snapshot_id=(execution.effective_execution_snapshot_id),
        effective_execution_snapshot_sha256=execution.snapshot_sha256,
        registry_sha256=successor.registry_sha256,
        normalized_evolution_intent_sha256=(
            context.task.admission.normalized_evolution_intent_sha256
        ),
        dataset_id=dataset.dataset_id,
        dataset_artifact_id=dataset.artifact_id,
        dataset_manifest_sha256=dataset.manifest_sha256,
        materialized_context_id=materialized.materialized_context_id,
        materialized_context_manifest_sha256=(materialized.materialized_context_manifest_sha256),
        method_artifact_ids=tuple(item.artifact_id for item in outputs),
    )


def _successor_transition_api_error(*, code: str, message: str) -> m2.ApiErrorV2:
    return m2.ApiErrorV2(
        request_id=f"successor-error-{secrets.token_hex(12)}",
        code=code,
        http_status=503,
        message=message,
        category="transition",
        retryable=False,
        repair_action="repair",
        next_action=(
            "Inspect remote diagnostics and repair the preserved transition before "
            "submitting another Task."
        ),
    )


class CoreScienceRunOwner:
    """Own the frozen Core run routes and execute one science run at a time."""

    def __init__(
        self,
        *,
        state_root: str | Path,
        project_store: CoreControlStoreV1,
        service_supervisor: CoreServiceSupervisor | _ServiceOwner,
        executable_registry: VerifiedExecutableRegistry,
        experiment_runner: ExperimentRunner = _run_core_authoritative_experiment,
        rollout_factory: RolloutFactory | None = None,
        evolution_factory: EvolutionFactory | None = None,
        clock: Callable[[], datetime] | None = None,
        poll_interval_seconds: float = 1.0,
        max_poll_attempts: int = 7200,
    ) -> None:
        if poll_interval_seconds < 0 or max_poll_attempts < 1:
            raise ValueError("science run polling configuration is invalid")
        self._project_store = project_store
        self._services = service_supervisor
        self._registry = executable_registry
        self._runner = experiment_runner
        self._rollout_factory = rollout_factory or (
            lambda binding: RolloutHttpClient(
                binding.rollout_url,
                headers=binding.request_headers(),
            )
        )
        self._evolution_factory = evolution_factory or (
            lambda binding: EvolutionHttpClient(
                binding.evolution_backend_url,
                headers=binding.request_headers(),
            )
        )
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._poll_interval = poll_interval_seconds
        self._max_poll_attempts = max_poll_attempts
        self._ledger = ScienceRunStore(Path(state_root) / "science-runs")
        self._project_in_flight = ProjectInFlightCoordinator(self._ledger)
        self._output_root = Path(state_root) / "science-run-output"
        self._output_root.mkdir(mode=0o700, parents=True, exist_ok=True)
        self._condition = threading.Condition()
        self._lifecycle_lock = threading.RLock()
        self._cancel_events: dict[str, threading.Event] = {}
        self._active_rollouts: dict[str, _AdmittingRolloutClient] = {}
        self._cancel_workers: dict[str, threading.Thread] = {}
        self._pending_cancellations: set[str] = set()
        self._pending_finalizations: set[str] = set()
        self._closed = False
        self._recover_interrupted_runs()
        self._worker = threading.Thread(
            target=self._worker_loop,
            name="openevo-science-run-owner",
            daemon=True,
        )
        self._worker.start()
        with self._condition:
            self._condition.notify_all()

    def invoke(self, operation_id: str, arguments: Mapping[str, object]) -> object:
        handlers: dict[str, Callable[[Mapping[str, object]], object]] = {
            "listCoreRunsV1": self._list_runs,
            "createCoreRunV1": self._create_run,
            "getCoreRunV1": self._get_run,
            "deleteCoreRunV1": self._delete_run,
            "cancelCoreRunV1": self._cancel_run,
            "retryCoreRunV1": self._retry_run,
            "getCoreRunTimelineV1": self._run_timeline,
            "getCoreRunLogsV1": self._run_logs,
            "getCoreRunContextV1": self._run_context,
            "listCoreRunArtifactsV1": self._run_artifacts,
        }
        handler = handlers.get(operation_id)
        if handler is None:
            raise CoreRunControlError(
                "run_operation_unavailable",
                "The science run owner does not implement this operation.",
                http_status=503,
                retryable=False,
            )
        try:
            return handler(arguments)
        except CoreRunControlError:
            raise
        except ScienceRunNotFound as exc:
            raise _owner_error("run_not_found", str(exc), 404, False) from exc
        except ScienceRunPreconditionFailed as exc:
            raise _owner_error("run_etag_precondition_failed", str(exc), 412, True) from exc
        except ScienceRunIdempotencyConflict as exc:
            raise _owner_error(
                "idempotency_key_reused",
                "The idempotency key was already used for a different request.",
                409,
                False,
            ) from exc
        except ScienceProjectInFlight as exc:
            raise _owner_error(
                "run_project_in_flight",
                "The project already has an admitted task or successor transition.",
                409,
                True,
            ) from exc
        except ScienceRunConflict as exc:
            raise _owner_error("run_conflict", str(exc), 409, False) from exc
        except (ScienceRunStoreError, ValueError) as exc:
            raise _owner_error(
                "run_state_invalid",
                "Core could not read or update the durable science run state.",
                503,
                True,
            ) from exc

    def counts(self) -> tuple[int, int]:
        try:
            return (
                len(self._ledger.active_run_ids()),
                len(self._ledger.queued_run_ids()),
            )
        except ScienceRunStoreError as exc:
            raise _owner_error(
                "run_owner_unavailable",
                "Core could not inspect the durable run ledger.",
                503,
                True,
            ) from exc

    @property
    def project_in_flight_coordinator(self) -> ProjectInFlightCoordinator:
        return self._project_in_flight

    async def verify(self, check: GenerationBoundRunAdmissionCheck) -> None:
        task_id = check.task_id
        if task_id is None:
            raise _admission_denied()
        run_id = self._ledger.run_for_admitted_task(task_id)
        if run_id is None:
            raise _admission_denied()
        try:
            run = self._ledger.get_run(run_id)
        except ScienceRunStoreError as exc:
            raise _admission_denied() from exc
        if run.status not in _ACTIVE_FOR_ADMISSION:
            raise _admission_denied()
        allow_create = check.operation is not RunAdmissionOperation.ROLLOUT_TASK_SUBMIT
        accepted = self._ledger.register_admission(
            run_id=run_id,
            operation=check.operation.value,
            task_id=task_id,
            session_id=check.session_id,
            generation_digest=check.generation_digest,
            registry_digest=check.registry_digest,
            framework_lock_digest=check.framework_lock_digest,
            payload_sha256=check.payload_sha256,
            allow_create=allow_create,
        )
        if not accepted:
            raise _admission_denied()

    def close(self) -> None:
        self.request_stop()
        self._worker.join(timeout=30.0)
        if self._worker.is_alive():
            raise RuntimeError("science run owner did not stop before shutdown")
        for worker in list(self._cancel_workers.values()):
            worker.join(timeout=30.0)
            if worker.is_alive():
                raise RuntimeError("science run cancellation owner did not stop")
        self._ledger.close()

    def request_stop(self) -> None:
        with self._condition:
            if self._closed:
                return
            self._closed = True
            for event in self._cancel_events.values():
                event.set()
            for run_id in tuple(self._active_rollouts):
                self._schedule_rollout_cancellation(run_id)
            self._condition.notify_all()

    def _create_run(self, arguments: Mapping[str, object]) -> Response:
        request = cast(m.RunCreateV1, arguments["request"])
        idempotency_key = cast(str, arguments["idempotency_key"])
        replay, admission = self._ledger.begin_create_run(
            request=request,
            idempotency_key=idempotency_key,
        )
        if replay is not None:
            return _response(replay, status_code=202)
        assert admission is not None
        try:
            project = self._validate_create_request(request)
            self._ensure_services(project)
            with (
                self._lifecycle_lock,
                self._project_in_flight.locked(),
                self._pin_create_authority(request) as authority,
            ):
                project = self._validate_create_authority(request, *authority)
                input_context = self._ledger.revision_context(
                    request.project_id,
                    request.required_revision.revision.id,
                )
                now = self._timestamp()
                run_id = admission.run_id
                queued_reason = _execution_queue_reason()
                attempt = _queued_attempt(
                    run_id=run_id,
                    number=1,
                    now=now,
                    queued_reason=queued_reason,
                )
                run = _run_model(
                    {
                        "id": run_id,
                        "project_id": request.project_id,
                        "project_snapshot": request.project_snapshot,
                        "task_snapshot": request.task_snapshot,
                        "workspace_snapshot": request.workspace_snapshot,
                        "registry_digest": request.expected_registry_digest,
                        "execution_mode": project.spec.execution_mode,
                        "capture_mode": project.spec.capture_mode,
                        "status": m.RunStatus.QUEUED,
                        "queued_reason": queued_reason,
                        "current_attempt_id": attempt.id,
                        "current_attempt": attempt,
                        "attempt_count": 1,
                        "pinned_revision": request.required_revision.revision,
                        "required_revision": request.required_revision,
                        "revision_transition": authority[2].transition,
                        "created_at": now,
                        "updated_at": now,
                        "admitted_at": now,
                        "attempts": [attempt],
                    },
                    version=1,
                )
                stored, replayed = self._ledger.commit_create_run(
                    admission,
                    run=run,
                    input_context=input_context,
                )
        finally:
            self._ledger.abort_create_run(admission)
        if not replayed:
            self._append_timeline(
                stored,
                m.TimelinePhase.ADMISSION,
                m.TimelineEventStatus.PENDING,
                "Run queued",
                "Core accepted the immutable project and revision request.",
            )
            self._append_log(stored, m.LogLevel.INFO, "Science run queued for admission.")
            with self._condition:
                self._condition.notify_all()
        return _response(stored, status_code=202)

    def _list_runs(self, arguments: Mapping[str, object]) -> m.RunPageV1:
        project_id = cast(str | None, arguments["project_id"])
        status = cast(m.RunStatus | None, arguments["status"])
        sort = cast(str, arguments["sort"])
        direction = cast(str, arguments["direction"])
        values = [
            run
            for run in self._ledger.list_runs()
            if (project_id is None or run.project_id == project_id)
            and (status is None or run.status is status)
        ]
        values.sort(
            key=lambda run: (getattr(run, sort) or "", run.id),
            reverse=direction == "desc",
        )
        summaries = [
            m.RunSummaryV1.model_validate(run.model_dump(mode="python", exclude={"attempts"}))
            for run in values
        ]
        selected, cursor, has_more = page_items(
            summaries,
            limit=cast(int, arguments["limit"]),
            after=cast(str | None, arguments["after"]),
            query=f"runs:{project_id}:{status}:{sort}:{direction}",
        )
        return m.RunPageV1(items=selected, next_cursor=cursor, has_more=has_more)

    def _get_run(self, arguments: Mapping[str, object]) -> Response:
        return _response(self._ledger.get_run(cast(str, arguments["run_id"])))

    def _delete_run(self, arguments: Mapping[str, object]) -> Response:
        run_id = cast(str, arguments["run_id"])
        key = cast(str, arguments["idempotency_key"])
        digest = _request_digest(None, cast(str, arguments["if_match"]))

        def delete(current: m.RunV1, _version: int) -> None:
            if current.status not in _TERMINAL:
                raise ScienceRunConflict("only terminal runs can be deleted")
            return None

        self._ledger.apply_mutation(
            "deleteCoreRunV1",
            run_id,
            key,
            digest,
            expected_etag=cast(str, arguments["if_match"]),
            status_code=204,
            transform=delete,
            deleted=True,
        )
        return Response(status_code=204)

    def _cancel_run(self, arguments: Mapping[str, object]) -> Response:
        run_id = cast(str, arguments["run_id"])
        request = cast(m.RunCancelRequestV1, arguments["request"])
        key = cast(str, arguments["idempotency_key"])
        digest = _request_digest(request, cast(str, arguments["if_match"]))

        def cancel(current: m.RunV1, version: int) -> m.RunV1:
            if current.status in _TERMINAL:
                raise ScienceRunConflict("terminal run cannot be cancelled")
            status = (
                m.RunStatus.CANCELLING
                if current.status is m.RunStatus.RUNNING
                else m.RunStatus.CANCELLED
            )
            return _transition_run(current, status, version=version, now=self._timestamp())

        with self._lifecycle_lock:
            updated, replayed = self._ledger.apply_mutation(
                "cancelCoreRunV1",
                run_id,
                key,
                digest,
                expected_etag=cast(str, arguments["if_match"]),
                status_code=202,
                transform=cancel,
            )
            assert updated is not None
            if not replayed:
                self._cancel_events.setdefault(run_id, threading.Event()).set()
                if updated.status is m.RunStatus.CANCELLING:
                    self._schedule_rollout_cancellation(run_id)
        if replayed:
            return _response(updated, status_code=202)
        status = updated.status
        self._append_timeline(
            updated,
            m.TimelinePhase.TERMINAL,
            (
                m.TimelineEventStatus.RUNNING
                if status is m.RunStatus.CANCELLING
                else m.TimelineEventStatus.CANCELLED
            ),
            "Cancellation requested",
            "Core recorded the user cancellation request.",
        )
        return _response(updated, status_code=202)

    def _retry_run(self, arguments: Mapping[str, object]) -> Response:
        run_id = cast(str, arguments["run_id"])
        request = cast(m.RunRetryRequestV1, arguments["request"])
        key = cast(str, arguments["idempotency_key"])
        digest = _request_digest(request, cast(str, arguments["if_match"]))
        now = self._timestamp()

        def retry(current: m.RunV1, version: int) -> m.RunV1:
            if (
                current.status not in {m.RunStatus.FAILED, m.RunStatus.CANCELLED}
                or current.current_attempt_id != request.terminal_attempt_id
            ):
                raise ScienceRunConflict("run retry does not bind the terminal attempt")
            if current.attempt_count >= 100:
                raise ScienceRunConflict("science run retry capacity is exhausted")
            if current.admitted_at is None or current.pinned_revision is None:
                raise ScienceRunConflict("science run retry requires its immutable admission")
            return _retry_model(current, version=version, now=now)

        updated, replayed = self._ledger.apply_mutation(
            "retryCoreRunV1",
            run_id,
            key,
            digest,
            expected_etag=cast(str, arguments["if_match"]),
            status_code=202,
            transform=retry,
            claim_project=True,
            timeline_builders=(
                lambda accepted, sequence: self._timeline_entry(
                    accepted,
                    sequence,
                    m.TimelinePhase.ADMISSION,
                    m.TimelineEventStatus.PENDING,
                    "Retry queued",
                    "Core queued a new attempt against the same immutable request.",
                ),
            ),
        )
        assert updated is not None
        if replayed:
            return _response(updated, status_code=202)
        self._cancel_events[run_id] = threading.Event()
        with self._condition:
            self._condition.notify_all()
        return _response(updated, status_code=202)

    def _run_timeline(self, arguments: Mapping[str, object]) -> m.RunTimelinePageV1:
        run_id = cast(str, arguments["run_id"])
        sort = cast(str, arguments["sort"])
        direction = cast(str, arguments["direction"])
        values = self._ledger.timeline(run_id)
        values.sort(
            key=lambda item: (getattr(item, sort), item.sequence),
            reverse=direction == "desc",
        )
        selected, cursor, has_more = page_items(
            values,
            limit=cast(int, arguments["limit"]),
            after=cast(str | None, arguments["after"]),
            query=f"timeline:{run_id}:{sort}:{direction}",
        )
        return m.RunTimelinePageV1(items=selected, next_cursor=cursor, has_more=has_more)

    def _run_logs(self, arguments: Mapping[str, object]) -> m.LogPageV1:
        run_id = cast(str, arguments["run_id"])
        stream = cast(m.LogStream | None, arguments["stream"])
        sort = cast(str, arguments["sort"])
        direction = cast(str, arguments["direction"])
        values = [
            item for item in self._ledger.logs(run_id) if stream is None or item.stream is stream
        ]
        values.sort(
            key=lambda item: (getattr(item, sort), item.sequence),
            reverse=direction == "desc",
        )
        selected, cursor, has_more = page_items(
            values,
            limit=cast(int, arguments["limit"]),
            after=cast(str | None, arguments["after"]),
            query=f"logs:{run_id}:{stream}:{sort}:{direction}",
        )
        return m.LogPageV1(items=selected, next_cursor=cursor, has_more=has_more)

    def _run_context(self, arguments: Mapping[str, object]) -> Response:
        run = self._ledger.get_run(cast(str, arguments["run_id"]))
        context = self._ledger.input_context(run.id)
        ids = [
            item
            for artifact_type, values in context.items()
            if artifact_type != "dataset"
            for item in values
        ]
        artifacts = self._ledger.artifacts_by_ids(ids)
        refs = [
            m.ContextArtifactRefV1(
                artifact_id=artifact.id,
                artifact_type=artifact.artifact_type,
                target_id=artifact.target_id,
                revision=run.required_revision.revision,
            )
            for artifact in artifacts
        ]
        adapters = [
            m.AdapterRefV1(
                artifact_id=artifact.id,
                adapter_id=artifact.metadata.adapter_id,
                base_model_ref=artifact.metadata.base_model_ref,
                revision=run.required_revision.revision,
            )
            for artifact in artifacts
            if isinstance(artifact, m.ParametricMemoryArtifactSummaryV1)
        ]
        payload = run.model_dump(mode="python", exclude={"id", "attempts"})
        payload.update(
            run_id=run.id,
            token_level_metrics_available=run.capture_mode is m.CaptureMode.TOKEN_LEVEL,
            artifacts=refs,
            adapters=adapters,
        )
        return _response(m.RunContextV1.model_validate(payload), etag=run.etag)

    def _run_artifacts(self, arguments: Mapping[str, object]) -> m.ArtifactPageV1:
        run_id = cast(str, arguments["run_id"])
        artifact_type = cast(m.ArtifactType | None, arguments["artifact_type"])
        sort = cast(str, arguments["sort"])
        direction = cast(str, arguments["direction"])
        values = [
            item
            for item in self._ledger.artifacts_for_run(run_id)
            if artifact_type is None or item.artifact_type is artifact_type
        ]
        attribute = "display_name" if sort == "title" else sort
        values.sort(
            key=lambda item: (str(getattr(item, attribute)), item.id),
            reverse=direction == "desc",
        )
        selected, cursor, has_more = page_items(
            values,
            limit=cast(int, arguments["limit"]),
            after=cast(str | None, arguments["after"]),
            query=f"artifacts:{run_id}:{artifact_type}:{sort}:{direction}",
        )
        return m.ArtifactPageV1(items=selected, next_cursor=cursor, has_more=has_more)

    def _worker_loop(self) -> None:
        try:
            while True:
                with self._condition:
                    cancellations = sorted(self._pending_cancellations)
                    pending = sorted(self._pending_finalizations)
                    queued = self._ledger.queued_run_ids()
                    while not self._closed and not cancellations and not pending and not queued:
                        self._condition.wait(timeout=1.0)
                        cancellations = sorted(self._pending_cancellations)
                        pending = sorted(self._pending_finalizations)
                        queued = self._ledger.queued_run_ids()
                    if self._closed:
                        return
                    if cancellations:
                        run_id = cancellations[0]
                        self._pending_cancellations.discard(run_id)
                        cancel = True
                        finalize = False
                    elif pending:
                        run_id = pending[0]
                        self._pending_finalizations.discard(run_id)
                        cancel = False
                        finalize = True
                    else:
                        run_id = queued[0]
                        cancel = False
                        finalize = False
                if cancel:
                    if not self._resume_cancellation(run_id):
                        with self._condition:
                            if not self._closed:
                                self._pending_cancellations.add(run_id)
                                self._condition.wait(timeout=max(1.0, self._poll_interval))
                    continue
                if finalize:
                    if not self._resume_finalization(run_id):
                        with self._condition:
                            if not self._closed:
                                self._pending_finalizations.add(run_id)
                                self._condition.wait(timeout=max(1.0, self._poll_interval))
                    continue
                self._execute(run_id)
        finally:
            self._ledger.close()

    def _execute(self, run_id: str) -> None:
        cancellation = self._cancel_events.setdefault(run_id, threading.Event())
        service_lease: ServiceRunLease | None = None
        rollout_base: RolloutClientProtocol | None = None
        evolution: EvolutionClientProtocol | None = None
        rollout: _AdmittingRolloutClient | None = None
        try:
            if cancellation.is_set():
                raise _RunCancelled()
            run = self._ledger.mutate_run(
                run_id,
                lambda current, version: _worker_transition(
                    current,
                    expected=m.RunStatus.QUEUED,
                    status=m.RunStatus.PREPARING,
                    version=version,
                    now=self._timestamp(),
                    cancellation=cancellation,
                ),
            )
            self._append_timeline(
                run,
                m.TimelinePhase.PREPARATION,
                m.TimelineEventStatus.RUNNING,
                "Preparing remote services",
                "Core is verifying the managed runtime and service generation.",
            )
            request = self._ledger.request_for_run(run_id)
            project = self._validate_create_request(request)
            _, service_lease = self._ensure_services(project, require_binding=True)
            if service_lease is None:
                raise _service_generation_changed()
            binding = service_lease.binding
            if binding.registry_digest != request.expected_registry_digest:
                raise RuntimeError("managed service registry changed before execution")
            if cancellation.is_set():
                raise _RunCancelled()
            workspace_path = (
                None
                if project.workspace_kind is m.WorkspaceSourceKind.SCRATCH
                else self._project_store.workspace_snapshot_path(
                    project.id,
                    request.workspace_snapshot,
                )
            )
            compiled = compile_science_execution(
                project,
                run_id=run_id,
                binding=binding,
                workspace_path=workspace_path,
            )
            rollout_base = self._rollout_factory(binding)
            evolution = self._evolution_factory(binding)
            rollout = _AdmittingRolloutClient(
                rollout_base,
                evolution_client=evolution,
                owner=self,
                run_id=run_id,
                binding=binding,
                cancellation=cancellation,
            )
            with self._lifecycle_lock:
                if cancellation.is_set():
                    raise _RunCancelled()
                self._active_rollouts[run_id] = rollout
                run = self._ledger.mutate_run(
                    run_id,
                    lambda current, version: _worker_transition(
                        current,
                        expected=m.RunStatus.PREPARING,
                        status=m.RunStatus.RUNNING,
                        version=version,
                        now=self._timestamp(),
                        cancellation=cancellation,
                    ),
                )
            self._append_timeline(
                run,
                m.TimelinePhase.EXECUTION,
                m.TimelineEventStatus.RUNNING,
                "Science task running",
                "The Codex harness is executing on the remote managed runtime.",
            )
            output_dir = self._output_root / run_id
            output_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
            try:
                result = self._runner(
                    compiled.config,
                    run_id=run_id,
                    task_ids=[compiled.task_id],
                    rounds_override=1,
                    initial_context_artifact_ids=self._ledger.input_context(run_id),
                    core_project_scope=compiled._core_project_scope,
                    managed_worker=True,
                    output_dir=output_dir,
                    artifact_root=output_dir / "worker-artifacts",
                    rollout_client=rollout,
                    evolution_client=evolution,
                    worker_runner=self._managed_worker_runner(evolution, cancellation),
                    poll_interval_seconds=self._poll_interval,
                    max_poll_attempts=self._max_poll_attempts,
                    executable_registry=self._registry,
                    execution_profile=compiled.execution_profile,
                )
                runtime_context_receipt = rollout.runtime_context_receipt
                runtime_context_authority = rollout.runtime_context_authority
                result.pop("runtime_context_receipt", None)
                result.pop("_runtime_context_authority", None)
                if runtime_context_receipt is not None:
                    result["runtime_context_receipt"] = runtime_context_receipt
                    result["_runtime_context_authority"] = runtime_context_authority
            finally:
                with self._lifecycle_lock:
                    if self._active_rollouts.get(run_id) is rollout:
                        self._active_rollouts.pop(run_id, None)
            if cancellation.is_set():
                raise _RunCancelled()
            if result.get("status") != "completed":
                raise RuntimeError("science experiment did not complete")
            self._ledger.store_result(run_id, result)
            self._finalize_completed_result(run_id, result, cancellation=cancellation)
        except _RunCancelled:
            self._complete_or_defer_cancellation(run_id, rollout)
        except _RunFinalizationConflict as exc:
            self._finish_failed(
                run_id,
                _owner_error(
                    "run_successor_conflict",
                    str(exc),
                    409,
                    False,
                ),
            )
        except BaseException as exc:
            if cancellation.is_set():
                self._complete_or_defer_cancellation(run_id, rollout)
            elif self._ledger.result_for_run(run_id) is not None:
                with self._condition:
                    self._pending_finalizations.add(run_id)
            else:
                self._finish_failed(run_id, exc)
        finally:
            if rollout is not None:
                with self._lifecycle_lock:
                    if self._active_rollouts.get(run_id) is rollout:
                        self._active_rollouts.pop(run_id, None)
            for client in (rollout_base, evolution):
                if client is None:
                    continue
                close = getattr(client, "close", None)
                if callable(close):
                    close()
            if service_lease is not None:
                service_lease.close()
            with self._condition:
                self._condition.notify_all()

    def _schedule_rollout_cancellation(self, run_id: str) -> None:
        authority = self._active_rollouts.get(run_id)
        existing = self._cancel_workers.get(run_id)
        if authority is None or (existing is not None and existing.is_alive()):
            return

        def terminate() -> None:
            try:
                authority.terminate()
            except BaseException:
                return
            finally:
                with self._condition:
                    self._condition.notify_all()

        worker = threading.Thread(
            target=terminate,
            name=f"openevo-science-run-cancel-{run_id}",
            daemon=True,
        )
        self._cancel_workers[run_id] = worker
        worker.start()

    def _complete_or_defer_cancellation(
        self,
        run_id: str,
        authority: _AdmittingRolloutClient | None,
    ) -> None:
        if authority is None or authority.termination_proven:
            self._finish_cancelled(run_id)
            return
        with self._condition:
            self._pending_cancellations.add(run_id)
            self._condition.notify_all()

    def _resume_cancellation(self, run_id: str) -> bool:
        try:
            run = self._ledger.get_run(run_id)
            if run.status is m.RunStatus.CANCELLED:
                return True
            if run.status is not m.RunStatus.CANCELLING:
                raise ScienceRunStoreError("recoverable cancellation has invalid run status")
            admission = self._ledger.rollout_task_admission(run_id)
            if admission is None:
                self._finish_cancelled(run_id)
                return True
            binding = self._services.run_binding()
            if (
                admission.generation_digest != binding.generation_digest
                or admission.registry_digest != binding.registry_digest
                or admission.framework_lock_digest != binding.framework_lock_digest
                or admission.registry_digest != run.registry_digest
            ):
                return False
            client = self._rollout_factory(binding)
            try:
                result = client.cancel_task(admission.task_id)
            finally:
                close = getattr(client, "close", None)
                if callable(close):
                    close()
            if result.get("task_id") != admission.task_id or result.get("status") != "cancelled":
                return False
            self._finish_cancelled(run_id)
            return True
        except BaseException:
            return False

    def _resume_finalization(self, run_id: str) -> bool:
        cancellation = self._cancel_events.setdefault(run_id, threading.Event())
        try:
            result = self._ledger.result_for_run(run_id)
            if result is None:
                raise ScienceRunStoreError("recoverable run has no persisted result")
            self._finalize_completed_result(run_id, result, cancellation=cancellation)
            return True
        except _RunCancelled:
            self._finish_cancelled(run_id)
            return True
        except _RunFinalizationConflict as exc:
            self._finish_failed(
                run_id,
                _owner_error(
                    "run_successor_conflict",
                    str(exc),
                    409,
                    False,
                ),
            )
            return True
        except BaseException:
            return False

    def _finalize_completed_result(
        self,
        run_id: str,
        result: Mapping[str, object],
        *,
        cancellation: threading.Event,
    ) -> None:
        with self._lifecycle_lock, self._project_in_flight.locked():
            run = self._ledger.get_run(run_id)
            if run.status is m.RunStatus.SUCCEEDED:
                return
            if cancellation.is_set() or run.status in {
                m.RunStatus.CANCELLING,
                m.RunStatus.CANCELLED,
            }:
                raise _RunCancelled()
            if run.status is not m.RunStatus.RUNNING:
                raise ScienceRunConflict("only a running task can publish its result")
            request = self._ledger.request_for_run(run_id)
            context = _result_context(result)
            runtime_context_receipt = _runtime_context_receipt(result)
            runtime_context_authority = _runtime_context_authority(result)
            _verify_runtime_context_receipt(
                runtime_context_receipt,
                revision_id=request.required_revision.revision.id,
                artifacts=_runtime_context_artifact_authority(self._ledger, run_id),
                authority=runtime_context_authority,
            )
            project = self._project_store.get_project(run.project_id)
            predecessor = request.required_revision.revision
            active_revision = project.active_revision
            if active_revision is None:
                raise _RunFinalizationConflict(
                    "project revision advanced before run finalization completed"
                )
            if active_revision == predecessor:
                preflight_revision = m.RevisionRefV1(
                    id=f"preflight-{run_id}",
                    project_id=run.project_id,
                    generation=predecessor.generation + 1,
                    manifest_sha256="0" * 64,
                )
                _project_artifacts(
                    result,
                    project=project,
                    revision=preflight_revision,
                    run_id=run_id,
                )
            try:
                successor = self._project_store.activate_evolution_revision(
                    run.project_id,
                    predecessor=predecessor,
                    run_id=run_id,
                    context_artifact_ids=context,
                )
            except ResourceConflictError as exc:
                raise _RunFinalizationConflict(
                    "project revision advanced before run finalization completed"
                ) from exc
            project = self._project_store.get_project(run.project_id)
            if project.active_revision != successor.revision:
                raise _RunFinalizationConflict(
                    "project revision advanced before run finalization completed"
                )
            artifacts = _project_artifacts(
                result,
                project=project,
                revision=successor.revision,
                run_id=run_id,
            )
            self._ledger.store_artifacts(run_id, successor.revision, artifacts)
            self._ledger.set_revision_context(run.project_id, successor.revision.id, context)
            artifact_ids = [artifact.id for artifact in artifacts]
            log_builders = [
                lambda terminal, sequence: self._log_entry(
                    terminal,
                    sequence,
                    m.LogLevel.INFO,
                    "Science run completed successfully.",
                )
            ]
            if runtime_context_receipt is not None:
                receipt_sha256 = hashlib.sha256(
                    _canonical_bytes(runtime_context_receipt)
                ).hexdigest()
                log_builders.append(
                    lambda terminal, sequence, digest=receipt_sha256: self._log_entry(
                        terminal,
                        sequence,
                        m.LogLevel.INFO,
                        f"Runtime context receipt v3: {digest}",
                    )
                )
            self._ledger.mutate_run_with_evidence(
                run_id,
                lambda current, version: _worker_transition(
                    current,
                    expected=m.RunStatus.RUNNING,
                    status=m.RunStatus.SUCCEEDED,
                    version=version,
                    now=self._timestamp(),
                    cancellation=cancellation,
                ),
                timeline_builders=(
                    lambda terminal, sequence: self._timeline_entry(
                        terminal,
                        sequence,
                        m.TimelinePhase.EVOLUTION,
                        m.TimelineEventStatus.SUCCEEDED,
                        "Evolution completed",
                        "Enabled evolution methods produced the next-session context.",
                        artifact_ids=artifact_ids,
                    ),
                    lambda terminal, sequence: self._timeline_entry(
                        terminal,
                        sequence,
                        m.TimelinePhase.REVISION,
                        m.TimelineEventStatus.SUCCEEDED,
                        "Next revision activated",
                        "The evolved context will be used by the next science task.",
                        artifact_ids=artifact_ids,
                    ),
                    lambda terminal, sequence: self._timeline_entry(
                        terminal,
                        sequence,
                        m.TimelinePhase.TERMINAL,
                        m.TimelineEventStatus.SUCCEEDED,
                        "Run completed",
                        "The science task and cross-session evolution completed successfully.",
                        artifact_ids=artifact_ids,
                    ),
                ),
                log_builders=tuple(log_builders),
            )

    def _managed_worker_runner(
        self,
        evolution: EvolutionClientProtocol,
        cancellation: threading.Event,
    ) -> Callable[..., list[dict[str, Any]]]:
        def wait_for_job(**kwargs: object) -> list[dict[str, Any]]:
            expected_job_id = cast(str, kwargs["expected_job_id"])
            observe = getattr(evolution, "get_internal_job_result", None)
            if not callable(observe):
                raise RuntimeError("managed evolution client cannot observe jobs")
            for attempt in range(self._max_poll_attempts):
                if attempt and cancellation.wait(self._poll_interval):
                    raise _RunCancelled()
                result = observe(expected_job_id)
                state = str(result.get("state"))
                if state == "succeeded":
                    return [result]
                if state in {"failed", "cancelled", "expired"}:
                    return [
                        {
                            "artifact_ids": [],
                            "error": "managed_evolution_job_failed",
                            "job_id": expected_job_id,
                        }
                    ]
            raise TimeoutError("managed evolution job did not finish in time")

        return wait_for_job

    def _ensure_services(
        self,
        project: m.ProjectV1,
        *,
        require_binding: bool = False,
    ) -> tuple[ServiceGroupSnapshot, ServiceRunLease | None]:
        image = MANAGED_RUNTIME_IMAGES["managed_science"]
        lease: ServiceRunLease | None = None
        try:
            if project.spec.execution_mode is m.ExecutionMode.CODEX_SUBSCRIPTION_TRANSCRIPT:
                execution_mode = ServiceExecutionMode.CODEX_SUBSCRIPTION_TRANSCRIPT
                arguments = {
                    "codex_model": project.spec.agent_model_ref,
                    "runtime_image": image,
                }
            else:
                execution_mode = ServiceExecutionMode.SELF_DEPLOYED
                arguments = {
                    "model_ref": project.spec.agent_model_ref,
                    "runtime_image": image,
                }
            if require_binding:
                snapshot, lease = self._services.ensure_run_binding(
                    execution_mode,
                    **arguments,
                )
            else:
                snapshot = self._services.ensure(execution_mode, **arguments)
                lease = None
        except CoreServiceControlError as exc:
            raise _owner_error(
                "run_service_supervisor_failed",
                "Core could not verify managed service readiness.",
                503,
                True,
            ) from exc
        if snapshot.execution_mode is not execution_mode:
            if lease is not None:
                lease.close()
            raise _owner_error(
                "run_service_mode_mismatch",
                "Managed services do not match the saved project execution mode.",
                503,
                True,
            )
        if not snapshot.run_ready:
            if lease is not None:
                lease.close()
            raise _readiness_error(snapshot.run_readiness_code)
        if snapshot.runtime_image != image:
            if lease is not None:
                lease.close()
            raise _service_generation_changed()
        binding = None if lease is None else lease.binding
        if require_binding and (
            binding is None
            or binding.execution_mode is not snapshot.execution_mode
            or binding.runtime_image != snapshot.runtime_image
            or binding.runtime_image_immutable_reference
            != snapshot.runtime_image_immutable_reference
            or binding.runtime_identity_digest != snapshot.runtime_identity_digest
            or binding.generation_digest != snapshot.generation_digest
        ):
            if lease is not None:
                lease.close()
            raise _service_generation_changed()
        return snapshot, lease

    def _validate_create_request(self, request: m.RunCreateV1) -> m.ProjectV1:
        with self._pin_create_authority(request) as authority:
            return self._validate_create_authority(request, *authority)

    @contextmanager
    def _pin_create_authority(
        self,
        request: m.RunCreateV1,
    ) -> Iterator[tuple[m.ProjectV1, m.RevisionV1, m.RevisionHeadV1]]:
        try:
            with self._project_store.pin_science_run_authority(
                request.project_id,
                request.required_revision.revision.id,
            ) as authority:
                yield authority
        except ResourceNotFoundError as exc:
            raise _owner_error(
                "run_project_not_found", "The saved project was not found.", 404, False
            ) from exc

    def _validate_create_authority(
        self,
        request: m.RunCreateV1,
        project: m.ProjectV1,
        revision: m.RevisionV1,
        head: m.RevisionHeadV1,
    ) -> m.ProjectV1:
        if project.status is not m.ProjectStatus.READY:
            raise _owner_error(
                "run_project_not_ready", "The saved project is not ready.", 409, True
            )
        if (
            request.project_snapshot != project.current_project_snapshot
            or request.task_snapshot != project.current_task_snapshot
            or request.workspace_snapshot != project.current_workspace_snapshot
            or request.expected_registry_digest != project.registry_digest
            or request.expected_registry_digest != self._registry.snapshot.registry_digest
        ):
            raise _owner_error(
                "run_snapshot_mismatch",
                "The run request no longer matches the authoritative project snapshots.",
                409,
                False,
            )
        if (
            request.required_revision.relation is not m.RequiredRevisionRelation.ACTIVE
            or request.required_revision.revision != head.active_revision
            or request.required_revision.reachable_from_revision_id != head.active_revision.id
            or revision.status is not m.RevisionStatus.ACTIVE
        ):
            raise _owner_error(
                "run_revision_uncommitted",
                "The required project revision is not active yet.",
                409,
                True,
            )
        if head.successor_revision is not None or head.transition is not None:
            transition = head.transition
            retryable = transition is None or transition.state not in {
                m.RevisionTransitionState.FAILED,
                m.RevisionTransitionState.CANCELLED,
                m.RevisionTransitionState.UNAVAILABLE,
            }
            raise _owner_error(
                "run_project_successor_not_ready",
                "The successor project head must be resolved before another run starts.",
                409,
                retryable,
            )
        return project

    def _recover_interrupted_runs(self) -> None:
        for run_id in self._ledger.active_run_ids():
            run = self._ledger.get_run(run_id)
            if run.status is m.RunStatus.CANCELLING:
                self._cancel_events.setdefault(run_id, threading.Event()).set()
                if self._ledger.result_for_run(run_id) is not None:
                    self._finish_cancelled(run_id)
                else:
                    self._pending_cancellations.add(run_id)
            elif self._ledger.result_for_run(run_id) is not None:
                self._pending_finalizations.add(run_id)
            elif (
                run.status is m.RunStatus.RUNNING
                and self._ledger.rollout_task_admission(run_id) is not None
            ):
                self._ledger.mutate_run(
                    run_id,
                    lambda current, version: _transition_run(
                        current,
                        m.RunStatus.CANCELLING,
                        version=version,
                        now=self._timestamp(),
                    ),
                )
                self._cancel_events.setdefault(run_id, threading.Event()).set()
                self._pending_cancellations.add(run_id)
            else:
                self._finish_failed(run_id, RuntimeError("Core restarted during the run"))

    def _finish_cancelled(self, run_id: str) -> None:
        try:
            current = self._ledger.get_run(run_id)
            if current.status in _TERMINAL:
                return
            terminal = self._ledger.mutate_run(
                run_id,
                lambda run, version: _transition_run(
                    run,
                    m.RunStatus.CANCELLED,
                    version=version,
                    now=self._timestamp(),
                ),
            )
            self._append_timeline(
                terminal,
                m.TimelinePhase.TERMINAL,
                m.TimelineEventStatus.CANCELLED,
                "Run cancelled",
                "Core stopped the run after the cancellation request.",
            )
        except ScienceRunStoreError:
            return

    def _finish_failed(self, run_id: str, exc: BaseException) -> None:
        try:
            current = self._ledger.get_run(run_id)
            if current.status in _TERMINAL:
                return
            if isinstance(exc, CoreRunControlError):
                error = _api_error(exc.code, str(exc), retryable=exc.retryable)
            else:
                error = _api_error(
                    "science_run_failed",
                    "The remote science run did not complete.",
                    retryable=True,
                )
            terminal = self._ledger.mutate_run(
                run_id,
                lambda run, version: _transition_run(
                    run,
                    m.RunStatus.FAILED,
                    version=version,
                    now=self._timestamp(),
                    error=error,
                ),
            )
            self._append_timeline(
                terminal,
                m.TimelinePhase.TERMINAL,
                m.TimelineEventStatus.FAILED,
                "Run failed",
                "Core preserved the failed attempt for inspection and retry.",
                error=error,
            )
            self._append_log(terminal, m.LogLevel.ERROR, "Science run failed.")
        except ScienceRunStoreError:
            return

    def _append_timeline(
        self,
        run: m.RunV1,
        phase: m.TimelinePhase,
        status: m.TimelineEventStatus,
        title: str,
        message: str,
        *,
        artifact_ids: Sequence[str] = (),
        error: m.ApiErrorV1 | None = None,
    ) -> None:
        self._ledger.append_timeline(
            run.id,
            lambda sequence: self._timeline_entry(
                run,
                sequence,
                phase,
                status,
                title,
                message,
                artifact_ids=artifact_ids,
                error=error,
            ),
        )

    def _timeline_entry(
        self,
        run: m.RunV1,
        sequence: int,
        phase: m.TimelinePhase,
        status: m.TimelineEventStatus,
        title: str,
        message: str,
        *,
        artifact_ids: Sequence[str] = (),
        error: m.ApiErrorV1 | None = None,
    ) -> m.TimelineEntryV1:
        occurred_at = self._timestamp()
        identity = {
            "artifact_ids": list(artifact_ids),
            "attempt_id": run.current_attempt_id,
            "error": None if error is None else error.model_dump(mode="json"),
            "message": message,
            "occurred_at": occurred_at,
            "phase": phase.value,
            "run_id": run.id,
            "sequence": sequence,
            "status": status.value,
            "title": title,
        }
        digest = hashlib.sha256(_canonical_bytes(identity)).hexdigest()
        return m.TimelineEntryV1(
            id=f"timeline-{digest[:24]}",
            run_id=run.id,
            attempt_id=run.current_attempt_id,
            sequence=sequence,
            service_id="core-control",
            phase=phase,
            status=status,
            title=title,
            message=message,
            occurred_at=occurred_at,
            artifact_ids=list(artifact_ids),
            content_sha256=digest,
            error=error,
        )

    def _append_log(self, run: m.RunV1, level: m.LogLevel, message: str) -> None:
        self._ledger.append_log(
            run.id,
            lambda sequence: self._log_entry(run, sequence, level, message),
        )

    def _log_entry(
        self,
        run: m.RunV1,
        sequence: int,
        level: m.LogLevel,
        message: str,
    ) -> m.LogEntryV1:
        occurred_at = self._timestamp()
        identity = {
            "attempt_id": run.current_attempt_id,
            "level": level.value,
            "message": message,
            "occurred_at": occurred_at,
            "run_id": run.id,
            "sequence": sequence,
        }
        digest = hashlib.sha256(_canonical_bytes(identity)).hexdigest()
        return m.LogEntryV1(
            id=f"log-{digest[:24]}",
            sequence=sequence,
            occurred_at=occurred_at,
            stream=m.LogStream.CORE,
            level=level,
            message=message,
            run_id=run.id,
            attempt_id=run.current_attempt_id,
            service_id="core-control",
            content_sha256=digest,
        )

    def _timestamp(self) -> str:
        return (
            self._clock()
            .astimezone(timezone.utc)
            .isoformat(timespec="microseconds")
            .replace("+00:00", "Z")
        )


class _AdmittingRolloutClient:
    def __init__(
        self,
        client: RolloutClientProtocol,
        *,
        evolution_client: EvolutionClientProtocol,
        owner: CoreScienceRunOwner,
        run_id: str,
        binding: ServiceRunBinding,
        cancellation: threading.Event,
    ) -> None:
        self._client = client
        self._evolution_client = evolution_client
        self._owner = owner
        self._run_id = run_id
        self._binding = binding
        self._cancellation = cancellation
        self._runtime_context_receipt: dict[str, object] | None = None
        self._runtime_context_authority: dict[str, object] | None = None
        self._expected_context_artifact_ids: tuple[str, ...] = ()
        self._expected_revision_id: str | None = None
        self._expected_context_artifacts: dict[str, dict[str, str]] = {}
        self._instruction: str | None = None
        self._operation_lock = threading.RLock()
        self._termination_condition = threading.Condition()
        self._submitted_task_id: str | None = None
        self._termination_in_progress = False
        self._termination_complete = False
        self._termination_error: BaseException | None = None

    @property
    def runtime_context_receipt(self) -> dict[str, object] | None:
        if self._runtime_context_receipt is None:
            return None
        return _runtime_context_receipt_copy(self._runtime_context_receipt)

    @property
    def runtime_context_authority(self) -> dict[str, object] | None:
        if self._runtime_context_authority is None:
            return None
        return _runtime_context_receipt_copy(self._runtime_context_authority)

    @property
    def termination_proven(self) -> bool:
        with self._termination_condition:
            return self._submitted_task_id is None or self._termination_complete

    def submit_task(self, payload: dict[str, Any]) -> str:
        canonical = canonicalize_task_request(payload)
        task_id = canonical.request.task_id
        payload_metadata = canonical.payload.get("metadata")
        evolution_metadata = (
            payload_metadata.get("evolution") if isinstance(payload_metadata, dict) else None
        )
        context_artifact_ids = (
            evolution_metadata.get("context_artifact_ids")
            if isinstance(evolution_metadata, dict)
            else []
        )
        if (
            not isinstance(context_artifact_ids, list)
            or not all(
                isinstance(artifact_id, str) and artifact_id
                for artifact_id in context_artifact_ids
            )
            or len(context_artifact_ids) != len(set(context_artifact_ids))
        ):
            raise ValueError("rollout payload has invalid context artifact identity")
        self._expected_context_artifact_ids = tuple(context_artifact_ids)
        instruction = canonical.payload.get("instruction")
        if not isinstance(instruction, str) or not instruction:
            raise ValueError("rollout payload has no instruction authority")
        self._instruction = instruction
        authoritative = _runtime_context_artifact_authority(
            self._owner._ledger,
            self._run_id,
        )
        if self._expected_context_artifact_ids != tuple(authoritative):
            raise ValueError("rollout payload context differs from revision authority")
        openevo_metadata = (
            payload_metadata.get("openevo") if isinstance(payload_metadata, dict) else None
        )
        revision_id = (
            openevo_metadata.get("revision_id") if isinstance(openevo_metadata, dict) else None
        )
        required_revision_id = self._owner._ledger.request_for_run(
            self._run_id
        ).required_revision.revision.id
        if self._expected_context_artifact_ids:
            if revision_id != required_revision_id:
                raise ValueError("rollout payload revision differs from run authority")
            self._expected_revision_id = required_revision_id
        elif revision_id is not None and revision_id != required_revision_id:
            raise ValueError("rollout payload revision differs from run authority")
        self._expected_context_artifacts = authoritative
        accepted = self._owner._ledger.register_admission(
            run_id=self._run_id,
            operation=RunAdmissionOperation.ROLLOUT_TASK_SUBMIT.value,
            task_id=task_id,
            session_id=None,
            generation_digest=self._binding.generation_digest,
            registry_digest=self._binding.registry_digest,
            framework_lock_digest=self._binding.framework_lock_digest,
            payload_sha256=canonical.payload_sha256,
            allow_create=True,
        )
        if not accepted:
            raise RuntimeError("rollout admission changed for an existing task")
        with self._operation_lock:
            if self._cancellation.is_set():
                raise _RunCancelled()
            if self._submitted_task_id not in {None, task_id}:
                raise RuntimeError("rollout task identity changed within one run")
            self._submitted_task_id = task_id
            submitted = self._client.submit_task(canonical.payload)
            if submitted != task_id:
                raise RuntimeError("rollout service changed the admitted task identity")
            if self._cancellation.is_set():
                self.terminate()
                raise _RunCancelled()
            return submitted

    def cancel_task(self, task_id: str) -> dict[str, Any]:
        if self._submitted_task_id not in {None, task_id}:
            raise RuntimeError("rollout cancellation task identity changed")
        self._submitted_task_id = task_id
        self.terminate()
        return {"task_id": task_id, "status": "cancelled"}

    def terminate(self) -> None:
        with self._termination_condition:
            while self._termination_in_progress:
                self._termination_condition.wait()
            if self._termination_complete:
                return
            self._termination_in_progress = True
        error: BaseException | None = None
        try:
            with self._operation_lock:
                task_id = self._submitted_task_id
                if task_id is None:
                    return
                result = self._client.cancel_task(task_id)
                if result.get("task_id") != task_id or result.get("status") != "cancelled":
                    raise RuntimeError("rollout cancellation did not prove termination")
        except BaseException as exc:
            error = exc
            raise
        finally:
            with self._termination_condition:
                self._termination_in_progress = False
                self._termination_complete = error is None
                self._termination_error = error
                self._termination_condition.notify_all()

    def get_task(self, task_id: str) -> dict[str, Any]:
        if self._cancellation.is_set():
            self.cancel_task(task_id)
            raise _RunCancelled()
        result = self._client.get_task(task_id)
        if self._cancellation.is_set():
            self.cancel_task(task_id)
            raise _RunCancelled()
        if result.get("status") == "completed":
            receipt = _rollout_runtime_context_receipt(result)
            authority = None
            if receipt is not None:
                if self._expected_revision_id is None or self._instruction is None:
                    raise ValueError("rollout runtime context authority is incomplete")
                context = self._evolution_client.get_context_runtime_authority(
                    cast(str, receipt["context_id"])
                )
                authority = build_runtime_injection_plan(
                    context=context,
                    revision_id=self._expected_revision_id,
                    instruction=self._instruction,
                    expected_artifact_ids=self._expected_context_artifact_ids,
                ).authority
            _verify_runtime_context_receipt(
                receipt,
                revision_id=self._expected_revision_id,
                artifacts=self._expected_context_artifacts,
                authority=authority,
            )
            if (
                self._runtime_context_receipt is not None
                and self._runtime_context_receipt != receipt
            ):
                raise RuntimeError("rollout runtime context receipt changed")
            self._runtime_context_receipt = (
                None if receipt is None else _runtime_context_receipt_copy(receipt)
            )
            self._runtime_context_authority = (
                None if authority is None else _runtime_context_receipt_copy(authority)
            )
        return result


def _worker_transition(
    run: m.RunV1,
    *,
    expected: m.RunStatus,
    status: m.RunStatus,
    version: int,
    now: str,
    cancellation: threading.Event,
) -> m.RunV1:
    if cancellation.is_set() or run.status in {
        m.RunStatus.CANCELLING,
        m.RunStatus.CANCELLED,
    }:
        raise _RunCancelled()
    if run.status is not expected:
        raise ScienceRunConflict(
            f"science run changed from {expected.value} before worker transition"
        )
    return _transition_run(run, status, version=version, now=now)


def _transition_run(
    run: m.RunV1,
    status: m.RunStatus,
    *,
    version: int,
    now: str,
    error: m.ApiErrorV1 | None = None,
) -> m.RunV1:
    allowed = {
        m.RunStatus.QUEUED: {m.RunStatus.PREPARING, m.RunStatus.CANCELLED},
        m.RunStatus.PREPARING: {
            m.RunStatus.RUNNING,
            m.RunStatus.FAILED,
            m.RunStatus.CANCELLED,
        },
        m.RunStatus.RUNNING: {
            m.RunStatus.CANCELLING,
            m.RunStatus.SUCCEEDED,
            m.RunStatus.FAILED,
            m.RunStatus.CANCELLED,
        },
        m.RunStatus.CANCELLING: {m.RunStatus.CANCELLED},
    }
    if status not in allowed.get(run.status, set()):
        raise ScienceRunConflict(
            f"science run cannot transition from {run.status.value} to {status.value}"
        )
    attempts = list(run.attempts)
    if status is m.RunStatus.PREPARING:
        if attempts and attempts[-1].status is m.RunStatus.QUEUED:
            attempt = attempts[-1].model_copy(
                update={
                    "status": status,
                    "queued_reason": None,
                    "updated_at": now,
                }
            )
            attempts[-1] = attempt
        else:
            attempt = m.AttemptV1(
                id=f"attempt-{secrets.token_hex(16)}",
                run_id=run.id,
                number=len(attempts) + 1,
                status=status,
                created_at=now,
                updated_at=now,
            )
            attempts.append(attempt)
    elif attempts:
        current = attempts[-1]
        started_at = current.started_at
        if status in {
            m.RunStatus.RUNNING,
            m.RunStatus.CANCELLING,
            m.RunStatus.SUCCEEDED,
            m.RunStatus.FAILED,
        }:
            started_at = started_at or run.started_at or run.admitted_at or now
        attempts[-1] = current.model_copy(
            update={
                "status": status,
                "queued_reason": None,
                "updated_at": now,
                "started_at": started_at,
                "finished_at": now if status in _TERMINAL else None,
                "error": error if status is m.RunStatus.FAILED else None,
            }
        )
    admitted_at = run.admitted_at
    pinned_revision = run.pinned_revision
    if status is not m.RunStatus.CANCELLED:
        admitted_at = admitted_at or now
        pinned_revision = pinned_revision or run.required_revision.revision
    started_at = run.started_at
    if status in {
        m.RunStatus.RUNNING,
        m.RunStatus.CANCELLING,
        m.RunStatus.SUCCEEDED,
        m.RunStatus.FAILED,
    }:
        started_at = started_at or now
    data = run.model_dump(mode="python", exclude={"etag", "attempts"})
    data.update(
        status=status,
        queued_reason=None,
        current_attempt_id=attempts[-1].id if attempts else None,
        current_attempt=attempts[-1] if attempts else None,
        attempt_count=len(attempts),
        current_error=error if status is m.RunStatus.FAILED else None,
        updated_at=now,
        admitted_at=admitted_at,
        pinned_revision=pinned_revision,
        started_at=started_at,
        finished_at=now if status in _TERMINAL else None,
        attempts=attempts,
    )
    return _run_model(data, version=version)


def _retry_model(run: m.RunV1, *, version: int, now: str) -> m.RunV1:
    queued_reason = _execution_queue_reason()
    attempt = _queued_attempt(
        run_id=run.id,
        number=len(run.attempts) + 1,
        now=now,
        queued_reason=queued_reason,
    )
    attempts = [
        *run.attempts,
        attempt,
    ]
    data = run.model_dump(mode="python", exclude={"etag", "attempts"})
    data.update(
        status=m.RunStatus.QUEUED,
        queued_reason=queued_reason,
        current_attempt_id=attempts[-1].id,
        current_attempt=attempts[-1],
        attempt_count=len(attempts),
        current_error=None,
        started_at=None,
        finished_at=None,
        updated_at=now,
        attempts=attempts,
    )
    return _run_model(data, version=version)


def _execution_queue_reason() -> m.QueuedReasonV1:
    return m.QueuedReasonV1(
        code=m.QueuedReasonCode.CAPACITY,
        summary="The admitted run is waiting for execution capacity.",
        retry_after_seconds=1,
    )


def _queued_attempt(
    *,
    run_id: str,
    number: int,
    now: str,
    queued_reason: m.QueuedReasonV1,
) -> m.AttemptV1:
    return m.AttemptV1(
        id=f"attempt-{secrets.token_hex(16)}",
        run_id=run_id,
        number=number,
        status=m.RunStatus.QUEUED,
        queued_reason=queued_reason,
        created_at=now,
        updated_at=now,
    )


def _run_model(data: Mapping[str, object], *, version: int) -> m.RunV1:
    provisional = m.RunV1.model_validate({**data, "etag": '"' + "0" * 64 + '"'})
    payload = provisional.model_dump(mode="json", exclude={"etag"})
    etag = (
        '"'
        + hashlib.sha256(
            _canonical_bytes({"resource_version": version, "run": payload})
        ).hexdigest()
        + '"'
    )
    return m.RunV1.model_validate_json(_canonical_bytes({**payload, "etag": etag}))


def _single_science_round(result: Mapping[str, object]) -> dict[str, Any]:
    tasks = result.get("tasks")
    if not isinstance(tasks, list) or len(tasks) != 1 or not isinstance(tasks[0], dict):
        raise ValueError("science result must contain exactly one task")
    rounds = tasks[0].get("rounds")
    if not isinstance(rounds, list) or len(rounds) != 1 or not isinstance(rounds[0], dict):
        raise ValueError("science result task must contain exactly one round")
    return rounds[0]


def _result_context(result: Mapping[str, object]) -> dict[str, list[str]]:
    raw = _single_science_round(result).get("artifact_ids")
    if not isinstance(raw, dict):
        raise ValueError("science result has no terminal artifact context")
    if len(raw) > 128:
        raise ValueError("science result has too many artifact context types")
    context: dict[str, list[str]] = {}
    seen_ids: set[str] = set()
    total = 0
    for artifact_type, values in raw.items():
        if not isinstance(artifact_type, str) or not isinstance(values, list):
            raise ValueError("science result artifact context is invalid")
        ids = [item for item in values if isinstance(item, str) and item]
        if (
            len(ids) != len(values)
            or len(ids) > 256
            or len(ids) != len(set(ids))
            or any(len(item.encode("utf-8")) > 256 for item in ids)
        ):
            raise ValueError("science result artifact IDs are invalid")
        if seen_ids.intersection(ids):
            raise ValueError("science result artifact ID has multiple context types")
        seen_ids.update(ids)
        total += len(ids)
        if total > 1024:
            raise ValueError("science result has too many artifact IDs")
        context[artifact_type] = ids
    for artifact_type in _CONTEXT_TYPES:
        context.setdefault(artifact_type, [])
    return context


def _rollout_runtime_context_receipt(
    rollout_result: Mapping[str, object],
) -> dict[str, object] | None:
    sessions = rollout_result.get("results")
    if not isinstance(sessions, list) or len(sessions) != 1:
        raise ValueError("science rollout must return exactly one terminal session")
    session = sessions[0]
    metadata = session.get("metadata") if isinstance(session, dict) else None
    evolution = metadata.get("evolution") if isinstance(metadata, dict) else None
    if not isinstance(evolution, dict):
        raise ValueError("science rollout has no evolution runtime metadata")
    context_artifact_ids = evolution.get("context_artifact_ids")
    if (
        not isinstance(context_artifact_ids, list)
        or len(context_artifact_ids) > 256
        or not all(_bounded_identity(artifact_id) for artifact_id in context_artifact_ids)
        or len(context_artifact_ids) != len(set(context_artifact_ids))
    ):
        raise ValueError("science rollout context artifact identity is invalid")
    value = evolution.get("runtime_injection_receipt")
    if not context_artifact_ids and value is None:
        return None
    if evolution.get("context_injected") is not True:
        raise ValueError("science rollout context was not injected exactly")
    receipt = _validate_runtime_context_receipt(value)
    if evolution.get("context_id") != receipt["context_id"]:
        raise ValueError("science rollout receipt context differs from runtime metadata")
    if tuple(item["artifact_id"] for item in receipt["artifacts"]) != tuple(context_artifact_ids):
        raise ValueError("science rollout receipt differs from runtime metadata")
    return receipt


def _runtime_context_receipt(
    result: Mapping[str, object],
) -> dict[str, object] | None:
    value = result.get("runtime_context_receipt")
    if value is None:
        return None
    validated = _validate_runtime_context_receipt(value)
    if validated != value:
        raise ValueError("science result runtime context receipt is not canonical")
    return validated


def _runtime_context_authority(
    result: Mapping[str, object],
) -> dict[str, object] | None:
    value = result.get("_runtime_context_authority")
    if value is None:
        return None
    validated = _validate_runtime_context_receipt(value)
    if validated != value:
        raise ValueError("science result runtime context authority is not canonical")
    return validated


def _runtime_context_receipt_copy(value: Mapping[str, object]) -> dict[str, object]:
    copied = json.loads(_canonical_bytes(value))
    if not isinstance(copied, dict):
        raise ValueError("science runtime context receipt is invalid")
    return cast(dict[str, object], copied)


def _validate_runtime_context_receipt(value: object) -> dict[str, object]:
    if not isinstance(value, dict) or set(value) != {
        "schema_version",
        "context_id",
        "revision_id",
        "instruction_sha256",
        "runtime_tree_sha256",
        "files",
        "artifacts",
    }:
        raise ValueError("science runtime context receipt is invalid")
    files = value.get("files")
    artifacts = value.get("artifacts")
    if (
        value.get("schema_version") != "3"
        or not _bounded_identity(value.get("context_id"))
        or not _bounded_identity(value.get("revision_id"))
        or not _sha256(value.get("instruction_sha256"))
        or not _sha256(value.get("runtime_tree_sha256"))
        or not isinstance(files, list)
        or not 0 < len(files) <= 4096
        or not isinstance(artifacts, list)
        or not 0 < len(artifacts) <= 256
    ):
        raise ValueError("science runtime context receipt is invalid")
    validated_files: list[dict[str, object]] = []
    total_bytes = 0
    for file in files:
        if not isinstance(file, dict) or set(file) != {
            "relative_path",
            "size_bytes",
            "sha256",
        }:
            raise ValueError("science runtime context file receipt is invalid")
        path = file.get("relative_path")
        size = file.get("size_bytes")
        digest = file.get("sha256")
        if (
            not isinstance(path, str)
            or not path
            or path.startswith("/")
            or "\\" in path
            or any(part in {"", ".", ".."} for part in path.split("/"))
            or not isinstance(size, int)
            or isinstance(size, bool)
            or size < 0
            or not _sha256(digest)
        ):
            raise ValueError("science runtime context file receipt is invalid")
        total_bytes += size
        validated_files.append({"relative_path": path, "size_bytes": size, "sha256": digest})
    paths = [cast(str, item["relative_path"]) for item in validated_files]
    files_by_path = {cast(str, item["relative_path"]): item for item in validated_files}
    if (
        total_bytes > 64 * 1024 * 1024
        or len(paths) != len(set(paths))
        or paths != sorted(paths)
        or value["runtime_tree_sha256"]
        != hashlib.sha256(_canonical_bytes({"files": validated_files})).hexdigest()
        or files_by_path.get("evolution/instruction.txt", {}).get("sha256")
        != value["instruction_sha256"]
    ):
        raise ValueError("science runtime context file receipt is not canonical")
    validated_artifacts: list[dict[str, object]] = []
    for artifact in artifacts:
        if not isinstance(artifact, dict) or set(artifact) != {
            "artifact_id",
            "artifact_type",
            "content_sha256",
            "runtime_paths",
            "runtime_tree_sha256",
        }:
            raise ValueError("science runtime context artifact receipt is invalid")
        artifact_id = artifact.get("artifact_id")
        artifact_type = artifact.get("artifact_type")
        runtime_paths = artifact.get("runtime_paths")
        if (
            not _bounded_identity(artifact_id)
            or artifact_type not in _CONTEXT_TYPES
            or artifact_type == "dataset"
            or not _sha256(artifact.get("content_sha256"))
            or not _sha256(artifact.get("runtime_tree_sha256"))
            or not isinstance(runtime_paths, list)
            or not runtime_paths
            or len(runtime_paths) > 4096
            or not all(isinstance(path, str) and path in files_by_path for path in runtime_paths)
            or len(runtime_paths) != len(set(runtime_paths))
        ):
            raise ValueError("science runtime context artifact receipt is invalid")
        runtime_entries = [files_by_path[cast(str, path)] for path in runtime_paths]
        if (
            artifact["runtime_tree_sha256"]
            != hashlib.sha256(_canonical_bytes({"files": runtime_entries})).hexdigest()
        ):
            raise ValueError("science runtime context artifact receipt digest is invalid")
        validated_artifacts.append(dict(artifact))
    if len({item["artifact_id"] for item in validated_artifacts}) != len(validated_artifacts):
        raise ValueError("science runtime context artifact receipt is duplicated")
    return {
        "schema_version": "3",
        "context_id": value["context_id"],
        "revision_id": value["revision_id"],
        "instruction_sha256": value["instruction_sha256"],
        "runtime_tree_sha256": value["runtime_tree_sha256"],
        "files": validated_files,
        "artifacts": validated_artifacts,
    }


def _runtime_context_artifact_authority(
    ledger: ScienceRunStore,
    run_id: str,
) -> dict[str, dict[str, str]]:
    context = ledger.input_context(run_id)
    expected_types: dict[str, str] = {}
    for artifact_type in _CONTEXT_TYPES:
        artifact_ids = context.get(artifact_type, [])
        if artifact_type == "dataset":
            continue
        for artifact_id in artifact_ids:
            if artifact_id in expected_types:
                raise ValueError("science revision context artifact identity is duplicated")
            expected_types[artifact_id] = artifact_type
    artifacts = ledger.artifacts_by_ids(list(expected_types))
    authority: dict[str, dict[str, str]] = {}
    for artifact in artifacts:
        artifact_type = artifact.artifact_type.value
        if expected_types.get(artifact.id) != artifact_type:
            raise ValueError("science revision context artifact type is invalid")
        authority[artifact.id] = {
            "artifact_type": artifact_type,
            "content_sha256": artifact.content_sha256,
        }
    return authority


def _verify_runtime_context_receipt(
    receipt: dict[str, object] | None,
    *,
    revision_id: str | None,
    artifacts: Mapping[str, Mapping[str, str]],
    authority: dict[str, object] | None,
) -> None:
    if not artifacts:
        if receipt is not None or authority is not None:
            raise ValueError("science run produced an unexpected runtime context receipt")
        return
    if (
        receipt is None
        or authority is None
        or receipt.get("revision_id") != revision_id
        or authority.get("revision_id") != revision_id
    ):
        raise ValueError("science runtime context receipt revision is invalid")
    if receipt != authority:
        raise ValueError("science runtime context receipt differs from expected rendering")
    received = cast(list[dict[str, object]], receipt["artifacts"])
    if tuple(item["artifact_id"] for item in received) != tuple(artifacts):
        raise ValueError("science runtime context receipt membership is invalid")
    for item in received:
        artifact_id = cast(str, item["artifact_id"])
        expected = artifacts[artifact_id]
        if (
            item["artifact_type"] != expected["artifact_type"]
            or item["content_sha256"] != expected["content_sha256"]
        ):
            raise ValueError("science runtime context receipt content authority is invalid")


def _project_artifacts(
    result: Mapping[str, object],
    *,
    project: m.ProjectV1,
    run_id: str,
    revision: m.RevisionRefV1,
) -> list[m.ArtifactSummaryV1]:
    outputs = _worker_output_records(result)
    terminal_context = _result_context(result)
    selected_artifact_ids = {
        artifact_id for artifact_ids in terminal_context.values() for artifact_id in artifact_ids
    }
    artifact_types_by_id = {
        artifact_id: artifact_type
        for artifact_type, artifact_ids in terminal_context.items()
        for artifact_id in artifact_ids
    }
    for artifact_id, output in outputs.items():
        output_type = output.get("type")
        if not isinstance(output_type, str) or not output_type:
            raise ValueError("science result artifact type is invalid")
        existing_type = artifact_types_by_id.get(artifact_id)
        if existing_type is not None and existing_type != output_type:
            raise ValueError("science result artifact context type is invalid")
        artifact_types_by_id[artifact_id] = output_type
    if len(outputs) > 1024:
        raise ValueError("science result has too many artifact outputs")
    projected: list[m.ArtifactSummaryV1] = []
    for artifact_id, output in outputs.items():
        try:
            artifact_type = m.ArtifactType(output["type"])
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError("science result artifact type is invalid") from exc
        name = output.get("name")
        manifest = output.get("manifest")
        lineage = output.get("lineage")
        scores = output.get("scores")
        created_at = output.get("created_at")
        digest = output.get("payload_manifest_digest")
        byte_size = output.get("payload_byte_size")
        file_count = output.get("payload_file_count")
        if (
            not isinstance(name, str)
            or not name
            or not isinstance(manifest, dict)
            or not isinstance(lineage, dict)
            or not isinstance(scores, dict)
            or not isinstance(created_at, str)
            or not isinstance(digest, str)
            or len(digest) != 64
            or any(character not in "0123456789abcdef" for character in digest)
            or not isinstance(byte_size, int)
            or isinstance(byte_size, bool)
            or byte_size < 0
            or not isinstance(file_count, int)
            or isinstance(file_count, bool)
            or file_count < 1
        ):
            raise ValueError("science result artifact metadata is incomplete")
        execution = lineage.get("openevo_execution")
        if not isinstance(execution, dict):
            raise ValueError("science result artifact lineage is incomplete")
        method_id = execution.get("method_id")
        job_id = execution.get("job_id")
        target_id = execution.get("target_id")
        if not all(isinstance(value, str) and value for value in (method_id, job_id, target_id)):
            raise ValueError("science result artifact execution identity is incomplete")
        source_dataset_ids, source_artifact_ids = _lineage_inputs(
            execution,
            artifact_types_by_id=artifact_types_by_id,
        )
        score_models = [
            m.ArtifactScoreV1(name=str(score_name), value=float(score_value))
            for score_name, score_value in scores.items()
            if isinstance(score_value, int | float)
            and not isinstance(score_value, bool)
            and math.isfinite(float(score_value))
        ]
        if len(score_models) != len(scores):
            raise ValueError("science result artifact scores are invalid")
        common: dict[str, Any] = {
            "id": artifact_id,
            "project_id": project.id,
            "run_id": run_id,
            "target_id": target_id,
            "display_name": name,
            "summary": f"Generated by {method_id}.",
            "byte_size": byte_size,
            "produced_revision": revision,
            "membership_revisions": [revision],
            "content_sha256": digest,
            "selected": artifact_id in selected_artifact_ids,
            "promoted": bool(output.get("promoted")),
            "release_enabled": artifact_type is not m.ArtifactType.PARAMETRIC_MEMORY,
            "compatibility": m.ArtifactCompatibilityV1(
                execution_modes=[project.spec.execution_mode],
                harness_ids=[project.spec.harness_id],
                base_model_refs=[project.spec.agent_model_ref],
            ),
            "lineage": m.ArtifactLineageV1(
                method_id=method_id,
                job_id=job_id,
                source_dataset_ids=source_dataset_ids,
                source_artifact_ids=source_artifact_ids,
            ),
            "scores": score_models,
            "created_at": created_at,
            "artifact_type": artifact_type,
        }
        if artifact_type is m.ArtifactType.TEXT_MEMORY:
            record_count = manifest.get("record_count", 0)
            if not isinstance(record_count, int) or isinstance(record_count, bool):
                raise ValueError("text memory record count is invalid")
            common["metadata"] = m.TextMemoryArtifactMetadataV1(
                record_count=record_count,
                source_dataset_ids=source_dataset_ids,
            )
            projected.append(m.TextMemoryArtifactSummaryV1.model_validate(common))
        elif artifact_type is m.ArtifactType.SKILL_BUNDLE:
            common["metadata"] = m.SkillBundleArtifactMetadataV1(document_count=file_count)
            projected.append(m.SkillBundleArtifactSummaryV1.model_validate(common))
        elif artifact_type is m.ArtifactType.AGENT_SYSTEM:
            common["metadata"] = m.AgentSystemArtifactMetadataV1(
                target_path=manifest.get("target_path", "AGENTS.md")
            )
            projected.append(m.AgentSystemArtifactSummaryV1.model_validate(common))
        else:
            common["release_enabled"] = False
            common["metadata"] = m.ParametricMemoryArtifactMetadataV1(
                adapter_id=manifest.get("adapter_id", artifact_id),
                base_model_ref=manifest.get("base_model", project.spec.agent_model_ref),
                adapter_format=manifest.get("adapter_format", "lora"),
            )
            projected.append(m.ParametricMemoryArtifactSummaryV1.model_validate(common))
    return projected


def _worker_output_records(result: Mapping[str, object]) -> dict[str, dict[str, Any]]:
    outputs: dict[str, dict[str, Any]] = {}
    artifact_ids: set[str] = set()
    jobs = _single_science_round(result).get("jobs")
    if not isinstance(jobs, list):
        raise ValueError("science result jobs are invalid")
    for job in jobs:
        worker_results = job.get("worker_results") if isinstance(job, dict) else None
        if not isinstance(worker_results, list):
            raise ValueError("science result worker outputs are invalid")
        for worker_result in worker_results:
            if not isinstance(worker_result, dict):
                raise ValueError("science result worker output is invalid")
            raw_ids = worker_result.get("artifact_ids")
            raw_outputs = worker_result.get("outputs")
            if not isinstance(raw_ids, list) or not isinstance(raw_outputs, list):
                raise ValueError("science result worker output inventory is incomplete")
            for artifact_id in raw_ids:
                if not isinstance(artifact_id, str) or not artifact_id:
                    raise ValueError("science result artifact ID is invalid")
                artifact_ids.add(artifact_id)
            for output in raw_outputs:
                artifact_id = output.get("artifact_id") if isinstance(output, dict) else None
                if not isinstance(artifact_id, str) or not artifact_id:
                    raise ValueError("science result artifact output is invalid")
                existing = outputs.get(artifact_id)
                if existing is not None and _canonical_bytes(existing) != _canonical_bytes(output):
                    raise ValueError("science result artifact output changed within the run")
                outputs[artifact_id] = output
    if set(outputs) != artifact_ids:
        raise ValueError("science result artifact output inventory is incomplete")
    return outputs


def _lineage_inputs(
    execution: Mapping[str, object],
    *,
    artifact_types_by_id: Mapping[str, str],
) -> tuple[list[str], list[str]]:
    datasets: list[str] = []
    artifacts: list[str] = []
    bindings = execution.get("input_bindings")
    if not isinstance(bindings, list) or len(bindings) > 128:
        raise ValueError("science result artifact input bindings are invalid")
    binding_ids: set[str] = set()
    artifact_digests: dict[str, str] = {}
    for raw_binding in bindings:
        try:
            binding = ResolvedMethodInputBinding.model_validate(raw_binding)
        except ValueError as exc:
            raise ValueError("science result artifact input binding is invalid") from exc
        if binding.binding_id in binding_ids:
            raise ValueError("science result artifact input binding is duplicated")
        binding_ids.add(binding.binding_id)
        for artifact_id, digest in zip(
            binding.artifact_ids,
            binding.artifact_digests,
            strict=True,
        ):
            artifact_type = artifact_types_by_id.get(artifact_id)
            if artifact_type is None:
                raise ValueError("science result artifact input is not in the result inventory")
            existing_digest = artifact_digests.get(artifact_id)
            if existing_digest is not None:
                if existing_digest != digest:
                    raise ValueError("science result artifact input digest is inconsistent")
                continue
            artifact_digests[artifact_id] = digest
            target = datasets if artifact_type == "dataset" else artifacts
            target.append(artifact_id)
            if len(target) > 128:
                raise ValueError("science result artifact lineage is too large")
    return datasets, artifacts


def _request_digest(request: m.ContractModel | None, etag: str) -> str:
    return hashlib.sha256(
        _canonical_bytes(
            {
                "etag": etag,
                "request": None if request is None else request.model_dump(mode="json"),
            }
        )
    ).hexdigest()


def _response(
    model: m.ContractModel,
    *,
    status_code: int = 200,
    etag: str | None = None,
) -> JSONResponse:
    resolved_etag = etag or getattr(model, "etag", None)
    return JSONResponse(
        status_code=status_code,
        content=model.model_dump(mode="json"),
        headers=None if resolved_etag is None else {"ETag": resolved_etag},
    )


def _api_error(code: str, message: str, *, retryable: bool) -> m.ApiErrorV1:
    return m.ApiErrorV1(
        request_id=f"run-error-{secrets.token_hex(8)}",
        code=code,
        http_status=503,
        message=message,
        severity=m.ErrorSeverity.BLOCKING,
        category=m.ErrorCategory.RUN,
        retryable=retryable,
        repair_action=(
            m.RepairAction.OPENEVO_CAN_RETRY if retryable else m.RepairAction.USER_ACTION_REQUIRED
        ),
        next_action="Retry the preserved run after checking remote service readiness.",
    )


def _owner_error(code: str, message: str, status: int, retryable: bool) -> CoreRunControlError:
    return CoreRunControlError(code, message, http_status=status, retryable=retryable)


def _raise_v2_owner_error(exc: Exception, *, operation_id: str) -> None:
    if isinstance(exc, ScienceEventCursorExpiredV2):
        raise CoreTaskControlError(
            "event_cursor_expired",
            "The requested v2 event replay cursor is no longer retained.",
            http_status=410,
            retryable=False,
        ) from exc
    if isinstance(exc, ScienceTaskNotReadyV2):
        raise CoreTaskControlError(
            "project_not_ready",
            "The project has unresolved state and cannot admit a Task.",
            http_status=409,
            retryable=True,
        ) from exc
    if isinstance(exc, ScienceTaskStaleSubmissionV2):
        raise CoreTaskControlError(
            "task_submission_stale",
            "The Task submission does not match the current project authority.",
            http_status=409,
            retryable=True,
        ) from exc
    if isinstance(exc, ScienceTaskIdempotencyConflictV2):
        raise CoreTaskControlError(
            "task_idempotency_key_reused",
            "The idempotency key was already used for a different v2 request.",
            http_status=409,
            retryable=False,
        ) from exc
    if isinstance(exc, ScienceTaskProjectInFlightV2):
        raise CoreTaskControlError(
            "task_project_in_flight",
            "The project already has an immutable Task in flight.",
            http_status=409,
            retryable=True,
        ) from exc
    if isinstance(exc, ScienceTaskETagChangedV2):
        raise CoreTaskControlError(
            "task_etag_changed",
            "The Task ETag changed before the requested mutation.",
            http_status=412,
            retryable=True,
        ) from exc
    if isinstance(exc, ScienceTaskPreconditionFailedV2):
        code = (
            "attempt_precondition_failed"
            if operation_id == "appendCoreTaskAttemptV2"
            else "task_precondition_failed"
        )
        raise CoreTaskControlError(
            code,
            "The immutable Task ownership precondition changed.",
            http_status=412,
            retryable=True,
        ) from exc
    if isinstance(exc, ScienceTaskTerminalV2):
        raise CoreTaskControlError(
            "task_terminal",
            "The immutable Task can no longer be mutated.",
            http_status=409,
            retryable=False,
        ) from exc
    if isinstance(exc, ScienceAttemptNotFoundV2):
        raise CoreTaskControlError(
            "attempt_not_found",
            "The requested v2 Attempt was not found.",
            http_status=404,
            retryable=False,
        ) from exc
    if isinstance(exc, ScienceTaskNotFoundV2):
        raise CoreTaskControlError(
            "task_not_found",
            "The requested v2 Task was not found.",
            http_status=404,
            retryable=False,
        ) from exc
    if isinstance(exc, ScienceTaskConflictV2):
        raise CoreTaskControlError(
            "task_conflict",
            "The v2 Task ownership mutation conflicts with durable state.",
            http_status=409,
            retryable=False,
        ) from exc
    if isinstance(exc, ScienceTaskStoreV2Error):
        raise CoreTaskControlError(
            "task_owner_unavailable",
            "Core could not read or update durable v2 Task ownership.",
            http_status=503,
            retryable=True,
        ) from exc
    if isinstance(exc, (TypeError, ValueError, KeyError)):
        raise CoreTaskControlError(
            "task_request_invalid",
            "The v2 Task ownership request is invalid.",
            http_status=422,
            retryable=False,
        ) from exc
    raise exc


def _require_v2_argument_keys(
    arguments: Mapping[str, object],
    required: set[str],
    *,
    optional: set[str] | None = None,
) -> None:
    if not isinstance(arguments, Mapping):
        raise TypeError("v2 Task arguments must be a mapping")
    keys = set(arguments)
    allowed = required | (optional or set())
    if (
        keys - allowed
        or not required.issubset(keys)
        or any(not isinstance(key, str) for key in keys)
    ):
        raise ValueError("v2 Task arguments do not match the closed operation")


def _v2_request_model(model_type: type[m2.ContractModel], value: object) -> Any:
    if type(value) is model_type:
        return model_type.model_validate(value.model_dump(mode="python"))
    return model_type.model_validate(value)


def _v2_string_argument(value: object, *, label: str) -> str:
    if not isinstance(value, str):
        raise TypeError(f"v2 {label} must be a string")
    return value


def _readiness_error(code: ServiceRunReadinessCode) -> CoreRunControlError:
    messages = {
        ServiceRunReadinessCode.CODEX_CLI_UNAVAILABLE: (
            "Codex CLI is unavailable on the remote Core host."
        ),
        ServiceRunReadinessCode.CODEX_SUBSCRIPTION_AUTH_UNAVAILABLE: (
            "Codex subscription login is unavailable on the remote Core host."
        ),
        ServiceRunReadinessCode.RUNTIME_EXECUTABLE_UNAVAILABLE: (
            "The managed Science runtime executable is unavailable."
        ),
        ServiceRunReadinessCode.RUNTIME_IMAGE_UNAVAILABLE: (
            "The managed Science runtime image is unavailable."
        ),
        ServiceRunReadinessCode.RUNTIME_EVIDENCE_INVALID: (
            "Managed Science runtime evidence is invalid."
        ),
        ServiceRunReadinessCode.SERVICE_GROUP_UNAVAILABLE: (
            "Required Core services are unavailable."
        ),
        ServiceRunReadinessCode.RUN_ADMISSION_UNAVAILABLE: (
            "The generation-bound science run admission owner is unavailable."
        ),
        ServiceRunReadinessCode.SELF_DEPLOYED_UNAVAILABLE: (
            "Self-deployed Science execution is unavailable."
        ),
    }
    message = messages.get(code, "Managed Science run readiness is unavailable.")
    return _owner_error(f"run_{code.value}", message, 503, True)


def _service_generation_changed() -> CoreRunControlError:
    return _owner_error(
        "run_service_generation_changed",
        "Managed services changed before the science run could start.",
        503,
        True,
    )


def _admission_denied() -> RunAdmissionError:
    return RunAdmissionError(
        "run_admission_denied",
        "The service request is not bound to an active Core science run.",
        status_code=403,
        retryable=False,
    )


def _canonical_bytes(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=True,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _sha256(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )


def _bounded_identity(value: object) -> bool:
    return isinstance(value, str) and 0 < len(value.encode("utf-8")) <= 256


__all__ = ["CoreScienceRunOwner", "CoreScienceTaskOwnerV2"]
