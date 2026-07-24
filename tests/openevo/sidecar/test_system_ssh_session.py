from __future__ import annotations

import base64
from dataclasses import replace
import hashlib
import io
import os
from pathlib import Path
from types import SimpleNamespace
import socket
import stat
import struct
import subprocess
import tempfile

import pytest

from desktop.sidecar import system_ssh_session as session_module
from desktop.sidecar.askpass_broker import AskpassPromptObservation, ProcessIdentity
from desktop.sidecar.system_ssh_session import (
    AskpassHelperAuthority,
    OwnedSshMasterProcess,
    SystemOpenSshHostTrust,
    SystemOpenSshSession,
    SystemOpenSshSessionError,
    SystemOpenSshSessionOwner,
    SystemOpenSshSessionSnapshot,
)
from openevo.deployment import SystemOpenSshAliasProfile
from openevo.deployment.host_keys import (
    PendingSystemHostKeyReview,
    SystemHostKeyReviewAuthority,
    classify_system_openssh_host_key_failure,
    inspect_system_known_hosts_policy,
)
from openevo.deployment.ssh import (
    SystemOpenSshAskpassEnvironment,
    build_system_openssh_environment,
)
from openevo.deployment.system_executables import (
    SSH_EXECUTABLE,
    SSH_KEYGEN_EXECUTABLE,
    SYSTEM_OPENSSH_OWNER_ARGUMENT,
)


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


def test_default_runtime_parent_avoids_the_macos_tmp_symlink() -> None:
    assert session_module._default_runtime_parent_for_platform("darwin") == Path(
        "/private/tmp"
    )
    assert session_module._default_runtime_parent_for_platform("linux") == Path("/tmp")


class _FakeProcess(OwnedSshMasterProcess):
    def __init__(self, pid: int, *, failure_stderr: bytes = b"") -> None:
        self.pid = pid
        self.returncode: int | None = None
        self.terminate_calls = 0
        self.kill_calls = 0
        self._failure_stderr = failure_stderr

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

    def captured_stderr(self) -> bytes:
        return self._failure_stderr


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


def _system_known_hosts(tmp_path: Path) -> Path:
    ssh_directory = tmp_path / ".ssh"
    ssh_directory.mkdir(mode=0o700, exist_ok=True)
    known_hosts = ssh_directory / "known_hosts"
    known_hosts.write_text("gpu.internal ssh-ed25519 AAAATEST\n", encoding="ascii")
    known_hosts.chmod(0o600)
    return known_hosts


def _valid_ed25519_public_key(marker: bytes) -> str:
    key_type = b"ssh-ed25519"
    key_data = hashlib.sha256(marker).digest()
    blob = b"".join(
        struct.pack(">I", len(field)) + field for field in (key_type, key_data)
    )
    return f"ssh-ed25519 {base64.b64encode(blob).decode('ascii')}"


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
    owner_path = short_tmp_path / "openevo-ssh-owner-fixture"
    owner_path.write_text(
        "#!/bin/sh\n"
        f"[ \"$1\" = \"{SYSTEM_OPENSSH_OWNER_ARGUMENT}\" ] || exit 126\n"
        "shift 2\n"
        "exec \"$@\"\n",
        encoding="ascii",
    )
    owner_path.chmod(0o755)
    owner = AskpassHelperAuthority.open(
        owner_path,
        expected_sha256=hashlib.sha256(owner_path.read_bytes()).hexdigest(),
    )
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
    process = session_module._SystemSshMasterLauncher(owner).spawn(
        [SSH_EXECUTABLE, "-V"],
        environment=environment,
        control_path=short_tmp_path / "unused",
    )

    assert process.wait(timeout=3.0) == 0


