from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from openevo.deployment import RemoteCommandResult, RemoteExecutorTransport
from openevo.deployment.ssh import SshRemoteExecutorTransport
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


class FakeTunnelProcess:
    def __init__(self) -> None:
        self.terminated = False
        self.killed = False
        self.waited = False

    def poll(self) -> int | None:
        return None

    def terminate(self) -> None:
        self.terminated = True

    def kill(self) -> None:
        self.killed = True

    def wait(self, timeout: float | None = None) -> int:
        self.waited = True
        return 0


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


def test_ssh_transport_satisfies_executor_protocol() -> None:
    assert isinstance(SshRemoteExecutorTransport(_profile()), RemoteExecutorTransport)


def test_run_invokes_ssh_with_batch_mode_and_maps_result() -> None:
    runner = RecordingRunner()
    transport = SshRemoteExecutorTransport(_profile(), runner=runner)

    result = transport.run("true", timeout_seconds=12.5)

    assert result == RemoteCommandResult(
        command="true",
        return_code=0,
        stdout="out",
        stderr="err",
    )
    assert runner.calls == [
        (
            [
                "ssh",
                "-p",
                "2222",
                "-o",
                "BatchMode=yes",
                "-l",
                "alice",
                "--",
                "gpu.example.edu",
                "true",
            ],
            12.5,
        )
    ]


def test_run_maps_nonzero_exit_without_throwing() -> None:
    runner = RecordingRunner(fail=True)
    transport = SshRemoteExecutorTransport(_profile(), runner=runner)

    result = transport.run("false")

    assert result.return_code == 7
    assert result.stdout == "out"
    assert result.stderr == "err"


def test_private_key_auth_adds_identity_file_as_argv(tmp_path: Path) -> None:
    key = tmp_path / "id_ed25519"
    key.write_text("key", encoding="utf-8")
    runner = RecordingRunner()
    transport = SshRemoteExecutorTransport(
        _profile(
            auth={
                "method": "private_key",
                "private_key_path": str(key),
            }
        ),
        runner=runner,
    )

    transport.run("true")

    argv = runner.calls[0][0]
    assert argv[0:6] == ["ssh", "-p", "2222", "-i", str(key), "-o"]
    assert "BatchMode=yes" in argv
    assert argv[-5:] == ["-l", "alice", "--", "gpu.example.edu", "true"]


def test_run_quotes_remote_env_values_and_cwd() -> None:
    runner = RecordingRunner()
    transport = SshRemoteExecutorTransport(_profile(), runner=runner)

    transport.run(
        "python script.py",
        cwd="/home/alice/project-dir",
        env={
            "HTTPS_PROXY": "http://127.0.0.1:7890",
            "PIP_INDEX_URL": "https://mirror.example/simple path",
        },
    )

    remote_command = runner.calls[0][0][-1]
    assert remote_command == (
        "cd /home/alice/project-dir && "
        "export HTTPS_PROXY=http://127.0.0.1:7890 "
        "PIP_INDEX_URL='https://mirror.example/simple path' "
        "&& python script.py"
    )


def test_run_exports_env_before_multiline_remote_script() -> None:
    runner = RecordingRunner()
    transport = SshRemoteExecutorTransport(_profile(), runner=runner)

    transport.run(
        "python3 - <<'PY'\n"
        "import os\n"
        "print(os.environ['HTTPS_PROXY'])\n"
        "PY\n"
        "nohup env PATH=\"$HOME/.local/bin:$PATH\" python3 server.py &",
        env={"HTTPS_PROXY": "http://proxy-user:proxy-secret@127.0.0.1:7890"},
    )

    remote_command = runner.calls[0][0][-1]
    assert remote_command.startswith(
        "export HTTPS_PROXY="
        "http://proxy-user:proxy-secret@127.0.0.1:7890 && python3 - <<'PY'"
    )
    assert "\nnohup env PATH=" in remote_command
    assert "env HTTPS_PROXY=" not in remote_command


def test_run_rejects_invalid_env_key() -> None:
    transport = SshRemoteExecutorTransport(_profile(), runner=RecordingRunner())

    with pytest.raises(ValueError, match="invalid remote environment key"):
        transport.run("true", env={"BAD KEY": "value"})


def test_run_rejects_relative_cwd() -> None:
    transport = SshRemoteExecutorTransport(_profile(), runner=RecordingRunner())

    with pytest.raises(ValueError, match="cwd must be an absolute remote path"):
        transport.run("true", cwd="relative")


def test_run_rejects_cwd_with_control_character() -> None:
    transport = SshRemoteExecutorTransport(_profile(), runner=RecordingRunner())

    with pytest.raises(ValueError, match="cwd must not contain control characters"):
        transport.run("true", cwd="/tmp/project\nid")


def test_run_rejects_cwd_with_shell_metacharacter() -> None:
    transport = SshRemoteExecutorTransport(_profile(), runner=RecordingRunner())

    with pytest.raises(ValueError, match="cwd contains unsupported characters"):
        transport.run("true", cwd="/tmp/project;touch-pwned")


def test_password_ref_auth_is_not_supported_without_vault() -> None:
    with pytest.raises(ValueError, match="password_ref") as exc_info:
        SshRemoteExecutorTransport(
            _profile(auth={"method": "password_ref", "password_ref": "secret-id"})
        )

    assert "secret-id" not in str(exc_info.value)


