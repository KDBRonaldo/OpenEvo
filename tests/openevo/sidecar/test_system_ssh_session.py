from __future__ import annotations

from dataclasses import replace
import hashlib
import os
from pathlib import Path
import socket
import stat
import subprocess
import tempfile

import pytest

from desktop.sidecar import system_ssh_session as session_module
from desktop.sidecar.askpass_broker import ProcessIdentity
from desktop.sidecar.system_ssh_session import (
    AskpassHelperAuthority,
    OwnedSshMasterProcess,
    SystemOpenSshSession,
    SystemOpenSshSessionError,
    SystemOpenSshSessionOwner,
)
from openevo.deployment import SystemOpenSshAliasProfile
from openevo.deployment.ssh import (
    SystemOpenSshAskpassEnvironment,
    build_system_openssh_environment,
)
from openevo.deployment.system_executables import SSH_EXECUTABLE


@pytest.fixture
def short_tmp_path() -> Path:
    path = Path(tempfile.mkdtemp(prefix="oe-ss-", dir="/tmp"))
    path.chmod(0o700)
    try:
        yield path
    finally:
        for root, directories, files in os.walk(path, topdown=False):
            for filename in files:
                (Path(root) / filename).unlink(missing_ok=True)
            for directory in directories:
                candidate = Path(root) / directory
                if candidate.is_symlink():
                    candidate.unlink()
                else:
                    candidate.rmdir()
        path.rmdir()


def _profile(profile_id: str = "profile-1", alias: str = "evolab") -> SystemOpenSshAliasProfile:
    return SystemOpenSshAliasProfile(profile_id=profile_id, ssh_host_alias=alias)


class _FakeProcess(OwnedSshMasterProcess):
    def __init__(self, pid: int) -> None:
        self.pid = pid
        self.returncode: int | None = None
        self.terminate_calls = 0
        self.kill_calls = 0

    def poll(self) -> int | None:
        return self.returncode

    def wait(self, timeout: float | None = None) -> int:
        del timeout
        if self.returncode is None:
            raise subprocess.TimeoutExpired("ssh", 1.0)
        return self.returncode

    def terminate(self) -> None:
        self.terminate_calls += 1
        self.returncode = -15

    def kill(self) -> None:
        self.kill_calls += 1
        self.returncode = -9


class _Inspector:
    def __init__(self, identity: ProcessIdentity) -> None:
        self.identity = identity

    def inspect(self, process_id: int) -> ProcessIdentity | None:
        return self.identity if process_id == self.identity.process_id else None


class _Launcher:
    def __init__(self, process: _FakeProcess, inspector: _Inspector) -> None:
        self.process = process
        self.inspector = inspector
        self.argv: list[str] | None = None
        self.environment: dict[str, str] | None = None
        self.socket_path: Path | None = None
        self._socket: socket.socket | None = None

    def spawn(
        self,
        argv: list[str],
        *,
        environment: dict[str, str],
        control_path: Path,
    ) -> OwnedSshMasterProcess:
        self.argv = list(argv)
        self.environment = dict(environment)
        self.socket_path = control_path
        self._socket = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self._socket.bind(str(control_path))
        os.chmod(control_path, 0o600)
        return self.process

    def close_socket(self) -> None:
        if self._socket is not None:
            self._socket.close()
            self._socket = None


class _Runner:
    def __init__(self) -> None:
        self.calls: list[tuple[list[str], dict[str, str], float]] = []
        self.return_code = 0

    def __call__(
        self,
        argv: list[str],
        environment: dict[str, str],
        timeout_seconds: float,
    ) -> subprocess.CompletedProcess[bytes]:
        self.calls.append((list(argv), dict(environment), timeout_seconds))
        return subprocess.CompletedProcess(argv, self.return_code, b"", b"")


def _helper(tmp_path: Path) -> AskpassHelperAuthority:
    path = tmp_path / "openevo-ssh-askpass"
    path.write_bytes(b"sealed native helper")
    path.chmod(0o755)
    return AskpassHelperAuthority.open(
        path,
        expected_sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
    )


