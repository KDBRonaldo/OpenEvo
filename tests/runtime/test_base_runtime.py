from __future__ import annotations

import asyncio
import hashlib
import inspect
import os
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
            target.mkdir()
            for index in range(257):
                (target / f"file-{index:03d}.txt").write_bytes(b"x")

    private_root = tmp_path / "compat-readback"
    private_root.mkdir(mode=0o700)
    private_root.chmod(0o700)
    budget = RuntimeReadbackBudget()

    readback = await runtime_base._bounded_public_runtime_readback(
        PublicDownloadRuntime(),
        "/custom/evolution",
        private_root / "evolution",
        budget=budget,
        relative_prefix="evolution",
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
            target.mkdir()
            for index in range(runtime_base.RUNTIME_READBACK_MAX_FILES + 1):
                (target / f"file-{index:04d}.txt").touch()

    private_root = tmp_path / "compat-readback"
    private_root.mkdir(mode=0o700)
    private_root.chmod(0o700)
    budget = RuntimeReadbackBudget()

    with pytest.raises(RuntimePathSecurityError, match="file (quota|budget)"):
        await runtime_base._bounded_public_runtime_readback(
            PublicDownloadRuntime(),
            "/custom/evolution",
            private_root / "evolution",
            budget=budget,
            relative_prefix="evolution",
        )

    assert budget.files_consumed == budget.max_files
    assert budget.nodes_consumed == budget.max_nodes
    assert budget.bytes_consumed == budget.max_bytes


@pytest.mark.asyncio
async def test_compatibility_download_quota_cancels_public_download_before_completion(
    tmp_path: Path,
) -> None:
    class PublicDownloadRuntime:
        cancelled = False
        completed = False

        async def download_dir(self, _remote_path: str, local_path: str) -> None:
            target = Path(local_path)
            target.mkdir()
            try:
                for index in range(10_000):
                    (target / f"file-{index:05d}.txt").touch()
                    if index % 100 == 0:
                        await asyncio.sleep(0.001)
            except asyncio.CancelledError:
                self.cancelled = True
                raise
            self.completed = True

    private_root = tmp_path / "compat-readback"
    private_root.mkdir(mode=0o700)
    private_root.chmod(0o700)
    budget = RuntimeReadbackBudget()
    runtime = PublicDownloadRuntime()

    with pytest.raises(RuntimePathSecurityError, match="file quota"):
        await runtime_base._bounded_public_runtime_readback(
            runtime,
            "/custom/evolution",
            private_root / "evolution",
            budget=budget,
            relative_prefix="evolution",
        )

    assert runtime.cancelled is True
    assert runtime.completed is False
    assert len(list((private_root / "evolution").iterdir())) < 10_000
    assert budget.files_consumed == budget.max_files
    assert budget.nodes_consumed == budget.max_nodes
    assert budget.bytes_consumed == budget.max_bytes


@pytest.mark.asyncio
async def test_compatibility_readback_rejects_more_than_64_mib(
    tmp_path: Path,
) -> None:
    class PublicDownloadRuntime:
        async def download_dir(self, _remote_path: str, local_path: str) -> None:
            target = Path(local_path)
            target.mkdir()
            oversized = target / "oversized.bin"
            with oversized.open("wb") as stream:
                stream.truncate(runtime_base.RUNTIME_READBACK_MAX_BYTES + 1)

    private_root = tmp_path / "compat-readback"
    private_root.mkdir(mode=0o700)
    private_root.chmod(0o700)
    budget = RuntimeReadbackBudget()

    with pytest.raises(RuntimePathSecurityError, match="byte (quota|budget)"):
        await runtime_base._bounded_public_runtime_readback(
            PublicDownloadRuntime(),
            "/custom/evolution",
            private_root / "evolution",
            budget=budget,
            relative_prefix="evolution",
        )

    assert budget.files_consumed == budget.max_files
    assert budget.nodes_consumed == budget.max_nodes
    assert budget.bytes_consumed == budget.max_bytes


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
        "docker",
        "cp",
        f"{'a' * 64}:/workspace/result.txt",
        str(local_file),
    )
    assert run_command.await_args_list[1].args == (
        "docker",
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
    assert (quarantines[0] / "replacement.txt").read_text(encoding="utf-8") == (
        "replacement"
    )


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
        assert (quarantines[0] / "replacement.txt").read_text(encoding="utf-8") == (
            "replacement"
        )
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
    fifo = (
        tmp_path / "source.fifo"
        if direction == "to"
        else runtime.session_dir / "source.fifo"
    )
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
