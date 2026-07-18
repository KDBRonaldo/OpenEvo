"""Rootless Bubblewrap-backed rollout runtime."""

from __future__ import annotations

import asyncio
import math
import os
import re
import signal
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from openevo.runtime.base import (
    BaseRuntime,
    RuntimeDownloadOperation,
    RuntimePathSecurityError,
)
from openevo.runtime.models import ExecResult, RuntimeSpec

_DEFAULT_BWRAP_BINARY: Final[str] = "/usr/bin/bwrap"
_DEFAULT_PATH: Final[str] = "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
_PRIVATE_HOME: Final[str] = "/home/openevo"
_PRIVATE_TMP: Final[str] = "/tmp"
_ENV_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")
_MAX_ENV_ITEMS: Final[int] = 1024
_MAX_ENV_NAME_BYTES: Final[int] = 255
_MAX_ENV_VALUE_BYTES: Final[int] = 128 * 1024
_MAX_ENV_TOTAL_BYTES: Final[int] = 1024 * 1024
_PROCESS_JOIN_SECONDS: Final[float] = 5.0
_DIRECTORY_FLAGS: Final[int] = os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_DIRECTORY
_FILE_FLAGS: Final[int] = os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_NONBLOCK
_REQUIRED_ROOTFS_DIRECTORIES: Final[tuple[tuple[str, ...], ...]] = (
    ("dev",),
    ("home",),
    ("openevo",),
    ("openevo", "session"),
    ("proc",),
    ("tmp",),
)


def _canonical_absolute_parts(value: str, *, label: str) -> tuple[str, ...]:
    if not isinstance(value, str) or not value.startswith("/") or "\x00" in value:
        raise ValueError(f"{label} must be a canonical absolute path")
    parts = value.split("/")
    if len(parts) < 2 or any(part in {"", ".", ".."} for part in parts[1:]):
        raise ValueError(f"{label} must be a canonical absolute path")
    return tuple(parts[1:])


def _directory_identity(value: os.stat_result) -> tuple[int, int, int, int]:
    return value.st_dev, value.st_ino, value.st_mode, value.st_uid


def _file_identity(
    value: os.stat_result,
) -> tuple[int, int, int, int, int, int, int, int]:
    return (
        value.st_dev,
        value.st_ino,
        value.st_mode,
        value.st_uid,
        value.st_nlink,
        value.st_size,
        value.st_mtime_ns,
        value.st_ctime_ns,
    )


@dataclass(slots=True)
class _DirectoryAuthority:
    path: Path
    names: tuple[str, ...]
    descriptors: list[int]
    identities: tuple[tuple[int, int, int, int], ...]

    @property
    def descriptor(self) -> int:
        return self.descriptors[-1]

    def verify(self, *, label: str) -> None:
        parent_fd = self.descriptors[0]
        if _directory_identity(os.fstat(parent_fd)) != self.identities[0]:
            raise RuntimePathSecurityError(f"{label} root descriptor changed")
        for index, (name, descriptor, expected) in enumerate(
            zip(self.names, self.descriptors[1:], self.identities[1:])
        ):
            if _directory_identity(os.fstat(descriptor)) != expected:
                raise RuntimePathSecurityError(f"{label} descriptor changed")
            try:
                named = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
            except OSError as exc:
                raise RuntimePathSecurityError(f"{label} path changed") from exc
            if _directory_identity(named) != expected:
                raise RuntimePathSecurityError(f"{label} path changed")
            parent_fd = self.descriptors[index + 1]

    def close(self) -> None:
        while self.descriptors:
            os.close(self.descriptors.pop())


@dataclass(slots=True)
class _ExecutableAuthority:
    path: Path
    parent: _DirectoryAuthority
    name: str
    descriptor: int
    identity: tuple[int, int, int, int, int, int, int, int]

    def verify(self) -> None:
        self.parent.verify(label="bubblewrap binary ancestor")
        opened = os.fstat(self.descriptor)
        if _file_identity(opened) != self.identity:
            raise RuntimePathSecurityError("bubblewrap binary descriptor changed")
        try:
            named = os.stat(
                self.name,
                dir_fd=self.parent.descriptor,
                follow_symlinks=False,
            )
        except OSError as exc:
            raise RuntimePathSecurityError("bubblewrap binary path changed") from exc
        if _file_identity(named) != self.identity:
            raise RuntimePathSecurityError("bubblewrap binary path changed")

    def close(self) -> None:
        os.close(self.descriptor)
        self.parent.close()


