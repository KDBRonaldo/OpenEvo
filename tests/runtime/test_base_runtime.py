from __future__ import annotations

import asyncio
import ctypes
import errno
import hashlib
import inspect
import os
import select
import sys
import threading
import time
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

import openevo.runtime as runtime_api
from openevo.runtime import base as runtime_base
from openevo.runtime import factory as runtime_factory
from openevo.runtime.apptainer import ApptainerRuntime
from openevo.runtime.base import (
    BaseRuntime,
    RuntimePathSecurityError,
    RuntimeReadbackBudget,
)
from openevo.runtime.docker import DockerRuntime
from openevo.runtime.docker_host import DOCKER_EXECUTABLE_PATH
from openevo.runtime.managed import MANAGED_RUNTIME_IMAGES
from openevo.runtime.models import ExecResult, RuntimeSpec


class ProbeRuntime(BaseRuntime):
    @property
    def runtime_id(self) -> str:
        return "probe"

    async def start(self) -> None:
        return None

    async def stop(self) -> None:
        self._destroyed = True

    async def exec(
        self,
        command: str,
        *,
        cwd: str | None = None,
        env: dict[str, str] | None = None,
        timeout_sec: float | None = None,
    ) -> ExecResult:
        del command, cwd, env, timeout_sec
        return ExecResult(return_code=0)

    async def upload_file(self, local_path: str, remote_path: str) -> None:
        del local_path, remote_path

    async def upload_dir(self, local_path: str, remote_path: str) -> None:
        del local_path, remote_path

    async def download_file(self, remote_path: str, local_path: str) -> None:
        copied = self._copy_from_bind_mount(remote_path, Path(local_path))
        assert copied is True

    async def download_dir(self, remote_path: str, local_path: str) -> None:
        copied = self._copy_from_bind_mount(remote_path, Path(local_path))
        assert copied is True


def _runtime(tmp_path: Path) -> ProbeRuntime:
    session_dir = tmp_path / "session"
    session_dir.mkdir()
    return ProbeRuntime(RuntimeSpec(image="runtime:latest"), "session", session_dir)


def _close_test_readback_mutation_descriptor() -> None:
    retained = getattr(
        runtime_base,
        "_READBACK_MUTATION_RETAINED_DESCRIPTOR",
        None,
    )
    if isinstance(retained, int) and retained >= 0:
        os.close(retained)
        runtime_base._READBACK_MUTATION_RETAINED_DESCRIPTOR = None


def _write_inotify_ignored(write_fd: int, watch: int, *, count: int = 1) -> None:
    payload = runtime_base._INOTIFY_EVENT_HEADER.pack(
        watch,
        runtime_base._IN_IGNORED,
        0,
        0,
    )
    for _ in range(count):
        os.write(write_fd, payload)


async def _compatibility_readback(
    runtime: object,
    tmp_path: Path,
    *,
    budget: RuntimeReadbackBudget,
) -> runtime_base.RuntimeReadback:
    temporary = runtime_base._create_runtime_readback_temporary_root(parent=tmp_path)
    try:
        return await runtime_base._bounded_public_runtime_readback(
            runtime,
            "/custom/evolution",
            temporary.path / "evolution",
            budget=budget,
            relative_prefix="evolution",
            temporary_root=temporary,
        )
    finally:
        await runtime_base._cleanup_runtime_readback_temporary_root(temporary)


def test_public_download_contract_remains_abstract_two_argument_none_api() -> None:
    assert BaseRuntime.download_file.__isabstractmethod__ is True
    assert BaseRuntime.download_dir.__isabstractmethod__ is True
    assert list(inspect.signature(BaseRuntime.download_file).parameters) == [
        "self",
        "remote_path",
        "local_path",
    ]
    assert list(inspect.signature(BaseRuntime.download_dir).parameters) == [
        "self",
        "remote_path",
        "local_path",
    ]
    assert DockerRuntime.download_file is not BaseRuntime.download_file
    assert DockerRuntime.download_dir is not BaseRuntime.download_dir
    assert ApptainerRuntime.download_file is not BaseRuntime.download_file
    assert ApptainerRuntime.download_dir is not BaseRuntime.download_dir
    assert (
        DockerRuntime._start_download_dir_operation
        is not BaseRuntime._start_download_dir_operation
    )
    assert (
        ApptainerRuntime._start_download_dir_operation
        is not BaseRuntime._start_download_dir_operation
    )
    assert not hasattr(runtime_api, "RuntimeDownloadOperation")
    assert not hasattr(runtime_api, "RuntimeReadback")
    assert not hasattr(runtime_api, "RuntimeReadbackBudget")
    assert not hasattr(runtime_api, "RuntimeReadbackFile")


@pytest.mark.asyncio
async def test_public_download_api_accepts_two_arguments_and_returns_none(
    tmp_path: Path,
) -> None:
    runtime = _runtime(tmp_path)
    source_file = runtime.session_dir / "result.txt"
    source_file.write_bytes(b"result")
    source_dir = runtime.session_dir / "results"
    source_dir.mkdir()
    (source_dir / "nested.txt").write_bytes(b"nested")
    local_file = tmp_path / "downloads" / "result.txt"
    local_dir = tmp_path / "downloads" / "results"

    file_result = await runtime.download_file(
        "/openevo/session/result.txt",
        str(local_file),
    )
    dir_result = await runtime.download_dir(
        "/openevo/session/results",
        str(local_dir),
    )

    assert file_result is None
    assert dir_result is None
    assert local_file.read_bytes() == b"result"
    assert (local_dir / "nested.txt").read_bytes() == b"nested"


@pytest.mark.asyncio
async def test_compatibility_readback_accepts_257_files_with_receipt_budget(
    tmp_path: Path,
) -> None:
    class PublicDownloadRuntime:
        async def download_dir(self, _remote_path: str, local_path: str) -> None:
            target = Path(local_path)
            target.mkdir(exist_ok=True)
            for index in range(257):
                (target / f"file-{index:03d}.txt").write_bytes(b"x")

    budget = RuntimeReadbackBudget()

    readback = await _compatibility_readback(
        PublicDownloadRuntime(),
        tmp_path,
        budget=budget,
    )

    assert len(readback.files) == 257
    assert budget.files_consumed == 257
    assert budget.nodes_consumed == 514
    assert budget.bytes_consumed == 257


@pytest.mark.asyncio
async def test_compatibility_readback_rejects_more_than_4096_files(
    tmp_path: Path,
) -> None:
    class PublicDownloadRuntime:
        async def download_dir(self, _remote_path: str, local_path: str) -> None:
            target = Path(local_path)
            target.mkdir(exist_ok=True)
            for index in range(runtime_base.RUNTIME_READBACK_MAX_FILES + 1):
                (target / f"file-{index:04d}.txt").touch()

    budget = RuntimeReadbackBudget()

    with pytest.raises(RuntimePathSecurityError, match="file (quota|budget)"):
        await _compatibility_readback(
            PublicDownloadRuntime(),
            tmp_path,
            budget=budget,
        )

    assert budget.files_consumed == budget.max_files + 1
    assert budget.nodes_consumed == budget.max_nodes
    assert budget.bytes_consumed == budget.max_bytes


@pytest.mark.asyncio
async def test_compatibility_download_quota_cancels_public_download_before_completion(
    tmp_path: Path,
) -> None:
    class PublicDownloadRuntime:
        cancelled = False
        completed = False
        written = 0

        async def download_dir(self, _remote_path: str, local_path: str) -> None:
            target = Path(local_path)
            target.mkdir(exist_ok=True)
            try:
                for index in range(10_000):
                    (target / f"file-{index:05d}.txt").touch()
                    self.written += 1
                    if index % 100 == 0:
                        await asyncio.sleep(0.001)
            except asyncio.CancelledError:
                self.cancelled = True
                raise
            self.completed = True

        def _start_download_dir_operation(
            self, remote_path: str, local_path: str
        ) -> runtime_base.RuntimeDownloadOperation:
            return runtime_base.RuntimeDownloadOperation(
                self.download_dir(remote_path, local_path)
            )

    budget = RuntimeReadbackBudget()
    runtime = PublicDownloadRuntime()

    with pytest.raises(RuntimePathSecurityError, match="file (quota|budget)"):
        await _compatibility_readback(
            runtime,
            tmp_path,
            budget=budget,
        )

    assert runtime.cancelled is True
    assert runtime.completed is False
    assert runtime.written < 10_000
    assert budget.files_consumed > budget.max_files
    assert budget.nodes_consumed == budget.max_nodes
    assert budget.bytes_consumed == budget.max_bytes


@pytest.mark.asyncio
async def test_compatibility_readback_rejects_more_than_64_mib(
    tmp_path: Path,
) -> None:
    class PublicDownloadRuntime:
        async def download_dir(self, _remote_path: str, local_path: str) -> None:
            target = Path(local_path)
            target.mkdir(exist_ok=True)
            oversized = target / "oversized.bin"
            with oversized.open("wb") as stream:
                stream.truncate(runtime_base.RUNTIME_READBACK_MAX_BYTES + 1)

    budget = RuntimeReadbackBudget()

    with pytest.raises(RuntimePathSecurityError, match="byte (quota|budget)"):
        await _compatibility_readback(
            PublicDownloadRuntime(),
            tmp_path,
            budget=budget,
        )

    assert budget.files_consumed >= budget.max_files
    assert budget.nodes_consumed >= budget.max_nodes
    assert budget.bytes_consumed >= budget.max_bytes


@pytest.mark.asyncio
async def test_compatibility_download_accounts_create_unlink_churn(
    tmp_path: Path,
) -> None:
    class ChurningRuntime:
        attempted = 0

        async def download_dir(self, _remote_path: str, local_path: str) -> None:
            target = Path(local_path)
            target.mkdir(exist_ok=True)
            await asyncio.sleep(0.05)
            churn = target / "churn.tmp"
            for _index in range(5_000):
                churn.touch()
                churn.unlink()
                self.attempted += 1

    runtime = ChurningRuntime()
    budget = RuntimeReadbackBudget()

    with pytest.raises(RuntimePathSecurityError):
        await _compatibility_readback(runtime, tmp_path, budget=budget)

    assert runtime.attempted == 5_000
    assert budget.files_consumed >= budget.max_files
    assert budget.nodes_consumed >= budget.max_nodes
    assert budget.bytes_consumed >= budget.max_bytes


