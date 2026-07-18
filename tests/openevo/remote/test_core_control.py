from __future__ import annotations

from dataclasses import asdict
import errno
import json
import hashlib
import os
from pathlib import Path
import signal
import stat
import subprocess
import sys
from types import SimpleNamespace
import venv

import pytest
from pydantic import SecretStr

from openevo.deployment import core_control
from openevo.backend.service import CoreServiceError, CoreServiceErrorCode
from openevo.deployment.core_control import (
    CoreControlBootstrapError,
    CoreControlBootstrapErrorCode,
    build_core_control_bootstrap_plan,
    execute_core_control_bootstrap,
    open_core_control_tunnel,
    parse_core_control_attachment,
)
from openevo.deployment.preflight import RemoteCommandResult
from openevo.deployment.core_runtime import CorePythonRuntimeAuthority
from openevo.deployment.ssh import SshTransportError, SshTransportErrorCode


def _attachment_json(**updates: object) -> str:
    payload: dict[str, object] = {
        "schema_version": 1,
        "host": "127.0.0.1",
        "port": 8765,
        "release_identity": "a" * 64,
        "registry_digest": "b" * 64,
        "source_commit": "1" * 40,
        "generation": "c" * 32,
        "status_proof": "d" * 64,
        "attached": True,
        "bearer_token": "E" * 64,
        "execution_mode": "subscription",
        "capture_mode": "transcript",
    }
    payload.update(updates)
    return json.dumps(payload, separators=(",", ":"), sort_keys=True)


def _runtime() -> CorePythonRuntimeAuthority:
    values: dict[str, object] = {
        "schema_version": 1,
        "executable_path": "/home/user/.local/share/uv/python/python3.11",
        "executable_sha256": "a" * 64,
        "device": 1,
        "inode": 2,
        "uid": 1000,
        "mode": 0o755,
        "byte_size": 1024,
        "mtime_ns": 3,
        "ctime_ns": 4,
        "version": [3, 11, 12],
    }
    canonical = json.dumps(values, sort_keys=True, separators=(",", ":")).encode()
    authority_id = hashlib.sha256(b"openevo-core-python-runtime-v1\0" + canonical).hexdigest()
    return CorePythonRuntimeAuthority(
        authority_id=authority_id,
        executable_path=str(values["executable_path"]),
        executable_sha256=str(values["executable_sha256"]),
        device=1,
        inode=2,
        uid=1000,
        mode=0o755,
        byte_size=1024,
        mtime_ns=3,
        ctime_ns=4,
        version=(3, 11, 12),
    )


class FakeTunnel:
    base_url = "http://openevo-core.local"

    def __init__(self) -> None:
        self.closed = False
        self.authority_checks = 0

    def verify_authority(self) -> None:
        self.authority_checks += 1

    def close(self) -> None:
        self.closed = True


class FakeTransport:
    def __init__(self, stdout: str, *, return_codes: list[int] | None = None) -> None:
        self.stdout = stdout
        self.return_codes = list(return_codes or [])
        self.commands: list[str] = []
        self.timeouts: list[float] = []
        self.tunnel_kwargs: dict[str, object] = {}
        self.secret_commands: list[str] = []
        self.tunnel = FakeTunnel()

    def run(
        self,
        command: str,
        *,
        cwd: str | None = None,
        env: dict[str, str] | None = None,
        timeout_seconds: float = 30.0,
    ) -> RemoteCommandResult:
        del cwd, env
        self.commands.append(command)
        self.timeouts.append(timeout_seconds)
        return RemoteCommandResult(
            command=command,
            return_code=self.return_codes.pop(0) if self.return_codes else 0,
            stdout='{"bootstrapped":true,"schema_version":1}',
        )

    def open_core_tunnel(self, **kwargs: object) -> object:
        self.tunnel_kwargs = kwargs
        return self.tunnel

    def run_secret(self, command: str, *, timeout_seconds: float = 30.0) -> SecretStr:
        self.secret_commands.append(command)
        self.timeouts.append(timeout_seconds)
        return SecretStr(self.stdout)

    def upload_dir(self, local_path: str, remote_path: str) -> None:
        del local_path, remote_path


def test_core_bootstrap_uses_one_host_locked_command_and_secret_channel() -> None:
    compile(core_control._GENERATION_BOOTSTRAP, "<generation-bootstrap>", "exec")
    plan = build_core_control_bootstrap_plan(
        runtime=_runtime(),
        wheel_path="/home/user/upload/openevo.whl",
        framework_lock="/home/user/upload/framework-lock.json",
        service_root="/home/user/.openevo/core",
        source_commit="1" * 40,
    )
    transport = FakeTransport(_attachment_json())

    attachment = execute_core_control_bootstrap(plan, transport)

    assert plan.port == 0
    assert attachment.remote_host == "127.0.0.1"
    assert attachment.remote_port == 8765
    assert attachment.execution_mode == "subscription"
    assert attachment.capture_mode == "transcript"
    assert attachment.bearer_token == "E" * 64
    assert "bearer_token" not in repr(attachment)
    assert "E" * 64 not in repr(asdict(attachment))
    assert len(transport.commands) == 1
    assert "openevo.backend.service" in transport.commands[0]
    assert " bootstrap " in transport.commands[0]
    assert "--wheel-path" in transport.commands[0]
    assert "--port 0" in transport.commands[0]
    assert "PYTHONPATH" not in transport.commands[0]
    assert "--force-reinstall" not in transport.commands[0]
    assert " -I " in transport.commands[0]
    assert "/releases/" in transport.commands[0]
    assert len(transport.secret_commands) == 1
    assert "consume-attachment" in transport.secret_commands[0]
    assert "/releases/" in transport.secret_commands[0]
    assert " -I " in transport.secret_commands[0]
    assert "python3 -m openevo" not in transport.secret_commands[0]
    combined = " ".join(transport.commands)
    assert "gateway" not in combined
    assert "worker" not in combined
    assert "vllm" not in combined.lower()
    assert all(0 < timeout <= plan.deadline_seconds for timeout in transport.timeouts)


