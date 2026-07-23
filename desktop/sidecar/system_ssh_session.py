"""One sidecar-owned system OpenSSH master for a v2 remote workspace."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
import hashlib
import locale
import os
from pathlib import Path
import selectors
import stat
import subprocess
import sys
import tempfile
import threading
import time
from typing import Protocol

from desktop.sidecar.askpass_broker import (
    AskpassAuthorizationBroker,
    AskpassBrokerError,
    ProcessIdentity,
    ProcessInspector,
    SystemProcessInspector,
)
from openevo.deployment.preflight import RemoteCommandResult
from openevo.deployment.profile import SystemOpenSshAliasProfile
from openevo.deployment.ssh import (
    SystemOpenSshAskpassEnvironment,
    build_system_openssh_command_argv,
    build_system_openssh_control_argv,
    build_system_openssh_core_tunnel_argv,
    build_system_openssh_environment,
    build_system_openssh_master_argv,
    build_system_openssh_upload_argv,
)
from openevo.deployment.system_executables import (
    SSH_EXECUTABLE,
    SYSTEM_OPENSSH_OWNER_ARGUMENT,
    VerifiedSystemExecutable,
)


_MAX_SAFE_GENERATION = (1 << 53) - 1
_MAX_HELPER_BYTES = 16 * 1024 * 1024
_MAX_CAPTURE_BYTES = 4 * 1024 * 1024
_CAPTURE_CHUNK_BYTES = 64 * 1024
_STARTUP_POLL_SECONDS = 0.02
_DEFAULT_STARTUP_TIMEOUT_SECONDS = 20.0
_MAX_STARTUP_TIMEOUT_SECONDS = 60.0
_DEFAULT_CLEANUP_TIMEOUT_SECONDS = 3.0
_MAX_CLEANUP_TIMEOUT_SECONDS = 10.0
_CONTROL_SOCKET_NAME = "m"
_BROKER_SOCKET_NAME = "a"
_RUNTIME_PREFIX = "oe-s-"
_SSH_OWNER_LAUNCHER = r"""
import os
import stat
import sys

if len(sys.argv) < 5 or sys.argv[1] != "--openevo-system-ssh-owner-v1":
    raise SystemExit(126)
descriptor_text = sys.argv[2]
if not descriptor_text.isascii() or not descriptor_text.isdecimal():
    raise SystemExit(126)
descriptor = int(descriptor_text)
argv = sys.argv[3:]
if not argv or argv[0] != "/usr/bin/ssh":
    raise SystemExit(126)
allowed = {
    "DISPLAY",
    "HOME",
    "LANG",
    "LC_ALL",
    "LC_CTYPE",
    "LC_MESSAGES",
    "OPENEVO_SSH_ASKPASS_CAPABILITY",
    "OPENEVO_SSH_ASKPASS_SOCKET",
    "OPENEVO_SSH_CONNECTION_GENERATION",
    "PATH",
    "SSH_ASKPASS",
    "SSH_ASKPASS_REQUIRE",
    "SSH_AUTH_SOCK",
}
if set(os.environ) - allowed:
    raise SystemExit(126)
identity_fields = (
    "st_dev",
    "st_ino",
    "st_mode",
    "st_uid",
    "st_nlink",
    "st_size",
    "st_mtime_ns",
    "st_ctime_ns",
)
opened = os.fstat(descriptor)
resolved = os.stat(argv[0], follow_symlinks=False)
if not stat.S_ISREG(opened.st_mode) or tuple(
    getattr(opened, field) for field in identity_fields
) != tuple(getattr(resolved, field) for field in identity_fields):
    raise SystemExit(126)
environment = dict(os.environ)
environment["OPENEVO_SSH_OWNER_PID"] = str(os.getpid())
os.set_inheritable(descriptor, False)
execution_path = argv[0] if sys.platform == "darwin" else f"/dev/fd/{descriptor}"
os.execve(execution_path, argv, environment)
"""


class SystemOpenSshSessionError(RuntimeError):
    """A fixed, renderer-safe owned-session failure."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(message)


class OwnedSshMasterProcess(Protocol):
    pid: int

    def poll(self) -> int | None: ...

    def wait(self, timeout: float | None = None) -> int: ...

    def terminate(self) -> None: ...

    def kill(self) -> None: ...


class SshMasterLauncher(Protocol):
    def spawn(
        self,
        argv: list[str],
        *,
        environment: dict[str, str],
        control_path: Path,
    ) -> OwnedSshMasterProcess: ...


