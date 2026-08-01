#!/usr/bin/env python3
"""Gate OpenEvo's production system-OpenSSH boundary against a local sshd."""

from __future__ import annotations

import argparse
from dataclasses import dataclass, field
import hashlib
import json
import os
from pathlib import Path
import pwd
import secrets
import shlex
import shutil
import socket
import stat
import subprocess
import sys
import tempfile
import threading
import time
from typing import Literal


_REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
if str(_REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPOSITORY_ROOT))

from desktop.sidecar.askpass_broker import (  # noqa: E402
    AskpassAuthorizationBroker,
    AskpassPromptObservation,
    ProcessIdentity,
    SystemProcessInspector,
)
from desktop.sidecar.system_ssh_session import (  # noqa: E402
    AskpassHelperAuthority,
    OwnedSshMasterProcess,
    SystemOpenSshHostTrust,
    SystemOpenSshSessionError,
    _SystemSshMasterLauncher,
    _run_verified_bounded_subprocess,
    _run_verified_follower_subprocess,
)
from openevo.deployment.host_keys import (  # noqa: E402
    SystemHostKeyFailureCode,
    classify_system_openssh_host_key_failure,
)
from openevo.deployment.profile import SystemOpenSshAliasProfile  # noqa: E402
from openevo.deployment.ssh import (  # noqa: E402
    SystemOpenSshAskpassEnvironment,
    build_system_openssh_command_argv,
    build_system_openssh_control_argv,
    build_system_openssh_core_tunnel_argv,
    build_system_openssh_environment,
    build_system_openssh_master_argv,
)
from openevo.deployment.system_executables import (  # noqa: E402
    MACOS_SYSTEM_COMMAND_PATH,
    SSH_EXECUTABLE,
    SSH_KEYGEN_EXECUTABLE,
    VerifiedSystemExecutable,
)


SSHD_EXECUTABLE = "/usr/sbin/sshd"
SSH_AGENT_EXECUTABLE = "/usr/bin/ssh-agent"
SSH_ADD_EXECUTABLE = "/usr/bin/ssh-add"
CLANG_EXECUTABLE = "/usr/bin/clang"
REMOTE_COMMAND = "printf 'openevo-system-ssh-command-v1\\n'"
STREAMING_TRANSFER_PAYLOAD = b"openevo-system-ssh-stream-v1\n"
_ROOT_PREFIX = ".oe-ssh-integration-"
_MAX_OUTPUT_BYTES = 256 * 1024
_START_TIMEOUT_SECONDS = 10.0
_PROCESS_TIMEOUT_SECONDS = 15.0


