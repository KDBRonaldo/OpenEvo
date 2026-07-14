from __future__ import annotations

import asyncio
import os
import sys
import time
from pathlib import Path

import pytest

from openevo.runtime import base as runtime_base
from openevo.runtime.base import BaseRuntime, RuntimePathSecurityError
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
        del remote_path, local_path

    async def download_dir(self, remote_path: str, local_path: str) -> None:
        del remote_path, local_path


def _runtime(tmp_path: Path) -> ProbeRuntime:
    session_dir = tmp_path / "session"
    session_dir.mkdir()
    return ProbeRuntime(RuntimeSpec(image="runtime:latest"), "session", session_dir)


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
