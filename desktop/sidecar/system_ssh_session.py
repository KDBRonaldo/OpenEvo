"""One sidecar-owned system OpenSSH master for a v2 remote workspace."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
import hashlib
import locale
import os
from pathlib import Path
import re
import selectors
import shlex
import signal
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
    AskpassPromptObservation,
    ProcessIdentity,
    ProcessInspector,
    SystemProcessInspector,
)
from desktop.sidecar.lifecycle_logs_v2 import (
    LifecycleLogSourceV2,
    LifecycleRawOutputObserverV2,
)
from openevo.deployment.preflight import RemoteCommandResult
from openevo.deployment.profile import SystemOpenSshAliasProfile
from openevo.deployment.remote_home import (
    REMOTE_HOME_PROBE_OUTPUT_LIMIT,
    RemoteHomeAuthority,
    build_remote_home_guarded_command,
    build_remote_home_guarded_rsync_path,
    build_remote_home_probe_command,
    parse_remote_home_probe,
)
from openevo.deployment.host_keys import (
    PendingSystemHostKeyReview,
    SystemHostKeyFailureCode,
    SystemHostKeyFailureEvidence,
    SystemHostKeyReviewAuthority,
    SystemKnownHostsPolicy,
    classify_system_openssh_host_key_failure,
    inspect_system_known_hosts_policy,
)
from openevo.deployment.ssh import (
    SystemOpenSshAskpassEnvironment,
    build_system_openssh_command_argv,
    build_system_openssh_control_argv,
    build_system_openssh_core_tunnel_argv,
    build_system_openssh_environment,
    build_system_openssh_probe_argv,
    build_system_openssh_master_argv,
    build_system_openssh_upload_argv,
    build_system_ssh_keygen_remove_argv,
)
from openevo.deployment.system_executables import (
    RSYNC_EXECUTABLE,
    SSH_EXECUTABLE,
    SYSTEM_OPENSSH_OWNER_ARGUMENT,
    VerifiedSystemExecutable,
)


_MAX_SAFE_GENERATION = (1 << 53) - 1
_MAX_HELPER_BYTES = 16 * 1024 * 1024
_MAX_CAPTURE_BYTES = 4 * 1024 * 1024
_MAX_MASTER_DIAGNOSTIC_BYTES = 64 * 1024
_CAPTURE_CHUNK_BYTES = 64 * 1024
_STARTUP_POLL_SECONDS = 0.02
_DEFAULT_STARTUP_TIMEOUT_SECONDS = 20.0
_MAX_STARTUP_TIMEOUT_SECONDS = 60.0
_DEFAULT_CLEANUP_TIMEOUT_SECONDS = 3.0
_MAX_CLEANUP_TIMEOUT_SECONDS = 10.0
_CONTROL_SOCKET_NAME = "m"
_BROKER_SOCKET_NAME = "a"
_RUNTIME_PREFIX = "oe-s-"


def _default_runtime_parent_for_platform(platform: str) -> Path:
    return Path("/private/tmp") if platform == "darwin" else Path("/tmp")


_DEFAULT_RUNTIME_PARENT = _default_runtime_parent_for_platform(sys.platform)


class SystemOpenSshSessionError(RuntimeError):
    """A fixed, renderer-safe owned-session failure."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        host_key_evidence: SystemHostKeyFailureEvidence | None = None,
        host_key_review: PendingSystemHostKeyReview | None = None,
    ) -> None:
        self.code = code
        self.host_key_evidence = host_key_evidence
        self.host_key_review = host_key_review
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
class SystemOpenSshTrustFailure:
    code: str
    evidence: SystemHostKeyFailureEvidence = field(repr=False)
    review: PendingSystemHostKeyReview | None


