"""In-process Session execution ownership for the self-hosted daemon.

The durable lifecycle belongs to :mod:`openevo.daemon.session_store`; this
module owns the complementary process-local concerns: exclusive admission,
worker thread lifetime, cancellation-signal lookup, and unconditional cleanup.
Domain-specific harness/workspace behavior is supplied by the daemon adapter
while the compatibility script is migrated incrementally.
"""

from __future__ import annotations

import secrets
import threading
from collections.abc import Callable
from typing import Any, Protocol


class SessionExecutionConflictError(RuntimeError):
    """A Session cannot start because another exclusive operation is active."""


class SessionRuntimeStore(Protocol):
    """Minimal durable authority required by :class:`SessionExecutionManager`."""

    def get_session(self, session_id: str) -> dict[str, Any]: ...

    def start_session(self, session_id: str, request: dict[str, Any]) -> None: ...

    def request_session_cancellation(self, session_id: str) -> dict[str, Any]: ...

    def cancellation_requested(self, session_id: str) -> bool: ...


class ExclusiveOperationLock(Protocol):
    def acquire(self, blocking: bool = True) -> bool: ...

    def release(self) -> None: ...


CancellationFactory = Callable[[], Any]
SessionExecutor = Callable[[str, dict[str, Any], Any], None]
ExecutionFailureHandler = Callable[[str, BaseException], None]
SessionIdFactory = Callable[[], str]


class SessionExecutionManager:
    """Own one daemon Session worker and its live cancellation signal.

    The manager deliberately does not know Codex, workspace, or Evolution
    details.  The executor must drive the durable Session to a terminal state;
    ``execution_failed`` is the final fail-closed guard for an exception that
    escapes that adapter or for a worker that cannot be started.
    """

    def __init__(
        self,
        *,
        store: SessionRuntimeStore,
        executor: SessionExecutor,
        cancellation_factory: CancellationFactory,
        execution_failed: ExecutionFailureHandler,
        operation_lock: ExclusiveOperationLock | None = None,
        session_id_factory: SessionIdFactory | None = None,
    ) -> None:
        self._store = store
        self._executor = executor
        self._cancellation_factory = cancellation_factory
        self._execution_failed = execution_failed
        self._operation_lock = operation_lock if operation_lock is not None else threading.Lock()
        self._session_id_factory = session_id_factory or _new_session_id
        self._active_lock = threading.Lock()
        self._active: dict[str, Any] = {}

    def submit(
        self,
        request: dict[str, Any],
        *,
        session_id: str | None = None,
    ) -> str:
        """Durably admit and asynchronously execute one Session.

        Explicit ``session_id`` values are idempotency identities.  An exact
        retry returns the original identity without acquiring the execution
        lock or starting another worker.
        """

        stable_request = dict(request)
        if session_id is not None:
            try:
                existing = self._store.get_session(session_id)
            except KeyError:
                pass
            else:
                expected = (
                    stable_request["project_id"],
                    stable_request["task_title"],
                    stable_request["instruction"],
                )
                actual = (
                    existing["project_id"],
                    existing["task_title"],
                    existing["instruction"],
                )
                requested_head_id = stable_request.get("project_head_id")
                if actual != expected or (
                    requested_head_id is not None
                    and existing.get("project_head_id") != requested_head_id
                ):
                    raise SessionExecutionConflictError(
                        "Task action_id is already bound to another request"
                    )
                return session_id

        if not self._operation_lock.acquire(blocking=False):
            raise SessionExecutionConflictError("another development session is running")
        admitted_session_id = session_id or self._session_id_factory()
        try:
            cancellation = self._cancellation_factory()
            if not callable(getattr(cancellation, "cancel", None)):
                raise TypeError("Session cancellation signal must provide cancel()")
            self._store.start_session(admitted_session_id, stable_request)
            admitted = self._store.get_session(admitted_session_id)
            execution_request = {
                **stable_request,
                "context_artifact_ids": list(admitted.get("context_artifact_ids", [])),
            }
        except BaseException:
            self._operation_lock.release()
            raise

        try:
            thread = threading.Thread(
                target=self._run,
                name=f"openevo-{admitted_session_id}",
                args=(admitted_session_id, execution_request, cancellation),
                daemon=True,
            )
            with self._active_lock:
                self._active[admitted_session_id] = cancellation
            if self._store.cancellation_requested(admitted_session_id):
                cancellation.cancel()
            thread.start()
        except BaseException as exc:
            with self._active_lock:
                self._active.pop(admitted_session_id, None)
            try:
                self._execution_failed(admitted_session_id, exc)
            except BaseException:
                pass
            finally:
                self._operation_lock.release()
            raise
        return admitted_session_id

    def cancel(self, session_id: str) -> dict[str, Any]:
        """Persist cancellation intent before signalling the live worker."""

        session = self._store.request_session_cancellation(session_id)
        with self._active_lock:
            cancellation = self._active.get(session_id)
        if cancellation is not None:
            cancellation.cancel()
        return session

    def active_session_ids(self) -> tuple[str, ...]:
        """Return a stable diagnostic snapshot without exposing signals."""

        with self._active_lock:
            return tuple(sorted(self._active))

    def _run(
        self,
        session_id: str,
        request: dict[str, Any],
        cancellation: Any,
    ) -> None:
        try:
            self._executor(session_id, request, cancellation)
            state = self._store.get_session(session_id).get("state")
            if state not in {"completed", "failed", "cancelled"}:
                raise RuntimeError("Session executor returned without recording a terminal state")
        except BaseException as exc:
            try:
                self._execution_failed(session_id, exc)
            except BaseException:
                # The process-level recovery pass remains authoritative if both
                # the adapter and its final fail-closed persistence hook fail.
                pass
        finally:
            with self._active_lock:
                self._active.pop(session_id, None)
            self._operation_lock.release()


def _new_session_id() -> str:
    return f"dev-session-{secrets.token_hex(8)}"
