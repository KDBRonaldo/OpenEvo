from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from openevo.runtime.docker import DockerRuntime
from openevo.runtime.managed import MANAGED_CODEX_HOME
from openevo.runtime.models import RuntimeSpec


@pytest.mark.asyncio
@pytest.mark.parametrize("host_uid", [0, 1000, 4242])
async def test_host_user_mode_sets_the_container_uid_without_permission_widening(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    host_uid: int,
) -> None:
    monkeypatch.setattr(os, "getuid", lambda: host_uid)
    monkeypatch.setattr(os, "getgid", lambda: host_uid + 1)
    runtime = DockerRuntime(
        RuntimeSpec(
            image="openevo/science-runtime:0.1.0",
            container_user="host",
            env={
                "HOME": "/openevo/session/home",
                "PATH": "/home/openevo/.local/bin:/usr/local/bin:/usr/bin:/bin",
            },
        ),
        "science-session",
        tmp_path,
    )
    run_command = AsyncMock(return_value=(0, None, None))
    monkeypatch.setattr(runtime, "_run_local_command", run_command)

    await runtime.start()

    create = run_command.await_args_list[0].args
    assert create[:4] == ("docker", "create", "--name", "openevo-science-session")
    assert ("--user", f"{host_uid}:{host_uid + 1}") == create[4:6]
    assert all("chmod" not in call.args for call in run_command.await_args_list)
    assert all(call.args[-2:] != ("id", "-u") for call in run_command.await_args_list)

    await runtime.exec("command -v codex && mkdir -p $HOME/.agents/skills")

    execute = run_command.await_args_list[-1].args
    assert "HOME=/openevo/session/home" in execute
    assert (
        "PATH=/home/openevo/.local/bin:/usr/local/bin:/usr/bin:/bin" in execute
    )
    assert execute[-3:-1] == ("bash", "-lc")
    assert execute[-1].startswith(
        "export HOME=/openevo/session/home; "
        "export PATH=/home/openevo/.local/bin:/usr/local/bin:/usr/bin:/bin; "
    )
    assert execute[-1].endswith(
        "command -v codex && mkdir -p $HOME/.agents/skills"
    )

    await runtime.stop()

    assert all("a+rwX" not in call.args for call in run_command.await_args_list)
    assert all("chmod" not in call.args for call in run_command.await_args_list)


@pytest.mark.asyncio
async def test_host_user_start_failure_never_runs_world_permission_fallback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = DockerRuntime(
        RuntimeSpec(image="managed:latest", container_user="host"),
        "science-session",
        tmp_path,
    )
    run_command = AsyncMock(
        side_effect=[
            (0, None, None),
            (1, None, "start failed"),
            (1, None, None),
            (0, None, None),
        ]
    )
    monkeypatch.setattr(runtime, "_run_local_command", run_command)

    with pytest.raises(RuntimeError, match="docker start failed"):
        await runtime.start()

    assert all("a+rwX" not in call.args for call in run_command.await_args_list)
    assert all("chmod" not in call.args for call in run_command.await_args_list)


@pytest.mark.asyncio
async def test_private_credential_root_is_mounted_outside_the_session_tree(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session_dir = tmp_path / "session"
    credential_dir = tmp_path / "credentials"
    session_dir.mkdir()
    credential_dir.mkdir(mode=0o700)
    runtime = DockerRuntime(
        RuntimeSpec(
            profile="managed_science",
            image="openevo/science-runtime:0.1.0",
            container_user="host",
        ),
        "science-session",
        session_dir,
        credential_dir=credential_dir,
    )
    run_command = AsyncMock(return_value=(0, None, None))
    monkeypatch.setattr(runtime, "_run_local_command", run_command)

    await runtime.start()

    create = run_command.await_args_list[0].args
    assert ("-v", f"{credential_dir}:{MANAGED_CODEX_HOME}") in zip(
        create,
        create[1:],
    )
    assert not credential_dir.is_relative_to(session_dir)


@pytest.mark.asyncio
async def test_stop_is_retryable_until_container_removal_is_proven(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = DockerRuntime(
        RuntimeSpec(image="runtime:latest", container_user="host"),
        "science-session",
        tmp_path,
    )
    run_command = AsyncMock(
        side_effect=[
            (1, None, "kill failed"),
            (1, None, "rm failed"),
            (0, "container-id", None),
            (0, None, None),
            (0, None, None),
        ]
    )
    monkeypatch.setattr(runtime, "_run_local_command", run_command)

    with pytest.raises(RuntimeError, match="could not be proven removed"):
        await runtime.stop()

    assert runtime._destroyed is False

    await runtime.stop()

    assert runtime._destroyed is True
    assert [call.args[1] for call in run_command.await_args_list] == [
        "kill",
        "rm",
        "container",
        "kill",
        "rm",
    ]


@pytest.mark.asyncio
async def test_stop_accepts_inspect_proof_that_container_is_already_absent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = DockerRuntime(
        RuntimeSpec(image="runtime:latest", container_user="host"),
        "science-session",
        tmp_path,
    )
    run_command = AsyncMock(
        side_effect=[
            (1, None, "No such container"),
            (1, None, "No such container"),
            (1, None, "Error: No such object: openevo-science-session"),
        ]
    )
    monkeypatch.setattr(runtime, "_run_local_command", run_command)

    await runtime.stop()

    assert runtime._destroyed is True


def test_container_user_rejects_unknown_modes(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="container_user"):
        DockerRuntime(
            RuntimeSpec(
                image="openevo/science-runtime:0.1.0",
                container_user="root",  # type: ignore[arg-type]
            ),
            "science-session",
            tmp_path,
        )


def test_runtime_spec_rejects_custom_entrypoint() -> None:
    with pytest.raises(ValueError, match="entrypoint"):
        RuntimeSpec.model_validate(
            {
                "profile": "managed_science",
                "image": "openevo/science-runtime:0.1.0",
                "container_user": "host",
                "entrypoint": ["/bin/sh"],
            }
        )