@pytest.mark.asyncio
async def test_compatibility_download_accounts_write_unlink_byte_churn(
    tmp_path: Path,
) -> None:
    class ChurningRuntime:
        async def download_dir(self, _remote_path: str, local_path: str) -> None:
            target = Path(local_path)
            target.mkdir(exist_ok=True)
            await asyncio.sleep(0.05)
            for index in range(65):
                churn = target / f"churn-{index:02d}.bin"
                with churn.open("wb") as stream:
                    stream.truncate(1024 * 1024)
                await asyncio.sleep(0.01)
                churn.unlink()

        def _start_download_dir_operation(
            self, remote_path: str, local_path: str
        ) -> runtime_base.RuntimeDownloadOperation:
            return runtime_base.RuntimeDownloadOperation(
                self.download_dir(remote_path, local_path)
            )

    budget = RuntimeReadbackBudget()

    # The inotify consumer can either observe enough close-write evidence to
    # exhaust the byte budget or observe the subsequent unlink first under
    # scheduler pressure. The latter is an equally closed security outcome:
    # the implementation refuses work whose byte cost became unobservable.
    with pytest.raises(
        RuntimePathSecurityError,
        match="(byte budget|byte work became unobservable)",
    ):
        await _compatibility_readback(ChurningRuntime(), tmp_path, budget=budget)

    assert budget.files_consumed >= budget.max_files
    assert budget.nodes_consumed >= budget.max_nodes
    assert budget.bytes_consumed >= budget.max_bytes


@pytest.mark.asyncio
async def test_compatibility_download_rejects_new_directory_watch_window(
    tmp_path: Path,
) -> None:
    class NestedChurningRuntime:
        async def download_dir(self, _remote_path: str, local_path: str) -> None:
            target = Path(local_path)
            target.mkdir(exist_ok=True)
            nested = target / "created-during-download"
            nested.mkdir()
            churn = nested / "gone-before-watch.tmp"
            for _index in range(5_000):
                churn.touch()
                churn.unlink()

    budget = RuntimeReadbackBudget()

    with pytest.raises(RuntimePathSecurityError, match="directory.*window"):
        await _compatibility_readback(NestedChurningRuntime(), tmp_path, budget=budget)

    assert budget.files_consumed >= budget.max_files
    assert budget.nodes_consumed >= budget.max_nodes
    assert budget.bytes_consumed >= budget.max_bytes


@pytest.mark.asyncio
async def test_compatibility_download_failure_exhausts_shared_budget(
    tmp_path: Path,
) -> None:
    class FailingRuntime:
        async def download_dir(self, _remote_path: str, local_path: str) -> None:
            target = Path(local_path)
            target.mkdir(exist_ok=True)
            (target / "partial.txt").write_text("partial", encoding="utf-8")
            raise RuntimeError("injected public download failure")

    budget = RuntimeReadbackBudget()

    with pytest.raises(RuntimeError, match="injected public download failure"):
        await _compatibility_readback(FailingRuntime(), tmp_path, budget=budget)

    assert budget.files_consumed >= budget.max_files
    assert budget.nodes_consumed >= budget.max_nodes
    assert budget.bytes_consumed >= budget.max_bytes


