from __future__ import annotations

import errno
import os
from pathlib import Path
import socket
import stat
import subprocess
import tempfile
import threading

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
        assert metadata.st_nlink == 1
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


def test_system_executable_rejects_hardlinked_metadata() -> None:
    metadata = os.stat(executables.SSH_EXECUTABLE, follow_symlinks=False)
    fields = list(metadata)
    fields[3] = 2
    hardlinked = os.stat_result(fields)

    with pytest.raises(ValueError, match="metadata"):
        executables._require_root_owned_executable(hardlinked)

    assert executables._executable_identity(hardlinked)[4] == 2


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
        listener.listen(4)
        socket_path.chmod(0o600)
        monkeypatch.setenv("SSH_AUTH_SOCK", str(socket_path))

        assert executables.closed_ssh_environment("ssh_agent") == {}
        source = executables.SshAgentSocketSource.from_environment("ssh_agent")
        assert source is not None
        assert str(socket_path) not in repr(source)

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


def test_agent_source_rejects_socket_replacement_before_connect(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    socket_path = tmp_path / "agent.sock"
    original = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    replacement = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    original.bind(str(socket_path))
    original.listen(4)
    socket_path.chmod(0o600)
    monkeypatch.setenv("SSH_AUTH_SOCK", str(socket_path))
    source = executables.SshAgentSocketSource.from_environment("ssh_agent")
    assert source is not None
    socket_path.unlink()
    replacement.bind(str(socket_path))
    replacement.listen(4)
    socket_path.chmod(0o600)
    try:
        with pytest.raises(ValueError, match="source identity changed"):
            source.open_proxy()
    finally:
        replacement.close()
        original.close()


def test_agent_source_rejects_socket_replacement_during_connect(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    socket_path = tmp_path / "agent.sock"
    original = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    replacement = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    original.bind(str(socket_path))
    original.listen(4)
    socket_path.chmod(0o600)
    monkeypatch.setenv("SSH_AUTH_SOCK", str(socket_path))
    source = executables.SshAgentSocketSource.from_environment("ssh_agent")
    assert source is not None
    real_connect = socket.socket.connect

    def connect_then_replace(stream: socket.socket, path: str) -> None:
        real_connect(stream, path)
        socket_path.unlink()
        replacement.bind(str(socket_path))
        replacement.listen(4)
        socket_path.chmod(0o600)

    monkeypatch.setattr(socket.socket, "connect", connect_then_replace)
    try:
        with pytest.raises(ValueError, match="binding changed"):
            source.open_proxy()
    finally:
        replacement.close()
        original.close()


def test_agent_source_rejects_socket_replace_and_restore_during_connect(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    socket_path = tmp_path / "agent.sock"
    held_path = tmp_path / "held-agent.sock"
    original = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    replacement = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    original.bind(str(socket_path))
    original.listen(4)
    socket_path.chmod(0o600)
    monkeypatch.setenv("SSH_AUTH_SOCK", str(socket_path))
    source = executables.SshAgentSocketSource.from_environment("ssh_agent")
    assert source is not None
    real_connect = socket.socket.connect

    def connect_through_replacement_then_restore(
        stream: socket.socket,
        path: str,
    ) -> None:
        socket_path.rename(held_path)
        replacement.bind(str(socket_path))
        replacement.listen(4)
        socket_path.chmod(0o600)
        real_connect(stream, path)
        socket_path.unlink()
        held_path.rename(socket_path)

    monkeypatch.setattr(socket.socket, "connect", connect_through_replacement_then_restore)
    try:
        with pytest.raises(
            ValueError,
            match="(path binding changed|directory changed during connect)",
        ):
            source.open_proxy()
    finally:
        replacement.close()
        original.close()


def test_agent_proxy_rejects_same_uid_steal_then_relays_for_owned_child(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    socket_path = tmp_path / "agent.sock"
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    listener.bind(str(socket_path))
    listener.listen(4)
    socket_path.chmod(0o600)
    monkeypatch.setenv("SSH_AUTH_SOCK", str(socket_path))
    observed: list[bytes] = []

    def serve_upstream() -> None:
        while True:
            connection, _address = listener.accept()
            with connection:
                payload = connection.recv(1024)
                if not payload:
                    continue
                observed.append(payload)
                connection.sendall(payload.upper())
                return

    upstream_thread = threading.Thread(target=serve_upstream, daemon=True)
    upstream_thread.start()
    source = executables.SshAgentSocketSource.from_environment("ssh_agent")
    assert source is not None
    proxy = source.open_proxy()
    proxy_root = Path(proxy.socket_path).parent
    attacker = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    attacker.settimeout(2)
    attacker.connect(proxy.socket_path)
    attacker.sendall(b"stolen")
    connector_python = "/usr/bin/python3"
    connector_metadata = os.stat(connector_python, follow_symlinks=False)
    assert connector_metadata.st_uid == 0
    child = subprocess.Popen(
        [
            connector_python,
            "-I",
            "-c",
            (
                "import socket,sys;"
                "stream=socket.socket(socket.AF_UNIX,socket.SOCK_STREAM);"
                "sys.stdin.buffer.read(1);"
                "stream.connect(sys.argv[1]);"
                "stream.sendall(b'owned child');"
                "sys.stdout.buffer.write(stream.recv(1024));"
                "sys.stdout.buffer.flush();"
                "stream.close()"
            ),
            proxy.socket_path,
        ],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env={},
        start_new_session=True,
    )
    try:
        proxy.bind_child(
            session_id=child.pid,
            process_group_id=child.pid,
            executable_identity=executables._peer_executable_identity(child.pid),
        )
        stdout, stderr = child.communicate(input=b"1", timeout=5)
        assert child.returncode == 0, stderr
        assert stdout == b"OWNED CHILD"
        try:
            assert attacker.recv(1) == b""
        except ConnectionResetError:
            pass
        upstream_thread.join(2)
        assert not upstream_thread.is_alive()
        assert observed == [b"owned child"]
    finally:
        attacker.close()
        if child.poll() is None:
            child.kill()
            child.wait()
        proxy.close()
        listener.close()
    assert not proxy_root.exists()


def test_agent_proxy_cleanup_keeps_recoverable_authority_on_path_replacement(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    socket_path = tmp_path / "agent.sock"
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    listener.bind(str(socket_path))
    listener.listen(4)
    socket_path.chmod(0o600)
    monkeypatch.setenv("SSH_AUTH_SOCK", str(socket_path))
    source = executables.SshAgentSocketSource.from_environment("ssh_agent")
    assert source is not None
    proxy = source.open_proxy()
    proxy_root = Path(proxy.socket_path).parent
    moved_root = proxy_root.with_name(f"{proxy_root.name}-held")
    proxy_root.rename(moved_root)
    proxy_root.mkdir(mode=0o700)
    canary = proxy_root / "canary"
    canary.write_text("replacement", encoding="ascii")
    try:
        with pytest.raises(RuntimeError, match="path binding changed"):
            proxy.close()
        assert canary.read_text(encoding="ascii") == "replacement"
        canary.unlink()
        proxy_root.rmdir()
        moved_root.rename(proxy_root)
        proxy.close()
        assert not proxy_root.exists()
    finally:
        if proxy_root.exists():
            proxy.close()
        listener.close()


def test_proxy_root_open_failure_retains_bounded_cleanup_authority(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    real_mkdir = os.mkdir
    real_open = os.open
    created_names: list[str] = []
    original_pending = set(executables._PENDING_AGENT_PROXY_CLEANUPS)
    monkeypatch.setattr(executables, "_agent_proxy_parent_path", lambda: str(tmp_path))

    def recording_mkdir(
        path: str,
        mode: int = 0o777,
        *,
        dir_fd: int | None = None,
    ) -> None:
        real_mkdir(path, mode=mode, dir_fd=dir_fd)
        created_names.append(path)

    def fail_new_root_open(
        path: str,
        flags: int,
        mode: int = 0o777,
        *,
        dir_fd: int | None = None,
    ) -> int:
        if created_names and path == created_names[-1]:
            raise OSError(errno.EMFILE, "injected descriptor exhaustion")
        return real_open(path, flags, mode, dir_fd=dir_fd)

    monkeypatch.setattr(executables.os, "mkdir", recording_mkdir)
    monkeypatch.setattr(executables.os, "open", fail_new_root_open)
    with pytest.raises(OSError, match="descriptor exhaustion"):
        executables._PrivateProxyRoot.create()

    new_pending = set(executables._PENDING_AGENT_PROXY_CLEANUPS) - original_pending
    assert len(new_pending) == 1
    assert len(created_names) == 1
    root_path = tmp_path / created_names[0]
    assert root_path.is_dir()

    monkeypatch.setattr(executables.os, "open", real_open)
    executables._retry_pending_proxy_cleanups()

    assert new_pending.isdisjoint(executables._PENDING_AGENT_PROXY_CLEANUPS)
    assert not root_path.exists()


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
    token = "0" * (executables._AGENT_PROXY_RANDOM_BYTES * 2)
    proxy_path = (
        f"{executables._agent_proxy_parent_path()}/openevo-agent-{token}/agent-{token}.sock"
    )
    assert len(os.fsencode(proxy_path)) < 104


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
