from __future__ import annotations

import asyncio
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import uuid
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from openevo.runtime.base import RuntimePathSecurityError
from openevo.runtime.docker import DockerRuntime, _CREDENTIAL_VIEW_NAME
from openevo.runtime.managed import (
    MANAGED_CODEX_HOME,
    MANAGED_RUNTIME_RELEASES,
    ManagedCredentialMount,
)
from openevo.runtime.models import RuntimeSpec


_REAL_DOCKER_IMAGE = "python:3.12-slim-bookworm"


def _managed_image_record(
    image_id: str,
    *,
    repo_digests: list[str] | None = None,
) -> dict[str, object]:
    return {
        "Id": image_id,
        "RepoDigests": [] if repo_digests is None else repo_digests,
        "Config": {"Labels": {"io.openevo.managed-runtime": "true"}},
    }


def _real_docker_unavailable(reason: str) -> None:
    if os.environ.get("OPENEVO_REQUIRE_REAL_DOCKER") == "1":
        pytest.fail(reason)
    pytest.skip(reason)


async def _probe_real_docker_credential_mount(tmp_path: Path) -> None:
    shared_parent, daemon_parent = _real_docker_shared_parent(tmp_path)
    session_dir = tmp_path / "credential-session"
    session_dir.mkdir()
    authority = _credential_mount(shared_parent / "credential-root")
    runtime = DockerRuntime(
        RuntimeSpec(image=_REAL_DOCKER_IMAGE, container_user="host"),
        f"credential-{uuid.uuid4().hex[:16]}",
        session_dir,
        credential_mount=authority,
        ownership_root=tmp_path / "credential-docker-authority",
    )
    runtime._credential_docker_source = daemon_parent / "credential-root"
    try:
        await runtime.start()
        original_auth = authority.root / "auth.json"
        renamed_auth = authority.root / "renamed-by-host.json"
        original_auth.rename(renamed_auth)
        original_auth.write_text(
            '{"access_token":"replacement-runtime-canary"}\n',
            encoding="utf-8",
        )
        original_auth.chmod(0o600)
        read_result = await runtime.exec(f"cat {MANAGED_CODEX_HOME}/auth.json")
        assert read_result.return_code == 0, read_result.stderr
        assert read_result.stdout == '{"access_token":"runtime-canary"}\n'
        move_result = await runtime.exec(
            f"mv {MANAGED_CODEX_HOME}/auth.json {MANAGED_CODEX_HOME}/agent-move.json"
        )
        assert move_result.return_code != 0
        assert renamed_auth.is_file()
        assert "replacement-runtime-canary" in original_auth.read_text(encoding="utf-8")
        assert not (authority.root / _CREDENTIAL_VIEW_NAME / "agent-move.json").exists()
    finally:
        if runtime.container_id is not None and not runtime.absence_proven:
            await runtime.stop()
        shutil.rmtree(shared_parent, ignore_errors=True)