def test_native_helper_owner_launcher_bypasses_the_frozen_python_archive(
    short_tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    helper = _helper(short_tmp_path)
    observed: dict[str, object] = {}

    class SpawnedProcess:
        pid = 902
        returncode: int | None = 0
        stderr = io.BytesIO()

        def poll(self) -> int | None:
            return self.returncode

        def wait(self, timeout: float | None = None) -> int:
            del timeout
            assert self.returncode is not None
            return self.returncode

        def terminate(self) -> None:
            self.returncode = -15

        def kill(self) -> None:
            self.returncode = -9

    def popen(argv: list[str], **options: object) -> SpawnedProcess:
        observed["argv"] = list(argv)
        observed.update(options)
        return SpawnedProcess()

    monkeypatch.setattr(session_module.subprocess, "Popen", popen)
    environment = build_system_openssh_environment(
        home=str(short_tmp_path),
        inherited={},
        askpass=SystemOpenSshAskpassEnvironment(
            helper_path=str(helper.path),
            broker_socket=str(short_tmp_path / "broker"),
            capability="a" * 64,
            connection_generation=1,
        ),
    )

    process = session_module._SystemSshMasterLauncher(helper).spawn(
        [SSH_EXECUTABLE, "-V"],
        environment=environment,
        control_path=short_tmp_path / "unused",
    )

    argv = observed["argv"]
    assert isinstance(argv, list)
    assert argv[:2] == [str(helper.path), SYSTEM_OPENSSH_OWNER_ARGUMENT]
    assert argv[2].isascii() and argv[2].isdecimal()
    assert argv[3:] == [SSH_EXECUTABLE, "-V"]
    assert observed["executable"] == str(helper.path)
    assert observed["env"] == environment
    assert observed["pass_fds"] == (int(argv[2]),)
    assert observed["start_new_session"] is False
    assert process.wait(timeout=1.0) == 0


def test_changed_host_key_failure_issues_path_free_review_from_effective_config(
    short_tmp_path: Path,
) -> None:
    known_hosts = _system_known_hosts(short_tmp_path)
    stderr = (
        "WARNING: REMOTE HOST IDENTIFICATION HAS CHANGED!\n"
        "The fingerprint for the ED25519 key sent by the remote host is\n"
        f"SHA256:{'A' * 43}.\n"
        f"Offending ED25519 key in {known_hosts}:1\n"
        "Host key verification failed.\n"
    ).encode()
    runner = _Runner()
    runner_results = [
        subprocess.CompletedProcess(
            [SSH_EXECUTABLE, "-G", "--", "evolab"],
            0,
            (
                "hostname gpu.internal\n"
                "port 22\n"
                "canonicalizehostname false\n"
                "hashknownhosts no\n"
                f"userknownhostsfile {known_hosts}\n"
                "globalknownhostsfile /etc/ssh/ssh_known_hosts\n"
            ).encode(),
            b"",
        )
    ]

    def run(
        argv: list[str], environment: dict[str, str], timeout_seconds: float
    ) -> subprocess.CompletedProcess[bytes]:
        runner.calls.append((list(argv), dict(environment), timeout_seconds))
        return runner_results.pop(0)

    trust = SystemOpenSshHostTrust(
        home=short_tmp_path,
        inherited_environment={"LANG": "en_US.UTF-8"},
        review_authority=SystemHostKeyReviewAuthority(hmac_key=b"t" * 32),
        runner=run,
    )
    failure = trust.evaluate_failure(_profile(), connection_generation=4, stderr=stderr)

    assert failure.code == "ssh_host_key_changed"
    assert failure.review is not None
    assert failure.review.repair_support == "automatic_replacement_available"
    assert str(known_hosts) not in repr(failure)
    assert runner.calls[0][0] == [SSH_EXECUTABLE, "-G", "--", "evolab"]
    assert set(runner.calls[0][1]) == {"HOME", "LANG", "PATH"}


def test_conditional_config_never_runs_ssh_g_and_requires_administrator(
    short_tmp_path: Path,
) -> None:
    known_hosts = _system_known_hosts(short_tmp_path)
    stderr = (
        "WARNING: REMOTE HOST IDENTIFICATION HAS CHANGED!\n"
        "The fingerprint for the ED25519 key sent by the remote host is\n"
        f"SHA256:{'B' * 43}.\n"
        f"Offending ED25519 key in {known_hosts}:1\n"
        "Host key verification failed.\n"
    ).encode()
    runner = _Runner()
    trust = SystemOpenSshHostTrust(
        home=short_tmp_path,
        inherited_environment={},
        runner=runner,
    )

    failure = trust.evaluate_failure(
        _profile(),
        connection_generation=5,
        stderr=stderr,
        conditional_config=True,
    )

    assert runner.calls == []
    assert failure.review is not None
    assert failure.review.repair_support == "administrator_required"


def test_review_replacement_uses_exact_verified_keygen_action(
    short_tmp_path: Path,
) -> None:
    known_hosts = _system_known_hosts(short_tmp_path)
    stderr = (
        "WARNING: REMOTE HOST IDENTIFICATION HAS CHANGED!\n"
        "The fingerprint for the ED25519 key sent by the remote host is\n"
        f"SHA256:{'C' * 43}.\n"
        f"Offending ED25519 key in {known_hosts}:1\n"
        "Host key verification failed.\n"
    ).encode()
    calls: list[tuple[list[str], dict[str, str], float]] = []

    def run(
        argv: list[str], environment: dict[str, str], timeout_seconds: float
    ) -> subprocess.CompletedProcess[bytes]:
        calls.append((list(argv), dict(environment), timeout_seconds))
        if argv[0] == SSH_EXECUTABLE:
            return subprocess.CompletedProcess(
                argv,
                0,
                (
                    "hostname gpu.internal\n"
                    "port 22\n"
                    "canonicalizehostname false\n"
                    "hashknownhosts no\n"
                    f"userknownhostsfile {known_hosts}\n"
                    "globalknownhostsfile /etc/ssh/ssh_known_hosts\n"
                ).encode(),
                b"",
            )
        known_hosts.write_text("", encoding="ascii")
        return subprocess.CompletedProcess(argv, 0, b"", b"")

    trust = SystemOpenSshHostTrust(
        home=short_tmp_path,
        inherited_environment={},
        review_authority=SystemHostKeyReviewAuthority(hmac_key=b"u" * 32),
        runner=run,
    )
    failure = trust.evaluate_failure(_profile(), connection_generation=6, stderr=stderr)
    assert failure.review is not None

    trust.replace_changed_key(
        failure.review,
        profile=_profile(),
        connection_generation=6,
        review_id=failure.review.review_id,
        review_sha256=failure.review.review_sha256,
    )

    assert calls[-1][0] == [
        SSH_KEYGEN_EXECUTABLE,
        "-R",
        "gpu.internal",
        "-f",
        str(known_hosts),
    ]
    assert set(calls[-1][1]) == {"HOME", "PATH"}


def test_review_replacement_maps_stale_or_failed_mutation_to_closed_error(
    short_tmp_path: Path,
) -> None:
    known_hosts = _system_known_hosts(short_tmp_path)
    stderr = (
        "WARNING: REMOTE HOST IDENTIFICATION HAS CHANGED!\n"
        "The fingerprint for the ED25519 key sent by the remote host is\n"
        f"SHA256:{'F' * 43}.\n"
        f"Offending ED25519 key in {known_hosts}:1\n"
        "Host key verification failed.\n"
    ).encode()

    def issue_review(
        runner: session_module.SessionRunner,
        *,
        generation: int,
    ) -> tuple[SystemOpenSshHostTrust, PendingSystemHostKeyReview]:
        trust = SystemOpenSshHostTrust(
            home=short_tmp_path,
            inherited_environment={},
            runner=runner,
        )
        failure = trust.evaluate_failure(
            _profile(),
            connection_generation=generation,
            stderr=stderr,
        )
        assert failure.review is not None
        return trust, failure.review

    config = (
        "hostname gpu.internal\n"
        "port 22\n"
        "canonicalizehostname false\n"
        "hashknownhosts no\n"
        f"userknownhostsfile {known_hosts}\n"
        "globalknownhostsfile /etc/ssh/ssh_known_hosts\n"
    ).encode()

    def stale_runner(
        argv: list[str], environment: dict[str, str], timeout_seconds: float
    ) -> subprocess.CompletedProcess[bytes]:
        del environment, timeout_seconds
        return subprocess.CompletedProcess(argv, 0, config, b"")

    stale_trust, stale_review = issue_review(stale_runner, generation=11)
    known_hosts.write_text("changed concurrently\n", encoding="ascii")
    with pytest.raises(SystemOpenSshSessionError) as stale_error:
        stale_trust.replace_changed_key(
            stale_review,
            profile=_profile(),
            connection_generation=11,
            review_id=stale_review.review_id,
            review_sha256=stale_review.review_sha256,
        )
    assert stale_error.value.code == "ssh_host_key_review_invalid"
    assert str(known_hosts) not in str(stale_error.value)

    known_hosts.write_text("gpu.internal ssh-ed25519 AAAATEST\n", encoding="ascii")

    def failed_runner(
        argv: list[str], environment: dict[str, str], timeout_seconds: float
    ) -> subprocess.CompletedProcess[bytes]:
        del environment, timeout_seconds
        if argv[0] == SSH_EXECUTABLE:
            return subprocess.CompletedProcess(argv, 0, config, b"")
        return subprocess.CompletedProcess(argv, 1, b"private path", b"private detail")

    failed_trust, failed_review = issue_review(failed_runner, generation=12)
    with pytest.raises(SystemOpenSshSessionError) as failed_error:
        failed_trust.replace_changed_key(
            failed_review,
            profile=_profile(),
            connection_generation=12,
            review_id=failed_review.review_id,
            review_sha256=failed_review.review_sha256,
        )
    assert failed_error.value.code == "ssh_host_key_repair_failed"
    assert "private" not in str(failed_error.value)


def test_review_replacement_executes_system_keygen_against_exact_user_file(
    short_tmp_path: Path,
) -> None:
    known_hosts = _system_known_hosts(short_tmp_path)
    known_hosts.write_text(
        "\n".join(
            (
                f"gpu.internal {_valid_ed25519_public_key(b'old-gpu-key')}",
                f"other.internal {_valid_ed25519_public_key(b'other-key')}",
                "",
            )
        ),
        encoding="ascii",
    )
    known_hosts.chmod(0o600)
    stderr = (
        "WARNING: REMOTE HOST IDENTIFICATION HAS CHANGED!\n"
        "The fingerprint for the ED25519 key sent by the remote host is\n"
        f"SHA256:{'E' * 43}.\n"
        f"Offending ED25519 key in {known_hosts}:1\n"
        "Host key verification failed.\n"
    ).encode()
    evidence = classify_system_openssh_host_key_failure(stderr)
    policy = inspect_system_known_hosts_policy(
        (
            "hostname gpu.internal\n"
            "port 22\n"
            "canonicalizehostname false\n"
            "hashknownhosts no\n"
            f"userknownhostsfile {known_hosts}\n"
            "globalknownhostsfile /etc/ssh/ssh_known_hosts\n"
        ).encode(),
        home=short_tmp_path,
        offending_known_hosts_file=known_hosts,
    )
    authority = SystemHostKeyReviewAuthority(hmac_key=b"v" * 32)
    review = authority.issue(
        _profile(),
        connection_generation=10,
        evidence=evidence,
        policy=policy,
    )
    trust = SystemOpenSshHostTrust(
        home=short_tmp_path,
        inherited_environment={},
        review_authority=authority,
    )

    trust.replace_changed_key(
        review,
        profile=_profile(),
        connection_generation=10,
        review_id=review.review_id,
        review_sha256=review.review_sha256,
    )

    rewritten = known_hosts.read_text(encoding="ascii")
    assert "gpu.internal" not in rewritten
    assert "other.internal" in rewritten
    assert stat.S_IMODE(known_hosts.stat().st_mode) == 0o600


def test_session_maps_master_changed_key_exit_without_retaining_raw_stderr(
    short_tmp_path: Path,
) -> None:
    known_hosts = _system_known_hosts(short_tmp_path)
    raw = (
        "WARNING: REMOTE HOST IDENTIFICATION HAS CHANGED!\n"
        "The fingerprint for the ED25519 key sent by the remote host is\n"
        f"SHA256:{'D' * 43}.\n"
        f"Offending ED25519 key in {known_hosts}:1\n"
        "Host key verification failed.\n"
    ).encode()
    identity = ProcessIdentity(
        process_id=991,
        parent_process_id=os.getpid(),
        process_group_id=os.getpgrp(),
        session_id=os.getsid(0),
        user_id=os.geteuid(),
        birth_identity="failed-master",
        executable_path=SSH_EXECUTABLE,
    )
    inspector = _Inspector(identity)
    process = _FakeProcess(991, failure_stderr=raw)
    process.returncode = 255

    class FailedLauncher:
        def spawn(
            self,
            argv: list[str],
            *,
            environment: dict[str, str],
            control_path: Path,
        ) -> OwnedSshMasterProcess:
            del argv, environment, control_path
            return process

    def trust_run(
        argv: list[str], environment: dict[str, str], timeout_seconds: float
    ) -> subprocess.CompletedProcess[bytes]:
        del environment, timeout_seconds
        return subprocess.CompletedProcess(
            argv,
            0,
            (
                "hostname gpu.internal\n"
                "port 22\n"
                "canonicalizehostname false\n"
                "hashknownhosts no\n"
                f"userknownhostsfile {known_hosts}\n"
                "globalknownhostsfile /etc/ssh/ssh_known_hosts\n"
            ).encode(),
            b"",
        )

    trust = SystemOpenSshHostTrust(
        home=short_tmp_path,
        inherited_environment={},
        runner=trust_run,
    )
    session = SystemOpenSshSession(
        _profile(),
        connection_generation=7,
        askpass_helper=_helper(short_tmp_path),
        owns_askpass_helper=True,
        home=str(short_tmp_path),
        inherited_environment={},
        runtime_parent=short_tmp_path,
        inspector=inspector,
        launcher=FailedLauncher(),
        host_trust=trust,
        startup_timeout_seconds=1.0,
        cleanup_timeout_seconds=0.2,
    )

    with pytest.raises(SystemOpenSshSessionError) as exc_info:
        session.start()

    assert exc_info.value.code == "ssh_host_key_changed"
    assert exc_info.value.host_key_review is not None
    assert str(known_hosts) not in repr(exc_info.value)
    assert raw.decode() not in str(exc_info.value)


@pytest.mark.parametrize(
    ("state", "kind", "code"),
    [
        ("cancelled", "password", "ssh_prompt_cancelled"),
        ("cancelled", "host_confirmation", "ssh_prompt_cancelled"),
        ("rejected", "host_confirmation", "ssh_host_key_rejected"),
        (
            "completed",
            "host_confirmation",
            "ssh_first_host_accepted_reconnect_required",
        ),
    ],
)
def test_master_exit_maps_native_prompt_outcome_without_prompt_text(
    short_tmp_path: Path,
    state: str,
    kind: str,
    code: str,
) -> None:
    session, process, _inspector, _launcher, _runner = _session(short_tmp_path)
    session._broker = SimpleNamespace(  # type: ignore[assignment]
        prompt_observation=AskpassPromptObservation(
            connection_generation=1,
            kind=kind,
            state=state,
        )
    )

    error = session._master_exit_error(process, during_startup=True)

    assert error.code == code
    assert "password:" not in str(error).casefold()
    session.askpass_helper.close()


def test_connection_owner_retries_once_after_first_host_acceptance() -> None:
    attempts: list[object] = []
    snapshot = SystemOpenSshSessionSnapshot(
        profile_id="profile-1",
        ssh_host_alias="evolab",
        connection_generation=8,
        runtime_directory=Path("/tmp/owned-runtime"),
        control_path=Path("/tmp/owned-runtime/m"),
        owner_process_id=901,
        owner_birth_identity="birth-901",
        process_group_id=900,
        session_id=800,
        control_socket_device=1,
        control_socket_inode=2,
    )

    class Attempt:
        def __init__(self, fail: bool) -> None:
            self.fail = fail
            self.close_calls = 0

        def start(self) -> SystemOpenSshSessionSnapshot:
            if self.fail:
                raise SystemOpenSshSessionError(
                    "ssh_first_host_accepted_reconnect_required",
                    "safe retry",
                )
            return snapshot

        def close(self) -> None:
            self.close_calls += 1

        def snapshot(self) -> SystemOpenSshSessionSnapshot:
            return snapshot

    def factory(_profile_value: SystemOpenSshAliasProfile, _generation: int):
        attempt = Attempt(fail=not attempts)
        attempts.append(attempt)
        return attempt

    owner = SystemOpenSshSessionOwner(factory)  # type: ignore[arg-type]
    try:
        assert owner.connect(_profile(), connection_generation=8) == snapshot
        assert len(attempts) == 2
        assert attempts[0].close_calls == 1  # type: ignore[attr-defined]
    finally:
        owner.close()