def _open_directory_authority(
    path: Path,
    *,
    label: str,
    expected_identity: tuple[int, int, int, int] | None = None,
    require_owner: bool,
) -> _DirectoryAuthority:
    parts = _canonical_absolute_parts(str(path), label=label)
    descriptors = [os.open("/", _DIRECTORY_FLAGS)]
    identities = [_directory_identity(os.fstat(descriptors[0]))]
    try:
        for part in parts:
            before = os.stat(part, dir_fd=descriptors[-1], follow_symlinks=False)
            if not stat.S_ISDIR(before.st_mode):
                raise RuntimePathSecurityError(f"{label} contains a non-directory")
            descriptor = os.open(part, _DIRECTORY_FLAGS, dir_fd=descriptors[-1])
            opened = os.fstat(descriptor)
            if _directory_identity(before) != _directory_identity(opened):
                os.close(descriptor)
                raise RuntimePathSecurityError(f"{label} changed while it was opened")
            descriptors.append(descriptor)
            identities.append(_directory_identity(opened))
        authority = _DirectoryAuthority(
            path=path,
            names=parts,
            descriptors=descriptors,
            identities=tuple(identities),
        )
        final_identity = authority.identities[-1]
        if expected_identity is not None and final_identity != expected_identity:
            raise RuntimePathSecurityError(f"{label} does not match its pinned identity")
        if require_owner and final_identity[3] != os.geteuid():
            raise RuntimePathSecurityError(f"{label} is not owned by the Core user")
        authority.verify(label=label)
        return authority
    except BaseException:
        while descriptors:
            os.close(descriptors.pop())
        raise


def _open_executable_authority(path: Path) -> _ExecutableAuthority:
    parts = _canonical_absolute_parts(str(path), label="bubblewrap binary")
    parent_path = Path("/").joinpath(*parts[:-1])
    parent = _open_directory_authority(
        parent_path,
        label="bubblewrap binary ancestor",
        require_owner=False,
    )
    descriptor = -1
    try:
        before = os.stat(parts[-1], dir_fd=parent.descriptor, follow_symlinks=False)
        mode = stat.S_IMODE(before.st_mode)
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_uid not in {0, os.geteuid()}
            or before.st_nlink != 1
            or mode & 0o022
            or not mode & 0o111
        ):
            raise RuntimePathSecurityError(
                "bubblewrap binary must be a root/Core-owned, single-link, non-writable executable"
            )
        descriptor = os.open(parts[-1], _FILE_FLAGS, dir_fd=parent.descriptor)
        opened = os.fstat(descriptor)
        identity = _file_identity(opened)
        if identity != _file_identity(before):
            raise RuntimePathSecurityError("bubblewrap binary changed while it was opened")
        authority = _ExecutableAuthority(
            path=path,
            parent=parent,
            name=parts[-1],
            descriptor=descriptor,
            identity=identity,
        )
        authority.verify()
        return authority
    except BaseException:
        if descriptor >= 0:
            os.close(descriptor)
        parent.close()
        raise


def _require_rootfs_mountpoints(authority: _DirectoryAuthority) -> None:
    authority.verify(label="bubblewrap rootfs")
    for parts in _REQUIRED_ROOTFS_DIRECTORIES:
        descriptor = os.dup(authority.descriptor)
        try:
            for part in parts:
                try:
                    next_descriptor = os.open(part, _DIRECTORY_FLAGS, dir_fd=descriptor)
                except OSError as exc:
                    target = "/" + "/".join(parts)
                    raise RuntimePathSecurityError(
                        f"bubblewrap rootfs requires real directory {target}"
                    ) from exc
                os.close(descriptor)
                descriptor = next_descriptor
        finally:
            os.close(descriptor)
    authority.verify(label="bubblewrap rootfs")


