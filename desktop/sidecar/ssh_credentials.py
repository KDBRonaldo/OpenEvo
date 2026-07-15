from __future__ import annotations

from collections.abc import Callable
import os
from pathlib import Path
import socket
import stat
import struct
import subprocess
import sys
import tempfile
from threading import Event, Lock, Thread
import time


_ASKPASS_MODE_ENV = "OPENEVO_SSH_ASKPASS_MODE"
_ASKPASS_SOCKET_ENV = "OPENEVO_SSH_ASKPASS_SOCKET"
_ASKPASS_MODE_VALUE = "openevo-askpass-v1"
_MAX_ASKPASS_BYTES = 16 * 1024
_MAX_ACTIVE_ASKPASS = 16
_ASKPASS_TIMEOUT_SECONDS = 15.0


class SshCredentialAdapterError(RuntimeError):
    """A local SSH credential process could not be prepared safely."""


class _OneShotAskpass:
    def __init__(self, secret: bytearray, *, timeout_seconds: float) -> None:
        if len(secret) > _MAX_ASKPASS_BYTES or b"\x00" in secret:
            raise ValueError("askpass secret exceeds its closed byte contract")
        self._secret = secret
        self._timeout_seconds = timeout_seconds
        self._temporary = tempfile.TemporaryDirectory(prefix="openevo-askpass-")
        os.chmod(self._temporary.name, 0o700)
        self.socket_path = Path(self._temporary.name) / "prompt.sock"
        self._ready = Event()
        self._stopped = Event()
        self._startup_failed = False
        self._thread = Thread(target=self._serve, name="openevo-askpass", daemon=True)
        self._thread.start()
        if not self._ready.wait(timeout=1.0) or self._startup_failed:
            raise SshCredentialAdapterError("SSH askpass IPC could not start")

    def _serve(self) -> None:
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            listener.bind(os.fspath(self.socket_path))
            os.chmod(self.socket_path, 0o600)
            listener.listen(1)
            listener.settimeout(self._timeout_seconds)
            self._ready.set()
            connection, _ = listener.accept()
            with connection:
                _require_same_user_peer(connection)
                if not self._stopped.is_set():
                    connection.sendall(struct.pack(">I", len(self._secret)))
                    connection.sendall(self._secret)
        except (OSError, ValueError):
            if not self._ready.is_set():
                self._startup_failed = True
                self._ready.set()
        finally:
            self._secret[:] = b"\x00" * len(self._secret)
            listener.close()
            try:
                self.socket_path.unlink(missing_ok=True)
            except OSError:
                pass
            self._temporary.cleanup()

    def close(self) -> None:
        self._stopped.set()
        if self._thread.is_alive() and self._ready.is_set() and not self._startup_failed:
            try:
                with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
                    connection.settimeout(0.5)
                    connection.connect(os.fspath(self.socket_path))
            except OSError:
                pass
        self._thread.join(timeout=1.0)
        if self._thread.is_alive():
            raise SshCredentialAdapterError("SSH askpass IPC could not stop")


class PasswordAskpassCredentialAdapter:
    """Serve one password prompt per managed OpenSSH process over local IPC."""

    def __init__(self, password: bytearray, *, helper_executable: Path | str) -> None:
        if not password or len(password) > _MAX_ASKPASS_BYTES or b"\x00" in password:
            raise ValueError("native SSH password exceeds its closed byte contract")
        self._password = password
        self._helper_executable = os.fspath(helper_executable)
        self._lock = Lock()
        self._active: list[_OneShotAskpass] = []
        self._closed = False

    def ssh_options(self) -> list[str]:
        return [
            "-o",
            "IdentityAgent=none",
            "-o",
            "IdentityFile=none",
            "-o",
            "PubkeyAuthentication=no",
            "-o",
            "PreferredAuthentications=password,keyboard-interactive",
            "-o",
            "NumberOfPasswordPrompts=1",
        ]

    def prepare_process_environment(self) -> dict[str, str]:
        with self._lock:
            if self._closed:
                raise SshCredentialAdapterError("SSH credential adapter is closed")
            self._active = [item for item in self._active if item._thread.is_alive()]
            if len(self._active) >= _MAX_ACTIVE_ASKPASS:
                raise SshCredentialAdapterError("SSH askpass capacity is exhausted")
            prompt = _OneShotAskpass(
                bytearray(self._password),
                timeout_seconds=_ASKPASS_TIMEOUT_SECONDS,
            )
            self._active.append(prompt)
        return _askpass_environment(
            helper_executable=self._helper_executable,
            socket_path=prompt.socket_path,
        )

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            self._password[:] = b"\x00" * len(self._password)
            active = self._active
            self._active = []
        for prompt in active:
            prompt.close()


