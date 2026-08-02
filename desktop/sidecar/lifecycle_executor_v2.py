"""Durable single-worker execution for Desktop lifecycle operations."""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
import threading
from typing import TypeAlias

from pydantic import TypeAdapter, ValidationError

from desktop.sidecar.contracts.v2 import models as m
from desktop.sidecar.lifecycle_logs_v2 import (
    LifecycleLogSourceV2,
    LifecycleOutputSanitizerV2,
    LifecycleRawOutputObserverV2,
)
from desktop.sidecar.provider_store_v2 import (
    DesktopProviderStoreV2,
    LifecycleLogAppendV2,
    LifecycleOperationAdvanceV2,
    LifecycleOperationCompletionV2,
    LifecycleOperationReservationV2,
    LifecycleOperationWorkV2,
    ProviderPreconditionFailedV2,
)


LifecycleRunnerV2: TypeAlias = Callable[
    ["LifecycleExecutionContextV2"],
    m.LifecycleResultV2,
]
LifecycleOperationObserverV2: TypeAlias = Callable[[m.LifecycleOperationV2], None]
LifecycleErrorMapperV2: TypeAlias = Callable[
    [BaseException, LifecycleOperationWorkV2],
    m.DesktopErrorV2,
]

_LIFECYCLE_KINDS = frozenset(
    {
        "profile_connect",
        "profile_disconnect",
        "host_key_review",
        "native_workspace_prepare",
        "project_create",
        "project_activate",
    }
)
_RESULT_ADAPTER = TypeAdapter(m.LifecycleResultV2)
_PROCESS_SHUTDOWN_FAILURE_FENCE_SECONDS = 0.5


class _LifecycleCancelled(Exception):
    """The user durably requested cancellation."""


class _LifecycleExecutorStopping(Exception):
    """The process is stopping and must leave durable work recoverable."""


class LifecycleOperationDeferredV2(Exception):
    """A durable operation is waiting for another lifecycle prerequisite."""


