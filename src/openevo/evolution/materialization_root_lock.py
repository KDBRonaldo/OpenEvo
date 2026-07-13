from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
import fcntl
import os
import threading
from typing import Iterator


_OPEN_FLAGS = os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_DIRECTORY


@dataclass(slots=True)
class _ThreadLockState:
    process_id: int
    descriptor: int
    depth: int


class MaterializationRootLock:
    """A thread-reentrant, process-exclusive lock for one directory path."""

    def __init__(self, path: str | os.PathLike[str]) -> None:
        self.path = os.path.abspath(os.fspath(path))
        self._process_id = os.getpid()
        self._thread_lock = threading.RLock()
        self._thread_state = threading.local()
        self._active_descriptor: int | None = None

    def _reset_after_fork(self, process_id: int) -> None:
        if self._process_id == process_id:
            return

        # Closing the child's duplicate preserves the parent's OFD lock. An explicit
        # LOCK_UN here would release the lock shared with the parent after fork.
        descriptor = self._active_descriptor
        if descriptor is not None:
            try:
                os.close(descriptor)
            except OSError:
                pass
        self._process_id = process_id
        self._thread_lock = threading.RLock()
        self._thread_state = threading.local()
        self._active_descriptor = None

    def _ensure_current_process(self) -> None:
        process_id = os.getpid()
        if self._process_id != process_id:
            self._reset_after_fork(process_id)

    @contextmanager
    def locked(self) -> Iterator[int]:
        """Yield a no-follow directory FD held under an exclusive flock."""

        self._ensure_current_process()
        process_id = os.getpid()
        thread_lock = self._thread_lock
        with thread_lock:
            state = getattr(self._thread_state, "lock_state", None)
            if state is not None and state.process_id == process_id:
                state.depth += 1
                try:
                    yield state.descriptor
                finally:
                    state.depth -= 1
                return

            descriptor = os.open(self.path, _OPEN_FLAGS)
            self._active_descriptor = descriptor
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX)
            except BaseException:
                try:
                    os.close(descriptor)
                finally:
                    self._active_descriptor = None
                raise

            state = _ThreadLockState(
                process_id=process_id,
                descriptor=descriptor,
                depth=1,
            )
            self._thread_state.lock_state = state
            try:
                yield descriptor
            finally:
                if os.getpid() == process_id:
                    state.depth -= 1
                    del self._thread_state.lock_state
                    try:
                        fcntl.flock(descriptor, fcntl.LOCK_UN)
                    finally:
                        try:
                            os.close(descriptor)
                        finally:
                            self._active_descriptor = None


_MANAGERS: dict[str, MaterializationRootLock] = {}
_MANAGERS_GUARD = threading.Lock()
_MANAGERS_PROCESS_ID = os.getpid()


def _reset_managers_after_fork() -> None:
    global _MANAGERS_GUARD, _MANAGERS_PROCESS_ID

    process_id = os.getpid()
    for manager in _MANAGERS.values():
        manager._reset_after_fork(process_id)
    _MANAGERS_GUARD = threading.Lock()
    _MANAGERS_PROCESS_ID = process_id


os.register_at_fork(after_in_child=_reset_managers_after_fork)


def get_materialization_root_lock(
    path: str | os.PathLike[str],
) -> MaterializationRootLock:
    """Return the process-local shared lock manager for ``path``."""

    global _MANAGERS_GUARD, _MANAGERS_PROCESS_ID

    process_id = os.getpid()
    if _MANAGERS_PROCESS_ID != process_id:
        _reset_managers_after_fork()
    normalized_path = os.path.abspath(os.fspath(path))
    with _MANAGERS_GUARD:
        manager = _MANAGERS.get(normalized_path)
        if manager is None:
            manager = MaterializationRootLock(normalized_path)
            _MANAGERS[normalized_path] = manager
        return manager


@contextmanager
def locked_materialization_root(
    path: str | os.PathLike[str],
) -> Iterator[int]:
    """Lock ``path`` and yield its shared, thread-owned directory FD."""

    with get_materialization_root_lock(path).locked() as descriptor:
        yield descriptor