class SshAgentCredentialAdapter:
    """Load private key bytes into one lifecycle-scoped ssh-agent."""

    def __init__(
        self,
        private_key: bytearray,
        passphrase: bytearray | None,
        *,
        helper_executable: Path | str,
        popen: Callable[..., subprocess.Popen[bytes]] = subprocess.Popen,
    ) -> None:
        if not private_key or len(private_key) > 1024 * 1024 or b"\x00" in private_key:
            raise ValueError("native SSH private key exceeds its closed byte contract")
        if passphrase is not None and (
            not passphrase
            or len(passphrase) > _MAX_ASKPASS_BYTES
            or b"\x00" in passphrase
        ):
            raise ValueError("native SSH passphrase exceeds its closed byte contract")
        self._private_key = private_key
        self._passphrase = passphrase
        self._helper_executable = os.fspath(helper_executable)
        self._popen = popen
        self._lock = Lock()
        self._closed = False
        self._temporary = tempfile.TemporaryDirectory(prefix="openevo-ssh-agent-")
        os.chmod(self._temporary.name, 0o700)
        self._socket_path = Path(self._temporary.name) / "agent.sock"
        self._agent: subprocess.Popen[bytes] | None = None
        try:
            self._start_agent()
            self._add_key()
        except BaseException:
            self.close()
            raise

    def ssh_options(self) -> list[str]:
        return [
            "-o",
            f"IdentityAgent={self._socket_path}",
            "-o",
            "IdentityFile=none",
            "-o",
            "IdentitiesOnly=yes",
            "-o",
            "PasswordAuthentication=no",
            "-o",
            "KbdInteractiveAuthentication=no",
            "-o",
            "PreferredAuthentications=publickey",
        ]

    def prepare_process_environment(self) -> None:
        with self._lock:
            if self._closed or self._agent is None or self._agent.poll() is not None:
                raise SshCredentialAdapterError("SSH private-key agent is unavailable")
        return None

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            agent = self._agent
            self._agent = None
            self._private_key[:] = b"\x00" * len(self._private_key)
            if self._passphrase is not None:
                self._passphrase[:] = b"\x00" * len(self._passphrase)
        if agent is not None and agent.poll() is None:
            try:
                agent.terminate()
                agent.wait(timeout=1.0)
            except (OSError, subprocess.SubprocessError):
                try:
                    agent.kill()
                    agent.wait(timeout=1.0)
                except (OSError, subprocess.SubprocessError):
                    pass
        self._temporary.cleanup()

    def _start_agent(self) -> None:
        self._agent = self._popen(
            ["ssh-agent", "-D", "-a", os.fspath(self._socket_path)],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        deadline = time.monotonic() + 3.0
        while time.monotonic() < deadline:
            if self._agent.poll() is not None:
                break
            try:
                mode = self._socket_path.stat().st_mode
            except FileNotFoundError:
                time.sleep(0.01)
                continue
            if stat.S_ISSOCK(mode):
                return
            break
        raise SshCredentialAdapterError("SSH private-key agent did not start")

    def _add_key(self) -> None:
        prompt = _OneShotAskpass(
            bytearray(self._passphrase or b""),
            timeout_seconds=_ASKPASS_TIMEOUT_SECONDS,
        )
        environment = _askpass_environment(
            helper_executable=self._helper_executable,
            socket_path=prompt.socket_path,
        )
        environment["SSH_AUTH_SOCK"] = os.fspath(self._socket_path)
        process = self._popen(
            ["ssh-add", "-"],
            stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            env=environment,
            start_new_session=True,
        )
        try:
            if process.stdin is None:
                raise SshCredentialAdapterError("SSH private-key input is unavailable")
            process.stdin.write(self._private_key)
            process.stdin.close()
            return_code = process.wait(timeout=10.0)
        except BaseException:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=1.0)
            raise
        finally:
            prompt.close()
        if return_code != 0:
            raise SshCredentialAdapterError("SSH private key could not be loaded")
        self._private_key[:] = b"\x00" * len(self._private_key)
        if self._passphrase is not None:
            self._passphrase[:] = b"\x00" * len(self._passphrase)