class SystemOpenSshHostTrust:
    """Mediate changed-key evidence without owning a second trust database."""

    def __init__(
        self,
        *,
        home: Path | str,
        inherited_environment: Mapping[str, str],
        review_authority: SystemHostKeyReviewAuthority | None = None,
        runner: SessionRunner | None = None,
    ) -> None:
        self._home = Path(home)
        self._inherited_environment = dict(inherited_environment)
        self._owns_review_authority = review_authority is None
        self._review_authority = review_authority or SystemHostKeyReviewAuthority()
        self._runner = runner or _run_verified_bounded_subprocess
        self._closed = False

    @property
    def review_authority(self) -> SystemHostKeyReviewAuthority:
        return self._review_authority

    def evaluate_failure(
        self,
        profile: SystemOpenSshAliasProfile,
        *,
        connection_generation: int,
        stderr: bytes,
        conditional_config: bool = False,
        timeout_seconds: float = 5.0,
    ) -> SystemOpenSshTrustFailure:
        if self._closed:
            raise _session_error(
                "ssh_host_trust_unavailable", "System SSH host trust is unavailable."
            )
        evidence = classify_system_openssh_host_key_failure(stderr)
        if evidence.code is not SystemHostKeyFailureCode.CHANGED:
            return SystemOpenSshTrustFailure(
                code=evidence.code.value,
                evidence=evidence,
                review=None,
            )
        if conditional_config:
            policy = SystemKnownHostsPolicy(
                repair_support="administrator_required",
                reason="conditional_config",
                known_hosts_file=None,
                lookup_token=None,
                _file_identity=None,
            )
        else:
            policy = self._inspect_policy(
                profile,
                offending_known_hosts_file=evidence.offending_known_hosts_file,
                timeout_seconds=timeout_seconds,
            )
        review = self._review_authority.issue(
            profile,
            connection_generation=connection_generation,
            evidence=evidence,
            policy=policy,
        )
        return SystemOpenSshTrustFailure(
            code=evidence.code.value,
            evidence=evidence,
            review=review,
        )

    def replace_changed_key(
        self,
        review: PendingSystemHostKeyReview,
        *,
        profile: SystemOpenSshAliasProfile,
        connection_generation: int,
        review_id: str,
        review_sha256: str,
        timeout_seconds: float = 5.0,
    ) -> None:
        if self._closed:
            raise _session_error(
                "ssh_host_trust_unavailable", "System SSH host trust is unavailable."
            )
        try:
            replacement = self._review_authority.claim_replacement(
                review,
                profile=profile,
                connection_generation=connection_generation,
                review_id=review_id,
                review_sha256=review_sha256,
            )
            replacement.verify_current()
        except (OSError, TypeError, ValueError) as exc:
            raise _session_error(
                "ssh_host_key_review_invalid",
                "The changed host-key review is no longer current.",
            ) from exc
        try:
            completed = self._runner(
                build_system_ssh_keygen_remove_argv(
                    lookup_token=replacement.lookup_token,
                    known_hosts_file=replacement.known_hosts_file,
                ),
                self._environment(),
                timeout_seconds,
            )
            if completed.returncode != 0:
                raise ValueError("system ssh-keygen replacement failed")
            replacement.verify_replaced()
        except (OSError, ValueError, subprocess.TimeoutExpired) as exc:
            raise _session_error(
                "ssh_host_key_repair_failed",
                "The changed host key could not be removed from the user trust store.",
            ) from exc

    def reissue_changed_key_review(
        self,
        current_review: PendingSystemHostKeyReview,
        *,
        profile: SystemOpenSshAliasProfile,
        connection_generation: int,
        review_id: str,
        review_sha256: str,
    ) -> PendingSystemHostKeyReview:
        if self._closed:
            raise _session_error(
                "ssh_host_trust_unavailable", "System SSH host trust is unavailable."
            )
        try:
            return self._review_authority.reissue_matching_review(
                current_review,
                profile=profile,
                connection_generation=connection_generation,
                review_id=review_id,
                review_sha256=review_sha256,
            )
        except (OSError, TypeError, ValueError) as exc:
            raise _session_error(
                "ssh_host_key_review_invalid",
                "The changed host-key review is no longer current.",
            ) from exc

    def _inspect_policy(
        self,
        profile: SystemOpenSshAliasProfile,
        *,
        offending_known_hosts_file: Path | None,
        timeout_seconds: float,
    ) -> SystemKnownHostsPolicy:
        try:
            completed = self._runner(
                build_system_openssh_probe_argv(profile),
                self._environment(),
                timeout_seconds,
            )
            if completed.returncode != 0 or type(completed.stdout) is not bytes:
                raise ValueError("effective OpenSSH config is unavailable")
            return inspect_system_known_hosts_policy(
                completed.stdout,
                home=self._home,
                offending_known_hosts_file=offending_known_hosts_file,
            )
        except (OSError, ValueError, subprocess.TimeoutExpired, SystemOpenSshSessionError):
            return SystemKnownHostsPolicy(
                repair_support="administrator_required",
                reason="effective_config_unavailable",
                known_hosts_file=None,
                lookup_token=None,
                _file_identity=None,
            )

    def _environment(self) -> dict[str, str]:
        return build_system_openssh_environment(
            home=os.fspath(self._home),
            inherited=self._inherited_environment,
        )

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self._owns_review_authority:
            self._review_authority.close()


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


