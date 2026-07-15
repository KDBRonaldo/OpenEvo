from __future__ import annotations

import io
from pathlib import Path
import socket
import time

from desktop.sidecar.ssh_credentials import (
    PasswordAskpassCredentialAdapter,
    SshAgentCredentialAdapter,
    _read_askpass_secret,
)


class _RecordingPipe(io.BytesIO):
    def __init__(self) -> None:
        super().__init__()
        self.recorded = b""

    def close(self) -> None:
        self.recorded = self.getvalue()
        super().close()


class _FakeProcess:
    def __init__(self, *, agent_socket: socket.socket | None = None) -> None:
        self.stdin = None if agent_socket is not None else _RecordingPipe()
        self._agent_socket = agent_socket
        self._return_code: int | None = None if agent_socket is not None else 0

    def poll(self) -> int | None:
        return self._return_code

    def wait(self, timeout: float | None = None) -> int:
        return self._return_code or 0

    def terminate(self) -> None:
        self._return_code = 0
        if self._agent_socket is not None:
            self._agent_socket.close()

    kill = terminate


class _RecordingPopen:
    def __init__(self) -> None:
        self.calls: list[tuple[list[str], dict[str, object], _FakeProcess]] = []

    def __call__(self, argv: list[str], **options: object) -> _FakeProcess:
        if argv[0] == "ssh-agent":
            listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            listener.bind(argv[argv.index("-a") + 1])
            process = _FakeProcess(agent_socket=listener)
        else:
            process = _FakeProcess()
        self.calls.append((argv, options, process))
        return process


def test_password_askpass_uses_one_shot_ipc_without_secret_argv_or_environment() -> None:
    secret = bytearray(b"credential-canary")
    adapter = PasswordAskpassCredentialAdapter(secret, helper_executable=Path("/bin/false"))

    environment = adapter.prepare_process_environment()
    recovered = _read_askpass_secret(environment)

    assert recovered == bytearray(b"credential-canary")
    assert all("credential-canary" not in value for value in environment.values())
    assert all("credential-canary" not in value for value in adapter.ssh_options())
    deadline = time.monotonic() + 1.0
    while time.monotonic() < deadline:
        if all(not item._thread.is_alive() for item in adapter._active):
            break
        time.sleep(0.01)
    adapter.close()
    assert secret == bytearray(b"\x00" * len(secret))


def test_password_adapter_forces_one_prompt_and_disables_public_keys() -> None:
    adapter = PasswordAskpassCredentialAdapter(
        bytearray(b"password"), helper_executable=Path("/bin/false")
    )

    options = adapter.ssh_options()

    assert "NumberOfPasswordPrompts=1" in options
    assert "PubkeyAuthentication=no" in options
    assert "IdentityAgent=none" in options
    adapter.close()


def test_password_adapter_close_interrupts_pending_prompt_and_zeroizes_copy() -> None:
    adapter = PasswordAskpassCredentialAdapter(
        bytearray(b"password"), helper_executable=Path("/bin/false")
    )
    adapter.prepare_process_environment()
    prompt = adapter._active[0]

    adapter.close()

    assert not prompt._thread.is_alive()
    assert prompt._secret == bytearray(b"\x00" * len(b"password"))


def test_private_key_adapter_uses_agent_and_ssh_add_stdin_without_secret_process_data() -> None:
    private_key = bytearray(b"private-key-canary")
    passphrase = bytearray(b"passphrase-canary")
    popen = _RecordingPopen()

    adapter = SshAgentCredentialAdapter(
        private_key,
        passphrase,
        helper_executable=Path("/packaged/openevo-sidecar"),
        popen=popen,
    )

    agent_call, add_call = popen.calls
    assert agent_call[0][0:2] == ["ssh-agent", "-D"]
    assert add_call[0] == ["ssh-add", "-"]
    assert add_call[2].stdin is not None
    assert add_call[2].stdin.recorded == b"private-key-canary"
    serialized_process_data = repr((agent_call[:2], add_call[:2]))
    assert "private-key-canary" not in serialized_process_data
    assert "passphrase-canary" not in serialized_process_data
    assert private_key == bytearray(b"\x00" * len(private_key))
    assert passphrase == bytearray(b"\x00" * len(passphrase))
    assert any(option.startswith("IdentityAgent=") for option in adapter.ssh_options())
    adapter.close()
