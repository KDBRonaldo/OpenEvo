from __future__ import annotations

import os
import shutil
import subprocess
import uuid
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from openevo.runtime.docker import DockerRuntime
from openevo.runtime.managed import MANAGED_CODEX_HOME
from openevo.runtime.models import RuntimeSpec


_REAL_DOCKER_IMAGE = "python:3.12-slim-bookworm"


def _write_mock_cidfile(args: tuple[str, ...], container_id: str) -> None:
    cidfile = Path(args[args.index("--cidfile") + 1])
    cidfile.write_text(container_id + "\n", encoding="ascii")


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
    inspect_count = 0

    async def run_command_impl(*args, **kwargs):
        nonlocal inspect_count
        del kwargs
        if args[1] == "create":
            _write_mock_cidfile(args, "sha256:host-user-container")
            return 0, "sha256:host-user-container\n", None
        if args[1:3] == ("container", "inspect"):
            inspect_count += 1
            if inspect_count < 3:
                return 0, "sha256:host-user-container\n", None
            return 1, None, "Error: No such object: sha256:host-user-container"
        return 0, None, None

    run_command = AsyncMock(side_effect=run_command_impl)
    monkeypatch.setattr(runtime, "_run_local_command", run_command)

    await runtime.start()

    create = run_command.await_args_list[0].args
    assert create[:4] == ("docker", "create", "--name", "openevo-science-session")
    assert create[4] == "--cidfile"
    assert ("--user", f"{host_uid}:{host_uid + 1}") == create[6:8]
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
    responses = iter(
        [
            (0, "sha256:start-failure-container\n", None),
            (0, "sha256:start-failure-container\n", None),
            (1, None, "start failed"),
            (0, "sha256:start-failure-container\n", None),
            (1, None, None),
            (0, None, None),
            (1, None, "Error: No such object: sha256:start-failure-container"),
        ]
    )

    async def run_command_impl(*args, **kwargs):
        del kwargs
        if args[1] == "create":
            _write_mock_cidfile(args, "sha256:start-failure-container")
        return next(responses)

    run_command = AsyncMock(side_effect=run_command_impl)
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

    async def run_command_impl(*args, **kwargs):
        del kwargs
        if args[1] == "create":
            _write_mock_cidfile(args, "sha256:credential-container")
            return 0, "sha256:credential-container\n", None
        if args[1:3] == ("container", "inspect"):
            return 0, "sha256:credential-container\n", None
        return 0, None, None

    run_command = AsyncMock(side_effect=run_command_impl)
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
    runtime._container_id = "container-id"
    run_command = AsyncMock(
        side_effect=[
            (0, "container-id", None),
            (1, None, "kill failed"),
            (1, None, "rm failed"),
            (0, "container-id", None),
            (0, "container-id", None),
            (0, None, None),
            (0, None, None),
            (1, None, "Error: No such object: container-id"),
        ]
    )
    monkeypatch.setattr(runtime, "_run_local_command", run_command)

    with pytest.raises(RuntimeError, match="could not be proven removed"):
        await runtime.stop()

    assert runtime._destroyed is False

    await runtime.stop()

    assert runtime._destroyed is True
    assert [call.args[1] for call in run_command.await_args_list] == [
        "container",
        "kill",
        "rm",
        "container",
        "container",
        "kill",
        "rm",
        "container",
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
    runtime._container_id = "container-id"
    run_command = AsyncMock(
        side_effect=[
            (1, None, "Error: No such object: container-id"),
        ]
    )
    monkeypatch.setattr(runtime, "_run_local_command", run_command)

    await runtime.stop()

    assert runtime._destroyed is True


@pytest.mark.asyncio
async def test_stop_requires_explicit_absence_proof_after_successful_remove(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = DockerRuntime(
        RuntimeSpec(image="runtime:latest", container_user="host"),
        "science-session",
        tmp_path,
    )
    runtime._container_id = "sha256:credential-container"
    run_command = AsyncMock(
        side_effect=[
            (0, "sha256:credential-container", None),
            (0, None, None),
            (0, None, None),
            (0, "sha256:credential-container", None),
        ]
    )
    monkeypatch.setattr(runtime, "_run_local_command", run_command)

    with pytest.raises(RuntimeError, match="could not be proven removed"):
        await runtime.stop()

    assert runtime._destroyed is False


@pytest.mark.asyncio
async def test_runtime_pins_container_id_and_uses_it_for_cleanup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = DockerRuntime(
        RuntimeSpec(image="runtime:latest", container_user="host"),
        "science-session",
        tmp_path,
    )
    responses = iter(
        [
            (0, "sha256:credential-container\n", None),
            (0, "sha256:credential-container\n", None),
            (0, None, None),
            (0, "sha256:credential-container\n", None),
            (0, None, None),
            (0, None, None),
            (1, None, "Error: No such object: sha256:credential-container"),
        ]
    )

    async def run_command_impl(*args, **kwargs):
        del kwargs
        if args[1] == "create":
            _write_mock_cidfile(args, "sha256:credential-container")
        return next(responses)

    run_command = AsyncMock(side_effect=run_command_impl)
    monkeypatch.setattr(runtime, "_run_local_command", run_command)

    await runtime.start()
    await runtime.stop()

    assert runtime.container_id == "sha256:credential-container"
    kill_args = run_command.await_args_list[4].args
    remove_args = run_command.await_args_list[5].args
    inspect_args = run_command.await_args_list[6].args
    assert kill_args[-1] == "sha256:credential-container"
    assert remove_args[-1] == "sha256:credential-container"
    assert inspect_args[-1] == "sha256:credential-container"


@pytest.mark.asyncio
async def test_create_name_collision_never_operates_on_the_external_container(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = DockerRuntime(
        RuntimeSpec(image="runtime:latest", container_user="host"),
        "collision-session",
        tmp_path,
    )
    run_command = AsyncMock(
        return_value=(
            1,
            None,
            "Conflict. The container name /openevo-collision-session is already in use",
        )
    )
    monkeypatch.setattr(runtime, "_run_local_command", run_command)

    with pytest.raises(RuntimeError, match="docker create failed"):
        await runtime.start()

    await runtime.stop()
    await runtime.cancel()

    commands = [call.args[1] for call in run_command.await_args_list]
    assert commands == ["create"]
    assert runtime.container_id is None


@pytest.mark.asyncio
async def test_successful_create_with_unverified_cid_never_falls_back_to_name_cleanup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = DockerRuntime(
        RuntimeSpec(image="runtime:latest", container_user="host"),
        "unverified-session",
        tmp_path,
    )
    candidate_id = "a" * 64

    async def run_command_impl(*args, **kwargs):
        del kwargs
        if args[1] == "create":
            cidfile = Path(args[args.index("--cidfile") + 1])
            cidfile.write_text(candidate_id + "\n", encoding="ascii")
            return 0, candidate_id + "\n", None
        if args[1:3] == ("container", "inspect"):
            return 1, None, "temporary daemon verification failure"
        raise AssertionError(f"unexpected destructive command: {args}")

    run_command = AsyncMock(side_effect=run_command_impl)
    monkeypatch.setattr(runtime, "_run_local_command", run_command)

    with pytest.raises(RuntimeError, match="could not be verified"):
        await runtime.start()
    with pytest.raises(RuntimeError, match="could not be verified"):
        await runtime.stop()

    assert runtime.container_id == candidate_id
    assert [call.args[1] for call in run_command.await_args_list] == [
        "create",
        "container",
        "container",
    ]


@pytest.mark.asyncio
async def test_real_docker_name_collision_preserves_running_external_container(
    tmp_path: Path,
) -> None:
    if shutil.which("docker") is None:
        pytest.skip("docker CLI is unavailable")
    info = subprocess.run(
        ["docker", "info", "--format", "{{.ServerVersion}}"],
        check=False,
        capture_output=True,
        text=True,
    )
    if info.returncode != 0:
        pytest.skip("docker daemon is unavailable")
    image = subprocess.run(
        ["docker", "image", "inspect", _REAL_DOCKER_IMAGE],
        check=False,
        capture_output=True,
        text=True,
    )
    if image.returncode != 0:
        pytest.skip(f"required local probe image is unavailable: {_REAL_DOCKER_IMAGE}")

    session_id = f"collision-{uuid.uuid4().hex[:16]}"
    container_name = f"openevo-{session_id}"
    created = subprocess.run(
        [
            "docker",
            "run",
            "-d",
            "--name",
            container_name,
            _REAL_DOCKER_IMAGE,
            "sleep",
            "300",
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    external_id = created.stdout.strip()
    runtime = DockerRuntime(
        RuntimeSpec(image=_REAL_DOCKER_IMAGE, container_user="host"),
        session_id,
        tmp_path,
    )
    try:
        with pytest.raises(RuntimeError, match="docker create failed"):
            await runtime.start()
        await runtime.stop()
        await runtime.cancel()

        inspected = subprocess.run(
            ["docker", "inspect", "--format", "{{.Id}} {{.State.Running}}", external_id],
            check=True,
            capture_output=True,
            text=True,
        )
        assert inspected.stdout.strip() == f"{external_id} true"
    finally:
        subprocess.run(
            ["docker", "rm", "-f", external_id],
            check=False,
            capture_output=True,
        )


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