class _CapturedSshMasterProcess:
    """Drain bounded SSH diagnostics without ever exposing the raw stream."""

    def __init__(
        self,
        process: subprocess.Popen[bytes],
        output_observer: LifecycleRawOutputObserverV2 | None = None,
    ) -> None:
        if process.stderr is None:
            raise ValueError("system OpenSSH diagnostic stream is unavailable")
        self._process = process
        self._stream = process.stderr
        self._output_observer = output_observer
        self._guard = threading.Lock()
        self._captured = bytearray()
        self._overflow = False
        self._finished = threading.Event()
        self._thread = threading.Thread(
            target=self._drain,
            name="openevo-system-ssh-diagnostics",
            daemon=True,
        )
        self._thread.start()

    @property
    def pid(self) -> int:
        return self._process.pid

    def poll(self) -> int | None:
        return self._process.poll()

    def wait(self, timeout: float | None = None) -> int:
        return self._process.wait(timeout=timeout)

    def terminate(self) -> None:
        self._process.terminate()

    def kill(self) -> None:
        self._process.kill()

    def captured_stderr(self) -> bytes:
        if self._process.poll() is not None:
            self._finished.wait(0.5)
        with self._guard:
            if not self._finished.is_set() or self._overflow:
                return b""
            return bytes(self._captured)

    def _drain(self) -> None:
        try:
            while True:
                chunk = self._stream.read(8_192)
                if not chunk:
                    break
                _notify_output_observer(self._output_observer, "ssh_stderr", chunk)
                with self._guard:
                    if self._overflow:
                        continue
                    if len(self._captured) + len(chunk) > _MAX_MASTER_DIAGNOSTIC_BYTES:
                        self._captured[:] = b"\0" * len(self._captured)
                        self._captured.clear()
                        self._overflow = True
                    else:
                        self._captured.extend(chunk)
        except OSError:
            with self._guard:
                self._captured[:] = b"\0" * len(self._captured)
                self._captured.clear()
                self._overflow = True
        finally:
            self._stream.close()
            self._finished.set()