def test_generation_bootstrap_keeps_proxy_only_in_install_environment(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    install_root = tmp_path / "core" / "releases" / ("1" * 32)
    wheel = tmp_path / "openevo.whl"
    wheel.write_bytes(b"wheel")
    captured_install: dict[str, str] = {}
    captured_service: dict[str, str] = {}

    class FakeEnvBuilder:
        def __init__(self, **_kwargs: object) -> None:
            pass

        def create(self, root: Path) -> None:
            interpreter = Path(root) / "bin" / "python"
            interpreter.parent.mkdir(parents=True)
            interpreter.write_bytes(b"python")
            interpreter.chmod(0o700)

    def fake_run(*_args: object, **kwargs: object) -> SimpleNamespace:
        captured_install.update(kwargs["env"])
        return SimpleNamespace(returncode=0)

    class ExecveCalled(RuntimeError):
        pass

    def fake_execve(_path: object, _argv: object, env: dict[str, str]) -> None:
        captured_service.update(env)
        raise ExecveCalled

    monkeypatch.setattr(venv, "EnvBuilder", FakeEnvBuilder)
    monkeypatch.setattr(subprocess, "run", fake_run)
    monkeypatch.setattr(core_control.os, "execve", fake_execve)
    monkeypatch.setattr(sys, "executable", sys.executable)
    monkeypatch.setattr(sys, "_base_executable", sys._base_executable)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "generation-bootstrap",
            str(install_root),
            str(wheel),
            sys.executable,
            "bootstrap",
        ],
    )
    proxy = "http://proxy-user:proxy-password@127.0.0.1:7890"
    for name in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy"):
        monkeypatch.setenv(name, proxy)
    monkeypatch.setenv("NO_PROXY", "127.0.0.1")
    monkeypatch.setenv("SSL_CERT_FILE", "/private/proxy-ca.pem")

    with pytest.raises(ExecveCalled):
        exec(compile(core_control._GENERATION_BOOTSTRAP, "<bootstrap>", "exec"), {})

    assert captured_install["HTTPS_PROXY"] == proxy
    assert captured_install["SSL_CERT_FILE"] == "/private/proxy-ca.pem"
    assert captured_service
    assert not {
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "NO_PROXY",
        "http_proxy",
        "https_proxy",
        "no_proxy",
        "SSL_CERT_FILE",
        "SSL_CERT_DIR",
    }.intersection(captured_service)
    assert install_root.is_dir()
    assert list((tmp_path / "core" / "release-staging").iterdir()) == []


@pytest.mark.parametrize("failure_phase", ["venv", "pip", "enospc"])
def test_generation_bootstrap_cleans_failed_private_stage(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure_phase: str,
) -> None:
    generation = "2" * 32
    install_root = tmp_path / "core" / "releases" / generation
    wheel = tmp_path / "openevo.whl"
    wheel.write_bytes(b"wheel")

    class FakeEnvBuilder:
        def __init__(self, **_kwargs: object) -> None:
            pass

        def create(self, root: Path) -> None:
            partial = Path(root) / "partial"
            partial.write_bytes(b"partial generation")
            if failure_phase == "venv":
                raise RuntimeError("private venv failure")
            if failure_phase == "enospc":
                raise OSError(errno.ENOSPC, "private path")
            interpreter = Path(root) / "bin" / "python"
            interpreter.parent.mkdir()
            interpreter.write_bytes(b"python")
            interpreter.chmod(0o700)

    def fake_run(*args: object, **_kwargs: object) -> SimpleNamespace:
        command = args[0]
        is_pip = isinstance(command, list) and "pip" in command
        return SimpleNamespace(returncode=1 if failure_phase == "pip" and is_pip else 0)

    monkeypatch.setattr(venv, "EnvBuilder", FakeEnvBuilder)
    monkeypatch.setattr(subprocess, "run", fake_run)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "generation-bootstrap",
            str(install_root),
            str(wheel),
            sys.executable,
            "bootstrap",
        ],
    )

    with pytest.raises(SystemExit) as failed:
        exec(compile(core_control._GENERATION_BOOTSTRAP, "<bootstrap>", "exec"), {})

    assert failed.value.code == 73
    assert not install_root.exists()
    assert list((tmp_path / "core" / "release-staging").iterdir()) == []


def test_generation_bootstrap_retains_unsafe_cleanup_authority_and_fails_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    generation = "3" * 32
    install_root = tmp_path / "core" / "releases" / generation
    wheel = tmp_path / "openevo.whl"
    wheel.write_bytes(b"wheel")

    class UnsafeEnvBuilder:
        def __init__(self, **_kwargs: object) -> None:
            pass

        def create(self, root: Path) -> None:
            os.mkfifo(Path(root) / "unsafe", 0o600)
            raise OSError(errno.ENOSPC, "private path")

    monkeypatch.setattr(venv, "EnvBuilder", UnsafeEnvBuilder)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "generation-bootstrap",
            str(install_root),
            str(wheel),
            sys.executable,
            "bootstrap",
        ],
    )

    for _attempt in range(2):
        with pytest.raises(SystemExit) as failed:
            exec(compile(core_control._GENERATION_BOOTSTRAP, "<bootstrap>", "exec"), {})
        assert failed.value.code == 73

    stages = list((tmp_path / "core" / "release-staging").iterdir())
    assert len(stages) == 1
    stage = stages[0]
    assert stage.name.startswith(f"retiring-{generation}-")
    assert (stage / ".openevo-generation-authority").is_file()
    assert stat.S_ISFIFO(os.lstat(stage / "unsafe").st_mode)
    assert not install_root.exists()


