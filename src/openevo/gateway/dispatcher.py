"""Stage-isolated session dispatcher for gateway execution.

Drives four stages — INIT, READY, RUNNING, POSTRUN — each with an isolated
worker pool. Queued vs. executing within a stage is the `inflight` bool on
`ManagedSession`, so the stage enum stays small. Eval-runtime prewarm is an
ad-hoc background task owned by the session handler, not a dedicated stage.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Awaitable, Callable, Final

from openevo.harness.models import AgentRunResult
from openevo.gateway.session_files import CredentialFileIdentity, CredentialRedactor
from openevo.rollout.models import SessionDispatchRequest, SessionResult, SessionStatus
from openevo.rollout.timer import StageTimer
from openevo.runtime.base import BaseRuntime
from openevo.runtime.managed import ManagedCredentialMount
from openevo.runtime.models import ExecInput

logger = logging.getLogger(__name__)

StageCallback = Callable[["ManagedSession"], Awaitable[None]]
StageTransitionCallback = Callable[["ManagedSession"], None]
BeforeCancelCallback = Callable[["ManagedSession"], None]
_STOP = object()
_SUBSCRIPTION_AUTH_MODES: Final[frozenset[str]] = frozenset(
    {"subscription", "chatgpt_subscription"}
)


class DispatcherUnavailableError(RuntimeError):
    """The dispatcher cannot atomically accept more session work."""


class SessionStage(str, Enum):
    INIT = "INIT"
    READY = "READY"
    RUNNING = "RUNNING"
    POSTRUN = "POSTRUN"


@dataclass(slots=True)
class DispatcherSnapshot:
    init_queue_depth: int = 0
    init_inflight: int = 0
    ready_depth: int = 0
    run_inflight: int = 0
    postrun_queue_depth: int = 0
    postrun_inflight: int = 0

    @property
    def active_count(self) -> int:
        return (
            self.init_queue_depth
            + self.init_inflight
            + self.ready_depth
            + self.run_inflight
            + self.postrun_queue_depth
            + self.postrun_inflight
        )


@dataclass(frozen=True, slots=True)
class DispatcherAdmissionLease:
    """Ephemeral right to publish one session while shutdown waits."""

    token: object = field(repr=False)


@dataclass(slots=True)
class ManagedSession:
    """Per-session state flowing through the gateway dispatcher."""

    request: SessionDispatchRequest
    timer: StageTimer
    session_dir: Path
    artifacts_dir: Path
    session_root_identity: tuple[int, int, int] | None = None
    log_authority_dir: Path | None = None
    log_authority_identity: tuple[int, int, int] | None = None
    credential_dir: Path | None = None
    credential_root_identity: tuple[int, int, int] | None = None
    credential_auth_identity: CredentialFileIdentity | None = None
    credential_mount: ManagedCredentialMount | None = None
    credential_redactor: CredentialRedactor | None = None
    runtime: BaseRuntime | None = None
    agent_result: AgentRunResult | None = None
    final_result: SessionResult | None = None
    pending_status: SessionStatus | None = None
    pending_error: str | None = None
    postrun_steps: list[ExecInput] = field(default_factory=list)
    eval_prewarm_task: asyncio.Task | None = None
    eval_runtime: BaseRuntime | None = None
    cancel_requested: bool = False
    cancel_event: asyncio.Event = field(default_factory=asyncio.Event)
    execution_deadline: float | None = None
    finalization_deadline: float | None = None
    runtime_cleanup_blocked: bool = False
    ready_slot_owned: bool = False
    cleanup_journal_revision: int = 0
    cleanup_journal_generation: str | None = None
    cleanup_journal_epoch: int | None = None
    cleanup_journal_epoch_token: str | None = None
    stage: SessionStage = SessionStage.INIT
    inflight: bool = False

    @property
    def session_id(self) -> str:
        return self.request.session_id

    @property
    def has_terminal_outcome(self) -> bool:
        return self.final_result is not None or self.pending_status is not None

    @property
    def credential_capable(self) -> bool:
        auth_mode = self.request.agent.settings.get("auth_mode")
        return (
            self.credential_dir is not None
            or self.credential_mount is not None
            or self.credential_redactor is not None
            or (isinstance(auth_mode, str) and auth_mode in _SUBSCRIPTION_AUTH_MODES)
        )


class SessionDispatcher:
    """Drive INIT -> READY -> RUNNING -> POSTRUN with isolated worker pools."""

    def __init__(
        self,
        *,
        max_init_workers: int,
        max_run_workers: int,
        max_postrun_workers: int,
    ) -> None:
        if max_init_workers < 1 or max_run_workers < 1 or max_postrun_workers < 1:
            raise ValueError("all stage worker counts must be at least 1")
        self.max_init_workers = max_init_workers
        self.max_run_workers = max_run_workers
        self.max_postrun_workers = max_postrun_workers
        self.on_init: StageCallback | None = None
        self.on_run: StageCallback | None = None
        self.on_postrun: StageCallback | None = None
        self.on_stage_change: StageTransitionCallback | None = None
        self._init_queue: asyncio.Queue[str | object] = asyncio.Queue()
        self._ready_queue: asyncio.Queue[str | object] = asyncio.Queue()
        self._postrun_queue: asyncio.Queue[str | object] = asyncio.Queue()
        self._ready_slots = asyncio.Semaphore(max_run_workers)
        self._sessions: dict[str, ManagedSession] = {}
        self._lock = asyncio.Lock()
        self._condition = asyncio.Condition(self._lock)
        self._workers: list[asyncio.Task[None]] = []
        self._started = False
        self._accepting = False
        self._admission_tokens: set[object] = set()

    async def start(self) -> None:
        async with self._condition:
            if self._started:
                return
            self._workers = [
                *(
                    asyncio.create_task(self._init_worker())
                    for _ in range(self.max_init_workers)
                ),
                *(asyncio.create_task(self._run_worker()) for _ in range(self.max_run_workers)),
                *(
                    asyncio.create_task(self._postrun_worker())
                    for _ in range(self.max_postrun_workers)
                ),
            ]
            self._started = True
            self._accepting = True

    async def stop(self) -> list[ManagedSession]:
        async with self._condition:
            if not self._started:
                return []
            self._accepting = False
            while self._admission_tokens:
                await self._condition.wait()
            self._started = False
            sessions = list(self._sessions.values())
            self._sessions.clear()
            workers = self._workers
            self._workers = []
        cancel_tasks: list[tuple[ManagedSession, Awaitable[None]]] = []
        for managed in sessions:
            managed.cancel_requested = True
            managed.cancel_event.set()
            self._release_ready_slot(managed)
            if managed.runtime is not None:
                cancel_tasks.append((managed, managed.runtime.cancel()))
        if cancel_tasks:
            outcomes = await asyncio.gather(
                *(operation for _, operation in cancel_tasks),
                return_exceptions=True,
            )
            for (managed, _), outcome in zip(cancel_tasks, outcomes, strict=True):
                if isinstance(outcome, BaseException):
                    self._log_session_exception(
                        managed,
                        "Runtime cancellation failed during dispatcher shutdown",
                        outcome,
                        level=logging.WARNING,
                    )
        for task in workers:
            task.cancel()
        if workers:
            await asyncio.gather(*workers, return_exceptions=True)
        return sessions

    async def reserve_admission(self) -> DispatcherAdmissionLease:
        async with self._condition:
            if not self._started or not self._accepting:
                raise DispatcherUnavailableError("dispatcher has not been started")
            token = object()
            self._admission_tokens.add(token)
            return DispatcherAdmissionLease(token=token)

    async def release_admission(self, lease: DispatcherAdmissionLease) -> None:
        async with self._condition:
            if lease.token in self._admission_tokens:
                self._admission_tokens.remove(lease.token)
                self._condition.notify_all()

    async def enqueue(
        self,
        managed: ManagedSession,
        *,
        admission: DispatcherAdmissionLease | None = None,
    ) -> None:
        async with self._condition:
            try:
                if not self._started or not self._accepting:
                    raise DispatcherUnavailableError("dispatcher has not been started")
                if admission is not None and admission.token not in self._admission_tokens:
                    raise DispatcherUnavailableError(
                        "dispatcher admission lease is unavailable"
                    )
                if managed.session_id in self._sessions:
                    raise ValueError(f"session {managed.session_id} is already enqueued")
                try:
                    self._sessions[managed.session_id] = managed
                    # The queue is unbounded. Publish while still holding the registry
                    # lock so cancellation cannot strand a registered session between
                    # the in-memory admission and its INIT work item.
                    self._init_queue.put_nowait(managed.session_id)
                except Exception as exc:
                    self._sessions.pop(managed.session_id, None)
                    raise DispatcherUnavailableError(
                        "dispatcher could not publish the INIT work item"
                    ) from exc
            finally:
                if admission is not None and admission.token in self._admission_tokens:
                    self._admission_tokens.remove(admission.token)
                    self._condition.notify_all()

    async def cancel(
        self,
        session_id: str,
        *,
        before_cancel: BeforeCancelCallback | None = None,
    ) -> bool:
        should_enqueue_postrun = False
        preserved_failures: list[BaseException] = []
        async with self._lock:
            managed = self._sessions.get(session_id)
            if managed is None:
                return False
            if managed.cancel_requested:
                return True
            if before_cancel is not None:
                before_cancel(managed)
            managed.cancel_requested = True
            managed.cancel_event.set()
            # READY includes both semaphore waiters and sessions that own a slot.
            if managed.stage == SessionStage.READY and not managed.inflight:
                self._release_ready_slot(managed)
                managed.stage = SessionStage.POSTRUN
                managed.inflight = False
                should_enqueue_postrun = True
            elif managed.stage == SessionStage.INIT and not managed.inflight:
                managed.stage = SessionStage.POSTRUN
                managed.inflight = False
                should_enqueue_postrun = True
        if managed.runtime is not None:
            try:
                await managed.runtime.cancel()
            except Exception as exc:
                self._log_session_exception(
                    managed,
                    "Runtime cancellation failed; continuing cleanup",
                    exc,
                    level=logging.WARNING,
                )
            except BaseException as exc:
                preserved_failures.append(exc)
        if should_enqueue_postrun:
            preserved_failures.extend(
                await self._await_owned_cleanup(self._enqueue_postrun(managed))
            )
        self._raise_preserved_failures(
            "runtime cancellation and post-run enqueue failed",
            preserved_failures,
        )
        return True

    async def active_count(self) -> int:
        return (await self.snapshot()).active_count

    async def owns_session(self, session_id: str) -> bool:
        """Return whether this dispatcher still owns the live session lifecycle."""

        async with self._lock:
            return session_id in self._sessions

    async def wait_terminated(self, session_id: str) -> None:
        async with self._condition:
            while session_id in self._sessions:
                await self._condition.wait()

    async def snapshot(self) -> DispatcherSnapshot:
        async with self._lock:
            snap = DispatcherSnapshot()
            for managed in self._sessions.values():
                if managed.stage == SessionStage.INIT:
                    if managed.inflight:
                        snap.init_inflight += 1
                    else:
                        snap.init_queue_depth += 1
                elif managed.stage == SessionStage.READY:
                    snap.ready_depth += 1
                elif managed.stage == SessionStage.RUNNING:
                    snap.run_inflight += 1
                elif managed.stage == SessionStage.POSTRUN:
                    if managed.inflight:
                        snap.postrun_inflight += 1
                    else:
                        snap.postrun_queue_depth += 1
            return snap

    # ------------------------------------------------------------------
    # Workers
    # ------------------------------------------------------------------

    async def _init_worker(self) -> None:
        while True:
            item = await self._init_queue.get()
            if item is _STOP:
                return
            session_id = str(item)
            managed = await self._begin(session_id, SessionStage.INIT)
            if managed is None:
                continue
            if not (managed.cancel_requested or managed.has_terminal_outcome):
                await self._safe_invoke(self.on_init, managed, SessionStage.INIT)
            await self._finish_init(managed)

    async def _run_worker(self) -> None:
        while True:
            item = await self._ready_queue.get()
            if item is _STOP:
                return
            session_id = str(item)
            managed = await self._begin(session_id, SessionStage.RUNNING, from_ready=True)
            if managed is None:
                continue
            if not (managed.cancel_requested or managed.has_terminal_outcome):
                await self._safe_invoke(self.on_run, managed, SessionStage.RUNNING)
            await self._transition_to_postrun(managed)

    async def _postrun_worker(self) -> None:
        while True:
            item = await self._postrun_queue.get()
            if item is _STOP:
                return
            session_id = str(item)
            managed = await self._begin(session_id, SessionStage.POSTRUN)
            if managed is None:
                continue
            interruptions = await self._await_owned_cleanup(
                self._invoke_postrun_and_pop(managed)
            )
            self._raise_preserved_failures(
                "post-run cleanup was interrupted",
                interruptions,
            )

    async def _safe_invoke(
        self,
        callback: StageCallback | None,
        managed: ManagedSession,
        stage: SessionStage,
        *,
        preserve_task_cancellation: bool = True,
    ) -> None:
        if callback is None:
            logger.error("Dispatcher stage %s has no callback", stage)
            return
        try:
            await callback(managed)
        except BaseException as exc:
            current = asyncio.current_task()
            if (
                preserve_task_cancellation
                and isinstance(exc, asyncio.CancelledError)
                and current is not None
                and current.cancelling()
            ):
                raise
            self._log_session_exception(
                managed,
                f"Dispatcher stage {stage} failed",
                exc,
            )

    async def _enqueue_postrun(self, managed: ManagedSession) -> None:
        self._notify_stage_change(managed)
        await self._postrun_queue.put(managed.session_id)

    async def _invoke_postrun_and_pop(self, managed: ManagedSession) -> None:
        try:
            await self._safe_invoke(
                self.on_postrun,
                managed,
                SessionStage.POSTRUN,
                preserve_task_cancellation=False,
            )
        finally:
            async with self._condition:
                self._sessions.pop(managed.session_id, None)
                self._condition.notify_all()

    @staticmethod
    async def _await_owned_cleanup(awaitable: Awaitable[None]) -> list[BaseException]:
        """Finish one owned cleanup operation while retaining caller interruption."""

        task = asyncio.ensure_future(awaitable)
        failures: list[BaseException] = []
        while True:
            try:
                await asyncio.shield(task)
                return failures
            except asyncio.CancelledError as exc:
                failures.append(exc)
                if not task.done():
                    continue
            except BaseException as exc:
                failures.append(exc)
                return failures
            try:
                task.result()
            except BaseException as exc:
                failures.append(exc)
            return failures

    @staticmethod
    def _raise_preserved_failures(message: str, failures: list[BaseException]) -> None:
        if not failures:
            return
        if len(failures) == 1:
            raise failures[0]
        raise BaseExceptionGroup(message, failures)

    async def _begin(
        self,
        session_id: str,
        stage: SessionStage,
        *,
        from_ready: bool = False,
    ) -> ManagedSession | None:
        """Mark the session as inflight in *stage*. Returns None if session is gone."""
        async with self._lock:
            managed = self._sessions.get(session_id)
            if managed is None:
                return None
            if from_ready:
                if managed.stage != SessionStage.READY or not managed.ready_slot_owned:
                    return None
            managed.stage = stage
            managed.inflight = True
        self._notify_stage_change(managed)
        return managed

    async def _finish_init(self, managed: ManagedSession) -> None:
        """After INIT callback returns, move to READY (waiting a run slot) or POSTRUN."""
        if managed.cancel_requested or managed.has_terminal_outcome:
            await self._move_to_postrun(managed)
            return

        async with self._lock:
            if managed.session_id not in self._sessions:
                return
            managed.stage = SessionStage.READY
            managed.inflight = False
        self._notify_stage_change(managed)

        acquired = await self._acquire_ready_slot(managed)
        if not acquired:
            await self._move_to_postrun(managed, release_ready=False)
            return
        await self._ready_queue.put(managed.session_id)

    async def _transition_to_postrun(self, managed: ManagedSession) -> None:
        # The RUN callback is done (or was skipped). The ready slot is released
        # back to the pool on exit of RUNNING.
        self._release_ready_slot(managed)
        await self._move_to_postrun(managed, release_ready=False)

    async def _move_to_postrun(
        self, managed: ManagedSession, *, release_ready: bool = True
    ) -> None:
        transitioned = False
        async with self._lock:
            if managed.session_id not in self._sessions:
                return
            if managed.stage == SessionStage.POSTRUN:
                return
            if release_ready and managed.stage == SessionStage.READY and not managed.inflight:
                self._release_ready_slot(managed)
            managed.stage = SessionStage.POSTRUN
            managed.inflight = False
            transitioned = True
        if transitioned:
            self._notify_stage_change(managed)
            await self._postrun_queue.put(managed.session_id)

    async def _acquire_ready_slot(self, managed: ManagedSession) -> bool:
        """Race the semaphore against session cancellation. Returns True on acquire."""
        if managed.cancel_event.is_set() or managed.has_terminal_outcome:
            return False
        acquire_task = asyncio.create_task(self._ready_slots.acquire())
        cancel_task = asyncio.create_task(managed.cancel_event.wait())
        ownership_transferred = False
        try:
            await asyncio.wait(
                {acquire_task, cancel_task},
                return_when=asyncio.FIRST_COMPLETED,
            )
            acquired = (
                acquire_task.done()
                and not acquire_task.cancelled()
                and acquire_task.exception() is None
            )
            if not acquired:
                return False
            async with self._lock:
                if (
                    managed.session_id in self._sessions
                    and managed.stage == SessionStage.READY
                    and not managed.cancel_event.is_set()
                    and not managed.has_terminal_outcome
                ):
                    managed.ready_slot_owned = True
                    ownership_transferred = True
                    return True
            return False
        finally:
            cancel_task.cancel()
            if not acquire_task.done():
                acquire_task.cancel()
            await asyncio.gather(acquire_task, cancel_task, return_exceptions=True)
            if (
                not ownership_transferred
                and not acquire_task.cancelled()
                and acquire_task.exception() is None
            ):
                self._ready_slots.release()

    def _release_ready_slot(self, managed: ManagedSession) -> bool:
        if not managed.ready_slot_owned:
            return False
        managed.ready_slot_owned = False
        self._ready_slots.release()
        return True

    def _notify_stage_change(self, managed: ManagedSession) -> None:
        callback = self.on_stage_change
        if callback is None:
            return
        try:
            callback(managed)
        except BaseException as exc:
            self._log_session_exception(
                managed,
                "Dispatcher stage-change callback failed",
                exc,
            )

    @staticmethod
    def _log_session_exception(
        managed: ManagedSession,
        message: str,
        exc: BaseException,
        *,
        level: int = logging.ERROR,
    ) -> None:
        if managed.credential_capable:
            rendered = type(exc).__name__
            if managed.credential_redactor is not None:
                detail = managed.credential_redactor.redact(str(exc))
                if detail:
                    rendered = f"{rendered}: {detail}"
            logger.log(
                level,
                "%s for session %s [%s]",
                message,
                managed.session_id,
                rendered,
            )
            return
        logger.log(
            level,
            "%s for session %s",
            message,
            managed.session_id,
            exc_info=(type(exc), exc, exc.__traceback__),
        )