def native_askpass_main() -> int:
    try:
        payload = _read_askpass_secret(os.environ)
        sys.stdout.buffer.write(payload)
        sys.stdout.buffer.write(b"\n")
        sys.stdout.buffer.flush()
        payload[:] = b"\x00" * len(payload)
        return 0
    except (OSError, ValueError):
        return 1


def is_native_askpass_invocation() -> bool:
    return os.environ.get(_ASKPASS_MODE_ENV) == _ASKPASS_MODE_VALUE


def _askpass_environment(*, helper_executable: str, socket_path: Path) -> dict[str, str]:
    environment = os.environ.copy()
    environment.update(
        {
            "DISPLAY": "openevo-native",
            "SSH_ASKPASS": helper_executable,
            "SSH_ASKPASS_REQUIRE": "force",
            _ASKPASS_MODE_ENV: _ASKPASS_MODE_VALUE,
            _ASKPASS_SOCKET_ENV: os.fspath(socket_path),
        }
    )
    return environment


def _read_askpass_secret(environment: os._Environ[str] | dict[str, str]) -> bytearray:
    if environment.get(_ASKPASS_MODE_ENV) != _ASKPASS_MODE_VALUE:
        raise ValueError("askpass invocation mode is invalid")
    path = environment.get(_ASKPASS_SOCKET_ENV)
    if path is None or len(os.fsencode(path)) > 1024 or not os.path.isabs(path):
        raise ValueError("askpass socket binding is invalid")
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
        connection.settimeout(3.0)
        connection.connect(path)
        encoded_size = _recv_exact(connection, 4)
        size = struct.unpack(">I", encoded_size)[0]
        if size > _MAX_ASKPASS_BYTES:
            raise ValueError("askpass response exceeds its byte limit")
        return _recv_exact_mutable(connection, size)


def _recv_exact(connection: socket.socket, size: int) -> bytes:
    payload = bytearray()
    while len(payload) < size:
        chunk = connection.recv(size - len(payload))
        if not chunk:
            raise ValueError("askpass response ended early")
        payload.extend(chunk)
    return bytes(payload)


def _recv_exact_mutable(connection: socket.socket, size: int) -> bytearray:
    payload = bytearray()
    while len(payload) < size:
        chunk = connection.recv(size - len(payload))
        if not chunk:
            raise ValueError("askpass response ended early")
        payload.extend(chunk)
    return payload


def _require_same_user_peer(connection: socket.socket) -> None:
    if hasattr(connection, "getpeereid"):
        uid, _gid = connection.getpeereid()  # type: ignore[attr-defined]
    elif hasattr(socket, "SO_PEERCRED"):
        credentials = connection.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12)
        _pid, uid, _gid = struct.unpack("3i", credentials)
    else:
        raise ValueError("askpass peer credentials are unavailable")
    if uid != os.geteuid():
        raise ValueError("askpass peer identity is invalid")


__all__ = (
    "PasswordAskpassCredentialAdapter",
    "SshAgentCredentialAdapter",
    "SshCredentialAdapterError",
    "is_native_askpass_invocation",
    "native_askpass_main",
)