class _SystemSshMasterLauncher:
    def __init__(
        self,
        owner_helper: AskpassHelperAuthority,
        output_observer: LifecycleRawOutputObserverV2 | None = None,
    ) -> None:
        if type(owner_helper) is not AskpassHelperAuthority:
            raise TypeError("system OpenSSH owner helper is invalid")
        self._owner_helper = owner_helper
        self._output_observer = output_observer

    def set_output_observer(self, observer: LifecycleRawOutputObserverV2) -> None:
        if not callable(observer):
            raise TypeError("system OpenSSH output observer is invalid")
        self._output_observer = observer

    def spawn(
        self,
        argv: list[str],
        *,
        environment: dict[str, str],
        control_path: Path,
    ) -> OwnedSshMasterProcess:
        del control_path
        self._owner_helper.verify()
        executable = VerifiedSystemExecutable.open(SSH_EXECUTABLE)
        process: subprocess.Popen[bytes] | None = None
        try:
            executable.verify_path_binding()
            launcher = os.fspath(self._owner_helper.path)
            process = subprocess.Popen(
                [
                    launcher,
                    SYSTEM_OPENSSH_OWNER_ARGUMENT,
                    str(executable.descriptor),
                    *argv,
                ],
                executable=launcher,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                close_fds=True,
                pass_fds=(executable.descriptor,),
                start_new_session=False,
                env=environment,
            )
            executable.verify_path_binding()
            self._owner_helper.verify()
            return _CapturedSshMasterProcess(process, self._output_observer)
        except BaseException:
            if process is not None and process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=0.5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=0.5)
            raise
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
        runtime_parent: Path | str = _DEFAULT_RUNTIME_PARENT,
        inspector: ProcessInspector | None = None,
        launcher: SshMasterLauncher | None = None,
        runner: SessionRunner | None = None,
        host_trust: SystemOpenSshHostTrust | None = None,
        output_observer: LifecycleRawOutputObserverV2 | None = None,
        conditional_config: bool = False,
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
        if host_trust is not None and not isinstance(host_trust, SystemOpenSshHostTrust):
            raise TypeError("system OpenSSH host trust authority is invalid")
        if output_observer is not None and not callable(output_observer):
            raise TypeError("system OpenSSH output observer is invalid")
        if type(conditional_config) is not bool:
            raise TypeError("conditional OpenSSH config flag must be boolean")
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
        self._output_observer = output_observer
        self._launcher = launcher or _SystemSshMasterLauncher(
            askpass_helper,
            output_observer,
        )
        self._runner = runner or _run_bounded_subprocess
        self._uses_default_runner = runner is None
        self._owns_host_trust = host_trust is None
        self._host_trust = host_trust or SystemOpenSshHostTrust(
            home=home,
            inherited_environment=inherited_environment,
        )
        self._conditional_config = conditional_config
        self._startup_timeout = startup_timeout_seconds
        self._cleanup_timeout = cleanup_timeout_seconds
        self._guard = threading.RLock()
        self._runtime: _PrivateRuntimeDirectory | None = None
        self._broker: AskpassAuthorizationBroker | None = None
        self._process: OwnedSshMasterProcess | None = None
        self._owner_identity: ProcessIdentity | None = None
        self._control_identity: _SocketIdentity | None = None
        self._snapshot: SystemOpenSshSessionSnapshot | None = None
        self._prompt_observer: Callable[[AskpassPromptObservation], None] | None = None
        self._started = False
        self._closed = False
        self._cancelled = False
        self._poisoned = False

    @property
    def askpass_helper(self) -> AskpassHelperAuthority:
        return self._askpass_helper

    @property
    def ssh_host_alias(self) -> str:
        return self._profile.ssh_host_alias

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

    @property
    def prompt_observation(self) -> AskpassPromptObservation | None:
        with self._guard:
            broker = self._broker
            return broker.prompt_observation if broker is not None else None

    def set_prompt_observer(
        self,
        observer: Callable[[AskpassPromptObservation], None],
    ) -> None:
        if not callable(observer):
            raise TypeError("SSH prompt observer must be callable")
        with self._guard:
            if self._started or self._closed or self._prompt_observer is not None:
                raise _session_error(
                    "ssh_session_state_invalid",
                    "SSH prompt observer cannot be changed.",
                )
            self._prompt_observer = observer

    def set_output_observer(self, observer: LifecycleRawOutputObserverV2) -> None:
        if not callable(observer):
            raise TypeError("SSH output observer must be callable")
        with self._guard:
            if self._started or self._closed or self._output_observer is not None:
                raise _session_error(
                    "ssh_session_state_invalid",
                    "SSH output observer cannot be changed.",
                )
            self._output_observer = observer
            configure = getattr(self._launcher, "set_output_observer", None)
            if callable(configure):
                configure(observer)

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
                observation_callback=self._prompt_observer,
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
        completed = self._run_session_subprocess(argv, environment, timeout_seconds)
        return RemoteCommandResult(
            command=command,
            return_code=completed.returncode,
            stdout=_decode_output(completed.stdout),
            stderr=_decode_output(completed.stderr),
        )

    def discover_remote_home_authority(
        self,
        *,
        timeout_seconds: float = 30.0,
    ) -> RemoteHomeAuthority:
        """Privately bind the effective remote NSS account to this master."""

        try:
            snapshot = self.snapshot()
            command = build_remote_home_probe_command()
            completed = self._run_session_subprocess(
                self.command_argv(command),
                self._base_environment(),
                timeout_seconds,
                observe_output=False,
                max_capture_bytes=REMOTE_HOME_PROBE_OUTPUT_LIMIT,
            )
            if self.snapshot() != snapshot:
                raise ValueError("SSH session binding changed during account discovery")
            return parse_remote_home_probe(
                profile_id=snapshot.profile_id,
                connection_generation=snapshot.connection_generation,
                return_code=completed.returncode,
                stdout=completed.stdout,
                stderr=completed.stderr,
            )
        except Exception:
            raise _session_error(
                "ssh_remote_account_unavailable",
                "The remote SSH account could not be verified.",
            ) from None

    def follower_environment(self) -> dict[str, str]:
        """Return the closed environment for followers of this exact master."""

        with self._guard:
            self._require_healthy_locked()
            return self._base_environment()

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
                    self._run_session_subprocess(
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
        if self._owns_host_trust:
            try:
                self._host_trust.close()
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
                raise self._master_exit_error(process, during_startup=True)
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
                raise self._master_exit_error(process, during_startup=True)
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
        completed = self._run_session_subprocess(
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

    def _run_session_subprocess(
        self,
        argv: list[str],
        environment: dict[str, str],
        timeout_seconds: float,
        *,
        observe_output: bool = True,
        max_capture_bytes: int = _MAX_CAPTURE_BYTES,
    ) -> subprocess.CompletedProcess[bytes]:
        _validate_capture_limit(max_capture_bytes)
        if self._uses_default_runner:
            return _run_bounded_subprocess(
                argv,
                environment,
                timeout_seconds,
                output_observer=self._output_observer if observe_output else None,
                max_capture_bytes=max_capture_bytes,
            )
        completed = self._runner(argv, environment, timeout_seconds)
        if type(completed.stdout) is bytes and type(completed.stderr) is bytes:
            if len(completed.stdout) + len(completed.stderr) > max_capture_bytes:
                raise _session_error(
                    "ssh_output_limit_exceeded",
                    "SSH command output exceeded its limit.",
                )
        if observe_output and type(completed.stdout) is bytes:
            _notify_output_observer(
                self._output_observer,
                "ssh_stdout",
                completed.stdout,
            )
        if observe_output and type(completed.stderr) is bytes:
            _notify_output_observer(
                self._output_observer,
                "ssh_stderr",
                completed.stderr,
            )
        return completed

    def _follower_output_observer(self) -> LifecycleRawOutputObserverV2 | None:
        with self._guard:
            return self._output_observer

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
            raise self._master_exit_error(process, during_startup=False)
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

    def _master_exit_error(
        self,
        process: OwnedSshMasterProcess,
        *,
        during_startup: bool,
    ) -> SystemOpenSshSessionError:
        observation = self.prompt_observation
        if observation is not None:
            if observation.state == "cancelled":
                return _session_error(
                    "ssh_prompt_cancelled",
                    "The system SSH prompt was cancelled.",
                )
            if observation.state == "rejected" and observation.kind == "host_confirmation":
                return _session_error(
                    "ssh_host_key_rejected",
                    "The first-use server identity was not approved.",
                )
            if observation.state == "completed" and observation.kind == "host_confirmation":
                return _session_error(
                    "ssh_first_host_accepted_reconnect_required",
                    "The first-use server identity was approved; authentication must reconnect.",
                )
        stderr = _captured_master_stderr(process)
        lowered = stderr.lower()
        if (
            b"host key verification failed" in lowered
            or b"remote host identification has changed" in lowered
        ):
            failure = self._host_trust.evaluate_failure(
                self._profile,
                connection_generation=self._generation,
                stderr=stderr,
                conditional_config=self._conditional_config,
            )
            messages = {
                SystemHostKeyFailureCode.CHANGED.value: (
                    "The configured server identity changed and requires review."
                ),
                SystemHostKeyFailureCode.FIRST_USE_FORBIDDEN.value: (
                    "The effective SSH policy forbids first-use host approval."
                ),
                SystemHostKeyFailureCode.VERIFICATION_FAILED.value: (
                    "System OpenSSH could not verify the server host key."
                ),
            }
            return _session_error(
                failure.code,
                messages[failure.code],
                host_key_evidence=failure.evidence,
                host_key_review=failure.review,
            )
        message = (
            "System OpenSSH master exited during startup."
            if during_startup
            else "System OpenSSH master exited."
        )
        return _session_error("ssh_master_exited", message)

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
        prompt_observer: Callable[[AskpassPromptObservation], None] | None = None,
        output_observer: LifecycleRawOutputObserverV2 | None = None,
    ) -> SystemOpenSshSessionSnapshot:
        with self._guard:
            if self._closed:
                raise _session_error("ssh_session_owner_closed", "SSH session owner is closed.")
            previous, self._active = self._active, None
            if previous is not None:
                previous.close()
            for attempt in range(2):
                session = self._factory(profile, connection_generation)
                try:
                    if prompt_observer is not None:
                        session.set_prompt_observer(prompt_observer)
                    if output_observer is not None:
                        session.set_output_observer(output_observer)
                    snapshot = session.start()
                except BaseException as exc:
                    try:
                        session.close()
                    except BaseException:
                        pass
                    if (
                        attempt == 0
                        and isinstance(exc, SystemOpenSshSessionError)
                        and exc.code == "ssh_first_host_accepted_reconnect_required"
                    ):
                        continue
                    raise
                self._active = session
                return snapshot
            raise AssertionError("system OpenSSH reconnect loop exhausted")

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


class SystemOpenSshFollowerTransportAuthority:
    """Seal rich deployment followers to one healthy owned SSH master."""

    _MAX_ISSUED = 64

    def __init__(
        self,
        session: SystemOpenSshSession,
        *,
        remote_home_authority: RemoteHomeAuthority,
    ) -> None:
        if type(session) is not SystemOpenSshSession:
            raise TypeError("system OpenSSH follower requires an exact session")
        if type(remote_home_authority) is not RemoteHomeAuthority:
            raise TypeError("system OpenSSH remote account authority is invalid")
        snapshot = session.snapshot()
        self._session = session
        self._remote_home_authority = remote_home_authority
        self._connection_generation = snapshot.connection_generation
        self.ssh_host_alias = session.ssh_host_alias
        self._guard = threading.Lock()
        self._issued: dict[tuple[str, ...], int] = {}
        if not self._matches(snapshot):
            raise ValueError("system OpenSSH remote account authority is inconsistent")

    @property
    def remote_user(self) -> str:
        return self._remote_home_authority.remote_user

    @property
    def remote_home_authority(self) -> RemoteHomeAuthority:
        return self._remote_home_authority

    @property
    def connection_generation(self) -> int:
        self.verify_authority()
        return self._connection_generation

    def verify_authority(self) -> None:
        if not self._matches(self._session.snapshot()):
            raise _session_error(
                "ssh_remote_account_unavailable",
                "The remote SSH account could not be verified.",
            )

    def command_argv(self, remote_command: str) -> list[str]:
        self.verify_authority()
        guarded = build_remote_home_guarded_command(
            self._remote_home_authority,
            remote_command,
        )
        return self._issue(self._session.command_argv(guarded))

    def rsync_argv(
        self,
        *,
        local_path: Path,
        remote_path: str,
        arguments: tuple[str, ...],
        remote_rsync_path: str | None,
    ) -> list[str]:
        allowed = {
            "--archive",
            "--delete",
            "--recursive",
            "--inplace",
            "--chmod=F600,D700",
            "--no-owner",
            "--no-group",
        }
        if (
            type(arguments) is not tuple
            or not arguments
            or any(
                argument not in allowed
                and re.fullmatch(r"--max-size=[1-9][0-9]{0,19}", argument) is None
                and re.fullmatch(r"--filter=protect /[A-Za-z0-9._-]{1,128}", argument) is None
                for argument in arguments
            )
            or (
                remote_rsync_path is not None
                and (
                    type(remote_rsync_path) is not str
                    or not remote_rsync_path
                    or len(remote_rsync_path.encode("utf-8")) > 65_536
                    or "\x00" in remote_rsync_path
                    or any(
                        ord(character) < 0x20 or ord(character) == 0x7F
                        for character in remote_rsync_path
                    )
                )
            )
        ):
            raise ValueError("system OpenSSH rsync request is invalid")
        base = self._session.upload_argv(
            local_path=local_path,
            remote_path=remote_path,
            delete=False,
        )
        shell_index = base.index("-e")
        argv = [base[0], *arguments]
        argv.extend(
            (
                "--rsync-path",
                build_remote_home_guarded_rsync_path(
                    self._remote_home_authority,
                    remote_rsync_path or RSYNC_EXECUTABLE,
                ),
            )
        )
        argv.extend(("-e", base[shell_index + 1], base[-2], base[-1]))
        return self._issue(argv)

    def core_tunnel_argv(self, *, remote_port: int) -> list[str]:
        return self._issue(self._session.core_tunnel_argv(remote_port=remote_port))

    def run_argv(
        self,
        argv: list[str],
        timeout_seconds: float,
        *,
        stdin_fd: int | None,
        cancel_event: threading.Event | None,
        stdout_source: LifecycleLogSourceV2 | None = "ssh_stdout",
        stderr_source: LifecycleLogSourceV2 | None = "ssh_stderr",
    ) -> subprocess.CompletedProcess[str]:
        self._consume(argv)
        self.verify_authority()
        completed = _run_verified_follower_subprocess(
            argv,
            self._session.follower_environment(),
            timeout_seconds,
            stdin_fd=stdin_fd,
            cancel_event=cancel_event,
            output_observer=self._session._follower_output_observer(),
            stdout_source=stdout_source,
            stderr_source=stderr_source,
        )
        self.verify_authority()
        return completed

    def start_tunnel(self, argv: list[str], stream_fd: int) -> _FollowerTunnelProcess:
        self._consume(argv)
        self.verify_authority()
        return _FollowerTunnelProcess.start(
            argv,
            stream_fd=stream_fd,
            environment=self._session.follower_environment(),
        )

    def _matches(self, snapshot: SystemOpenSshSessionSnapshot) -> bool:
        try:
            return self._remote_home_authority.matches(
                profile_id=snapshot.profile_id,
                connection_generation=snapshot.connection_generation,
                remote_user=self._remote_home_authority.remote_user,
            )
        except Exception:
            return False

    def __repr__(self) -> str:
        return "SystemOpenSshFollowerTransportAuthority(<sealed>)"

    def _issue(self, argv: list[str]) -> list[str]:
        self.verify_authority()
        identity = tuple(argv)
        with self._guard:
            if sum(self._issued.values()) >= self._MAX_ISSUED:
                raise SystemOpenSshSessionError(
                    "ssh_follower_capacity_full",
                    "System SSH follower capacity is full.",
                )
            self._issued[identity] = self._issued.get(identity, 0) + 1
        return list(argv)

    def _consume(self, argv: list[str]) -> None:
        identity = tuple(argv)
        with self._guard:
            count = self._issued.get(identity, 0)
            if count <= 0:
                raise SystemOpenSshSessionError(
                    "ssh_follower_authority_invalid",
                    "System SSH follower authority is invalid.",
                )
            if count == 1:
                self._issued.pop(identity)
            else:
                self._issued[identity] = count - 1


class _FollowerTunnelProcess:
    def __init__(self, process: subprocess.Popen[bytes]) -> None:
        self._process = process

    @classmethod
    def start(
        cls,
        argv: list[str],
        *,
        stream_fd: int,
        environment: dict[str, str],
    ) -> _FollowerTunnelProcess:
        if not argv or argv[0] != SSH_EXECUTABLE:
            raise ValueError("system OpenSSH tunnel executable is invalid")
        executable = VerifiedSystemExecutable.open(SSH_EXECUTABLE)
        try:
            executable.verify_path_binding()
            process = subprocess.Popen(
                argv,
                executable=executable.execution_path,
                stdin=stream_fd,
                stdout=stream_fd,
                stderr=subprocess.DEVNULL,
                env=environment,
                close_fds=True,
                pass_fds=(executable.descriptor,),
                start_new_session=True,
            )
            executable.verify_path_binding()
            return cls(process)
        finally:
            executable.close()

    def poll(self) -> int | None:
        return self._process.poll()

    def wait(self, timeout: float | None = None) -> int:
        return self._process.wait(timeout=timeout)

    def terminate(self) -> None:
        _signal_follower_group(self._process, signal.SIGTERM)

    def kill(self) -> None:
        _signal_follower_group(self._process, signal.SIGKILL)


def _run_verified_follower_subprocess(
    argv: list[str],
    environment: dict[str, str],
    timeout_seconds: float,
    *,
    stdin_fd: int | None,
    cancel_event: threading.Event | None,
    output_observer: LifecycleRawOutputObserverV2 | None = None,
    stdout_source: LifecycleLogSourceV2 | None = "ssh_stdout",
    stderr_source: LifecycleLogSourceV2 | None = "ssh_stderr",
) -> subprocess.CompletedProcess[str]:
    if (
        not argv
        or argv[0] not in {SSH_EXECUTABLE, RSYNC_EXECUTABLE}
        or not 0 < timeout_seconds <= 3600
        or (stdin_fd is not None and (type(stdin_fd) is not int or stdin_fd < 0))
        or (cancel_event is not None and not isinstance(cancel_event, threading.Event))
        or stdout_source not in {None, "ssh_stdout", "daemon_stdout"}
        or stderr_source not in {None, "ssh_stderr", "daemon_stderr"}
    ):
        raise ValueError("system OpenSSH follower subprocess request is invalid")
    if cancel_event is not None and cancel_event.is_set():
        raise SystemOpenSshSessionError(
            "ssh_connection_cancelled",
            "System SSH follower was cancelled.",
        )
    executable = VerifiedSystemExecutable.open(argv[0])
    nested: VerifiedSystemExecutable | None = None
    spawn_argv = list(argv)
    try:
        if argv[0] == RSYNC_EXECUTABLE:
            try:
                shell_index = spawn_argv.index("-e") + 1
                shell = shlex.split(spawn_argv[shell_index])
            except (ValueError, IndexError) as exc:
                raise ValueError("system OpenSSH rsync shell is invalid") from exc
            if not shell or shell[0] != SSH_EXECUTABLE:
                raise ValueError("system OpenSSH rsync shell is invalid")
            nested = VerifiedSystemExecutable.open(SSH_EXECUTABLE)
            nested.verify_path_binding()
            shell[0] = nested.execution_path
            spawn_argv[shell_index] = shlex.join(shell)
        executable.verify_path_binding()
        pass_fds = [executable.descriptor]
        if nested is not None:
            pass_fds.append(nested.descriptor)
        process = subprocess.Popen(
            spawn_argv,
            executable=executable.execution_path,
            stdin=subprocess.DEVNULL if stdin_fd is None else stdin_fd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=environment,
            close_fds=True,
            pass_fds=tuple(pass_fds),
            start_new_session=True,
        )
        executable.verify_path_binding()
        if nested is not None:
            nested.verify_path_binding()
        completed = _collect_follower_process(
            process,
            argv,
            timeout_seconds,
            cancel_event=cancel_event,
            output_observer=output_observer,
            stdout_source=stdout_source,
            stderr_source=stderr_source,
        )
        encoding = locale.getpreferredencoding(False)
        return subprocess.CompletedProcess(
            argv,
            completed.returncode,
            stdout=(completed.stdout or b"").decode(encoding),
            stderr=(completed.stderr or b"").decode(encoding),
        )
    finally:
        if nested is not None:
            nested.close()
        executable.close()


def _collect_follower_process(
    process: subprocess.Popen[bytes],
    argv: list[str],
    timeout_seconds: float,
    *,
    cancel_event: threading.Event | None,
    output_observer: LifecycleRawOutputObserverV2 | None = None,
    stdout_source: LifecycleLogSourceV2 | None = "ssh_stdout",
    stderr_source: LifecycleLogSourceV2 | None = "ssh_stderr",
) -> subprocess.CompletedProcess[bytes]:
    assert process.stdout is not None and process.stderr is not None
    deadline = time.monotonic() + timeout_seconds
    selector = selectors.DefaultSelector()
    selector.register(process.stdout, selectors.EVENT_READ, "stdout")
    selector.register(process.stderr, selectors.EVENT_READ, "stderr")
    chunks: dict[str, list[bytes]] = {"stdout": [], "stderr": []}
    captured = 0
    try:
        while selector.get_map():
            if cancel_event is not None and cancel_event.is_set():
                raise SystemOpenSshSessionError(
                    "ssh_connection_cancelled",
                    "System SSH follower was cancelled.",
                )
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
                        "ssh_output_limit_exceeded",
                        "System SSH follower output exceeded its limit.",
                    )
                source = stdout_source if key.data == "stdout" else stderr_source
                if source is not None:
                    _notify_output_observer(output_observer, source, chunk)
                chunks[key.data].append(chunk)
        process.wait(timeout=max(0.001, deadline - time.monotonic()))
    except BaseException:
        if process.poll() is None:
            _signal_follower_group(process, signal.SIGTERM)
            try:
                process.wait(timeout=0.5)
            except subprocess.TimeoutExpired:
                _signal_follower_group(process, signal.SIGKILL)
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