def _session(
    tmp_path: Path,
    *,
    generation: int = 1,
    process_id: int = 901,
    profile: SystemOpenSshAliasProfile | None = None,
) -> tuple[SystemOpenSshSession, _FakeProcess, _Inspector, _Launcher, _Runner]:
    identity = ProcessIdentity(
        process_id=process_id,
        parent_process_id=os.getpid(),
        process_group_id=os.getpgrp(),
        session_id=os.getsid(0),
        user_id=os.geteuid(),
        birth_identity=f"birth-{process_id}",
        executable_path=SSH_EXECUTABLE,
    )
    inspector = _Inspector(identity)
    process = _FakeProcess(process_id)
    launcher = _Launcher(process, inspector)
    runner = _Runner()
    session = SystemOpenSshSession(
        profile or _profile(),
        connection_generation=generation,
        askpass_helper=_helper(tmp_path),
        owns_askpass_helper=True,
        home=str(tmp_path),
        inherited_environment={"LANG": "en_US.UTF-8"},
        runtime_parent=tmp_path,
        inspector=inspector,
        launcher=launcher,
        runner=runner,
        startup_timeout_seconds=1.0,
        cleanup_timeout_seconds=0.2,
    )
    return session, process, inspector, launcher, runner


def test_session_owns_private_runtime_master_and_exact_socket_identity(
    short_tmp_path: Path,
) -> None:
    tmp_path = short_tmp_path
    session, process, _inspector, launcher, runner = _session(tmp_path)
    try:
        snapshot = session.start()
        runtime = snapshot.runtime_directory
        control = snapshot.control_path

        assert runtime.parent == tmp_path
        assert len(os.fsencode(control)) <= 103
        assert stat.S_IMODE(os.lstat(runtime).st_mode) == 0o700
        assert stat.S_ISSOCK(os.lstat(control).st_mode)
        assert snapshot.owner_process_id == process.pid
        assert snapshot.owner_birth_identity == f"birth-{process.pid}"
        assert snapshot.process_group_id == os.getpgrp()
        assert snapshot.control_socket_device == os.lstat(control).st_dev
        assert snapshot.control_socket_inode == os.lstat(control).st_ino
        assert snapshot.ssh_host_alias == "evolab"
        assert snapshot.connection_generation == 1
        assert launcher.argv is not None
        assert launcher.argv[0] == SSH_EXECUTABLE
        assert launcher.argv[1:4] == ["-M", "-S", str(control)]
        assert "ControlPersist=no" in launcher.argv
        assert "ClearAllForwardings=yes" in launcher.argv
        assert runner.calls[0][0][-3:] == ["-O", "check", "--", "evolab"][-3:]
    finally:
        session.close()
        launcher.close_socket()


def test_master_environment_is_closed_and_bound_to_one_broker_capability(
    short_tmp_path: Path,
) -> None:
    tmp_path = short_tmp_path
    session, _process, _inspector, launcher, _runner = _session(tmp_path, generation=9)
    try:
        session.start()
        assert launcher.environment is not None
        assert set(launcher.environment) == {
            "DISPLAY",
            "HOME",
            "LANG",
            "OPENEVO_SSH_ASKPASS_CAPABILITY",
            "OPENEVO_SSH_ASKPASS_SOCKET",
            "OPENEVO_SSH_CONNECTION_GENERATION",
            "PATH",
            "SSH_ASKPASS",
            "SSH_ASKPASS_REQUIRE",
        }
        assert launcher.environment["OPENEVO_SSH_CONNECTION_GENERATION"] == "9"
        assert launcher.environment["SSH_ASKPASS"] == str(session.askpass_helper.path)
        assert len(launcher.environment["OPENEVO_SSH_ASKPASS_CAPABILITY"]) == 64
    finally:
        session.close()
        launcher.close_socket()


def test_all_followers_reuse_only_the_exact_owned_socket(short_tmp_path: Path) -> None:
    tmp_path = short_tmp_path
    local = tmp_path / "workspace"
    local.mkdir()
    session, _process, _inspector, launcher, _runner = _session(tmp_path)
    try:
        snapshot = session.start()

        command = session.command_argv("true")
        upload = session.upload_argv(local_path=local, remote_path="/srv/workspace")
        tunnel = session.core_tunnel_argv(remote_port=8765)

        assert command[0:3] == [SSH_EXECUTABLE, "-S", str(snapshot.control_path)]
        remote_shell = upload[upload.index("-e") + 1]
        assert f"-S {snapshot.control_path}" in remote_shell
        assert tunnel[0:3] == [SSH_EXECUTABLE, "-S", str(snapshot.control_path)]
        assert "ControlMaster=no" in command
        assert "ControlMaster=no" in tunnel
    finally:
        session.close()
        launcher.close_socket()


