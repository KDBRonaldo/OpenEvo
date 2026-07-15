"""Durable Core owner for ordinary-user science runs."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path
import secrets
import threading
from typing import Any, Iterator, Protocol, cast

from fastapi.responses import JSONResponse, Response

from openevo.backend.contracts.v1 import models as m
from openevo.backend.contracts.v1.store import (
    CoreControlStoreV1,
    ResourceNotFoundError,
)
from openevo.backend.run_control import CoreRunControlError
from openevo.backend.service_control import CoreServiceControlError
from openevo.backend.science_execution import compile_science_execution
from openevo.backend.science_run_store import (
    ScienceRunConflict,
    ScienceRunIdempotencyConflict,
    ScienceRunNotFound,
    ScienceRunPreconditionFailed,
    ScienceRunStore,
    ScienceRunStoreError,
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
from openevo.experiments import EvolutionHttpClient, RolloutHttpClient, run_experiment
from openevo.experiments.clients import EvolutionClientProtocol, RolloutClientProtocol
from openevo.internal_auth import (
    GenerationBoundRunAdmissionCheck,
    RunAdmissionError,
    RunAdmissionOperation,
)
from openevo.projects.science.compiler import MANAGED_RUNTIME_IMAGES
from openevo.rollout.models import canonicalize_task_request


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


class _RunCancelled(RuntimeError):
    pass


class CoreScienceRunOwner:
    """Own the frozen Core run routes and execute one science run at a time."""

    def __init__(
        self,
        *,
        state_root: str | Path,
        project_store: CoreControlStoreV1,
        service_supervisor: CoreServiceSupervisor | _ServiceOwner,
        executable_registry: VerifiedExecutableRegistry,
        experiment_runner: ExperimentRunner = run_experiment,
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
        self._output_root = Path(state_root) / "science-run-output"
        self._output_root.mkdir(mode=0o700, parents=True, exist_ok=True)
        self._condition = threading.Condition()
        self._lifecycle_lock = threading.RLock()
        self._cancel_events: dict[str, threading.Event] = {}
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
        self._ledger.close()

    def request_stop(self) -> None:
        with self._condition:
            if self._closed:
                return
            self._closed = True
            for event in self._cancel_events.values():
                event.set()
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
            with self._lifecycle_lock, self._pin_create_authority(request) as authority:
                project = self._validate_create_authority(request, *authority)
                input_context = self._ledger.revision_context(
                    request.project_id,
                    request.required_revision.revision.id,
                )
                now = self._timestamp()
                run_id = f"run-{secrets.token_hex(16)}"
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
                        "queued_reason": m.QueuedReasonV1(
                            code=m.QueuedReasonCode.ADMISSION_PENDING,
                            summary="Core is admitting the saved project revision.",
                            retry_after_seconds=1,
                        ),
                        "attempt_count": 0,
                        "required_revision": request.required_revision,
                        "revision_transition": authority[2].transition,
                        "created_at": now,
                        "updated_at": now,
                        "attempts": [],
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
            return _retry_model(current, version=version, now=now)

        updated, replayed = self._ledger.apply_mutation(
            "retryCoreRunV1",
            run_id,
            key,
            digest,
            expected_etag=cast(str, arguments["if_match"]),
            status_code=202,
            transform=retry,
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
                    pending = sorted(self._pending_finalizations)
                    queued = self._ledger.queued_run_ids()
                    while not self._closed and not pending and not queued:
                        self._condition.wait(timeout=1.0)
                        pending = sorted(self._pending_finalizations)
                        queued = self._ledger.queued_run_ids()
                    if self._closed:
                        return
                    if pending:
                        run_id = pending[0]
                        self._pending_finalizations.discard(run_id)
                        finalize = True
                    else:
                        run_id = queued[0]
                        finalize = False
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
            rollout_base = self._rollout_factory(binding)
            evolution = self._evolution_factory(binding)
            rollout = _AdmittingRolloutClient(
                rollout_base,
                owner=self,
                run_id=run_id,
                binding=binding,
                cancellation=cancellation,
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
            finally:
                for client in (rollout_base, evolution):
                    close = getattr(client, "close", None)
                    if callable(close):
                        close()
            if cancellation.is_set():
                raise _RunCancelled()
            if result.get("status") != "completed":
                raise RuntimeError("science experiment did not complete")
            self._ledger.store_result(run_id, result)
            self._finalize_completed_result(run_id, result, cancellation=cancellation)
        except _RunCancelled:
            self._finish_cancelled(run_id)
        except BaseException as exc:
            if cancellation.is_set():
                self._finish_cancelled(run_id)
            elif self._ledger.result_for_run(run_id) is not None:
                with self._condition:
                    self._pending_finalizations.add(run_id)
            else:
                self._finish_failed(run_id, exc)
        finally:
            if service_lease is not None:
                service_lease.close()
            with self._condition:
                self._condition.notify_all()

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
        except BaseException:
            return False

    def _finalize_completed_result(
        self,
        run_id: str,
        result: Mapping[str, object],
        *,
        cancellation: threading.Event,
    ) -> None:
        with self._lifecycle_lock:
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
            project = self._project_store.get_project(run.project_id)
            predecessor = request.required_revision.revision
            active_revision = project.active_revision
            if active_revision is None or (
                active_revision != predecessor
                and active_revision.generation != predecessor.generation + 1
            ):
                raise ScienceRunConflict(
                    "project revision advanced before run finalization completed"
                )
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
            successor = self._project_store.activate_evolution_revision(
                run.project_id,
                predecessor=predecessor,
                run_id=run_id,
                context_artifact_ids=context,
            )
            project = self._project_store.get_project(run.project_id)
            if project.active_revision != successor.revision:
                raise ScienceRunConflict(
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
                log_builders=(
                    lambda terminal, sequence: self._log_entry(
                        terminal,
                        sequence,
                        m.LogLevel.INFO,
                        "Science run completed successfully.",
                    ),
                ),
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
        return project

    def _recover_interrupted_runs(self) -> None:
        for run_id in self._ledger.active_run_ids():
            run = self._ledger.get_run(run_id)
            if run.status is m.RunStatus.CANCELLING:
                self._cancel_events.setdefault(run_id, threading.Event()).set()
                self._finish_cancelled(run_id)
            elif self._ledger.result_for_run(run_id) is not None:
                self._pending_finalizations.add(run_id)
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
        owner: CoreScienceRunOwner,
        run_id: str,
        binding: ServiceRunBinding,
        cancellation: threading.Event,
    ) -> None:
        self._client = client
        self._owner = owner
        self._run_id = run_id
        self._binding = binding
        self._cancellation = cancellation

    def submit_task(self, payload: dict[str, Any]) -> str:
        if self._cancellation.is_set():
            raise _RunCancelled()
        canonical = canonicalize_task_request(payload)
        task_id = canonical.request.task_id
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
        submitted = self._client.submit_task(canonical.payload)
        if submitted != task_id:
            raise RuntimeError("rollout service changed the admitted task identity")
        return submitted

    def get_task(self, task_id: str) -> dict[str, Any]:
        if self._cancellation.is_set():
            raise _RunCancelled()
        result = self._client.get_task(task_id)
        if self._cancellation.is_set():
            raise _RunCancelled()
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
    queued_reason = m.QueuedReasonV1(
        code=m.QueuedReasonCode.ADMISSION_PENDING,
        summary="Core is admitting the retry attempt.",
        retry_after_seconds=1,
    )
    attempts = [
        *run.attempts,
        m.AttemptV1(
            id=f"attempt-{secrets.token_hex(16)}",
            run_id=run.id,
            number=len(run.attempts) + 1,
            status=m.RunStatus.QUEUED,
            queued_reason=queued_reason,
            created_at=now,
            updated_at=now,
        ),
    ]
    data = run.model_dump(mode="python", exclude={"etag", "attempts"})
    data.update(
        status=m.RunStatus.QUEUED,
        queued_reason=queued_reason,
        current_attempt_id=attempts[-1].id,
        current_attempt=attempts[-1],
        attempt_count=len(attempts),
        current_error=None,
        pinned_revision=None,
        admitted_at=None,
        started_at=None,
        finished_at=None,
        updated_at=now,
        attempts=attempts,
    )
    return _run_model(data, version=version)


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


def _result_context(result: Mapping[str, object]) -> dict[str, list[str]]:
    try:
        tasks = result["tasks"]
        task = cast(list[dict[str, Any]], tasks)[0]
        round_result = cast(list[dict[str, Any]], task["rounds"])[-1]
        raw = cast(dict[str, object], round_result["artifact_ids"])
    except (KeyError, IndexError, TypeError) as exc:
        raise ValueError("science result has no terminal artifact context") from exc
    if len(raw) > 128:
        raise ValueError("science result has too many artifact context types")
    context: dict[str, list[str]] = {}
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
        total += len(ids)
        if total > 1024:
            raise ValueError("science result has too many artifact IDs")
        context[artifact_type] = ids
    for artifact_type in _CONTEXT_TYPES:
        context.setdefault(artifact_type, [])
    return context


def _project_artifacts(
    result: Mapping[str, object],
    *,
    project: m.ProjectV1,
    run_id: str,
    revision: m.RevisionRefV1,
) -> list[m.ArtifactSummaryV1]:
    outputs = _worker_output_records(result)
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
        source_dataset_ids, source_artifact_ids = _lineage_inputs(execution)
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
            "selected": bool(output.get("promoted")),
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
    tasks = result.get("tasks")
    if not isinstance(tasks, list) or not tasks:
        raise ValueError("science result has no task outputs")
    for task in tasks:
        rounds = task.get("rounds") if isinstance(task, dict) else None
        if not isinstance(rounds, list) or not rounds:
            raise ValueError("science result task rounds are invalid")
        for round_result in rounds:
            jobs = round_result.get("jobs") if isinstance(round_result, dict) else None
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
                        artifact_id = (
                            output.get("artifact_id") if isinstance(output, dict) else None
                        )
                        if not isinstance(artifact_id, str) or not artifact_id:
                            raise ValueError("science result artifact output is invalid")
                        existing = outputs.get(artifact_id)
                        if existing is not None and _canonical_bytes(existing) != _canonical_bytes(
                            output
                        ):
                            raise ValueError(
                                "science result artifact output changed within the run"
                            )
                        outputs[artifact_id] = output
    if set(outputs) != artifact_ids:
        raise ValueError("science result artifact output inventory is incomplete")
    return outputs


def _lineage_inputs(execution: Mapping[str, object]) -> tuple[list[str], list[str]]:
    datasets: list[str] = []
    artifacts: list[str] = []
    bindings = execution.get("input_bindings")
    if not isinstance(bindings, list):
        return datasets, artifacts
    for binding in bindings:
        if not isinstance(binding, dict):
            continue
        artifact_id = binding.get("artifact_id")
        artifact_type = binding.get("artifact_type")
        if not isinstance(artifact_id, str):
            continue
        (datasets if artifact_type == "dataset" else artifacts).append(artifact_id)
    return datasets[:128], artifacts[:128]


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


__all__ = ["CoreScienceRunOwner"]