def _signal_follower_group(
    process: subprocess.Popen[bytes],
    signal_number: signal.Signals,
) -> None:
    if process.poll() is not None:
        return
    try:
        os.killpg(process.pid, signal_number)
    except ProcessLookupError:
        return


def _notify_output_observer(
    observer: LifecycleRawOutputObserverV2 | None,
    source: LifecycleLogSourceV2,
    chunk: bytes,
) -> None:
    if observer is None or not chunk:
        return
    try:
        observer(source, chunk)
    except Exception:
        # Process-output observation is diagnostic and cannot change child
        # success, failure, cancellation, or timeout authority.
        pass


def _run_bounded_subprocess(
    argv: list[str],
    environment: dict[str, str],
    timeout_seconds: float,
    *,
    output_observer: LifecycleRawOutputObserverV2 | None = None,
    max_capture_bytes: int = _MAX_CAPTURE_BYTES,
) -> subprocess.CompletedProcess[bytes]:
    if not 0 < timeout_seconds <= 3600:
        raise ValueError("SSH subprocess timeout is invalid")
    _validate_capture_limit(max_capture_bytes)
    process = subprocess.Popen(
        argv,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=environment,
        close_fds=True,
        start_new_session=False,
    )
    return _collect_bounded_process(
        process,
        argv,
        timeout_seconds,
        output_observer=output_observer,
        max_capture_bytes=max_capture_bytes,
    )