SessionRunner = Callable[
    [list[str], dict[str, str], float],
    subprocess.CompletedProcess[bytes],
]


@dataclass(frozen=True, slots=True)
class _FileIdentity:
    device: int
    inode: int
    mode: int
    links: int
    owner: int
    size: int
    modified_ns: int
    changed_ns: int


@dataclass(frozen=True, slots=True)
class _DirectoryIdentity:
    device: int
    inode: int
    mode: int
    owner: int


@dataclass(frozen=True, slots=True)
class _SocketIdentity:
    device: int
    inode: int
    mode: int
    links: int
    owner: int


class AskpassHelperAuthority:
    """Hold and revalidate the exact inventoried native helper."""

    def __init__(
        self,
        *,
        path: Path,
        descriptor: int,
        identity: _FileIdentity,
        sha256: str,
    ) -> None:
        self._path = path
        self._descriptor = descriptor
        self._identity = identity
        self._sha256 = sha256
        self._closed = False

    @classmethod
    def open(
        cls,
        path: Path | str,
        *,
        expected_sha256: str,
        expected_byte_size: int | None = None,
    ) -> AskpassHelperAuthority:
        candidate = Path(path)
        if (
            not candidate.is_absolute()
            or len(os.fsencode(candidate)) > 4_096
            or len(expected_sha256) != 64
            or any(value not in "0123456789abcdef" for value in expected_sha256)
            or (
                expected_byte_size is not None
                and (
                    type(expected_byte_size) is not int
                    or not 0 < expected_byte_size <= _MAX_HELPER_BYTES
                )
            )
        ):
            raise _session_error("askpass_helper_invalid", "SSH askpass helper is invalid.")
        try:
            before = os.lstat(candidate)
            _require_helper_metadata(before)
            descriptor = os.open(candidate, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
        except (OSError, ValueError) as exc:
            raise _session_error(
                "askpass_helper_invalid", "SSH askpass helper is unavailable."
            ) from exc
        try:
            opened = os.fstat(descriptor)
            _require_helper_metadata(opened)
            identity = _file_identity(opened)
            if expected_byte_size is not None and identity.size != expected_byte_size:
                raise ValueError("helper size does not match its inventory")
            if _file_identity(before) != identity:
                raise ValueError("helper changed while opening")
            digest = _hash_descriptor(descriptor, identity.size)
            after = os.lstat(candidate)
            if _file_identity(after) != identity or digest != expected_sha256:
                raise ValueError("helper identity does not match its inventory")
            return cls(
                path=candidate,
                descriptor=descriptor,
                identity=identity,
                sha256=digest,
            )
        except BaseException as exc:
            os.close(descriptor)
            if isinstance(exc, SystemOpenSshSessionError):
                raise
            raise _session_error(
                "askpass_helper_invalid", "SSH askpass helper identity is invalid."
            ) from exc

    @property
    def path(self) -> Path:
        return self._path

    @property
    def sha256(self) -> str:
        return self._sha256

    def verify(self) -> None:
        if self._closed:
            raise _session_error(
                "askpass_helper_invalid", "SSH askpass helper authority is closed."
            )
        try:
            opened = os.fstat(self._descriptor)
            current = os.lstat(self._path)
            _require_helper_metadata(opened)
            _require_helper_metadata(current)
        except (OSError, ValueError) as exc:
            raise _session_error(
                "askpass_helper_invalid", "SSH askpass helper identity changed."
            ) from exc
        if _file_identity(opened) != self._identity or _file_identity(current) != self._identity:
            raise _session_error("askpass_helper_invalid", "SSH askpass helper identity changed.")

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        os.close(self._descriptor)
        self._descriptor = -1

    def __repr__(self) -> str:
        return "AskpassHelperAuthority(<sealed>)"


@dataclass(frozen=True, slots=True)
class SystemOpenSshSessionSnapshot:
    profile_id: str
    ssh_host_alias: str
    connection_generation: int
    runtime_directory: Path
    control_path: Path
    owner_process_id: int
    owner_birth_identity: str
    process_group_id: int
    session_id: int
    control_socket_device: int
    control_socket_inode: int


class _SystemSshMasterLauncher:
    def spawn(
        self,
        argv: list[str],
        *,
        environment: dict[str, str],
        control_path: Path,
    ) -> OwnedSshMasterProcess:
        del control_path
        executable = VerifiedSystemExecutable.open(SSH_EXECUTABLE)
        try:
            executable.verify_path_binding()
            launcher = _owned_python_launcher()
            process = subprocess.Popen(
                [
                    launcher,
                    "-I",
                    "-c",
                    _SSH_OWNER_LAUNCHER,
                    SYSTEM_OPENSSH_OWNER_ARGUMENT,
                    str(executable.descriptor),
                    *argv,
                ],
                executable=launcher,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                close_fds=True,
                pass_fds=(executable.descriptor,),
                start_new_session=False,
                env=environment,
            )
            executable.verify_path_binding()
            return process
        finally:
            executable.close()


class _PrivateRuntimeDirectory:
    def __init__(self, *, path: Path, descriptor: int, identity: _DirectoryIdentity) -> None:
        self.path = path
        self.descriptor = descriptor
        self.identity = identity
        self._removed = False

    @classmethod
    def create(cls, parent: Path | str) -> _PrivateRuntimeDirectory:
        parent_path = Path(parent)
        _require_runtime_parent(parent_path)
        try:
            path = Path(tempfile.mkdtemp(prefix=_RUNTIME_PREFIX, dir=parent_path))
            os.chmod(path, 0o700, follow_symlinks=False)
            descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        except OSError as exc:
            raise _session_error(
                "ssh_runtime_unavailable", "Private SSH runtime could not be created."
            ) from exc
        try:
            metadata = os.fstat(descriptor)
            path_metadata = os.lstat(path)
            _require_private_directory(metadata)
            identity = _directory_identity(metadata)
            if _directory_identity(path_metadata) != identity:
                raise ValueError("runtime path binding changed")
            if len(os.fsencode(path / _CONTROL_SOCKET_NAME)) > 103:
                raise ValueError("runtime control path is too long")
            return cls(path=path, descriptor=descriptor, identity=identity)
        except BaseException:
            os.close(descriptor)
            try:
                path.rmdir()
            except OSError:
                pass
            raise

    def verify(self) -> None:
        try:
            opened = os.fstat(self.descriptor)
            current = os.lstat(self.path)
            _require_private_directory(opened)
            _require_private_directory(current)
        except (OSError, ValueError) as exc:
            raise _session_error(
                "ssh_runtime_identity_changed", "Private SSH runtime identity changed."
            ) from exc
        if (
            _directory_identity(opened) != self.identity
            or _directory_identity(current) != self.identity
        ):
            raise _session_error(
                "ssh_runtime_identity_changed", "Private SSH runtime identity changed."
            )

    def remove(self) -> None:
        if self._removed:
            return
        self.verify()
        with os.scandir(self.descriptor) as entries:
            if next(entries, None) is not None:
                raise _session_error(
                    "ssh_cleanup_failed", "Private SSH runtime cleanup is incomplete."
                )
        try:
            os.rmdir(self.path)
        except OSError as exc:
            raise _session_error(
                "ssh_cleanup_failed", "Private SSH runtime cleanup failed."
            ) from exc
        self._removed = True
        os.close(self.descriptor)
        self.descriptor = -1


class SystemOpenSshSession:
    """Own one interactive OpenSSH master and all follower authority."""

    def __init__(
        self,
        profile: SystemOpenSshAliasProfile,
        *,
        connection_generation: int,
        askpass_helper: AskpassHelperAuthority,
        home: Path | str,
        inherited_environment: Mapping[str, str],
        owns_askpass_helper: bool = False,
        runtime_parent: Path | str = "/tmp",
        inspector: ProcessInspector | None = None,
        launcher: SshMasterLauncher | None = None,
        runner: SessionRunner | None = None,
        startup_timeout_seconds: float = _DEFAULT_STARTUP_TIMEOUT_SECONDS,
        cleanup_timeout_seconds: float = _DEFAULT_CLEANUP_TIMEOUT_SECONDS,
    ) -> None:
        if not isinstance(profile, SystemOpenSshAliasProfile):
            raise TypeError("system OpenSSH session requires an alias profile")
        if (
            type(connection_generation) is not int
            or not 1 <= connection_generation <= _MAX_SAFE_GENERATION
        ):
            raise ValueError("connection generation is invalid")
        if not isinstance(askpass_helper, AskpassHelperAuthority):
            raise TypeError("system OpenSSH session requires a sealed askpass helper")
        if type(owns_askpass_helper) is not bool:
            raise TypeError("askpass helper ownership must be boolean")
        if not 0 < startup_timeout_seconds <= _MAX_STARTUP_TIMEOUT_SECONDS:
            raise ValueError("SSH startup timeout is invalid")
        if not 0 < cleanup_timeout_seconds <= _MAX_CLEANUP_TIMEOUT_SECONDS:
            raise ValueError("SSH cleanup timeout is invalid")
        self._profile = profile
        self._generation = connection_generation
        self._askpass_helper = askpass_helper
        self._owns_askpass_helper = owns_askpass_helper
        self._home = os.fspath(home)
        self._inherited_environment = dict(inherited_environment)
        self._runtime_parent = Path(runtime_parent)
        self._inspector = inspector or SystemProcessInspector()
        self._launcher = launcher or _SystemSshMasterLauncher()
        self._runner = runner or _run_bounded_subprocess
        self._startup_timeout = startup_timeout_seconds
        self._cleanup_timeout = cleanup_timeout_seconds
        self._guard = threading.RLock()
        self._runtime: _PrivateRuntimeDirectory | None = None
        self._broker: AskpassAuthorizationBroker | None = None
        self._process: OwnedSshMasterProcess | None = None
        self._owner_identity: ProcessIdentity | None = None
        self._control_identity: _SocketIdentity | None = None
        self._snapshot: SystemOpenSshSessionSnapshot | None = None
        self._started = False
        self._closed = False
        self._cancelled = False
        self._poisoned = False

    @property
    def askpass_helper(self) -> AskpassHelperAuthority:
        return self._askpass_helper

    @property
    def closed(self) -> bool:
        with self._guard:
            return self._closed

    @property
    def cancelled(self) -> bool:
        with self._guard:
            return self._cancelled

    @property
    def poisoned(self) -> bool:
        with self._guard:
            return self._poisoned

    def start(self) -> SystemOpenSshSessionSnapshot:
        with self._guard:
            if self._started or self._closed:
                raise _session_error("ssh_session_state_invalid", "SSH session cannot be started.")
            self._started = True
        try:
            runtime = _PrivateRuntimeDirectory.create(self._runtime_parent)
            self._runtime = runtime
            control_path = runtime.path / _CONTROL_SOCKET_NAME
            broker = AskpassAuthorizationBroker(
                runtime.path / _BROKER_SOCKET_NAME,
                helper_path=self._askpass_helper.path,
                inspector=self._inspector,
            )
            self._broker = broker
            broker.start()
            capability = broker.issue_capability(connection_generation=self._generation)
            self._askpass_helper.verify()
            environment = build_system_openssh_environment(
                home=self._home,
                inherited=self._inherited_environment,
                askpass=SystemOpenSshAskpassEnvironment(
                    helper_path=str(self._askpass_helper.path),
                    broker_socket=str(broker.socket_path),
                    capability=capability.value,
                    connection_generation=self._generation,
                ),
            )
            argv = build_system_openssh_master_argv(
                self._profile,
                control_path=control_path,
            )
            process = self._launcher.spawn(
                argv,
                environment=environment,
                control_path=control_path,
            )
            self._process = process
            deadline = time.monotonic() + self._startup_timeout
            owner = self._await_owner_identity(process, deadline=deadline)
            self._owner_identity = owner
            broker.bind_owner(capability, owner)
            control_identity = self._await_control_socket(
                process,
                control_path=control_path,
                deadline=deadline,
            )
            self._control_identity = control_identity
            self._check_master(deadline=deadline)
            snapshot = SystemOpenSshSessionSnapshot(
                profile_id=self._profile.profile_id,
                ssh_host_alias=self._profile.ssh_host_alias,
                connection_generation=self._generation,
                runtime_directory=runtime.path,
                control_path=control_path,
                owner_process_id=owner.process_id,
                owner_birth_identity=owner.birth_identity,
                process_group_id=owner.process_group_id,
                session_id=owner.session_id,
                control_socket_device=control_identity.device,
                control_socket_inode=control_identity.inode,
            )
            with self._guard:
                if self._closed or self._cancelled:
                    raise _session_error(
                        "ssh_connection_cancelled", "SSH connection was cancelled."
                    )
                self._snapshot = snapshot
            return snapshot
        except BaseException:
            try:
                self._shutdown()
            except BaseException:
                pass
            raise

    def snapshot(self) -> SystemOpenSshSessionSnapshot:
        with self._guard:
            if self._snapshot is None:
                raise _session_error("ssh_session_unavailable", "SSH session is not connected.")
            self._require_healthy_locked()
            return self._snapshot

    def command_argv(self, remote_command: str) -> list[str]:
        with self._guard:
            snapshot = self._require_healthy_locked()
            return build_system_openssh_command_argv(
                self._profile,
                control_path=snapshot.control_path,
                remote_command=remote_command,
            )

    def upload_argv(
        self,
        *,
        local_path: Path | str,
        remote_path: str,
        delete: bool = False,
    ) -> list[str]:
        with self._guard:
            snapshot = self._require_healthy_locked()
            return build_system_openssh_upload_argv(
                self._profile,
                control_path=snapshot.control_path,
                local_path=local_path,
                remote_path=remote_path,
                delete=delete,
            )

    def core_tunnel_argv(self, *, remote_port: int) -> list[str]:
        with self._guard:
            snapshot = self._require_healthy_locked()
            return build_system_openssh_core_tunnel_argv(
                self._profile,
                control_path=snapshot.control_path,
                remote_port=remote_port,
            )

    def run(self, command: str, *, timeout_seconds: float = 30.0) -> RemoteCommandResult:
        argv = self.command_argv(command)
        environment = self._base_environment()
        completed = self._runner(argv, environment, timeout_seconds)
        return RemoteCommandResult(
            command=command,
            return_code=completed.returncode,
            stdout=_decode_output(completed.stdout),
            stderr=_decode_output(completed.stderr),
        )

    def cancel(self) -> None:
        with self._guard:
            if self._closed:
                return
            self._cancelled = True
            broker = self._broker
        if broker is not None:
            broker.cancel_generation(self._generation)
        self._shutdown()

    def close(self) -> None:
        self._shutdown()

    def _shutdown(self) -> None:
        with self._guard:
            if self._closed:
                return
            self._closed = True
            broker = self._broker
            process = self._process
            owner = self._owner_identity
            runtime = self._runtime
            control_identity = self._control_identity
            snapshot = self._snapshot
        failure: BaseException | None = None
        if broker is not None:
            try:
                broker.cancel_generation(self._generation)
            except BaseException as exc:
                failure = exc
        process_owned = process is not None and owner is not None and self._same_owner(owner)
        if process is not None and process.poll() is None and process_owned:
            if snapshot is not None and control_identity is not None:
                try:
                    self._verify_control_socket(snapshot.control_path, control_identity)
                    self._runner(
                        build_system_openssh_control_argv(
                            self._profile,
                            control_path=snapshot.control_path,
                            operation="exit",
                        ),
                        self._base_environment(),
                        min(self._cleanup_timeout, 1.0),
                    )
                except BaseException as exc:
                    if failure is None:
                        failure = exc
            if not _wait_process(process, self._cleanup_timeout / 3):
                try:
                    process.terminate()
                except BaseException as exc:
                    if failure is None:
                        failure = exc
                if not _wait_process(process, self._cleanup_timeout / 3):
                    try:
                        process.kill()
                    except BaseException as exc:
                        if failure is None:
                            failure = exc
                    if not _wait_process(process, self._cleanup_timeout / 3) and failure is None:
                        failure = _session_error(
                            "ssh_cleanup_failed", "SSH master did not stop before its deadline."
                        )
        elif process is not None and process.poll() is None and not process_owned:
            if failure is None:
                failure = _session_error(
                    "ssh_process_identity_changed",
                    "SSH master process identity changed before cleanup.",
                )
        if broker is not None:
            try:
                broker.close()
            except BaseException as exc:
                if failure is None:
                    failure = exc
        if runtime is not None:
            control_path = runtime.path / _CONTROL_SOCKET_NAME
            if control_identity is not None:
                try:
                    metadata = os.lstat(control_path)
                except FileNotFoundError:
                    pass
                except OSError as exc:
                    if failure is None:
                        failure = exc
                else:
                    if _socket_identity(metadata) != control_identity:
                        if failure is None:
                            failure = _session_error(
                                "ssh_control_socket_changed",
                                "SSH control socket identity changed before cleanup.",
                            )
                    else:
                        try:
                            control_path.unlink()
                        except OSError as exc:
                            if failure is None:
                                failure = exc
            try:
                runtime.remove()
            except BaseException as exc:
                if failure is None:
                    failure = exc
        if self._owns_askpass_helper:
            try:
                self._askpass_helper.close()
            except BaseException as exc:
                if failure is None:
                    failure = exc
        if failure is not None:
            if isinstance(failure, SystemOpenSshSessionError):
                raise failure
            if isinstance(failure, AskpassBrokerError):
                raise _session_error(
                    "ssh_askpass_broker_failed", "SSH prompt broker cleanup failed."
                ) from failure
            raise _session_error("ssh_cleanup_failed", "SSH session cleanup failed.") from failure

    def _await_owner_identity(
        self,
        process: OwnedSshMasterProcess,
        *,
        deadline: float,
    ) -> ProcessIdentity:
        while time.monotonic() < deadline:
            if process.poll() is not None:
                raise _session_error(
                    "ssh_master_exited", "System OpenSSH master exited during startup."
                )
            identity = self._inspector.inspect(process.pid)
            if identity is not None and identity.executable_path == SSH_EXECUTABLE:
                if (
                    identity.user_id != os.geteuid()
                    or identity.process_group_id != os.getpgrp()
                    or identity.session_id != os.getsid(0)
                ):
                    raise _session_error(
                        "ssh_process_identity_changed",
                        "System OpenSSH master has an invalid process authority.",
                    )
                return identity
            time.sleep(_STARTUP_POLL_SECONDS)
        raise _session_error(
            "ssh_startup_timeout", "System OpenSSH master did not start before its deadline."
        )

    def _await_control_socket(
        self,
        process: OwnedSshMasterProcess,
        *,
        control_path: Path,
        deadline: float,
    ) -> _SocketIdentity:
        while time.monotonic() < deadline:
            if process.poll() is not None:
                raise _session_error(
                    "ssh_master_exited", "System OpenSSH master exited during startup."
                )
            try:
                metadata = os.lstat(control_path)
            except FileNotFoundError:
                time.sleep(_STARTUP_POLL_SECONDS)
                continue
            except OSError as exc:
                raise _session_error(
                    "ssh_control_socket_invalid", "SSH control socket is unavailable."
                ) from exc
            _require_private_socket(metadata)
            return _socket_identity(metadata)
        raise _session_error(
            "ssh_startup_timeout", "SSH control socket did not start before its deadline."
        )

    def _check_master(self, *, deadline: float) -> None:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise _session_error(
                "ssh_startup_timeout", "SSH master check exceeded its startup deadline."
            )
        runtime = self._runtime
        if runtime is None:
            raise _session_error("ssh_session_unavailable", "SSH runtime is unavailable.")
        completed = self._runner(
            build_system_openssh_control_argv(
                self._profile,
                control_path=runtime.path / _CONTROL_SOCKET_NAME,
                operation="check",
            ),
            self._base_environment(),
            remaining,
        )
        if completed.returncode != 0:
            raise _session_error(
                "ssh_connection_failed", "System OpenSSH could not establish the connection."
            )

    def _base_environment(self) -> dict[str, str]:
        return build_system_openssh_environment(
            home=self._home,
            inherited=self._inherited_environment,
        )

    def _require_healthy_locked(self) -> SystemOpenSshSessionSnapshot:
        if self._closed or self._cancelled or self._snapshot is None:
            raise _session_error("ssh_session_unavailable", "SSH session is not connected.")
        if self._poisoned:
            raise _session_error(
                "ssh_session_poisoned", "SSH session authority is no longer valid."
            )
        process = self._process
        owner = self._owner_identity
        runtime = self._runtime
        control_identity = self._control_identity
        assert process is not None and owner is not None
        assert runtime is not None and control_identity is not None
        if process.poll() is not None:
            self._poisoned = True
            raise _session_error("ssh_master_exited", "System OpenSSH master exited.")
        if not self._same_owner(owner):
            self._poisoned = True
            raise _session_error(
                "ssh_process_identity_changed", "System OpenSSH process identity changed."
            )
        try:
            runtime.verify()
            self._askpass_helper.verify()
            if self._broker is None:
                raise AskpassBrokerError("broker missing")
            self._broker.verify_socket_binding()
            self._verify_control_socket(self._snapshot.control_path, control_identity)
        except BaseException as exc:
            self._poisoned = True
            if isinstance(exc, SystemOpenSshSessionError):
                raise
            raise _session_error(
                "ssh_control_socket_changed", "SSH control socket identity changed."
            ) from exc
        return self._snapshot

    def _same_owner(self, owner: ProcessIdentity) -> bool:
        return self._inspector.inspect(owner.process_id) == owner

    @staticmethod
    def _verify_control_socket(path: Path, expected: _SocketIdentity) -> None:
        try:
            metadata = os.lstat(path)
            _require_private_socket(metadata)
        except (OSError, ValueError) as exc:
            raise _session_error(
                "ssh_control_socket_changed", "SSH control socket identity changed."
            ) from exc
        if _socket_identity(metadata) != expected:
            raise _session_error(
                "ssh_control_socket_changed", "SSH control socket identity changed."
            )

    def __enter__(self) -> SystemOpenSshSession:
        self.start()
        return self

    def __exit__(self, _type: object, _value: object, _traceback: object) -> None:
        self.close()


SessionFactory = Callable[
    [SystemOpenSshAliasProfile, int],
    SystemOpenSshSession,
]


class SystemOpenSshSessionOwner:
    """Serialize profile replacement without ever adopting an ambient master."""

    def __init__(self, factory: SessionFactory) -> None:
        self._factory = factory
        self._guard = threading.Lock()
        self._active: SystemOpenSshSession | None = None
        self._closed = False

    def connect(
        self,
        profile: SystemOpenSshAliasProfile,
        *,
        connection_generation: int,
    ) -> SystemOpenSshSessionSnapshot:
        with self._guard:
            if self._closed:
                raise _session_error("ssh_session_owner_closed", "SSH session owner is closed.")
            previous, self._active = self._active, None
            if previous is not None:
                previous.close()
            session = self._factory(profile, connection_generation)
            try:
                snapshot = session.start()
            except BaseException:
                try:
                    session.close()
                except BaseException:
                    pass
                raise
            self._active = session
            return snapshot

    def disconnect(self) -> None:
        with self._guard:
            active, self._active = self._active, None
            if active is not None:
                active.close()

    def active_session(self) -> SystemOpenSshSession:
        with self._guard:
            if self._active is None:
                raise _session_error("ssh_session_unavailable", "SSH session is not connected.")
            self._active.snapshot()
            return self._active

    def close(self) -> None:
        with self._guard:
            if self._closed:
                return
            self._closed = True
            active, self._active = self._active, None
            if active is not None:
                active.close()


def _owned_python_launcher() -> str:
    if sys.platform.startswith("linux") and getattr(sys, "frozen", False) is True:
        return "/proc/self/exe"
    return sys.executable


def _run_bounded_subprocess(
    argv: list[str],
    environment: dict[str, str],
    timeout_seconds: float,
) -> subprocess.CompletedProcess[bytes]:
    if not 0 < timeout_seconds <= 3600:
        raise ValueError("SSH subprocess timeout is invalid")
    process = subprocess.Popen(
        argv,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=environment,
        close_fds=True,
        start_new_session=False,
    )
    assert process.stdout is not None and process.stderr is not None
    deadline = time.monotonic() + timeout_seconds
    selector = selectors.DefaultSelector()
    selector.register(process.stdout, selectors.EVENT_READ, "stdout")
    selector.register(process.stderr, selectors.EVENT_READ, "stderr")
    chunks: dict[str, list[bytes]] = {"stdout": [], "stderr": []}
    captured = 0
    try:
        while selector.get_map():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise subprocess.TimeoutExpired(argv, timeout_seconds)
            for key, _events in selector.select(min(remaining, 0.05)):
                chunk = os.read(
                    key.fd,
                    min(_CAPTURE_CHUNK_BYTES, _MAX_CAPTURE_BYTES - captured + 1),
                )
                if not chunk:
                    selector.unregister(key.fileobj)
                    continue
                captured += len(chunk)
                if captured > _MAX_CAPTURE_BYTES:
                    raise SystemOpenSshSessionError(
                        "ssh_output_limit_exceeded", "SSH command output exceeded its limit."
                    )
                chunks[key.data].append(chunk)
        remaining = deadline - time.monotonic()
        process.wait(timeout=max(0.001, remaining))
    except BaseException:
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=0.5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=0.5)
        raise
    finally:
        selector.close()
        process.stdout.close()
        process.stderr.close()
    return subprocess.CompletedProcess(
        argv,
        process.returncode,
        stdout=b"".join(chunks["stdout"]),
        stderr=b"".join(chunks["stderr"]),
    )


