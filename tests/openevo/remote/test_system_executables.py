from __future__ import annotations

import os
from pathlib import Path
import socket
import stat
import subprocess
import tempfile

import pytest

import openevo.deployment.host_keys as host_keys
import openevo.deployment.system_executables as executables
from openevo.deployment.ssh import _run_subprocess


@pytest.mark.parametrize(
    "path",
    [
        executables.SSH_EXECUTABLE,
        executables.SSH_KEYSCAN_EXECUTABLE,
        executables.RSYNC_EXECUTABLE,
    ],
)
def test_fixed_system_executable_holds_verified_root_owned_identity(path: str) -> None:
    with executables.VerifiedSystemExecutable.open(path) as authority:
        authority.verify_path_binding()
        metadata = os.fstat(authority.descriptor)

        assert stat.S_ISREG(metadata.st_mode)
        assert metadata.st_uid == 0
        assert stat.S_IMODE(metadata.st_mode) & 0o022 == 0
        assert authority.display_path == path
        assert authority.execution_path == f"/dev/fd/{authority.descriptor}"


@pytest.mark.parametrize(
    "path",
    ["ssh", "./ssh", "/tmp/ssh", "/usr/local/bin/ssh", "/usr/bin/../bin/ssh"],
)
def test_system_executable_rejects_path_lookup_and_non_allowlisted_paths(path: str) -> None:
    with pytest.raises(ValueError, match="allowlisted"):
        executables.VerifiedSystemExecutable.open(path)