def _generation_subprocess_source(
    *,
    install_root: Path,
    wheel: Path,
    kill_during_install: bool = False,
    fault: str | None = None,
) -> str:
    run_body = (
        "os.kill(os.getpid(), signal.SIGKILL)"
        if kill_during_install
        else "return types.SimpleNamespace(returncode=0)"
    )
    return f"""
import errno, os, pathlib, signal, subprocess, sys, types, venv
fault = {fault!r}
real_fsync = os.fsync
real_mkdir = os.mkdir
real_rmdir = os.rmdir
real_unlink = os.unlink
real_write = os.write
authority_fd = None
fault_fired = False
cleanup_faults = {{
    'before_retiring_fsync',
    'after_retiring_fsync',
    'before_clear_fsync',
    'after_clear_fsync',
    'before_discard_quarantine_fsync',
    'after_discard_quarantine_fsync',
    'before_discard_staging_fsync',
    'after_discard_staging_fsync',
    'after_authority_unlink',
    'before_empty_discard_fsync',
    'after_empty_discard_fsync',
    'before_discard_rmdir',
    'after_discard_rmdir',
}}
def kill():
    os.kill(os.getpid(), signal.SIGKILL)
def fd_path(fd):
    try:
        return pathlib.Path(os.readlink(f'/proc/self/fd/{{fd}}'))
    except OSError:
        return None
def fd_names(fd):
    try:
        return set(os.listdir(fd))
    except OSError:
        return set()
def fault_mkdir(path, *args, **kwargs):
    result = real_mkdir(path, *args, **kwargs)
    if fault == 'after_mkdir' and str(path).startswith('creating-'):
        kill()
    return result
def fault_write(fd, payload):
    global authority_fd, fault_fired
    if payload.startswith(b'openevo-core-generation-stage-v2'):
        authority_fd = fd
        if fault == 'authority_write' and not fault_fired:
            fault_fired = True
            raise OSError(errno.ENOSPC, 'authority write fault')
    result = real_write(fd, payload)
    if fault == 'after_authority_write' and fd == authority_fd:
        kill()
    return result
def fault_fsync(fd):
    global fault_fired
    path = fd_path(fd)
    name = path.name if path is not None else ''
    names = fd_names(fd)
    if fault == 'authority_fsync' and fd == authority_fd and not fault_fired:
        fault_fired = True
        raise OSError(errno.ENOSPC, 'authority fsync fault')
    before = (
        (fault == 'before_creating_fsync' and name == 'release-staging' and any(item.startswith('creating-') for item in names))
        or (fault == 'before_pending_fsync' and name == 'release-staging' and any(item.startswith('pending-') for item in names))
        or (fault == 'before_authority_dir_fsync' and name.startswith('pending-'))
        or (fault == 'before_active_fsync' and name == 'release-staging' and any(item.startswith('active-') for item in names))
        or (fault == 'before_install_stage_fsync' and name.startswith('active-') and 'bin' in names)
        or (fault == 'before_retiring_fsync' and name == 'release-staging' and any(item.startswith('retiring-') for item in names))
        or (fault == 'before_clear_fsync' and name.startswith('retiring-'))
        or (fault == 'before_discard_quarantine_fsync' and name == 'release-quarantine' and any(item.startswith('discard-') for item in names))
        or (fault == 'before_discard_staging_fsync' and name == 'release-staging' and not names and (path.parent / 'release-quarantine').is_dir())
        or (fault == 'before_empty_discard_fsync' and name.startswith('discard-') and '.openevo-generation-authority' not in names)
        or (fault == 'before_release_fsync' and name == 'releases' and {install_root.name!r} in names)
        or (fault == 'before_publish_staging_fsync' and name == 'release-staging' and not names and (path.parent / 'releases' / {install_root.name!r}).is_dir())
    )
    if before:
        kill()
    result = real_fsync(fd)
    after = (
        (fault == 'after_creating_fsync' and name == 'release-staging' and any(item.startswith('creating-') for item in names))
        or (fault == 'after_pending_fsync' and name == 'release-staging' and any(item.startswith('pending-') for item in names))
        or (fault == 'after_authority_file_fsync' and fd == authority_fd)
        or (fault == 'after_authority_dir_fsync' and name.startswith('pending-'))
        or (fault == 'after_active_fsync' and name == 'release-staging' and any(item.startswith('active-') for item in names))
        or (fault == 'after_install_stage_fsync' and name.startswith('active-') and 'bin' in names)
        or (fault == 'after_retiring_fsync' and name == 'release-staging' and any(item.startswith('retiring-') for item in names))
        or (fault == 'after_clear_fsync' and name.startswith('retiring-'))
        or (fault == 'after_discard_quarantine_fsync' and name == 'release-quarantine' and any(item.startswith('discard-') for item in names))
        or (fault == 'after_discard_staging_fsync' and name == 'release-staging' and not names and (path.parent / 'release-quarantine').is_dir())
        or (fault == 'after_empty_discard_fsync' and name.startswith('discard-') and '.openevo-generation-authority' not in names)
        or (fault == 'after_release_fsync' and name == 'releases' and {install_root.name!r} in names)
        or (fault == 'after_publish_staging_fsync' and name == 'release-staging' and not names and (path.parent / 'releases' / {install_root.name!r}).is_dir())
    )
    if after:
        kill()
    return result
def fault_unlink(path, *args, **kwargs):
    result = real_unlink(path, *args, **kwargs)
    if fault == 'after_authority_unlink' and path == '.openevo-generation-authority':
        kill()
    return result
def fault_rmdir(path, *args, **kwargs):
    if fault == 'before_discard_rmdir' and str(path).startswith('discard-'):
        kill()
    result = real_rmdir(path, *args, **kwargs)
    if fault == 'after_discard_rmdir' and str(path).startswith('discard-'):
        kill()
    return result
class FakeEnvBuilder:
    def __init__(self, **kwargs):
        pass
    def create(self, root):
        interpreter = pathlib.Path(root) / 'bin' / 'python'
        interpreter.parent.mkdir(parents=True)
        interpreter.write_bytes(b'python')
        interpreter.chmod(0o700)
def fake_run(*args, **kwargs):
    if fault in cleanup_faults:
        return types.SimpleNamespace(returncode=1)
    {run_body}
venv.EnvBuilder = FakeEnvBuilder
subprocess.run = fake_run
os.fsync = fault_fsync
os.mkdir = fault_mkdir
os.rmdir = fault_rmdir
os.unlink = fault_unlink
os.write = fault_write
os.execve = lambda *args: (_ for _ in ()).throw(SystemExit(0))
sys.argv = ['generation-bootstrap', {str(install_root)!r}, {str(wheel)!r}, sys.executable, 'bootstrap']
exec(compile({core_control._GENERATION_BOOTSTRAP!r}, '<bootstrap>', 'exec'), {{}})
"""