def test_passphrase_ref_is_not_supported_without_vault(tmp_path: Path) -> None:
    key = tmp_path / "id_ed25519"
    key.write_text("key", encoding="utf-8")

    with pytest.raises(ValueError, match="passphrase_ref") as exc_info:
        SshRemoteExecutorTransport(
            _profile(
                auth={
                    "method": "private_key",
                    "private_key_path": str(key),
                    "passphrase_ref": "passphrase-secret",
                }
            )
        )

    assert "passphrase-secret" not in str(exc_info.value)


def test_rejects_unsafe_remote_identity() -> None:
    with pytest.raises(ValueError, match="host"):
        SshRemoteExecutorTransport(_profile(host="-oProxyCommand=bad"))


def test_rejects_host_with_at_sign_to_prevent_target_rewrite() -> None:
    with pytest.raises(ValueError, match="host"):
        SshRemoteExecutorTransport(_profile(host="trusted.example@attacker.example"))


def test_rejects_user_with_at_sign_to_prevent_target_rewrite() -> None:
    with pytest.raises(ValueError, match="user"):
        SshRemoteExecutorTransport(_profile(user="alice@trusted.example"))


def test_rejects_colon_host_until_ipv6_rsync_destinations_are_supported() -> None:
    with pytest.raises(ValueError, match="host"):
        SshRemoteExecutorTransport(_profile(host="2001:db8::1"))


def test_upload_dir_creates_remote_parent_and_runs_rsync(tmp_path: Path) -> None:
    local = tmp_path / "workspace"
    local.mkdir()
    runner = RecordingRunner()
    transport = SshRemoteExecutorTransport(_profile(), runner=runner)

    transport.upload_dir(str(local), "/home/alice/.openevo/workspaces/task")

    assert runner.calls[0][0][-1] == "mkdir -p /home/alice/.openevo/workspaces/task"
    assert runner.calls[1][0][0:3] == ["rsync", "-az", "--delete"]
    assert runner.calls[1][0][-2] == f"{local}/"
    assert runner.calls[1][0][-1] == "gpu.example.edu:/home/alice/.openevo/workspaces/task/"
    assert "-l alice" in runner.calls[1][0][4]


def test_open_tunnel_starts_ssh_local_forwarding_and_closes_process() -> None:
    starter = RecordingTunnelStarter()
    transport = SshRemoteExecutorTransport(
        _profile(),
        runner=RecordingRunner(),
        tunnel_starter=starter,
        port_allocator=lambda: 49155,
    )

    tunnel = transport.open_tunnel(remote_port=8765, wait_for_ready=False)

    assert tunnel.local_port == 49155
    assert tunnel.remote_port == 8765
    assert tunnel.base_url == "http://127.0.0.1:49155"
    assert starter.calls == [
        [
            "ssh",
            "-p",
            "2222",
            "-o",
            "BatchMode=yes",
            "-l",
            "alice",
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


def test_upload_dir_rejects_missing_local_path(tmp_path: Path) -> None:
    transport = SshRemoteExecutorTransport(_profile(), runner=RecordingRunner())

    with pytest.raises(FileNotFoundError):
        transport.upload_dir(str(tmp_path / "missing"), "/remote/path")


def test_upload_dir_rejects_non_directory_local_path(tmp_path: Path) -> None:
    local = tmp_path / "workspace.txt"
    local.write_text("not a directory", encoding="utf-8")
    transport = SshRemoteExecutorTransport(_profile(), runner=RecordingRunner())

    with pytest.raises(ValueError, match="not a directory"):
        transport.upload_dir(str(local), "/remote/path")


def test_upload_dir_rejects_relative_remote_path(tmp_path: Path) -> None:
    local = tmp_path / "workspace"
    local.mkdir()
    transport = SshRemoteExecutorTransport(_profile(), runner=RecordingRunner())

    with pytest.raises(ValueError, match="remote_path must be an absolute remote path"):
        transport.upload_dir(str(local), "relative/path")


def test_upload_dir_rejects_remote_path_with_control_character(tmp_path: Path) -> None:
    local = tmp_path / "workspace"
    local.mkdir()
    transport = SshRemoteExecutorTransport(_profile(), runner=RecordingRunner())

    with pytest.raises(ValueError, match="remote_path must not contain control characters"):
        transport.upload_dir(str(local), "/remote/path\nid")


def test_upload_dir_rejects_remote_path_with_shell_metacharacter(tmp_path: Path) -> None:
    local = tmp_path / "workspace"
    local.mkdir()
    transport = SshRemoteExecutorTransport(_profile(), runner=RecordingRunner())

    with pytest.raises(ValueError, match="remote_path contains unsupported characters"):
        transport.upload_dir(str(local), "/remote/path;touch-pwned")


def test_upload_dir_raises_when_remote_mkdir_fails(tmp_path: Path) -> None:
    local = tmp_path / "workspace"
    local.mkdir()
    runner = RecordingRunner(fail=True)
    transport = SshRemoteExecutorTransport(_profile(), runner=runner)

    with pytest.raises(RuntimeError, match="remote mkdir failed"):
        transport.upload_dir(str(local), "/remote/path")


def test_upload_dir_raises_when_rsync_fails(tmp_path: Path) -> None:
    local = tmp_path / "workspace"
    local.mkdir()
    runner = FailSecondCallRunner()
    transport = SshRemoteExecutorTransport(_profile(), runner=runner)

    with pytest.raises(RuntimeError, match="rsync failed"):
        transport.upload_dir(str(local), "/remote/path")