@pytest.mark.asyncio
async def test_compatibility_monitor_oserror_exhausts_shared_budget(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class PublicDownloadRuntime:
        async def download_dir(self, _remote_path: str, local_path: str) -> None:
            target = Path(local_path)
            target.mkdir(exist_ok=True)
            (target / "result.txt").write_text("result", encoding="utf-8")

    def fail_quota_inspection(*_args: object, **_kwargs: object) -> None:
        raise OSError("injected quota inspection failure")

    monkeypatch.setattr(
        runtime_base,
        "_require_runtime_download_quota",
        fail_quota_inspection,
    )
    budget = RuntimeReadbackBudget()

    with pytest.raises(RuntimePathSecurityError, match="monitor failed closed"):
        await _compatibility_readback(PublicDownloadRuntime(), tmp_path, budget=budget)

    assert budget.files_consumed >= budget.max_files
    assert budget.nodes_consumed >= budget.max_nodes
    assert budget.bytes_consumed >= budget.max_bytes


@pytest.mark.asyncio
async def test_compatibility_download_refusing_cancellation_has_hard_join_bound(
    tmp_path: Path,
) -> None:
    class RefusingRuntime:
        started = asyncio.Event()
        release = threading.Event()
        cancellation_seen = False

        async def download_dir(self, _remote_path: str, local_path: str) -> None:
            target = Path(local_path)
            target.mkdir(exist_ok=True)
            (target / "result.txt").write_text("result", encoding="utf-8")
            self.started.set()
            while not self.release.is_set():
                try:
                    await asyncio.sleep(0.01)
                except asyncio.CancelledError:
                    self.cancellation_seen = True

    runtime = RefusingRuntime()
    budget = RuntimeReadbackBudget()
    task = asyncio.create_task(_compatibility_readback(runtime, tmp_path, budget=budget))
    await asyncio.wait_for(runtime.started.wait(), timeout=1)
    started = asyncio.get_running_loop().time()
    task.cancel()

    with pytest.raises(RuntimePathSecurityError, match="hard join bound"):
        await asyncio.wait_for(task, timeout=2)

    try:
        assert asyncio.get_running_loop().time() - started < 1.8
        assert runtime.cancellation_seen is True
        assert list(tmp_path.glob(".openevo-readback-quarantine-*"))
        assert budget.files_consumed >= budget.max_files
        assert budget.nodes_consumed >= budget.max_nodes
        assert budget.bytes_consumed >= budget.max_bytes
    finally:
        runtime.release.set()
        await asyncio.sleep(0.1)
        assert list(tmp_path.glob(".openevo-readback-quarantine-*"))


@pytest.mark.asyncio
async def test_cancelled_to_thread_download_is_permanently_isolated(
    tmp_path: Path,
) -> None:
    class ThreadedRuntime:
        started = threading.Event()
        release = threading.Event()

        @classmethod
        def blocking_download(cls, local_path: str) -> None:
            target = Path(local_path)
            target.mkdir(exist_ok=True)
            (target / "partial.txt").write_text("partial", encoding="utf-8")
            cls.started.set()
            cls.release.wait(5)

        async def download_dir(self, _remote_path: str, local_path: str) -> None:
            await asyncio.to_thread(self.blocking_download, local_path)

    runtime = ThreadedRuntime()
    budget = RuntimeReadbackBudget()
    task = asyncio.create_task(_compatibility_readback(runtime, tmp_path, budget=budget))
    deadline = asyncio.get_running_loop().time() + 1
    while not runtime.started.is_set():
        if asyncio.get_running_loop().time() >= deadline:
            pytest.fail("threaded downloader did not start")
        await asyncio.sleep(0.01)
    task.cancel()

    with pytest.raises(RuntimePathSecurityError, match="hard join bound"):
        await asyncio.wait_for(task, timeout=2)

    quarantines = list(tmp_path.glob(".openevo-readback-quarantine-*"))
    assert len(quarantines) == 1
    runtime.release.set()
    await asyncio.sleep(0.1)
    assert quarantines[0].exists()
    assert budget.files_consumed >= budget.max_files
    assert budget.nodes_consumed >= budget.max_nodes
    assert budget.bytes_consumed >= budget.max_bytes


@pytest.mark.asyncio
async def test_docker_public_download_uses_docker_cp_outside_session_bind(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session_dir = tmp_path / "docker-session"
    session_dir.mkdir()
    runtime = DockerRuntime(
        RuntimeSpec(image="runtime:latest"),
        "docker-download",
        session_dir,
    )
    runtime._container_id = "a" * 64
    runtime._ownership_state = "verified"
    monkeypatch.setattr(runtime, "_copy_from_bind_mount", lambda *_args: False)
    run_command = AsyncMock(return_value=(0, None, None))
    monkeypatch.setattr(runtime, "_run_local_command", run_command)
    local_file = tmp_path / "docker-downloads" / "result.txt"
    local_dir = tmp_path / "docker-downloads" / "results"

    assert await runtime.download_file("/workspace/result.txt", str(local_file)) is None
    assert await runtime.download_dir("/workspace/results", str(local_dir)) is None

    assert run_command.await_args_list[0].args == (
        DOCKER_EXECUTABLE_PATH,
        "cp",
        f"{'a' * 64}:/workspace/result.txt",
        str(local_file),
    )
    assert run_command.await_args_list[1].args == (
        DOCKER_EXECUTABLE_PATH,
        "cp",
        f"{'a' * 64}:/workspace/results",
        str(local_dir),
    )


@pytest.mark.asyncio
async def test_apptainer_public_download_uses_tar_outside_session_bind(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session_dir = tmp_path / "apptainer-session"
    session_dir.mkdir()
    runtime = ApptainerRuntime(
        RuntimeSpec(backend="apptainer", image="runtime.sif"),
        "apptainer-download",
        session_dir,
    )
    monkeypatch.setattr(runtime, "_copy_from_bind_mount", lambda *_args: False)
    run_command = AsyncMock(return_value=(0, None, None))
    monkeypatch.setattr(runtime, "_run_local_command", run_command)
    local_file = tmp_path / "apptainer-downloads" / "result.txt"
    local_dir = tmp_path / "apptainer-downloads" / "results"

    assert await runtime.download_file("/workspace/result.txt", str(local_file)) is None
    assert await runtime.download_dir("/workspace/results", str(local_dir)) is None

    file_command = run_command.await_args_list[0].args
    dir_command = run_command.await_args_list[1].args
    assert file_command[:2] == ("bash", "-c")
    assert "tar -cf - -C /workspace result.txt" in file_command[2]
    assert f"tar -xf - -C {local_file.parent}" in file_command[2]
    assert dir_command[:2] == ("bash", "-c")
    assert "tar -cf - -C /workspace/results ." in dir_command[2]
    assert f"tar -xf - -C {local_dir}" in dir_command[2]


def test_runtime_factory_admits_plugin_download_override(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class ReadbackOverrideRuntime(ProbeRuntime):
        async def download_dir(
            self,
            remote_path: str,
            local_path: str,
        ) -> None:
            del remote_path, local_path

    monkeypatch.setattr(
        runtime_factory,
        "_import_runtime_class",
        lambda _import_path: ReadbackOverrideRuntime,
    )

    runtime = runtime_factory.create_runtime(
        RuntimeSpec(
            image="runtime:latest",
            import_path="tests.runtime:ReadbackOverrideRuntime",
        ),
        "plugin-session",
        tmp_path / "plugin-session",
    )

    assert type(runtime) is ReadbackOverrideRuntime
    assert runtime.download_dir.__func__ is ReadbackOverrideRuntime.download_dir


@pytest.mark.parametrize(
    "runtime_path",
    [
        "/openevo/session/../outside",
        "/openevo/session/work/../../outside",
        "/openevo/session/./workspace",
        "/openevo/session//workspace",
        "/openevo/session/workspace/",
    ],
)
def test_resolve_host_path_rejects_noncanonical_session_paths(
    tmp_path: Path,
    runtime_path: str,
) -> None:
    runtime = _runtime(tmp_path)

    with pytest.raises(RuntimePathSecurityError):
        runtime.resolve_host_path(runtime_path)


@pytest.mark.parametrize("leaf", [False, True])
def test_resolve_host_path_rejects_parent_and_final_symlinks(
    tmp_path: Path,
    leaf: bool,
) -> None:
    runtime = _runtime(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    if leaf:
        (runtime.session_dir / "workspace").symlink_to(
            outside / "target",
        )
        runtime_path = "/openevo/session/workspace"
    else:
        (runtime.session_dir / "workspace").symlink_to(
            outside,
            target_is_directory=True,
        )
        runtime_path = "/openevo/session/workspace/target"

    with pytest.raises(RuntimePathSecurityError):
        runtime.resolve_host_path(runtime_path)


def test_copy_to_bind_mount_rejects_final_symlink_without_overwriting_outside(
    tmp_path: Path,
) -> None:
    runtime = _runtime(tmp_path)
    source = tmp_path / "source.txt"
    source.write_text("trusted source", encoding="utf-8")
    outside = tmp_path / "outside.txt"
    outside.write_text("outside remains", encoding="utf-8")
    (runtime.session_dir / "target.txt").symlink_to(outside)

    with pytest.raises(RuntimePathSecurityError):
        runtime._copy_to_bind_mount(
            str(source),
            "/openevo/session/target.txt",
        )

    assert outside.read_text(encoding="utf-8") == "outside remains"


@pytest.mark.parametrize(
    "pin_factory",
    [
        lambda descriptors: runtime_base._DirectoryPin(
            path=Path("/"),
            parts=(),
            descriptors=descriptors,
            identities=(),
        ),
        lambda descriptors: runtime_base._RelativeDirectoryPin(
            names=(),
            descriptors=descriptors,
            identities=(),
        ),
    ],
)
def test_directory_pin_close_attempts_every_descriptor_after_first_failure(
    monkeypatch: pytest.MonkeyPatch,
    pin_factory,
) -> None:
    descriptors = [101, 102, 103]
    pin = pin_factory(descriptors)
    closed: list[int] = []

    def fail_first_close(descriptor: int) -> None:
        closed.append(descriptor)
        if descriptor == 103:
            raise OSError(errno.EIO, "injected close failure")

    monkeypatch.setattr(runtime_base.os, "close", fail_first_close)

    with pytest.raises(OSError, match="injected close failure"):
        pin.close()

    assert closed == [103, 102, 101]
    assert descriptors == []


def test_copy_readback_regular_file_closes_source_after_target_close_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source_root = tmp_path / "source"
    target_root = tmp_path / "target"
    source_root.mkdir()
    target_root.mkdir()
    source_path = source_root / "memory.md"
    source_path.write_bytes(b"memory")
    source_parent_fd = os.open(source_root, os.O_RDONLY | os.O_CLOEXEC | os.O_DIRECTORY)
    target_parent_fd = os.open(target_root, os.O_RDONLY | os.O_CLOEXEC | os.O_DIRECTORY)
    source_stat = os.stat(source_path, follow_symlinks=False)
    source = runtime_base._ReadbackSourceEntry(
        name="memory.md",
        relative_path="memory.md",
        stat=source_stat,
        is_directory=False,
    )
    original_open = runtime_base.os.open
    original_close = runtime_base.os.close
    opened_source: list[int] = []
    opened_target: list[int] = []

    def record_open(path, flags, *args, **kwargs):
        descriptor = original_open(path, flags, *args, **kwargs)
        if path == "memory.md":
            if kwargs.get("dir_fd") == source_parent_fd:
                opened_source.append(descriptor)
            elif kwargs.get("dir_fd") == target_parent_fd:
                opened_target.append(descriptor)
        return descriptor

    target_close_failed = False

    def fail_target_close(descriptor: int) -> None:
        nonlocal target_close_failed
        if opened_target and descriptor == opened_target[-1] and not target_close_failed:
            target_close_failed = True
            raise OSError(errno.EIO, "injected target close failure")
        original_close(descriptor)

    monkeypatch.setattr(runtime_base.os, "open", record_open)
    monkeypatch.setattr(runtime_base.os, "close", fail_target_close)

    try:
        with pytest.raises(OSError, match="injected target close failure"):
            runtime_base._copy_readback_regular_file(
                source_parent_fd,
                source,
                target_parent_fd,
                budget=RuntimeReadbackBudget(),
                cancellation=threading.Event(),
            )

        assert opened_source
        with pytest.raises(OSError, match="Bad file descriptor"):
            os.fstat(opened_source[-1])
    finally:
        for descriptor in opened_target:
            try:
                original_close(descriptor)
            except OSError:
                pass
        original_close(source_parent_fd)
        original_close(target_parent_fd)


def test_copy_readback_regular_file_preserves_body_error_when_cleanup_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source_root = tmp_path / "source"
    target_root = tmp_path / "target"
    source_root.mkdir()
    target_root.mkdir()
    source_path = source_root / "memory.md"
    source_path.write_bytes(b"memory")
    source_parent_fd = os.open(source_root, os.O_RDONLY | os.O_CLOEXEC | os.O_DIRECTORY)
    target_parent_fd = os.open(target_root, os.O_RDONLY | os.O_CLOEXEC | os.O_DIRECTORY)
    source_stat = os.stat(source_path, follow_symlinks=False)
    source = runtime_base._ReadbackSourceEntry(
        name="memory.md",
        relative_path="memory.md",
        stat=source_stat,
        is_directory=False,
    )
    original_open = runtime_base.os.open
    original_close = runtime_base.os.close
    opened_source: list[int] = []
    opened_target: list[int] = []

    def record_open(path, flags, *args, **kwargs):
        descriptor = original_open(path, flags, *args, **kwargs)
        if path == "memory.md":
            if kwargs.get("dir_fd") == source_parent_fd:
                opened_source.append(descriptor)
            elif kwargs.get("dir_fd") == target_parent_fd:
                opened_target.append(descriptor)
        return descriptor

    target_close_failed = False

    def fail_target_close(descriptor: int) -> None:
        nonlocal target_close_failed
        if opened_target and descriptor == opened_target[-1] and not target_close_failed:
            target_close_failed = True
            raise OSError(errno.EIO, "injected target close failure")
        original_close(descriptor)

    def fail_body(*_args, **_kwargs):
        raise RuntimePathSecurityError("injected body failure")

    monkeypatch.setattr(runtime_base.os, "open", record_open)
    monkeypatch.setattr(runtime_base.os, "close", fail_target_close)
    monkeypatch.setattr(runtime_base.os, "pread", fail_body)

    try:
        with pytest.raises(RuntimePathSecurityError, match="injected body failure") as captured:
            runtime_base._copy_readback_regular_file(
                source_parent_fd,
                source,
                target_parent_fd,
                budget=RuntimeReadbackBudget(),
                cancellation=threading.Event(),
            )

        assert "injected target close failure" in str(
            getattr(captured.value, "cleanup_error", "")
        )
        assert opened_source
        with pytest.raises(OSError, match="Bad file descriptor"):
            os.fstat(opened_source[-1])
    finally:
        for descriptor in opened_target:
            try:
                original_close(descriptor)
            except OSError:
                pass
        original_close(source_parent_fd)
        original_close(target_parent_fd)


@pytest.mark.parametrize("body_failure", [False, True])
def test_copy_readback_directory_closes_child_source_and_preserves_body_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    body_failure: bool,
) -> None:
    source_root = tmp_path / "source"
    target_root = tmp_path / "target"
    (source_root / "nested").mkdir(parents=True)
    target_root.mkdir()
    source_fd = os.open(source_root, os.O_RDONLY | os.O_CLOEXEC | os.O_DIRECTORY)
    target_fd = os.open(target_root, os.O_RDONLY | os.O_CLOEXEC | os.O_DIRECTORY)
    original_open = runtime_base.os.open
    original_close = runtime_base.os.close
    original_fsync = runtime_base.os.fsync
    opened_source: list[int] = []
    opened_target: list[int] = []

    def record_open(path, flags, *args, **kwargs):
        descriptor = original_open(path, flags, *args, **kwargs)
        if path == "nested":
            if kwargs.get("dir_fd") == source_fd:
                opened_source.append(descriptor)
            elif kwargs.get("dir_fd") == target_fd:
                opened_target.append(descriptor)
        return descriptor

    target_close_failed = False

    def fail_target_close(descriptor: int) -> None:
        nonlocal target_close_failed
        if opened_target and descriptor == opened_target[-1] and not target_close_failed:
            target_close_failed = True
            raise OSError(errno.EIO, "injected target close failure")
        original_close(descriptor)

    def maybe_fail_body(descriptor: int) -> None:
        if body_failure and opened_target and descriptor == opened_target[-1]:
            raise RuntimePathSecurityError("injected body failure")
        original_fsync(descriptor)

    class MutationAuthority:
        def add(self, _directory_fd: int) -> None:
            return None

    monkeypatch.setattr(runtime_base.os, "open", record_open)
    monkeypatch.setattr(runtime_base.os, "close", fail_target_close)
    monkeypatch.setattr(runtime_base.os, "fsync", maybe_fail_body)

    try:
        if body_failure:
            with pytest.raises(
                RuntimePathSecurityError,
                match="injected body failure",
            ) as captured:
                runtime_base._copy_readback_directory(
                    source_fd,
                    target_fd,
                    relative_prefix="evolution",
                    expected_owner=os.geteuid(),
                    budget=RuntimeReadbackBudget(),
                    cancellation=threading.Event(),
                    mutation_authority=MutationAuthority(),
                    depth=0,
                )
            assert "injected target close failure" in str(
                getattr(captured.value, "cleanup_error", "")
            )
        else:
            with pytest.raises(OSError, match="injected target close failure"):
                runtime_base._copy_readback_directory(
                    source_fd,
                    target_fd,
                    relative_prefix="evolution",
                    expected_owner=os.geteuid(),
                    budget=RuntimeReadbackBudget(),
                    cancellation=threading.Event(),
                    mutation_authority=MutationAuthority(),
                    depth=0,
                )

        assert opened_source
        with pytest.raises(OSError, match="Bad file descriptor"):
            os.fstat(opened_source[-1])
    finally:
        for descriptor in opened_target:
            try:
                original_close(descriptor)
            except OSError:
                pass
        original_close(source_fd)
        original_close(target_fd)


def test_readback_mutation_authority_retains_one_inotify_instance(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    read_fd, write_fd = os.pipe()
    os.set_blocking(read_fd, False)
    init_calls = 0
    next_watch = 0
    removed_watches: list[int] = []

    class FakeCall:
        argtypes: object = None
        restype: object = None

        def __init__(self, call):
            self._call = call

        def __call__(self, *args):
            return self._call(*args)

    def init(_flags: int) -> int:
        nonlocal init_calls
        init_calls += 1
        if init_calls == 1:
            return os.dup(read_fd)
        ctypes.set_errno(errno.EMFILE)
        return -1

    def add_watch(_descriptor: int, _path: bytes, _mask: int) -> int:
        nonlocal next_watch
        next_watch += 1
        return next_watch

    def remove_watch(_descriptor: int, watch: int) -> int:
        removed_watches.append(watch)
        _write_inotify_ignored(write_fd, watch)
        return 0

    class FakeLibc:
        inotify_init1 = FakeCall(init)
        inotify_add_watch = FakeCall(add_watch)
        inotify_rm_watch = FakeCall(remove_watch)

    monkeypatch.setattr(runtime_base.sys, "platform", "linux")
    monkeypatch.setattr(runtime_base.ctypes, "CDLL", lambda *_args, **_kwargs: FakeLibc())
    monkeypatch.setattr(
        runtime_base,
        "_READBACK_MUTATION_RETAINED_DESCRIPTOR",
        None,
        raising=False,
    )
    monkeypatch.setattr(
        runtime_base,
        "_READBACK_MUTATION_RETAINED_PID",
        None,
        raising=False,
    )

    try:
        first = runtime_base._ReadbackMutationAuthority()
        first.add(read_fd)
        first.close()
        second = runtime_base._ReadbackMutationAuthority()
        second.add(read_fd)
        second.close()

        assert init_calls == 1
        assert removed_watches == [1, 2]
    finally:
        _close_test_readback_mutation_descriptor()
        os.close(read_fd)
        os.close(write_fd)


@pytest.mark.parametrize(
    ("failed_call", "error", "expected"),
    [
        ("init", errno.EMFILE, "inotify_init1:EMFILE"),
        ("add", errno.ENOSPC, "inotify_add_watch:ENOSPC"),
    ],
)
def test_readback_mutation_authority_classifies_syscall_failure_without_path(
    monkeypatch: pytest.MonkeyPatch,
    failed_call: str,
    error: int,
    expected: str,
) -> None:
    read_fd, write_fd = os.pipe()
    os.set_blocking(read_fd, False)

    class FakeCall:
        argtypes: object = None
        restype: object = None

        def __init__(self, call):
            self._call = call

        def __call__(self, *args):
            return self._call(*args)

    def init(_flags: int) -> int:
        if failed_call == "init":
            ctypes.set_errno(error)
            return -1
        return os.dup(read_fd)

    def add_watch(_descriptor: int, _path: bytes, _mask: int) -> int:
        ctypes.set_errno(error)
        return -1

    class FakeLibc:
        inotify_init1 = FakeCall(init)
        inotify_add_watch = FakeCall(add_watch)
        inotify_rm_watch = FakeCall(lambda _descriptor, _watch: 0)

    monkeypatch.setattr(runtime_base.sys, "platform", "linux")
    monkeypatch.setattr(runtime_base.ctypes, "CDLL", lambda *_args, **_kwargs: FakeLibc())
    monkeypatch.setattr(
        runtime_base,
        "_READBACK_MUTATION_RETAINED_DESCRIPTOR",
        None,
        raising=False,
    )
    monkeypatch.setattr(
        runtime_base,
        "_READBACK_MUTATION_RETAINED_PID",
        None,
        raising=False,
    )

    authority = None
    try:
        with pytest.raises(RuntimePathSecurityError, match=expected) as captured:
            authority = runtime_base._ReadbackMutationAuthority()
            authority.add(read_fd)
        assert "/proc/" not in str(captured.value)
    finally:
        if authority is not None:
            authority.close()
        _close_test_readback_mutation_descriptor()
        os.close(read_fd)
        os.close(write_fd)


@pytest.mark.skipif(not hasattr(os, "fork"), reason="requires fork")
def test_readback_mutation_authority_resets_inherited_lock_after_fork(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    read_fd, write_fd = os.pipe()
    os.set_blocking(read_fd, False)

    class FakeCall:
        argtypes: object = None
        restype: object = None

        def __init__(self, call):
            self._call = call

        def __call__(self, *args):
            return self._call(*args)

    init_calls = 0

    def init(_flags: int) -> int:
        nonlocal init_calls
        init_calls += 1
        return os.dup(read_fd)

    class FakeLibc:
        inotify_init1 = FakeCall(init)
        inotify_add_watch = FakeCall(lambda _descriptor, _path, _mask: 1)
        inotify_rm_watch = FakeCall(
            lambda _descriptor, watch: (_write_inotify_ignored(write_fd, watch), 0)[1]
        )

    monkeypatch.setattr(runtime_base.sys, "platform", "linux")
    monkeypatch.setattr(runtime_base.ctypes, "CDLL", lambda *_args, **_kwargs: FakeLibc())
    monkeypatch.setattr(
        runtime_base,
        "_READBACK_MUTATION_RETAINED_DESCRIPTOR",
        None,
        raising=False,
    )
    monkeypatch.setattr(
        runtime_base,
        "_READBACK_MUTATION_RETAINED_PID",
        None,
        raising=False,
    )

    authority = runtime_base._ReadbackMutationAuthority()
    authority.add(read_fd)
    result_read, result_write = os.pipe()
    pid = os.fork()
    if pid == 0:
        try:
            inherited_rejected = False
            try:
                authority.require_quiet()
            except RuntimePathSecurityError:
                inherited_rejected = True
            child = runtime_base._ReadbackMutationAuthority()
            child.add(read_fd)
            child.close()
            os.write(
                result_write,
                b"ok" if inherited_rejected and init_calls == 2 else b"fail",
            )
        except BaseException:
            os.write(result_write, b"fail")
        finally:
            os._exit(0)

    os.close(result_write)
    try:
        ready, _, _ = select.select([result_read], [], [], 2.0)
        assert ready, "child remained blocked on inherited readback lock"
        assert os.read(result_read, 4) == b"ok"
        _, status = os.waitpid(pid, 0)
        assert os.WIFEXITED(status) and os.WEXITSTATUS(status) == 0
        assert init_calls == 1
    finally:
        try:
            os.kill(pid, 9)
        except ProcessLookupError:
            pass
        try:
            os.waitpid(pid, 0)
        except ChildProcessError:
            pass
        authority.close()
        _close_test_readback_mutation_descriptor()
        os.close(result_read)
        os.close(read_fd)
        os.close(write_fd)


def test_readback_mutation_authority_poisoned_descriptor_is_recreated_after_cleanup_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    read_fd, write_fd = os.pipe()
    os.set_blocking(read_fd, False)
    init_calls = 0
    remove_calls = 0

    class FakeCall:
        argtypes: object = None
        restype: object = None

        def __init__(self, call):
            self._call = call

        def __call__(self, *args):
            return self._call(*args)

    def init(_flags: int) -> int:
        nonlocal init_calls
        init_calls += 1
        return os.dup(read_fd)

    def remove_watch(_descriptor: int, _watch: int) -> int:
        nonlocal remove_calls
        remove_calls += 1
        if remove_calls == 1:
            ctypes.set_errno(errno.EIO)
            return -1
        _write_inotify_ignored(write_fd, _watch)
        return 0

    class FakeLibc:
        inotify_init1 = FakeCall(init)
        inotify_add_watch = FakeCall(lambda _descriptor, _path, _mask: 1)
        inotify_rm_watch = FakeCall(remove_watch)

    monkeypatch.setattr(runtime_base.sys, "platform", "linux")
    monkeypatch.setattr(runtime_base.ctypes, "CDLL", lambda *_args, **_kwargs: FakeLibc())
    monkeypatch.setattr(
        runtime_base,
        "_READBACK_MUTATION_RETAINED_DESCRIPTOR",
        None,
        raising=False,
    )
    monkeypatch.setattr(
        runtime_base,
        "_READBACK_MUTATION_RETAINED_PID",
        None,
        raising=False,
    )

    try:
        first = runtime_base._ReadbackMutationAuthority()
        first.add(read_fd)
        with pytest.raises(RuntimePathSecurityError, match="inotify_rm_watch:EIO"):
            first.close()
        second = runtime_base._ReadbackMutationAuthority()
        second.add(read_fd)
        second.close()

        assert init_calls == 2
        assert remove_calls == 2
    finally:
        _close_test_readback_mutation_descriptor()
        os.close(read_fd)
        os.close(write_fd)


@pytest.mark.parametrize(
    ("ack_count", "expected"),
    [
        (0, "missing_ack"),
        (2, "unexpected_event"),
    ],
)
def test_readback_mutation_authority_rejects_missing_or_duplicate_teardown_ack(
    monkeypatch: pytest.MonkeyPatch,
    ack_count: int,
    expected: str,
) -> None:
    read_fd, write_fd = os.pipe()
    os.set_blocking(read_fd, False)

    class FakeCall:
        argtypes: object = None
        restype: object = None

        def __init__(self, call):
            self._call = call

        def __call__(self, *args):
            return self._call(*args)

    def remove_watch(_descriptor: int, watch: int) -> int:
        _write_inotify_ignored(write_fd, watch, count=ack_count)
        return 0

    class FakeLibc:
        inotify_init1 = FakeCall(lambda _flags: os.dup(read_fd))
        inotify_add_watch = FakeCall(lambda _descriptor, _path, _mask: 1)
        inotify_rm_watch = FakeCall(remove_watch)

    monkeypatch.setattr(runtime_base.sys, "platform", "linux")
    monkeypatch.setattr(runtime_base.ctypes, "CDLL", lambda *_args, **_kwargs: FakeLibc())
    monkeypatch.setattr(
        runtime_base,
        "_READBACK_MUTATION_RETAINED_DESCRIPTOR",
        None,
        raising=False,
    )
    monkeypatch.setattr(
        runtime_base,
        "_READBACK_MUTATION_RETAINED_PID",
        None,
        raising=False,
    )

    try:
        authority = runtime_base._ReadbackMutationAuthority()
        authority.add(read_fd)
        with pytest.raises(RuntimePathSecurityError, match=expected):
            authority.close()
    finally:
        _close_test_readback_mutation_descriptor()
        os.close(read_fd)
        os.close(write_fd)


def test_readback_mutation_authority_rejects_late_mutation_before_reuse(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    read_fd, write_fd = os.pipe()
    os.set_blocking(read_fd, False)
    init_calls = 0

    class FakeCall:
        argtypes: object = None
        restype: object = None

        def __init__(self, call):
            self._call = call

        def __call__(self, *args):
            return self._call(*args)

    def init(_flags: int) -> int:
        nonlocal init_calls
        init_calls += 1
        return os.dup(read_fd)

    class FakeLibc:
        inotify_init1 = FakeCall(init)
        inotify_add_watch = FakeCall(lambda _descriptor, _path, _mask: 1)
        inotify_rm_watch = FakeCall(
            lambda _descriptor, watch: (_write_inotify_ignored(write_fd, watch), 0)[1]
        )

    monkeypatch.setattr(runtime_base.sys, "platform", "linux")
    monkeypatch.setattr(runtime_base.ctypes, "CDLL", lambda *_args, **_kwargs: FakeLibc())
    monkeypatch.setattr(
        runtime_base,
        "_READBACK_MUTATION_RETAINED_DESCRIPTOR",
        None,
        raising=False,
    )
    monkeypatch.setattr(
        runtime_base,
        "_READBACK_MUTATION_RETAINED_PID",
        None,
        raising=False,
    )

    try:
        first = runtime_base._ReadbackMutationAuthority()
        first.add(read_fd)
        os.write(
            write_fd,
            runtime_base._INOTIFY_EVENT_HEADER.pack(
                1,
                runtime_base._IN_MODIFY,
                0,
                0,
            ),
        )
        with pytest.raises(RuntimePathSecurityError, match="changed during transfer"):
            first.close()

        second = runtime_base._ReadbackMutationAuthority()
        second.add(read_fd)
        second.close()
        assert init_calls == 2
    finally:
        _close_test_readback_mutation_descriptor()
        os.close(read_fd)
        os.close(write_fd)


def test_readback_mutation_authority_poisoned_instance_rejects_reuse(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    read_fd, write_fd = os.pipe()
    os.set_blocking(read_fd, False)
    add_calls = 0

    class FakeCall:
        argtypes: object = None
        restype: object = None

        def __init__(self, call):
            self._call = call

        def __call__(self, *args):
            return self._call(*args)

    def add_watch(_descriptor: int, _path: bytes, _mask: int) -> int:
        nonlocal add_calls
        add_calls += 1
        return 1

    class FakeLibc:
        inotify_init1 = FakeCall(lambda _flags: os.dup(read_fd))
        inotify_add_watch = FakeCall(add_watch)
        inotify_rm_watch = FakeCall(
            lambda _descriptor, watch: (_write_inotify_ignored(write_fd, watch), 0)[1]
        )

    monkeypatch.setattr(runtime_base.sys, "platform", "linux")
    monkeypatch.setattr(runtime_base.ctypes, "CDLL", lambda *_args, **_kwargs: FakeLibc())
    monkeypatch.setattr(
        runtime_base,
        "_READBACK_MUTATION_RETAINED_DESCRIPTOR",
        None,
        raising=False,
    )
    monkeypatch.setattr(
        runtime_base,
        "_READBACK_MUTATION_RETAINED_PID",
        None,
        raising=False,
    )

    try:
        authority = runtime_base._ReadbackMutationAuthority()
        authority.add(read_fd)
        os.write(
            write_fd,
            runtime_base._INOTIFY_EVENT_HEADER.pack(
                1,
                runtime_base._IN_MODIFY,
                0,
                0,
            ),
        )
        with pytest.raises(RuntimePathSecurityError, match="changed during transfer"):
            authority.require_quiet()

        def fail_read(*_args, **_kwargs):
            raise AssertionError("poisoned authority must not read its descriptor")

        monkeypatch.setattr(runtime_base.os, "read", fail_read)
        with pytest.raises(RuntimePathSecurityError, match="poisoned"):
            authority.require_quiet()
        with pytest.raises(RuntimePathSecurityError, match="poisoned"):
            authority.add(read_fd)
        assert add_calls == 1
    finally:
        authority.close()
        _close_test_readback_mutation_descriptor()
        os.close(read_fd)
        os.close(write_fd)


def test_readback_mutation_authority_lock_wait_observes_cancellation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    read_fd, write_fd = os.pipe()
    os.set_blocking(read_fd, False)

    class FakeCall:
        argtypes: object = None
        restype: object = None

        def __init__(self, call):
            self._call = call

        def __call__(self, *args):
            return self._call(*args)

    class FakeLibc:
        inotify_init1 = FakeCall(lambda _flags: os.dup(read_fd))
        inotify_add_watch = FakeCall(lambda _descriptor, _path, _mask: 1)
        inotify_rm_watch = FakeCall(
            lambda _descriptor, watch: (_write_inotify_ignored(write_fd, watch), 0)[1]
        )

    monkeypatch.setattr(runtime_base.sys, "platform", "linux")
    monkeypatch.setattr(runtime_base.ctypes, "CDLL", lambda *_args, **_kwargs: FakeLibc())
    monkeypatch.setattr(
        runtime_base,
        "_READBACK_MUTATION_RETAINED_DESCRIPTOR",
        None,
        raising=False,
    )
    monkeypatch.setattr(
        runtime_base,
        "_READBACK_MUTATION_RETAINED_PID",
        None,
        raising=False,
    )

    first = runtime_base._ReadbackMutationAuthority()
    cancellation = threading.Event()
    observed: list[BaseException] = []

    def wait_for_authority() -> None:
        try:
            runtime_base._ReadbackMutationAuthority(cancellation=cancellation)
        except BaseException as exc:
            observed.append(exc)

    worker = threading.Thread(target=wait_for_authority)
    worker.start()
    cancellation.set()
    worker.join(timeout=1.0)

    try:
        assert not worker.is_alive()
        assert len(observed) == 1
        assert isinstance(observed[0], RuntimePathSecurityError)
        assert "cancelled" in str(observed[0])
    finally:
        first.close()
        _close_test_readback_mutation_descriptor()
        os.close(read_fd)
        os.close(write_fd)


def test_scan_runtime_download_closes_target_fd_when_authority_close_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = tmp_path / "evolution"
    target.mkdir()
    (target / "memory.md").write_bytes(b"memory")
    parent_fd = os.open(tmp_path, os.O_RDONLY | os.O_CLOEXEC | os.O_DIRECTORY)
    opened_target_fds: list[int] = []
    original_open = runtime_base.os.open

    def record_open(path, flags, *args, **kwargs):
        descriptor = original_open(path, flags, *args, **kwargs)
        if path == "evolution" and kwargs.get("dir_fd") == parent_fd:
            opened_target_fds.append(descriptor)
        return descriptor

    class FailingAuthority:
        def __init__(self, *, cancellation=None):
            del cancellation

        def add(self, _directory_fd: int) -> None:
            return None

        def require_quiet(self) -> None:
            return None

        def close(self) -> None:
            raise RuntimePathSecurityError("injected close failure")

    monkeypatch.setattr(runtime_base.sys, "platform", "linux")
    monkeypatch.setattr(runtime_base.os, "open", record_open)
    monkeypatch.setattr(runtime_base, "_ReadbackMutationAuthority", FailingAuthority)
    budget = RuntimeReadbackBudget()
    accounting = runtime_base._RuntimeDownloadAccounting(
        budget,
        relative_prefix="evolution",
    )

    try:
        with pytest.raises(RuntimePathSecurityError, match="injected close failure"):
            runtime_base._scan_runtime_download_sync(
                parent_fd,
                "evolution",
                "evolution",
                runtime_base._directory_identity(os.fstat(parent_fd)),
                os.geteuid(),
                budget,
                threading.Event(),
                accounting,
            )
        assert opened_target_fds
        with pytest.raises(OSError, match="Bad file descriptor"):
            os.fstat(opened_target_fds[-1])
    finally:
        os.close(parent_fd)


def test_scan_runtime_download_preserves_body_error_when_authority_close_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = tmp_path / "evolution"
    target.mkdir()
    (target / "memory.md").write_bytes(b"memory")
    parent_fd = os.open(tmp_path, os.O_RDONLY | os.O_CLOEXEC | os.O_DIRECTORY)

    class FailingAuthority:
        def __init__(self, *, cancellation=None):
            del cancellation

        def add(self, _directory_fd: int) -> None:
            return None

        def require_quiet(self) -> None:
            return None

        def close(self) -> None:
            raise RuntimePathSecurityError("close failure")

    def fail_scan(*args, **kwargs):
        del args, kwargs
        raise RuntimePathSecurityError("body failure")

    monkeypatch.setattr(runtime_base.sys, "platform", "linux")
    monkeypatch.setattr(runtime_base, "_ReadbackMutationAuthority", FailingAuthority)
    monkeypatch.setattr(runtime_base, "_scan_runtime_download_directory", fail_scan)
    budget = RuntimeReadbackBudget()
    accounting = runtime_base._RuntimeDownloadAccounting(
        budget,
        relative_prefix="evolution",
    )

    try:
        with pytest.raises(RuntimePathSecurityError, match="body failure") as captured:
            runtime_base._scan_runtime_download_sync(
                parent_fd,
                "evolution",
                "evolution",
                runtime_base._directory_identity(os.fstat(parent_fd)),
                os.geteuid(),
                budget,
                threading.Event(),
                accounting,
            )
        assert "close failure" in str(getattr(captured.value, "cleanup_error", ""))
    finally:
        os.close(parent_fd)


@pytest.mark.asyncio
@pytest.mark.skipif(
    not sys.platform.startswith("linux"),
    reason="sealed readback requires Linux",
)
async def test_trusted_readback_close_failure_still_discards_publication_and_staging(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = _runtime(tmp_path)
    source = runtime.session_dir / "evolution"
    source.mkdir()
    (source / "memory.md").write_bytes(b"memory")
    target = tmp_path / "readback" / "evolution"
    original_close = runtime_base._ReadbackMutationAuthority.close

    def fail_after_close(authority) -> None:
        original_close(authority)
        raise RuntimePathSecurityError("injected close failure")

    monkeypatch.setattr(
        runtime_base._ReadbackMutationAuthority,
        "close",
        fail_after_close,
    )

    with pytest.raises(RuntimePathSecurityError, match="cleanup"):
        await runtime_base._sealed_session_bind_readback(
            runtime,
            "/openevo/session/evolution",
            target,
            budget=RuntimeReadbackBudget(),
            expected_directory=True,
        )

    assert not target.exists()
    assert list(target.parent.glob(".*.openevo-readback-*")) == []


@pytest.mark.asyncio
@pytest.mark.skipif(
    not sys.platform.startswith("linux"),
    reason="sealed readback requires Linux",
)
async def test_trusted_readback_late_authority_failure_discards_completed_publication(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = _runtime(tmp_path)
    source = runtime.session_dir / "evolution"
    source.mkdir()
    (source / "memory.md").write_bytes(b"memory")
    target = tmp_path / "readback" / "evolution"
    original_close = runtime_base._ReadbackMutationAuthority.close

    def fail_after_close(authority) -> None:
        original_close(authority)
        raise RuntimePathSecurityError("late mutation detected during close")

    monkeypatch.setattr(
        runtime_base._ReadbackMutationAuthority,
        "close",
        fail_after_close,
    )

    with pytest.raises(RuntimePathSecurityError, match="cleanup"):
        await runtime_base._sealed_session_bind_readback(
            runtime,
            "/openevo/session/evolution",
            target,
            budget=RuntimeReadbackBudget(),
            expected_directory=True,
        )

    assert not target.exists()


@pytest.mark.asyncio
async def test_trusted_readback_publishes_stable_private_tree_and_source_inventory(
    tmp_path: Path,
) -> None:
    runtime = _runtime(tmp_path)
    source = runtime.session_dir / "evolution"
    nested = source / "skills" / "parser"
    nested.mkdir(parents=True)
    (source / "instruction.txt").write_bytes(b"use stable context")
    (nested / "SKILL.md").write_bytes(b"stable skill")
    target = tmp_path / "readback" / "evolution"
    budget = RuntimeReadbackBudget()

    result = await runtime_base._sealed_session_bind_readback(
        runtime,
        "/openevo/session/evolution",
        target,
        budget=budget,
        expected_directory=True,
    )

    assert [(item.relative_path, item.size_bytes, item.sha256) for item in result.files] == [
        (
            "instruction.txt",
            len(b"use stable context"),
            hashlib.sha256(b"use stable context").hexdigest(),
        ),
        (
            "skills/parser/SKILL.md",
            len(b"stable skill"),
            hashlib.sha256(b"stable skill").hexdigest(),
        ),
    ]
    assert (target / "instruction.txt").read_bytes() == b"use stable context"
    assert (target / "skills" / "parser" / "SKILL.md").read_bytes() == b"stable skill"
    assert (target.stat().st_mode & 0o777) == 0o700
    assert ((target / "instruction.txt").stat().st_mode & 0o777) == 0o600
    assert budget.files_consumed == 2
    assert budget.bytes_consumed == len(b"use stable contextstable skill")
    assert budget.nodes_consumed == 8
    assert list(target.parent.glob(".*.openevo-readback-*")) == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "mutation",
    ["late_file", "remove", "rename", "add_remove_aba", "replace_restore"],
)
async def test_trusted_readback_rejects_source_tree_mutation_after_enumeration(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mutation: str,
) -> None:
    runtime = _runtime(tmp_path)
    source = runtime.session_dir / "evolution"
    source.mkdir()
    payload = source / "memory.md"
    payload.write_bytes(b"stable memory")
    target = tmp_path / "readback" / "evolution"
    original_copy = runtime_base._copy_readback_regular_file
    mutated = False

    def mutate_after_copy(*args, **kwargs):
        nonlocal mutated
        result = original_copy(*args, **kwargs)
        if not mutated:
            mutated = True
            if mutation == "late_file":
                (source / "late.md").write_bytes(b"late")
            elif mutation == "remove":
                payload.unlink()
            elif mutation == "rename":
                payload.rename(source / "renamed.md")
            elif mutation == "add_remove_aba":
                transient = source / "transient.md"
                transient.write_bytes(b"transient")
                transient.unlink()
            else:
                displaced = source / "memory.original"
                payload.rename(displaced)
                payload.write_bytes(b"replacement")
                payload.unlink()
                displaced.rename(payload)
        return result

    monkeypatch.setattr(runtime_base, "_copy_readback_regular_file", mutate_after_copy)
    budget = RuntimeReadbackBudget()

    with pytest.raises(RuntimePathSecurityError, match="changed"):
        await runtime_base._sealed_session_bind_readback(
            runtime,
            "/openevo/session/evolution",
            target,
            budget=budget,
            expected_directory=True,
        )

    assert mutated is True
    assert not target.exists()
    assert list(target.parent.glob(".*.openevo-readback-*")) == []
    assert budget.files_consumed == 1
    assert budget.bytes_consumed == len(b"stable memory")


@pytest.mark.asyncio
async def test_trusted_readback_rejects_in_place_source_mutation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = _runtime(tmp_path)
    source = runtime.session_dir / "evolution"
    source.mkdir()
    payload = source / "memory.md"
    payload.write_bytes(b"stable memory")
    target = tmp_path / "readback" / "evolution"
    original_pread = runtime_base.os.pread
    mutated = False

    def mutate_during_read(descriptor: int, size: int, offset: int) -> bytes:
        nonlocal mutated
        chunk = original_pread(descriptor, size, offset)
        if not mutated:
            mutated = True
            payload.write_bytes(b"changed bytes")
        return chunk

    monkeypatch.setattr(runtime_base.os, "pread", mutate_during_read)

    with pytest.raises(RuntimePathSecurityError, match="changed"):
        await runtime_base._sealed_session_bind_readback(
            runtime,
            "/openevo/session/evolution",
            target,
            budget=RuntimeReadbackBudget(),
            expected_directory=True,
        )

    assert mutated is True
    assert not target.exists()
    assert list(target.parent.glob(".*.openevo-readback-*")) == []


@pytest.mark.asyncio
@pytest.mark.parametrize("entry_type", ["symlink", "hardlink"])
async def test_trusted_readback_rejects_linked_source_members(
    tmp_path: Path,
    entry_type: str,
) -> None:
    runtime = _runtime(tmp_path)
    source = runtime.session_dir / "evolution"
    source.mkdir()
    outside = runtime.session_dir / "outside.md"
    outside.write_bytes(b"outside")
    member = source / "member.md"
    if entry_type == "symlink":
        member.symlink_to(outside)
    else:
        os.link(outside, member)
    target = tmp_path / "readback" / "evolution"

    with pytest.raises(RuntimePathSecurityError, match="link"):
        await runtime_base._sealed_session_bind_readback(
            runtime,
            "/openevo/session/evolution",
            target,
            budget=RuntimeReadbackBudget(),
            expected_directory=True,
        )

    assert not target.exists()


@pytest.mark.asyncio
async def test_trusted_readback_rejects_sparse_file_before_host_payload_write(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = _runtime(tmp_path)
    source = runtime.session_dir / "evolution"
    source.mkdir()
    sparse = source / "sparse.bin"
    with sparse.open("wb") as stream:
        stream.truncate(runtime_base.RUNTIME_READBACK_MAX_BYTES + 1)
    target = tmp_path / "readback" / "evolution"
    budget = RuntimeReadbackBudget()
    writes = 0
    original_pwrite = runtime_base.os.pwrite

    def record_write(*args, **kwargs):
        nonlocal writes
        writes += 1
        return original_pwrite(*args, **kwargs)

    monkeypatch.setattr(runtime_base.os, "pwrite", record_write)

    with pytest.raises(RuntimePathSecurityError, match="byte budget"):
        await runtime_base._sealed_session_bind_readback(
            runtime,
            "/openevo/session/evolution",
            target,
            budget=budget,
            expected_directory=True,
        )

    assert budget.bytes_consumed == 0
    assert writes == 0
    assert not target.exists()
    assert list(target.parent.glob(".*.openevo-readback-*")) == []


@pytest.mark.asyncio
async def test_trusted_readback_rejects_file_growth_during_stream(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = _runtime(tmp_path)
    source = runtime.session_dir / "evolution"
    source.mkdir()
    growing = source / "growing.bin"
    growing.write_bytes(b"initial")
    target = tmp_path / "readback" / "evolution"
    original_pread = runtime_base.os.pread
    grew = False

    def grow_after_read(descriptor: int, size: int, offset: int) -> bytes:
        nonlocal grew
        chunk = original_pread(descriptor, size, offset)
        if not grew:
            grew = True
            with growing.open("ab") as stream:
                stream.write(b"growth")
        return chunk

    monkeypatch.setattr(runtime_base.os, "pread", grow_after_read)
    budget = RuntimeReadbackBudget()

    with pytest.raises(RuntimePathSecurityError, match="changed"):
        await runtime_base._sealed_session_bind_readback(
            runtime,
            "/openevo/session/evolution",
            target,
            budget=budget,
            expected_directory=True,
        )

    assert grew is True
    assert budget.bytes_consumed == len(b"initial")
    assert not target.exists()


@pytest.mark.asyncio
async def test_trusted_readback_file_budget_stops_before_any_payload_publish(
    tmp_path: Path,
) -> None:
    runtime = _runtime(tmp_path)
    source = runtime.session_dir / "evolution"
    source.mkdir()
    for index in range(3):
        (source / f"{index}.txt").write_bytes(b"x")
    target = tmp_path / "readback" / "evolution"
    budget = RuntimeReadbackBudget(max_files=2)

    with pytest.raises(RuntimePathSecurityError, match="file budget"):
        await runtime_base._sealed_session_bind_readback(
            runtime,
            "/openevo/session/evolution",
            target,
            budget=budget,
            expected_directory=True,
        )

    assert budget.files_consumed == 3
    assert budget.bytes_consumed == 0
    assert not target.exists()


@pytest.mark.asyncio
async def test_trusted_readback_default_file_limit_rejects_large_inventory_before_copy(
    tmp_path: Path,
) -> None:
    runtime = _runtime(tmp_path)
    source = runtime.session_dir / "evolution"
    source.mkdir()
    for index in range(runtime_base.RUNTIME_READBACK_MAX_FILES + 1):
        (source / f"{index:04d}.txt").touch()
    target = tmp_path / "readback" / "evolution"
    budget = RuntimeReadbackBudget()

    with pytest.raises(RuntimePathSecurityError, match="file budget"):
        await runtime_base._sealed_session_bind_readback(
            runtime,
            "/openevo/session/evolution",
            target,
            budget=budget,
            expected_directory=True,
        )

    assert budget.files_consumed == runtime_base.RUNTIME_READBACK_MAX_FILES + 1
    assert budget.bytes_consumed == 0
    assert not target.exists()


@pytest.mark.asyncio
async def test_trusted_readback_node_attempt_budget_is_non_refundable(
    tmp_path: Path,
) -> None:
    runtime = _runtime(tmp_path)
    source = runtime.session_dir / "evolution"
    source.mkdir()
    (source / "a.txt").write_bytes(b"a")
    (source / "b.txt").write_bytes(b"b")
    target = tmp_path / "readback" / "evolution"
    budget = RuntimeReadbackBudget(max_nodes=2)

    with pytest.raises(RuntimePathSecurityError, match="node budget"):
        await runtime_base._sealed_session_bind_readback(
            runtime,
            "/openevo/session/evolution",
            target,
            budget=budget,
            expected_directory=True,
        )

    assert budget.nodes_consumed == 3
    assert budget.files_consumed == 2
    assert budget.bytes_consumed == 2
    assert not target.exists()


@pytest.mark.asyncio
async def test_trusted_readback_cancellation_waits_for_private_staging_cleanup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = _runtime(tmp_path)
    source = runtime.session_dir / "evolution"
    source.mkdir()
    (source / "memory.md").write_bytes(b"memory")
    target = tmp_path / "readback" / "evolution"
    original_copy = runtime_base._copy_readback_regular_file
    original_discard = runtime_base._discard_readback_staging
    started = threading.Event()
    cleanup_started = threading.Event()
    release_cleanup = threading.Event()

    def wait_for_cancel(*args, **kwargs):
        cancellation = kwargs["cancellation"]
        started.set()
        while not cancellation.is_set():
            time.sleep(0.005)
        return original_copy(*args, **kwargs)

    def delayed_discard(*args, **kwargs):
        cleanup_started.set()
        assert release_cleanup.wait(1)
        return original_discard(*args, **kwargs)

    monkeypatch.setattr(runtime_base, "_copy_readback_regular_file", wait_for_cancel)
    monkeypatch.setattr(runtime_base, "_discard_readback_staging", delayed_discard)
    task = asyncio.create_task(
        runtime_base._sealed_session_bind_readback(
            runtime,
            "/openevo/session/evolution",
            target,
            budget=RuntimeReadbackBudget(),
            expected_directory=True,
        )
    )
    assert await asyncio.to_thread(started.wait, 1)
    task.cancel()
    assert await asyncio.to_thread(cleanup_started.wait, 1)
    task.cancel()
    assert not task.done()
    release_cleanup.set()

    with pytest.raises(asyncio.CancelledError):
        await task

    assert not target.exists()
    assert list(target.parent.glob(".*.openevo-readback-*")) == []


@pytest.mark.asyncio
async def test_trusted_readback_timeout_cancels_worker_before_return(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = _runtime(tmp_path)
    source = runtime.session_dir / "evolution"
    source.mkdir()
    (source / "memory.md").write_bytes(b"memory")
    target = tmp_path / "readback" / "evolution"
    original_copy = runtime_base._copy_readback_regular_file

    def wait_for_cancel(*args, **kwargs):
        cancellation = kwargs["cancellation"]
        while not cancellation.is_set():
            time.sleep(0.005)
        return original_copy(*args, **kwargs)

    monkeypatch.setattr(runtime_base, "_copy_readback_regular_file", wait_for_cancel)

    with pytest.raises(TimeoutError):
        await asyncio.wait_for(
            runtime_base._sealed_session_bind_readback(
                runtime,
                "/openevo/session/evolution",
                target,
                budget=RuntimeReadbackBudget(),
                expected_directory=True,
            ),
            timeout=0.05,
        )

    assert not target.exists()
    assert list(target.parent.glob(".*.openevo-readback-*")) == []


@pytest.mark.asyncio
@pytest.mark.parametrize("replacement_timing", ["before_publish", "after_publish"])
async def test_trusted_readback_never_removes_replacement_target(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    replacement_timing: str,
) -> None:
    runtime = _runtime(tmp_path)
    source = runtime.session_dir / "evolution"
    source.mkdir()
    (source / "memory.md").write_bytes(b"memory")
    target = tmp_path / "readback" / "evolution"
    displaced = target.with_name("displaced-readback")
    original_rename = runtime_base._rename_readback_noreplace

    def replace_target(source_fd: int, source_name: str, target_fd: int, target_name: str) -> None:
        target.parent.mkdir(parents=True, exist_ok=True)
        if replacement_timing == "before_publish":
            target.mkdir()
            (target / "owner.txt").write_text("replacement", encoding="utf-8")
            original_rename(source_fd, source_name, target_fd, target_name)
            return
        original_rename(source_fd, source_name, target_fd, target_name)
        target.rename(displaced)
        target.mkdir()
        (target / "owner.txt").write_text("replacement", encoding="utf-8")

    monkeypatch.setattr(runtime_base, "_rename_readback_noreplace", replace_target)

    with pytest.raises(RuntimePathSecurityError):
        await runtime_base._sealed_session_bind_readback(
            runtime,
            "/openevo/session/evolution",
            target,
            budget=RuntimeReadbackBudget(),
            expected_directory=True,
        )

    assert (target / "owner.txt").read_text(encoding="utf-8") == "replacement"
    assert list(target.parent.glob(".*.openevo-readback-*")) == []


@pytest.mark.asyncio
async def test_trusted_readback_removes_its_failed_published_tree(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = _runtime(tmp_path)
    source = runtime.session_dir / "evolution"
    source.mkdir()
    (source / "memory.md").write_bytes(b"memory")
    target = tmp_path / "readback" / "evolution"
    original_verify = runtime_base._verify_readback_target_directory
    verifications = 0

    def reject_post_publish(*args, **kwargs):
        nonlocal verifications
        verifications += 1
        original_verify(*args, **kwargs)
        if verifications == 2:
            raise RuntimePathSecurityError("post-publication mutation")

    monkeypatch.setattr(
        runtime_base,
        "_verify_readback_target_directory",
        reject_post_publish,
    )

    with pytest.raises(RuntimePathSecurityError, match="post-publication"):
        await runtime_base._sealed_session_bind_readback(
            runtime,
            "/openevo/session/evolution",
            target,
            budget=RuntimeReadbackBudget(),
            expected_directory=True,
        )

    assert verifications == 2
    assert not target.exists()
    assert list(target.parent.glob(".*.openevo-readback-*")) == []


def _opened_cleanup_entry(
    parent: Path,
    name: str,
    *,
    is_directory: bool,
) -> tuple[int, int, tuple[int, ...]]:
    parent_fd = os.open(parent, os.O_RDONLY | os.O_CLOEXEC | os.O_DIRECTORY)
    path = parent / name
    if is_directory:
        path.mkdir()
        (path / "original.txt").write_text("original", encoding="utf-8")
        entry_fd = os.open(
            name,
            os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_DIRECTORY,
            dir_fd=parent_fd,
        )
    else:
        path.write_text("original", encoding="utf-8")
        entry_fd = os.open(
            name,
            os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW,
            dir_fd=parent_fd,
        )
    return (
        parent_fd,
        entry_fd,
        runtime_base._readback_object_identity(os.fstat(entry_fd)),
    )


def test_discard_readback_staging_preserves_raced_directory_replacement(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    parent = tmp_path / "cleanup"
    parent.mkdir(mode=0o700)
    parent_fd, staging_fd, identity = _opened_cleanup_entry(
        parent,
        "staging",
        is_directory=True,
    )
    original_rename = runtime_base._rename_readback_cleanup_noreplace
    displaced = parent / "displaced-staging"

    def race_after_identity_check(
        source_fd: int,
        source_name: str,
        target_fd: int,
        target_name: str,
    ) -> None:
        (parent / source_name).rename(displaced)
        replacement = parent / source_name
        replacement.mkdir()
        (replacement / "replacement.txt").write_text("replacement", encoding="utf-8")
        original_rename(source_fd, source_name, target_fd, target_name)

    monkeypatch.setattr(
        runtime_base,
        "_rename_readback_cleanup_noreplace",
        race_after_identity_check,
    )
    try:
        with pytest.raises(RuntimePathSecurityError, match="quarantine identity"):
            runtime_base._discard_readback_staging(
                parent_fd,
                "staging",
                staging_fd,
                identity,
            )
    finally:
        os.close(staging_fd)
        os.close(parent_fd)

    assert (displaced / "original.txt").read_text(encoding="utf-8") == "original"
    quarantines = list(parent.glob(".openevo-readback-quarantine-*"))
    assert len(quarantines) == 1
    assert (quarantines[0] / "replacement.txt").read_text(encoding="utf-8") == ("replacement")


@pytest.mark.parametrize("is_directory", [False, True])
def test_discard_readback_publication_preserves_raced_replacement(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    is_directory: bool,
) -> None:
    parent = tmp_path / "cleanup"
    parent.mkdir(mode=0o700)
    parent_fd, publication_fd, identity = _opened_cleanup_entry(
        parent,
        "publication",
        is_directory=is_directory,
    )
    original_rename = runtime_base._rename_readback_cleanup_noreplace
    displaced = parent / "displaced-publication"

    def race_after_identity_check(
        source_fd: int,
        source_name: str,
        target_fd: int,
        target_name: str,
    ) -> None:
        (parent / source_name).rename(displaced)
        replacement = parent / source_name
        if is_directory:
            replacement.mkdir()
            (replacement / "replacement.txt").write_text(
                "replacement",
                encoding="utf-8",
            )
        else:
            replacement.write_text("replacement", encoding="utf-8")
        original_rename(source_fd, source_name, target_fd, target_name)

    monkeypatch.setattr(
        runtime_base,
        "_rename_readback_cleanup_noreplace",
        race_after_identity_check,
    )
    try:
        with pytest.raises(RuntimePathSecurityError, match="quarantine identity"):
            runtime_base._discard_readback_publication(
                parent_fd,
                "publication",
                publication_fd,
                identity,
                is_directory=is_directory,
            )
    finally:
        os.close(publication_fd)
        os.close(parent_fd)

    if is_directory:
        assert (displaced / "original.txt").read_text(encoding="utf-8") == "original"
        quarantines = list(parent.glob(".openevo-readback-quarantine-*"))
        assert len(quarantines) == 1
        assert (quarantines[0] / "replacement.txt").read_text(encoding="utf-8") == ("replacement")
    else:
        assert displaced.read_text(encoding="utf-8") == "original"
        quarantines = list(parent.glob(".openevo-readback-quarantine-*"))
        assert len(quarantines) == 1
        assert quarantines[0].read_text(encoding="utf-8") == "replacement"


def test_discard_readback_publication_does_not_follow_raced_symlink(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    parent = tmp_path / "cleanup"
    parent.mkdir(mode=0o700)
    parent_fd, publication_fd, identity = _opened_cleanup_entry(
        parent,
        "publication",
        is_directory=False,
    )
    original_rename = runtime_base._rename_readback_cleanup_noreplace
    displaced = parent / "displaced-publication"
    outside = tmp_path / "outside.txt"
    outside.write_text("outside", encoding="utf-8")

    def race_after_identity_check(
        source_fd: int,
        source_name: str,
        target_fd: int,
        target_name: str,
    ) -> None:
        (parent / source_name).rename(displaced)
        (parent / source_name).symlink_to(outside)
        original_rename(source_fd, source_name, target_fd, target_name)

    monkeypatch.setattr(
        runtime_base,
        "_rename_readback_cleanup_noreplace",
        race_after_identity_check,
    )
    try:
        with pytest.raises(RuntimePathSecurityError, match="quarantine identity"):
            runtime_base._discard_readback_publication(
                parent_fd,
                "publication",
                publication_fd,
                identity,
                is_directory=False,
            )
    finally:
        os.close(publication_fd)
        os.close(parent_fd)

    assert displaced.read_text(encoding="utf-8") == "original"
    assert outside.read_text(encoding="utf-8") == "outside"
    quarantines = list(parent.glob(".openevo-readback-quarantine-*"))
    assert len(quarantines) == 1
    assert quarantines[0].is_symlink()


@pytest.mark.parametrize("child_is_directory", [False, True])
def test_discard_readback_publication_preserves_raced_child_replacement(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    child_is_directory: bool,
) -> None:
    parent = tmp_path / "cleanup"
    parent.mkdir(mode=0o700)
    parent_fd, publication_fd, identity = _opened_cleanup_entry(
        parent,
        "publication",
        is_directory=True,
    )
    publication = parent / "publication"
    child = publication / "child"
    if child_is_directory:
        child.mkdir()
        (child / "original-child.txt").write_text("original", encoding="utf-8")
    else:
        child.write_text("original", encoding="utf-8")
    original_rename = runtime_base._rename_readback_cleanup_noreplace
    raced = False

    def race_child_after_identity_check(
        source_fd: int,
        source_name: str,
        target_fd: int,
        target_name: str,
    ) -> None:
        nonlocal raced
        if source_name == "child" and not raced:
            raced = True
            os.rename(
                "child",
                "displaced-child",
                src_dir_fd=source_fd,
                dst_dir_fd=source_fd,
            )
            if child_is_directory:
                os.mkdir("child", mode=0o700, dir_fd=source_fd)
                child_fd = os.open(
                    "child",
                    os.O_RDONLY | os.O_CLOEXEC | os.O_DIRECTORY,
                    dir_fd=source_fd,
                )
                try:
                    replacement_fd = os.open(
                        "replacement-child.txt",
                        os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC,
                        0o600,
                        dir_fd=child_fd,
                    )
                    os.write(replacement_fd, b"replacement")
                    os.close(replacement_fd)
                finally:
                    os.close(child_fd)
            else:
                replacement_fd = os.open(
                    "child",
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC,
                    0o600,
                    dir_fd=source_fd,
                )
                os.write(replacement_fd, b"replacement")
                os.close(replacement_fd)
        original_rename(source_fd, source_name, target_fd, target_name)

    monkeypatch.setattr(
        runtime_base,
        "_rename_readback_cleanup_noreplace",
        race_child_after_identity_check,
    )
    try:
        with pytest.raises(RuntimePathSecurityError, match="quarantine identity"):
            runtime_base._discard_readback_publication(
                parent_fd,
                "publication",
                publication_fd,
                identity,
                is_directory=True,
            )
    finally:
        os.close(publication_fd)
        os.close(parent_fd)

    assert raced is True
    top_quarantines = list(parent.glob(".openevo-readback-quarantine-*"))
    assert len(top_quarantines) == 1
    quarantined_publication = top_quarantines[0]
    child_quarantines = list(quarantined_publication.glob(".openevo-readback-quarantine-*"))
    assert len(child_quarantines) == 1
    displaced = quarantined_publication / "displaced-child"
    if child_is_directory:
        assert (displaced / "original-child.txt").read_text(encoding="utf-8") == ("original")
        assert (child_quarantines[0] / "replacement-child.txt").read_text(
            encoding="utf-8"
        ) == "replacement"
    else:
        assert displaced.read_text(encoding="utf-8") == "original"
        assert child_quarantines[0].read_text(encoding="utf-8") == "replacement"


def test_discard_readback_publication_does_not_follow_nested_symlink(
    tmp_path: Path,
) -> None:
    parent = tmp_path / "cleanup"
    parent.mkdir(mode=0o700)
    parent_fd, publication_fd, identity = _opened_cleanup_entry(
        parent,
        "publication",
        is_directory=True,
    )
    outside = tmp_path / "outside.txt"
    outside.write_text("outside", encoding="utf-8")
    (parent / "publication" / "child-link").symlink_to(outside)
    try:
        with pytest.raises(RuntimePathSecurityError, match="link or special file"):
            runtime_base._discard_readback_publication(
                parent_fd,
                "publication",
                publication_fd,
                identity,
                is_directory=True,
            )
    finally:
        os.close(publication_fd)
        os.close(parent_fd)

    assert outside.read_text(encoding="utf-8") == "outside"
    top_quarantines = list(parent.glob(".openevo-readback-quarantine-*"))
    assert len(top_quarantines) == 1
    assert (top_quarantines[0] / "child-link").is_symlink()


def test_copy_from_bind_mount_rejects_parent_symlink_without_reading_outside(
    tmp_path: Path,
) -> None:
    runtime = _runtime(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_text("outside secret", encoding="utf-8")
    (runtime.session_dir / "workspace").symlink_to(
        outside,
        target_is_directory=True,
    )
    destination = tmp_path / "captured.txt"

    with pytest.raises(RuntimePathSecurityError):
        runtime._copy_from_bind_mount(
            "/openevo/session/workspace/secret.txt",
            destination,
        )

    assert not destination.exists()


@pytest.mark.parametrize("direction", ["to", "from"])
def test_bind_copy_rejects_fifo_leaf_without_blocking(
    tmp_path: Path,
    direction: str,
) -> None:
    runtime = _runtime(tmp_path)
    fifo = tmp_path / "source.fifo" if direction == "to" else runtime.session_dir / "source.fifo"
    os.mkfifo(fifo, mode=0o600)
    started = time.monotonic()

    with pytest.raises(RuntimePathSecurityError, match="special"):
        if direction == "to":
            runtime._copy_to_bind_mount(
                str(fifo),
                "/openevo/session/target.txt",
            )
        else:
            runtime._copy_from_bind_mount(
                "/openevo/session/source.fifo",
                tmp_path / "target.txt",
            )

    assert time.monotonic() - started < 0.1


def test_copy_to_bind_mount_rechecks_target_directory_after_replacement(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = _runtime(tmp_path)
    source = tmp_path / "source.txt"
    source.write_bytes(b"trusted bytes")
    target_parent = runtime.session_dir / "workspace"
    target_parent.mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_text("outside remains", encoding="utf-8")
    original_copy = runtime_base._copy_fd_contents
    replaced = False

    def replace_target_parent(
        source_fd: int,
        target_fd: int,
        expected_size: int,
    ) -> None:
        nonlocal replaced
        if not replaced:
            replaced = True
            target_parent.rename(runtime.session_dir / "workspace-pinned")
            target_parent.mkdir()
            (target_parent / "target.txt").symlink_to(outside)
        original_copy(source_fd, target_fd, expected_size)

    monkeypatch.setattr(runtime_base, "_copy_fd_contents", replace_target_parent)

    with pytest.raises(RuntimePathSecurityError, match="changed"):
        runtime._copy_to_bind_mount(
            str(source),
            "/openevo/session/workspace/target.txt",
        )

    assert replaced is True
    assert outside.read_text(encoding="utf-8") == "outside remains"


def test_runtime_spec_rejects_noncanonical_prepare_target() -> None:
    with pytest.raises(ValueError, match="prepare target"):
        RuntimeSpec(
            image="runtime:latest",
            prepare=[
                {
                    "type": "upload_file",
                    "source": "/tmp/source",
                    "target": "/openevo/session/../outside",
                }
            ],
        )


@pytest.mark.parametrize(
    ("override", "message"),
    [
        ({"backend": "apptainer"}, "Docker"),
        ({"image": "attacker:latest"}, "exact Core-managed image"),
        ({"container_user": "image"}, "container_user='host'"),
    ],
)
def test_runtime_spec_rejects_invalid_managed_self_deployed_binding(
    override: dict[str, str],
    message: str,
) -> None:
    payload = {
        "backend": "docker",
        "profile": "managed_science",
        "image": MANAGED_RUNTIME_IMAGES["managed_science"],
        "container_user": "host",
        **override,
    }

    with pytest.raises(ValueError, match=message):
        RuntimeSpec.model_validate(payload)


def test_runtime_spec_accepts_exact_managed_self_deployed_binding() -> None:
    spec = RuntimeSpec(
        backend="docker",
        profile="managed_science",
        image=MANAGED_RUNTIME_IMAGES["managed_science"],
        container_user="host",
    )

    assert spec.profile == "managed_science"


@pytest.mark.asyncio
async def test_local_command_timeout_preserves_bounded_stdout_and_stderr(
    tmp_path: Path,
) -> None:
    runtime = _runtime(tmp_path)

    return_code, stdout, stderr = await runtime._run_local_command(
        sys.executable,
        "-c",
        (
            "import sys,time; "
            "print('stdout-before-timeout', flush=True); "
            "print('stderr-before-timeout', file=sys.stderr, flush=True); "
            "time.sleep(30)"
        ),
        timeout=0.1,
        capture=True,
    )

    assert return_code == -1
    assert stdout == "stdout-before-timeout\n"
    assert stderr == "stderr-before-timeout\n"
    assert runtime._active_process is None


@pytest.mark.asyncio
async def test_local_command_runtime_cancel_preserves_output_before_termination(
    tmp_path: Path,
) -> None:
    runtime = _runtime(tmp_path)
    command_task = asyncio.create_task(
        runtime._run_local_command(
            sys.executable,
            "-c",
            "import time; print('before-cancel', flush=True); time.sleep(30)",
            capture=True,
        )
    )
    while runtime._active_process is None:
        await asyncio.sleep(0)
    await asyncio.sleep(0.05)

    await runtime.cancel()
    return_code, stdout, stderr = await asyncio.wait_for(command_task, timeout=2)

    assert return_code != 0
    assert stdout == "before-cancel\n"
    assert stderr is None
    assert runtime._active_process is None
