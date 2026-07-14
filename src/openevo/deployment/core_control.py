from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
import hmac
import os
import re
import secrets
import shlex
import socket
import sys
import time
from typing import Literal, Protocol

from pydantic import SecretStr

from openevo.backend.runtime_identity import RuntimeIdentityError, load_bounded_json
from openevo.backend.service import (
    CoreServiceError,
    CoreServiceErrorCode,
    authenticate_core_service_endpoint,
)
from openevo.deployment.executor import RemoteExecutorTransport
from openevo.deployment.core_runtime import (
    CorePythonRuntimeAuthority,
    build_verified_python_command,
)
from openevo.deployment.preflight import RemoteCommandResult
from openevo.deployment.ssh import SshTransportError, SshTransportErrorCode


_BEARER_PATTERN = re.compile(r"[A-Za-z0-9_-]{64}\Z")
_DIGEST_PATTERN = re.compile(r"[0-9a-f]{64}\Z")
_SOURCE_COMMIT_PATTERN = re.compile(r"[0-9a-f]{40}\Z")
_MAX_BOOTSTRAP_JSON_BYTES = 4096
_REMOTE_PATH_PATTERN = re.compile(r"/(?:[A-Za-z0-9._@%+=,-]+/)*[A-Za-z0-9._@%+=,-]+\Z")
_GENERATION_BOOTSTRAP = r"""
import ctypes
import errno
import fcntl
import os
from pathlib import Path
import re
import subprocess
import stat
import sys
import venv

install_root = Path(sys.argv[1])
wheel_path = Path(sys.argv[2])
base_executable = sys.argv[3]
service_argv = sys.argv[4:]
uid = os.geteuid()
generation = install_root.name
service_root = install_root.parent.parent
staging_name = "staged-" + generation
authority_name = ".openevo-generation-authority"
max_staged_generations = 8
max_cleanup_nodes = 200000
max_cleanup_bytes = 2 * 1024 * 1024 * 1024
max_cleanup_depth = 64
dir_flags = os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW
file_flags = os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW


class InstallFailure(Exception):
    pass


def fail():
    raise InstallFailure


def open_absolute_dir(path):
    parts = str(path).split("/")[1:]
    if not parts or any(not part or part in {".", ".."} for part in parts):
        fail()
    fd = os.open("/", dir_flags)
    try:
        for part in parts:
            child = os.open(part, dir_flags, dir_fd=fd)
            os.close(fd)
            fd = child
        return fd
    except BaseException:
        os.close(fd)
        raise


def require_owned_dir(fd):
    metadata = os.fstat(fd)
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or metadata.st_uid != uid
        or stat.S_IMODE(metadata.st_mode) != 0o700
    ):
        fail()
    return metadata


def require_binding(path, fd):
    rebound = open_absolute_dir(path)
    try:
        expected = os.fstat(fd)
        current = os.fstat(rebound)
        if (expected.st_dev, expected.st_ino) != (current.st_dev, current.st_ino):
            fail()
    finally:
        os.close(rebound)


def ensure_private_dir(parent, name):
    try:
        os.mkdir(name, 0o700, dir_fd=parent)
        os.fsync(parent)
    except FileExistsError:
        pass
    fd = os.open(name, dir_flags, dir_fd=parent)
    try:
        opened = require_owned_dir(fd)
        current = os.stat(name, dir_fd=parent, follow_symlinks=False)
        if (opened.st_dev, opened.st_ino) != (current.st_dev, current.st_ino):
            fail()
        return fd
    except BaseException:
        os.close(fd)
        raise


def open_lock(parent, name):
    fd = os.open(
        name,
        os.O_RDWR | os.O_CREAT | os.O_CLOEXEC | os.O_NOFOLLOW,
        0o600,
        dir_fd=parent,
    )
    try:
        opened = os.fstat(fd)
        current = os.stat(name, dir_fd=parent, follow_symlinks=False)
        if (
            not stat.S_ISREG(opened.st_mode)
            or opened.st_uid != uid
            or opened.st_nlink != 1
            or stat.S_IMODE(opened.st_mode) != 0o600
            or (opened.st_dev, opened.st_ino) != (current.st_dev, current.st_ino)
        ):
            fail()
        fcntl.flock(fd, fcntl.LOCK_EX)
        current = os.stat(name, dir_fd=parent, follow_symlinks=False)
        if (opened.st_dev, opened.st_ino) != (current.st_dev, current.st_ino):
            fail()
        return fd
    except BaseException:
        os.close(fd)
        raise


def create_authority(stage_fd, stage_metadata):
    fd = os.open(
        authority_name,
        os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | os.O_NOFOLLOW,
        0o600,
        dir_fd=stage_fd,
    )
    try:
        payload = (
            "openevo-core-generation-stage-v1\n"
            + generation
            + "\n"
            + str(stage_metadata.st_dev)
            + ":"
            + str(stage_metadata.st_ino)
            + "\n"
        ).encode("ascii")
        if os.write(fd, payload) != len(payload):
            fail()
        os.fsync(fd)
        opened = os.fstat(fd)
        current = os.stat(authority_name, dir_fd=stage_fd, follow_symlinks=False)
        if (
            not stat.S_ISREG(opened.st_mode)
            or opened.st_uid != uid
            or opened.st_nlink != 1
            or stat.S_IMODE(opened.st_mode) != 0o600
            or opened.st_size != len(payload)
            or (opened.st_dev, opened.st_ino) != (current.st_dev, current.st_ino)
        ):
            fail()
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        os.fsync(stage_fd)
        return fd
    except BaseException:
        os.close(fd)
        raise


def verify_authority(stage_fd, stage_name, *, lock):
    authority_fd = os.open(
        authority_name,
        os.O_RDWR | os.O_CLOEXEC | os.O_NOFOLLOW,
        dir_fd=stage_fd,
    )
    try:
        stage_metadata = require_owned_dir(stage_fd)
        authority = os.fstat(authority_fd)
        current = os.stat(authority_name, dir_fd=stage_fd, follow_symlinks=False)
        expected = (
            "openevo-core-generation-stage-v1\n"
            + stage_name[len("staged-"):]
            + "\n"
            + str(stage_metadata.st_dev)
            + ":"
            + str(stage_metadata.st_ino)
            + "\n"
        ).encode("ascii")
        if (
            not stat.S_ISREG(authority.st_mode)
            or authority.st_uid != uid
            or authority.st_nlink != 1
            or stat.S_IMODE(authority.st_mode) != 0o600
            or authority.st_size != len(expected)
            or authority.st_size > 256
            or (authority.st_dev, authority.st_ino) != (current.st_dev, current.st_ino)
        ):
            fail()
        content = os.pread(authority_fd, 257, 0)
        if content != expected:
            fail()
        if lock:
            try:
                fcntl.flock(authority_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                fail()
        return authority_fd
    except BaseException:
        os.close(authority_fd)
        raise


def cleanup_stage(staging_fd, stage_name):
    stage_fd = os.open(stage_name, dir_flags, dir_fd=staging_fd)
    authority_fd = -1
    budget = {"nodes": 0, "bytes": 0}
    try:
        opened = require_owned_dir(stage_fd)
        current = os.stat(stage_name, dir_fd=staging_fd, follow_symlinks=False)
        if (opened.st_dev, opened.st_ino) != (current.st_dev, current.st_ino):
            fail()
        authority_fd = verify_authority(stage_fd, stage_name, lock=True)

        def clear_directory(fd, depth):
            if depth > max_cleanup_depth:
                fail()
            names = []
            with os.scandir(fd) as entries:
                for entry in entries:
                    budget["nodes"] += 1
                    if budget["nodes"] > max_cleanup_nodes:
                        fail()
                    names.append(entry.name)
            for name in names:
                if depth == 0 and name == authority_name:
                    continue
                metadata = os.stat(name, dir_fd=fd, follow_symlinks=False)
                budget["bytes"] += max(0, metadata.st_size)
                if budget["bytes"] > max_cleanup_bytes or metadata.st_uid != uid:
                    fail()
                if stat.S_ISDIR(metadata.st_mode):
                    child_fd = os.open(name, dir_flags, dir_fd=fd)
                    try:
                        child = os.fstat(child_fd)
                        current_child = os.stat(name, dir_fd=fd, follow_symlinks=False)
                        if (child.st_dev, child.st_ino) != (
                            current_child.st_dev,
                            current_child.st_ino,
                        ):
                            fail()
                        clear_directory(child_fd, depth + 1)
                    finally:
                        os.close(child_fd)
                    os.rmdir(name, dir_fd=fd)
                elif stat.S_ISREG(metadata.st_mode):
                    child_fd = os.open(name, file_flags, dir_fd=fd)
                    try:
                        child = os.fstat(child_fd)
                        current_child = os.stat(name, dir_fd=fd, follow_symlinks=False)
                        if (
                            child.st_uid != uid
                            or child.st_nlink != 1
                            or (child.st_dev, child.st_ino)
                            != (current_child.st_dev, current_child.st_ino)
                        ):
                            fail()
                        os.unlink(name, dir_fd=fd)
                    finally:
                        os.close(child_fd)
                elif stat.S_ISLNK(metadata.st_mode):
                    os.unlink(name, dir_fd=fd)
                else:
                    fail()
            os.fsync(fd)

        clear_directory(stage_fd, 0)
        current_authority = os.stat(
            authority_name, dir_fd=stage_fd, follow_symlinks=False
        )
        opened_authority = os.fstat(authority_fd)
        if (opened_authority.st_dev, opened_authority.st_ino) != (
            current_authority.st_dev,
            current_authority.st_ino,
        ):
            fail()
        os.unlink(authority_name, dir_fd=stage_fd)
        os.fsync(stage_fd)
        current = os.stat(stage_name, dir_fd=staging_fd, follow_symlinks=False)
        if (opened.st_dev, opened.st_ino) != (current.st_dev, current.st_ino):
            fail()
    finally:
        if authority_fd >= 0:
            os.close(authority_fd)
        os.close(stage_fd)
    os.rmdir(stage_name, dir_fd=staging_fd)
    os.fsync(staging_fd)


def reconcile_staging(staging_fd):
    names = []
    with os.scandir(staging_fd) as entries:
        for entry in entries:
            if len(names) >= max_staged_generations:
                fail()
            names.append(entry.name)
    for name in names:
        if re.fullmatch(r"staged-[0-9a-f]{32}", name) is None:
            fail()
        cleanup_stage(staging_fd, name)


def rename_noreplace(source_parent, source_name, destination_parent, destination_name):
    libc = ctypes.CDLL(None, use_errno=True)
    renameat2 = getattr(libc, "renameat2", None)
    if renameat2 is None:
        fail()
    renameat2.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
    renameat2.restype = ctypes.c_int
    result = renameat2(
        source_parent,
        os.fsencode(source_name),
        destination_parent,
        os.fsencode(destination_name),
        1,
    )
    if result == 0:
        return
    error = ctypes.get_errno()
    if error == errno.EEXIST:
        fail()
    raise OSError(error, "Core generation publication failed")


if (
    not install_root.is_absolute()
    or re.fullmatch(r"[0-9a-f]{32}", generation) is None
    or install_root != service_root / "releases" / generation
):
    raise SystemExit(73)

# FD-bound invocation deliberately reports /proc/self/fd/N. Restore the verified
# canonical base path so venv copies and pyvenv.cfg bind the selected runtime.
sys.executable = base_executable
sys._base_executable = base_executable
install_environment = {
    key: value
    for key, value in os.environ.items()
    if key
    in {
        "HOME",
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "LANG",
        "LC_ALL",
        "NO_PROXY",
        "PATH",
        "SSL_CERT_FILE",
        "SSL_CERT_DIR",
        "http_proxy",
        "https_proxy",
        "no_proxy",
    }
}
service_environment = {
    key: value
    for key, value in os.environ.items()
    if key in {"HOME", "LANG", "LC_ALL", "PATH"}
}

service_root_fd = -1
lock_fd = -1
staging_fd = -1
releases_fd = -1
stage_fd = -1
authority_fd = -1
published = False
try:
    try:
        service_root.mkdir(mode=0o700, parents=True, exist_ok=True)
        service_root_fd = open_absolute_dir(service_root)
        require_owned_dir(service_root_fd)
        require_binding(service_root, service_root_fd)
        lock_fd = open_lock(service_root_fd, "generation-install.lock")
        require_binding(service_root, service_root_fd)
        staging_fd = ensure_private_dir(service_root_fd, "release-staging")
        releases_fd = ensure_private_dir(service_root_fd, "releases")
        reconcile_staging(staging_fd)
        require_binding(service_root, service_root_fd)
        os.mkdir(staging_name, 0o700, dir_fd=staging_fd)
        os.fsync(staging_fd)
        stage_fd = os.open(staging_name, dir_flags, dir_fd=staging_fd)
        stage_metadata = require_owned_dir(stage_fd)
        current_stage = os.stat(staging_name, dir_fd=staging_fd, follow_symlinks=False)
        if (stage_metadata.st_dev, stage_metadata.st_ino) != (
            current_stage.st_dev,
            current_stage.st_ino,
        ):
            fail()
        authority_fd = create_authority(stage_fd, stage_metadata)
        staged_root = Path("/proc/self/fd") / str(staging_fd) / staging_name
        venv.EnvBuilder(with_pip=False, symlinks=False).create(staged_root)
        staged_interpreter = staged_root / "bin" / "python"
        module_runner = (
            "import os,runpy,sys;"
            "stage_fd=sys.argv[1];"
            "module=sys.argv[2];"
            "sys.executable=f'/proc/{os.getpid()}/fd/{stage_fd}/bin/python';"
            "sys.argv=[module,*sys.argv[3:]];"
            "runpy.run_module(module,run_name='__main__')"
        )
        for arguments in (
            [
                str(staged_interpreter),
                "-I",
                "-c",
                module_runner,
                str(stage_fd),
                "ensurepip",
                "--upgrade",
            ],
            [
                str(staged_interpreter),
                "-I",
                "-c",
                module_runner,
                str(stage_fd),
                "pip",
                "install",
                "--isolated",
                "--disable-pip-version-check",
                "--only-binary=:all:",
                "--no-compile",
                str(wheel_path),
            ],
            [
                str(staged_interpreter),
                "-I",
                "-c",
                "import openevo.backend.service",
            ],
        ):
            completed = subprocess.run(
                arguments,
                env=install_environment,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
                close_fds=True,
                pass_fds=(authority_fd, stage_fd, staging_fd),
            )
            if completed.returncode != 0:
                fail()
        interpreter_metadata = os.stat(
            "bin/python", dir_fd=stage_fd, follow_symlinks=False
        )
        if (
            not stat.S_ISREG(interpreter_metadata.st_mode)
            or interpreter_metadata.st_uid != uid
            or interpreter_metadata.st_nlink != 1
            or interpreter_metadata.st_mode & 0o111 == 0
        ):
            fail()
        require_binding(service_root, service_root_fd)
        current_stage = os.stat(staging_name, dir_fd=staging_fd, follow_symlinks=False)
        if (stage_metadata.st_dev, stage_metadata.st_ino) != (
            current_stage.st_dev,
            current_stage.st_ino,
        ):
            fail()
        os.fsync(stage_fd)
        rename_noreplace(staging_fd, staging_name, releases_fd, generation)
        published = True
        os.fsync(releases_fd)
        os.fsync(staging_fd)
        published_metadata = os.stat(
            generation, dir_fd=releases_fd, follow_symlinks=False
        )
        if (stage_metadata.st_dev, stage_metadata.st_ino) != (
            published_metadata.st_dev,
            published_metadata.st_ino,
        ):
            fail()
    except BaseException as exc:
        if isinstance(exc, (KeyboardInterrupt, SystemExit)):
            raise
        if not published and staging_fd >= 0:
            try:
                if authority_fd >= 0:
                    os.close(authority_fd)
                    authority_fd = -1
                if stage_fd >= 0:
                    os.close(stage_fd)
                    stage_fd = -1
                cleanup_stage(staging_fd, staging_name)
            except BaseException:
                pass
        raise SystemExit(73) from None
finally:
    if authority_fd >= 0:
        os.close(authority_fd)
    if stage_fd >= 0:
        os.close(stage_fd)
    if releases_fd >= 0:
        os.close(releases_fd)
    if staging_fd >= 0:
        os.close(staging_fd)
    if lock_fd >= 0:
        os.close(lock_fd)
    if service_root_fd >= 0:
        os.close(service_root_fd)

final_interpreter = install_root / "bin" / "python"
try:
    os.execve(
        final_interpreter,
        [str(final_interpreter), "-I", "-m", "openevo.backend.service", *service_argv],
        service_environment,
    )
except OSError:
    raise SystemExit(74) from None
""".strip()


