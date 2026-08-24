from __future__ import annotations

import threading
import time
from collections.abc import Callable
from typing import Any

import pytest

from openevo.daemon import session_runtime
from openevo.daemon.session_runtime import (
    SessionExecutionConflictError,
    SessionExecutionManager,
)


class _Cancellation:
    def __init__(self) -> None:
        self.requested = threading.Event()

    def cancel(self) -> None:
        self.requested.set()


class _Store:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.records: dict[str, dict[str, Any]] = {}
        self.cancellation_order: list[str] = []

    def get_session(self, session_id: str) -> dict[str, Any]:
        with self._lock:
            try:
                return dict(self.records[session_id])
            except KeyError:
                raise KeyError(session_id) from None

    def start_session(self, session_id: str, request: dict[str, str]) -> None:
        with self._lock:
            if session_id in self.records:
                raise RuntimeError("duplicate Session")
            self.records[session_id] = {**request, "state": "running"}

    def request_session_cancellation(self, session_id: str) -> dict[str, Any]:
        with self._lock:
            record = self.records[session_id]
            record["state"] = "cancelling"
            self.cancellation_order.append("persisted")
            return dict(record)

    def cancellation_requested(self, session_id: str) -> bool:
        with self._lock:
            return self.records[session_id]["state"] == "cancelling"

    def fail(self, session_id: str, error: BaseException) -> None:
        with self._lock:
            self.records[session_id]["state"] = "failed"
            self.records[session_id]["error"] = str(error)

    def finish(self, session_id: str, state: str) -> None:
        with self._lock:
            self.records[session_id]["state"] = state


def _request(instruction: str = "Wait") -> dict[str, str]:
    return {
        "project_id": "project-1",
        "project_name": "Project",
        "task_title": "Task",
        "instruction": instruction,
    }


def _wait_until(predicate: Callable[[], bool], *, timeout: float = 2.0) -> None:
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() >= deadline:
            raise AssertionError("condition did not become true")
        time.sleep(0.005)


def test_manager_persists_cancellation_before_signalling_and_releases_lock() -> None:
    store = _Store()
    started = threading.Event()
    observed_order: list[str] = []
    generated_ids = iter(("session-1", "session-2"))

    def execute(
        session_id: str,
        request: dict[str, str],
        cancellation: _Cancellation,
    ) -> None:
        if request["instruction"] == "Wait":
            started.set()
            assert cancellation.requested.wait(2)
            observed_order.extend(store.cancellation_order)
            observed_order.append("signalled")
            store.finish(session_id, "cancelled")
        else:
            store.finish(session_id, "completed")

    manager = SessionExecutionManager(
        store=store,
        executor=execute,
        cancellation_factory=_Cancellation,
        execution_failed=store.fail,
        session_id_factory=lambda: next(generated_ids),
    )

    assert manager.submit(_request()) == "session-1"
    assert started.wait(2)
    assert manager.active_session_ids() == ("session-1",)
    with pytest.raises(
        SessionExecutionConflictError,
        match="another development session is running",
    ):
        manager.submit(_request("Another"))

    assert manager.cancel("session-1")["state"] == "cancelling"
    _wait_until(lambda: manager.active_session_ids() == ())
    assert observed_order == ["persisted", "signalled"]

    assert manager.submit(_request("Quick")) == "session-2"
    _wait_until(lambda: manager.active_session_ids() == ())


def test_explicit_session_identity_is_exactly_idempotent() -> None:
    store = _Store()
    started = threading.Event()

    def execute(
        _session_id: str,
        _request_value: dict[str, str],
        cancellation: _Cancellation,
    ) -> None:
        started.set()
        assert cancellation.requested.wait(2)
        store.finish(_session_id, "cancelled")

    manager = SessionExecutionManager(
        store=store,
        executor=execute,
        cancellation_factory=_Cancellation,
        execution_failed=store.fail,
    )
    request = _request()
    assert manager.submit(request, session_id="action-1") == "action-1"
    assert started.wait(2)
    assert manager.submit(request, session_id="action-1") == "action-1"

    with pytest.raises(
        SessionExecutionConflictError,
        match="already bound to another request",
    ):
        manager.submit({**request, "instruction": "Different"}, session_id="action-1")

    manager.cancel("action-1")
    _wait_until(lambda: manager.active_session_ids() == ())


