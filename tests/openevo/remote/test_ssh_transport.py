from __future__ import annotations

import base64
import errno
import hashlib
import json
import os
import re
import shlex
import signal
import socket
import subprocess
import struct
import sys
import threading
import time
import traceback
from pathlib import Path

import pytest

import openevo.deployment.ssh as ssh_module
from openevo.deployment import RemoteCommandResult, RemoteExecutorTransport
from openevo.deployment.host_keys import (
    HostKeyStoreError,
    HostKeyStoreErrorCode,
    ProviderKnownHostStore,
    TrustedKnownHostsBinding,
)
from openevo.deployment.ssh import (
    SshRemoteExecutorTransport,
    SshTransportError,
    SshTransportErrorCode,
)
from openevo.deployment import RemoteProfileConfig


class RecordingRunner:
    def __init__(self, *, fail: bool = False, timeout: bool = False) -> None:
        self.fail = fail
        self.timeout = timeout
        self.calls: list[tuple[list[str], float]] = []

    def __call__(
        self, argv: list[str], timeout_seconds: float
    ) -> subprocess.CompletedProcess[str]:
        self.calls.append((argv, timeout_seconds))
        if self.timeout:
            raise subprocess.TimeoutExpired(argv, timeout_seconds)
        return subprocess.CompletedProcess(
            argv,
            7 if self.fail else 0,
            stdout="out",
            stderr="err",
        )


class _TestExecutableAuthority:
    def __init__(self, path: str) -> None:
        self.descriptor = os.open(path, os.O_RDONLY)

    @property
    def execution_path(self) -> str:
        return f"/dev/fd/{self.descriptor}"

    def verify_path_binding(self) -> None:
        os.fstat(self.descriptor)

    def close(self) -> None:
        descriptor, self.descriptor = self.descriptor, -1
        if descriptor >= 0:
            os.close(descriptor)


@pytest.fixture(autouse=True)
def _allow_test_python_subprocess(monkeypatch: pytest.MonkeyPatch) -> None:
    production_prepare = ssh_module._prepare_verified_spawn

    def prepare(argv: list[str]):
        if argv and argv[0] == sys.executable:
            executable = _TestExecutableAuthority(sys.executable)
            return executable, [], list(argv)
        return production_prepare(argv)

    monkeypatch.setattr(ssh_module, "_prepare_verified_spawn", prepare)


class FailSecondCallRunner(RecordingRunner):
    def __call__(
        self, argv: list[str], timeout_seconds: float
    ) -> subprocess.CompletedProcess[str]:
        self.calls.append((argv, timeout_seconds))
        return subprocess.CompletedProcess(
            argv,
            9 if len(self.calls) == 2 else 0,
            stdout="out",
            stderr="rsync exploded" if len(self.calls) == 2 else "err",
        )


class RecordingTunnelStarter:
    def __init__(self) -> None:
        self.calls: list[list[str]] = []
        self.processes: list[FakeTunnelProcess] = []

    def __call__(self, argv: list[str]) -> "FakeTunnelProcess":
        self.calls.append(argv)
        process = FakeTunnelProcess()
        self.processes.append(process)
        return process


class RecordingCoreConnectionStarter:
    def __init__(self) -> None:
        self.calls: list[tuple[list[str], tuple[object, ...]]] = []
        self.processes: list[FakeTunnelProcess] = []
        self.streams: list[socket.socket] = []

    def __call__(self, argv: list[str], stream_fd: int) -> "FakeTunnelProcess":
        metadata = os.fstat(stream_fd)
        stream = socket.socket(fileno=os.dup(stream_fd))
        self.calls.append(
            (
                argv,
                (
                    stream.family,
                    stream.type,
                    stream.getsockopt(socket.SOL_SOCKET, socket.SO_TYPE),
                    metadata.st_uid,
                    (
                        stream_fd,
                        metadata.st_dev,
                        metadata.st_ino,
                        metadata.st_ctime_ns,
                    ),
                    stream.getsockname(),
                ),
            )
        )
        self.streams.append(stream)
        process = FakeTunnelProcess()
        self.processes.append(process)
        return process


class FakeTunnelProcess:
    def __init__(self) -> None:
        self.terminated = False
        self.killed = False
        self.waited = False
        self.return_code: int | None = None

    def poll(self) -> int | None:
        if self.return_code is not None:
            return self.return_code
        return 0 if self.terminated or self.killed else None

    def terminate(self) -> None:
        self.terminated = True

    def kill(self) -> None:
        self.killed = True

    def wait(self, timeout: float | None = None) -> int:
        self.waited = True
        if self.poll() is None:
            raise subprocess.TimeoutExpired("ssh", timeout)
        return 0

    def exit(self, return_code: int = 0) -> None:
        self.return_code = return_code


def _profile(**extra) -> RemoteProfileConfig:
    payload = {
        "version": 1,
        "id": "lab-gpu",
        "host": "gpu.example.edu",
        "port": 2222,
        "user": "alice",
    }
    payload.update(extra)
    return RemoteProfileConfig.model_validate(payload)


def _trusted_binding(
    tmp_path: Path,
    profile: RemoteProfileConfig | None = None,
) -> TrustedKnownHostsBinding:
    active_profile = profile or _profile()
    key_type = "ssh-ed25519"
    encoded_type = key_type.encode("ascii")
    key = hashlib.sha256(b"transport-test-key").digest()
    blob = struct.pack(">I", len(encoded_type)) + encoded_type + struct.pack(">I", len(key)) + key
    public_key = f"{key_type} {base64.b64encode(blob).decode('ascii')}"
    host = (
        active_profile.host
        if active_profile.port == 22
        else f"[{active_profile.host}]:{active_profile.port}"
    )

    def runner(argv: list[str], timeout_seconds: float) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(
            argv,
            0,
            stdout=f"{host} {public_key}\n",
            stderr="",
        )

    store = ProviderKnownHostStore(tmp_path / "known-hosts", runner=runner)
    pending = store.probe(active_profile)
    candidate = pending.candidates[0]
    return store.confirm(
        pending,
        profile=active_profile,
        algorithm=candidate.algorithm,
        fingerprint=candidate.fingerprint,
    )


def _transport(
    tmp_path: Path,
    *,
    profile: RemoteProfileConfig | None = None,
    runner: RecordingRunner | None = None,
    tunnel_starter: RecordingTunnelStarter | None = None,
    port_allocator=None,
    core_connection_starter=None,
) -> SshRemoteExecutorTransport:
    active_profile = profile or _profile()
    return SshRemoteExecutorTransport(
        active_profile,
        trusted_host=_trusted_binding(tmp_path, active_profile),
        runner=runner,
        tunnel_starter=tunnel_starter,
        port_allocator=port_allocator,
        core_connection_starter=core_connection_starter,
    )


def _expected_ssh_base(
    profile: RemoteProfileConfig,
    binding: TrustedKnownHostsBinding,
    *,
    key_path: Path | None = None,
) -> list[str]:
    argv = [ssh_module.SSH_EXECUTABLE, "-F", "/dev/null", "-p", str(profile.port)]
    if key_path is not None:
        argv.extend(
            [
                "-o",
                "IdentityFile=none",
                "-i",
                str(key_path),
                "-o",
                "IdentitiesOnly=yes",
                "-o",
                "IdentityAgent=none",
            ]
        )
    else:
        argv.extend(
            [
                "-o",
                "IdentityFile=none",
                "-o",
                "IdentitiesOnly=yes",
                "-o",
                "IdentityAgent=SSH_AUTH_SOCK",
            ]
        )
    argv.extend(
        [
            "-o",
            "PasswordAuthentication=no",
            "-o",
            "KbdInteractiveAuthentication=no",
            "-o",
            "ChallengeResponseAuthentication=no",
            "-o",
            "GSSAPIAuthentication=no",
            "-o",
            "HostbasedAuthentication=no",
            "-o",
            "StrictHostKeyChecking=yes",
            "-o",
            f"UserKnownHostsFile={binding.known_hosts_file}",
            "-o",
            "GlobalKnownHostsFile=/dev/null",
            "-o",
            "UpdateHostKeys=no",
            "-o",
            "CheckHostIP=no",
            "-o",
            "VerifyHostKeyDNS=no",
            "-o",
            "KnownHostsCommand=none",
            "-o",
            "HashKnownHosts=no",
            "-o",
            f"HostKeyAlgorithms={binding.algorithm}",
            "-o",
            "BatchMode=yes",
            "-l",
            profile.user,
        ]
    )
    return argv


def _assert_marked_command(wrapped: str, expected: str) -> str:
    assert wrapped.startswith(f"(\n{expected}\n)\n__openevo_remote_status=$?\n")
    match = re.search(r"(__OPENEVO_REMOTE_COMPLETION_[0-9a-f]{32}__=)", wrapped)
    assert match is not None
    assert wrapped.endswith('exit "$__openevo_remote_status"')
    return match.group(1)


def test_transport_fails_closed_without_trusted_host_binding() -> None:
    with pytest.raises(SshTransportError) as exc_info:
        SshRemoteExecutorTransport(_profile())

    assert exc_info.value.code is SshTransportErrorCode.INVALID_REQUEST


def test_run_forces_provider_known_hosts_options(tmp_path: Path) -> None:
    profile = _profile()
    binding = _trusted_binding(tmp_path, profile)
    runner = RecordingRunner()
    transport = SshRemoteExecutorTransport(
        profile,
        trusted_host=binding,
        runner=runner,
    )

    transport.run("true")

    argv = runner.calls[0][0]
    known_hosts = next(
        value.removeprefix("UserKnownHostsFile=")
        for value in argv
        if value.startswith("UserKnownHostsFile=")
    )
    assert known_hosts != str(binding.known_hosts_file)
    assert not Path(known_hosts).exists()
    expected = _expected_ssh_base(profile, binding)
    expected[expected.index(f"UserKnownHostsFile={binding.known_hosts_file}")] = (
        f"UserKnownHostsFile={known_hosts}"
    )
    assert argv[:-1] == [
        *expected,
        "--",
        "gpu.example.edu",
    ]
    _assert_marked_command(argv[-1], "true")


def test_run_revalidates_binding_before_spawning(tmp_path: Path) -> None:
    profile = _profile()
    binding = _trusted_binding(tmp_path, profile)
    runner = RecordingRunner()
    transport = SshRemoteExecutorTransport(
        profile,
        trusted_host=binding,
        runner=runner,
    )
    binding.known_hosts_file.unlink()

    with pytest.raises(SshTransportError) as exc_info:
        transport.run("true")

    assert exc_info.value.code is SshTransportErrorCode.HOST_KEY_VERIFICATION_FAILED
    assert runner.calls == []


@pytest.mark.parametrize("operation", ["run", "rsync"])
def test_synchronous_spawn_cancellation_removes_known_hosts_lease(
    tmp_path: Path,
    operation: str,
) -> None:
    class Cancelled(BaseException):
        pass

    class CancellingRunner:
        def __init__(self) -> None:
            self.calls: list[list[str]] = []

        def __call__(
            self,
            argv: list[str],
            _timeout_seconds: float,
        ) -> subprocess.CompletedProcess[str]:
            self.calls.append(argv)
            if operation == "run" or len(self.calls) == 2:
                raise Cancelled
            marker = re.search(r"(__OPENEVO_REMOTE_COMPLETION_[0-9a-f]{32}__=)", argv[-1])
            assert marker is not None
            return subprocess.CompletedProcess(
                argv,
                0,
                stdout="",
                stderr=f"{marker.group(1)}0\n",
            )

    runner = CancellingRunner()
    transport = _transport(tmp_path, runner=runner)
    local = tmp_path / "workspace"
    local.mkdir()

    with pytest.raises(Cancelled):
        if operation == "run":
            transport.run("true")
        else:
            transport.upload_dir(str(local), "/remote/workspace")

    assert not list(tmp_path.rglob(".openevo-ssh-lease-*"))
    assert ssh_module._ORPHANED_TRUST_LEASES == {}


def test_ipv6_rsync_and_tunnel_keep_exact_trust_binding(tmp_path: Path) -> None:
    profile = _profile(host="2001:db8::8")
    binding = _trusted_binding(tmp_path, profile)
    local = tmp_path / "workspace"
    local.mkdir()
    runner = RecordingRunner()
    starter = RecordingTunnelStarter()
    transport = SshRemoteExecutorTransport(
        profile,
        trusted_host=binding,
        runner=runner,
        tunnel_starter=starter,
    )

    transport.upload_dir(str(local), "/remote/workspace")
    tunnel = transport.open_tunnel(
        remote_port=8765,
        local_port=49155,
        wait_for_ready=False,
    )

    rsync_argv = runner.calls[1][0]
    assert rsync_argv[-1] == "[2001:db8::8]:/remote/workspace/"
    assert "StrictHostKeyChecking=yes" in rsync_argv[4]
    assert "UserKnownHostsFile=" in rsync_argv[4]
    assert str(binding.known_hosts_file) not in rsync_argv[4]
    assert starter.calls[0][-2:] == ["--", "2001:db8::8"]
    assert "StrictHostKeyChecking=yes" in starter.calls[0]
    assert any(value.startswith("UserKnownHostsFile=") for value in starter.calls[0])
    assert f"UserKnownHostsFile={binding.known_hosts_file}" not in starter.calls[0]
    tunnel.close()


def test_ssh_transport_satisfies_executor_protocol(tmp_path: Path) -> None:
    assert isinstance(_transport(tmp_path), RemoteExecutorTransport)


def test_run_invokes_ssh_with_batch_mode_and_maps_result(tmp_path: Path) -> None:
    runner = RecordingRunner()
    transport = _transport(tmp_path, runner=runner)

    result = transport.run("true", timeout_seconds=12.5)

    assert result == RemoteCommandResult(
        command="true",
        return_code=0,
        stdout="out",
        stderr="err",
    )
    assert runner.calls[0][0][-5:-1] == [
        "-l",
        "alice",
        "--",
        "gpu.example.edu",
    ]
    _assert_marked_command(runner.calls[0][0][-1], "true")
    assert runner.calls[0][1] == 12.5


def test_run_secret_never_builds_a_remote_command_result_or_exposes_repr(
    tmp_path: Path,
) -> None:
    bearer = "SECRET_BEARER_CANARY"

    class SecretRunner(RecordingRunner):
        def __call__(
            self, argv: list[str], timeout_seconds: float
        ) -> subprocess.CompletedProcess[str]:
            self.calls.append((argv, timeout_seconds))
            marker = _assert_marked_command(argv[-1], "consume-secret")
            return subprocess.CompletedProcess(
                argv,
                0,
                stdout=bearer,
                stderr=f"\n{marker}0\n",
            )

    transport = _transport(tmp_path, runner=SecretRunner())

    result = transport.run_secret("consume-secret")

    assert result.get_secret_value() == bearer
    assert bearer not in repr(result)
    assert not hasattr(result, "model_dump")


