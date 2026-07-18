from __future__ import annotations

import asyncio
import os
import signal
import sys
from pathlib import Path
from typing import Any

import pytest

from openevo.runtime import BubblewrapRuntime
from openevo.runtime import bubblewrap as bubblewrap_module
from openevo.runtime.base import RuntimePathSecurityError
from openevo.runtime.factory import create_runtime
from openevo.runtime.models import RuntimeSpec


def _make_executable(path: Path) -> Path:
    path.write_text("#!/bin/sh\nexit 0\n", encoding="ascii")
    path.chmod(0o700)
    return path


def _make_rootfs(path: Path) -> Path:
    for relative in ("dev", "home", "openevo/session", "proc", "tmp"):
        (path / relative).mkdir(parents=True, exist_ok=True)
    return path


def _runtime(
    tmp_path: Path,
    *,
    allow_internet: bool = True,
    env: dict[str, str] | None = None,
    workdir: str | None = None,
) -> BubblewrapRuntime:
    rootfs = _make_rootfs(tmp_path / "rootfs")
    session = tmp_path / "session"
    binary = _make_executable(tmp_path / "bwrap")
    return BubblewrapRuntime(
        RuntimeSpec(
            backend="bubblewrap",
            container_user="host",
            image=str(rootfs),
            allow_internet=allow_internet,
            env=env or {},
            workdir=workdir,
            kwargs={"bwrap_binary": str(binary)},
        ),
        "bubblewrap-test",
        session,
    )


class _CompletedProcess:
    def __init__(self, *, stdout: bytes = b"", stderr: bytes = b"", returncode: int = 0):
        self.pid = 4242
        self.returncode: int | None = returncode
        self.stdout = asyncio.StreamReader()
        self.stderr = asyncio.StreamReader()
        self.stdout.feed_data(stdout)
        self.stdout.feed_eof()
        self.stderr.feed_data(stderr)
        self.stderr.feed_eof()

    async def wait(self) -> int:
        assert self.returncode is not None
        return self.returncode


def _option_value(args: tuple[str, ...], option: str) -> str:
    return args[args.index(option) + 1]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("allow_internet", "shares_network"),
    [(True, True), (False, False)],
)
async def test_exec_uses_fd_bound_read_only_rootfs_and_writable_session(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    allow_internet: bool,
    shares_network: bool,
) -> None:
    runtime = _runtime(
        tmp_path,
        allow_internet=allow_internet,
        env={"CUSTOM": "spec"},
    )
    await runtime.start()
    calls: list[tuple[tuple[str, ...], dict[str, Any]]] = []

    async def create_process(*args: str, **kwargs: Any) -> _CompletedProcess:
        calls.append((args, kwargs))
        return _CompletedProcess(stdout=b"ok\n")

    monkeypatch.setattr(
        bubblewrap_module.asyncio,
        "create_subprocess_exec",
        create_process,
    )

    result = await runtime.exec(
        "printf ok",
        cwd="/openevo/session/work",
        env={"CUSTOM": "command", "EXTRA": "value"},
    )

    assert result.return_code == 0
    assert result.stdout == "ok\n"
    assert len(calls) == 1
    args, kwargs = calls[0]
    assert args[0] == str(tmp_path / "bwrap")
    assert "--unshare-all" in args
    assert "--die-with-parent" in args
    assert "--new-session" in args
    assert ("--share-net" in args) is shares_network
    assert "--clearenv" in args
    assert _option_value(args, "--ro-bind-fd").isdigit()
    assert args[args.index("--ro-bind-fd") + 2] == "/"
    assert _option_value(args, "--bind-fd").isdigit()
    assert args[args.index("--bind-fd") + 2] == "/openevo/session"
    assert ("--tmpfs", "/tmp") == args[args.index("--tmpfs") : args.index("--tmpfs") + 2]
    assert "/home" in [
        args[index + 1] for index, value in enumerate(args[:-1]) if value == "--tmpfs"
    ]
    assert _option_value(args, "--chdir") == "/openevo/session/work"
    assert args[-4:] == ("--", "/bin/bash", "-lc", "printf ok")
    setenv = {
        args[index + 1]: args[index + 2]
        for index, value in enumerate(args[:-2])
        if value == "--setenv"
    }
    assert setenv["HOME"] == "/home/openevo"
    assert setenv["TMPDIR"] == "/tmp"
    assert setenv["CUSTOM"] == "command"
    assert setenv["EXTRA"] == "value"
    assert kwargs["env"] == {}
    assert kwargs["start_new_session"] is True
    assert set(kwargs["pass_fds"]) == {
        runtime._binary_authority.descriptor,
        runtime._rootfs_authority.descriptor,
        runtime._session_authority.descriptor,
    }
    assert kwargs["executable"] == (f"/proc/self/fd/{runtime._binary_authority.descriptor}")
    await runtime.stop()