class IntegrationError(RuntimeError):
    """One closed integration-gate failure code."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


@dataclass(frozen=True, slots=True)
class FixturePaths:
    root: Path
    ssh_directory: Path
    client_config: Path
    authorized_keys: Path
    known_hosts: Path
    first_known_hosts: Path
    cancel_known_hosts: Path
    forbidden_known_hosts: Path
    direct_host_key: Path
    jump_host_key: Path
    agent_key: Path
    identity_key: Path
    encrypted_key: Path
    ambient_key: Path
    direct_pid: Path
    jump_pid: Path
    direct_sshd_config: Path
    jump_sshd_config: Path
    controlled_agent_socket: Path
    ambient_agent_socket: Path
    ambient_control: Path
    ambient_control_pattern: Path
    proxy_command: Path
    askpass_source: Path
    askpass_helper: Path
    askpass_responses: Path
    askpass_events: Path
    stream_source: Path
    stream_target: Path

    @classmethod
    def for_root(cls, root: Path | str) -> FixturePaths:
        root_path = Path(root)
        ssh_directory = root_path / "ssh"
        return cls(
            root=root_path,
            ssh_directory=ssh_directory,
            client_config=ssh_directory / "config",
            authorized_keys=ssh_directory / "authorized_keys",
            known_hosts=ssh_directory / "known_hosts",
            first_known_hosts=ssh_directory / "known_hosts_first",
            cancel_known_hosts=ssh_directory / "known_hosts_cancel",
            forbidden_known_hosts=ssh_directory / "known_hosts_forbidden",
            direct_host_key=ssh_directory / "host_direct",
            jump_host_key=ssh_directory / "host_jump",
            agent_key=ssh_directory / "id_agent",
            identity_key=ssh_directory / "id_identity",
            encrypted_key=ssh_directory / "id_encrypted",
            ambient_key=ssh_directory / "id_ambient",
            direct_pid=root_path / "sshd-direct.pid",
            jump_pid=root_path / "sshd-jump.pid",
            direct_sshd_config=root_path / "sshd-direct.conf",
            jump_sshd_config=root_path / "sshd-jump.conf",
            controlled_agent_socket=root_path / "agent-controlled.sock",
            ambient_agent_socket=root_path / "agent-ambient.sock",
            ambient_control=root_path / "ambient-master.sock",
            ambient_control_pattern=root_path / "ambient-%C.sock",
            proxy_command=root_path / "proxy-command",
            askpass_source=root_path / "askpass-fixture.c",
            askpass_helper=root_path / "askpass-fixture",
            askpass_responses=root_path / "askpass-responses",
            askpass_events=root_path / "askpass-events",
            stream_source=root_path / "stream-source",
            stream_target=root_path / "stream-target",
        )


@dataclass(frozen=True, slots=True)
class FixtureTopology:
    direct_port: int
    jump_port: int
    core_port: int
    user: str


@dataclass(frozen=True, slots=True)
class ProductionSshPlan:
    master: list[str]
    command: list[str]
    stream: list[str]
    tunnel: list[str]
    exit_master: list[str]


def build_production_plan(
    profile: SystemOpenSshAliasProfile,
    *,
    control_path: Path,
    remote_stream_path: str,
    core_port: int,
) -> ProductionSshPlan:
    """Build every operation through the shipped alias-only builders."""

    return ProductionSshPlan(
        master=build_system_openssh_master_argv(profile, control_path=control_path),
        command=build_system_openssh_command_argv(
            profile,
            control_path=control_path,
            remote_command=REMOTE_COMMAND,
        ),
        stream=build_system_openssh_command_argv(
            profile,
            control_path=control_path,
            remote_command=build_stream_receive_command(remote_stream_path),
        ),
        tunnel=build_system_openssh_core_tunnel_argv(
            profile,
            control_path=control_path,
            remote_port=core_port,
        ),
        exit_master=build_system_openssh_control_argv(
            profile,
            control_path=control_path,
            operation="exit",
        ),
    )


def build_stream_receive_command(remote_path: str) -> str:
    if not isinstance(remote_path, str) or not remote_path.startswith("/"):
        raise IntegrationError("stream_target_invalid")
    return f"umask 077; /bin/cat > {shlex.quote(remote_path)}"


@dataclass(frozen=True, slots=True)
class IntegrationEvidence:
    status: Literal["ready", "passed"]
    platform: str
    checks: dict[str, bool] | list[str]
    password_result: Literal["authenticated", "prompt_rejected"] | None = None
    schema_version: int = 1

    @staticmethod
    def required_checks() -> list[str]:
        return [
            "alias_authority",
            "direct_agent",
            "identity_file",
            "encrypted_identity_askpass",
            "password_askpass",
            "proxy_jump",
            "proxy_command",
            "first_host_accept",
            "first_host_cancel",
            "strict_first_use",
            "changed_host_key",
            "repeated_changed_host_key",
            "command",
            "streaming_transfer",
            "core_tunnel",
            "master_reuse",
            "ambient_control_master_isolation",
            "ambient_credential_isolation",
            "cancellation",
            "cleanup",
        ]

    @classmethod
    def complete(
        cls,
        *,
        password_result: Literal["authenticated", "prompt_rejected"],
        platform: str = "darwin",
    ) -> IntegrationEvidence:
        return cls(
            status="passed",
            platform=platform,
            checks={key: True for key in cls.required_checks()},
            password_result=password_result,
        )

    def to_json(self) -> str:
        payload: dict[str, object] = {
            "checks": self.checks,
            "platform": self.platform,
            "schema_version": self.schema_version,
            "status": self.status,
        }
        if self.password_result is not None:
            payload["password_result"] = self.password_result
        return json.dumps(payload, ensure_ascii=True, sort_keys=True, separators=(",", ":"))


def _verified_tool(path: str) -> Path | None:
    candidate = Path(path)
    try:
        metadata = candidate.lstat()
    except OSError:
        return None
    if (
        not stat.S_ISREG(metadata.st_mode)
        or metadata.st_uid != 0
        or stat.S_IMODE(metadata.st_mode) & 0o022
        or not metadata.st_mode & stat.S_IXUSR
    ):
        return None
    return candidate


def verify_substrate(*, platform_name: str | None = None) -> dict[str, Path]:
    platform_name = sys.platform if platform_name is None else platform_name
    if platform_name != "darwin":
        raise IntegrationError("unsupported_platform")
    required = (
        SSHD_EXECUTABLE,
        SSH_EXECUTABLE,
        SSH_KEYGEN_EXECUTABLE,
        SSH_AGENT_EXECUTABLE,
        SSH_ADD_EXECUTABLE,
        CLANG_EXECUTABLE,
    )
    observed: dict[str, Path] = {}
    for path in required:
        verified = _verified_tool(path)
        if verified is None:
            raise IntegrationError("substrate_unavailable")
        observed[path] = verified
    return observed


def render_sshd_config(
    paths: FixturePaths,
    topology: FixtureTopology,
    *,
    port: int,
    host_key: Path,
    pid_file: Path,
) -> bytes:
    _require_port(port)
    lines = (
        f"Port {port}",
        "ListenAddress 127.0.0.1",
        "AddressFamily inet",
        f"HostKey {host_key}",
        f"PidFile {pid_file}",
        f"AuthorizedKeysFile {paths.authorized_keys}",
        "PubkeyAuthentication yes",
        "PasswordAuthentication yes",
        "KbdInteractiveAuthentication no",
        "UsePAM no",
        "PermitRootLogin no",
        f"AllowUsers {topology.user}",
        "StrictModes no",
        "UseDNS no",
        "LogLevel ERROR",
        "PrintMotd no",
        "PermitUserEnvironment no",
        "AllowAgentForwarding no",
        "AllowTcpForwarding yes",
        "GatewayPorts no",
        "X11Forwarding no",
        "PermitTunnel no",
        "PermitTTY no",
        "Subsystem sftp internal-sftp",
        "",
    )
    return "\n".join(lines).encode("utf-8")


def _host_block(
    alias: str,
    topology: FixtureTopology,
    paths: FixturePaths,
    *,
    identity_file: Path | None,
    identity_agent: Path | Literal["none"],
    known_hosts: Path,
    strict_host_key_checking: Literal["yes", "ask"],
    port: int | None = None,
    extra: tuple[str, ...] = (),
) -> list[str]:
    effective_port = topology.direct_port if port is None else port
    lines = [
        f"Host {alias}",
        "  HostName 127.0.0.1",
        f"  Port {effective_port}",
        f"  User {topology.user}",
        f"  IdentityAgent {identity_agent}",
        "  IdentitiesOnly yes",
        f"  UserKnownHostsFile {known_hosts}",
        "  GlobalKnownHostsFile none",
        "  HashKnownHosts no",
        f"  StrictHostKeyChecking {strict_host_key_checking}",
    ]
    if identity_file is not None:
        lines.append(f"  IdentityFile {identity_file}")
    lines.extend(f"  {value}" for value in extra)
    return lines


def render_ssh_config(paths: FixturePaths, topology: FixtureTopology) -> bytes:
    blocks: list[list[str]] = [
        _host_block(
            "direct-agent",
            topology,
            paths,
            identity_file=paths.agent_key.with_suffix(".pub"),
            identity_agent=paths.controlled_agent_socket,
            known_hosts=paths.known_hosts,
            strict_host_key_checking="yes",
        ),
        _host_block(
            "identity-file",
            topology,
            paths,
            identity_file=paths.identity_key,
            identity_agent="none",
            known_hosts=paths.known_hosts,
            strict_host_key_checking="yes",
        ),
        _host_block(
            "encrypted-identity",
            topology,
            paths,
            identity_file=paths.encrypted_key,
            identity_agent="none",
            known_hosts=paths.known_hosts,
            strict_host_key_checking="yes",
        ),
        _host_block(
            "fixture-jump",
            topology,
            paths,
            identity_file=paths.identity_key,
            identity_agent="none",
            known_hosts=paths.known_hosts,
            strict_host_key_checking="yes",
            port=topology.jump_port,
            extra=("ControlMaster no", "ControlPath none", "ControlPersist no"),
        ),
        _host_block(
            "proxy-jump",
            topology,
            paths,
            identity_file=paths.identity_key,
            identity_agent="none",
            known_hosts=paths.known_hosts,
            strict_host_key_checking="yes",
            extra=("ProxyJump fixture-jump",),
        ),
        _host_block(
            "proxy-command",
            topology,
            paths,
            identity_file=paths.identity_key,
            identity_agent="none",
            known_hosts=paths.known_hosts,
            strict_host_key_checking="yes",
            extra=(f"ProxyCommand {paths.proxy_command} %h %p",),
        ),
        _host_block(
            "first-accept",
            topology,
            paths,
            identity_file=paths.identity_key,
            identity_agent="none",
            known_hosts=paths.first_known_hosts,
            strict_host_key_checking="ask",
        ),
        _host_block(
            "first-cancel",
            topology,
            paths,
            identity_file=paths.identity_key,
            identity_agent="none",
            known_hosts=paths.cancel_known_hosts,
            strict_host_key_checking="ask",
        ),
        _host_block(
            "strict-first-use",
            topology,
            paths,
            identity_file=paths.identity_key,
            identity_agent="none",
            known_hosts=paths.forbidden_known_hosts,
            strict_host_key_checking="yes",
        ),
        _host_block(
            "password-only",
            topology,
            paths,
            identity_file=None,
            identity_agent="none",
            known_hosts=paths.known_hosts,
            strict_host_key_checking="yes",
            extra=(
                "PubkeyAuthentication no",
                "PreferredAuthentications password",
                "PasswordAuthentication yes",
                "NumberOfPasswordPrompts 1",
            ),
        ),
    ]
    lines: list[str] = []
    for block in blocks:
        lines.extend(block)
        lines.append("")
    lines.extend(
        (
            "Host *",
            "  BatchMode no",
            "  ConnectTimeout 5",
            "  ConnectionAttempts 1",
            "  LogLevel ERROR",
            "  ForwardAgent no",
            "  PermitLocalCommand no",
            "  RequestTTY no",
            "  ControlMaster auto",
            f"  ControlPath {paths.ambient_control_pattern}",
            "  ControlPersist 60",
            "",
        )
    )
    return "\n".join(lines).encode("utf-8")


@dataclass(frozen=True, slots=True)
class _FixtureSecrets:
    passphrase: str = field(repr=False)
    password: str = field(repr=False)


@dataclass(frozen=True, slots=True)
class _FixtureResult:
    checks: dict[str, bool]
    password_result: Literal["authenticated", "prompt_rejected"]


@dataclass(frozen=True, slots=True)
class _ConnectionFailure(Exception):
    code: str
    stderr: bytes = field(repr=False)
    observation: AskpassPromptObservation | None = field(repr=False)

    def __str__(self) -> str:
        return self.code


class _OwnedProcess:
    def __init__(self, process: subprocess.Popen[bytes], *, code: str) -> None:
        self.process = process
        self.code = code

    def stop(self) -> None:
        if self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=3.0)
            except subprocess.TimeoutExpired:
                self.process.kill()
                try:
                    self.process.wait(timeout=3.0)
                except subprocess.TimeoutExpired as exc:
                    raise IntegrationError(self.code) from exc


class _Sshd(_OwnedProcess):
    @classmethod
    def start(cls, config: Path, port: int, pid_file: Path) -> _Sshd:
        _require_port(port)
        validation = _run_tool(
            [SSHD_EXECUTABLE, "-t", "-f", str(config)],
            timeout_seconds=5.0,
            check=False,
        )
        if validation.returncode != 0:
            raise IntegrationError("sshd_configuration_failed")
        process = subprocess.Popen(
            [SSHD_EXECUTABLE, "-D", "-e", "-f", str(config)],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            env={"PATH": MACOS_SYSTEM_COMMAND_PATH},
            close_fds=True,
            start_new_session=False,
        )
        owned = cls(process, code="sshd_cleanup_failed")
        try:
            _wait_sshd_pid_file(process, pid_file)
        except BaseException:
            owned.stop()
            raise
        return owned


class _Agent(_OwnedProcess):
    @classmethod
    def start(cls, socket_path: Path, key: Path) -> _Agent:
        process = subprocess.Popen(
            [SSH_AGENT_EXECUTABLE, "-D", "-a", str(socket_path)],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            env={"PATH": MACOS_SYSTEM_COMMAND_PATH},
            close_fds=True,
            start_new_session=False,
        )
        owned = cls(process, code="ssh_agent_cleanup_failed")
        try:
            _wait_unix_socket(process, socket_path)
            result = _run_tool(
                [SSH_ADD_EXECUTABLE, str(key)],
                environment={
                    "HOME": str(Path.home()),
                    "PATH": MACOS_SYSTEM_COMMAND_PATH,
                    "SSH_AUTH_SOCK": str(socket_path),
                },
                timeout_seconds=5.0,
                check=False,
            )
            if result.returncode != 0:
                raise IntegrationError("ssh_agent_key_load_failed")
        except BaseException:
            owned.stop()
            raise
        return owned


class _EchoServer:
    def __init__(self, port: int) -> None:
        self._listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._listener.bind(("127.0.0.1", port))
        self._listener.listen(4)
        self._listener.settimeout(0.2)
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    def _serve(self) -> None:
        while not self._stop.is_set():
            try:
                connection, _address = self._listener.accept()
            except socket.timeout:
                continue
            except OSError:
                return
            with connection:
                connection.settimeout(2.0)
                try:
                    payload = connection.recv(4096)
                    if payload:
                        connection.sendall(payload)
                except OSError:
                    pass

    def close(self) -> None:
        self._stop.set()
        self._listener.close()
        self._thread.join(2.0)
        if self._thread.is_alive():
            raise IntegrationError("core_fixture_cleanup_failed")


class _FixtureSession:
    def __init__(
        self,
        paths: FixturePaths,
        alias: str,
        *,
        generation: int,
        helper: AskpassHelperAuthority,
        inherited_environment: dict[str, str],
    ) -> None:
        self.paths = paths
        self.profile = SystemOpenSshAliasProfile(
            profile_id=f"fixture-{generation}",
            ssh_host_alias=alias,
        )
        self.generation = generation
        self.helper = helper
        self.inherited_environment = inherited_environment
        self.runtime = Path(tempfile.mkdtemp(prefix="s-", dir=paths.root))
        self.runtime.chmod(0o700)
        self.control_path = self.runtime / "m"
        self.broker = AskpassAuthorizationBroker(
            self.runtime / "a",
            helper_path=helper.path,
        )
        self.process: OwnedSshMasterProcess | None = None
        self.owner: ProcessIdentity | None = None
        self.environment: dict[str, str] | None = None
        self._closed = False

    @property
    def observation(self) -> AskpassPromptObservation | None:
        return self.broker.prompt_observation

    def start(self) -> None:
        self.broker.start()
        capability = self.broker.issue_capability(connection_generation=self.generation)
        self.helper.verify()
        self.environment = build_system_openssh_environment(
            home=str(Path.home()),
            inherited=self.inherited_environment,
            askpass=SystemOpenSshAskpassEnvironment(
                helper_path=str(self.helper.path),
                broker_socket=str(self.broker.socket_path),
                capability=capability.value,
                connection_generation=self.generation,
            ),
        )
        argv = _inject_fixture_config(
            build_system_openssh_master_argv(
                self.profile,
                control_path=self.control_path,
                connect_timeout_seconds=5,
                keepalive_interval_seconds=5,
                keepalive_count=1,
            ),
            self.paths.client_config,
        )
        try:
            self.process = _SystemSshMasterLauncher(self.helper).spawn(
                argv,
                environment=self.environment,
                control_path=self.control_path,
            )
            self.owner = _await_process_identity(self.process)
            self.broker.bind_owner(capability, self.owner)
            self._await_ready()
        except BaseException as exc:
            stderr = _captured_stderr(self.process)
            observation = self.observation
            self.close(suppress=True)
            if isinstance(exc, IntegrationError):
                code = exc.code
            elif isinstance(exc, SystemOpenSshSessionError):
                code = exc.code
            else:
                code = "ssh_connection_failed"
            raise _ConnectionFailure(code, stderr, observation) from None

    def _await_ready(self) -> None:
        assert self.process is not None and self.environment is not None
        deadline = time.monotonic() + _START_TIMEOUT_SECONDS
        while time.monotonic() < deadline:
            if self.process.poll() is not None:
                raise IntegrationError("ssh_master_exited")
            if self.control_path.exists():
                result = self.run_argv(
                    _inject_fixture_config(
                        build_system_openssh_control_argv(
                            self.profile,
                            control_path=self.control_path,
                            operation="check",
                        ),
                        self.paths.client_config,
                    ),
                    timeout_seconds=2.0,
                )
                if result.returncode == 0:
                    return
            time.sleep(0.02)
        raise IntegrationError("ssh_startup_timeout")

    def production_plan(self, core_port: int) -> ProductionSshPlan:
        return build_production_plan(
            self.profile,
            control_path=self.control_path,
            remote_stream_path=str(self.paths.stream_target),
            core_port=core_port,
        )

    def run_argv(
        self,
        argv: list[str],
        *,
        timeout_seconds: float = _PROCESS_TIMEOUT_SECONDS,
    ) -> subprocess.CompletedProcess[bytes]:
        if self.environment is None:
            raise IntegrationError("ssh_session_unavailable")
        return _run_verified_bounded_subprocess(
            argv,
            self.environment,
            timeout_seconds,
        )

    def run_command(self, core_port: int) -> subprocess.CompletedProcess[bytes]:
        plan = _bind_fixture_config(self.production_plan(core_port), self.paths.client_config)
        return self.run_argv(plan.command)

    def run_streaming_argv(
        self,
        argv: list[str],
        source: Path,
        *,
        timeout_seconds: float = _PROCESS_TIMEOUT_SECONDS,
    ) -> subprocess.CompletedProcess[str]:
        if self.environment is None:
            raise IntegrationError("ssh_session_unavailable")
        descriptor = os.open(source, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW)
        try:
            before = os.fstat(descriptor)
            if (
                not stat.S_ISREG(before.st_mode)
                or before.st_uid != os.geteuid()
                or before.st_nlink != 1
                or stat.S_IMODE(before.st_mode) != 0o600
            ):
                raise IntegrationError("stream_source_invalid")
            completed = _run_verified_follower_subprocess(
                argv,
                self.environment,
                timeout_seconds,
                stdin_fd=descriptor,
                cancel_event=None,
            )
            after = os.fstat(descriptor)
            if (
                after.st_dev,
                after.st_ino,
                after.st_size,
                after.st_mtime_ns,
            ) != (
                before.st_dev,
                before.st_ino,
                before.st_size,
                before.st_mtime_ns,
            ):
                raise IntegrationError("stream_source_changed")
            return completed
        finally:
            os.close(descriptor)

    def close(self, *, suppress: bool = False) -> None:
        if self._closed:
            return
        self._closed = True
        failure: BaseException | None = None
        process = self.process
        if process is not None and process.poll() is None and self.environment is not None:
            try:
                control = _inject_fixture_config(
                    build_system_openssh_control_argv(
                        self.profile,
                        control_path=self.control_path,
                        operation="exit",
                    ),
                    self.paths.client_config,
                )
                self.run_argv(control, timeout_seconds=2.0)
            except BaseException as exc:
                failure = exc
            if not _wait_process(process, 2.0):
                process.terminate()
                if not _wait_process(process, 2.0):
                    process.kill()
                    if not _wait_process(process, 2.0):
                        failure = IntegrationError("ssh_master_cleanup_failed")
        try:
            self.broker.close()
        except BaseException as exc:
            failure = failure or exc
        for path in (self.control_path,):
            try:
                metadata = path.lstat()
            except FileNotFoundError:
                continue
            if stat.S_ISSOCK(metadata.st_mode) and metadata.st_uid == os.geteuid():
                path.unlink()
            else:
                failure = failure or IntegrationError("ssh_socket_cleanup_failed")
        try:
            self.runtime.rmdir()
        except OSError as exc:
            failure = failure or exc
        if failure is not None and not suppress:
            raise IntegrationError("ssh_session_cleanup_failed") from failure


def _require_port(value: int) -> None:
    if type(value) is not int or not 1024 <= value <= 65535:
        raise ValueError("fixture port is invalid")


def _allocate_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


def _private_root_identity(path: Path) -> tuple[int, int, int, int]:
    metadata = path.lstat()
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or metadata.st_uid != os.geteuid()
        or stat.S_IMODE(metadata.st_mode) != 0o700
    ):
        raise IntegrationError("fixture_root_authority_changed")
    return metadata.st_dev, metadata.st_ino, metadata.st_mode, metadata.st_uid


def _remove_fixture_root(
    root: Path,
    *,
    home: Path,
    identity: tuple[int, int, int, int],
) -> None:
    if (
        root.parent != home
        or not root.name.startswith(_ROOT_PREFIX)
        or _private_root_identity(root) != identity
        or not getattr(shutil.rmtree, "avoids_symlink_attacks", False)
    ):
        raise IntegrationError("fixture_root_authority_changed")
    shutil.rmtree(root)
    if root.exists():
        raise IntegrationError("fixture_cleanup_failed")


def _write_private(path: Path, content: bytes, *, mode: int = 0o600) -> None:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW
    descriptor = os.open(path, flags, mode)
    try:
        offset = 0
        while offset < len(content):
            written = os.write(descriptor, content[offset:])
            if written <= 0:
                raise IntegrationError("fixture_write_failed")
            offset += written
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _replace_private(path: Path, content: bytes) -> None:
    temporary = path.with_name(f".{path.name}.{secrets.token_hex(8)}")
    _write_private(temporary, content)
    os.replace(temporary, path)


def _run_tool(
    argv: list[str],
    *,
    environment: dict[str, str] | None = None,
    timeout_seconds: float,
    check: bool,
) -> subprocess.CompletedProcess[bytes]:
    try:
        completed = subprocess.run(
            argv,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=environment,
            close_fds=True,
            timeout=timeout_seconds,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise IntegrationError("fixture_command_failed") from exc
    if len(completed.stdout) + len(completed.stderr) > _MAX_OUTPUT_BYTES:
        raise IntegrationError("fixture_output_limit_exceeded")
    if check and completed.returncode != 0:
        raise IntegrationError("fixture_command_failed")
    return completed


def _generate_key(path: Path, *, passphrase: str = "") -> None:
    result = _run_tool(
        [
            SSH_KEYGEN_EXECUTABLE,
            "-q",
            "-t",
            "ed25519",
            "-N",
            passphrase,
            "-C",
            "openevo-system-ssh-fixture",
            "-f",
            str(path),
        ],
        timeout_seconds=10.0,
        check=False,
    )
    if result.returncode != 0:
        raise IntegrationError("fixture_key_generation_failed")
    path.chmod(0o600)
    path.with_suffix(".pub").chmod(0o600)


def _public_key(path: Path) -> str:
    fields = path.with_suffix(".pub").read_text(encoding="ascii").strip().split()
    if len(fields) < 2 or fields[0] != "ssh-ed25519":
        raise IntegrationError("fixture_public_key_invalid")
    return f"{fields[0]} {fields[1]}"


def _known_host(port: int, host_key: Path) -> str:
    return f"[127.0.0.1]:{port} {_public_key(host_key)}"


def _render_proxy_command(paths: FixturePaths) -> bytes:
    config = shlex.quote(str(paths.client_config))
    return (
        "#!/bin/sh\n"
        "test \"$#\" -eq 2 || exit 126\n"
        f"exec {SSH_EXECUTABLE} -F {config} "
        "-o ControlMaster=no -o ControlPath=none -o ControlPersist=no "
        "-o ClearAllForwardings=yes -o PermitLocalCommand=no "
        "-o ForwardAgent=no -o RequestTTY=no -W \"$1:$2\" -T -- fixture-jump\n"
    ).encode("utf-8")


def _c_string(value: Path) -> str:
    return json.dumps(str(value), ensure_ascii=True)


def _askpass_fixture_source(paths: FixturePaths) -> bytes:
    source = r'''
#include <CommonCrypto/CommonDigest.h>
#include <errno.h>
#include <fcntl.h>
#include <limits.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <strings.h>
#include <sys/socket.h>
#include <sys/stat.h>
#include <sys/un.h>
#include <unistd.h>

#define RESPONSE_PATH __RESPONSE_PATH__
#define EVENT_PATH __EVENT_PATH__
#define SSH_PATH "/usr/bin/ssh"
#define SSH_OWNER_ARGUMENT "--openevo-system-ssh-owner-v1"

extern char **environ;

static int owner_environment_key_allowed(const char *entry) {
    static const char *allowed[] = {
        "DISPLAY", "HOME", "LANG", "LC_ALL", "LC_CTYPE", "LC_MESSAGES",
        "OPENEVO_SSH_ASKPASS_CAPABILITY", "OPENEVO_SSH_ASKPASS_SOCKET",
        "OPENEVO_SSH_CONNECTION_GENERATION", "PATH", "SSH_ASKPASS",
        "SSH_ASKPASS_REQUIRE", "SSH_AUTH_SOCK"
    };
    const char *separator = strchr(entry, '=');
    if (!separator || separator == entry) return 0;
    size_t key_bytes = (size_t)(separator - entry);
    for (size_t index = 0; index < sizeof(allowed) / sizeof(allowed[0]); ++index) {
        if (strlen(allowed[index]) == key_bytes &&
            strncmp(entry, allowed[index], key_bytes) == 0) return 1;
    }
    return 0;
}

static int owner_environment_is_closed(void) {
    static const char *required[] = {
        "DISPLAY", "HOME", "OPENEVO_SSH_ASKPASS_CAPABILITY",
        "OPENEVO_SSH_ASKPASS_SOCKET", "OPENEVO_SSH_CONNECTION_GENERATION",
        "PATH", "SSH_ASKPASS", "SSH_ASKPASS_REQUIRE"
    };
    if (getenv("OPENEVO_SSH_OWNER_PID")) return 0;
    for (char **entry = environ; *entry; ++entry)
        if (!owner_environment_key_allowed(*entry)) return 0;
    for (size_t index = 0; index < sizeof(required) / sizeof(required[0]); ++index)
        if (!getenv(required[index]) || !*getenv(required[index])) return 0;
    return strcmp(getenv("DISPLAY"), "openevo-ssh-askpass") == 0 &&
           strcmp(getenv("SSH_ASKPASS_REQUIRE"), "force") == 0;
}

static int same_system_executable(const struct stat *left, const struct stat *right) {
    return S_ISREG(left->st_mode) && S_ISREG(right->st_mode) &&
           left->st_uid == 0 && right->st_uid == 0 &&
           left->st_nlink == 1 && right->st_nlink == 1 &&
           !(left->st_mode & 022) && !(right->st_mode & 022) &&
           (left->st_mode & 0111) && (right->st_mode & 0111) &&
           left->st_dev == right->st_dev && left->st_ino == right->st_ino &&
           left->st_mode == right->st_mode && left->st_size == right->st_size;
}

static int run_system_ssh_owner(int argc, char **argv) {
    if (argc < 5 || strcmp(argv[1], SSH_OWNER_ARGUMENT) != 0 ||
        strcmp(argv[3], SSH_PATH) != 0) return 126;
    errno = 0;
    char *descriptor_end = NULL;
    long descriptor_value = strtol(argv[2], &descriptor_end, 10);
    char canonical_descriptor[32];
    if (errno || !descriptor_end || *descriptor_end || descriptor_value < 3 ||
        descriptor_value > INT_MAX ||
        snprintf(canonical_descriptor, sizeof(canonical_descriptor), "%ld",
                 descriptor_value) <= 0 ||
        strcmp(canonical_descriptor, argv[2]) != 0) return 126;
    unsetenv("__CF_USER_TEXT_ENCODING");
    if (!owner_environment_is_closed()) return 126;
    int descriptor = (int)descriptor_value;
    struct stat held;
    struct stat current;
    if (fstat(descriptor, &held) != 0 || lstat(SSH_PATH, &current) != 0 ||
        !same_system_executable(&held, &current)) return 126;
    char owner_pid[32];
    if (snprintf(owner_pid, sizeof(owner_pid), "%d", getpid()) <= 0 ||
        setenv("OPENEVO_SSH_OWNER_PID", owner_pid, 1) != 0) return 126;
    int flags = fcntl(descriptor, F_GETFD);
    if (flags < 0 || fcntl(descriptor, F_SETFD, flags | FD_CLOEXEC) != 0 ||
        fstat(descriptor, &held) != 0 || lstat(SSH_PATH, &current) != 0 ||
        !same_system_executable(&held, &current)) return 126;
    execve(SSH_PATH, &argv[3], environ);
    return 126;
}

static void zero_memory(void *raw, size_t length) {
    volatile unsigned char *value = raw;
    while (length-- > 0) *value++ = 0;
}

static int write_all(int fd, const void *raw, size_t length) {
    const unsigned char *value = raw;
    while (length > 0) {
        ssize_t count = write(fd, value, length);
        if (count <= 0) return -1;
        value += count;
        length -= (size_t)count;
    }
    return 0;
}

static int read_value(const char *kind, char *output, size_t capacity) {
    int fd = open(RESPONSE_PATH, O_RDONLY | O_NOFOLLOW);
    if (fd < 0) return -1;
    struct stat metadata;
    if (fstat(fd, &metadata) != 0 || !S_ISREG(metadata.st_mode) ||
        metadata.st_uid != geteuid() || metadata.st_nlink != 1 ||
        (metadata.st_mode & 077) != 0 || metadata.st_size <= 0 ||
        metadata.st_size > 8192) {
        close(fd);
        return -1;
    }
    char buffer[8193];
    ssize_t count = read(fd, buffer, sizeof(buffer) - 1);
    close(fd);
    if (count <= 0 || count != metadata.st_size) return -1;
    buffer[count] = '\0';
    char prefix[64];
    int prefix_length = snprintf(prefix, sizeof(prefix), "%s=", kind);
    if (prefix_length <= 0 || (size_t)prefix_length >= sizeof(prefix)) return -1;
    char *line = buffer;
    while (line && *line) {
        char *end = strchr(line, '\n');
        if (end) *end = '\0';
        if (strncmp(line, prefix, (size_t)prefix_length) == 0) {
            const char *value = line + prefix_length;
            size_t length = strlen(value);
            if (length == 0 || length + 1 > capacity || strchr(value, '\r')) return -1;
            memcpy(output, value, length + 1);
            zero_memory(buffer, sizeof(buffer));
            return 0;
        }
        line = end ? end + 1 : NULL;
    }
    zero_memory(buffer, sizeof(buffer));
    return -1;
}

static int exchange(const char *event, const char *outcome, const char *kind,
                    const char *digest, size_t prompt_bytes) {
    const char *socket_path = getenv("OPENEVO_SSH_ASKPASS_SOCKET");
    const char *capability = getenv("OPENEVO_SSH_ASKPASS_CAPABILITY");
    const char *generation = getenv("OPENEVO_SSH_CONNECTION_GENERATION");
    const char *owner = getenv("OPENEVO_SSH_OWNER_PID");
    if (!socket_path || !capability || !generation || !owner ||
        strlen(socket_path) == 0 || strlen(socket_path) > 103 ||
        strlen(capability) != 64) return -1;
    struct sockaddr_un address;
    memset(&address, 0, sizeof(address));
    address.sun_family = AF_UNIX;
    memcpy(address.sun_path, socket_path, strlen(socket_path) + 1);
    int descriptor = socket(AF_UNIX, SOCK_STREAM, 0);
    if (descriptor < 0 || connect(descriptor, (struct sockaddr *)&address,
                                  sizeof(address)) != 0) {
        if (descriptor >= 0) close(descriptor);
        return -1;
    }
    char request[1024];
    int length;
    if (outcome) {
        length = snprintf(request, sizeof(request),
            "{\"schema_version\":1,\"event\":\"%s\",\"capability\":\"%s\","
            "\"connection_generation\":%s,\"helper_pid\":%d,"
            "\"ssh_parent_pid\":%d,\"owner_pid\":%s,\"prompt_kind\":\"%s\","
            "\"prompt_sha256\":\"%s\",\"prompt_bytes\":%zu,\"outcome\":\"%s\"}\n",
            event, capability, generation, getpid(), getppid(), owner, kind,
            digest, prompt_bytes, outcome);
    } else {
        length = snprintf(request, sizeof(request),
            "{\"schema_version\":1,\"event\":\"%s\",\"capability\":\"%s\","
            "\"connection_generation\":%s,\"helper_pid\":%d,"
            "\"ssh_parent_pid\":%d,\"owner_pid\":%s,\"prompt_kind\":\"%s\","
            "\"prompt_sha256\":\"%s\",\"prompt_bytes\":%zu}\n",
            event, capability, generation, getpid(), getppid(), owner, kind,
            digest, prompt_bytes);
    }
    if (length <= 0 || (size_t)length >= sizeof(request) ||
        write_all(descriptor, request, (size_t)length) != 0) {
        zero_memory(request, sizeof(request));
        close(descriptor);
        return -1;
    }
    zero_memory(request, sizeof(request));
    char response[512];
    size_t offset = 0;
    while (offset + 1 < sizeof(response)) {
        ssize_t count = read(descriptor, response + offset, 1);
        if (count != 1) break;
        if (response[offset++] == '\n') break;
    }
    close(descriptor);
    response[offset] = '\0';
    return strstr(response, "\"authorized\":true") ? 0 : -1;
}

static void record_event(const char *kind, const char *outcome) {
    int fd = open(EVENT_PATH, O_WRONLY | O_APPEND | O_NOFOLLOW);
    if (fd < 0) return;
    dprintf(fd, "%s %s\n", kind, outcome);
    close(fd);
}

int main(int argc, char **argv) {
    if (argc > 1 && strcmp(argv[1], SSH_OWNER_ARGUMENT) == 0)
        return run_system_ssh_owner(argc, argv);
    if (argc != 2) return 126;
    const char *prompt = argv[1];
    size_t prompt_bytes = strlen(prompt);
    if (prompt_bytes == 0 || prompt_bytes > 2048) return 126;
    char lowered[2049];
    for (size_t index = 0; index < prompt_bytes; ++index) {
        unsigned char value = (unsigned char)prompt[index];
        lowered[index] = value >= 'A' && value <= 'Z' ? (char)(value + 32) : (char)value;
    }
    lowered[prompt_bytes] = '\0';
    size_t trimmed_bytes = prompt_bytes;
    while (trimmed_bytes > 0 &&
           (lowered[trimmed_bytes - 1] == ' ' || lowered[trimmed_bytes - 1] == '\t' ||
            lowered[trimmed_bytes - 1] == '\r' || lowered[trimmed_bytes - 1] == '\n'))
        --trimmed_bytes;
    const char *kind = NULL;
    if (trimmed_bytes > 0 && strstr(lowered, "enter passphrase for key") &&
        lowered[trimmed_bytes - 1] == ':')
        kind = "passphrase";
    else if (trimmed_bytes > 0 && strstr(lowered, "password:") &&
             lowered[trimmed_bytes - 1] == ':')
        kind = "password";
    else if (strstr(lowered, "the authenticity of host '") &&
             strstr(lowered, "are you sure you want to continue connecting") &&
             strstr(lowered, "yes/no"))
        kind = "host_confirmation";
    if (!kind) return 126;
    unsigned char hash[CC_SHA256_DIGEST_LENGTH];
    CC_SHA256(prompt, (CC_LONG)prompt_bytes, hash);
    char digest[65];
    for (size_t index = 0; index < sizeof(hash); ++index)
        snprintf(digest + index * 2, 3, "%02x", hash[index]);
    if (exchange("authorize", NULL, kind, digest, prompt_bytes) != 0) return 126;
    char value[4097];
    if (read_value(kind, value, sizeof(value)) != 0) return 126;
    const char *outcome = "accepted";
    if (strcmp(value, "cancel") == 0) outcome = "cancelled";
    else if (strcmp(kind, "host_confirmation") == 0 && strcmp(value, "no") == 0)
        outcome = "rejected";
    if (exchange("complete", outcome, kind, digest, prompt_bytes) != 0) {
        zero_memory(value, sizeof(value));
        return 126;
    }
    record_event(kind, outcome);
    if (strcmp(outcome, "cancelled") == 0) {
        zero_memory(value, sizeof(value));
        return 1;
    }
    if (strcmp(kind, "host_confirmation") == 0) {
        const char *answer = strcmp(outcome, "accepted") == 0 ? "yes\n" : "no\n";
        if (write_all(STDOUT_FILENO, answer, strlen(answer)) != 0) return 126;
    } else {
        size_t length = strlen(value);
        if (write_all(STDOUT_FILENO, value, length) != 0 ||
            write_all(STDOUT_FILENO, "\n", 1) != 0) {
            zero_memory(value, sizeof(value));
            return 126;
        }
    }
    zero_memory(value, sizeof(value));
    return 0;
}
'''
    source = source.replace("__RESPONSE_PATH__", _c_string(paths.askpass_responses))
    source = source.replace("__EVENT_PATH__", _c_string(paths.askpass_events))
    return source.encode("utf-8")


def _compile_askpass_helper(paths: FixturePaths) -> AskpassHelperAuthority:
    _write_private(paths.askpass_source, _askpass_fixture_source(paths))
    result = _run_tool(
        [
            CLANG_EXECUTABLE,
            "-O2",
            "-Wall",
            "-Wextra",
            "-Werror",
            "-Wno-deprecated-declarations",
            "-o",
            str(paths.askpass_helper),
            str(paths.askpass_source),
        ],
        timeout_seconds=30.0,
        check=False,
    )
    if result.returncode != 0:
        raise IntegrationError("askpass_fixture_build_failed")
    paths.askpass_helper.chmod(0o755)
    digest = hashlib.sha256(paths.askpass_helper.read_bytes()).hexdigest()
    return AskpassHelperAuthority.open(
        paths.askpass_helper,
        expected_sha256=digest,
        expected_byte_size=paths.askpass_helper.stat().st_size,
    )


def _write_askpass_responses(
    paths: FixturePaths,
    secrets_value: _FixtureSecrets,
    *,
    host_confirmation: Literal["yes", "no", "cancel"],
) -> None:
    content = (
        f"password={secrets_value.password}\n"
        f"passphrase={secrets_value.passphrase}\n"
        f"host_confirmation={host_confirmation}\n"
    ).encode("ascii")
    if paths.askpass_responses.exists():
        _replace_private(paths.askpass_responses, content)
    else:
        _write_private(paths.askpass_responses, content)


def _prepare_fixture(paths: FixturePaths, topology: FixtureTopology) -> _FixtureSecrets:
    paths.ssh_directory.mkdir(mode=0o700)
    _write_private(paths.stream_source, STREAMING_TRANSFER_PAYLOAD)
    passphrase = secrets.token_hex(16)
    password = secrets.token_hex(16)
    fixture_secrets = _FixtureSecrets(passphrase=passphrase, password=password)
    for key in (
        paths.direct_host_key,
        paths.jump_host_key,
        paths.agent_key,
        paths.identity_key,
        paths.ambient_key,
    ):
        _generate_key(key)
    _generate_key(paths.encrypted_key, passphrase=passphrase)
    authorized = "\n".join(
        (
            _public_key(paths.agent_key),
            _public_key(paths.identity_key),
            _public_key(paths.encrypted_key),
            "",
        )
    ).encode("ascii")
    _write_private(paths.authorized_keys, authorized)
    known_hosts = "\n".join(
        (
            _known_host(topology.direct_port, paths.direct_host_key),
            _known_host(topology.jump_port, paths.jump_host_key),
            "",
        )
    ).encode("ascii")
    _write_private(paths.known_hosts, known_hosts)
    for path in (
        paths.first_known_hosts,
        paths.cancel_known_hosts,
        paths.forbidden_known_hosts,
        paths.askpass_events,
    ):
        _write_private(path, b"")
    _write_private(paths.proxy_command, _render_proxy_command(paths), mode=0o700)
    _write_private(paths.client_config, render_ssh_config(paths, topology))
    _write_private(
        paths.direct_sshd_config,
        render_sshd_config(
            paths,
            topology,
            port=topology.direct_port,
            host_key=paths.direct_host_key,
            pid_file=paths.direct_pid,
        ),
    )
    _write_private(
        paths.jump_sshd_config,
        render_sshd_config(
            paths,
            topology,
            port=topology.jump_port,
            host_key=paths.jump_host_key,
            pid_file=paths.jump_pid,
        ),
    )
    _write_askpass_responses(paths, fixture_secrets, host_confirmation="yes")
    return fixture_secrets


def _inject_fixture_config(argv: list[str], config: Path) -> list[str]:
    if not argv or argv[0] != SSH_EXECUTABLE:
        raise IntegrationError("fixture_ssh_argv_invalid")
    return [SSH_EXECUTABLE, "-F", str(config), *argv[1:]]


def _bind_fixture_config(plan: ProductionSshPlan, config: Path) -> ProductionSshPlan:
    return ProductionSshPlan(
        master=_inject_fixture_config(plan.master, config),
        command=_inject_fixture_config(plan.command, config),
        stream=_inject_fixture_config(plan.stream, config),
        tunnel=_inject_fixture_config(plan.tunnel, config),
        exit_master=_inject_fixture_config(plan.exit_master, config),
    )


def _await_process_identity(process: OwnedSshMasterProcess) -> ProcessIdentity:
    inspector = SystemProcessInspector()
    deadline = time.monotonic() + _START_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise IntegrationError("ssh_master_exited")
        identity = inspector.inspect(process.pid)
        if identity is not None and identity.executable_path == SSH_EXECUTABLE:
            return identity
        time.sleep(0.01)
    raise IntegrationError("ssh_owner_unavailable")


def _captured_stderr(process: OwnedSshMasterProcess | None) -> bytes:
    if process is None:
        return b""
    reader = getattr(process, "captured_stderr", None)
    if not callable(reader):
        return b""
    value = reader()
    return value if type(value) is bytes and len(value) <= _MAX_OUTPUT_BYTES else b""


def _wait_process(process: OwnedSshMasterProcess, timeout: float) -> bool:
    if process.poll() is not None:
        return True
    try:
        process.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        return False
    return True


def _wait_sshd_pid_file(process: subprocess.Popen[bytes], pid_file: Path) -> None:
    deadline = time.monotonic() + _START_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise IntegrationError("sshd_start_failed")
        try:
            metadata = pid_file.lstat()
            value = pid_file.read_text(encoding="ascii")
        except (FileNotFoundError, OSError, UnicodeError):
            time.sleep(0.02)
            continue
        if (
            stat.S_ISREG(metadata.st_mode)
            and metadata.st_uid == os.geteuid()
            and metadata.st_nlink == 1
            and value.strip() == str(process.pid)
        ):
            return
        raise IntegrationError("sshd_pid_authority_invalid")
    raise IntegrationError("sshd_start_timeout")


def _wait_unix_socket(process: subprocess.Popen[bytes], path: Path) -> None:
    deadline = time.monotonic() + _START_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise IntegrationError("ssh_agent_start_failed")
        try:
            metadata = path.lstat()
        except FileNotFoundError:
            time.sleep(0.02)
            continue
        if stat.S_ISSOCK(metadata.st_mode) and metadata.st_uid == os.geteuid():
            return
        raise IntegrationError("ssh_agent_socket_invalid")
    raise IntegrationError("ssh_agent_start_timeout")


def _start_ambient_master(
    paths: FixturePaths,
    environment: dict[str, str],
) -> subprocess.Popen[bytes]:
    process = subprocess.Popen(
        [
            SSH_EXECUTABLE,
            "-F",
            str(paths.client_config),
            "-M",
            "-S",
            str(paths.ambient_control),
            "-o",
            "ControlPersist=no",
            "-N",
            "-T",
            "--",
            "identity-file",
        ],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        env=environment,
        close_fds=True,
        start_new_session=False,
    )
    _wait_unix_socket(process, paths.ambient_control)
    return process


def _stop_ambient_master(
    paths: FixturePaths,
    environment: dict[str, str],
    process: subprocess.Popen[bytes],
) -> None:
    _run_tool(
        [
            SSH_EXECUTABLE,
            "-F",
            str(paths.client_config),
            "-S",
            str(paths.ambient_control),
            "-O",
            "exit",
            "--",
            "identity-file",
        ],
        environment=environment,
        timeout_seconds=3.0,
        check=False,
    )
    try:
        process.wait(timeout=3.0)
    except subprocess.TimeoutExpired:
        process.terminate()
        try:
            process.wait(timeout=2.0)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=2.0)


def _assert_command(session: _FixtureSession, core_port: int) -> None:
    completed = session.run_command(core_port)
    if completed.returncode != 0 or completed.stdout != b"openevo-system-ssh-command-v1\n":
        raise IntegrationError("ssh_command_failed")


def _assert_streaming_transfer(session: _FixtureSession, core_port: int) -> None:
    plan = _bind_fixture_config(session.production_plan(core_port), session.paths.client_config)
    completed = session.run_streaming_argv(plan.stream, session.paths.stream_source)
    target = session.paths.stream_target
    if (
        completed.returncode != 0
        or not target.is_file()
        or target.read_bytes() != STREAMING_TRANSFER_PAYLOAD
    ):
        raise IntegrationError("ssh_streaming_transfer_failed")


def _assert_tunnel(session: _FixtureSession, core_port: int) -> None:
    assert session.environment is not None
    plan = _bind_fixture_config(session.production_plan(core_port), session.paths.client_config)
    executable = VerifiedSystemExecutable.open(SSH_EXECUTABLE)
    try:
        executable.verify_path_binding()
        process = subprocess.Popen(
            plan.tunnel,
            executable=executable.execution_path,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=session.environment,
            close_fds=True,
            pass_fds=(executable.descriptor,),
            start_new_session=False,
        )
        executable.verify_path_binding()
        marker = b"openevo-core-tunnel-v1"
        try:
            stdout, stderr = process.communicate(input=marker, timeout=5.0)
        except subprocess.TimeoutExpired:
            process.kill()
            process.communicate()
            raise IntegrationError("ssh_core_tunnel_failed") from None
    finally:
        executable.close()
    if process.returncode != 0 or stdout != marker or len(stderr) > _MAX_OUTPUT_BYTES:
        raise IntegrationError("ssh_core_tunnel_failed")


def _connect(
    paths: FixturePaths,
    alias: str,
    *,
    generation: int,
    helper: AskpassHelperAuthority,
    inherited_environment: dict[str, str],
) -> _FixtureSession:
    session = _FixtureSession(
        paths,
        alias,
        generation=generation,
        helper=helper,
        inherited_environment=inherited_environment,
    )
    session.start()
    return session


def _rotate_direct_host_key(paths: FixturePaths) -> None:
    for path in (paths.direct_host_key, paths.direct_host_key.with_suffix(".pub")):
        metadata = path.lstat()
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != os.geteuid():
            raise IntegrationError("fixture_host_key_invalid")
        path.unlink()
    _generate_key(paths.direct_host_key)


def _probe_config_runner(paths: FixturePaths):
    def run(
        argv: list[str],
        environment: dict[str, str],
        timeout_seconds: float,
    ) -> subprocess.CompletedProcess[bytes]:
        effective = (
            _inject_fixture_config(argv, paths.client_config)
            if argv[:2] == [SSH_EXECUTABLE, "-G"]
            else argv
        )
        return _run_verified_bounded_subprocess(effective, environment, timeout_seconds)

    return run


def _assert_host_trust_flows(
    paths: FixturePaths,
    topology: FixtureTopology,
    *,
    helper: AskpassHelperAuthority,
    inherited_environment: dict[str, str],
    secrets_value: _FixtureSecrets,
    direct_server: _Sshd,
    generation: int,
) -> tuple[_Sshd, int, dict[str, bool]]:
    checks: dict[str, bool] = {}
    _write_askpass_responses(paths, secrets_value, host_confirmation="yes")
    first = _connect(
        paths,
        "first-accept",
        generation=generation,
        helper=helper,
        inherited_environment=inherited_environment,
    )
    generation += 1
    try:
        observation = first.observation
        checks["first_host_accept"] = bool(
            observation
            and observation.kind == "host_confirmation"
            and observation.state == "completed"
        )
    finally:
        first.close()

    _write_askpass_responses(paths, secrets_value, host_confirmation="cancel")
    try:
        _connect(
            paths,
            "first-cancel",
            generation=generation,
            helper=helper,
            inherited_environment=inherited_environment,
        )
    except _ConnectionFailure as failure:
        observation = failure.observation
        checks["first_host_cancel"] = bool(
            observation
            and observation.kind == "host_confirmation"
            and observation.state == "cancelled"
        )
    else:
        raise IntegrationError("first_host_cancel_not_enforced")
    generation += 1

    try:
        _connect(
            paths,
            "strict-first-use",
            generation=generation,
            helper=helper,
            inherited_environment=inherited_environment,
        )
    except _ConnectionFailure as failure:
        evidence = classify_system_openssh_host_key_failure(failure.stderr)
        checks["strict_first_use"] = (
            evidence.code is SystemHostKeyFailureCode.FIRST_USE_FORBIDDEN
        )
    else:
        raise IntegrationError("strict_first_use_not_enforced")
    generation += 1

    _write_askpass_responses(paths, secrets_value, host_confirmation="yes")
    direct_server.stop()
    _rotate_direct_host_key(paths)
    direct_server = _Sshd.start(
        paths.direct_sshd_config,
        topology.direct_port,
        paths.direct_pid,
    )
    try:
        _connect(
            paths,
            "first-accept",
            generation=generation,
            helper=helper,
            inherited_environment=inherited_environment,
        )
    except _ConnectionFailure as failure:
        changed_stderr = failure.stderr
        evidence = classify_system_openssh_host_key_failure(changed_stderr)
        if evidence.code is not SystemHostKeyFailureCode.CHANGED:
            raise IntegrationError("changed_host_key_not_detected")
    else:
        raise IntegrationError("changed_host_key_not_blocked")

    trust = SystemOpenSshHostTrust(
        home=paths.root.parent,
        inherited_environment=inherited_environment,
        runner=_probe_config_runner(paths),
    )
    failure = trust.evaluate_failure(
        SystemOpenSshAliasProfile(
            profile_id="fixture-host-review",
            ssh_host_alias="first-accept",
        ),
        connection_generation=generation,
        stderr=changed_stderr,
    )
    if failure.review is None or failure.review.repair_support != "automatic_replacement_available":
        raise IntegrationError("changed_host_key_review_unavailable")
    trust.replace_changed_key(
        failure.review,
        profile=SystemOpenSshAliasProfile(
            profile_id="fixture-host-review",
            ssh_host_alias="first-accept",
        ),
        connection_generation=generation,
        review_id=failure.review.review_id,
        review_sha256=failure.review.review_sha256,
    )
    checks["changed_host_key"] = True
    generation += 1

    accepted = _connect(
        paths,
        "first-accept",
        generation=generation,
        helper=helper,
        inherited_environment=inherited_environment,
    )
    accepted.close()
    generation += 1
    direct_server.stop()
    _rotate_direct_host_key(paths)
    direct_server = _Sshd.start(
        paths.direct_sshd_config,
        topology.direct_port,
        paths.direct_pid,
    )
    try:
        _connect(
            paths,
            "first-accept",
            generation=generation,
            helper=helper,
            inherited_environment=inherited_environment,
        )
    except _ConnectionFailure as repeated:
        repeated_evidence = classify_system_openssh_host_key_failure(repeated.stderr)
        checks["repeated_changed_host_key"] = (
            repeated_evidence.code is SystemHostKeyFailureCode.CHANGED
            and repeated_evidence.presented_fingerprints
            != failure.evidence.presented_fingerprints
        )
    else:
        raise IntegrationError("repeated_changed_host_key_not_blocked")
    trust.close()
    return direct_server, generation + 1, checks


def _run_fixture(paths: FixturePaths, topology: FixtureTopology) -> _FixtureResult:
    fixture_secrets = _prepare_fixture(paths, topology)
    helper = _compile_askpass_helper(paths)
    controlled_agent: _Agent | None = None
    ambient_agent: _Agent | None = None
    direct_server: _Sshd | None = None
    jump_server: _Sshd | None = None
    echo_server: _EchoServer | None = None
    ambient_master: subprocess.Popen[bytes] | None = None
    sessions: list[_FixtureSession] = []
    checks = {key: False for key in IntegrationEvidence.required_checks()}
    password_result: Literal["authenticated", "prompt_rejected"] = "prompt_rejected"
    generation = 1
    cleanup_failure: BaseException | None = None
    try:
        controlled_agent = _Agent.start(paths.controlled_agent_socket, paths.agent_key)
        agent_key_metadata = paths.agent_key.lstat()
        if (
            not stat.S_ISREG(agent_key_metadata.st_mode)
            or agent_key_metadata.st_uid != os.geteuid()
            or agent_key_metadata.st_nlink != 1
        ):
            raise IntegrationError("controlled_agent_key_invalid")
        paths.agent_key.unlink()
        ambient_agent = _Agent.start(paths.ambient_agent_socket, paths.ambient_key)
        inherited = {
            "LANG": "C",
            "SSH_AUTH_SOCK": str(paths.ambient_agent_socket),
        }
        direct_server = _Sshd.start(
            paths.direct_sshd_config,
            topology.direct_port,
            paths.direct_pid,
        )
        jump_server = _Sshd.start(
            paths.jump_sshd_config,
            topology.jump_port,
            paths.jump_pid,
        )
        echo_server = _EchoServer(topology.core_port)
        ambient_environment = build_system_openssh_environment(
            home=str(Path.home()),
            inherited=inherited,
        )
        ambient_master = _start_ambient_master(paths, ambient_environment)

        direct = _connect(
            paths,
            "direct-agent",
            generation=generation,
            helper=helper,
            inherited_environment=inherited,
        )
        generation += 1
        sessions.append(direct)
        checks["direct_agent"] = direct.observation is None
        checks["ambient_credential_isolation"] = direct.process is not None
        checks["ambient_control_master_isolation"] = bool(
            direct.process is not None
            and direct.process.pid != ambient_master.pid
            and direct.control_path != paths.ambient_control
            and ambient_master.poll() is None
        )
        owner_pid = direct.process.pid if direct.process is not None else -1
        _assert_command(direct, topology.core_port)
        checks["command"] = True
        _assert_streaming_transfer(direct, topology.core_port)
        checks["streaming_transfer"] = True
        _assert_tunnel(direct, topology.core_port)
        checks["core_tunnel"] = True
        _assert_command(direct, topology.core_port)
        checks["master_reuse"] = bool(
            direct.process is not None
            and direct.process.pid == owner_pid
            and direct.process.poll() is None
        )
        direct.close()
        sessions.remove(direct)

        identity = _connect(
            paths,
            "identity-file",
            generation=generation,
            helper=helper,
            inherited_environment=inherited,
        )
        generation += 1
        sessions.append(identity)
        _assert_command(identity, topology.core_port)
        checks["identity_file"] = identity.observation is None
        identity.close()
        sessions.remove(identity)

        encrypted = _connect(
            paths,
            "encrypted-identity",
            generation=generation,
            helper=helper,
            inherited_environment=inherited,
        )
        generation += 1
        sessions.append(encrypted)
        _assert_command(encrypted, topology.core_port)
        encrypted_observation = encrypted.observation
        checks["encrypted_identity_askpass"] = bool(
            encrypted_observation
            and encrypted_observation.kind == "passphrase"
            and encrypted_observation.state == "completed"
        )
        encrypted.close()
        sessions.remove(encrypted)

        for alias, check in (("proxy-jump", "proxy_jump"), ("proxy-command", "proxy_command")):
            proxied = _connect(
                paths,
                alias,
                generation=generation,
                helper=helper,
                inherited_environment=inherited,
            )
            generation += 1
            sessions.append(proxied)
            _assert_command(proxied, topology.core_port)
            checks[check] = proxied.observation is None
            proxied.close()
            sessions.remove(proxied)

        try:
            password_session = _connect(
                paths,
                "password-only",
                generation=generation,
                helper=helper,
                inherited_environment=inherited,
            )
        except _ConnectionFailure as password_failure:
            observation = password_failure.observation
            checks["password_askpass"] = bool(
                observation
                and observation.kind == "password"
                and observation.state == "completed"
            )
            password_result = "prompt_rejected"
        else:
            sessions.append(password_session)
            observation = password_session.observation
            checks["password_askpass"] = bool(
                observation
                and observation.kind == "password"
                and observation.state == "completed"
            )
            password_result = "authenticated"
            password_session.close()
            sessions.remove(password_session)
        generation += 1

        direct_server, generation, host_checks = _assert_host_trust_flows(
            paths,
            topology,
            helper=helper,
            inherited_environment=inherited,
            secrets_value=fixture_secrets,
            direct_server=direct_server,
            generation=generation,
        )
        checks.update(host_checks)

        cancelled = _connect(
            paths,
            "fixture-jump",
            generation=generation,
            helper=helper,
            inherited_environment=inherited,
        )
        sessions.append(cancelled)
        cancelled.close()
        sessions.remove(cancelled)
        checks["cancellation"] = bool(
            cancelled.process is not None and cancelled.process.poll() is not None
        )
        checks["alias_authority"] = True
    finally:
        for session in reversed(sessions):
            try:
                session.close()
            except BaseException as exc:
                cleanup_failure = cleanup_failure or exc
        if ambient_master is not None:
            try:
                _stop_ambient_master(paths, ambient_environment, ambient_master)
            except BaseException as exc:
                cleanup_failure = cleanup_failure or exc
        for resource in (echo_server, jump_server, direct_server, ambient_agent, controlled_agent):
            if resource is None:
                continue
            try:
                resource.close() if isinstance(resource, _EchoServer) else resource.stop()
            except BaseException as exc:
                cleanup_failure = cleanup_failure or exc
        try:
            helper.close()
        except BaseException as exc:
            cleanup_failure = cleanup_failure or exc
    if cleanup_failure is not None:
        raise IntegrationError("fixture_cleanup_failed") from cleanup_failure
    checks["cleanup"] = True
    if not all(checks.values()):
        raise IntegrationError("integration_check_incomplete")
    events = paths.askpass_events.read_text(encoding="ascii")
    if fixture_secrets.password in events or fixture_secrets.passphrase in events:
        raise IntegrationError("askpass_secret_retained")
    return _FixtureResult(checks=checks, password_result=password_result)


def run_integration() -> IntegrationEvidence:
    verify_substrate()
    account = pwd.getpwuid(os.geteuid())
    home = Path(account.pw_dir)
    if not home.is_absolute() or home != Path.home() or not home.is_dir():
        raise IntegrationError("home_authority_unavailable")
    root = Path(tempfile.mkdtemp(prefix=_ROOT_PREFIX, dir=home))
    root.chmod(0o700)
    root_identity = _private_root_identity(root)
    paths = FixturePaths.for_root(root)
    topology = FixtureTopology(
        direct_port=_allocate_port(),
        jump_port=_allocate_port(),
        core_port=_allocate_port(),
        user=account.pw_name,
    )
    if len({topology.direct_port, topology.jump_port, topology.core_port}) != 3:
        _remove_fixture_root(root, home=home, identity=root_identity)
        raise IntegrationError("fixture_port_collision")
    try:
        result = _run_fixture(paths, topology)
    finally:
        _remove_fixture_root(root, home=home, identity=root_identity)
    checks = dict(result.checks)
    checks["cleanup"] = True
    return IntegrationEvidence(
        status="passed",
        platform="darwin",
        checks=checks,
        password_result=result.password_result,
    )


def _write_evidence(path: Path, evidence: IntegrationEvidence) -> None:
    if not path.is_absolute() or path.exists():
        raise IntegrationError("evidence_path_invalid")
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    _write_private(path, (evidence.to_json() + "\n").encode("ascii"))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--structural-check", action="store_true")
    parser.add_argument("--require-complete", action="store_true")
    parser.add_argument("--evidence-out", type=Path)
    args = parser.parse_args(argv)
    if args.structural_check:
        if args.require_complete or args.evidence_out is not None:
            parser.error("--structural-check cannot be combined with runtime options")
        print(
            IntegrationEvidence(
                status="ready",
                platform="structural",
                checks=IntegrationEvidence.required_checks(),
            ).to_json()
        )
        return 0
    if not args.require_complete:
        parser.error("the real gate requires --require-complete")
    try:
        evidence = run_integration()
        if args.evidence_out is not None:
            _write_evidence(args.evidence_out, evidence)
    except IntegrationError as exc:
        print(f"desktop_system_ssh_integration_failed:{exc.code}", file=sys.stderr)
        return 1
    except Exception:
        print("desktop_system_ssh_integration_failed:unexpected_failure", file=sys.stderr)
        return 1
    print(evidence.to_json())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
