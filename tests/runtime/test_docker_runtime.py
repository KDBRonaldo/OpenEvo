from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from openevo.runtime.docker import DockerRuntime
from openevo.runtime.models import RuntimeSpec


@pytest.mark.asyncio
async def test_host_user_mode_sets_the_container_uid_without_permission_widening(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(os, "getuid", lambda: 4242)
    monkeypatch.setattr(os, "getgid", lambda: 4343)
    runtime = DockerRuntime(
        RuntimeSpec(
            image="openevo/science-runtime:0.1.0",
            container_user="host",
        ),
        "science-session",
        tmp_path,
    )
    run_command = AsyncMock(return_value=(0, None, None))
    monkeypatch.setattr(runtime, "_run_local_command", run_command)

    await runtime.start()

    create = run_command.await_args_list[0].args
    assert create[:4] == ("docker", "create", "--name", "openevo-science-session")
    assert ("--user", "4242:4343") == create[4:6]
    assert all("chmod" not in call.args for call in run_command.await_args_list)
    assert all(call.args[-2:] != ("id", "-u") for call in run_command.await_args_list)


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