def test_system_executable_rejects_path_replacement_during_open(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    real_stat = executables.os.stat
    observed = 0

    def replaced_stat(path: str, *args: object, **kwargs: object) -> os.stat_result:
        nonlocal observed
        metadata = real_stat(path, *args, **kwargs)
        if path != Path(executables.SSH_EXECUTABLE).name:
            return metadata
        observed += 1
        if observed != 2:
            return metadata
        fields = list(metadata)
        fields[1] += 1
        return os.stat_result(fields)

    monkeypatch.setattr(executables.os, "stat", replaced_stat)

    with pytest.raises(ValueError, match="binding changed"):
        executables.VerifiedSystemExecutable.open(executables.SSH_EXECUTABLE)


def test_malicious_path_ssh_is_never_executed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    canary = tmp_path / "path-ssh-ran"
    fake_ssh = tmp_path / "ssh"
    fake_ssh.write_text(f"#!/bin/sh\ntouch {canary}\n", encoding="ascii")
    fake_ssh.chmod(0o755)
    monkeypatch.setenv("PATH", str(tmp_path))

    completed = _run_subprocess([executables.SSH_EXECUTABLE, "-V"], 5, env={})

    assert completed.returncode == 0
    assert not canary.exists()


def test_keyscan_executes_only_the_held_fixed_binary() -> None:
    completed = host_keys._run_keyscan(
        [executables.SSH_KEYSCAN_EXECUTABLE, "-h"],
        5,
    )

    assert completed.args == [executables.SSH_KEYSCAN_EXECUTABLE, "-h"]


def test_closed_ssh_environment_drops_all_ordinary_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("SSH_AUTH_SOCK", raising=False)
    monkeypatch.setenv("PATH", "/attacker/bin")
    monkeypatch.setenv("SSH_ASKPASS", "/attacker/askpass")
    monkeypatch.setenv("DISPLAY", "attacker:0")
    monkeypatch.setenv("LD_PRELOAD", "/attacker/library.so")
    monkeypatch.setenv("HTTP_PROXY", "http://attacker.invalid")

    assert executables.closed_ssh_environment("ssh_agent") == {}
    assert executables.closed_ssh_environment("private_key") == {}


def test_closed_ssh_environment_accepts_only_an_owner_private_unix_socket(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    socket_path = tmp_path / "agent.sock"
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        listener.bind(str(socket_path))
        socket_path.chmod(0o600)
        monkeypatch.setenv("SSH_AUTH_SOCK", str(socket_path))

        assert executables.closed_ssh_environment("ssh_agent") == {
            "SSH_AUTH_SOCK": str(socket_path)
        }

        socket_path.chmod(0o620)
        with pytest.raises(ValueError, match="identity"):
            executables.closed_ssh_environment("ssh_agent")
    finally:
        listener.close()


def test_closed_ssh_environment_rejects_symlink_and_regular_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    regular = tmp_path / "not-an-agent"
    regular.write_text("canary", encoding="ascii")
    regular.chmod(0o600)
    link = tmp_path / "agent.sock"
    link.symlink_to(regular)

    for candidate in (regular, link):
        monkeypatch.setenv("SSH_AUTH_SOCK", str(candidate))
        with pytest.raises(ValueError, match="identity"):
            executables.closed_ssh_environment("ssh_agent")


def test_agent_socket_authority_rejects_ancestor_path_replacement(tmp_path: Path) -> None:
    agent_root = tmp_path / "agent"
    agent_root.mkdir(mode=0o700)
    socket_path = agent_root / "agent.sock"
    original = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    replacement = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    original.bind(str(socket_path))
    socket_path.chmod(0o600)
    authority = executables.VerifiedSshAgentSocket.open(str(socket_path))
    moved = tmp_path / "moved-agent"
    try:
        agent_root.rename(moved)
        agent_root.mkdir(mode=0o700)
        replacement.bind(str(socket_path))
        socket_path.chmod(0o600)

        with pytest.raises(ValueError, match="ancestor path binding"):
            authority.verify_path_binding()
    finally:
        authority.close()
        replacement.close()
        original.close()


def test_macos_agent_socket_aliases_are_normalized_without_realpath(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(executables.sys, "platform", "darwin")

    assert executables._canonical_agent_socket_path("/tmp/agent.sock") == (
        "/private/tmp/agent.sock"
    )
    assert executables._canonical_agent_socket_path("/var/folders/x/agent.sock") == (
        "/private/var/folders/x/agent.sock"
    )
    assert executables._canonical_agent_socket_path("/private/tmp/agent.sock") == (
        "/private/tmp/agent.sock"
    )


def test_packaged_sidecar_dispatches_owned_subprocess_birth_to_held_executable() -> None:
    packaged = os.environ.get("OPENEVO_PACKAGED_SIDECAR_PATH")
    if packaged is None:
        pytest.skip("requires a freshly generated packaged sidecar")
    packaged_path = Path(packaged)
    if not packaged_path.is_file():
        pytest.fail("packaged sidecar path is missing")

    with (
        tempfile.TemporaryFile(prefix="openevo-packaged-birth-") as birth_record,
        executables.VerifiedSystemExecutable.open(executables.SSH_EXECUTABLE) as executable,
    ):
        completed = subprocess.run(
            [
                str(packaged_path),
                "-I",
                "-c",
                "ignored-by-packaged-dispatch",
                executables.OWNED_SUBPROCESS_BIRTH_ARGUMENT,
                str(birth_record.fileno()),
                str(executable.descriptor),
                executables.SSH_EXECUTABLE,
                "-V",
            ],
            check=False,
            capture_output=True,
            env={},
            pass_fds=(birth_record.fileno(), executable.descriptor),
            start_new_session=True,
        )
        executable.verify_path_binding()
        record = os.pread(birth_record.fileno(), 129, 0)

    assert completed.returncode == 0
    fields = record.rstrip(b"\n").split(b" ")
    assert len(fields) == 3
    assert all(field.isdigit() for field in fields)
    assert fields[0] == fields[1] == fields[2]


def test_packaged_birth_dispatch_rejects_non_file_birth_authority() -> None:
    reader, writer = os.pipe()
    try:
        with executables.VerifiedSystemExecutable.open(executables.SSH_EXECUTABLE) as executable:
            with pytest.raises(ValueError, match="birth authority"):
                executables.run_packaged_owned_subprocess_birth(
                    [
                        executables.OWNED_SUBPROCESS_BIRTH_ARGUMENT,
                        str(writer),
                        str(executable.descriptor),
                        executables.SSH_EXECUTABLE,
                        "-V",
                    ]
                )
    finally:
        os.close(writer)
        os.close(reader)