def _run_verified_bounded_subprocess(
    argv: list[str],
    environment: dict[str, str],
    timeout_seconds: float,
    *,
    output_observer: LifecycleRawOutputObserverV2 | None = None,
    max_capture_bytes: int = _MAX_CAPTURE_BYTES,
) -> subprocess.CompletedProcess[bytes]:
    if not argv:
        raise ValueError("verified SSH subprocess argv is empty")
    _validate_capture_limit(max_capture_bytes)
    executable = VerifiedSystemExecutable.open(argv[0])
    try:
        executable.verify_path_binding()
        process = subprocess.Popen(
            argv,
            executable=executable.execution_path,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=environment,
            close_fds=True,
            pass_fds=(executable.descriptor,),
            start_new_session=False,
        )
        executable.verify_path_binding()
        return _collect_bounded_process(
            process,
            argv,
            timeout_seconds,
            output_observer=output_observer,
            max_capture_bytes=max_capture_bytes,
        )
    finally:
        executable.close()


def _collect_bounded_process(
    process: subprocess.Popen[bytes],
    argv: list[str],
    timeout_seconds: float,
    *,
    output_observer: LifecycleRawOutputObserverV2 | None = None,
    max_capture_bytes: int = _MAX_CAPTURE_BYTES,
) -> subprocess.CompletedProcess[bytes]:
    if not 0 < timeout_seconds <= 3600:
        raise ValueError("SSH subprocess timeout is invalid")
    _validate_capture_limit(max_capture_bytes)
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
                    min(_CAPTURE_CHUNK_BYTES, max_capture_bytes - captured + 1),
                )
                if not chunk:
                    selector.unregister(key.fileobj)
                    continue
                captured += len(chunk)
                if captured > max_capture_bytes:
                    raise _session_error(
                        "ssh_output_limit_exceeded",
                        "SSH command output exceeded its limit.",
                    )
                _notify_output_observer(
                    output_observer,
                    "ssh_stdout" if key.data == "stdout" else "ssh_stderr",
                    chunk,
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


def _validate_capture_limit(value: int) -> None:
    if type(value) is not int or not 1 <= value <= _MAX_CAPTURE_BYTES:
        raise ValueError("SSH subprocess capture limit is invalid")


def _captured_master_stderr(process: OwnedSshMasterProcess) -> bytes:
    capture = getattr(process, "captured_stderr", None)
    if not callable(capture):
        return b""
    try:
        value = capture()
    except BaseException:
        return b""
    if type(value) is not bytes or len(value) > _MAX_MASTER_DIAGNOSTIC_BYTES:
        return b""
    return value


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


def _session_error(
    code: str,
    message: str,
    *,
    host_key_evidence: SystemHostKeyFailureEvidence | None = None,
    host_key_review: PendingSystemHostKeyReview | None = None,
) -> SystemOpenSshSessionError:
    return SystemOpenSshSessionError(
        code,
        message,
        host_key_evidence=host_key_evidence,
        host_key_review=host_key_review,
    )


__all__ = (
    "AskpassHelperAuthority",
    "OwnedSshMasterProcess",
    "SshMasterLauncher",
    "SystemOpenSshHostTrust",
    "SystemOpenSshFollowerTransportAuthority",
    "SystemOpenSshSession",
    "SystemOpenSshSessionError",
    "SystemOpenSshSessionOwner",
    "SystemOpenSshSessionSnapshot",
    "SystemOpenSshTrustFailure",
)