def test_run_secret_failure_drops_payload_from_error_and_diagnostics(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    bearer = "FAILED_SECRET_BEARER_CANARY"

    class FailedSecretRunner(RecordingRunner):
        def __call__(
            self, argv: list[str], timeout_seconds: float
        ) -> subprocess.CompletedProcess[str]:
            self.calls.append((argv, timeout_seconds))
            marker = _assert_marked_command(argv[-1], "consume-secret")
            return subprocess.CompletedProcess(
                argv,
                7,
                stdout=bearer,
                stderr=f"remote failure {bearer}\n{marker}7\n",
            )

    transport = _transport(tmp_path, runner=FailedSecretRunner())

    with pytest.raises(SshTransportError) as exc_info:
        transport.run_secret("consume-secret")

    assert bearer not in str(exc_info.value)
    assert bearer not in repr(exc_info.value)
    assert bearer not in caplog.text


def test_run_maps_remote_nonzero_exit_without_throwing(tmp_path: Path) -> None:
    runner = RecordingRunner(fail=True)
    transport = _transport(tmp_path, runner=runner)

    result = transport.run("false")

    assert result.return_code == 7
    assert result.stdout == "out"
    assert result.stderr == "err"


def test_transport_close_interrupts_an_active_default_runner_subprocess(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    profile = _profile()
    transport = SshRemoteExecutorTransport(
        profile,
        trusted_host=_trusted_binding(tmp_path, profile),
    )
    monkeypatch.setattr(
        transport,
        "_ssh_argv",
        lambda _command, _known_hosts: [
            sys.executable,
            "-c",
            "import time; time.sleep(30)",
        ],
    )
    outcome: list[object] = []

    def run() -> None:
        try:
            outcome.append(transport.run("true", timeout_seconds=30))
        except BaseException as exc:
            outcome.append(exc)

    worker = threading.Thread(target=run)
    worker.start()
    deadline = time.monotonic() + 2
    while True:
        with transport._operation_guard:
            active = bool(transport._active_subprocesses)
        if active:
            break
        if time.monotonic() >= deadline:
            raise AssertionError("transport subprocess did not start")
        time.sleep(0.01)

    started = time.monotonic()
    transport.close()
    elapsed = time.monotonic() - started
    worker.join(timeout=2)

    assert elapsed < 0.5
    assert not worker.is_alive()
    assert len(outcome) == 1
    assert isinstance(outcome[0], SshTransportError)
    with transport._operation_guard:
        assert transport._active_subprocesses == set()


def test_run_preserves_verified_remote_exit_255(tmp_path: Path) -> None:
    class Remote255Runner(RecordingRunner):
        def __call__(
            self, argv: list[str], timeout_seconds: float
        ) -> subprocess.CompletedProcess[str]:
            self.calls.append((argv, timeout_seconds))
            marker = _assert_marked_command(argv[-1], "exit 255")
            return subprocess.CompletedProcess(
                argv,
                255,
                stdout="remote stdout",
                stderr=f"remote stderr\n{marker}255\n",
            )

    transport = _transport(tmp_path, runner=Remote255Runner())

    result = transport.run("exit 255")

    assert result == RemoteCommandResult(
        command="exit 255",
        return_code=255,
        stdout="remote stdout",
        stderr="remote stderr",
    )


def test_private_key_auth_adds_identity_file_as_argv(tmp_path: Path) -> None:
    key = tmp_path / "id_ed25519"
    key.write_text("key", encoding="utf-8")
    runner = RecordingRunner()
    profile = _profile(
        auth={
            "method": "private_key",
            "private_key_path": str(key),
        }
    )
    transport = _transport(
        tmp_path,
        profile=profile,
        runner=runner,
    )

    transport.run("true")

    argv = runner.calls[0][0]
    assert argv[0:10] == [
        ssh_module.SSH_EXECUTABLE,
        "-F",
        "/dev/null",
        "-p",
        "2222",
        "-o",
        "IdentityFile=none",
        "-i",
        str(key),
        "-o",
    ]
    assert "IdentitiesOnly=yes" in argv
    assert "IdentityAgent=none" in argv
    assert "BatchMode=yes" in argv
    assert argv[-5:-1] == ["-l", "alice", "--", "gpu.example.edu"]
    _assert_marked_command(argv[-1], "true")


@pytest.mark.skipif(
    not Path(ssh_module.SSH_EXECUTABLE).is_file(),
    reason="fixed OpenSSH binary is unavailable",
)
def test_private_key_final_openssh_config_contains_only_explicit_identity(
    tmp_path: Path,
) -> None:
    key = tmp_path / "id_ed25519"
    key.write_text("key", encoding="utf-8")
    effective: dict[str, list[str]] = {}

    class EffectiveConfigRunner(RecordingRunner):
        def __call__(
            self, argv: list[str], timeout_seconds: float
        ) -> subprocess.CompletedProcess[str]:
            self.calls.append((argv, timeout_seconds))
            config_argv = [argv[0], "-G", *argv[1:-3], argv[-2]]
            inspected = subprocess.run(
                config_argv,
                check=True,
                capture_output=True,
                text=True,
            )
            for line in inspected.stdout.splitlines():
                name, _, value = line.partition(" ")
                effective.setdefault(name, []).append(value)
            return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

    profile = _profile(auth={"method": "private_key", "private_key_path": str(key)})
    transport = _transport(tmp_path, profile=profile, runner=EffectiveConfigRunner())

    transport.run("true")

    assert effective["identitiesonly"] == ["yes"]
    assert effective["identityagent"] == ["none"]
    assert effective["identityfile"] == ["none", str(key)]


@pytest.mark.skipif(
    not Path(ssh_module.SSH_EXECUTABLE).is_file(),
    reason="fixed OpenSSH binary is unavailable",
)
def test_ssh_agent_final_openssh_config_has_no_authentication_fallback(
    tmp_path: Path,
) -> None:
    runner = RecordingRunner()
    profile = _profile()
    transport = _transport(tmp_path, profile=profile, runner=runner)

    transport.run("true")

    argv = runner.calls[0][0]
    inspected = subprocess.run(
        [argv[0], "-G", *argv[1:-3], argv[-2]],
        check=True,
        capture_output=True,
        text=True,
        env={},
    )
    effective: dict[str, list[str]] = {}
    for line in inspected.stdout.splitlines():
        name, _, value = line.partition(" ")
        effective.setdefault(name, []).append(value)
    assert effective["identityfile"] == ["none"]
    assert effective["identitiesonly"] == ["yes"]
    assert effective["identityagent"] == ["SSH_AUTH_SOCK"]
    assert effective["passwordauthentication"] == ["no"]
    assert effective["kbdinteractiveauthentication"] == ["no"]
    assert effective["gssapiauthentication"] == ["no"]
    assert effective["hostbasedauthentication"] == ["no"]


def test_private_key_isolation_is_identical_for_command_rsync_and_tunnel(
    tmp_path: Path,
) -> None:
    key = tmp_path / "id_ed25519"
    key.write_text("key", encoding="utf-8")
    local = tmp_path / "workspace"
    local.mkdir()
    profile = _profile(auth={"method": "private_key", "private_key_path": str(key)})
    runner = RecordingRunner()
    starter = RecordingTunnelStarter()
    transport = _transport(
        tmp_path,
        profile=profile,
        runner=runner,
        tunnel_starter=starter,
    )

    transport.run("true")
    transport.upload_dir(str(local), "/remote/workspace")
    tunnel = transport.open_tunnel(remote_port=8765, local_port=49155, wait_for_ready=False)

    command_argv = runner.calls[0][0]
    rsync_shell = runner.calls[2][0][4]
    tunnel_argv = starter.calls[0]
    for required in (
        "-F",
        "/dev/null",
        "IdentitiesOnly=yes",
        "IdentityAgent=none",
        "IdentityFile=none",
    ):
        assert required in command_argv
        assert shlex.quote(required) in rsync_shell
        assert required in tunnel_argv
    assert command_argv.count(str(key)) == 1
    assert shlex.split(rsync_shell).count(str(key)) == 1
    assert tunnel_argv.count(str(key)) == 1
    tunnel.close()


def test_spawn_lease_survives_trust_root_rename_and_replacement(tmp_path: Path) -> None:
    profile = _profile()
    binding = _trusted_binding(tmp_path, profile)
    original_root = binding.known_hosts_file.parent
    moved_root = tmp_path / "moved-known-hosts"
    canary = "TRUST_PATH_CANARY"

    class RacingRunner(RecordingRunner):
        def __call__(
            self, argv: list[str], timeout_seconds: float
        ) -> subprocess.CompletedProcess[str]:
            self.calls.append((argv, timeout_seconds))
            lease_path = Path(
                next(
                    value.removeprefix("UserKnownHostsFile=")
                    for value in argv
                    if value.startswith("UserKnownHostsFile=")
                )
            )
            original_root.rename(moved_root)
            original_root.mkdir(mode=0o700)
            (original_root / binding.known_hosts_file.name).write_text(canary, encoding="utf-8")
            assert binding.public_key in lease_path.read_text(encoding="utf-8")
            assert canary not in lease_path.read_text(encoding="utf-8")
            return subprocess.CompletedProcess(argv, 0, stdout="ok", stderr="")

    runner = RacingRunner()
    transport = SshRemoteExecutorTransport(profile, trusted_host=binding, runner=runner)

    assert transport.run("true").ok
    with pytest.raises(ValueError, match="root binding changed"):
        binding.validate_for(profile)


def test_unverified_ssh_255_is_connection_failure_and_logs_no_process_output(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    canary = "TRUST_PATH_CANARY"

    class HostMismatchRunner(RecordingRunner):
        def __call__(
            self, argv: list[str], timeout_seconds: float
        ) -> subprocess.CompletedProcess[str]:
            self.calls.append((argv, timeout_seconds))
            return subprocess.CompletedProcess(
                argv,
                255,
                stdout="",
                stderr=(
                    "REMOTE HOST IDENTIFICATION HAS CHANGED; offending key in "
                    f"/private/{canary}/known_hosts"
                ),
            )

    transport = _transport(tmp_path, runner=HostMismatchRunner())

    with pytest.raises(SshTransportError) as exc_info:
        transport.run("true")

    assert exc_info.value.code is SshTransportErrorCode.CONNECTION_FAILED
    assert canary not in str(exc_info.value)
    assert canary not in caplog.text
    assert "REMOTE HOST" not in caplog.text
    assert "code=connection_failed" in caplog.text
    assert "return_code" not in caplog.text
    assert re.search(r"diagnostic_id=[0-9a-f]{24}", caplog.text)


def test_rsync_error_does_not_leak_known_hosts_path(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    local = tmp_path / "workspace"
    local.mkdir()
    canary = "TRUST_PATH_CANARY"

    class CanaryRsyncRunner(RecordingRunner):
        def __call__(
            self, argv: list[str], timeout_seconds: float
        ) -> subprocess.CompletedProcess[str]:
            self.calls.append((argv, timeout_seconds))
            if len(self.calls) == 1:
                return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")
            return subprocess.CompletedProcess(
                argv,
                255,
                stdout="",
                stderr=f"failed to open /private/{canary}/known_hosts",
            )

    transport = _transport(tmp_path, runner=CanaryRsyncRunner())

    with pytest.raises(SshTransportError) as exc_info:
        transport.upload_dir(str(local), "/remote/workspace")

    assert exc_info.value.code is SshTransportErrorCode.RSYNC_FAILED
    assert canary not in str(exc_info.value)
    assert canary not in caplog.text
    assert "failed to open" not in caplog.text
    assert "code=rsync_failed" in caplog.text


def test_run_quotes_remote_env_values_and_cwd(tmp_path: Path) -> None:
    runner = RecordingRunner()
    transport = _transport(tmp_path, runner=runner)

    transport.run(
        "python script.py",
        cwd="/home/alice/project-dir",
        env={
            "HTTPS_PROXY": "http://127.0.0.1:7890",
            "PIP_INDEX_URL": "https://mirror.example/simple path",
        },
    )

    remote_command = runner.calls[0][0][-1]
    _assert_marked_command(
        remote_command,
        "cd /home/alice/project-dir && "
        "export HTTPS_PROXY=http://127.0.0.1:7890 "
        "PIP_INDEX_URL='https://mirror.example/simple path' "
        "&& python script.py",
    )


def test_run_exports_env_before_multiline_remote_script(tmp_path: Path) -> None:
    runner = RecordingRunner()
    transport = _transport(tmp_path, runner=runner)

    transport.run(
        "python3 - <<'PY'\n"
        "import os\n"
        "print(os.environ['HTTPS_PROXY'])\n"
        "PY\n"
        'nohup env PATH="$HOME/.local/bin:$PATH" python3 server.py &',
        env={"HTTPS_PROXY": "http://proxy-user:proxy-secret@127.0.0.1:7890"},
    )

    remote_command = runner.calls[0][0][-1]
    expected = (
        "export HTTPS_PROXY=http://proxy-user:proxy-secret@127.0.0.1:7890 && "
        "python3 - <<'PY'\n"
        "import os\n"
        "print(os.environ['HTTPS_PROXY'])\n"
        "PY\n"
        'nohup env PATH="$HOME/.local/bin:$PATH" python3 server.py &'
    )
    _assert_marked_command(remote_command, expected)
    assert "env HTTPS_PROXY=" not in remote_command


def test_run_rejects_invalid_env_key(tmp_path: Path) -> None:
    transport = _transport(tmp_path, runner=RecordingRunner())

    with pytest.raises(SshTransportError) as exc_info:
        transport.run("true", env={"BAD KEY": "value"})

    assert exc_info.value.code is SshTransportErrorCode.INVALID_REQUEST


def test_run_rejects_relative_cwd(tmp_path: Path) -> None:
    transport = _transport(tmp_path, runner=RecordingRunner())

    with pytest.raises(SshTransportError) as exc_info:
        transport.run("true", cwd="relative")

    assert exc_info.value.code is SshTransportErrorCode.INVALID_REQUEST


def test_run_rejects_cwd_with_control_character(tmp_path: Path) -> None:
    transport = _transport(tmp_path, runner=RecordingRunner())

    with pytest.raises(SshTransportError) as exc_info:
        transport.run("true", cwd="/tmp/project\nid")

    assert exc_info.value.code is SshTransportErrorCode.INVALID_REQUEST


def test_run_rejects_cwd_with_shell_metacharacter(tmp_path: Path) -> None:
    transport = _transport(tmp_path, runner=RecordingRunner())

    with pytest.raises(SshTransportError) as exc_info:
        transport.run("true", cwd="/tmp/project;touch-pwned")

    assert exc_info.value.code is SshTransportErrorCode.INVALID_REQUEST


def test_password_ref_auth_is_not_supported_without_vault() -> None:
    with pytest.raises(SshTransportError) as exc_info:
        SshRemoteExecutorTransport(
            _profile(auth={"method": "password_ref", "password_ref": "secret-id"})
        )

    assert exc_info.value.code is SshTransportErrorCode.INVALID_REQUEST
    assert "secret-id" not in str(exc_info.value)


def test_passphrase_ref_is_not_supported_without_vault(tmp_path: Path) -> None:
    key = tmp_path / "id_ed25519"
    key.write_text("key", encoding="utf-8")

    with pytest.raises(SshTransportError) as exc_info:
        SshRemoteExecutorTransport(
            _profile(
                auth={
                    "method": "private_key",
                    "private_key_path": str(key),
                    "passphrase_ref": "passphrase-secret",
                }
            )
        )

    assert exc_info.value.code is SshTransportErrorCode.INVALID_REQUEST
    assert "passphrase-secret" not in str(exc_info.value)


def test_rejects_unsafe_remote_identity() -> None:
    with pytest.raises(SshTransportError) as exc_info:
        SshRemoteExecutorTransport(_profile(host="-oProxyCommand=bad"))

    assert exc_info.value.code is SshTransportErrorCode.INVALID_REQUEST


def test_rejects_host_with_at_sign_to_prevent_target_rewrite() -> None:
    with pytest.raises(SshTransportError) as exc_info:
        SshRemoteExecutorTransport(_profile(host="trusted.example@attacker.example"))

    assert exc_info.value.code is SshTransportErrorCode.INVALID_REQUEST


def test_rejects_colon_option_injection_that_is_not_ipv6() -> None:
    with pytest.raises(SshTransportError) as exc_info:
        SshRemoteExecutorTransport(_profile(host="trusted.example:ProxyCommand=bad"))

    assert exc_info.value.code is SshTransportErrorCode.INVALID_REQUEST


def test_rejects_user_with_at_sign_to_prevent_target_rewrite() -> None:
    with pytest.raises(SshTransportError) as exc_info:
        SshRemoteExecutorTransport(_profile(user="alice@trusted.example"))

    assert exc_info.value.code is SshTransportErrorCode.INVALID_REQUEST


def test_upload_dir_creates_remote_parent_and_runs_rsync(tmp_path: Path) -> None:
    local = tmp_path / "workspace"
    local.mkdir()
    profile = _profile()
    binding = _trusted_binding(tmp_path, profile)
    runner = RecordingRunner()
    transport = SshRemoteExecutorTransport(
        profile,
        trusted_host=binding,
        runner=runner,
    )

    transport.upload_dir(str(local), "/home/alice/.openevo/workspaces/task")

    _assert_marked_command(
        runner.calls[0][0][-1],
        "mkdir -p /home/alice/.openevo/workspaces/task",
    )
    actual_shell = shlex.split(runner.calls[1][0][4])
    lease_option = next(value for value in actual_shell if value.startswith("UserKnownHostsFile="))
    assert str(binding.known_hosts_file) not in lease_option
    expected_shell = _expected_ssh_base(profile, binding)
    expected_shell[expected_shell.index(f"UserKnownHostsFile={binding.known_hosts_file}")] = (
        lease_option
    )
    assert runner.calls[1][0] == [
        ssh_module.RSYNC_EXECUTABLE,
        "-az",
        "--delete",
        "-e",
        shlex.join(expected_shell),
        f"{local}/",
        "gpu.example.edu:/home/alice/.openevo/workspaces/task/",
    ]


def test_open_tunnel_starts_ssh_local_forwarding_and_closes_process(
    tmp_path: Path,
) -> None:
    profile = _profile()
    binding = _trusted_binding(tmp_path, profile)
    starter = RecordingTunnelStarter()
    transport = SshRemoteExecutorTransport(
        profile,
        trusted_host=binding,
        runner=RecordingRunner(),
        tunnel_starter=starter,
        port_allocator=lambda: 49155,
    )

    tunnel = transport.open_tunnel(remote_port=8765, wait_for_ready=False)

    assert tunnel.local_port == 49155
    assert tunnel.remote_port == 8765
    assert tunnel.base_url == "http://127.0.0.1:49155"
    lease_option = next(
        value for value in starter.calls[0] if value.startswith("UserKnownHostsFile=")
    )
    lease_path = Path(lease_option.removeprefix("UserKnownHostsFile="))
    assert lease_path.exists()
    expected_base = _expected_ssh_base(profile, binding)
    expected_base[expected_base.index(f"UserKnownHostsFile={binding.known_hosts_file}")] = (
        lease_option
    )
    assert starter.calls == [
        [
            *expected_base,
            "-o",
            "ExitOnForwardFailure=yes",
            "-N",
            "-L",
            "127.0.0.1:49155:127.0.0.1:8765",
            "--",
            "gpu.example.edu",
        ]
    ]

    tunnel.close()

    assert starter.processes[0].terminated is True
    assert starter.processes[0].waited is True
    assert starter.processes[0].killed is False
    assert not lease_path.exists()

    tunnel.close()
    assert tunnel.closed is True


def test_open_tunnel_readiness_cancellation_closes_forward_and_trust_lease(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Cancelled(BaseException):
        pass

    profile = _profile()
    binding = _trusted_binding(tmp_path, profile)
    starter = RecordingTunnelStarter()
    transport = SshRemoteExecutorTransport(
        profile,
        trusted_host=binding,
        runner=RecordingRunner(),
        tunnel_starter=starter,
        port_allocator=lambda: 49155,
    )

    def cancel_readiness(*_args: object, **_kwargs: object) -> None:
        raise Cancelled

    monkeypatch.setattr(ssh_module, "_wait_for_local_port", cancel_readiness)

    with pytest.raises(Cancelled):
        transport.open_tunnel(remote_port=8765)

    process = starter.processes[0]
    lease_path = Path(
        next(
            value.removeprefix("UserKnownHostsFile=")
            for value in starter.calls[0]
            if value.startswith("UserKnownHostsFile=")
        )
    )
    assert process.terminated is True
    assert process.waited is True
    assert not lease_path.exists()
    assert all(tunnel.process is not process for tunnel in ssh_module._ORPHANED_TUNNELS.values())


@pytest.mark.parametrize("interruption", [KeyboardInterrupt, SystemExit])
@pytest.mark.parametrize("tunnel_kind", ["forward", "core_connection"])
def test_tunnel_popen_cancellation_owns_and_reaps_entire_process_group(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    interruption: type[BaseException],
    tunnel_kind: str,
) -> None:
    original_popen = subprocess.Popen
    descendant_path = tmp_path / f"{tunnel_kind}-descendant.pid"
    spawned: list[subprocess.Popen[bytes]] = []
    observed_argv: list[list[str]] = []
    observed_kwargs: list[dict[str, object]] = []
    lease_paths: list[Path] = []
    sleeper = (
        "import os,signal,subprocess,sys,time;"
        "signal.signal(signal.SIGTERM,signal.SIG_IGN);"
        "child=subprocess.Popen([sys.executable,'-c',"
        "'import signal,time;signal.signal(signal.SIGTERM,signal.SIG_IGN);time.sleep(60)']);"
        "open(sys.argv[1],'w').write(str(child.pid));"
        "time.sleep(60)"
    )

    def popen_then_cancel(
        argv: list[str],
        *args: object,
        **kwargs: object,
    ) -> subprocess.Popen[bytes]:
        actual = list(argv)
        observed_argv.append(actual)
        observed_kwargs.append(dict(kwargs))
        for value in actual:
            if value.startswith("UserKnownHostsFile="):
                lease_paths.append(Path(value.removeprefix("UserKnownHostsFile=")))
        if len(actual) >= 7 and actual[3] == ssh_module._SUBPROCESS_BIRTH_LAUNCHER:
            production_executable_fd = int(actual[6])
            test_executable_fd = os.open(sys.executable, os.O_RDONLY)
            replacement = [
                *actual[:6],
                str(test_executable_fd),
                sys.executable,
                "-c",
                sleeper,
                str(descendant_path),
            ]
            kwargs["pass_fds"] = tuple(
                test_executable_fd if descriptor == production_executable_fd else descriptor
                for descriptor in kwargs.get("pass_fds", ())
            )
        else:
            test_executable_fd = -1
            replacement = [sys.executable, "-c", sleeper, str(descendant_path)]
        try:
            process = original_popen(replacement, *args, **kwargs)
        finally:
            if test_executable_fd >= 0:
                os.close(test_executable_fd)
        spawned.append(process)
        deadline = time.monotonic() + 3
        while not descendant_path.exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        assert descendant_path.exists()
        raise interruption()

    def process_stopped(process_id: int) -> bool:
        try:
            status = Path(f"/proc/{process_id}/stat").read_text(encoding="ascii")
        except FileNotFoundError:
            return True
        fields = status[status.rfind(")") + 2 :].split()
        return bool(fields) and fields[0] in {"X", "Z"}

    monkeypatch.setattr(ssh_module, "_TUNNEL_SUBPROCESS_POPEN", popen_then_cancel)
    transport = _transport(tmp_path)
    orphan_ids = set(ssh_module._ORPHANED_SUBPROCESSES)
    tunnel = None
    try:
        with pytest.raises(interruption):
            if tunnel_kind == "forward":
                transport.open_tunnel(
                    remote_port=8765,
                    local_port=49157,
                    wait_for_ready=False,
                )
            else:
                tunnel = transport.open_core_tunnel(remote_port=8765)
                tunnel.open_verified_socket(timeout_seconds=1.0)

        assert len(spawned) == 1
        leader_id = spawned[0].pid
        descendant_id = int(descendant_path.read_text(encoding="ascii"))
        assert observed_kwargs[0].get("start_new_session") is True
        assert process_stopped(leader_id)
        assert process_stopped(descendant_id)
        assert len(lease_paths) == 1
        assert not lease_paths[0].exists()
        assert set(ssh_module._ORPHANED_SUBPROCESSES) == orphan_ids
    finally:
        if tunnel is not None:
            try:
                tunnel.close()
            except BaseException:
                pass
        for process_id in [
            *(process.pid for process in spawned),
            *(
                [int(descendant_path.read_text(encoding="ascii"))]
                if descendant_path.exists()
                else []
            ),
        ]:
            try:
                os.kill(process_id, signal.SIGKILL)
            except ProcessLookupError:
                pass
        for process in spawned:
            try:
                process.wait(timeout=3)
            except (ChildProcessError, subprocess.TimeoutExpired):
                pass


@pytest.mark.parametrize("with_descendant", [False, True], ids=("short-lived", "descendant"))
def test_production_tunnel_exit_observation_does_not_reap_before_group_cleanup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    with_descendant: bool,
) -> None:
    pid_path = tmp_path / f"production-tunnel-{with_descendant}.json"
    tunnel_program = tmp_path / f"production-tunnel-{with_descendant}.py"
    program_lines = ["import json", "import os", "import sys"]
    if with_descendant:
        program_lines.extend(
            (
                "import subprocess",
                "child = subprocess.Popen([sys.executable, '-c', "
                "'import signal,time; signal.signal(signal.SIGTERM, signal.SIG_IGN); "
                "time.sleep(30)'])",
                "process_ids = [os.getpid(), child.pid]",
            )
        )
    else:
        program_lines.append("process_ids = [os.getpid()]")
    program_lines.extend(
        (
            "with open(sys.argv[1], 'w', encoding='ascii') as stream:",
            "    json.dump(process_ids, stream)",
            "    stream.flush()",
            "    os.fsync(stream.fileno())",
        )
    )
    tunnel_program.write_text("\n".join(program_lines), encoding="ascii")

    transport = _transport(tmp_path)
    lease_paths: list[Path] = []
    orphan_ids = set(ssh_module._ORPHANED_SUBPROCESSES)

    def local_tunnel_argv(known_hosts_file: Path) -> list[str]:
        lease_paths.append(known_hosts_file)
        return [sys.executable, str(tunnel_program), str(pid_path)]

    monkeypatch.setattr(transport, "_ssh_base_argv", local_tunnel_argv)
    monkeypatch.setattr(ssh_module, "_SUBPROCESS_TERMINATE_GRACE_SECONDS", 0.2)
    tunnel = transport.open_tunnel(
        remote_port=8765,
        local_port=49159 if with_descendant else 49158,
        wait_for_ready=False,
    )
    authority = tunnel._process_authority
    assert authority is not None
    process = authority.process
    assert process is not None
    process_ids: list[int] = []
    try:
        deadline = time.monotonic() + 3
        while not tunnel.closed and time.monotonic() < deadline:
            if pid_path.exists() and not process_ids:
                try:
                    process_ids = json.loads(pid_path.read_text(encoding="ascii"))
                except json.JSONDecodeError:
                    pass
            time.sleep(0.01)

        assert tunnel.closed is True
        assert process.returncode is not None
        assert authority.released is True
        assert set(ssh_module._ORPHANED_SUBPROCESSES) == orphan_ids
        assert len(lease_paths) == 1
        assert not lease_paths[0].exists()
        assert process_ids
        _assert_processes_gone(*process_ids)
    finally:
        if not process_ids and pid_path.exists():
            try:
                process_ids = json.loads(pid_path.read_text(encoding="ascii"))
            except json.JSONDecodeError:
                pass
        if not authority.released:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            try:
                process.wait(timeout=3)
            except (ChildProcessError, subprocess.TimeoutExpired):
                pass
            authority.mark_group_cleanup_confirmed()
            authority.release()
        for process_id in process_ids:
            try:
                os.kill(process_id, signal.SIGKILL)
            except ProcessLookupError:
                pass


def test_production_tunnel_concurrent_close_releases_authority_before_restart(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tunnel_program = tmp_path / "production-tunnel-concurrent.py"
    tunnel_program.write_text("import time\ntime.sleep(30)\n", encoding="ascii")
    transport = _transport(tmp_path)
    lease_paths: list[Path] = []
    orphan_ids = set(ssh_module._ORPHANED_SUBPROCESSES)

    def local_tunnel_argv(known_hosts_file: Path) -> list[str]:
        lease_paths.append(known_hosts_file)
        return [sys.executable, str(tunnel_program)]

    monkeypatch.setattr(transport, "_ssh_base_argv", local_tunnel_argv)
    monkeypatch.setattr(ssh_module, "_SUBPROCESS_TERMINATE_GRACE_SECONDS", 0.2)
    tunnel = transport.open_tunnel(
        remote_port=8765,
        local_port=49160,
        wait_for_ready=False,
    )
    authority = tunnel._process_authority
    assert authority is not None
    process = authority.process
    assert process is not None
    with pytest.raises(RuntimeError, match="non-reaping observer"):
        process.poll()

    failures: list[BaseException] = []

    def close_tunnel() -> None:
        try:
            tunnel.close()
        except BaseException as exc:
            failures.append(exc)

    closers = [threading.Thread(target=close_tunnel) for _ in range(4)]
    for closer in closers:
        closer.start()
    for closer in closers:
        closer.join(timeout=3)

    assert all(not closer.is_alive() for closer in closers)
    assert failures == []
    assert tunnel.closed is True
    assert process.returncode is not None
    assert authority.released is True

    restarted = transport.open_tunnel(
        remote_port=8765,
        local_port=49160,
        wait_for_ready=False,
    )
    restarted.close()

    assert restarted.closed is True
    assert len(lease_paths) == 2
    assert all(not path.exists() for path in lease_paths)
    assert set(ssh_module._ORPHANED_SUBPROCESSES) == orphan_ids


@pytest.mark.parametrize("tunnel_kind", ["forward", "core_connection"])
def test_tunnel_authority_acquire_cancellation_releases_slot_and_trust_lease(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    tunnel_kind: str,
) -> None:
    class Cancelled(BaseException):
        pass

    original_acquire = ssh_module._OwnedSubprocessAuthority.acquire
    lease_paths: list[Path] = []

    def acquire_then_cancel(authority: ssh_module._OwnedSubprocessAuthority) -> None:
        original_acquire(authority)
        raise Cancelled

    monkeypatch.setattr(
        ssh_module._OwnedSubprocessAuthority,
        "acquire",
        acquire_then_cancel,
    )
    transport = _transport(tmp_path)
    orphan_ids = set(ssh_module._ORPHANED_SUBPROCESSES)
    original_base_argv = transport._ssh_base_argv

    def record_lease(known_hosts_file: Path) -> list[str]:
        lease_paths.append(known_hosts_file)
        return original_base_argv(known_hosts_file)

    monkeypatch.setattr(transport, "_ssh_base_argv", record_lease)
    tunnel = None
    try:
        with pytest.raises(Cancelled):
            if tunnel_kind == "forward":
                transport.open_tunnel(
                    remote_port=8765,
                    local_port=49158,
                    wait_for_ready=False,
                )
            else:
                tunnel = transport.open_core_tunnel(remote_port=8765)
                tunnel.open_verified_socket(timeout_seconds=1.0)

        assert len(lease_paths) == 1
        assert not lease_paths[0].exists()
        assert set(ssh_module._ORPHANED_SUBPROCESSES) == orphan_ids
    finally:
        if tunnel is not None:
            try:
                tunnel.close()
            except BaseException:
                pass


def test_core_tunnel_uses_parent_owned_socketpair_and_per_connection_ssh_child(
    tmp_path: Path,
) -> None:
    starter = RecordingCoreConnectionStarter()
    transport = _transport(
        tmp_path,
        core_connection_starter=starter,
    )

    tunnel = transport.open_core_tunnel(remote_port=8765)

    assert tunnel.base_url == "http://openevo-core.local"
    connection = tunnel.open_verified_socket(timeout_seconds=1.0)
    second_connection = tunnel.open_verified_socket(timeout_seconds=1.0)
    assert len(starter.calls) == 2
    assert tunnel._endpoint._pending_child is None
    for argv, (family, declared_type, kernel_type, uid, identity, local_name) in starter.calls:
        assert argv[-4:] == ["-W", "127.0.0.1:8765", "--", "gpu.example.edu"]
        assert "-L" not in argv
        assert "-S" not in argv
        assert "ControlMaster=yes" not in argv
        assert family == socket.AF_UNIX
        assert declared_type == socket.SOCK_STREAM
        assert kernel_type == socket.SOCK_STREAM
        assert uid == os.geteuid()
        assert isinstance(identity, tuple)
        assert len(identity) == 4
        assert all(isinstance(value, int) for value in identity)
        assert local_name in ("", b"")
    local_metadata = os.fstat(connection.fileno())
    assert connection.family == socket.AF_UNIX
    assert connection.type == socket.SOCK_STREAM
    assert connection.getsockopt(socket.SOL_SOCKET, socket.SO_TYPE) == socket.SOCK_STREAM
    assert local_metadata.st_uid == os.geteuid()
    assert connection.getsockname() in ("", b"")
    assert connection.getpeername() in ("", b"")
    connection.close()
    second_connection.close()
    tunnel.verify_authority()

    starter.processes[1].return_code = 255
    with pytest.raises(SshTransportError) as exited:
        tunnel.verify_authority()
    assert exited.value.code is SshTransportErrorCode.CONNECTION_FAILED

    tunnel.close()
    for stream in starter.streams:
        stream.close()


def test_core_tunnel_connection_start_cancellation_closes_parent_stream_and_propagates(
    tmp_path: Path,
) -> None:
    class Cancelled(BaseException):
        pass

    observed_fd = -1

    def cancel(_argv: list[str], stream_fd: int) -> FakeTunnelProcess:
        nonlocal observed_fd
        observed_fd = stream_fd
        raise Cancelled

    transport = _transport(
        tmp_path,
        core_connection_starter=cancel,
    )
    tunnel = transport.open_core_tunnel(remote_port=8765)

    with pytest.raises(Cancelled):
        tunnel.open_verified_socket(timeout_seconds=1.0)
    with pytest.raises(OSError):
        os.fstat(observed_fd)

    tunnel.close()


def test_core_tunnel_never_fchmods_anonymous_socketpair_on_darwin(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    starter = RecordingCoreConnectionStarter()
    tunnel = _transport(
        tmp_path,
        core_connection_starter=starter,
    ).open_core_tunnel(remote_port=8765)
    fchmod_calls: list[tuple[int, int]] = []

    def darwin_fchmod(descriptor: int, mode: int) -> None:
        fchmod_calls.append((descriptor, mode))
        raise OSError(errno.EINVAL, "Invalid argument")

    monkeypatch.setattr(ssh_module.os, "fchmod", darwin_fchmod)

    connection = tunnel.open_verified_socket(timeout_seconds=1.0)

    assert fchmod_calls == []
    assert connection.family == socket.AF_UNIX
    connection.close()
    tunnel.close()
    for stream in starter.streams:
        stream.close()


def test_core_tunnel_rejects_non_stream_socketpair(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    starter = RecordingCoreConnectionStarter()
    tunnel = _transport(
        tmp_path,
        core_connection_starter=starter,
    ).open_core_tunnel(remote_port=8765)
    socketpair = socket.socketpair

    def datagram_socketpair(
        family: socket.AddressFamily,
        socket_type: socket.SocketKind,
    ) -> tuple[socket.socket, socket.socket]:
        assert family == socket.AF_UNIX
        assert socket_type == socket.SOCK_STREAM
        return socketpair(socket.AF_UNIX, socket.SOCK_DGRAM)

    monkeypatch.setattr(ssh_module.socket, "socketpair", datagram_socketpair)

    with pytest.raises(SshTransportError) as rejected:
        tunnel.open_verified_socket(timeout_seconds=1.0)

    assert rejected.value.code is SshTransportErrorCode.CONNECTION_FAILED
    assert starter.calls == []
    tunnel.close()


def test_core_tunnel_rejects_socketpair_not_owned_by_effective_uid(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    starter = RecordingCoreConnectionStarter()
    tunnel = _transport(
        tmp_path,
        core_connection_starter=starter,
    ).open_core_tunnel(remote_port=8765)
    socketpair = socket.socketpair
    fstat = os.fstat
    socket_fds: set[int] = set()

    def recording_socketpair(
        family: socket.AddressFamily,
        socket_type: socket.SocketKind,
    ) -> tuple[socket.socket, socket.socket]:
        streams = socketpair(family, socket_type)
        socket_fds.update(stream.fileno() for stream in streams)
        return streams

    def foreign_fstat(descriptor: int) -> os.stat_result:
        metadata = fstat(descriptor)
        if descriptor not in socket_fds:
            return metadata
        values = list(metadata)
        values[4] = os.geteuid() + 1
        return os.stat_result(values)

    monkeypatch.setattr(ssh_module.socket, "socketpair", recording_socketpair)
    monkeypatch.setattr(ssh_module.os, "fstat", foreign_fstat)

    with pytest.raises(SshTransportError) as rejected:
        tunnel.open_verified_socket(timeout_seconds=1.0)

    assert rejected.value.code is SshTransportErrorCode.CONNECTION_FAILED
    assert starter.calls == []
    tunnel.close()


def test_core_tunnel_rejects_peer_fd_replacement_after_child_spawn(
    tmp_path: Path,
) -> None:
    replacement_local, replacement_peer = socket.socketpair(
        socket.AF_UNIX,
        socket.SOCK_STREAM,
    )
    process = FakeTunnelProcess()

    def replace_peer(_argv: list[str], stream_fd: int) -> FakeTunnelProcess:
        os.dup2(replacement_peer.fileno(), stream_fd)
        return process

    tunnel = _transport(
        tmp_path,
        core_connection_starter=replace_peer,
    ).open_core_tunnel(remote_port=8765)
    try:
        with pytest.raises(SshTransportError) as rejected:
            tunnel.open_verified_socket(timeout_seconds=1.0)

        assert rejected.value.code is SshTransportErrorCode.CONNECTION_FAILED
        assert process.terminated is True
        assert process.waited is True
        assert tunnel._endpoint._children == {}
    finally:
        tunnel.close()
        replacement_local.close()
        replacement_peer.close()


def test_core_tunnel_rejects_child_that_exits_during_connection_start(
    tmp_path: Path,
) -> None:
    process = FakeTunnelProcess()
    process.return_code = 255

    tunnel = _transport(
        tmp_path,
        core_connection_starter=lambda _argv, _fd: process,
    ).open_core_tunnel(remote_port=8765)

    with pytest.raises(SshTransportError) as rejected:
        tunnel.open_verified_socket(timeout_seconds=1.0)

    assert rejected.value.code is SshTransportErrorCode.CONNECTION_FAILED
    assert tunnel._endpoint._children == {}
    tunnel.close()


def test_core_tunnel_child_authority_poll_failure_fails_closed_and_cleans_up(
    tmp_path: Path,
) -> None:
    class PollFailureProcess(FakeTunnelProcess):
        def __init__(self) -> None:
            super().__init__()
            self.poll_fails = True

        def poll(self) -> int | None:
            if self.poll_fails:
                raise OSError("poll failed")
            return super().poll()

        def terminate(self) -> None:
            self.terminated = True
            self.poll_fails = False

    process = PollFailureProcess()
    tunnel = _transport(
        tmp_path,
        core_connection_starter=lambda _argv, _fd: process,
    ).open_core_tunnel(remote_port=8765)

    with pytest.raises(SshTransportError) as rejected:
        tunnel.open_verified_socket(timeout_seconds=1.0)

    assert rejected.value.code is SshTransportErrorCode.CONNECTION_FAILED
    assert process.terminated is True
    assert process.waited is True
    assert tunnel._endpoint._children == {}
    tunnel.close()


def test_core_tunnel_pre_registration_cancellation_quarantines_pending_child(
    tmp_path: Path,
) -> None:
    class Cancelled(BaseException):
        pass

    cancelled = Cancelled()

    class CancelOnIncrement(int):
        def __add__(self, other: object) -> int:
            assert other == 1
            raise cancelled

    class RecoverablePendingProcess(FakeTunnelProcess):
        cleanup_fails = True

        def poll(self) -> int | None:
            return None if self.cleanup_fails else super().poll()

        def terminate(self) -> None:
            self.terminated = True
            if not self.cleanup_fails:
                self.return_code = -15

        def wait(self, timeout: float | None = None) -> int:
            self.waited = True
            cleanup_started.set()
            if self.cleanup_fails:
                release_cleanup.wait(timeout=5)
                raise subprocess.TimeoutExpired("ssh", timeout)
            assert self.return_code is not None
            return self.return_code

        def kill(self) -> None:
            self.killed = True
            if not self.cleanup_fails:
                self.return_code = -9

    cleanup_started = threading.Event()
    release_cleanup = threading.Event()
    process = RecoverablePendingProcess()
    starts = 0

    def start(_argv: list[str], _stream_fd: int) -> FakeTunnelProcess:
        nonlocal starts
        starts += 1
        return process

    tunnel = _transport(
        tmp_path,
        core_connection_starter=start,
    ).open_core_tunnel(remote_port=8765)
    tunnel._endpoint._next_generation = CancelOnIncrement(0)
    results: dict[str, BaseException | object] = {}
    first_done = threading.Event()
    second_done = threading.Event()

    def open_connection(name: str, done: threading.Event) -> None:
        try:
            results[name] = tunnel.open_verified_socket(timeout_seconds=1.0)
        except BaseException as exc:
            results[name] = exc
        finally:
            done.set()

    first = threading.Thread(target=open_connection, args=("first", first_done))
    second = threading.Thread(target=open_connection, args=("second", second_done))
    try:
        first.start()
        assert cleanup_started.wait(timeout=2)
        assert tunnel._endpoint._pending_child is not None
        assert tunnel._endpoint._pending_child.process is process
        assert tunnel._endpoint._close_requested is True
        assert id(tunnel._endpoint) in ssh_module._ORPHANED_CORE_TUNNELS

        second.start()
        assert second_done.wait(timeout=2)
        assert isinstance(results["second"], SshTransportError)
        assert results["second"].code is SshTransportErrorCode.CONNECTION_FAILED
        assert starts == 1

        release_cleanup.set()
        first.join(timeout=5)
        second.join(timeout=5)

        assert first_done.is_set()
        assert results["first"] is cancelled
        assert process.terminated is True
        assert process.waited is True
        assert process.killed is True
        assert tunnel._endpoint._children == {}
        assert tunnel._endpoint._pending_child is not None
        assert tunnel._endpoint._pending_child.process is process
        assert tunnel._endpoint._trust_lease is not None
        assert tunnel.closed is False

        process.cleanup_fails = False
        process.return_code = -9
        ssh_module._retry_orphaned_tunnel_cleanup()
        assert tunnel.closed is True
        assert tunnel._endpoint._pending_child is None
        assert tunnel._endpoint._trust_lease is None
        assert id(tunnel._endpoint) not in ssh_module._ORPHANED_CORE_TUNNELS
    finally:
        release_cleanup.set()
        first.join(timeout=5)
        if second.ident is not None:
            second.join(timeout=5)
        process.cleanup_fails = False
        process.return_code = -9
        if not tunnel.closed:
            try:
                tunnel.close()
            except BaseException:
                pass


@pytest.mark.parametrize("insert_before_failure", [False, True])
def test_core_tunnel_registry_insertion_failure_cleans_unregistered_child(
    tmp_path: Path,
    insert_before_failure: bool,
) -> None:
    class FailingChildRegistry(dict[int, FakeTunnelProcess]):
        attempts = 0

        def __setitem__(self, key: int, value: FakeTunnelProcess) -> None:
            self.attempts += 1
            if insert_before_failure:
                super().__setitem__(key, value)
            raise OSError("child registry insertion failed")

    class KillConfirmedProcess(FakeTunnelProcess):
        terminate_calls = 0
        wait_calls = 0
        kill_calls = 0

        def poll(self) -> int | None:
            return self.return_code

        def terminate(self) -> None:
            self.terminate_calls += 1
            self.terminated = True

        def wait(self, timeout: float | None = None) -> int:
            self.wait_calls += 1
            self.waited = True
            if self.return_code is None:
                raise subprocess.TimeoutExpired("ssh", timeout)
            return self.return_code

        def kill(self) -> None:
            self.kill_calls += 1
            self.killed = True
            self.return_code = -9

    registry = FailingChildRegistry()
    process = KillConfirmedProcess()
    starts = 0

    def start(_argv: list[str], _stream_fd: int) -> FakeTunnelProcess:
        nonlocal starts
        starts += 1
        return process

    tunnel = _transport(
        tmp_path,
        core_connection_starter=start,
    ).open_core_tunnel(remote_port=8765)
    tunnel._endpoint._children = registry

    with pytest.raises(SshTransportError) as rejected:
        tunnel.open_verified_socket(timeout_seconds=1.0)

    assert rejected.value.code is SshTransportErrorCode.CONNECTION_FAILED
    assert rejected.value.__cause__ is None
    assert rejected.value.__context__ is None
    assert registry.attempts == 1
    assert process.terminated is True
    assert process.waited is True
    assert process.killed is True
    assert process.terminate_calls == 1
    assert process.wait_calls == 2
    assert process.kill_calls == 1
    assert tunnel._endpoint._pending_child is None
    assert tunnel.closed is True

    with pytest.raises(SshTransportError) as closed:
        tunnel.open_verified_socket(timeout_seconds=1.0)

    assert closed.value.code is SshTransportErrorCode.CONNECTION_FAILED
    assert starts == 1


def test_core_tunnel_verify_authority_failure_permanently_poisons_endpoint(
    tmp_path: Path,
) -> None:
    class RecoverableAuthorityProcess(FakeTunnelProcess):
        authority_fails = False
        cleanup_fails = True

        def poll(self) -> int | None:
            if self.authority_fails:
                raise OSError("authority probe failed")
            return None if self.cleanup_fails else super().poll()

        def terminate(self) -> None:
            self.terminated = True
            if not self.cleanup_fails:
                self.return_code = -15

        def wait(self, timeout: float | None = None) -> int:
            self.waited = True
            if self.cleanup_fails:
                raise subprocess.TimeoutExpired("ssh", timeout)
            assert self.return_code is not None
            return self.return_code

        def kill(self) -> None:
            self.killed = True
            if not self.cleanup_fails:
                self.return_code = -9

    process = RecoverableAuthorityProcess()
    starts = 0

    def start(_argv: list[str], _stream_fd: int) -> FakeTunnelProcess:
        nonlocal starts
        starts += 1
        return process

    tunnel = _transport(
        tmp_path,
        core_connection_starter=start,
    ).open_core_tunnel(remote_port=8765)
    connection = tunnel.open_verified_socket(timeout_seconds=1.0)
    connection.close()
    process.authority_fails = True

    with pytest.raises(SshTransportError) as rejected:
        tunnel.verify_authority()

    assert rejected.value.code is SshTransportErrorCode.CONNECTION_FAILED
    assert process.terminated is True
    assert process.waited is True
    assert process.killed is True
    assert tunnel._endpoint._close_requested is True
    assert id(tunnel._endpoint) in ssh_module._ORPHANED_CORE_TUNNELS

    process.authority_fails = False
    with pytest.raises(SshTransportError) as poisoned:
        tunnel.open_verified_socket(timeout_seconds=1.0)

    assert poisoned.value.code is SshTransportErrorCode.CONNECTION_FAILED
    assert starts == 1

    process.cleanup_fails = False
    ssh_module._retry_orphaned_tunnel_cleanup()
    assert tunnel.closed is True
    assert id(tunnel._endpoint) not in ssh_module._ORPHANED_CORE_TUNNELS


def test_core_tunnel_identity_failure_poisons_endpoint_when_child_exit_is_unconfirmed(
    tmp_path: Path,
) -> None:
    class RecoverableProcess(FakeTunnelProcess):
        cleanup_fails = True

        def poll(self) -> int | None:
            return None if self.cleanup_fails else super().poll()

        def terminate(self) -> None:
            self.terminated = True
            if not self.cleanup_fails:
                self.return_code = -15

        def wait(self, timeout: float | None = None) -> int:
            self.waited = True
            if self.cleanup_fails:
                raise subprocess.TimeoutExpired("ssh", timeout)
            assert self.return_code is not None
            return self.return_code

        def kill(self) -> None:
            self.killed = True
            if not self.cleanup_fails:
                self.return_code = -9

    replacement_local, replacement_peer = socket.socketpair(
        socket.AF_UNIX,
        socket.SOCK_STREAM,
    )
    process = RecoverableProcess()
    starts = 0

    def replace_peer(_argv: list[str], stream_fd: int) -> FakeTunnelProcess:
        nonlocal starts
        starts += 1
        os.dup2(replacement_peer.fileno(), stream_fd)
        return process

    tunnel = _transport(
        tmp_path,
        core_connection_starter=replace_peer,
    ).open_core_tunnel(remote_port=8765)
    try:
        with pytest.raises(SshTransportError) as rejected:
            tunnel.open_verified_socket(timeout_seconds=1.0)

        assert rejected.value.code is SshTransportErrorCode.CONNECTION_FAILED
        assert process.terminated is True
        assert process.waited is True
        assert process.killed is True
        assert tunnel._endpoint._close_requested is True
        assert id(tunnel._endpoint) in ssh_module._ORPHANED_CORE_TUNNELS

        with pytest.raises(SshTransportError) as poisoned:
            tunnel.open_verified_socket(timeout_seconds=1.0)

        assert poisoned.value.code is SshTransportErrorCode.CONNECTION_FAILED
        assert starts == 1

        process.cleanup_fails = False
        ssh_module._retry_orphaned_tunnel_cleanup()
        assert tunnel.closed is True
        assert id(tunnel._endpoint) not in ssh_module._ORPHANED_CORE_TUNNELS
    finally:
        replacement_local.close()
        replacement_peer.close()


@pytest.mark.parametrize("closed_side", ["local", "peer"])
def test_core_tunnel_closed_connection_fd_preserves_typed_failure_and_closes_endpoint(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    closed_side: str,
) -> None:
    socketpair = socket.socketpair
    pairs: list[tuple[socket.socket, socket.socket]] = []
    process = FakeTunnelProcess()
    starts = 0

    def recording_socketpair(
        family: socket.AddressFamily,
        socket_type: socket.SocketKind,
    ) -> tuple[socket.socket, socket.socket]:
        streams = socketpair(family, socket_type)
        pairs.append(streams)
        return streams

    def close_connection_fd(_argv: list[str], _stream_fd: int) -> FakeTunnelProcess:
        nonlocal starts
        starts += 1
        selected = pairs[-1][0 if closed_side == "local" else 1]
        os.close(selected.fileno())
        return process

    monkeypatch.setattr(ssh_module.socket, "socketpair", recording_socketpair)
    tunnel = _transport(
        tmp_path,
        core_connection_starter=close_connection_fd,
    ).open_core_tunnel(remote_port=8765)
    try:
        with pytest.raises(SshTransportError) as rejected:
            tunnel.open_verified_socket(timeout_seconds=1.0)

        assert rejected.value.code is SshTransportErrorCode.CONNECTION_FAILED
        assert rejected.value.__cause__ is None
        assert rejected.value.__context__ is None
        assert process.terminated is True
        assert process.waited is True
        assert tunnel.closed is True

        with pytest.raises(SshTransportError) as closed:
            tunnel.open_verified_socket(timeout_seconds=1.0)

        assert closed.value.code is SshTransportErrorCode.CONNECTION_FAILED
        assert starts == 1
    finally:
        if not tunnel.closed:
            tunnel.close()
        for streams in pairs:
            for stream in streams:
                try:
                    stream.close()
                except OSError:
                    pass


def test_core_tunnel_poisons_before_dual_close_failures_and_concurrent_open(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class RecoverableProcess(FakeTunnelProcess):
        cleanup_fails = True

        def poll(self) -> int | None:
            return None if self.cleanup_fails else super().poll()

        def terminate(self) -> None:
            self.terminated = True
            if not self.cleanup_fails:
                self.return_code = -15

        def wait(self, timeout: float | None = None) -> int:
            self.waited = True
            if self.cleanup_fails:
                raise subprocess.TimeoutExpired("ssh", timeout)
            assert self.return_code is not None
            return self.return_code

        def kill(self) -> None:
            self.killed = True
            if not self.cleanup_fails:
                self.return_code = -9

    class PausingCloseSocket:
        def __init__(self, stream: socket.socket, side: str) -> None:
            self._stream = stream
            self._side = side

        def __getattr__(self, name: str) -> object:
            return getattr(self._stream, name)

        def close(self) -> None:
            close_calls[self._side] += 1
            if self._side == "local":
                local_close_started.set()
                release_local_close.wait(timeout=5)
            try:
                self._stream.close()
            except OSError:
                close_failures[self._side] += 1
                raise
            close_failures[self._side] += 1
            raise OSError(errno.EBADF, "injected closed socket")

    socketpair = socket.socketpair
    raw_pairs: list[tuple[socket.socket, socket.socket]] = []
    close_calls = {"local": 0, "peer": 0}
    close_failures = {"local": 0, "peer": 0}
    local_close_started = threading.Event()
    release_local_close = threading.Event()
    first_process = RecoverableProcess()
    later_processes: list[FakeTunnelProcess] = []
    starts = 0

    def pausing_socketpair(
        family: socket.AddressFamily,
        socket_type: socket.SocketKind,
    ) -> tuple[PausingCloseSocket, PausingCloseSocket]:
        local_stream, child_stream = socketpair(family, socket_type)
        raw_pairs.append((local_stream, child_stream))
        return (
            PausingCloseSocket(local_stream, "local"),
            PausingCloseSocket(child_stream, "peer"),
        )

    def close_both_fds(_argv: list[str], _stream_fd: int) -> FakeTunnelProcess:
        nonlocal starts
        starts += 1
        if starts == 1:
            local_stream, child_stream = raw_pairs[-1]
            os.close(local_stream.fileno())
            os.close(child_stream.fileno())
            return first_process
        process = FakeTunnelProcess()
        later_processes.append(process)
        return process

    monkeypatch.setattr(ssh_module.socket, "socketpair", pausing_socketpair)
    tunnel = _transport(
        tmp_path,
        core_connection_starter=close_both_fds,
    ).open_core_tunnel(remote_port=8765)
    results: dict[str, BaseException | object] = {}
    first_done = threading.Event()
    second_done = threading.Event()

    def open_connection(name: str, done: threading.Event) -> None:
        try:
            results[name] = tunnel.open_verified_socket(timeout_seconds=1.0)
        except BaseException as exc:
            results[name] = exc
        finally:
            done.set()

    first = threading.Thread(target=open_connection, args=("first", first_done))
    second = threading.Thread(target=open_connection, args=("second", second_done))
    try:
        first.start()
        assert local_close_started.wait(timeout=2)
        second.start()
        second_rejected_during_cleanup = second_done.wait(timeout=2)
        release_local_close.set()
        first.join(timeout=5)
        second.join(timeout=5)

        assert second_rejected_during_cleanup is True
        assert first_done.is_set()
        assert second_done.is_set()
        assert isinstance(results["first"], SshTransportError)
        assert results["first"].code is SshTransportErrorCode.CONNECTION_FAILED
        assert results["first"].__cause__ is None
        assert results["first"].__context__ is None
        assert isinstance(results["second"], SshTransportError)
        assert results["second"].code is SshTransportErrorCode.CONNECTION_FAILED
        assert starts == 1
        assert later_processes == []
        assert close_calls == {"local": 1, "peer": 1}
        assert close_failures == {"local": 1, "peer": 1}
        assert first_process.terminated is True
        assert first_process.waited is True
        assert first_process.killed is True
        assert tunnel._endpoint._close_requested is True
        assert id(tunnel._endpoint) in ssh_module._ORPHANED_CORE_TUNNELS
        assert tunnel.closed is False

        first_process.cleanup_fails = False
        ssh_module._retry_orphaned_tunnel_cleanup()
        assert tunnel.closed is True
        assert id(tunnel._endpoint) not in ssh_module._ORPHANED_CORE_TUNNELS
    finally:
        release_local_close.set()
        first.join(timeout=5)
        if second.ident is not None:
            second.join(timeout=5)
        for result in results.values():
            if isinstance(result, PausingCloseSocket):
                try:
                    result.close()
                except OSError:
                    pass
        first_process.cleanup_fails = False
        if not tunnel.closed:
            try:
                tunnel.close()
            except BaseException:
                pass
        for local_stream, child_stream in raw_pairs:
            for stream in (local_stream, child_stream):
                try:
                    stream.close()
                except OSError:
                    pass


def test_core_connection_authority_passes_birth_and_peer_fds_to_exact_ssh_child(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observed: list[tuple[list[str], dict[str, object]]] = []
    process = FakeTunnelProcess()
    process.pid = 424_242
    process.returncode = None

    def popen(argv: list[str], **kwargs: object) -> FakeTunnelProcess:
        observed.append((argv, kwargs))
        return process

    monkeypatch.setattr(ssh_module, "_TUNNEL_SUBPROCESS_POPEN", popen)
    monkeypatch.setattr(
        ssh_module._OwnedSubprocessAuthority,
        "initialize_observer",
        lambda _self: None,
    )
    argv = [
        ssh_module.SSH_EXECUTABLE,
        "-F",
        "/dev/null",
        "-W",
        "127.0.0.1:8765",
        "--",
        "host",
    ]
    authority = ssh_module._OwnedSubprocessAuthority(trust_ownership=None)
    authority.acquire()

    authority.spawn_tunnel(argv, stream_fd=42)

    assert authority.process is process
    assert len(observed) == 1
    actual_argv, kwargs = observed[0]
    birth_fd = int(actual_argv[5])
    executable_fd = int(actual_argv[6])
    assert actual_argv == [
        sys.executable,
        "-I",
        "-c",
        ssh_module._SUBPROCESS_BIRTH_LAUNCHER,
        ssh_module.OWNED_SUBPROCESS_BIRTH_ARGUMENT,
        str(birth_fd),
        str(executable_fd),
        *argv,
    ]
    assert kwargs == {
        "stdin": 42,
        "stdout": 42,
        "stderr": subprocess.DEVNULL,
        "close_fds": True,
        "pass_fds": (birth_fd, executable_fd, 42),
        "text": False,
        "start_new_session": True,
        "executable": sys.executable,
        "env": {},
    }
    process.returncode = 0
    authority.mark_group_cleanup_confirmed()
    assert authority.release() is True


def test_core_connection_subprocess_bridges_a_real_parent_owned_af_unix_stream() -> None:
    local_stream, child_stream = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
    authority = ssh_module._OwnedSubprocessAuthority(trust_ownership=None)
    authority.acquire()
    try:
        authority.spawn_tunnel(
            [
                sys.executable,
                "-c",
                (
                    "import sys; data=sys.stdin.buffer.read(); "
                    "sys.stdout.buffer.write(data.upper()); sys.stdout.buffer.flush()"
                ),
            ],
            stream_fd=child_stream.fileno(),
        )
        child_stream.close()
        local_stream.sendall(b"core relay")
        local_stream.shutdown(socket.SHUT_WR)
        assert local_stream.recv(64) == b"CORE RELAY"
        authority.cleanup()
        assert authority.process is not None
        assert authority.process.returncode is not None
        assert authority.released is True
    finally:
        local_stream.close()
        child_stream.close()
        if not authority.released:
            authority.cleanup()


def test_core_tunnel_close_quarantines_unconfirmed_connection_child(
    tmp_path: Path,
) -> None:
    class RecoverableProcess(FakeTunnelProcess):
        def __init__(self) -> None:
            super().__init__()
            self.cleanup_fails = True

        def terminate(self) -> None:
            if self.cleanup_fails:
                raise OSError("terminate failed")
            self.return_code = -15

        def wait(self, timeout: float | None = None) -> int:
            if self.cleanup_fails:
                raise subprocess.TimeoutExpired("ssh", timeout)
            assert self.return_code is not None
            return self.return_code

        def kill(self) -> None:
            if self.cleanup_fails:
                raise OSError("kill failed")
            self.return_code = -9

    process = RecoverableProcess()

    def start(_argv: list[str], _stream_fd: int) -> FakeTunnelProcess:
        return process

    tunnel = _transport(
        tmp_path,
        core_connection_starter=start,
    ).open_core_tunnel(remote_port=8765)
    connection = tunnel.open_verified_socket(timeout_seconds=1.0)
    connection.close()

    with pytest.raises(OSError):
        tunnel.close()

    assert id(tunnel._endpoint) in ssh_module._ORPHANED_CORE_TUNNELS
    assert tunnel.closed is False
    process.cleanup_fails = False
    ssh_module._retry_orphaned_tunnel_cleanup()
    assert tunnel.closed is True
    assert id(tunnel._endpoint) not in ssh_module._ORPHANED_CORE_TUNNELS


def test_core_tunnel_close_quarantines_lease_cleanup_cancellation() -> None:
    class Cancelled(BaseException):
        pass

    class Lease:
        cleanup_fails = True

        def __exit__(self, *_args: object) -> None:
            if self.cleanup_fails:
                raise Cancelled

    class TrustedHost:
        def _register_tunnel(self, _closer) -> object:
            return lambda: None

    lease = Lease()
    endpoint = ssh_module._CoreTunnelEndpoint(
        connection_starter=lambda _argv, _fd: FakeTunnelProcess(),
        connection_argv=["ssh"],
        trust_lease=lease,
        trusted_host=TrustedHost(),
        process_environment={},
    )

    with pytest.raises(Cancelled):
        endpoint.close()

    assert endpoint.closed is False
    assert id(endpoint) in ssh_module._ORPHANED_CORE_TUNNELS
    lease.cleanup_fails = False
    ssh_module._retry_orphaned_tunnel_cleanup()
    assert endpoint.closed is True
    assert id(endpoint) not in ssh_module._ORPHANED_CORE_TUNNELS


def test_untransferred_trust_lease_cleanup_failure_is_retryable() -> None:
    class Lease:
        cleanup_fails = True

        def __exit__(self, *_args: object) -> None:
            if self.cleanup_fails:
                raise OSError("lease cleanup failed")

    lease = Lease()
    ssh_module._release_trust_lease(lease)
    assert id(lease) in ssh_module._ORPHANED_TRUST_LEASES
    lease.cleanup_fails = False
    ssh_module._retry_orphaned_tunnel_cleanup()
    assert id(lease) not in ssh_module._ORPHANED_TRUST_LEASES


def test_open_tunnel_thread_start_failure_cleans_process_registration_and_lease(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class StubbornTunnelProcess(FakeTunnelProcess):
        def poll(self) -> int | None:
            return 0 if self.killed else None

        def wait(self, timeout: float | None = None) -> int:
            self.waited = True
            if not self.killed:
                raise subprocess.TimeoutExpired("ssh", timeout)
            return 0

    class StubbornTunnelStarter(RecordingTunnelStarter):
        def __call__(self, argv: list[str]) -> FakeTunnelProcess:
            self.calls.append(argv)
            process = StubbornTunnelProcess()
            self.processes.append(process)
            return process

    profile = _profile()
    binding = _trusted_binding(tmp_path, profile)
    starter = StubbornTunnelStarter()
    transport = SshRemoteExecutorTransport(
        profile,
        trusted_host=binding,
        runner=RecordingRunner(),
        tunnel_starter=starter,
        port_allocator=lambda: 49155,
    )

    def fail_start(self: threading.Thread) -> None:
        raise RuntimeError("SECRET_THREAD_START_FAILURE")

    monkeypatch.setattr(threading.Thread, "start", fail_start)

    with pytest.raises(SshTransportError) as exc_info:
        transport.open_tunnel(remote_port=8765, wait_for_ready=False)

    error = exc_info.value
    process = starter.processes[0]
    lease_path = Path(
        next(
            value.removeprefix("UserKnownHostsFile=")
            for value in starter.calls[0]
            if value.startswith("UserKnownHostsFile=")
        )
    )
    assert error.code is SshTransportErrorCode.START_FAILED
    assert process.terminated is True
    assert process.waited is True
    assert process.killed is True
    assert not lease_path.exists()
    assert error.__cause__ is None
    assert error.__context__ is None
    assert "SECRET_THREAD_START_FAILURE" not in "".join(traceback.format_exception(error))

    store = ProviderKnownHostStore(
        binding.known_hosts_file.parent,
        runner=lambda argv, timeout: subprocess.CompletedProcess(argv, 1),
        lock_timeout_seconds=0.05,
    )
    store.revoke(profile, expected_fingerprint=binding.fingerprint)


def test_open_tunnel_constructor_cancellation_cleans_child_and_propagates(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Cancelled(BaseException):
        pass

    starter = RecordingTunnelStarter()
    transport = _transport(
        tmp_path,
        tunnel_starter=starter,
        port_allocator=lambda: 49155,
    )

    def cancel_start(_self: threading.Thread) -> None:
        raise Cancelled

    monkeypatch.setattr(threading.Thread, "start", cancel_start)

    with pytest.raises(Cancelled):
        transport.open_tunnel(remote_port=8765, wait_for_ready=False)

    assert starter.processes[0].terminated is True
    assert starter.processes[0].waited is True


def test_failed_tunnel_construction_retains_lease_until_exit_is_confirmed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class RecoverableCleanupProcess(FakeTunnelProcess):
        def __init__(self) -> None:
            super().__init__()
            self.cleanup_fails = True
            self.terminate_calls = 0
            self.wait_calls = 0
            self.kill_calls = 0

        def poll(self) -> int | None:
            return self.return_code

        def terminate(self) -> None:
            self.terminate_calls += 1
            if self.cleanup_fails:
                raise OSError("SECRET_TERMINATE_FAILURE")
            self.return_code = -15

        def wait(self, timeout: float | None = None) -> int:
            self.wait_calls += 1
            if self.cleanup_fails:
                raise OSError("SECRET_WAIT_FAILURE")
            if self.return_code is None:
                raise subprocess.TimeoutExpired("ssh", timeout)
            return self.return_code

        def kill(self) -> None:
            self.kill_calls += 1
            if self.cleanup_fails:
                raise OSError("SECRET_KILL_FAILURE")
            self.return_code = -9

    class RecoverableCleanupStarter(RecordingTunnelStarter):
        def __call__(self, argv: list[str]) -> FakeTunnelProcess:
            self.calls.append(argv)
            process = RecoverableCleanupProcess()
            self.processes.append(process)
            return process

    profile = _profile()
    binding = _trusted_binding(tmp_path, profile)
    starter = RecoverableCleanupStarter()
    transport = SshRemoteExecutorTransport(
        profile,
        trusted_host=binding,
        runner=RecordingRunner(),
        tunnel_starter=starter,
        port_allocator=lambda: 49155,
    )

    def fail_start(self: threading.Thread) -> None:
        raise RuntimeError("SECRET_THREAD_START_FAILURE")

    monkeypatch.setattr(threading.Thread, "start", fail_start)

    with pytest.raises(SshTransportError) as exc_info:
        transport.open_tunnel(remote_port=8765, wait_for_ready=False)

    error = exc_info.value
    process = starter.processes[0]
    assert isinstance(process, RecoverableCleanupProcess)
    lease_path = Path(
        next(
            value.removeprefix("UserKnownHostsFile=")
            for value in starter.calls[0]
            if value.startswith("UserKnownHostsFile=")
        )
    )
    orphan = next(
        tunnel for tunnel in ssh_module._ORPHANED_TUNNELS.values() if tunnel.process is process
    )
    assert error.code is SshTransportErrorCode.START_FAILED
    assert error.__cause__ is None
    assert error.__context__ is None
    assert "SECRET_" not in "".join(traceback.format_exception(error))
    assert process.terminate_calls == 1
    assert process.wait_calls == 2
    assert process.kill_calls == 1
    assert orphan.closed is False
    assert lease_path.exists()

    store = ProviderKnownHostStore(
        binding.known_hosts_file.parent,
        runner=lambda argv, timeout: subprocess.CompletedProcess(argv, 1),
        lock_timeout_seconds=0.05,
    )
    with pytest.raises(HostKeyStoreError) as revoke_error:
        store.revoke(profile, expected_fingerprint=binding.fingerprint)

    assert revoke_error.value.code is HostKeyStoreErrorCode.HOST_KEY_IN_USE
    assert orphan.closed is False
    assert lease_path.exists()
    assert binding.known_hosts_file.exists()
    assert process.terminate_calls == 2
    assert process.wait_calls == 4
    assert process.kill_calls == 2

    process.cleanup_fails = False
    store.revoke(profile, expected_fingerprint=binding.fingerprint)

    assert orphan.closed is True
    assert not lease_path.exists()
    assert not binding.known_hosts_file.exists()
    assert id(orphan) not in ssh_module._ORPHANED_TUNNELS
    assert process.terminate_calls == 3
    assert process.wait_calls == 5
    assert process.kill_calls == 2
    cleanup_calls = (
        process.terminate_calls,
        process.wait_calls,
        process.kill_calls,
    )
    ssh_module._retry_orphaned_tunnel_cleanup()
    assert cleanup_calls == (
        process.terminate_calls,
        process.wait_calls,
        process.kill_calls,
    )


def test_ssh_errors_do_not_expose_secret_exception_state(
    tmp_path: Path,
) -> None:
    secrets = (
        "SECRET_REMOTE_COMMAND",
        "SECRET_STDERR",
        "SECRET_STDOUT",
        "SECRET_LOCAL_PATH",
        "SECRET_REMOTE_PATH",
        "SECRET_LEASE_TOKEN",
    )

    class SecretFailureRunner(RecordingRunner):
        def __call__(
            self, argv: list[str], timeout_seconds: float
        ) -> subprocess.CompletedProcess[str]:
            self.calls.append((argv, timeout_seconds))
            raise OSError(" ".join(secrets))

    transport = _transport(tmp_path, runner=SecretFailureRunner())

    captured: SshTransportError | None = None
    try:
        secret_command = secrets[0]
        transport.run(secret_command)
    except SshTransportError as error:
        captured = error
        rendered = "".join(traceback.format_exception(error))
        chain = (error.__cause__, error.__context__)
    else:  # pragma: no cover - the injected runner always fails
        raise AssertionError("SSH failure was not raised")

    assert captured is not None
    assert captured.code is SshTransportErrorCode.START_FAILED
    assert chain == (None, None)
    for secret in secrets:
        assert secret not in str(captured)
        assert secret not in rendered


def test_ssh_timeout_discards_command_output_and_exception_chain(tmp_path: Path) -> None:
    secrets = (
        "SECRET_TIMEOUT_COMMAND",
        "SECRET_TIMEOUT_STDOUT",
        "SECRET_TIMEOUT_STDERR",
    )

    class SecretTimeoutRunner(RecordingRunner):
        def __call__(
            self, argv: list[str], timeout_seconds: float
        ) -> subprocess.CompletedProcess[str]:
            self.calls.append((argv, timeout_seconds))
            raise subprocess.TimeoutExpired(
                [secrets[0]],
                timeout_seconds,
                output=secrets[1],
                stderr=secrets[2],
            )

    transport = _transport(tmp_path, runner=SecretTimeoutRunner())
    secret_command = secrets[0]

    with pytest.raises(SshTransportError) as exc_info:
        transport.run(secret_command)

    error = exc_info.value
    rendered = "".join(traceback.format_exception(error))
    assert error.code is SshTransportErrorCode.TIMEOUT
    assert error.__cause__ is None
    assert error.__context__ is None
    for secret in secrets:
        assert secret not in str(error)
        assert secret not in rendered


def test_default_runner_bounds_output_and_reaps_overflowing_process(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pid_path = tmp_path / "overflow-pids.json"
    producer = tmp_path / "produce-output.py"
    producer.write_text(
        "\n".join(
            (
                "import os",
                "import json",
                "import subprocess",
                "import sys",
                "import time",
                "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'])",
                "with open(sys.argv[1], 'w', encoding='ascii') as stream:",
                "    json.dump([os.getpid(), os.getpgid(0), os.getsid(0), child.pid], stream)",
                "    stream.flush()",
                "    os.fsync(stream.fileno())",
                "os.write(1, b'SECRET_STDOUT_CANARY' * (3 * 1024 * 1024 // 20))",
                "os.write(2, b'SECRET_STDERR_CANARY' * (3 * 1024 * 1024 // 20))",
                "time.sleep(30)",
            )
        ),
        encoding="ascii",
    )
    transport = _transport(tmp_path)
    monkeypatch.setattr(
        transport,
        "_ssh_argv",
        lambda _command, _known_hosts: [sys.executable, str(producer), str(pid_path)],
    )

    secret_command = "SECRET_REMOTE_COMMAND"
    with pytest.raises(SshTransportError) as exc_info:
        transport.run(secret_command, timeout_seconds=10)

    assert exc_info.value.code is SshTransportErrorCode.START_FAILED
    rendered = "".join(traceback.format_exception(exc_info.value))
    assert "SECRET_REMOTE_COMMAND" not in rendered
    assert "SECRET_STDOUT_CANARY" not in rendered
    assert "SECRET_STDERR_CANARY" not in rendered
    leader_id, process_group_id, session_id, descendant_id = json.loads(
        pid_path.read_text(encoding="ascii")
    )
    assert (process_group_id, session_id) == (leader_id, leader_id)
    _assert_processes_gone(leader_id, descendant_id)


def test_default_runner_timeout_terminates_and_reaps_entire_process_group(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pid_path = tmp_path / "timeout-pids.json"
    producer = tmp_path / "timeout-with-descendant.py"
    producer.write_text(
        "\n".join(
            (
                "import json",
                "import os",
                "import subprocess",
                "import sys",
                "import time",
                "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'])",
                "with open(sys.argv[1], 'w', encoding='ascii') as stream:",
                "    json.dump([os.getpid(), child.pid], stream)",
                "    stream.flush()",
                "    os.fsync(stream.fileno())",
                "time.sleep(30)",
            )
        ),
        encoding="ascii",
    )
    transport = _transport(tmp_path)
    monkeypatch.delattr(ssh_module.os, "waitid", raising=False)
    monkeypatch.setattr(ssh_module, "_SUBPROCESS_TERMINATE_GRACE_SECONDS", 0.05)
    monkeypatch.setattr(
        transport,
        "_ssh_argv",
        lambda _command, _known_hosts: [sys.executable, str(producer), str(pid_path)],
    )

    with pytest.raises(SshTransportError) as exc_info:
        transport.run("private command", timeout_seconds=0.2)

    assert exc_info.value.code is SshTransportErrorCode.TIMEOUT
    leader_id, descendant_id = json.loads(pid_path.read_text(encoding="ascii"))
    _assert_processes_gone(leader_id, descendant_id)


@pytest.mark.parametrize("without_waitid", [False, True], ids=("waitid", "portable"))
@pytest.mark.parametrize("leader_return_code", [0, 9])
def test_default_runner_returns_leader_result_when_descendant_inherits_pipes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    leader_return_code: int,
    without_waitid: bool,
) -> None:
    pid_path = tmp_path / f"inherited-pipes-{leader_return_code}.json"
    producer = tmp_path / f"leader-exits-{leader_return_code}.py"
    descendant_script = (
        "import os,time;"
        "os.write(1,b'descendant stdout\\n');"
        "os.write(2,b'descendant stderr\\n');"
        "time.sleep(30)"
    )
    producer.write_text(
        "\n".join(
            (
                "import json",
                "import os",
                "import subprocess",
                "import sys",
                f"child = subprocess.Popen([sys.executable, '-c', {descendant_script!r}])",
                "with open(sys.argv[1], 'w', encoding='ascii') as stream:",
                "    json.dump([os.getpid(), child.pid], stream)",
                "    stream.flush()",
                "    os.fsync(stream.fileno())",
                "print('leader stdout', flush=True)",
                "print('leader stderr', file=sys.stderr, flush=True)",
                f"raise SystemExit({leader_return_code})",
            )
        ),
        encoding="ascii",
    )
    if without_waitid:
        monkeypatch.delattr(ssh_module.os, "waitid", raising=False)
    monkeypatch.setattr(ssh_module, "_SUBPROCESS_TERMINATE_GRACE_SECONDS", 0.05)
    monkeypatch.setattr(ssh_module, "_SUBPROCESS_DESCENDANT_PIPE_GRACE_SECONDS", 0.05)

    started = time.monotonic()
    completed = ssh_module._run_subprocess(
        [sys.executable, str(producer), str(pid_path)],
        0.4,
    )
    elapsed = time.monotonic() - started

    assert completed.returncode == leader_return_code
    assert set(completed.stdout.splitlines()) == {"leader stdout", "descendant stdout"}
    assert set(completed.stderr.splitlines()) == {"leader stderr", "descendant stderr"}
    assert elapsed < 0.4
    leader_id, descendant_id = json.loads(pid_path.read_text(encoding="ascii"))
    _assert_processes_gone(leader_id, descendant_id)


def test_default_runner_does_not_require_os_waitid(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pid_path = tmp_path / "no-waitid-pid"
    producer = tmp_path / "no-waitid.py"
    producer.write_text(
        "import os,sys\n"
        "open(sys.argv[1], 'w', encoding='ascii').write(str(os.getpid()))\n"
        "raise SystemExit(23)\n",
        encoding="ascii",
    )
    monkeypatch.delattr(ssh_module.os, "waitid", raising=False)

    completed = ssh_module._run_subprocess(
        [sys.executable, str(producer), str(pid_path)],
        1,
    )

    assert completed.returncode == 23
    _assert_processes_gone(int(pid_path.read_text(encoding="ascii")))


def test_default_runner_cancellation_terminates_and_reaps_entire_process_group(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pid_path = tmp_path / "cancel-pids.json"
    producer = tmp_path / "cancel-with-descendant.py"
    producer.write_text(
        "\n".join(
            (
                "import json",
                "import os",
                "import subprocess",
                "import sys",
                "import time",
                "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'])",
                "pid_path = sys.argv[1]",
                "pending_pid_path = f'{pid_path}.pending'",
                "with open(pending_pid_path, 'w', encoding='ascii') as stream:",
                "    json.dump([os.getpid(), child.pid], stream)",
                "    stream.flush()",
                "    os.fsync(stream.fileno())",
                "os.replace(pending_pid_path, pid_path)",
                "time.sleep(30)",
            )
        ),
        encoding="ascii",
    )

    spawned_process_ids: list[int] | None = None

    def cancel_after_spawn(*_args: object, **_kwargs: object) -> tuple[bytes, bytes]:
        nonlocal spawned_process_ids
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            try:
                spawned_process_ids = json.loads(pid_path.read_text(encoding="ascii"))
            except FileNotFoundError:
                time.sleep(0.01)
                continue
            break
        assert spawned_process_ids is not None
        raise KeyboardInterrupt

    monkeypatch.delattr(ssh_module.os, "waitid", raising=False)
    monkeypatch.setattr(ssh_module, "_SUBPROCESS_TERMINATE_GRACE_SECONDS", 0.05)
    monkeypatch.setattr(ssh_module, "_capture_subprocess_output", cancel_after_spawn)

    with pytest.raises(KeyboardInterrupt):
        ssh_module._run_subprocess(
            [sys.executable, str(producer), str(pid_path)],
            5,
        )

    assert spawned_process_ids is not None
    leader_id, descendant_id = spawned_process_ids
    _assert_processes_gone(leader_id, descendant_id)


def test_portable_observer_never_reaps_before_group_cleanup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original_isfile = os.path.isfile
    original_signal_group = ssh_module._signal_owned_process_group
    original_observe_group = ssh_module._observe_owned_process_group_states
    observed_returncodes: list[int | None] = []
    observed_group_returncodes: list[int | None] = []

    def fail_kqueue() -> object:
        raise OSError("kqueue unavailable")

    def record_signal_group(*args: object, **kwargs: object) -> None:
        process = args[0]
        assert isinstance(process, subprocess.Popen)
        observed_returncodes.append(process.returncode)
        original_signal_group(*args, **kwargs)

    def record_observe_group(*args: object, **kwargs: object) -> dict[int, str]:
        process = args[0]
        assert isinstance(process, subprocess.Popen)
        observed_group_returncodes.append(process.returncode)
        return original_observe_group(*args, **kwargs)

    monkeypatch.delattr(ssh_module.os, "waitid", raising=False)
    monkeypatch.setattr(ssh_module.select, "kqueue", fail_kqueue, raising=False)
    monkeypatch.setattr(
        ssh_module.os.path,
        "isfile",
        lambda path: False if str(path).startswith("/proc/") else original_isfile(path),
    )
    monkeypatch.setattr(ssh_module, "_signal_owned_process_group", record_signal_group)
    monkeypatch.setattr(ssh_module, "_observe_owned_process_group_states", record_observe_group)

    completed = ssh_module._run_subprocess(
        [sys.executable, "-c", "raise SystemExit(23)"],
        1,
    )

    assert completed.returncode == 23
    assert observed_returncodes
    assert observed_group_returncodes
    assert all(return_code is None for return_code in observed_returncodes)
    assert all(return_code is None for return_code in observed_group_returncodes)


def test_default_runner_entry_cancellation_releases_caller_owned_trust_lease(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    transport = _transport(tmp_path)
    lease_paths: list[Path] = []
    orphan_ids = set(ssh_module._ORPHANED_SUBPROCESSES)
    orphan_trust_ids = set(ssh_module._ORPHANED_TRUST_LEASES)

    def local_argv(_command: str, known_hosts_file: Path) -> list[str]:
        lease_paths.append(known_hosts_file)
        return [sys.executable, "-c", "raise SystemExit(0)"]

    def cancel_at_runner_entry(*_args: object, **_kwargs: object) -> object:
        raise KeyboardInterrupt

    monkeypatch.setattr(transport, "_ssh_argv", local_argv)
    monkeypatch.setattr(ssh_module, "_run_subprocess", cancel_at_runner_entry)

    with pytest.raises(KeyboardInterrupt):
        transport.run_secret("private command")

    assert len(lease_paths) == 1
    assert not lease_paths[0].exists()
    assert set(ssh_module._ORPHANED_SUBPROCESSES) == orphan_ids
    assert set(ssh_module._ORPHANED_TRUST_LEASES) == orphan_trust_ids


@pytest.mark.parametrize("interruption", [KeyboardInterrupt, SystemExit])
def test_secret_popen_return_cancellation_publishes_one_recoverable_owner(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    interruption: type[BaseException],
) -> None:
    transport = _transport(tmp_path)
    original_popen = ssh_module._OWNED_SUBPROCESS_POPEN
    original_observe_group = ssh_module._observe_owned_process_group_states
    orphan_ids = set(ssh_module._ORPHANED_SUBPROCESSES)
    orphan_trust_ids = set(ssh_module._ORPHANED_TRUST_LEASES)
    lease_paths: list[Path] = []
    started: list[subprocess.Popen[bytes]] = []

    def local_argv(_command: str, known_hosts_file: Path) -> list[str]:
        lease_paths.append(known_hosts_file)
        return [sys.executable, "-c", "import time; time.sleep(30)"]

    def spawn_then_cancel(*args: object, **kwargs: object) -> subprocess.Popen[bytes]:
        process = original_popen(*args, **kwargs)
        started.append(process)
        raise interruption

    def report_uninterruptible_member(
        process: subprocess.Popen[bytes],
        *,
        process_group_id: int,
    ) -> dict[int, str]:
        assert process_group_id == process.pid
        return {process.pid: "Z", process.pid + 1: "D"}

    monkeypatch.setattr(transport, "_ssh_argv", local_argv)
    monkeypatch.setattr(ssh_module, "_MAX_OWNED_SUBPROCESSES", 1)
    monkeypatch.setattr(ssh_module, "_SUBPROCESS_TERMINATE_GRACE_SECONDS", 0.01)
    monkeypatch.setattr(ssh_module, "_OWNED_SUBPROCESS_POPEN", spawn_then_cancel)
    monkeypatch.setattr(
        ssh_module,
        "_observe_owned_process_group_states",
        report_uninterruptible_member,
    )

    retained: ssh_module._OwnedSubprocessAuthority | None = None
    try:
        with pytest.raises(interruption):
            transport.run_secret("private command")

        new_authorities = tuple(
            authority
            for authority_id, authority in ssh_module._ORPHANED_SUBPROCESSES.items()
            if authority_id not in orphan_ids
        )
        assert len(started) == 1
        assert len(new_authorities) == 1
        retained = new_authorities[0]
        birth_record = retained._birth_record
        assert birth_record is not None
        assert birth_record.closed is False
        assert retained.process.pid == started[0].pid
        assert retained.process.returncode is None
        assert len(lease_paths) == 1
        assert lease_paths[0].exists()
        assert set(ssh_module._ORPHANED_TRUST_LEASES) == orphan_trust_ids
        with pytest.raises(RuntimeError, match="ownership capacity"):
            ssh_module._OwnedSubprocessAuthority.spawn(
                [sys.executable, "-c", "raise SystemExit(0)"],
            )

        monkeypatch.setattr(ssh_module, "_OWNED_SUBPROCESS_POPEN", original_popen)
        monkeypatch.setattr(
            ssh_module,
            "_observe_owned_process_group_states",
            original_observe_group,
        )
        ssh_module._retry_orphaned_subprocess_cleanup()

        assert retained.process.returncode is not None
        assert birth_record.closed is True
        assert retained._birth_record is None
        assert id(retained) not in ssh_module._ORPHANED_SUBPROCESSES
        assert not lease_paths[0].exists()
        assert (
            ssh_module._run_subprocess(
                [sys.executable, "-c", "raise SystemExit(0)"],
                1,
            ).returncode
            == 0
        )
    finally:
        monkeypatch.setattr(ssh_module, "_OWNED_SUBPROCESS_POPEN", original_popen)
        monkeypatch.setattr(
            ssh_module,
            "_observe_owned_process_group_states",
            original_observe_group,
        )
        if retained is not None and id(retained) in ssh_module._ORPHANED_SUBPROCESSES:
            retained.cleanup()
        for process in started:
            if process.returncode is not None:
                continue
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            try:
                process.wait(timeout=3)
            except ChildProcessError:
                pass


def test_recovered_poll_cannot_reap_short_leader_before_retryable_group_cleanup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Cancelled(BaseException):
        pass

    transport = _transport(tmp_path)
    original_popen = ssh_module._OWNED_SUBPROCESS_POPEN
    original_cleanup = ssh_module._terminate_and_reap_subprocess
    original_confirm_disappeared = ssh_module._confirm_owned_process_group_disappeared
    original_signal_group = ssh_module._signal_owned_process_group
    orphan_ids = set(ssh_module._ORPHANED_SUBPROCESSES)
    orphan_trust_ids = set(ssh_module._ORPHANED_TRUST_LEASES)
    lease_paths: list[Path] = []
    started: list[subprocess.Popen[bytes]] = []
    child_path = tmp_path / "recovered-child.pid"
    cleanup_blocked = True
    defer_disappearance_once = True
    signal_count = 0

    leader_program = (
        "import subprocess,sys;"
        "child=subprocess.Popen([sys.executable,'-c',"
        "'import signal,time;signal.signal(signal.SIGTERM,signal.SIG_IGN);time.sleep(30)']);"
        "open(sys.argv[1],'w',encoding='ascii').write(str(child.pid))"
    )

    def local_argv(_command: str, known_hosts_file: Path) -> list[str]:
        lease_paths.append(known_hosts_file)
        return [sys.executable, "-c", leader_program, str(child_path)]

    def spawn_then_cancel(*args: object, **kwargs: object) -> subprocess.Popen[bytes]:
        process = original_popen(*args, **kwargs)
        started.append(process)
        deadline = time.monotonic() + 3
        while not child_path.exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        assert child_path.exists()
        raise Cancelled

    def controlled_cleanup(*args: object, **kwargs: object) -> None:
        if cleanup_blocked:
            raise OSError("initial cleanup deferred")
        original_cleanup(*args, **kwargs)

    def confirm_disappeared_once(*, process_group_id: int) -> None:
        nonlocal defer_disappearance_once
        if defer_disappearance_once:
            defer_disappearance_once = False
            raise OSError("disappearance proof deferred")
        original_confirm_disappeared(process_group_id=process_group_id)

    def record_signal_group(*args: object, **kwargs: object) -> None:
        nonlocal signal_count
        signal_count += 1
        original_signal_group(*args, **kwargs)

    monkeypatch.setattr(transport, "_ssh_argv", local_argv)
    monkeypatch.setattr(ssh_module, "_MAX_OWNED_SUBPROCESSES", 1)
    monkeypatch.setattr(ssh_module, "_SUBPROCESS_TERMINATE_GRACE_SECONDS", 0.2)
    monkeypatch.setattr(ssh_module, "_OWNED_SUBPROCESS_POPEN", spawn_then_cancel)
    monkeypatch.setattr(ssh_module, "_terminate_and_reap_subprocess", controlled_cleanup)

    authority: ssh_module._OwnedSubprocessAuthority | None = None
    child_id: int | None = None
    try:
        with pytest.raises(Cancelled):
            transport.run_secret("private command")

        new_authorities = tuple(
            retained
            for authority_id, retained in ssh_module._ORPHANED_SUBPROCESSES.items()
            if authority_id not in orphan_ids
        )
        assert len(started) == 1
        assert len(new_authorities) == 1
        authority = new_authorities[0]
        process = authority.process
        assert isinstance(process, ssh_module._RecoveredSubprocess)
        child_id = int(child_path.read_text(encoding="ascii"))

        authority.initialize_observer()
        deadline = time.monotonic() + 3
        while not authority.leader_exited():
            if time.monotonic() >= deadline:
                pytest.fail("short-lived recovered leader did not exit")
            time.sleep(0.01)

        with pytest.raises(RuntimeError, match="non-reaping observer"):
            process.poll()
        assert process.returncode is None
        os.kill(child_id, 0)
        assert authority.released is False
        assert len(lease_paths) == 1
        assert lease_paths[0].exists()

        cleanup_blocked = False
        monkeypatch.setattr(ssh_module, "_OWNED_SUBPROCESS_POPEN", original_popen)
        monkeypatch.setattr(
            ssh_module,
            "_confirm_owned_process_group_disappeared",
            confirm_disappeared_once,
        )
        monkeypatch.setattr(ssh_module, "_signal_owned_process_group", record_signal_group)

        with pytest.raises(OSError, match="disappearance proof deferred"):
            authority.cleanup()

        assert process.returncode is not None
        assert signal_count >= 2
        signals_before_retry = signal_count
        assert authority.released is False
        assert lease_paths[0].exists()

        authority.cleanup()
        authority.cleanup()

        _assert_processes_gone(child_id)
        assert signal_count == signals_before_retry
        assert authority.released is True
        assert authority._slot_held is False
        assert set(ssh_module._ORPHANED_SUBPROCESSES) == orphan_ids
        assert set(ssh_module._ORPHANED_TRUST_LEASES) == orphan_trust_ids
        assert not lease_paths[0].exists()
    finally:
        cleanup_blocked = False
        monkeypatch.setattr(ssh_module, "_OWNED_SUBPROCESS_POPEN", original_popen)
        monkeypatch.setattr(ssh_module, "_terminate_and_reap_subprocess", original_cleanup)
        monkeypatch.setattr(
            ssh_module,
            "_confirm_owned_process_group_disappeared",
            original_confirm_disappeared,
        )
        monkeypatch.setattr(ssh_module, "_signal_owned_process_group", original_signal_group)
        if authority is not None and not authority.released:
            try:
                authority.cleanup()
            except BaseException:
                pass
        if child_id is not None:
            try:
                os.kill(child_id, signal.SIGKILL)
            except ProcessLookupError:
                pass
        for process in started:
            if process.returncode is not None:
                continue
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            try:
                process.wait(timeout=3)
            except ChildProcessError:
                pass


def test_secret_runner_retains_lease_until_group_termination_is_observed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    transport = _transport(tmp_path)
    original_observe_group = ssh_module._observe_owned_process_group_states
    orphan_ids = set(ssh_module._ORPHANED_SUBPROCESSES)
    orphan_trust_ids = set(ssh_module._ORPHANED_TRUST_LEASES)
    lease_paths: list[Path] = []

    def local_argv(_command: str, known_hosts_file: Path) -> list[str]:
        lease_paths.append(known_hosts_file)
        return [sys.executable, "-c", "import time; time.sleep(30)"]

    def cancel_capture(*_args: object, **_kwargs: object) -> tuple[bytes, bytes]:
        raise KeyboardInterrupt

    def report_uninterruptible_member(
        process: subprocess.Popen[bytes],
        *,
        process_group_id: int,
    ) -> dict[int, str]:
        assert process_group_id == process.pid
        return {process.pid: "Z", process.pid + 1: "D"}

    monkeypatch.setattr(transport, "_ssh_argv", local_argv)
    monkeypatch.setattr(ssh_module, "_capture_subprocess_output", cancel_capture)
    monkeypatch.setattr(ssh_module, "_SUBPROCESS_TERMINATE_GRACE_SECONDS", 0.01)
    monkeypatch.setattr(
        ssh_module,
        "_observe_owned_process_group_states",
        report_uninterruptible_member,
    )

    authority: ssh_module._OwnedSubprocessAuthority | None = None
    try:
        with pytest.raises(KeyboardInterrupt):
            transport.run_secret("private command")

        retained = tuple(
            authority
            for authority_id, authority in ssh_module._ORPHANED_SUBPROCESSES.items()
            if authority_id not in orphan_ids
        )
        assert len(retained) == 1
        authority = retained[0]
        assert authority.process.returncode is None
        assert len(lease_paths) == 1
        assert lease_paths[0].exists()
        assert set(ssh_module._ORPHANED_TRUST_LEASES) == orphan_trust_ids

        monkeypatch.setattr(
            ssh_module,
            "_observe_owned_process_group_states",
            original_observe_group,
        )
        ssh_module._retry_orphaned_subprocess_cleanup()

        assert authority.process.returncode is not None
        assert id(authority) not in ssh_module._ORPHANED_SUBPROCESSES
        assert not lease_paths[0].exists()
    finally:
        monkeypatch.setattr(
            ssh_module,
            "_observe_owned_process_group_states",
            original_observe_group,
        )
        if authority is not None and id(authority) in ssh_module._ORPHANED_SUBPROCESSES:
            authority.cleanup()


def test_group_signal_failure_retains_slot_registry_and_trust_lease(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Lease:
        exits = 0

        def __exit__(self, *_args: object) -> None:
            self.exits += 1

    lease = Lease()
    authority = ssh_module._OwnedSubprocessAuthority.spawn(
        [sys.executable, "-c", "import time; time.sleep(30)"],
        trust_lease=lease,
    )
    authority.initialize_observer()
    original_signal_group = ssh_module._signal_owned_process_group
    monkeypatch.setattr(ssh_module, "_SUBPROCESS_TERMINATE_GRACE_SECONDS", 0.01)
    monkeypatch.setattr(
        ssh_module,
        "_signal_owned_process_group",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("signal failed")),
    )

    try:
        with pytest.raises(OSError, match="signal failed"):
            authority.cleanup()

        assert authority.process.returncode is None
        assert id(authority) in ssh_module._ORPHANED_SUBPROCESSES
        assert lease.exits == 0

        monkeypatch.setattr(
            ssh_module,
            "_signal_owned_process_group",
            original_signal_group,
        )
        authority.cleanup()
        assert authority.process.returncode is not None
        assert id(authority) not in ssh_module._ORPHANED_SUBPROCESSES
        assert lease.exits == 1
    finally:
        monkeypatch.setattr(
            ssh_module,
            "_signal_owned_process_group",
            original_signal_group,
        )
        if authority.process.returncode is None:
            authority.cleanup()


def test_authority_owned_lease_cleanup_failure_cannot_retry_independently(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Lease:
        cleanup_fails = True
        exits = 0

        def __exit__(self, *_args: object) -> None:
            self.exits += 1
            if self.cleanup_fails:
                raise OSError("lease cleanup failed")

    lease = Lease()
    authority = ssh_module._OwnedSubprocessAuthority.spawn(
        [sys.executable, "-c", "import time; time.sleep(30)"],
        trust_lease=lease,
    )
    authority.initialize_observer()
    monkeypatch.setattr(ssh_module, "_SUBPROCESS_TERMINATE_GRACE_SECONDS", 0.05)

    try:
        authority.cleanup()

        assert authority.process.returncode is not None
        assert id(authority) in ssh_module._ORPHANED_SUBPROCESSES
        assert id(lease) not in ssh_module._ORPHANED_TRUST_LEASES
        assert lease.exits == 1

        lease.cleanup_fails = False
        authority.cleanup()

        assert id(authority) not in ssh_module._ORPHANED_SUBPROCESSES
        assert id(lease) not in ssh_module._ORPHANED_TRUST_LEASES
        assert lease.exits == 2
    finally:
        lease.cleanup_fails = False
        if id(authority) in ssh_module._ORPHANED_SUBPROCESSES:
            authority.cleanup()


def test_default_runner_bounds_multiple_cleanup_orphans_and_reaps_on_retry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Cancelled(BaseException):
        pass

    original_cleanup = ssh_module._terminate_and_reap_subprocess
    original_popen = ssh_module._OWNED_SUBPROCESS_POPEN
    cleanup_fails = True
    started: list[subprocess.Popen[bytes]] = []

    def recording_popen(*args: object, **kwargs: object) -> subprocess.Popen[bytes]:
        process = original_popen(*args, **kwargs)
        started.append(process)
        return process

    def cancel_capture(*_args: object, **_kwargs: object) -> tuple[bytes, bytes]:
        raise Cancelled

    def controlled_cleanup(*args: object, **kwargs: object) -> None:
        if cleanup_fails:
            raise OSError("cleanup failed")
        original_cleanup(*args, **kwargs)

    monkeypatch.setattr(ssh_module, "_MAX_OWNED_SUBPROCESSES", 3)
    monkeypatch.setattr(ssh_module, "_OWNED_SUBPROCESS_POPEN", recording_popen)
    monkeypatch.setattr(ssh_module, "_capture_subprocess_output", cancel_capture)
    monkeypatch.setattr(
        ssh_module,
        "_terminate_and_reap_subprocess",
        controlled_cleanup,
    )

    try:
        for _index in range(3):
            with pytest.raises(Cancelled):
                ssh_module._run_subprocess(
                    [sys.executable, "-c", "import time; time.sleep(30)"],
                    1,
                )

        assert len(ssh_module._ORPHANED_SUBPROCESSES) == 3
        with pytest.raises(RuntimeError, match="ownership capacity"):
            ssh_module._run_subprocess(
                [sys.executable, "-c", "import time; time.sleep(30)"],
                1,
            )
        assert len(started) == 3

        cleanup_fails = False
        ssh_module._retry_orphaned_subprocess_cleanup()
        assert ssh_module._ORPHANED_SUBPROCESSES == {}
        assert all(process.poll() is not None for process in started)
    finally:
        cleanup_fails = False
        ssh_module._retry_orphaned_subprocess_cleanup()


def _assert_processes_gone(*process_ids: int) -> None:
    deadline = time.monotonic() + 3
    remaining = set(process_ids)
    while remaining and time.monotonic() < deadline:
        for process_id in tuple(remaining):
            try:
                os.kill(process_id, 0)
            except ProcessLookupError:
                remaining.remove(process_id)
        if remaining:
            time.sleep(0.01)
    assert remaining == set()


def test_tunnel_context_manager_and_exit_monitor_release_trust_lease(
    tmp_path: Path,
) -> None:
    profile = _profile()
    binding = _trusted_binding(tmp_path, profile)
    starter = RecordingTunnelStarter()
    transport = SshRemoteExecutorTransport(
        profile,
        trusted_host=binding,
        runner=RecordingRunner(),
        tunnel_starter=starter,
        port_allocator=lambda: 49155,
    )

    with transport.open_tunnel(remote_port=8765, wait_for_ready=False) as tunnel:
        lease_option = next(
            value for value in starter.calls[0] if value.startswith("UserKnownHostsFile=")
        )
        lease_path = Path(lease_option.removeprefix("UserKnownHostsFile="))
        assert lease_path.exists()

    assert tunnel.closed is True
    assert not lease_path.exists()

    monitored = transport.open_tunnel(remote_port=8765, wait_for_ready=False)
    monitored_process = starter.processes[1]
    monitored_process.exit()
    deadline = time.monotonic() + 1.0
    while not monitored.closed and time.monotonic() < deadline:
        time.sleep(0.01)
    assert monitored.closed is True


def test_trust_mutation_requests_matching_tunnel_close_before_replace(
    tmp_path: Path,
) -> None:
    profile = _profile()
    binding = _trusted_binding(tmp_path, profile)
    starter = RecordingTunnelStarter()
    transport = SshRemoteExecutorTransport(
        profile,
        trusted_host=binding,
        runner=RecordingRunner(),
        tunnel_starter=starter,
        port_allocator=lambda: 49155,
    )
    tunnel = transport.open_tunnel(remote_port=8765, wait_for_ready=False)
    store = ProviderKnownHostStore(
        binding.known_hosts_file.parent,
        runner=lambda argv, timeout: subprocess.CompletedProcess(argv, 1),
        lock_timeout_seconds=0.5,
    )

    store.revoke(profile, expected_fingerprint=binding.fingerprint)

    assert starter.processes[0].terminated is True
    assert tunnel.closed is True
    assert not binding.known_hosts_file.exists()


def test_upload_dir_rejects_missing_local_path(tmp_path: Path) -> None:
    transport = _transport(tmp_path, runner=RecordingRunner())

    with pytest.raises(SshTransportError) as exc_info:
        transport.upload_dir(str(tmp_path / "missing"), "/remote/path")

    assert exc_info.value.code is SshTransportErrorCode.INVALID_REQUEST


def test_upload_dir_rejects_non_directory_local_path(tmp_path: Path) -> None:
    local = tmp_path / "workspace.txt"
    local.write_text("not a directory", encoding="utf-8")
    transport = _transport(tmp_path, runner=RecordingRunner())

    with pytest.raises(SshTransportError) as exc_info:
        transport.upload_dir(str(local), "/remote/path")

    assert exc_info.value.code is SshTransportErrorCode.INVALID_REQUEST


def test_upload_dir_rejects_relative_remote_path(tmp_path: Path) -> None:
    local = tmp_path / "workspace"
    local.mkdir()
    transport = _transport(tmp_path, runner=RecordingRunner())

    with pytest.raises(SshTransportError) as exc_info:
        transport.upload_dir(str(local), "relative/path")

    assert exc_info.value.code is SshTransportErrorCode.INVALID_REQUEST


def test_upload_dir_rejects_remote_path_with_control_character(tmp_path: Path) -> None:
    local = tmp_path / "workspace"
    local.mkdir()
    transport = _transport(tmp_path, runner=RecordingRunner())

    with pytest.raises(SshTransportError) as exc_info:
        transport.upload_dir(str(local), "/remote/path\nid")

    assert exc_info.value.code is SshTransportErrorCode.INVALID_REQUEST


def test_upload_dir_rejects_remote_path_with_shell_metacharacter(tmp_path: Path) -> None:
    local = tmp_path / "workspace"
    local.mkdir()
    transport = _transport(tmp_path, runner=RecordingRunner())

    with pytest.raises(SshTransportError) as exc_info:
        transport.upload_dir(str(local), "/remote/path;touch-pwned")

    assert exc_info.value.code is SshTransportErrorCode.INVALID_REQUEST


def test_upload_dir_raises_when_remote_mkdir_fails(tmp_path: Path) -> None:
    local = tmp_path / "workspace"
    local.mkdir()
    runner = RecordingRunner(fail=True)
    transport = _transport(tmp_path, runner=runner)

    with pytest.raises(SshTransportError) as exc_info:
        transport.upload_dir(str(local), "/remote/path")

    assert exc_info.value.code is SshTransportErrorCode.RSYNC_FAILED


def test_upload_dir_raises_when_rsync_fails(tmp_path: Path) -> None:
    local = tmp_path / "workspace"
    local.mkdir()
    runner = FailSecondCallRunner()
    transport = _transport(tmp_path, runner=runner)

    with pytest.raises(SshTransportError) as exc_info:
        transport.upload_dir(str(local), "/remote/path")

    assert exc_info.value.code is SshTransportErrorCode.RSYNC_FAILED