def _validate_cwd(value: str, *, label: str) -> str:
    if value == "/":
        return value
    _canonical_absolute_parts(value, label=label)
    return value


def _validate_environment(values: dict[str, str]) -> dict[str, str]:
    if len(values) > _MAX_ENV_ITEMS:
        raise ValueError("bubblewrap environment exceeds the item limit")
    total_bytes = 0
    validated: dict[str, str] = {}
    for key, value in values.items():
        if not isinstance(key, str) or not _ENV_NAME.fullmatch(key):
            raise ValueError("bubblewrap environment contains an invalid variable name")
        if key in {"HOME", "TMPDIR", "PWD"}:
            raise ValueError(f"bubblewrap environment cannot override {key}")
        if not isinstance(value, str) or "\x00" in value:
            raise ValueError(f"bubblewrap environment variable {key!r} is invalid")
        key_bytes = len(key.encode("utf-8"))
        value_bytes = len(value.encode("utf-8"))
        if key_bytes > _MAX_ENV_NAME_BYTES or value_bytes > _MAX_ENV_VALUE_BYTES:
            raise ValueError("bubblewrap environment variable exceeds the byte limit")
        total_bytes += key_bytes + value_bytes
        if total_bytes > _MAX_ENV_TOTAL_BYTES:
            raise ValueError("bubblewrap environment exceeds the total byte limit")
        validated[key] = value
    return validated