def test_generation_bootstrap_recovers_sigkill_residue_and_retries_successfully(
    tmp_path: Path,
) -> None:
    service_root = tmp_path / "core"
    crashed_generation = "4" * 32
    recovered_generation = "5" * 32
    wheel = tmp_path / "openevo.whl"
    wheel.write_bytes(b"wheel")
    crashed_root = service_root / "releases" / crashed_generation
    crashed = subprocess.run(
        [
            sys.executable,
            "-I",
            "-c",
            _generation_subprocess_source(
                install_root=crashed_root,
                wheel=wheel,
                kill_during_install=True,
            ),
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    assert crashed.returncode == -signal.SIGKILL
    stale_stages = list((service_root / "release-staging").iterdir())
    assert len(stale_stages) == 1
    stale_stage = stale_stages[0]
    assert stale_stage.name.startswith(f"active-{crashed_generation}-")
    assert (stale_stage / ".openevo-generation-authority").is_file()

    recovered_root = service_root / "releases" / recovered_generation
    recovered = subprocess.run(
        [
            sys.executable,
            "-I",
            "-c",
            _generation_subprocess_source(install_root=recovered_root, wheel=wheel),
        ],
        check=False,
        capture_output=True,
        text=True,
    )

    assert recovered.returncode == 0, recovered.stderr
    assert recovered_root.is_dir()
    assert not stale_stage.exists()
    assert list((service_root / "release-staging").iterdir()) == []


@pytest.mark.parametrize(
    ("fault", "residue_root", "residue_state", "quarantined"),
    [
        ("after_mkdir", "release-staging", "creating", True),
        ("after_authority_unlink", "release-quarantine", "discard", False),
    ],
)
def test_generation_bootstrap_recovers_exact_sigkill_windows(
    tmp_path: Path,
    fault: str,
    residue_root: str,
    residue_state: str,
    quarantined: bool,
) -> None:
    service_root = tmp_path / "core"
    crashed_generation = "9" * 32
    retried_generation = "a" * 32
    wheel = tmp_path / "openevo.whl"
    wheel.write_bytes(b"wheel")
    crashed = subprocess.run(
        [
            sys.executable,
            "-I",
            "-c",
            _generation_subprocess_source(
                install_root=service_root / "releases" / crashed_generation,
                wheel=wheel,
                fault=fault,
            ),
        ],
        check=False,
        capture_output=True,
        text=True,
    )

    assert crashed.returncode == -signal.SIGKILL, crashed.stderr
    residue = list((service_root / residue_root).iterdir())
    assert len(residue) == 1
    assert residue[0].name.startswith(f"{residue_state}-{crashed_generation}")
    if fault == "after_authority_unlink":
        assert not (residue[0] / ".openevo-generation-authority").exists()

    retried = subprocess.run(
        [
            sys.executable,
            "-I",
            "-c",
            _generation_subprocess_source(
                install_root=service_root / "releases" / retried_generation,
                wheel=wheel,
            ),
        ],
        check=False,
        capture_output=True,
        text=True,
    )

    assert retried.returncode == 0, retried.stderr
    assert (service_root / "releases" / retried_generation).is_dir()
    assert list((service_root / "release-staging").iterdir()) == []
    quarantine = list((service_root / "release-quarantine").iterdir())
    assert bool(quarantine) is quarantined
    if quarantine:
        assert quarantine[0].name.startswith(f"quarantined-{crashed_generation}-")


@pytest.mark.parametrize(
    "fault",
    [
        "after_mkdir",
        "before_creating_fsync",
        "after_creating_fsync",
        "before_pending_fsync",
        "after_pending_fsync",
        "after_authority_write",
        "after_authority_file_fsync",
        "before_authority_dir_fsync",
        "after_authority_dir_fsync",
        "before_active_fsync",
        "after_active_fsync",
        "before_install_stage_fsync",
        "after_install_stage_fsync",
        "before_release_fsync",
        "after_release_fsync",
        "before_publish_staging_fsync",
        "after_publish_staging_fsync",
        "before_retiring_fsync",
        "after_retiring_fsync",
        "before_clear_fsync",
        "after_clear_fsync",
        "before_discard_quarantine_fsync",
        "after_discard_quarantine_fsync",
        "before_discard_staging_fsync",
        "after_discard_staging_fsync",
        "after_authority_unlink",
        "before_empty_discard_fsync",
        "after_empty_discard_fsync",
        "before_discard_rmdir",
        "after_discard_rmdir",
    ],
)
def test_generation_bootstrap_recovers_every_persistence_boundary(
    tmp_path: Path,
    fault: str,
) -> None:
    service_root = tmp_path / "core"
    crashed_generation = "3" * 32
    retried_generation = "4" * 32
    wheel = tmp_path / "openevo.whl"
    wheel.write_bytes(b"wheel")
    crashed = subprocess.run(
        [
            sys.executable,
            "-I",
            "-c",
            _generation_subprocess_source(
                install_root=service_root / "releases" / crashed_generation,
                wheel=wheel,
                fault=fault,
            ),
        ],
        check=False,
        capture_output=True,
        text=True,
    )

    assert crashed.returncode == -signal.SIGKILL, (fault, crashed.stderr)

    retried = subprocess.run(
        [
            sys.executable,
            "-I",
            "-c",
            _generation_subprocess_source(
                install_root=service_root / "releases" / retried_generation,
                wheel=wheel,
            ),
        ],
        check=False,
        capture_output=True,
        text=True,
    )

    assert retried.returncode == 0, (fault, retried.stderr)
    assert (service_root / "releases" / retried_generation).is_dir()
    assert list((service_root / "release-staging").iterdir()) == []
    assert not any(
        path.name.startswith("discard-")
        for path in (service_root / "release-quarantine").iterdir()
    )


@pytest.mark.parametrize("fault", ["authority_write", "authority_fsync"])
def test_generation_bootstrap_recovers_authority_io_failure(
    tmp_path: Path,
    fault: str,
) -> None:
    service_root = tmp_path / "core"
    failed_generation = "b" * 32
    retried_generation = "c" * 32
    wheel = tmp_path / "openevo.whl"
    wheel.write_bytes(b"wheel")
    failed = subprocess.run(
        [
            sys.executable,
            "-I",
            "-c",
            _generation_subprocess_source(
                install_root=service_root / "releases" / failed_generation,
                wheel=wheel,
                fault=fault,
            ),
        ],
        check=False,
        capture_output=True,
        text=True,
    )

    assert failed.returncode == 73, failed.stderr
    assert list((service_root / "release-staging").iterdir()) == []

    retried = subprocess.run(
        [
            sys.executable,
            "-I",
            "-c",
            _generation_subprocess_source(
                install_root=service_root / "releases" / retried_generation,
                wheel=wheel,
            ),
        ],
        check=False,
        capture_output=True,
        text=True,
    )

    assert retried.returncode == 0, retried.stderr
    assert (service_root / "releases" / retried_generation).is_dir()
    assert list((service_root / "release-staging").iterdir()) == []


def test_generation_bootstrap_recovers_sigkill_before_tombstone_removal(
    tmp_path: Path,
) -> None:
    service_root = tmp_path / "core"
    crashed_generation = "f" * 32
    retried_generation = "0" * 32
    wheel = tmp_path / "openevo.whl"
    wheel.write_bytes(b"wheel")
    crashed = subprocess.run(
        [
            sys.executable,
            "-I",
            "-c",
            _generation_subprocess_source(
                install_root=service_root / "releases" / crashed_generation,
                wheel=wheel,
                fault="before_discard_rmdir",
            ),
        ],
        check=False,
        capture_output=True,
        text=True,
    )

    assert crashed.returncode == -signal.SIGKILL, crashed.stderr
    assert list((service_root / "release-staging").iterdir()) == []
    tombstones = list((service_root / "release-quarantine").iterdir())
    assert len(tombstones) == 1
    assert tombstones[0].name.startswith(f"discard-{crashed_generation}-")

    retried = subprocess.run(
        [
            sys.executable,
            "-I",
            "-c",
            _generation_subprocess_source(
                install_root=service_root / "releases" / retried_generation,
                wheel=wheel,
            ),
        ],
        check=False,
        capture_output=True,
        text=True,
    )

    assert retried.returncode == 0, retried.stderr
    assert (service_root / "releases" / retried_generation).is_dir()
    assert list((service_root / "release-staging").iterdir()) == []
    assert list((service_root / "release-quarantine").iterdir()) == []


def test_generation_bootstrap_quarantines_legacy_unauthorized_stage_without_deleting(
    tmp_path: Path,
) -> None:
    service_root = tmp_path / "core"
    legacy_generation = "d" * 32
    retried_generation = "e" * 32
    staging_root = _prepare_private_stage_root(service_root)
    legacy_stage = staging_root / f"staged-{legacy_generation}"
    legacy_stage.mkdir(mode=0o700)
    sentinel = legacy_stage / "foreign-sentinel"
    sentinel.write_bytes(b"preserve me")
    wheel = tmp_path / "openevo.whl"
    wheel.write_bytes(b"wheel")

    retried = subprocess.run(
        [
            sys.executable,
            "-I",
            "-c",
            _generation_subprocess_source(
                install_root=service_root / "releases" / retried_generation,
                wheel=wheel,
            ),
        ],
        check=False,
        capture_output=True,
        text=True,
    )

    assert retried.returncode == 0, retried.stderr
    assert list((service_root / "release-staging").iterdir()) == []
    quarantined = list((service_root / "release-quarantine").iterdir())
    assert len(quarantined) == 1
    assert quarantined[0].name.startswith(f"quarantined-{legacy_generation}-")
    assert (quarantined[0] / sentinel.name).read_bytes() == b"preserve me"


@pytest.mark.parametrize("state", ["pending", "retiring"])
def test_generation_bootstrap_never_claims_authority_free_bound_stage(
    tmp_path: Path,
    state: str,
) -> None:
    service_root = tmp_path / "core"
    foreign_generation = "1" * 32
    retried_generation = "2" * 32
    staging_root = _prepare_private_stage_root(service_root)
    foreign_stage = staging_root / "unbound"
    foreign_stage.mkdir(mode=0o700)
    metadata = foreign_stage.stat()
    bound_name = f"{state}-{foreign_generation}-{metadata.st_dev:x}-{metadata.st_ino:x}"
    foreign_stage = foreign_stage.rename(staging_root / bound_name)
    sentinel = foreign_stage / "foreign-sentinel"
    sentinel.write_bytes(b"preserve me")
    wheel = tmp_path / "openevo.whl"
    wheel.write_bytes(b"wheel")

    retried = subprocess.run(
        [
            sys.executable,
            "-I",
            "-c",
            _generation_subprocess_source(
                install_root=service_root / "releases" / retried_generation,
                wheel=wheel,
            ),
        ],
        check=False,
        capture_output=True,
        text=True,
    )

    assert retried.returncode == 0, retried.stderr
    assert list(staging_root.iterdir()) == []
    quarantined = list((service_root / "release-quarantine").iterdir())
    assert len(quarantined) == 1
    assert quarantined[0].name.startswith(f"quarantined-{foreign_generation}-")
    assert (quarantined[0] / sentinel.name).read_bytes() == b"preserve me"


def _create_bound_stage(
    service_root: Path,
    *,
    generation: str,
    state: str,
) -> Path:
    staging_root = _prepare_private_stage_root(service_root)
    stage = staging_root / "unbound"
    stage.mkdir(mode=0o700)
    metadata = stage.stat()
    return stage.rename(
        staging_root / f"{state}-{generation}-{metadata.st_dev:x}-{metadata.st_ino:x}"
    )


def _prepare_private_stage_root(service_root: Path) -> Path:
    service_root.mkdir(mode=0o700, parents=True, exist_ok=True)
    service_root.chmod(0o700)
    staging_root = service_root / "release-staging"
    staging_root.mkdir(mode=0o700, exist_ok=True)
    staging_root.chmod(0o700)
    return staging_root


def _write_stage_authority(stage: Path, generation: str) -> Path:
    metadata = stage.stat()
    authority = stage / ".openevo-generation-authority"
    authority.write_text(
        f"openevo-core-generation-stage-v2\n{generation}\n{metadata.st_dev}:{metadata.st_ino}\n",
        encoding="ascii",
    )
    authority.chmod(0o600)
    return authority


@pytest.mark.parametrize(
    "corruption",
    [
        "stage_symlink",
        "stage_mode",
        "stage_identity",
        "authority_symlink",
        "authority_mode",
        "authority_identity",
        "authority_owner",
    ],
)
def test_generation_bootstrap_rejects_untrusted_active_stage_without_deleting(
    tmp_path: Path,
    corruption: str,
) -> None:
    service_root = tmp_path / "core"
    generation = "5" * 32
    retry_generation = "6" * 32
    external = tmp_path / "external"
    external.mkdir()
    external_sentinel = external / "sentinel"
    external_sentinel.write_bytes(b"preserve external")
    staging_root = service_root / "release-staging"

    if corruption == "stage_symlink":
        staging_root.mkdir(mode=0o700, parents=True)
        stage = staging_root / f"active-{generation}-1-1"
        stage.symlink_to(external, target_is_directory=True)
        preserved = external_sentinel
    else:
        stage = _create_bound_stage(
            service_root,
            generation=generation,
            state="active",
        )
        sentinel = stage / "foreign-sentinel"
        sentinel.write_bytes(b"preserve stage")
        preserved = sentinel
        authority = _write_stage_authority(stage, generation)
        if corruption == "stage_mode":
            stage.chmod(0o755)
        elif corruption == "stage_identity":
            metadata = stage.stat()
            stage = stage.rename(
                staging_root / f"active-{generation}-{metadata.st_dev:x}-{metadata.st_ino + 1:x}"
            )
            preserved = stage / sentinel.name
        elif corruption == "authority_symlink":
            authority.unlink()
            authority.symlink_to(external_sentinel)
        elif corruption == "authority_mode":
            authority.chmod(0o644)
        elif corruption == "authority_identity":
            authority.write_text(
                f"openevo-core-generation-stage-v2\n{'7' * 32}\n1:1\n",
                encoding="ascii",
            )
        elif corruption == "authority_owner":
            if os.geteuid() != 0:
                pytest.skip("changing authority ownership requires root")
            os.chown(authority, 1, 1)

    wheel = tmp_path / "openevo.whl"
    wheel.write_bytes(b"wheel")
    retried = subprocess.run(
        [
            sys.executable,
            "-I",
            "-c",
            _generation_subprocess_source(
                install_root=service_root / "releases" / retry_generation,
                wheel=wheel,
            ),
        ],
        check=False,
        capture_output=True,
        text=True,
    )

    assert retried.returncode == 73, (corruption, retried.stderr)
    assert preserved.read_bytes().startswith(b"preserve")
    assert not (service_root / "releases" / retry_generation).exists()


def test_generation_bootstrap_unlinks_stage_symlink_without_following_target(
    tmp_path: Path,
) -> None:
    service_root = tmp_path / "core"
    stale_generation = "7" * 32
    retry_generation = "8" * 32
    stage = _create_bound_stage(
        service_root,
        generation=stale_generation,
        state="active",
    )
    _write_stage_authority(stage, stale_generation)
    external = tmp_path / "external-sentinel"
    external.write_bytes(b"preserve external")
    (stage / "external-link").symlink_to(external)
    wheel = tmp_path / "openevo.whl"
    wheel.write_bytes(b"wheel")

    retried = subprocess.run(
        [
            sys.executable,
            "-I",
            "-c",
            _generation_subprocess_source(
                install_root=service_root / "releases" / retry_generation,
                wheel=wheel,
            ),
        ],
        check=False,
        capture_output=True,
        text=True,
    )

    assert retried.returncode == 0, retried.stderr
    assert external.read_bytes() == b"preserve external"
    assert list((service_root / "release-staging").iterdir()) == []


def test_generation_bootstrap_never_traverses_authority_free_tombstone(
    tmp_path: Path,
) -> None:
    service_root = tmp_path / "core"
    stale_generation = "9" * 32
    retry_generation = "a" * 32
    quarantine_root = service_root / "release-quarantine"
    tombstone = quarantine_root / "unbound"
    tombstone.mkdir(mode=0o700, parents=True)
    metadata = tombstone.stat()
    tombstone = tombstone.rename(
        quarantine_root
        / (f"discard-{stale_generation}-{metadata.st_dev:x}-{metadata.st_ino:x}-{'b' * 32}")
    )
    sentinel = tombstone / "foreign-sentinel"
    sentinel.write_bytes(b"preserve tombstone")
    wheel = tmp_path / "openevo.whl"
    wheel.write_bytes(b"wheel")

    retried = subprocess.run(
        [
            sys.executable,
            "-I",
            "-c",
            _generation_subprocess_source(
                install_root=service_root / "releases" / retry_generation,
                wheel=wheel,
            ),
        ],
        check=False,
        capture_output=True,
        text=True,
    )

    assert retried.returncode == 73, retried.stderr
    assert sentinel.read_bytes() == b"preserve tombstone"
    assert not (service_root / "releases" / retry_generation).exists()


def test_generation_bootstrap_serializes_concurrent_publications(tmp_path: Path) -> None:
    service_root = tmp_path / "core"
    wheel = tmp_path / "openevo.whl"
    wheel.write_bytes(b"wheel")
    generations = ("6" * 32, "7" * 32)
    processes = [
        subprocess.Popen(
            [
                sys.executable,
                "-I",
                "-c",
                _generation_subprocess_source(
                    install_root=service_root / "releases" / generation,
                    wheel=wheel,
                ),
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        for generation in generations
    ]

    results = [process.communicate(timeout=20) for process in processes]

    assert [process.returncode for process in processes] == [0, 0], results
    assert {path.name for path in (service_root / "releases").iterdir()} == set(generations)
    assert list((service_root / "release-staging").iterdir()) == []


def test_generation_bootstrap_rejects_service_root_replacement(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    generation = "8" * 32
    service_root = tmp_path / "core"
    displaced = tmp_path / "displaced-core"
    install_root = service_root / "releases" / generation
    wheel = tmp_path / "openevo.whl"
    wheel.write_bytes(b"wheel")

    class FakeEnvBuilder:
        def __init__(self, **_kwargs: object) -> None:
            pass

        def create(self, root: Path) -> None:
            interpreter = Path(root) / "bin" / "python"
            interpreter.parent.mkdir(parents=True)
            interpreter.write_bytes(b"python")
            interpreter.chmod(0o700)

    replaced = False

    def replace_root(*_args: object, **_kwargs: object) -> SimpleNamespace:
        nonlocal replaced
        if not replaced:
            replaced = True
            service_root.rename(displaced)
            service_root.mkdir(mode=0o700)
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(venv, "EnvBuilder", FakeEnvBuilder)
    monkeypatch.setattr(subprocess, "run", replace_root)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "generation-bootstrap",
            str(install_root),
            str(wheel),
            sys.executable,
            "bootstrap",
        ],
    )

    with pytest.raises(SystemExit) as failed:
        exec(compile(core_control._GENERATION_BOOTSTRAP, "<bootstrap>", "exec"), {})

    assert failed.value.code == 73
    assert replaced is True
    assert not install_root.exists()
    assert list((displaced / "release-staging").iterdir()) == []


def test_core_bootstrap_rejects_pathsep_and_non_closed_remote_paths() -> None:
    for path in (
        f"/home/user/upload{os.pathsep}/tmp/openevo.whl",
        "/home/user/upload/openevo wheel.whl",
        "/home/user/upload/openevo\\wheel.whl",
    ):
        with pytest.raises(CoreControlBootstrapError) as exc_info:
            build_core_control_bootstrap_plan(
                runtime=_runtime(),
                wheel_path=path,
                framework_lock="/home/user/upload/framework-lock.json",
                service_root="/home/user/.openevo/core",
                source_commit="1" * 40,
            )
        assert exc_info.value.code is CoreControlBootstrapErrorCode.INVALID_PLAN


@pytest.mark.parametrize("secret_phase", [False, True])
def test_core_bootstrap_preserves_ssh_timeout_as_retryable_deadline(
    secret_phase: bool,
) -> None:
    plan = build_core_control_bootstrap_plan(
        runtime=_runtime(),
        wheel_path="/home/user/upload/openevo.whl",
        framework_lock="/home/user/upload/framework-lock.json",
        service_root="/home/user/.openevo/core",
        source_commit="1" * 40,
    )

    class TimeoutTransport(FakeTransport):
        def run(self, command: str, **kwargs: object) -> RemoteCommandResult:
            if not secret_phase:
                raise SshTransportError(SshTransportErrorCode.TIMEOUT)
            return super().run(command, **kwargs)

        def run_secret(self, command: str, **kwargs: object) -> SecretStr:
            if secret_phase:
                raise SshTransportError(SshTransportErrorCode.TIMEOUT)
            return super().run_secret(command, **kwargs)

    with pytest.raises(CoreControlBootstrapError) as exc_info:
        execute_core_control_bootstrap(plan, TimeoutTransport(_attachment_json()))

    assert exc_info.value.code is CoreControlBootstrapErrorCode.DEADLINE_EXCEEDED
    assert exc_info.value.retryable is True


def test_core_bootstrap_maps_generation_install_exit_without_private_output() -> None:
    plan = build_core_control_bootstrap_plan(
        runtime=_runtime(),
        wheel_path="/secret/upload/openevo.whl",
        framework_lock="/secret/upload/framework-lock.json",
        service_root="/home/user/.openevo/core",
        source_commit="1" * 40,
    )
    transport = FakeTransport("private proxy credential", return_codes=[73])

    with pytest.raises(CoreControlBootstrapError) as failed:
        execute_core_control_bootstrap(plan, transport)

    assert failed.value.code is CoreControlBootstrapErrorCode.INSTALL_FAILED
    assert failed.value.retryable is True
    assert "installed" in str(failed.value)
    assert "secret" not in str(failed.value)
    assert "proxy" not in str(failed.value)
    assert transport.secret_commands == []


def test_bootstrap_does_not_expose_bearer_in_normal_command_result() -> None:
    plan = build_core_control_bootstrap_plan(
        runtime=_runtime(),
        wheel_path="/home/user/upload/openevo.whl",
        framework_lock="/home/user/upload/framework-lock.json",
        service_root="/home/user/.openevo/core",
        source_commit="1" * 40,
    )
    transport = FakeTransport(_attachment_json())

    execute_core_control_bootstrap(plan, transport)

    assert len(transport.commands) == 1
    assert transport.stdout not in repr(transport.run(transport.commands[0]))
    assert "E" * 64 not in " ".join(transport.commands)


def test_core_bootstrap_parser_rejects_duplicate_oversized_and_bad_bearer() -> None:
    duplicate = _attachment_json()[:-1] + ',"port":9999}'
    for payload in (duplicate, "x" * 5000, _attachment_json(bearer_token="short")):
        with pytest.raises(CoreControlBootstrapError) as exc_info:
            parse_core_control_attachment(SecretStr(payload))
        assert exc_info.value.code is CoreControlBootstrapErrorCode.RESPONSE_INVALID
        assert "short" not in str(exc_info.value)


def test_core_bootstrap_failure_does_not_expose_command_or_paths() -> None:
    plan = build_core_control_bootstrap_plan(
        runtime=_runtime(),
        wheel_path="/secret/upload/openevo.whl",
        framework_lock="/secret/upload/framework-lock.json",
        service_root="/secret/home/.openevo/core",
        source_commit="1" * 40,
    )

    class FailingTransport(FakeTransport):
        def run(self, command: str, **kwargs: object) -> RemoteCommandResult:
            del kwargs
            return RemoteCommandResult(
                command=command,
                return_code=1,
                stderr="Authorization: Bearer super-secret /secret/home",
            )

    with pytest.raises(CoreControlBootstrapError) as exc_info:
        execute_core_control_bootstrap(plan, FailingTransport(""))
    rendered = str(exc_info.value)
    assert "super-secret" not in rendered
    assert "/secret" not in rendered
    assert "pip" not in rendered


def test_tunnel_authenticates_generation_and_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    attachment = parse_core_control_attachment(SecretStr(_attachment_json()))
    transport = FakeTransport("")
    calls: list[dict[str, object]] = []

    def authenticate(**kwargs: object) -> str:
        calls.append(kwargs)
        return attachment.status_proof

    monkeypatch.setattr(core_control, "authenticate_core_service_endpoint", authenticate)
    tunnel = open_core_control_tunnel(attachment, transport)
    assert tunnel.base_url == transport.tunnel.base_url
    assert tunnel.generation == attachment.generation
    assert tunnel.bearer_token == attachment.bearer_token
    assert "E" * 64 not in repr(tunnel)
    assert calls[0]["generation"] == attachment.generation
    assert calls[0]["release_identity"] == attachment.release_identity
    assert calls[0]["registry_digest"] == attachment.registry_digest
    assert calls[0]["endpoint"] is transport.tunnel
    assert transport.tunnel.authority_checks == 1
    assert transport.tunnel_kwargs == {
        "remote_port": 8765,
        "remote_host": "127.0.0.1",
        "wait_for_ready": True,
        "timeout_seconds": 10.0,
    }


def test_tunnel_rejects_restarted_or_wrong_core_and_closes_forward(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    attachment = parse_core_control_attachment(SecretStr(_attachment_json()))
    transport = FakeTransport("")
    monkeypatch.setattr(
        core_control,
        "authenticate_core_service_endpoint",
        lambda **_kwargs: "0" * 64,
    )

    with pytest.raises(CoreControlBootstrapError) as exc_info:
        open_core_control_tunnel(attachment, transport)

    assert exc_info.value.code is CoreControlBootstrapErrorCode.RESPONSE_INVALID
    assert transport.tunnel.closed is True
    assert "E" * 64 not in repr(exc_info.value)


def test_tunnel_rejects_base_url_not_bound_to_verified_local_endpoint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    attachment = parse_core_control_attachment(SecretStr(_attachment_json()))
    transport = FakeTransport("")
    transport.tunnel.base_url = "http://127.0.0.1:43124"
    monkeypatch.setattr(
        core_control,
        "authenticate_core_service_endpoint",
        lambda **_kwargs: attachment.status_proof,
    )

    with pytest.raises(CoreControlBootstrapError) as exc_info:
        open_core_control_tunnel(attachment, transport)

    assert exc_info.value.code is CoreControlBootstrapErrorCode.RESPONSE_INVALID
    assert transport.tunnel.closed is True


def test_tunnel_open_preserves_ssh_timeout_as_retryable_deadline() -> None:
    attachment = parse_core_control_attachment(SecretStr(_attachment_json()))

    class TimeoutTransport(FakeTransport):
        def open_core_tunnel(self, **kwargs: object) -> object:
            del kwargs
            raise SshTransportError(SshTransportErrorCode.TIMEOUT)

    with pytest.raises(CoreControlBootstrapError) as exc_info:
        open_core_control_tunnel(attachment, TimeoutTransport(""))

    assert exc_info.value.code is CoreControlBootstrapErrorCode.DEADLINE_EXCEEDED
    assert exc_info.value.retryable is True


@pytest.mark.parametrize(
    ("failure", "expected_code"),
    [
        (
            CoreServiceError(
                CoreServiceErrorCode.DEADLINE_EXCEEDED,
                "deadline",
                retryable=True,
            ),
            CoreControlBootstrapErrorCode.DEADLINE_EXCEEDED,
        ),
        (RuntimeError("ssh daemon exited"), CoreControlBootstrapErrorCode.SERVICE_FAILED),
    ],
)
def test_tunnel_authentication_preserves_retryable_failures_and_closes(
    monkeypatch: pytest.MonkeyPatch,
    failure: BaseException,
    expected_code: CoreControlBootstrapErrorCode,
) -> None:
    attachment = parse_core_control_attachment(SecretStr(_attachment_json()))
    transport = FakeTransport("")

    def fail_authenticate(**_kwargs: object) -> str:
        raise failure

    monkeypatch.setattr(core_control, "authenticate_core_service_endpoint", fail_authenticate)

    with pytest.raises(CoreControlBootstrapError) as exc_info:
        open_core_control_tunnel(attachment, transport)

    assert exc_info.value.code is expected_code
    assert exc_info.value.retryable is True
    assert transport.tunnel.closed is True


def test_tunnel_authentication_base_exception_closes_before_propagation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    attachment = parse_core_control_attachment(SecretStr(_attachment_json()))
    transport = FakeTransport("")

    class Cancelled(BaseException):
        pass

    def cancel(**_kwargs: object) -> str:
        raise Cancelled

    monkeypatch.setattr(core_control, "authenticate_core_service_endpoint", cancel)

    with pytest.raises(Cancelled):
        open_core_control_tunnel(attachment, transport)

    assert transport.tunnel.closed is True
    assert sys.exc_info() == (None, None, None)