def _wait_process(process: OwnedSshMasterProcess, timeout_seconds: float) -> bool:
    if process.poll() is not None:
        return True
    try:
        process.wait(timeout=max(0.001, timeout_seconds))
        return True
    except subprocess.TimeoutExpired:
        return process.poll() is not None
    except (ChildProcessError, OSError):
        return process.poll() is not None


def _require_runtime_parent(path: Path) -> None:
    if not path.is_absolute():
        raise _session_error(
            "ssh_runtime_unavailable", "SSH runtime parent must be an absolute path."
        )
    try:
        metadata = os.lstat(path)
    except OSError as exc:
        raise _session_error(
            "ssh_runtime_unavailable", "SSH runtime parent is unavailable."
        ) from exc
    private_owner = metadata.st_uid == os.geteuid() and stat.S_IMODE(metadata.st_mode) == 0o700
    root_sticky = (
        metadata.st_uid == 0
        and metadata.st_mode & stat.S_ISVTX
        and stat.S_IMODE(metadata.st_mode) & 0o002
    )
    if not stat.S_ISDIR(metadata.st_mode) or not (private_owner or root_sticky):
        raise _session_error(
            "ssh_runtime_unavailable", "SSH runtime parent is not a safe directory."
        )


def _require_private_directory(metadata: os.stat_result) -> None:
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or stat.S_IMODE(metadata.st_mode) != 0o700
        or metadata.st_uid != os.geteuid()
    ):
        raise ValueError("private SSH runtime identity is invalid")