class BubblewrapRuntime(BaseRuntime):
    """A rootless sandbox created from a user-owned rootfs directory."""

    def __init__(self, spec: RuntimeSpec, session_id: str, session_dir: Path) -> None:
        super().__init__(spec, session_id, session_dir)
        if spec.container_user != "host":
            raise ValueError("bubblewrap runtime requires container_user='host'")
        if spec.network not in (None, "", "host", "none"):
            raise ValueError("bubblewrap runtime supports only host or isolated networking")
        if spec.allow_internet and spec.network == "none":
            raise ValueError(
                "bubblewrap runtime cannot combine allow_internet=true with network='none'"
            )
        options = dict(spec.kwargs)
        binary = options.pop("bwrap_binary", _DEFAULT_BWRAP_BINARY)
        if options:
            names = ", ".join(sorted(str(name) for name in options))
            raise ValueError(f"unsupported bubblewrap runtime options: {names}")
        if not isinstance(binary, str):
            raise ValueError("bubblewrap binary override must be a path string")
        _canonical_absolute_parts(binary, label="bubblewrap binary")
        rootfs = Path(spec.image)
        _canonical_absolute_parts(str(rootfs), label="bubblewrap rootfs")
        if rootfs == self.session_dir or rootfs.is_relative_to(self.session_dir):
            raise ValueError("bubblewrap rootfs must be outside the writable session tree")
        if self.session_dir.is_relative_to(rootfs):
            raise ValueError("bubblewrap session tree must be outside the rootfs")

        self._binary_path = Path(binary)
        self._rootfs_path = rootfs
        self._binary_authority: _ExecutableAuthority | None = None
        self._rootfs_authority: _DirectoryAuthority | None = None
        self._session_authority: _DirectoryAuthority | None = None
        self._started = False
        self._exec_lock = asyncio.Lock()
        self._termination_lock = asyncio.Lock()

    @property
    def runtime_id(self) -> str:
        return f"bubblewrap:{self.session_id}"

    @property
    def can_disable_internet(self) -> bool:
        return True

    async def start(self) -> None:
        if self._destroyed:
            raise RuntimeError("bubblewrap runtime was already destroyed")
        if self._started:
            return
        _validate_environment(self.spec.env)
        if self.spec.workdir is not None:
            _validate_cwd(self.spec.workdir, label="bubblewrap workdir")

        binary: _ExecutableAuthority | None = None
        rootfs: _DirectoryAuthority | None = None
        session: _DirectoryAuthority | None = None
        try:
            binary = _open_executable_authority(self._binary_path)
            rootfs = _open_directory_authority(
                self._rootfs_path,
                label="bubblewrap rootfs",
                require_owner=True,
            )
            _require_rootfs_mountpoints(rootfs)
            session = _open_directory_authority(
                self.session_dir,
                label="bubblewrap session",
                expected_identity=self._session_root_identity,
                require_owner=True,
            )
            self._binary_authority = binary
            self._rootfs_authority = rootfs
            self._session_authority = session
            self._started = True
        except BaseException:
            if session is not None:
                session.close()
            if rootfs is not None:
                rootfs.close()
            if binary is not None:
                binary.close()
            raise

    async def stop(self) -> None:
        if self._destroyed:
            return
        self._destroyed = True
        process = self._active_process
        if process is not None and process.returncode is None:
            await self._kill_process_group(process)
        async with self._exec_lock:
            self._close_authorities()

    async def cancel(self) -> None:
        process = self._active_process
        if process is not None and process.returncode is None:
            await self._kill_process_group(process)
        await self.stop()

    async def exec(
        self,
        command: str,
        *,
        cwd: str | None = None,
        env: dict[str, str] | None = None,
        timeout_sec: float | None = None,
    ) -> ExecResult:
        if not isinstance(command, str) or "\x00" in command:
            raise ValueError("bubblewrap command must be a NUL-free string")
        if timeout_sec is not None and (
            not isinstance(timeout_sec, (int, float))
            or isinstance(timeout_sec, bool)
            or not math.isfinite(timeout_sec)
            or timeout_sec <= 0
        ):
            raise ValueError("bubblewrap timeout must be a positive finite number")
        effective_cwd = cwd or self.spec.workdir or self.runtime_session_dir
        _validate_cwd(effective_cwd, label="bubblewrap cwd")
        effective_env = _validate_environment({**self.spec.env, **(env or {})})

        async with self._exec_lock:
            self._require_live_authorities()
            args, pass_fds, executable = self._build_exec_argv(
                command,
                cwd=effective_cwd,
                env=effective_env,
            )
            process = await self._spawn_process(
                args,
                executable=executable,
                pass_fds=pass_fds,
            )
            self._active_process = process
            assert process.stdout is not None
            assert process.stderr is not None
            stdout_buffer = bytearray()
            stderr_buffer = bytearray()
            drains = [
                asyncio.create_task(self._drain_bounded_stream(process.stdout, stdout_buffer)),
                asyncio.create_task(self._drain_bounded_stream(process.stderr, stderr_buffer)),
            ]
            try:
                try:
                    if timeout_sec is None:
                        await process.wait()
                    else:
                        await asyncio.wait_for(process.wait(), timeout=timeout_sec)
                except TimeoutError:
                    await self._kill_process_group(process)
                    await self._finish_stream_drains(drains)
                    return ExecResult(
                        stdout=self._decode_capture(stdout_buffer),
                        stderr=self._decode_capture(stderr_buffer),
                        return_code=-1,
                    )
                except asyncio.CancelledError:
                    await self._kill_process_group(process)
                    await self._finish_stream_drains(drains)
                    raise
                await self._finish_stream_drains(drains)
                self._verify_authorities()
                return ExecResult(
                    stdout=self._decode_capture(stdout_buffer),
                    stderr=self._decode_capture(stderr_buffer),
                    return_code=process.returncode if process.returncode is not None else -1,
                )
            finally:
                if self._active_process is process:
                    self._active_process = None

    async def _spawn_process(
        self,
        args: tuple[str, ...],
        *,
        executable: str,
        pass_fds: tuple[int, ...],
    ) -> asyncio.subprocess.Process:
        spawn = asyncio.create_task(
            asyncio.create_subprocess_exec(
                *args,
                executable=executable,
                pass_fds=pass_fds,
                start_new_session=True,
                env={},
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        )
        try:
            return await asyncio.shield(spawn)
        except asyncio.CancelledError:
            process: asyncio.subprocess.Process | None = None
            while not spawn.done():
                try:
                    process = await asyncio.shield(spawn)
                except asyncio.CancelledError:
                    continue
                except BaseException:
                    break
            if process is None and not spawn.cancelled():
                try:
                    process = spawn.result()
                except BaseException:
                    pass
            if process is not None:
                await self._kill_process_group(process)
            raise

    async def upload_file(self, local_path: str, remote_path: str) -> None:
        if not self._copy_to_bind_mount(local_path, remote_path):
            raise RuntimePathSecurityError("bubblewrap uploads must target /openevo/session")

    async def upload_dir(self, local_path: str, remote_path: str) -> None:
        if not self._copy_to_bind_mount(local_path, remote_path):
            raise RuntimePathSecurityError("bubblewrap uploads must target /openevo/session")

    async def download_file(self, remote_path: str, local_path: str) -> None:
        if not self._copy_from_bind_mount(remote_path, Path(local_path)):
            raise RuntimePathSecurityError("bubblewrap downloads must source /openevo/session")

    async def download_dir(self, remote_path: str, local_path: str) -> None:
        if not self._copy_from_bind_mount(remote_path, Path(local_path)):
            raise RuntimePathSecurityError("bubblewrap downloads must source /openevo/session")

    def _start_download_dir_operation(
        self, remote_path: str, local_path: str
    ) -> RuntimeDownloadOperation:
        return RuntimeDownloadOperation(self.download_dir(remote_path, local_path))

    def _build_exec_argv(
        self,
        command: str,
        *,
        cwd: str,
        env: dict[str, str],
    ) -> tuple[tuple[str, ...], tuple[int, ...], str]:
        binary = self._binary_authority
        rootfs = self._rootfs_authority
        session = self._session_authority
        if binary is None or rootfs is None or session is None:
            raise RuntimeError("bubblewrap runtime is not started")
        executable = f"/proc/self/fd/{binary.descriptor}"
        args = [
            str(binary.path),
            "--die-with-parent",
            "--new-session",
            "--unshare-all",
        ]
        if self.spec.allow_internet:
            args.append("--share-net")
        args.extend(
            [
                "--clearenv",
                "--ro-bind-fd",
                str(rootfs.descriptor),
                "/",
                "--proc",
                "/proc",
                "--dev",
                "/dev",
                "--tmpfs",
                _PRIVATE_TMP,
                "--tmpfs",
                "/home",
                "--dir",
                _PRIVATE_HOME,
                "--bind-fd",
                str(session.descriptor),
                self.runtime_session_dir,
                "--chdir",
                cwd,
            ]
        )
        process_env = {
            "HOME": _PRIVATE_HOME,
            "LANG": "C.UTF-8",
            "PATH": _DEFAULT_PATH,
            "TMPDIR": _PRIVATE_TMP,
            **env,
        }
        for key in sorted(process_env):
            args.extend(["--setenv", key, process_env[key]])
        args.extend(["--", "/bin/bash", "-lc", command])
        return (
            tuple(args),
            (binary.descriptor, rootfs.descriptor, session.descriptor),
            executable,
        )

    def _require_live_authorities(self) -> None:
        if self._destroyed:
            raise RuntimeError("bubblewrap runtime was already destroyed")
        if not self._started:
            raise RuntimeError("bubblewrap runtime is not started")
        self._verify_authorities()

    def _verify_authorities(self) -> None:
        binary = self._binary_authority
        rootfs = self._rootfs_authority
        session = self._session_authority
        if binary is None or rootfs is None or session is None:
            raise RuntimeError("bubblewrap runtime authority is unavailable")
        binary.verify()
        rootfs.verify(label="bubblewrap rootfs")
        session.verify(label="bubblewrap session")

    def _close_authorities(self) -> None:
        if self._session_authority is not None:
            self._session_authority.close()
            self._session_authority = None
        if self._rootfs_authority is not None:
            self._rootfs_authority.close()
            self._rootfs_authority = None
        if self._binary_authority is not None:
            self._binary_authority.close()
            self._binary_authority = None
        self._started = False

    async def _kill_process_group(self, process: asyncio.subprocess.Process) -> None:
        async with self._termination_lock:
            if process.returncode is not None:
                return
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            try:
                async with asyncio.timeout(_PROCESS_JOIN_SECONDS):
                    await process.wait()
            except TimeoutError as exc:
                raise RuntimeError("bubblewrap process group did not terminate") from exc
            except ProcessLookupError:
                pass