def test_factory_selects_bubblewrap_and_rejects_unclaimed_capabilities(
    tmp_path: Path,
) -> None:
    rootfs = tmp_path / "rootfs"
    rootfs.mkdir()
    spec = RuntimeSpec(
        backend="bubblewrap",
        container_user="host",
        image=str(rootfs),
    )

    runtime = create_runtime(spec, "factory", tmp_path / "session")

    assert isinstance(runtime, BubblewrapRuntime)
    assert runtime.supports_gpus is False
    assert runtime.supports_cpu_limits is False
    assert runtime.supports_memory_limits is False
    assert runtime.supports_storage_limits is False
    assert runtime.can_disable_internet is True
    assert runtime._binary_path == Path("/usr/bin/bwrap")

    for field, value in (
        ("gpus", 1),
        ("cpus", 1),
        ("memory_mb", 128),
        ("storage_mb", 128),
    ):
        payload = spec.model_dump()
        payload[field] = value
        with pytest.raises(ValueError, match="does not support"):
            create_runtime(
                RuntimeSpec.model_validate(payload),
                f"unsupported-{field}",
                tmp_path / f"session-{field}",
            )


@pytest.mark.parametrize(
    ("updates", "error"),
    [
        ({"container_user": "image"}, "container_user"),
        ({"network": "bridge"}, "network"),
        ({"network": "none"}, "cannot combine"),
        ({"kwargs": {"unknown": True}}, "unsupported"),
        ({"image": "relative/rootfs"}, "canonical absolute"),
    ],
)
def test_constructor_rejects_unsupported_or_ambiguous_configuration(
    tmp_path: Path,
    updates: dict[str, object],
    error: str,
) -> None:
    rootfs = tmp_path / "rootfs"
    rootfs.mkdir()
    payload: dict[str, object] = {
        "backend": "bubblewrap",
        "container_user": "host",
        "image": str(rootfs),
    }
    payload.update(updates)

    with pytest.raises(ValueError, match=error):
        BubblewrapRuntime(
            RuntimeSpec.model_validate(payload),
            "invalid-config",
            tmp_path / "session",
        )


@pytest.mark.asyncio
async def test_start_rejects_rootfs_symlink(tmp_path: Path) -> None:
    real_rootfs = _make_rootfs(tmp_path / "real-rootfs")
    linked_rootfs = tmp_path / "rootfs"
    linked_rootfs.symlink_to(real_rootfs, target_is_directory=True)
    binary = _make_executable(tmp_path / "bwrap")
    runtime = BubblewrapRuntime(
        RuntimeSpec(
            backend="bubblewrap",
            container_user="host",
            image=str(linked_rootfs),
            kwargs={"bwrap_binary": str(binary)},
        ),
        "rootfs-symlink",
        tmp_path / "session",
    )

    with pytest.raises(RuntimePathSecurityError, match="non-directory"):
        await runtime.start()


@pytest.mark.asyncio
@pytest.mark.parametrize("replacement", ["missing", "symlink"])
async def test_start_requires_real_rootfs_mountpoint_directories(
    tmp_path: Path,
    replacement: str,
) -> None:
    runtime = _runtime(tmp_path)
    mountpoint = runtime._rootfs_path / "openevo" / "session"
    mountpoint.rmdir()
    if replacement == "symlink":
        mountpoint.symlink_to(runtime._rootfs_path / "tmp", target_is_directory=True)

    with pytest.raises(
        RuntimePathSecurityError,
        match="requires real directory /openevo/session",
    ):
        await runtime.start()


@pytest.mark.asyncio
async def test_start_rejects_symlinked_or_writable_binary(tmp_path: Path) -> None:
    rootfs = _make_rootfs(tmp_path / "rootfs")
    real_binary = _make_executable(tmp_path / "real-bwrap")
    linked_binary = tmp_path / "bwrap-link"
    linked_binary.symlink_to(real_binary)
    linked_runtime = BubblewrapRuntime(
        RuntimeSpec(
            backend="bubblewrap",
            container_user="host",
            image=str(rootfs),
            kwargs={"bwrap_binary": str(linked_binary)},
        ),
        "binary-symlink",
        tmp_path / "linked-session",
    )

    with pytest.raises(RuntimePathSecurityError, match="executable"):
        await linked_runtime.start()

    real_binary.chmod(0o722)
    writable_runtime = BubblewrapRuntime(
        RuntimeSpec(
            backend="bubblewrap",
            container_user="host",
            image=str(rootfs),
            kwargs={"bwrap_binary": str(real_binary)},
        ),
        "binary-writable",
        tmp_path / "writable-session",
    )
    with pytest.raises(RuntimePathSecurityError, match="non-writable"):
        await writable_runtime.start()


