from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import fcntl
import multiprocessing
import os
from pathlib import Path
import select
import threading

import pytest

from openevo.evolution import materialization_root_lock
from openevo.evolution.materialization_root_lock import (
    get_materialization_root_lock,
    locked_materialization_root,
)


def _lock_in_spawned_process(
    root: str,
    attempting: multiprocessing.synchronize.Event,
    acquired: multiprocessing.synchronize.Event,
) -> None:
    attempting.set()
    with locked_materialization_root(root):
        acquired.set()


def _fd_count() -> int:
    return len(os.listdir("/proc/self/fd"))


def test_same_absolute_path_shares_manager(tmp_path: Path) -> None:
    root = tmp_path / "materializations"
    root.mkdir()

    first = get_materialization_root_lock(root)
    second = get_materialization_root_lock(root.parent / "." / root.name)

    assert first is second


def test_same_thread_reentry_reuses_fd_until_outermost_exit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "materializations"
    root.mkdir()
    real_flock = materialization_root_lock.fcntl.flock
    flock_operations: list[int] = []

    def tracked_flock(descriptor: int, operation: int) -> None:
        flock_operations.append(operation)
        real_flock(descriptor, operation)

    monkeypatch.setattr(materialization_root_lock.fcntl, "flock", tracked_flock)

    with locked_materialization_root(root) as outer_descriptor:
        opened = os.fstat(outer_descriptor)
        with locked_materialization_root(root) as inner_descriptor:
            assert inner_descriptor == outer_descriptor
            assert os.fstat(inner_descriptor) == opened
        assert os.fstat(outer_descriptor) == opened
        assert flock_operations == [fcntl.LOCK_EX]

    with pytest.raises(OSError):
        os.fstat(outer_descriptor)
    assert flock_operations == [fcntl.LOCK_EX, fcntl.LOCK_UN]


def test_different_threads_are_serialized_and_get_a_live_owned_fd(tmp_path: Path) -> None:
    root = tmp_path / "materializations"
    root.mkdir()
    manager = get_materialization_root_lock(root)
    attempting = threading.Event()
    acquired = threading.Event()

    def acquire_in_other_thread() -> tuple[int, int]:
        attempting.set()
        with manager.locked() as descriptor:
            acquired.set()
            return threading.get_ident(), os.fstat(descriptor).st_ino

    with ThreadPoolExecutor(max_workers=1) as executor:
        with manager.locked() as owner_descriptor:
            owner_thread = threading.get_ident()
            owner_inode = os.fstat(owner_descriptor).st_ino
            future = executor.submit(acquire_in_other_thread)
            assert attempting.wait(timeout=5)
            assert not acquired.wait(timeout=0.2)
            assert os.fstat(owner_descriptor).st_ino == owner_inode

        other_thread, other_inode = future.result(timeout=5)

    assert other_thread != owner_thread
    assert other_inode == owner_inode


def test_different_processes_are_serialized_by_flock(tmp_path: Path) -> None:
    root = tmp_path / "materializations"
    root.mkdir()
    process_context = multiprocessing.get_context("spawn")
    attempting = process_context.Event()
    acquired = process_context.Event()
    process = process_context.Process(
        target=_lock_in_spawned_process,
        args=(os.fspath(root), attempting, acquired),
    )

    with locked_materialization_root(root):
        process.start()
        assert attempting.wait(timeout=5)
        assert not acquired.wait(timeout=0.2)

    assert acquired.wait(timeout=5)
    process.join(timeout=5)
    if process.is_alive():
        process.terminate()
        process.join(timeout=5)
        pytest.fail("spawned lock contender did not terminate")
    assert process.exitcode == 0


def test_forked_child_does_not_treat_inherited_thread_state_as_reentry(
    tmp_path: Path,
) -> None:
    root = tmp_path / "materializations"
    root.mkdir()
    manager = get_materialization_root_lock(root)
    read_fd, write_fd = os.pipe()
    child_pid: int | None = None

    try:
        with manager.locked():
            child_pid = os.fork()
            if child_pid == 0:
                os.close(read_fd)
                try:
                    os.write(write_fd, b"A")
                    with manager.locked():
                        os.write(write_fd, b"L")
                except BaseException:
                    os.write(write_fd, b"E")
                    os._exit(1)
                finally:
                    os.close(write_fd)
                os._exit(0)

            os.close(write_fd)
            write_fd = -1
            ready, _, _ = select.select([read_fd], [], [], 5)
            assert ready and os.read(read_fd, 1) == b"A"
            ready, _, _ = select.select([read_fd], [], [], 0.2)
            assert not ready

        ready, _, _ = select.select([read_fd], [], [], 5)
        assert ready and os.read(read_fd, 1) == b"L"
    finally:
        if write_fd >= 0:
            os.close(write_fd)
        os.close(read_fd)
        if child_pid not in (None, 0):
            waited_pid, status = os.waitpid(child_pid, 0)
            assert waited_pid == child_pid
            assert os.waitstatus_to_exitcode(status) == 0


def test_lock_opens_a_no_follow_directory_descriptor(tmp_path: Path) -> None:
    root = tmp_path / "materializations"
    root.mkdir()
    alias = tmp_path / "alias"
    alias.symlink_to(root, target_is_directory=True)

    with locked_materialization_root(root) as descriptor:
        assert os.path.samestat(os.fstat(descriptor), root.stat())

    with pytest.raises(OSError):
        with locked_materialization_root(alias):
            pytest.fail("a symlink must not be opened as the lock root")


def test_body_exception_releases_lock_and_closes_fd(tmp_path: Path) -> None:
    root = tmp_path / "materializations"
    root.mkdir()
    before = _fd_count()

    with pytest.raises(RuntimeError, match="injected body failure"):
        with locked_materialization_root(root) as descriptor:
            raise RuntimeError("injected body failure")

    assert _fd_count() == before
    with pytest.raises(OSError):
        os.fstat(descriptor)
    with locked_materialization_root(root):
        pass


def test_flock_failure_closes_opened_fd(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "materializations"
    root.mkdir()
    before = _fd_count()

    def fail_flock(_descriptor: int, operation: int) -> None:
        assert operation == fcntl.LOCK_EX
        raise OSError("injected flock failure")

    monkeypatch.setattr(materialization_root_lock.fcntl, "flock", fail_flock)
    with pytest.raises(OSError, match="injected flock failure"):
        with locked_materialization_root(root):
            pytest.fail("failed flock must not yield")

    assert _fd_count() == before


def test_unlock_failure_still_closes_fd(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "materializations"
    root.mkdir()
    before = _fd_count()
    real_flock = materialization_root_lock.fcntl.flock

    def fail_unlock(descriptor: int, operation: int) -> None:
        if operation == fcntl.LOCK_UN:
            raise OSError("injected unlock failure")
        real_flock(descriptor, operation)

    monkeypatch.setattr(materialization_root_lock.fcntl, "flock", fail_unlock)
    with pytest.raises(OSError, match="injected unlock failure"):
        with locked_materialization_root(root) as descriptor:
            assert os.fstat(descriptor).st_ino == root.stat().st_ino

    assert _fd_count() == before
    with pytest.raises(OSError):
        os.fstat(descriptor)