class LifecycleExecutionContextV2:
    """A runner's narrow interface to one persisted lifecycle operation."""

    def __init__(
        self,
        *,
        store: DesktopProviderStoreV2,
        work: LifecycleOperationWorkV2,
        publish: LifecycleOperationObserverV2,
        secret_canaries: Iterable[str],
        forbidden_endpoints: Iterable[str],
        forbidden_paths: Iterable[str],
    ) -> None:
        self._store = store
        self._work = work
        self._publish = publish
        self._mutation_lock = threading.RLock()
        self._cancellation_event = threading.Event()
        self._shutdown_requested = False
        self._output_sanitizer = LifecycleOutputSanitizerV2(
            self._append_sanitized_log,
            secret_canaries=secret_canaries,
            forbidden_endpoints=forbidden_endpoints,
            forbidden_paths=forbidden_paths,
        )

    @property
    def operation(self) -> m.LifecycleOperationV2:
        with self._mutation_lock:
            return self._work.operation

    @property
    def request(self) -> object:
        """Return the exact closed request persisted before execution."""

        return self._work.request

    @property
    def idempotency_key(self) -> str:
        return self._work.idempotency_key

    @property
    def cancellation_event(self) -> threading.Event:
        return self._cancellation_event

    @property
    def output_observer(self) -> LifecycleRawOutputObserverV2:
        return self._output_sanitizer

    def checkpoint(
        self,
        phase: m.LifecyclePhaseV2,
        progress: m.LifecycleProgressV2 | None,
        *,
        cancellable: bool,
    ) -> m.LifecycleOperationV2:
        """Durably advance progress, checking cancellation first."""

        with self._mutation_lock:
            self._raise_if_interrupted_locked()
            current = self._work.operation
            cancellable = current.cancellable and cancellable
            target_phase_index = m.LIFECYCLE_PHASES.index(phase)
            if target_phase_index <= current.phase_index:
                retained_progress = self._retained_replay_progress(
                    current.progress,
                    progress,
                    same_phase=target_phase_index == current.phase_index,
                )
                if retained_progress == current.progress and cancellable == current.cancellable:
                    return current
                phase = current.phase
                progress = retained_progress
            updated = self._store.advance_lifecycle_operation(
                LifecycleOperationAdvanceV2(
                    operation_id=self._work.operation.operation_id,
                    expected_etag=self._work.operation.etag,
                    phase=phase,
                    progress=progress,
                    cancellable=cancellable,
                )
            )
            self._work = LifecycleOperationWorkV2(
                operation=updated,
                request=self._work.request,
                idempotency_key=self._work.idempotency_key,
                cancellation_requested=False,
            )
        self._publish(updated)
        return updated

    @staticmethod
    def _retained_replay_progress(
        current: m.LifecycleProgressV2 | None,
        updated: m.LifecycleProgressV2 | None,
        *,
        same_phase: bool,
    ) -> m.LifecycleProgressV2 | None:
        if not same_phase or current is None:
            return current
        if isinstance(current, m.LifecycleProgressIndeterminateV2):
            return current if updated is None else updated
        if (
            updated is None
            or isinstance(updated, m.LifecycleProgressIndeterminateV2)
            or type(updated) is not type(current)
            or updated.total != current.total
            or updated.completed < current.completed
        ):
            return current
        return updated

    def check_cancelled(self) -> None:
        """Raise at an explicit safe interruption checkpoint."""

        with self._mutation_lock:
            self._raise_if_interrupted_locked()

    def flush_output(self) -> None:
        self._output_sanitizer.flush()

    def close_output(self) -> None:
        self._output_sanitizer.close()

    def current_work(self) -> LifecycleOperationWorkV2:
        with self._mutation_lock:
            self._refresh_locked()
            return self._work

    def request_user_cancellation(self) -> None:
        self._cancellation_event.set()

    def request_shutdown(self) -> None:
        with self._mutation_lock:
            self._shutdown_requested = True
            self._cancellation_event.set()

    def is_shutdown_requested(self) -> bool:
        with self._mutation_lock:
            return self._shutdown_requested

    def _append_sanitized_log(
        self,
        source: LifecycleLogSourceV2,
        text: str,
        truncated: bool,
    ) -> None:
        with self._mutation_lock:
            if self._shutdown_requested:
                return
            latest = self._store.get_lifecycle_operation_work(self._work.operation.operation_id)
            self._work = latest
            if latest.operation.status != "running":
                return
            updated = self._store.append_lifecycle_log(
                LifecycleLogAppendV2(
                    operation_id=latest.operation.operation_id,
                    source=source,
                    text=text,
                    truncated=truncated,
                )
            )
            self._work = LifecycleOperationWorkV2(
                operation=updated,
                request=latest.request,
                idempotency_key=latest.idempotency_key,
                cancellation_requested=latest.cancellation_requested,
            )
        self._publish(updated)

    def _refresh_locked(self) -> None:
        self._work = self._store.get_lifecycle_operation_work(self._work.operation.operation_id)

    def _raise_if_interrupted_locked(self) -> None:
        if self._shutdown_requested:
            raise _LifecycleExecutorStopping
        self._refresh_locked()
        if self._work.cancellation_requested:
            self._cancellation_event.set()
            raise _LifecycleCancelled