@pytest.mark.asyncio
async def test_start_requires_core_owned_rootfs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = _runtime(tmp_path)
    actual_uid = os.geteuid()
    monkeypatch.setattr(
        bubblewrap_module.os,
        "geteuid",
        lambda: actual_uid + 1,
    )

    with pytest.raises(RuntimePathSecurityError, match="rootfs.*not owned"):
        await runtime.start()


@pytest.mark.asyncio
@pytest.mark.parametrize("authority", ["rootfs", "session"])
async def test_exec_rejects_replaced_directory_authority(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    authority: str,
) -> None:
    runtime = _runtime(tmp_path)
    await runtime.start()
    target = runtime._rootfs_path if authority == "rootfs" else runtime.session_dir
    displaced = target.with_name(f"{target.name}-displaced")
    target.rename(displaced)
    target.mkdir()

    async def unexpected_spawn(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("bubblewrap must not spawn after authority replacement")

    monkeypatch.setattr(
        bubblewrap_module.asyncio,
        "create_subprocess_exec",
        unexpected_spawn,
    )

    with pytest.raises(RuntimePathSecurityError, match=f"bubblewrap {authority} path"):
        await runtime.exec("true")
    await runtime.stop()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("cwd", "env", "error"),
    [
        ("/openevo/session/../outside", None, "canonical absolute"),
        (None, {"BAD-NAME": "value"}, "invalid variable name"),
        (None, {"HOME": "/openevo/session/home"}, "cannot override HOME"),
        (None, {"VALUE": "bad\x00value"}, "is invalid"),
    ],
)
async def test_exec_rejects_nonclosed_cwd_and_environment(
    tmp_path: Path,
    cwd: str | None,
    env: dict[str, str] | None,
    error: str,
) -> None:
    runtime = _runtime(tmp_path)
    await runtime.start()
    with pytest.raises(ValueError, match=error):
        await runtime.exec("true", cwd=cwd, env=env)
    await runtime.stop()


@pytest.mark.asyncio
async def test_root_is_a_valid_closed_working_directory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = _runtime(tmp_path, workdir="/")
    await runtime.start()

    async def create_process(*_args: str, **_kwargs: Any) -> _CompletedProcess:
        return _CompletedProcess()

    monkeypatch.setattr(
        bubblewrap_module.asyncio,
        "create_subprocess_exec",
        create_process,
    )
    assert (await runtime.exec("true")).return_code == 0
    await runtime.stop()


@pytest.mark.asyncio
async def test_transfers_are_limited_to_safe_session_bind_paths(tmp_path: Path) -> None:
    runtime = _runtime(tmp_path)
    source = tmp_path / "source.txt"
    source.write_text("payload", encoding="utf-8")

    await runtime.upload_file(str(source), "/openevo/session/work/source.txt")
    destination = tmp_path / "download" / "source.txt"
    await runtime.download_file(
        "/openevo/session/work/source.txt",
        str(destination),
    )

    assert destination.read_text(encoding="utf-8") == "payload"
    with pytest.raises(RuntimePathSecurityError, match="uploads must target"):
        await runtime.upload_file(str(source), "/workspace/source.txt")
    with pytest.raises(RuntimePathSecurityError, match="downloads must source"):
        await runtime.download_file("/workspace/source.txt", str(destination))

    outside = tmp_path / "outside.txt"
    outside.write_text("outside", encoding="utf-8")
    (runtime.session_dir / "linked.txt").symlink_to(outside)
    with pytest.raises(RuntimePathSecurityError, match="link or special"):
        await runtime.download_file(
            "/openevo/session/linked.txt",
            str(tmp_path / "linked-download.txt"),
        )