def test_durable_cancellation_that_wins_admission_race_reaches_worker() -> None:
    class _PreCancelledStore(_Store):
        def start_session(self, session_id: str, request: dict[str, str]) -> None:
            super().start_session(session_id, request)
            with self._lock:
                self.records[session_id]["state"] = "cancelling"
                self.cancellation_order.append("persisted")

    store = _PreCancelledStore()
    cancellation_seen = threading.Event()

    def execute(
        _session_id: str,
        _request_value: dict[str, str],
        cancellation: _Cancellation,
    ) -> None:
        if cancellation.requested.is_set():
            cancellation_seen.set()
            store.finish(_session_id, "cancelled")

    manager = SessionExecutionManager(
        store=store,
        executor=execute,
        cancellation_factory=_Cancellation,
        execution_failed=store.fail,
        session_id_factory=lambda: "pre-cancelled",
    )

    assert manager.submit(_request()) == "pre-cancelled"
    assert cancellation_seen.wait(2)
    _wait_until(lambda: manager.active_session_ids() == ())


def test_escaped_executor_failure_is_terminal_and_does_not_leak_exclusivity() -> None:
    store = _Store()
    generated_ids = iter(("failed-session", "next-session"))

    def execute(
        _session_id: str,
        request: dict[str, str],
        _cancellation: _Cancellation,
    ) -> None:
        if request["instruction"] == "Fail":
            raise RuntimeError("runner exploded")
        store.finish(_session_id, "completed")

    manager = SessionExecutionManager(
        store=store,
        executor=execute,
        cancellation_factory=_Cancellation,
        execution_failed=store.fail,
        session_id_factory=lambda: next(generated_ids),
    )

    assert manager.submit(_request("Fail")) == "failed-session"
    _wait_until(lambda: store.get_session("failed-session")["state"] == "failed")
    _wait_until(lambda: manager.active_session_ids() == ())
    assert store.get_session("failed-session")["error"] == "runner exploded"

    assert manager.submit(_request("Quick")) == "next-session"
    _wait_until(lambda: manager.active_session_ids() == ())


def test_executor_return_without_terminal_state_fails_closed() -> None:
    store = _Store()
    manager = SessionExecutionManager(
        store=store,
        executor=lambda *_args: None,
        cancellation_factory=_Cancellation,
        execution_failed=store.fail,
        session_id_factory=lambda: "unterminated-session",
    )

    assert manager.submit(_request("Return early")) == "unterminated-session"
    _wait_until(lambda: store.get_session("unterminated-session")["state"] == "failed")
    assert (
        "without recording a terminal state" in store.get_session("unterminated-session")["error"]
    )
    _wait_until(lambda: manager.active_session_ids() == ())


def test_worker_start_failure_is_recorded_and_releases_external_lock(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _Store()
    operation_lock = threading.Lock()

    class _BrokenThread:
        def start(self) -> None:
            raise RuntimeError("thread unavailable")

    monkeypatch.setattr(
        session_runtime.threading,
        "Thread",
        lambda **_kwargs: _BrokenThread(),
    )
    manager = SessionExecutionManager(
        store=store,
        executor=lambda *_args: None,
        cancellation_factory=_Cancellation,
        execution_failed=store.fail,
        operation_lock=operation_lock,
        session_id_factory=lambda: "failed-start",
    )

    with pytest.raises(RuntimeError, match="thread unavailable"):
        manager.submit(_request())
    assert store.get_session("failed-start")["state"] == "failed"
    assert manager.active_session_ids() == ()
    assert operation_lock.acquire(blocking=False) is True
    operation_lock.release()