def _real_docker_shared_parent(tmp_path: Path) -> tuple[Path, Path]:
    direct_probe = tmp_path / "daemon-path-probe"
    direct_probe.mkdir()
    visible = subprocess.run(
        [
            "docker",
            "run",
            "--rm",
            "--mount",
            f"type=bind,source={direct_probe},target=/probe,readonly",
            _REAL_DOCKER_IMAGE,
            "test",
            "-d",
            "/probe",
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    direct_probe.rmdir()
    if visible.returncode == 0:
        parent = Path(tempfile.mkdtemp(prefix="openevo-credential-", dir=tmp_path))
        return parent, parent

    current_container = os.environ.get("HOSTNAME") or os.uname().nodename
    inspected = subprocess.run(
        ["docker", "container", "inspect", current_container],
        check=False,
        capture_output=True,
        text=True,
    )
    if inspected.returncode == 0:
        payload = json.loads(inspected.stdout)
        mounts = payload[0].get("Mounts", []) if len(payload) == 1 else []
        for mount in mounts:
            destination = Path(str(mount.get("Destination", "")))
            source = Path(str(mount.get("Source", "")))
            if (
                mount.get("Type") == "bind"
                and mount.get("RW") is True
                and destination.is_dir()
                and os.access(destination, os.W_OK | os.X_OK)
                and source.is_absolute()
            ):
                parent = Path(tempfile.mkdtemp(prefix="openevo-credential-", dir=destination))
                return parent, source / parent.relative_to(destination)
    _real_docker_unavailable(
        "Docker daemon has no writable bind root shared with the test process"
    )
    raise AssertionError("unreachable")


@pytest.fixture(autouse=True)
def _isolate_default_ownership_root(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "openevo.runtime.docker._DEFAULT_OWNERSHIP_ROOT",
        tmp_path.parent / ".docker-authority" / tmp_path.name,
    )


def _write_mock_cidfile(
    args: tuple[str, ...],
    container_id: str,
    *,
    mode: int = 0o644,
) -> None:
    cidfile = Path(args[args.index("--cidfile") + 1])
    cidfile.write_text(container_id + "\n", encoding="ascii")
    cidfile.chmod(mode)


def _credential_mount(root: Path) -> ManagedCredentialMount:
    root.mkdir(mode=0o700)
    auth = root / "auth.json"
    auth.write_text('{"access_token":"runtime-canary"}\n', encoding="utf-8")
    auth.chmod(0o600)
    root_state = root.stat(follow_symlinks=False)
    auth_state = auth.stat(follow_symlinks=False)
    return ManagedCredentialMount(
        root=root,
        root_identity=(root_state.st_dev, root_state.st_ino, root_state.st_uid),
        auth_identity=(
            auth_state.st_dev,
            auth_state.st_ino,
            auth_state.st_mode,
            auth_state.st_uid,
            auth_state.st_nlink,
            auth_state.st_size,
            auth_state.st_mtime_ns,
            auth_state.st_ctime_ns,
        ),
    )


def _credential_stat_output(authority: ManagedCredentialMount) -> str:
    root = (authority.root / _CREDENTIAL_VIEW_NAME).stat(follow_symlinks=False)
    auth = (authority.root / "auth.json").stat(follow_symlinks=False)
    return (
        f"{root.st_dev} {root.st_ino} {root.st_mode:x} {root.st_uid} "
        f"{root.st_nlink} {root.st_size}\n"
        f"{auth.st_dev} {auth.st_ino} {auth.st_mode:x} {auth.st_uid} "
        f"{auth.st_nlink} {auth.st_size}\n"
    )


def _credential_mount_inspect_output(authority: ManagedCredentialMount) -> str:
    return json.dumps(
        [
            {
                "Type": "bind",
                "Source": str(authority.root / _CREDENTIAL_VIEW_NAME),
                "Destination": MANAGED_CODEX_HOME,
                "RW": False,
            },
            {
                "Type": "bind",
                "Source": str(authority.root / "auth.json"),
                "Destination": f"{MANAGED_CODEX_HOME}/auth.json",
                "RW": False,
            },
        ]
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["upload", "download"])
async def test_bind_copy_security_failure_never_falls_back_to_docker_cp(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    operation: str,
) -> None:
    runtime = DockerRuntime(
        RuntimeSpec(image="runtime:latest"),
        "secure-copy-session",
        tmp_path,
    )
    run_command = AsyncMock()
    monkeypatch.setattr(runtime, "_run_local_command", run_command)
    if operation == "upload":
        monkeypatch.setattr(
            runtime,
            "_copy_to_bind_mount",
            lambda *args: (_ for _ in ()).throw(RuntimePathSecurityError("unsafe bind target")),
        )
        operation_call = runtime.upload_file(
            str(tmp_path / "source.txt"),
            "/openevo/session/target.txt",
        )
    else:
        monkeypatch.setattr(
            runtime,
            "_copy_from_bind_mount",
            lambda *args: (_ for _ in ()).throw(RuntimePathSecurityError("unsafe bind source")),
        )
        operation_call = runtime.download_file(
            "/openevo/session/source.txt",
            str(tmp_path / "target.txt"),
        )

    with pytest.raises(RuntimePathSecurityError, match="unsafe bind"):
        await operation_call

    run_command.assert_not_awaited()


@pytest.mark.asyncio
async def test_create_normalizes_umask_cidfile_to_private_mode_before_inspect(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = DockerRuntime(
        RuntimeSpec(image="runtime:latest", container_user="host"),
        "cidfile-mode-session",
        tmp_path,
    )
    container_id = "0" * 64
    container_present = True
    mode_seen_by_inspect: int | None = None

    async def run_command_impl(*args, **kwargs):
        nonlocal container_present, mode_seen_by_inspect
        del kwargs
        if args[1] == "create":
            _write_mock_cidfile(args, container_id, mode=0o644)
            assert stat.S_IMODE(runtime._cidfile.stat().st_mode) == 0o644
            return 0, container_id + "\n", None
        if args[1:3] == ("container", "inspect"):
            mode_seen_by_inspect = stat.S_IMODE(runtime._cidfile.stat().st_mode)
            if container_present:
                return 0, container_id + "\n", None
            return 1, None, f"Error: No such object: {container_id}"
        if args[1] == "rm":
            container_present = False
        return 0, None, None

    run_command = AsyncMock(side_effect=run_command_impl)
    monkeypatch.setattr(runtime, "_run_local_command", run_command)

    await runtime.start()

    assert mode_seen_by_inspect == 0o600
    assert stat.S_IMODE(runtime._cidfile.stat().st_mode) == 0o600
    assert stat.S_IMODE(runtime._ownership_lock.stat().st_mode) == 0o600

    await runtime.stop()


@pytest.mark.asyncio
async def test_create_rejects_executable_cidfile_mode_without_docker_action(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = DockerRuntime(
        RuntimeSpec(image="runtime:latest", container_user="host"),
        "hostile-cidfile-mode-session",
        tmp_path,
    )
    container_id = "1" * 64

    async def run_command_impl(*args, **kwargs):
        del kwargs
        if args[1] == "create":
            _write_mock_cidfile(args, container_id, mode=0o755)
            return 0, container_id + "\n", None
        raise AssertionError(f"untrusted cidfile must not authorize Docker: {args}")

    run_command = AsyncMock(side_effect=run_command_impl)
    monkeypatch.setattr(runtime, "_run_local_command", run_command)

    with pytest.raises(RuntimeError, match="ownership could not be verified"):
        await runtime.start()

    assert runtime.container_id is None
    assert stat.S_IMODE(runtime._cidfile.stat().st_mode) == 0o755
    assert [call.args[1] for call in run_command.await_args_list] == ["create"]


@pytest.mark.asyncio
async def test_create_rejects_cidfile_replacement_during_fd_mode_tightening(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = DockerRuntime(
        RuntimeSpec(image="runtime:latest", container_user="host"),
        "replaced-cidfile-mode-session",
        tmp_path,
    )
    container_id = "2" * 64
    replacement_id = "3" * 64
    real_fchmod = os.fchmod
    replaced = False

    def replace_cidfile_after_fchmod(descriptor: int, mode: int) -> None:
        nonlocal replaced
        real_fchmod(descriptor, mode)
        if os.fstat(descriptor).st_size and not replaced:
            replaced = True
            runtime._cidfile.unlink()
            runtime._cidfile.write_text(replacement_id + "\n", encoding="ascii")
            runtime._cidfile.chmod(0o600)

    async def run_command_impl(*args, **kwargs):
        del kwargs
        if args[1] == "create":
            _write_mock_cidfile(args, container_id, mode=0o644)
            return 0, container_id + "\n", None
        raise AssertionError(f"replaced cidfile must not authorize Docker: {args}")

    run_command = AsyncMock(side_effect=run_command_impl)
    monkeypatch.setattr(runtime, "_run_local_command", run_command)
    monkeypatch.setattr(os, "fchmod", replace_cidfile_after_fchmod)

    with pytest.raises(RuntimeError, match="ownership could not be verified"):
        await runtime.start()

    assert replaced is True
    assert runtime.container_id is None
    assert runtime._cidfile.read_text(encoding="ascii") == replacement_id + "\n"
    assert [call.args[1] for call in run_command.await_args_list] == ["create"]


@pytest.mark.asyncio
async def test_failed_create_retains_authority_if_opened_cidfile_is_unlinked(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = DockerRuntime(
        RuntimeSpec(image="runtime:latest", container_user="host"),
        "unlinked-cidfile-mode-session",
        tmp_path,
    )
    container_id = "4" * 64
    real_fchmod = os.fchmod
    unlinked = False

    def unlink_cidfile_after_fchmod(descriptor: int, mode: int) -> None:
        nonlocal unlinked
        real_fchmod(descriptor, mode)
        if os.fstat(descriptor).st_size and not unlinked:
            unlinked = True
            runtime._cidfile.unlink()

    async def run_command_impl(*args, **kwargs):
        del kwargs
        if args[1] == "create":
            _write_mock_cidfile(args, container_id, mode=0o644)
            return 1, None, "create interrupted after writing cidfile"
        raise AssertionError(f"unlinked cidfile must not authorize Docker: {args}")

    run_command = AsyncMock(side_effect=run_command_impl)
    monkeypatch.setattr(runtime, "_run_local_command", run_command)
    monkeypatch.setattr(os, "fchmod", unlink_cidfile_after_fchmod)

    with pytest.raises(RuntimeError, match="ownership could not be verified"):
        await runtime.start()

    assert unlinked is True
    assert runtime.container_id is None
    assert runtime._destroyed is False
    assert runtime._ownership_lock.exists()
    assert [call.args[1] for call in run_command.await_args_list] == ["create"]


@pytest.mark.asyncio
@pytest.mark.parametrize("host_uid", [0, 1000, 4242])
async def test_host_user_mode_sets_the_container_uid_without_permission_widening(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    host_uid: int,
) -> None:
    container_id = "1" * 64
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
            _write_mock_cidfile(args, container_id)
            return 0, container_id + "\n", None
        if args[1:3] == ("container", "inspect"):
            inspect_count += 1
            if inspect_count < 3:
                return 0, container_id + "\n", None
            return 1, None, f"Error: No such object: {container_id}"
        return 0, None, None

    run_command = AsyncMock(side_effect=run_command_impl)
    monkeypatch.setattr(runtime, "_run_local_command", run_command)
    await runtime.start()

    create = run_command.await_args_list[0].args
    assert create[:4] == ("docker", "create", "--name", "openevo-science-session")
    assert create[4] == "--cidfile"
    volume_sources = [
        create[index + 1].split(":", 1)[0]
        for index, value in enumerate(create[:-1])
        if value == "-v"
    ]
    assert str(runtime._ownership_dir) not in volume_sources
    assert ("--user", f"{host_uid}:{host_uid + 1}") == create[6:8]
    assert all("chmod" not in call.args for call in run_command.await_args_list)
    assert all(call.args[-2:] != ("id", "-u") for call in run_command.await_args_list)

    await runtime.exec("command -v codex && mkdir -p $HOME/.agents/skills")

    execute = run_command.await_args_list[-1].args
    assert "HOME=/openevo/session/home" in execute
    assert "PATH=/home/openevo/.local/bin:/usr/local/bin:/usr/bin:/bin" in execute
    assert execute[-3:-1] == ("bash", "-lc")
    assert execute[-1].startswith(
        "export HOME=/openevo/session/home; "
        "export PATH=/home/openevo/.local/bin:/usr/local/bin:/usr/bin:/bin; "
    )
    assert execute[-1].endswith("command -v codex && mkdir -p $HOME/.agents/skills")

    await runtime.stop()

    assert all("a+rwX" not in call.args for call in run_command.await_args_list)
    assert all("chmod" not in call.args for call in run_command.await_args_list)


@pytest.mark.asyncio
async def test_host_user_start_failure_never_runs_world_permission_fallback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    container_id = "2" * 64
    runtime = DockerRuntime(
        RuntimeSpec(image="managed:latest", container_user="host"),
        "science-session",
        tmp_path,
    )
    responses = iter(
        [
            (0, container_id + "\n", None),
            (0, container_id + "\n", None),
            (1, None, "start failed"),
            (0, container_id + "\n", None),
            (1, None, None),
            (0, None, None),
            (1, None, f"Error: No such object: {container_id}"),
        ]
    )

    async def run_command_impl(*args, **kwargs):
        del kwargs
        if args[1] == "create":
            _write_mock_cidfile(args, container_id)
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
    container_id = "3" * 64
    session_dir = tmp_path / "session"
    credential_dir = tmp_path / "credentials"
    session_dir.mkdir()
    credential_mount = _credential_mount(credential_dir)
    runtime = DockerRuntime(
        RuntimeSpec(
            profile="managed_science",
            image="openevo/science-runtime:0.1.0",
            container_user="host",
        ),
        "science-session",
        session_dir,
        credential_mount=credential_mount,
    )

    async def run_command_impl(*args, **kwargs):
        del kwargs
        if args[1:3] == ("image", "inspect"):
            digest = MANAGED_RUNTIME_RELEASES["managed_science"].trusted_digest
            return 0, json.dumps([_managed_image_record(digest)]), None
        if args[1] == "create":
            _write_mock_cidfile(args, container_id)
            return 0, container_id + "\n", None
        if args[1:3] == ("container", "inspect"):
            if any("json .Mounts" in str(value) for value in args):
                return 0, _credential_mount_inspect_output(credential_mount), None
            if any("State.Pid" in str(value) for value in args):
                return 0, f"{container_id}|1234|started-at|true|0\n", None
            return 0, container_id + "\n", None
        if args[1] == "exec" and "/usr/bin/stat" in args:
            return 0, _credential_stat_output(credential_mount), None
        return 0, None, None

    run_command = AsyncMock(side_effect=run_command_impl)
    monkeypatch.setattr(runtime, "_run_local_command", run_command)
    await runtime.start()

    create = next(call.args for call in run_command.await_args_list if call.args[1] == "create")
    mounts = [create[index + 1] for index, value in enumerate(create) if value == "--mount"]
    credential_mounts = [mount for mount in mounts if MANAGED_CODEX_HOME in mount]
    assert credential_mounts == [
        "type=bind,"
        f"source={credential_dir / _CREDENTIAL_VIEW_NAME},"
        f"target={MANAGED_CODEX_HOME},"
        "readonly",
        "type=bind,"
        f"source={credential_dir / 'auth.json'},"
        f"target={MANAGED_CODEX_HOME}/auth.json,"
        "readonly",
    ]
    assert not credential_dir.is_relative_to(session_dir)


@pytest.mark.asyncio
async def test_credential_mount_rejects_path_replacement_adopted_by_docker(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    container_id = "6" * 64
    session_dir = tmp_path / "session"
    credential_dir = tmp_path / "credentials"
    displaced = tmp_path / "displaced-credentials"
    session_dir.mkdir()
    authority = _credential_mount(credential_dir)
    runtime = DockerRuntime(
        RuntimeSpec(
            profile="managed_science",
            image="openevo/science-runtime:0.1.0",
            container_user="host",
        ),
        "credential-race-session",
        session_dir,
        credential_mount=authority,
    )
    container_present = True
    replacement: ManagedCredentialMount | None = None

    async def run_command_impl(*args, **kwargs):
        nonlocal container_present, replacement
        del kwargs
        if args[1:3] == ("image", "inspect"):
            digest = MANAGED_RUNTIME_RELEASES["managed_science"].trusted_digest
            return 0, json.dumps([_managed_image_record(digest)]), None
        if args[1] == "create":
            credential_dir.rename(displaced)
            replacement = _credential_mount(credential_dir)
            assert replacement.root_identity != authority.root_identity
            credential_mounts = [
                args[index + 1]
                for index, value in enumerate(args)
                if value == "--mount" and MANAGED_CODEX_HOME in args[index + 1]
            ]
            assert credential_mounts == [
                "type=bind,"
                f"source={authority.root / _CREDENTIAL_VIEW_NAME},"
                f"target={MANAGED_CODEX_HOME},"
                "readonly",
                "type=bind,"
                f"source={authority.root / 'auth.json'},"
                f"target={MANAGED_CODEX_HOME}/auth.json,"
                "readonly",
            ]
            _write_mock_cidfile(args, container_id)
            return 0, container_id, None
        if args[1:3] == ("container", "inspect"):
            if container_present:
                if any("json .Mounts" in str(value) for value in args):
                    assert replacement is not None
                    return 0, _credential_mount_inspect_output(replacement), None
                if any("State.Pid" in str(value) for value in args):
                    return 0, f"{container_id}|5678|started-at|true|0\n", None
                return 0, container_id, None
            return 1, None, f"Error: No such object: {container_id}"
        if args[1] == "exec" and "/usr/bin/stat" in args:
            assert replacement is not None
            return 0, _credential_stat_output(replacement), None
        if args[1] == "rm":
            container_present = False
        return 0, None, None

    monkeypatch.setattr(runtime, "_run_local_command", AsyncMock(side_effect=run_command_impl))
    with pytest.raises(RuntimeError, match="did not preserve"):
        await runtime.start()

    assert runtime._credential_root_fd == -1
    assert runtime._credential_view_fd == -1
    assert runtime._credential_target_fd == -1
    assert runtime._credential_auth_fd == -1


@pytest.mark.asyncio
async def test_credential_mount_rejects_replacement_after_final_process_identity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    container_id = "8" * 64
    session_dir = tmp_path / "session"
    credential_dir = tmp_path / "credentials"
    displaced = tmp_path / "displaced-credentials"
    session_dir.mkdir()
    authority = _credential_mount(credential_dir)
    runtime = DockerRuntime(
        RuntimeSpec(
            profile="managed_science",
            image="openevo/science-runtime:0.1.0",
            container_user="host",
        ),
        "credential-final-race-session",
        session_dir,
        credential_mount=authority,
    )
    process_inspects = 0
    container_present = True

    async def run_command_impl(*args, **kwargs):
        nonlocal container_present, process_inspects
        del kwargs
        if args[1:3] == ("image", "inspect"):
            digest = MANAGED_RUNTIME_RELEASES["managed_science"].trusted_digest
            return 0, json.dumps([_managed_image_record(digest)]), None
        if args[1] == "create":
            _write_mock_cidfile(args, container_id)
            return 0, container_id, None
        if args[1:3] == ("container", "inspect"):
            if not container_present:
                return 1, None, f"Error: No such object: {container_id}"
            if any("json .Mounts" in str(value) for value in args):
                return 0, _credential_mount_inspect_output(authority), None
            if any("State.Pid" in str(value) for value in args):
                process_inspects += 1
                if process_inspects == 2:
                    credential_dir.rename(displaced)
                    _credential_mount(credential_dir)
                return 0, f"{container_id}|9012|started-at|true|0\n", None
            return 0, container_id, None
        if args[1] == "exec" and "/usr/bin/stat" in args:
            return 0, _credential_stat_output(authority), None
        if args[1] == "rm":
            container_present = False
        return 0, None, None

    run_command = AsyncMock(side_effect=run_command_impl)
    monkeypatch.setattr(runtime, "_run_local_command", run_command)

    with pytest.raises(RuntimeError, match="did not adopt"):
        await runtime.start()

    assert process_inspects == 2
    assert any(call.args[1] == "rm" for call in run_command.await_args_list)
    assert runtime.absence_proven is True


@pytest.mark.asyncio
async def test_credential_create_reconciliation_log_omits_traceback_canary(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    secret = "docker-create-reconciliation-canary"
    session_dir = tmp_path / "session"
    session_dir.mkdir()
    runtime = DockerRuntime(
        RuntimeSpec(image="runtime:latest", container_user="host"),
        "credential-create-log",
        session_dir,
        credential_mount=_credential_mount(tmp_path / "credentials"),
    )

    class CanaryFailure(RuntimeError):
        pass

    async def fail_reconciliation(*, explicit_failure: bool) -> None:
        del explicit_failure
        traceback_local = secret
        raise CanaryFailure(traceback_local) from ValueError(secret)

    monkeypatch.setattr(runtime, "_reconcile_create_ownership", fail_reconciliation)
    with caplog.at_level("WARNING", logger="openevo.runtime.docker"):
        await runtime._reconcile_create_after_interruption(explicit_failure=False)

    assert secret not in caplog.text
    assert "Traceback" not in caplog.text
    assert "CanaryFailure" in caplog.text


@pytest.mark.asyncio
@pytest.mark.parametrize("repo_digests", [None, [], ["example/runtime@sha256:" + "b" * 64]])
async def test_managed_image_drift_fails_before_credential_mount_or_create(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    repo_digests: list[str] | None,
) -> None:
    session_dir = tmp_path / "session"
    credential_dir = tmp_path / "credentials"
    session_dir.mkdir()
    credential_mount = _credential_mount(credential_dir)
    runtime = DockerRuntime(
        RuntimeSpec(
            profile="managed_science",
            image="openevo/science-runtime:0.1.0",
            container_user="host",
        ),
        "drifted-science-session",
        session_dir,
        credential_mount=credential_mount,
    )
    inspected = {
        "Id": "sha256:" + "c" * 64,
        "RepoDigests": repo_digests,
        "Config": {"Labels": {"io.openevo.managed-runtime": "true"}},
    }
    run_command = AsyncMock(return_value=(0, json.dumps([inspected]), None))
    monkeypatch.setattr(runtime, "_run_local_command", run_command)

    with pytest.raises(RuntimeError, match="image digest mismatch"):
        await runtime.start()

    assert [call.args[1:3] for call in run_command.await_args_list] == [("image", "inspect")]
    assert str(credential_dir) not in repr(run_command.await_args_list)


@pytest.mark.asyncio
async def test_managed_repo_digest_is_the_immutable_docker_create_target(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    container_id = "7" * 64
    release = MANAGED_RUNTIME_RELEASES["managed_science"]
    runtime = DockerRuntime(
        RuntimeSpec(
            profile="managed_science",
            image=release.image,
            container_user="host",
        ),
        "repo-digest-session",
        tmp_path,
    )

    async def run_command_impl(*args, **kwargs):
        del kwargs
        if args[1:3] == ("image", "inspect"):
            return (
                0,
                json.dumps(
                    [
                        {
                            "Id": "sha256:" + "d" * 64,
                            "RepoDigests": [release.immutable_reference],
                            "Config": {"Labels": {"io.openevo.managed-runtime": "true"}},
                        }
                    ]
                ),
                None,
            )
        if args[1] == "create":
            _write_mock_cidfile(args, container_id)
            return 0, container_id, None
        if args[1:3] == ("container", "inspect"):
            return 0, container_id, None
        return 0, None, None

    run_command = AsyncMock(side_effect=run_command_impl)
    monkeypatch.setattr(runtime, "_run_local_command", run_command)

    await runtime.start()

    create = next(call.args for call in run_command.await_args_list if call.args[1] == "create")
    assert release.immutable_reference in create
    assert release.image not in create


@pytest.mark.asyncio
async def test_stop_is_retryable_until_container_removal_is_proven(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    container_id = "4" * 64
    runtime = DockerRuntime(
        RuntimeSpec(image="runtime:latest", container_user="host"),
        "science-session",
        tmp_path,
    )
    runtime._container_id = container_id
    run_command = AsyncMock(
        side_effect=[
            (0, container_id, None),
            (1, None, "kill failed"),
            (1, None, "rm failed"),
            (0, container_id, None),
            (0, container_id, None),
            (0, None, None),
            (0, None, None),
            (1, None, f"Error: No such object: {container_id}"),
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
    container_id = "5" * 64
    runtime = DockerRuntime(
        RuntimeSpec(image="runtime:latest", container_user="host"),
        "science-session",
        tmp_path,
    )
    runtime._container_id = container_id
    run_command = AsyncMock(
        side_effect=[
            (1, None, f"Error: No such object: {container_id}"),
        ]
    )
    monkeypatch.setattr(runtime, "_run_local_command", run_command)

    await runtime.stop()

    assert runtime._destroyed is True


@pytest.mark.asyncio
async def test_stop_rejects_non_immutable_container_reference_without_docker_command(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = DockerRuntime(
        RuntimeSpec(image="runtime:latest", container_user="host"),
        "invalid-container-reference",
        tmp_path,
    )
    runtime._container_id = "openevo-external-container"
    run_command = AsyncMock()
    monkeypatch.setattr(runtime, "_run_local_command", run_command)

    with pytest.raises(RuntimeError, match="container ID is invalid"):
        await runtime.stop()

    run_command.assert_not_awaited()


@pytest.mark.asyncio
async def test_stop_requires_explicit_absence_proof_after_successful_remove(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    container_id = "6" * 64
    runtime = DockerRuntime(
        RuntimeSpec(image="runtime:latest", container_user="host"),
        "science-session",
        tmp_path,
    )
    runtime._container_id = container_id
    run_command = AsyncMock(
        side_effect=[
            (0, container_id, None),
            (0, None, None),
            (0, None, None),
            (0, container_id, None),
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
    container_id = "7" * 64
    runtime = DockerRuntime(
        RuntimeSpec(image="runtime:latest", container_user="host"),
        "science-session",
        tmp_path,
    )
    responses = iter(
        [
            (0, container_id + "\n", None),
            (0, container_id + "\n", None),
            (0, None, None),
            (0, container_id + "\n", None),
            (0, None, None),
            (0, None, None),
            (1, None, f"Error: No such object: {container_id}"),
        ]
    )

    async def run_command_impl(*args, **kwargs):
        del kwargs
        if args[1] == "create":
            _write_mock_cidfile(args, container_id)
        return next(responses)

    run_command = AsyncMock(side_effect=run_command_impl)
    monkeypatch.setattr(runtime, "_run_local_command", run_command)

    await runtime.start()
    await runtime.stop()

    assert runtime.container_id == container_id
    kill_args = run_command.await_args_list[4].args
    remove_args = run_command.await_args_list[5].args
    inspect_args = run_command.await_args_list[6].args
    assert kill_args[-1] == container_id
    assert remove_args[-1] == container_id
    assert inspect_args[-1] == container_id


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
async def test_cancelled_create_adopts_cidfile_and_cleans_only_immutable_id(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    authority_root = tmp_path / "core-private-docker-authority"
    runtime = DockerRuntime(
        RuntimeSpec(image="runtime:latest", container_user="host"),
        "cancelled-create-session",
        tmp_path / "session",
        ownership_root=authority_root,
    )
    container_id = "c" * 64
    create_has_written_cid = asyncio.Event()
    create_wait = asyncio.Event()
    container_present = True

    async def run_command_impl(*args, **kwargs):
        nonlocal container_present
        del kwargs
        if args[1] == "create":
            _write_mock_cidfile(args, container_id)
            create_has_written_cid.set()
            await create_wait.wait()
            return 0, container_id + "\n", None
        if args[1:3] == ("container", "inspect"):
            if container_present:
                return 0, container_id + "\n", None
            return 1, None, f"Error: No such object: {container_id}"
        if args[1] == "rm":
            assert args[-1] == container_id
            container_present = False
            return 0, None, None
        if args[1] == "kill":
            assert args[-1] == container_id
            return 0, None, None
        raise AssertionError(f"unexpected docker command: {args}")

    run_command = AsyncMock(side_effect=run_command_impl)
    monkeypatch.setattr(runtime, "_run_local_command", run_command)

    start_task = asyncio.create_task(runtime.start())
    await create_has_written_cid.wait()
    start_task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await start_task

    assert runtime.container_id == container_id
    assert runtime._destroyed is False
    assert runtime._cidfile.exists()
    assert runtime._ownership_lock.exists()

    await runtime.stop()

    assert runtime.absence_proven is True
    assert not runtime._cidfile.exists()
    assert not runtime._ownership_lock.exists()
    destructive = [
        call.args for call in run_command.await_args_list if call.args[1] in {"kill", "rm"}
    ]
    assert destructive
    assert all(command[-1] == container_id for command in destructive)


@pytest.mark.asyncio
async def test_create_exception_after_cidfile_retains_recoverable_ownership(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    authority_root = tmp_path / "core-private-docker-authority"
    runtime = DockerRuntime(
        RuntimeSpec(image="runtime:latest", container_user="host"),
        "exception-create-session",
        tmp_path / "session",
        ownership_root=authority_root,
    )
    container_id = "e" * 64
    container_present = True

    async def run_command_impl(*args, **kwargs):
        nonlocal container_present
        del kwargs
        if args[1] == "create":
            _write_mock_cidfile(args, container_id)
            raise RuntimeError("docker transport interrupted")
        if args[1:3] == ("container", "inspect"):
            if container_present:
                return 0, container_id + "\n", None
            return 1, None, f"Error: No such object: {container_id}"
        if args[1] == "rm":
            container_present = False
        return 0, None, None

    run_command = AsyncMock(side_effect=run_command_impl)
    monkeypatch.setattr(runtime, "_run_local_command", run_command)

    with pytest.raises(RuntimeError, match="transport interrupted"):
        await runtime.start()

    assert runtime.container_id == container_id
    assert runtime._cidfile.exists()
    await runtime.stop()
    assert runtime.absence_proven is True


@pytest.mark.asyncio
async def test_create_timeout_after_cidfile_retains_recoverable_ownership(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    authority_root = tmp_path / "core-private-docker-authority"
    runtime = DockerRuntime(
        RuntimeSpec(image="runtime:latest", container_user="host"),
        "timeout-create-session",
        tmp_path / "session",
        ownership_root=authority_root,
    )
    container_id = "8" * 64
    container_present = True

    async def run_command_impl(*args, **kwargs):
        nonlocal container_present
        del kwargs
        if args[1] == "create":
            _write_mock_cidfile(args, container_id)
            return -1, None, None
        if args[1:3] == ("container", "inspect"):
            if container_present:
                return 0, container_id + "\n", None
            return 1, None, f"Error: No such object: {container_id}"
        if args[1] == "rm":
            container_present = False
        return 0, None, None

    run_command = AsyncMock(side_effect=run_command_impl)
    monkeypatch.setattr(runtime, "_run_local_command", run_command)

    with pytest.raises(RuntimeError, match="docker create failed with exit code -1"):
        await runtime.start()

    assert runtime.container_id == container_id
    assert runtime._cidfile.exists()
    await runtime.stop()
    assert runtime.absence_proven is True


@pytest.mark.asyncio
async def test_private_authority_recovery_discovers_and_removes_owned_container(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    authority_root = tmp_path / "core-private-docker-authority"
    runtime = DockerRuntime(
        RuntimeSpec(image="runtime:latest", container_user="host"),
        "crashed-create-session",
        tmp_path / "session",
        ownership_root=authority_root,
    )
    container_id = "d" * 64
    runtime._prepare_create_ownership()
    runtime._cidfile.write_text(container_id + "\n", encoding="ascii")
    os.close(runtime._ownership_lock_fd)
    runtime._ownership_lock_fd = -1
    container_present = True
    commands: list[tuple[str, ...]] = []

    async def run_command_impl(self, *args, **kwargs):
        nonlocal container_present
        del self, kwargs
        commands.append(args)
        if args[1:3] == ("container", "inspect"):
            if container_present:
                return 0, container_id + "\n", None
            return 1, None, f"Error: No such object: {container_id}"
        if args[1] == "rm":
            container_present = False
        return 0, None, None

    monkeypatch.setattr(DockerRuntime, "_run_local_command", run_command_impl)

    await DockerRuntime.recover_ownership_root(authority_root)

    assert container_present is False
    assert authority_root.exists()
    assert list(authority_root.iterdir()) == []
    assert all(command[-1] == container_id for command in commands)


@pytest.mark.asyncio
async def test_recovery_refuses_authority_lock_held_by_live_runtime(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    authority_root = tmp_path / "core-private-docker-authority"
    runtime = DockerRuntime(
        RuntimeSpec(image="runtime:latest", container_user="host"),
        "live-runtime-session",
        tmp_path / "session",
        ownership_root=authority_root,
    )
    runtime._prepare_create_ownership()
    runtime._cidfile.write_text("9" * 64 + "\n", encoding="ascii")
    commands: list[tuple[str, ...]] = []

    async def reject_command(self, *args, **kwargs):
        del self, kwargs
        commands.append(args)
        raise AssertionError("live authority must not reach recovery commands")

    monkeypatch.setattr(DockerRuntime, "_run_local_command", reject_command)

    with pytest.raises(RuntimeError, match="held by another process"):
        await DockerRuntime.recover_ownership_root(authority_root)

    assert commands == []
    assert runtime._ownership_lock_fd >= 0
    runtime._release_ownership_files()


@pytest.mark.asyncio
async def test_recovery_rejects_agent_replaced_cidfile_without_docker_command(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session_dir = tmp_path / "session"
    session_dir.mkdir()
    authority_root = tmp_path / "core-private-docker-authority"
    runtime = DockerRuntime(
        RuntimeSpec(image="runtime:latest", container_user="host"),
        "tampered-recovery-session",
        session_dir,
        ownership_root=authority_root,
    )
    runtime._prepare_create_ownership()
    outside_cid = session_dir / "agent-cid"
    outside_cid.write_text("f" * 64 + "\n", encoding="ascii")
    runtime._cidfile.symlink_to(outside_cid)
    os.close(runtime._ownership_lock_fd)
    runtime._ownership_lock_fd = -1
    commands: list[tuple[str, ...]] = []

    async def reject_command(self, *args, **kwargs):
        del self, kwargs
        commands.append(args)
        raise AssertionError("tampered authority must not reach Docker")

    monkeypatch.setattr(DockerRuntime, "_run_local_command", reject_command)

    with pytest.raises(RuntimeError):
        await DockerRuntime.recover_ownership_root(authority_root)

    assert commands == []
    assert outside_cid.exists()


@pytest.mark.asyncio
async def test_authority_root_symlink_into_agent_mount_fails_before_create(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session_dir = tmp_path / "session"
    agent_controlled = session_dir / "agent-controlled-authority"
    agent_controlled.mkdir(parents=True)
    authority_root = tmp_path / "core-private-docker-authority"
    authority_root.symlink_to(agent_controlled, target_is_directory=True)
    runtime = DockerRuntime(
        RuntimeSpec(image="runtime:latest", container_user="host"),
        "tampered-authority-session",
        session_dir,
        ownership_root=authority_root,
    )
    run_command = AsyncMock()
    monkeypatch.setattr(runtime, "_run_local_command", run_command)

    with pytest.raises(RuntimeError, match="ownership directory is not private"):
        await runtime.start()

    run_command.assert_not_awaited()


@pytest.mark.asyncio
async def test_authority_root_ancestor_symlink_into_agent_mount_fails_before_create(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session_dir = tmp_path / "session"
    agent_controlled = session_dir / "authority"
    agent_controlled.mkdir(parents=True)
    authority_parent = tmp_path / "authority-parent"
    authority_parent.symlink_to(session_dir, target_is_directory=True)
    runtime = DockerRuntime(
        RuntimeSpec(image="runtime:latest", container_user="host"),
        "ancestor-tampered-authority-session",
        session_dir,
        ownership_root=authority_parent / "authority",
    )
    run_command = AsyncMock()
    monkeypatch.setattr(runtime, "_run_local_command", run_command)

    with pytest.raises(RuntimeError, match="ownership directory is not private"):
        await runtime.start()

    run_command.assert_not_awaited()


def test_agent_and_eval_runtimes_share_unmounted_root_but_have_unique_records(
    tmp_path: Path,
) -> None:
    session_dir = tmp_path / "sessions" / "session"
    eval_session_dir = session_dir / "eval_runtime"
    authority_root = tmp_path / "core-private" / "docker-authority"
    spec = RuntimeSpec(image="runtime:latest", container_user="host")

    agent_runtime = DockerRuntime(
        spec,
        "shared-session",
        session_dir,
        ownership_root=authority_root,
    )
    eval_runtime = DockerRuntime(
        spec,
        "shared-session-eval",
        eval_session_dir,
        ownership_root=authority_root,
    )

    assert agent_runtime._ownership_dir == authority_root
    assert eval_runtime._ownership_dir == authority_root
    assert not authority_root.is_relative_to(session_dir)
    assert agent_runtime._cidfile != eval_runtime._cidfile
    assert str(authority_root) not in f"{session_dir}:{agent_runtime.runtime_session_dir}"


def test_eval_runtime_rejects_authority_inside_main_session_bind(tmp_path: Path) -> None:
    main_session_dir = tmp_path / "session"
    eval_session_dir = main_session_dir / "eval_runtime"

    with pytest.raises(ValueError, match="outside the session tree"):
        DockerRuntime(
            RuntimeSpec(image="runtime:latest", container_user="host"),
            "shared-session-eval",
            eval_session_dir,
            ownership_root=main_session_dir,
        )


@pytest.mark.asyncio
async def test_cancelled_local_command_is_killed_and_reaped(tmp_path: Path) -> None:
    runtime = DockerRuntime(
        RuntimeSpec(image="runtime:latest", container_user="host"),
        "local-command-cancel",
        tmp_path,
    )
    command_task = asyncio.create_task(
        runtime._run_local_command(
            sys.executable,
            "-c",
            "import time; time.sleep(30)",
            capture=True,
        )
    )
    async with asyncio.timeout(2):
        while runtime._active_process is None:
            await asyncio.sleep(0.01)
    process = runtime._active_process
    assert process is not None

    command_task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await command_task

    assert process.returncode is not None
    assert runtime._active_process is None


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
        _real_docker_unavailable("docker CLI is unavailable")
    info = subprocess.run(
        ["docker", "info", "--format", "{{.ServerVersion}}"],
        check=False,
        capture_output=True,
        text=True,
    )
    if info.returncode != 0:
        _real_docker_unavailable("docker daemon is unavailable")
    image = subprocess.run(
        ["docker", "image", "inspect", _REAL_DOCKER_IMAGE],
        check=False,
        capture_output=True,
        text=True,
    )
    if image.returncode != 0:
        _real_docker_unavailable(
            f"required local probe image is unavailable: {_REAL_DOCKER_IMAGE}"
        )

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


@pytest.mark.asyncio
async def test_real_docker_credential_auth_inode_remains_pinned_after_host_replacement(
    tmp_path: Path,
) -> None:
    if shutil.which("docker") is None:
        _real_docker_unavailable("docker CLI is unavailable")
    info = subprocess.run(
        ["docker", "info", "--format", "{{.ServerVersion}}"],
        check=False,
        capture_output=True,
        text=True,
    )
    if info.returncode != 0:
        _real_docker_unavailable("docker daemon is unavailable")
    image = subprocess.run(
        ["docker", "image", "inspect", _REAL_DOCKER_IMAGE],
        check=False,
        capture_output=True,
        text=True,
    )
    if image.returncode != 0:
        _real_docker_unavailable(
            f"required local probe image is unavailable: {_REAL_DOCKER_IMAGE}"
        )

    await _probe_real_docker_credential_mount(tmp_path)


@pytest.mark.asyncio
async def test_real_docker_cancel_after_cidfile_is_recoverably_owned(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    if shutil.which("docker") is None:
        _real_docker_unavailable("docker CLI is unavailable")
    info = subprocess.run(
        ["docker", "info", "--format", "{{.ServerVersion}}"],
        check=False,
        capture_output=True,
        text=True,
    )
    if info.returncode != 0:
        _real_docker_unavailable("docker daemon is unavailable")
    image = subprocess.run(
        ["docker", "image", "inspect", _REAL_DOCKER_IMAGE],
        check=False,
        capture_output=True,
        text=True,
    )
    if image.returncode != 0:
        _real_docker_unavailable(
            f"required local probe image is unavailable: {_REAL_DOCKER_IMAGE}"
        )

    session_dir = tmp_path / "session"
    session_dir.mkdir()
    runtime = DockerRuntime(
        RuntimeSpec(image=_REAL_DOCKER_IMAGE, container_user="host"),
        f"cancel-{uuid.uuid4().hex[:16]}",
        session_dir,
        ownership_root=tmp_path / "core-private-docker-authority",
    )
    original_run_command = runtime._run_local_command
    cidfile_written = asyncio.Event()
    hold_create_return = asyncio.Event()

    async def delayed_create_return(*args, **kwargs):
        result = await original_run_command(*args, **kwargs)
        if args[1] == "create" and result[0] == 0:
            assert runtime._cidfile.exists()
            cidfile_written.set()
            await hold_create_return.wait()
        return result

    monkeypatch.setattr(runtime, "_run_local_command", delayed_create_return)
    previous_umask = os.umask(0o022)
    umask_restored = False
    start_task = asyncio.create_task(runtime.start())
    try:
        await asyncio.wait_for(cidfile_written.wait(), timeout=30)
        assert stat.S_IMODE(runtime._cidfile.stat().st_mode) == 0o644
        os.umask(previous_umask)
        umask_restored = True
        start_task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await start_task

        container_id = runtime.container_id
        assert container_id is not None
        assert len(container_id) == 64
        assert stat.S_IMODE(runtime._cidfile.stat().st_mode) == 0o600
        await runtime.stop()

        inspected = subprocess.run(
            ["docker", "container", "inspect", container_id],
            check=False,
            capture_output=True,
            text=True,
        )
        assert inspected.returncode != 0
    finally:
        if not umask_restored:
            os.umask(previous_umask)
        if not start_task.done():
            start_task.cancel()
            await asyncio.gather(start_task, return_exceptions=True)
        if runtime.container_id is not None:
            subprocess.run(
                ["docker", "rm", "-f", runtime.container_id],
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