@pytest.mark.asyncio
async def test_cancel_kills_and_reaps_the_real_host_process_group(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = _runtime(tmp_path)
    await runtime.start()
    original_create = asyncio.create_subprocess_exec
    spawned: list[asyncio.subprocess.Process] = []

    async def create_sleep_process(*_args: str, **kwargs: Any) -> asyncio.subprocess.Process:
        process = await original_create(
            sys.executable,
            "-c",
            "import time; time.sleep(30)",
            env=kwargs["env"],
            start_new_session=kwargs["start_new_session"],
            stdout=kwargs["stdout"],
            stderr=kwargs["stderr"],
        )
        spawned.append(process)
        return process

    monkeypatch.setattr(
        bubblewrap_module.asyncio,
        "create_subprocess_exec",
        create_sleep_process,
    )
    execution = asyncio.create_task(runtime.exec("sleep 30"))
    async with asyncio.timeout(2):
        while runtime._active_process is None:
            await asyncio.sleep(0.01)

    await runtime.cancel()
    result = await execution

    assert result.return_code == -signal.SIGKILL
    assert len(spawned) == 1
    assert spawned[0].returncode == -signal.SIGKILL
    assert runtime._active_process is None
    assert runtime._destroyed is True


@pytest.mark.asyncio
async def test_cancellation_during_spawn_waits_for_process_group_cleanup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = _runtime(tmp_path)
    await runtime.start()
    release_spawn = asyncio.Event()
    process = _CompletedProcess()
    process.returncode = None
    killed: list[tuple[int, int]] = []

    async def delayed_spawn(*_args: str, **_kwargs: Any) -> _CompletedProcess:
        await release_spawn.wait()
        return process

    async def wait() -> int:
        while process.returncode is None:
            await asyncio.sleep(0)
        return process.returncode

    def killpg(pid: int, sig: int) -> None:
        killed.append((pid, sig))
        process.returncode = -sig

    process.wait = wait  # type: ignore[method-assign]
    monkeypatch.setattr(
        bubblewrap_module.asyncio,
        "create_subprocess_exec",
        delayed_spawn,
    )
    monkeypatch.setattr(bubblewrap_module.os, "killpg", killpg)
    execution = asyncio.create_task(runtime.exec("true"))
    await asyncio.sleep(0)
    execution.cancel()
    release_spawn.set()

    with pytest.raises(asyncio.CancelledError):
        await execution

    assert killed == [(process.pid, signal.SIGKILL)]
    assert process.returncode == -signal.SIGKILL
    assert runtime._active_process is None
    await runtime.stop()


@pytest.mark.asyncio
async def test_timeout_kills_and_reaps_the_real_host_process_group(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = _runtime(tmp_path)
    await runtime.start()
    original_create = asyncio.create_subprocess_exec
    spawned: list[asyncio.subprocess.Process] = []

    async def create_sleep_process(*_args: str, **kwargs: Any) -> asyncio.subprocess.Process:
        process = await original_create(
            sys.executable,
            "-c",
            "import time; time.sleep(30)",
            env=kwargs["env"],
            start_new_session=kwargs["start_new_session"],
            stdout=kwargs["stdout"],
            stderr=kwargs["stderr"],
        )
        spawned.append(process)
        return process

    monkeypatch.setattr(
        bubblewrap_module.asyncio,
        "create_subprocess_exec",
        create_sleep_process,
    )

    result = await runtime.exec("sleep 30", timeout_sec=0.05)

    assert result.return_code == -1
    assert len(spawned) == 1
    assert spawned[0].returncode == -signal.SIGKILL
    assert runtime._active_process is None
    await runtime.stop()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_real_bubblewrap_rootless_smoke(tmp_path: Path) -> None:
    rootfs_value = os.environ.get("OPENEVO_BWRAP_ROOTFS")
    if not rootfs_value:
        pytest.skip("set OPENEVO_BWRAP_ROOTFS to a user-owned rootfs directory")
    if not Path("/usr/bin/bwrap").is_file():
        pytest.skip("/usr/bin/bwrap is unavailable")

    runtime = BubblewrapRuntime(
        RuntimeSpec(
            backend="bubblewrap",
            container_user="host",
            image=rootfs_value,
            allow_internet=False,
        ),
        "real-bubblewrap-smoke",
        tmp_path / "session",
    )
    await runtime.start()
    try:
        result = await runtime.exec(
            'test "$HOME" = /home/openevo'
            ' && touch "$HOME/private-home" /tmp/private-tmp'
            " && ! touch /openevo-rootfs-write-probe 2>/dev/null"
            " && printf rootless-ok > /openevo/session/result.txt",
            timeout_sec=10,
        )
        assert result.return_code == 0, result.stderr
        assert (runtime.session_dir / "result.txt").read_text(encoding="utf-8") == "rootless-ok"
    finally:
        await runtime.stop()