def _require_private_socket(metadata: os.stat_result) -> None:
    if (
        not stat.S_ISSOCK(metadata.st_mode)
        or metadata.st_uid != os.geteuid()
        or metadata.st_nlink != 1
        or stat.S_IMODE(metadata.st_mode) & 0o077
    ):
        raise ValueError("private SSH socket identity is invalid")


def _require_helper_metadata(metadata: os.stat_result) -> None:
    if (
        not stat.S_ISREG(metadata.st_mode)
        or metadata.st_uid not in {0, os.geteuid()}
        or stat.S_IMODE(metadata.st_mode) != 0o755
        or metadata.st_nlink != 1
        or not 0 < metadata.st_size <= _MAX_HELPER_BYTES
    ):
        raise ValueError("askpass helper metadata is invalid")


def _file_identity(metadata: os.stat_result) -> _FileIdentity:
    return _FileIdentity(
        device=metadata.st_dev,
        inode=metadata.st_ino,
        mode=metadata.st_mode,
        links=metadata.st_nlink,
        owner=metadata.st_uid,
        size=metadata.st_size,
        modified_ns=metadata.st_mtime_ns,
        changed_ns=metadata.st_ctime_ns,
    )


def _directory_identity(metadata: os.stat_result) -> _DirectoryIdentity:
    return _DirectoryIdentity(
        device=metadata.st_dev,
        inode=metadata.st_ino,
        mode=metadata.st_mode,
        owner=metadata.st_uid,
    )


