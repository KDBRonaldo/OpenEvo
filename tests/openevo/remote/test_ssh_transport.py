from __future__ import annotations

import base64
import errno
import hashlib
import os
import re
import shlex
import shutil
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
    argv = ["ssh", "-F", "/dev/null", "-p", str(profile.port)]
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
    argv.extend(
        [
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
        "ssh",
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


@pytest.mark.skipif(shutil.which("ssh") is None, reason="OpenSSH is unavailable")
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
        "rsync",
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


def test_core_connection_subprocess_passes_only_peer_fd_to_exact_ssh_child(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observed: list[tuple[list[str], dict[str, object]]] = []
    process = FakeTunnelProcess()

    def popen(argv: list[str], **kwargs: object) -> FakeTunnelProcess:
        observed.append((argv, kwargs))
        return process

    monkeypatch.setattr(ssh_module.subprocess, "Popen", popen)
    argv = ["ssh", "-F", "/dev/null", "-W", "127.0.0.1:8765", "--", "host"]

    result = ssh_module._start_core_connection_subprocess(argv, 42)

    assert result is process
    assert observed == [
        (
            argv,
            {
                "stdin": 42,
                "stdout": 42,
                "stderr": subprocess.DEVNULL,
                "close_fds": True,
                "pass_fds": (42,),
                "text": False,
            },
        )
    ]


def test_core_connection_subprocess_bridges_a_real_parent_owned_af_unix_stream() -> None:
    local_stream, child_stream = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        child = ssh_module._start_core_connection_subprocess(
            [
                sys.executable,
                "-c",
                (
                    "import sys; data=sys.stdin.buffer.read(); "
                    "sys.stdout.buffer.write(data.upper()); sys.stdout.buffer.flush()"
                ),
            ],
            child_stream.fileno(),
        )
        child_stream.close()
        local_stream.sendall(b"core relay")
        local_stream.shutdown(socket.SHUT_WR)
        assert local_stream.recv(64) == b"CORE RELAY"
        assert child.wait(timeout=5) == 0
    finally:
        local_stream.close()
        child_stream.close()


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