def test_pid_reuse_or_control_socket_replacement_poison_the_session(
    short_tmp_path: Path,
) -> None:
    tmp_path = short_tmp_path
    session, _process, inspector, launcher, _runner = _session(tmp_path)
    snapshot = session.start()
    inspector.identity = replace(inspector.identity, birth_identity="reused")
    with pytest.raises(SystemOpenSshSessionError, match="process identity"):
        session.command_argv("true")
    assert session.poisoned
    with pytest.raises(SystemOpenSshSessionError, match="process identity"):
        session.close()
    launcher.close_socket()

    (tmp_path / "second").mkdir(mode=0o700)
    second, _process2, _inspector2, launcher2, _runner2 = _session(
        tmp_path / "second",
        process_id=902,
    )
    snapshot = second.start()
    launcher2.close_socket()
    snapshot.control_path.unlink()
    replacement = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    replacement.bind(str(snapshot.control_path))
    try:
        with pytest.raises(SystemOpenSshSessionError, match="socket identity"):
            second.command_argv("true")
        assert second.poisoned
    finally:
        with pytest.raises(SystemOpenSshSessionError, match="socket identity"):
            second.close()
        replacement.close()
        snapshot.control_path.unlink(missing_ok=True)


def test_child_exit_cancellation_and_close_have_bounded_cleanup(
    short_tmp_path: Path,
) -> None:
    tmp_path = short_tmp_path
    session, process, _inspector, launcher, _runner = _session(tmp_path)
    session.start()
    process.returncode = 255
    with pytest.raises(SystemOpenSshSessionError, match="master exited"):
        session.command_argv("true")
    session.cancel()
    assert session.cancelled
    session.close()
    launcher.close_socket()


def test_close_escalates_to_kill_when_graceful_exit_does_not_reap(
    short_tmp_path: Path,
) -> None:
    tmp_path = short_tmp_path
    session, process, _inspector, launcher, runner = _session(tmp_path)
    session.start()
    original_terminate = process.terminate

    def ignore_terminate() -> None:
        process.terminate_calls += 1

    process.terminate = ignore_terminate  # type: ignore[method-assign]
    runner.return_code = 255
    session.close()

    assert process.terminate_calls == 1
    assert process.kill_calls == 1
    process.terminate = original_terminate  # type: ignore[method-assign]
    launcher.close_socket()


def test_reconnect_never_adopts_an_ambient_master(short_tmp_path: Path) -> None:
    tmp_path = short_tmp_path
    created: list[tuple[SystemOpenSshSession, _Launcher]] = []

    def factory(
        profile: SystemOpenSshAliasProfile,
        generation: int,
    ) -> SystemOpenSshSession:
        root = tmp_path / f"g{generation}"
        root.mkdir(mode=0o700)
        session, _process, _inspector, launcher, _runner = _session(
            root,
            generation=generation,
            process_id=950 + generation,
            profile=profile,
        )
        created.append((session, launcher))
        return session

    owner = SystemOpenSshSessionOwner(factory)
    try:
        first = owner.connect(_profile(), connection_generation=1)
        second = owner.connect(_profile(), connection_generation=2)

        assert first.control_path != second.control_path
        assert first.connection_generation == 1
        assert second.connection_generation == 2
        assert created[0][0].closed
        assert created[0][1].argv is not None
        assert "-M" in created[0][1].argv
        assert created[1][1].argv is not None
        assert "-M" in created[1][1].argv
        assert "ControlMaster=auto" not in created[1][1].argv
        assert "ControlPersist=yes" not in created[1][1].argv
    finally:
        owner.close()
        for _session_value, launcher in created:
            launcher.close_socket()


def test_helper_authority_rejects_symlink_digest_drift_and_insecure_mode(
    short_tmp_path: Path,
) -> None:
    tmp_path = short_tmp_path
    helper = tmp_path / "helper"
    helper.write_bytes(b"helper")
    helper.chmod(0o755)
    digest = hashlib.sha256(b"helper").hexdigest()
    alias = tmp_path / "alias"
    alias.symlink_to(helper)

    with pytest.raises(SystemOpenSshSessionError):
        AskpassHelperAuthority.open(alias, expected_sha256=digest)
    helper.chmod(0o775)
    with pytest.raises(SystemOpenSshSessionError):
        AskpassHelperAuthority.open(helper, expected_sha256=digest)
    helper.chmod(0o755)
    with pytest.raises(SystemOpenSshSessionError):
        AskpassHelperAuthority.open(helper, expected_sha256="0" * 64)


def test_default_owner_launcher_execs_the_verified_system_ssh_image(
    short_tmp_path: Path,
) -> None:
    environment = build_system_openssh_environment(
        home=str(short_tmp_path),
        inherited={},
        askpass=SystemOpenSshAskpassEnvironment(
            helper_path=str(short_tmp_path / "helper"),
            broker_socket=str(short_tmp_path / "broker"),
            capability="a" * 64,
            connection_generation=1,
        ),
    )
    process = session_module._SystemSshMasterLauncher().spawn(
        [SSH_EXECUTABLE, "-V"],
        environment=environment,
        control_path=short_tmp_path / "unused",
    )

    assert process.wait(timeout=3.0) == 0