class CoreControlBootstrapErrorCode(StrEnum):
    INVALID_PLAN = "core_bootstrap_plan_invalid"
    INSTALL_FAILED = "core_bootstrap_install_failed"
    VERIFICATION_FAILED = "core_bootstrap_verification_failed"
    SERVICE_FAILED = "core_bootstrap_service_failed"
    RESPONSE_INVALID = "core_bootstrap_response_invalid"
    DEADLINE_EXCEEDED = "core_bootstrap_deadline_exceeded"


class CoreControlBootstrapError(RuntimeError):
    def __init__(
        self,
        code: CoreControlBootstrapErrorCode,
        message: str,
        *,
        retryable: bool,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.retryable = retryable


@dataclass(frozen=True, slots=True)
class CoreControlBootstrapPlan:
    source_commit: str
    port: int = 0
    deadline_seconds: float = 90.0
    replace_mismatched: bool = False
    _wheel_path: str = field(repr=False, compare=False, default="")
    _framework_lock: str = field(repr=False, compare=False, default="")
    _service_root: str = field(repr=False, compare=False, default="")
    _runtime: CorePythonRuntimeAuthority | None = field(
        repr=False,
        compare=False,
        default=None,
    )

    def __post_init__(self) -> None:
        if (
            _SOURCE_COMMIT_PATTERN.fullmatch(self.source_commit) is None
            or not 0 <= self.port <= 65535
            or not 1 <= self.deadline_seconds <= 300
            or not _is_remote_absolute_path(self._wheel_path)
            or not _is_remote_absolute_path(self._framework_lock)
            or not _is_remote_absolute_path(self._service_root)
            or not isinstance(self._runtime, CorePythonRuntimeAuthority)
        ):
            raise CoreControlBootstrapError(
                CoreControlBootstrapErrorCode.INVALID_PLAN,
                "Core bootstrap settings are invalid.",
                retryable=False,
            )
        assert self._runtime is not None
        try:
            self._runtime.__post_init__()
        except ValueError:
            raise CoreControlBootstrapError(
                CoreControlBootstrapErrorCode.INVALID_PLAN,
                "Core bootstrap settings are invalid.",
                retryable=False,
            ) from None


@dataclass(frozen=True, slots=True)
class RemoteCoreControlAttachment:
    remote_host: str
    remote_port: int
    execution_mode: Literal["subscription"]
    capture_mode: Literal["transcript"]
    release_identity: str
    registry_digest: str
    source_commit: str
    generation: str
    status_proof: str
    attached: bool
    _bearer: SecretStr = field(repr=False, compare=False)

    @property
    def bearer_token(self) -> str:
        return self._bearer.get_secret_value()


class CoreTunnelHandle(Protocol):
    @property
    def base_url(self) -> str: ...

    def verify_authority(self) -> None: ...

    def open_verified_socket(self, *, timeout_seconds: float) -> socket.socket: ...

    def close(self) -> None: ...


class CoreBootstrapTransport(RemoteExecutorTransport, Protocol):
    def run_secret(
        self,
        command: str,
        *,
        timeout_seconds: float = 30.0,
    ) -> SecretStr: ...


class CoreTunnelTransport(Protocol):
    def open_core_tunnel(
        self,
        *,
        remote_port: int,
        remote_host: str = "127.0.0.1",
        wait_for_ready: bool = True,
        timeout_seconds: float = 10.0,
    ) -> CoreTunnelHandle: ...


@dataclass(frozen=True, slots=True)
class VerifiedCoreControlTunnel:
    base_url: str
    release_identity: str
    registry_digest: str
    source_commit: str
    generation: str
    status_proof: str
    _tunnel: CoreTunnelHandle = field(repr=False, compare=False)
    _bearer: SecretStr = field(repr=False, compare=False)

    @property
    def bearer_token(self) -> str:
        return self._bearer.get_secret_value()

    def verify_authority(self) -> None:
        self._tunnel.verify_authority()

    def open_verified_socket(self, *, timeout_seconds: float) -> socket.socket:
        return self._tunnel.open_verified_socket(timeout_seconds=timeout_seconds)

    def close(self) -> None:
        self._tunnel.close()

    def __enter__(self) -> VerifiedCoreControlTunnel:
        return self

    def __exit__(self, _exc_type: object, _exc: object, _traceback: object) -> None:
        self.close()


def build_core_control_bootstrap_plan(
    *,
    runtime: CorePythonRuntimeAuthority,
    wheel_path: str,
    framework_lock: str,
    service_root: str,
    source_commit: str,
    port: int = 0,
    deadline_seconds: float = 90.0,
    replace_mismatched: bool = False,
) -> CoreControlBootstrapPlan:
    if (
        not _is_remote_absolute_path(wheel_path)
        or not _is_remote_absolute_path(framework_lock)
        or not _is_remote_absolute_path(service_root)
        or _SOURCE_COMMIT_PATTERN.fullmatch(source_commit) is None
        or not 0 <= port <= 65535
        or not 1 <= deadline_seconds <= 300
        or not isinstance(runtime, CorePythonRuntimeAuthority)
    ):
        raise CoreControlBootstrapError(
            CoreControlBootstrapErrorCode.INVALID_PLAN,
            "Core bootstrap settings are invalid.",
            retryable=False,
        )
    try:
        runtime.__post_init__()
    except ValueError:
        raise CoreControlBootstrapError(
            CoreControlBootstrapErrorCode.INVALID_PLAN,
            "Core bootstrap settings are invalid.",
            retryable=False,
        ) from None
    return CoreControlBootstrapPlan(
        source_commit=source_commit,
        port=port,
        deadline_seconds=deadline_seconds,
        replace_mismatched=replace_mismatched,
        _wheel_path=wheel_path,
        _framework_lock=framework_lock,
        _service_root=service_root,
        _runtime=runtime,
    )


def execute_core_control_bootstrap(
    plan: CoreControlBootstrapPlan,
    transport: CoreBootstrapTransport,
) -> RemoteCoreControlAttachment:
    deadline = time.monotonic() + plan.deadline_seconds
    if plan._runtime is None:
        raise CoreControlBootstrapError(
            CoreControlBootstrapErrorCode.INVALID_PLAN,
            "Core bootstrap settings are invalid.",
            retryable=False,
        )
    attachment_name = f"bootstrap-{secrets.token_hex(16)}.json"
    install_generation = secrets.token_hex(16)
    install_root = f"{plan._service_root}/releases/{install_generation}"
    service_arguments = (
        "bootstrap"
        f" --service-root {shlex.quote(plan._service_root)}"
        f" --wheel-path {shlex.quote(plan._wheel_path)}"
        f" --framework-lock {shlex.quote(plan._framework_lock)}"
        f" --source-commit {plan.source_commit}"
        f" --attachment-name {attachment_name}"
        f" --install-generation {install_generation}"
        f" --port {plan.port}"
        f" --deadline-seconds {max(1.0, plan.deadline_seconds)}"
        + (" --replace-mismatched" if plan.replace_mismatched else "")
    )
    service_command = build_verified_python_command(
        plan._runtime,
        _GENERATION_BOOTSTRAP,
        install_root,
        plan._wheel_path,
        plan._runtime.executable_path,
        *shlex.split(service_arguments),
    )
    service = _run_bootstrap_command(
        transport,
        service_command,
        deadline=deadline,
        code=CoreControlBootstrapErrorCode.SERVICE_FAILED,
        message="Core Control could not be attached or started.",
    )
    if not service.ok:
        code = (
            CoreControlBootstrapErrorCode.INSTALL_FAILED
            if service.return_code == 73
            else CoreControlBootstrapErrorCode.SERVICE_FAILED
        )
        message = (
            "The isolated OpenEvo Core generation could not be installed."
            if code is CoreControlBootstrapErrorCode.INSTALL_FAILED
            else "Core Control could not be attached or started."
        )
        raise CoreControlBootstrapError(
            code,
            message,
            retryable=True,
        )
    consume_command = (
        f"{shlex.quote(install_root + '/bin/python')} -I "
        "-m openevo.backend.service consume-attachment"
        f" --service-root {shlex.quote(plan._service_root)}"
        f" --attachment-name {attachment_name}"
    )
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise CoreControlBootstrapError(
            CoreControlBootstrapErrorCode.DEADLINE_EXCEEDED,
            "Core bootstrap exceeded its total deadline.",
            retryable=True,
        )
    try:
        payload = transport.run_secret(consume_command, timeout_seconds=remaining)
    except SshTransportError as exc:
        if exc.code is SshTransportErrorCode.TIMEOUT:
            raise CoreControlBootstrapError(
                CoreControlBootstrapErrorCode.DEADLINE_EXCEEDED,
                "Core bootstrap exceeded its total deadline.",
                retryable=True,
            ) from None
        raise CoreControlBootstrapError(
            CoreControlBootstrapErrorCode.RESPONSE_INVALID,
            "Core bootstrap attachment could not be read securely.",
            retryable=True,
        ) from None
    except Exception:
        raise CoreControlBootstrapError(
            CoreControlBootstrapErrorCode.RESPONSE_INVALID,
            "Core bootstrap attachment could not be read securely.",
            retryable=True,
        ) from None
    return parse_core_control_attachment(payload)


def _run_bootstrap_command(
    transport: RemoteExecutorTransport,
    command: str,
    *,
    deadline: float,
    code: CoreControlBootstrapErrorCode,
    message: str,
) -> RemoteCommandResult:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise CoreControlBootstrapError(
            CoreControlBootstrapErrorCode.DEADLINE_EXCEEDED,
            "Core bootstrap exceeded its total deadline.",
            retryable=True,
        )
    try:
        return transport.run(command, timeout_seconds=remaining)
    except SshTransportError as exc:
        if exc.code is SshTransportErrorCode.TIMEOUT:
            raise CoreControlBootstrapError(
                CoreControlBootstrapErrorCode.DEADLINE_EXCEEDED,
                "Core bootstrap exceeded its total deadline.",
                retryable=True,
            ) from None
        raise CoreControlBootstrapError(code, message, retryable=True) from None
    except Exception:
        raise CoreControlBootstrapError(code, message, retryable=True) from None


def parse_core_control_attachment(payload: SecretStr) -> RemoteCoreControlAttachment:
    if not isinstance(payload, SecretStr):
        raise CoreControlBootstrapError(
            CoreControlBootstrapErrorCode.RESPONSE_INVALID,
            "Core bootstrap returned an invalid attachment.",
            retryable=False,
        )
    payload_value = payload.get_secret_value()
    if len(payload_value) > _MAX_BOOTSTRAP_JSON_BYTES:
        raise CoreControlBootstrapError(
            CoreControlBootstrapErrorCode.RESPONSE_INVALID,
            "Core bootstrap returned an invalid attachment.",
            retryable=False,
        )
    try:
        encoded = payload_value.encode("utf-8")
    except UnicodeError:
        raise CoreControlBootstrapError(
            CoreControlBootstrapErrorCode.RESPONSE_INVALID,
            "Core bootstrap returned an invalid attachment.",
            retryable=False,
        ) from None
    try:
        value = load_bounded_json(encoded, max_bytes=_MAX_BOOTSTRAP_JSON_BYTES)
    except RuntimeIdentityError:
        raise CoreControlBootstrapError(
            CoreControlBootstrapErrorCode.RESPONSE_INVALID,
            "Core bootstrap returned an invalid attachment.",
            retryable=False,
        ) from None
    expected = {
        "schema_version",
        "host",
        "port",
        "release_identity",
        "registry_digest",
        "source_commit",
        "generation",
        "status_proof",
        "attached",
        "bearer_token",
        "execution_mode",
        "capture_mode",
    }
    if not isinstance(value, dict) or set(value) != expected:
        raise CoreControlBootstrapError(
            CoreControlBootstrapErrorCode.RESPONSE_INVALID,
            "Core bootstrap returned an invalid attachment.",
            retryable=False,
        )
    port = value.get("port")
    attached = value.get("attached")
    bearer = value.get("bearer_token")
    if (
        value.get("schema_version") != 1
        or value.get("host") != "127.0.0.1"
        or type(port) is not int
        or not 1 <= port <= 65535
        or type(attached) is not bool
        or value.get("execution_mode") != "subscription"
        or value.get("capture_mode") != "transcript"
        or not isinstance(bearer, str)
        or _BEARER_PATTERN.fullmatch(bearer) is None
        or not _valid_digest(value.get("release_identity"))
        or not _valid_digest(value.get("registry_digest"))
        or not isinstance(value.get("source_commit"), str)
        or _SOURCE_COMMIT_PATTERN.fullmatch(value["source_commit"]) is None
        or not _valid_digest(value.get("status_proof"))
        or not isinstance(value.get("generation"), str)
        or re.fullmatch(r"[0-9a-f]{32}", value["generation"]) is None
    ):
        raise CoreControlBootstrapError(
            CoreControlBootstrapErrorCode.RESPONSE_INVALID,
            "Core bootstrap returned an invalid attachment.",
            retryable=False,
        )
    return RemoteCoreControlAttachment(
        remote_host="127.0.0.1",
        remote_port=port,
        execution_mode="subscription",
        capture_mode="transcript",
        release_identity=value["release_identity"],
        registry_digest=value["registry_digest"],
        source_commit=value["source_commit"],
        generation=value["generation"],
        status_proof=value["status_proof"],
        attached=attached,
        _bearer=SecretStr(bearer),
    )


def open_core_control_tunnel(
    attachment: RemoteCoreControlAttachment,
    transport: CoreTunnelTransport,
    *,
    timeout_seconds: float = 10.0,
) -> VerifiedCoreControlTunnel:
    if timeout_seconds <= 0 or timeout_seconds > 60:
        raise CoreControlBootstrapError(
            CoreControlBootstrapErrorCode.INVALID_PLAN,
            "Core tunnel settings are invalid.",
            retryable=False,
        )
    try:
        tunnel = transport.open_core_tunnel(
            remote_port=attachment.remote_port,
            remote_host="127.0.0.1",
            wait_for_ready=True,
            timeout_seconds=timeout_seconds,
        )
    except SshTransportError as exc:
        code = (
            CoreControlBootstrapErrorCode.DEADLINE_EXCEEDED
            if exc.code is SshTransportErrorCode.TIMEOUT
            else CoreControlBootstrapErrorCode.SERVICE_FAILED
        )
        message = (
            "Core Control tunnel opening exceeded its deadline."
            if code is CoreControlBootstrapErrorCode.DEADLINE_EXCEEDED
            else "The Core Control tunnel could not be opened."
        )
        raise CoreControlBootstrapError(code, message, retryable=True) from None
    except Exception:
        raise CoreControlBootstrapError(
            CoreControlBootstrapErrorCode.SERVICE_FAILED,
            "The Core Control tunnel could not be opened.",
            retryable=True,
        ) from None
    verified: VerifiedCoreControlTunnel | None = None
    try:
        expected_base_url = "http://openevo-core.local"
        if tunnel.base_url != expected_base_url:
            raise CoreControlBootstrapError(
                CoreControlBootstrapErrorCode.RESPONSE_INVALID,
                "The Core Control tunnel endpoint is invalid.",
                retryable=False,
            )
        proof = authenticate_core_service_endpoint(
            host=None,
            port=None,
            bearer=attachment.bearer_token,
            release_identity=attachment.release_identity,
            registry_digest=attachment.registry_digest,
            source_commit=attachment.source_commit,
            generation=attachment.generation,
            deadline=time.monotonic() + timeout_seconds,
            endpoint=tunnel,
        )
        if not hmac.compare_digest(proof, attachment.status_proof):
            raise CoreControlBootstrapError(
                CoreControlBootstrapErrorCode.RESPONSE_INVALID,
                "The Core Control tunnel identity did not match its attachment.",
                retryable=False,
            )
        tunnel.verify_authority()
        verified = VerifiedCoreControlTunnel(
            base_url=expected_base_url,
            release_identity=attachment.release_identity,
            registry_digest=attachment.registry_digest,
            source_commit=attachment.source_commit,
            generation=attachment.generation,
            status_proof=proof,
            _tunnel=tunnel,
            _bearer=SecretStr(attachment.bearer_token),
        )
        return verified
    except CoreControlBootstrapError:
        raise
    except CoreServiceError as exc:
        if exc.code is CoreServiceErrorCode.DEADLINE_EXCEEDED:
            code = CoreControlBootstrapErrorCode.DEADLINE_EXCEEDED
            message = "Core Control tunnel authentication exceeded its deadline."
        elif exc.code is CoreServiceErrorCode.STATUS_INVALID:
            code = CoreControlBootstrapErrorCode.RESPONSE_INVALID
            message = "The Core Control tunnel identity response was invalid."
        else:
            code = CoreControlBootstrapErrorCode.SERVICE_FAILED
            message = "The Core Control tunnel could not reach the remote daemon."
        raise CoreControlBootstrapError(
            code,
            message,
            retryable=exc.retryable,
        ) from None
    except Exception:
        raise CoreControlBootstrapError(
            CoreControlBootstrapErrorCode.SERVICE_FAILED,
            "The Core Control tunnel could not reach the remote daemon.",
            retryable=True,
        ) from None
    finally:
        if verified is None or sys.exc_info()[0] is not None:
            try:
                tunnel.close()
            except BaseException:
                pass


def _valid_digest(value: object) -> bool:
    return isinstance(value, str) and _DIGEST_PATTERN.fullmatch(value) is not None


def _is_remote_absolute_path(value: str) -> bool:
    return (
        isinstance(value, str)
        and _REMOTE_PATH_PATTERN.fullmatch(value) is not None
        and os.pathsep not in value
        and "\0" not in value
        and all(part not in {"", ".", ".."} for part in value.split("/")[1:])
    )


__all__ = [
    "CoreBootstrapTransport",
    "CoreControlBootstrapError",
    "CoreControlBootstrapErrorCode",
    "CoreControlBootstrapPlan",
    "RemoteCoreControlAttachment",
    "VerifiedCoreControlTunnel",
    "build_core_control_bootstrap_plan",
    "execute_core_control_bootstrap",
    "open_core_control_tunnel",
    "parse_core_control_attachment",
]