class DesktopLifecycleExecutorV2:
    """Execute only persisted lifecycle work, FIFO, on one daemon worker."""

    def __init__(
        self,
        store: DesktopProviderStoreV2,
        *,
        runners: Mapping[str, LifecycleRunnerV2],
        operation_observer: LifecycleOperationObserverV2 | None = None,
        error_mapper: LifecycleErrorMapperV2 | None = None,
        secret_canaries: Iterable[str] = (),
        forbidden_endpoints: Iterable[str] = (),
        forbidden_paths: Iterable[str] = (),
        close_timeout_seconds: float = 5.0,
    ) -> None:
        if set(runners) != _LIFECYCLE_KINDS or any(
            not callable(runner) for runner in runners.values()
        ):
            raise ValueError("lifecycle runners must cover the exact closed v2 kind set")
        if operation_observer is not None and not callable(operation_observer):
            raise TypeError("lifecycle operation observer must be callable")
        if error_mapper is not None and not callable(error_mapper):
            raise TypeError("lifecycle error mapper must be callable")
        if close_timeout_seconds <= 0:
            raise ValueError("lifecycle close timeout must be positive")
        self._store = store
        self._runners = dict(runners)
        self._observer = operation_observer
        self._error_mapper = error_mapper or self._default_error_mapper
        self._secret_canaries = tuple(secret_canaries)
        self._forbidden_endpoints = tuple(forbidden_endpoints)
        self._forbidden_paths = tuple(forbidden_paths)
        self._close_timeout_seconds = close_timeout_seconds
        self._condition = threading.Condition()
        self._worker: threading.Thread | None = None
        self._active_context: LifecycleExecutionContextV2 | None = None
        self._started = False
        self._stopping = False
        self._fatal_error: BaseException | None = None
        self._deferred_operation_ids: set[str] = set()

    def start(self) -> None:
        """Reconcile persisted authority before accepting reservations."""

        with self._condition:
            if self._started:
                return
            if self._stopping:
                raise RuntimeError("lifecycle executor is closed")
            recovered = self._store.reconcile_lifecycle_operations()
            self._started = True
            self._worker = threading.Thread(
                target=self._worker_main,
                name="openevo-desktop-lifecycle-v2",
                daemon=True,
            )
            self._worker.start()
        for work in recovered:
            self._publish(work.operation)
        self._wake_worker()

    def reserve(
        self,
        reservation: LifecycleOperationReservationV2,
        *,
        idempotency_key: str,
    ) -> m.LifecycleOperationV2:
        self._require_available()
        operation = self._store.reserve_lifecycle_operation(
            reservation,
            idempotency_key=idempotency_key,
        )
        self._publish(operation)
        self._wake_worker()
        return operation

    def cancel(
        self,
        operation_id: str,
        *,
        if_match: str,
        idempotency_key: str,
    ) -> m.LifecycleOperationV2:
        self._require_available()
        operation = self._store.request_lifecycle_cancellation(
            operation_id,
            if_match=if_match,
            idempotency_key=idempotency_key,
        )
        with self._condition:
            context = self._active_context
            if context is not None and context.operation.operation_id == operation_id:
                context.request_user_cancellation()
            self._condition.notify_all()
        self._publish(operation)
        return operation

    def close(self) -> None:
        self.request_shutdown()
        with self._condition:
            worker = self._worker
        if worker is not None and worker is not threading.current_thread():
            worker.join(timeout=self._close_timeout_seconds)

    def request_shutdown(self) -> None:
        """Fence durable completion as soon as process shutdown is signalled."""

        with self._condition:
            self._stopping = True
            context = self._active_context
            if context is not None:
                context.request_shutdown()
            self._condition.notify_all()

    def observe_progress(
        self,
        phase: m.LifecyclePhaseV2,
        progress: m.LifecycleProgressV2 | None,
        cancellable: bool,
    ) -> None:
        """Route an owned bridge checkpoint to the active persisted operation."""

        with self._condition:
            context = self._active_context
        if context is not None:
            context.checkpoint(phase, progress, cancellable=cancellable)

    def observe_output(self, source: LifecycleLogSourceV2, chunk: bytes) -> None:
        """Route owned SSH/Daemon bytes to the active operation sanitizer."""

        with self._condition:
            context = self._active_context
        if context is not None:
            context.output_observer(source, chunk)

    def __enter__(self) -> DesktopLifecycleExecutorV2:
        self.start()
        return self

    def __exit__(self, *args: object) -> None:
        del args
        self.close()

    def _worker_main(self) -> None:
        try:
            while True:
                with self._condition:
                    if self._stopping:
                        return
                with self._condition:
                    deferred = frozenset(self._deferred_operation_ids)
                work = self._store.claim_next_lifecycle_operation(
                    exclude_running_operation_ids=deferred,
                )
                if work is None:
                    with self._condition:
                        if self._stopping:
                            return
                        self._condition.wait()
                    continue
                self._publish(work.operation)
                was_deferred = self._execute(work)
                with self._condition:
                    if was_deferred:
                        self._deferred_operation_ids.add(work.operation.operation_id)
                    else:
                        self._deferred_operation_ids.clear()
        except BaseException as exc:
            with self._condition:
                self._fatal_error = exc
                self._stopping = True
                self._condition.notify_all()

    def _execute(self, work: LifecycleOperationWorkV2) -> bool:
        context = LifecycleExecutionContextV2(
            store=self._store,
            work=work,
            publish=self._publish,
            secret_canaries=self._secret_canaries,
            forbidden_endpoints=self._forbidden_endpoints,
            forbidden_paths=self._forbidden_paths,
        )
        with self._condition:
            if self._stopping:
                return False
            self._active_context = context
        try:
            if work.cancellation_requested:
                raise _LifecycleCancelled
            result = self._runners[work.operation.kind](context)
            try:
                validated_result = _RESULT_ADAPTER.validate_python(result, strict=True)
            except ValidationError as exc:
                raise RuntimeError("lifecycle runner returned an invalid result") from exc
            context.flush_output()
            if context.is_shutdown_requested():
                raise _LifecycleExecutorStopping
            current = context.current_work()
            if current.cancellation_requested:
                raise _LifecycleCancelled
            self._finish_with_fence(
                current,
                status="succeeded",
                result=validated_result,
                failure=None,
            )
        except _LifecycleExecutorStopping:
            return False
        except LifecycleOperationDeferredV2:
            if context.is_shutdown_requested():
                return False
            context.flush_output()
            return True
        except _LifecycleCancelled:
            if context.is_shutdown_requested():
                return False
            context.flush_output()
            current = context.current_work()
            self._finish_with_fence(
                current,
                status="cancelled",
                result=None,
                failure=None,
            )
        except BaseException as exc:
            if context.is_shutdown_requested():
                return False
            # An external process-group shutdown can terminate the owned SSH
            # master before the main-thread signal handler runs. Yield the GIL
            # briefly so that handler can fence this durable operation before
            # an infrastructure failure is committed as a terminal outcome.
            context.cancellation_event.wait(_PROCESS_SHUTDOWN_FAILURE_FENCE_SECONDS)
            if context.is_shutdown_requested():
                return False
            context.flush_output()
            current = context.current_work()
            if current.cancellation_requested:
                self._finish_with_fence(
                    current,
                    status="cancelled",
                    result=None,
                    failure=None,
                )
            else:
                failure = self._safe_error(exc, current)
                if context.is_shutdown_requested():
                    return False
                self._finish_with_fence(
                    current,
                    status="failed",
                    result=None,
                    failure=failure,
                )
        finally:
            context.close_output()
            with self._condition:
                if self._active_context is context:
                    self._active_context = None
                self._condition.notify_all()
        return False

    def _finish_with_fence(
        self,
        work: LifecycleOperationWorkV2,
        *,
        status: str,
        result: m.LifecycleResultV2 | None,
        failure: m.DesktopErrorV2 | None,
    ) -> m.LifecycleOperationV2:
        current = work
        terminal_status = status
        terminal_result = result
        terminal_failure = failure
        for _attempt in range(3):
            if terminal_status != "cancelled" and current.cancellation_requested:
                terminal_status = "cancelled"
                terminal_result = None
                terminal_failure = None
            try:
                operation = self._store.finish_lifecycle_operation(
                    LifecycleOperationCompletionV2(
                        operation_id=current.operation.operation_id,
                        expected_etag=current.operation.etag,
                        status=terminal_status,
                        result=terminal_result,
                        failure=terminal_failure,
                    )
                )
            except ProviderPreconditionFailedV2:
                current = self._store.get_lifecycle_operation_work(current.operation.operation_id)
                continue
            self._publish(operation)
            return operation
        raise ProviderPreconditionFailedV2(
            "lifecycle operation changed repeatedly during terminal commit"
        )

    def _safe_error(
        self,
        exc: BaseException,
        work: LifecycleOperationWorkV2,
    ) -> m.DesktopErrorV2:
        try:
            error = self._error_mapper(exc, work)
            return m.DesktopErrorV2.model_validate(error, strict=True)
        except BaseException:
            return self._default_error_mapper(exc, work)

    @staticmethod
    def _default_error_mapper(
        _exc: BaseException,
        work: LifecycleOperationWorkV2,
    ) -> m.DesktopErrorV2:
        return m.DesktopErrorV2(
            code="lifecycle_operation_failed",
            summary="The lifecycle operation could not be completed.",
            retryable=True,
            action="retry",
            affected_resource_id=work.operation.resource.resource_id,
        )

    def _require_available(self) -> None:
        with self._condition:
            if not self._started:
                raise RuntimeError("lifecycle executor has not started")
            if self._stopping:
                if self._fatal_error is not None:
                    raise RuntimeError("lifecycle executor is unavailable")
                raise RuntimeError("lifecycle executor is closed")

    def _wake_worker(self) -> None:
        with self._condition:
            self._condition.notify_all()

    def _publish(self, operation: m.LifecycleOperationV2) -> None:
        if self._observer is None:
            return
        try:
            self._observer(operation)
        except BaseException:
            pass


__all__ = [
    "DesktopLifecycleExecutorV2",
    "LifecycleErrorMapperV2",
    "LifecycleExecutionContextV2",
    "LifecycleOperationDeferredV2",
    "LifecycleOperationObserverV2",
    "LifecycleRunnerV2",
]