def _socket_identity(metadata: os.stat_result) -> _SocketIdentity:
    return _SocketIdentity(
        device=metadata.st_dev,
        inode=metadata.st_ino,
        mode=metadata.st_mode,
        links=metadata.st_nlink,
        owner=metadata.st_uid,
    )


def _hash_descriptor(descriptor: int, expected_size: int) -> str:
    digest = hashlib.sha256()
    offset = 0
    while offset < expected_size:
        chunk = os.pread(descriptor, min(1024 * 1024, expected_size - offset), offset)
        if not chunk:
            raise ValueError("askpass helper ended before its declared size")
        digest.update(chunk)
        offset += len(chunk)
    if os.pread(descriptor, 1, offset):
        raise ValueError("askpass helper exceeds its declared size")
    return digest.hexdigest()


def _decode_output(value: bytes | str | None) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    return value.decode(locale.getpreferredencoding(False), errors="replace")


def _session_error(code: str, message: str) -> SystemOpenSshSessionError:
    return SystemOpenSshSessionError(code, message)


__all__ = (
    "AskpassHelperAuthority",
    "OwnedSshMasterProcess",
    "SshMasterLauncher",
    "SystemOpenSshSession",
    "SystemOpenSshSessionError",
    "SystemOpenSshSessionOwner",
    "SystemOpenSshSessionSnapshot",
)
