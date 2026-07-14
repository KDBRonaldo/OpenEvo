"""Docker-backed rollout runtime."""

from __future__ import annotations

import logging
import os
import shlex
import stat
from hashlib import sha256
from pathlib import Path
from typing import Final, Literal

from openevo.runtime.base import BaseRuntime
from openevo.runtime.managed import MANAGED_CODEX_HOME
from openevo.runtime.models import ExecResult, RuntimeSpec

logger = logging.getLogger(__name__)

_CIDFILE_MAX_BYTES: Final[int] = 128
_OWNERSHIP_DIRECTORY_NAME: Final[str] = ".openevo-docker-ownership"
_DIRECTORY_FLAGS: Final[int] = os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_DIRECTORY
_OwnershipState = Literal["none", "candidate", "verified"]


def _object_identity(value: os.stat_result) -> tuple[int, int]:
    return (value.st_dev, value.st_ino)


def _full_file_identity(
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


def _unlink_if_same_identity(
    directory_fd: int,
    name: str,
    expected: tuple[int, int] | None,
) -> None:
    if expected is None:
        return
    try:
        current = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
    except FileNotFoundError:
        return
    if _object_identity(current) == expected:
        os.unlink(name, dir_fd=directory_fd)


class DockerRuntime(BaseRuntime):
    """Long-lived Docker container used across init, run, and post-run."""

    def __init__(
        self,
        spec: RuntimeSpec,
        session_id: str,
        session_dir: Path,
        *,
        credential_dir: Path | None = None,
    ) -> None:
        super().__init__(spec, session_id, session_dir)
        if credential_dir is not None:
            if not credential_dir.is_absolute():
                raise ValueError("credential_dir must be absolute")
            try:
                credential_dir.relative_to(session_dir)
            except ValueError:
                pass
            else:
                raise ValueError("credential_dir must be outside the session tree")
        self._credential_dir = credential_dir
        # Use enough of the session_id to preserve the "-eval" suffix used by
        # fresh evaluator runtimes, avoiding collisions with the agent runtime.
        safe_name = session_id.replace("/", "-")[:55]
        self._container_name = f"openevo-{safe_name}"
        self._container_id: str | None = None
        ownership_key = sha256(
            f"{session_id}\0{session_dir.absolute()}".encode("utf-8")
        ).hexdigest()
        self._ownership_dir = session_dir.parent / _OWNERSHIP_DIRECTORY_NAME
        self._cidfile = self._ownership_dir / f"{ownership_key}.cid"
        self._ownership_lock = self._ownership_dir / f"{ownership_key}.lock"
        self._ownership_lock_fd = -1
        self._cidfile_identity: tuple[int, int] | None = None
        self._ownership_state: _OwnershipState = "none"
        self._create_succeeded = False
        self._absence_proven = False
        self._chmod_needed: bool | None = False if spec.container_user == "host" else None

    @property
    def runtime_id(self) -> str:
        return self._container_name

    @property
    def container_id(self) -> str | None:
        return self._container_id

    @property
    def absence_proven(self) -> bool:
        return self._absence_proven

    @property
    def _container_ref(self) -> str:
        if self._container_id is None or self._ownership_state != "verified":
            raise RuntimeError("docker container ownership has not been verified")
        return self._container_id

    @property
    def supports_gpus(self) -> bool:
        return True

    @property
    def can_disable_internet(self) -> bool:
        return True

    @property
    def supports_cpu_limits(self) -> bool:
        return True

    @property
    def supports_memory_limits(self) -> bool:
        return True

    async def start(self) -> None:
        if self._destroyed:
            raise RuntimeError("docker runtime was already destroyed")
        self._prepare_create_ownership()
        create_args = [
            "docker",
            "create",
            "--name",
            self._container_name,
            "--cidfile",
            str(self._cidfile),
        ]
        if self.spec.container_user == "host":
            create_args.extend(["--user", f"{os.getuid()}:{os.getgid()}"])
        if not self.spec.allow_internet:
            create_args.extend(["--network", "none"])
        elif self.spec.network:
            create_args.extend(["--network", self.spec.network])
        if self.spec.gpus > 0:
            create_args.extend(["--gpus", str(self.spec.gpus)])
        if self.spec.cpus is not None:
            create_args.extend(["--cpus", str(self.spec.cpus)])
        if self.spec.memory_mb is not None:
            create_args.extend(["--memory", f"{self.spec.memory_mb}m"])
        create_args.extend(["-v", f"{self.session_dir}:{self.runtime_session_dir}"])
        if self._credential_dir is not None:
            create_args.extend(["-v", f"{self._credential_dir}:{MANAGED_CODEX_HOME}"])
        # Additional volumes from kwargs (e.g., Docker socket for agents that need DinD)
        for vol in self.spec.kwargs.get("volumes", []):
            create_args.extend(["-v", vol])
        create_args.extend([self.spec.image, "sleep", "infinity"])
        rc, stdout, stderr = await self._run_local_command(
            *create_args,
            capture=True,
            timeout=self._START_TIMEOUT,
        )
        if rc != 0:
            self._mark_no_ownership()
            raise RuntimeError(f"docker create failed with exit code {rc}: {stderr}")
        self._create_succeeded = True
        try:
            container_id = self._read_created_container_id()
        except Exception as exc:
            raise RuntimeError(
                "docker create succeeded but container ownership could not be verified; "
                "cleanup/recovery state was retained"
            ) from exc
        self._container_id = container_id
        self._ownership_state = "candidate"
        if str(stdout or "").strip() != container_id:
            raise RuntimeError(
                "docker create succeeded but container ownership could not be verified; "
                "cleanup/recovery state was retained"
            )
        verification = await self._verify_container_id()
        if verification != "present":
            raise RuntimeError(
                "docker create succeeded but container ownership could not be verified; "
                "cleanup/recovery state was retained"
            )
        self._ownership_state = "verified"
        rc, _, stderr = await self._run_local_command(
            "docker",
            "start",
            self._container_ref,
            capture=True,
            timeout=self._START_TIMEOUT,
        )
        if rc != 0:
            await self.stop()
            raise RuntimeError(f"docker start failed with exit code {rc}: {stderr}")
        # Skip the chmod when container and host UIDs match — recursive chmod
        # over a large session dir can be expensive and is only needed when the
        # container user can't write to host-owned bind-mounted files.
        self._chmod_needed = (
            False if self.spec.container_user == "host" else await self._detect_chmod_needed()
        )
        if self._chmod_needed:
            await self._run_local_command(
                "docker",
                "exec",
                "--user",
                "root",
                self._container_ref,
                "chmod",
                "-R",
                "a+rwX",
                self.runtime_session_dir,
                timeout=self._STOP_TIMEOUT,
            )

    _START_TIMEOUT = 600.0  # seconds for docker create / start under high rollout load
    _STOP_TIMEOUT = 30.0  # seconds per cleanup command

    async def stop(self) -> None:
        if self._destroyed:
            return
        if self._container_id is None:
            if self._create_succeeded:
                raise RuntimeError(
                    "docker create ownership is unresolved; cleanup/recovery state was retained"
                )
            self._mark_no_ownership()
            return

        verification = await self._verify_container_id()
        if verification == "absent":
            self._mark_owned_container_absent()
            return
        if verification != "present":
            raise RuntimeError(
                "docker container ownership could not be verified; "
                "cleanup/recovery state was retained"
            )
        self._ownership_state = "verified"
        # chmod is best-effort so the host can reclaim bind-mounted files.
        # Skip when UIDs match (no permission mismatch to resolve).
        if self.spec.container_user != "host" and self._chmod_needed is not False:
            try:
                await self._run_local_command(
                    "docker",
                    "exec",
                    "--user",
                    "root",
                    self._container_ref,
                    "chmod",
                    "-R",
                    "a+rwX",
                    self.runtime_session_dir,
                    timeout=self._STOP_TIMEOUT,
                )
            except Exception:
                logger.warning("chmod cleanup failed for %s", self._container_name)
        # kill first (instant SIGKILL), then rm to remove metadata.
        try:
            await self._run_local_command(
                "docker",
                "kill",
                self._container_ref,
                timeout=self._STOP_TIMEOUT,
            )
        except Exception:
            logger.warning("docker kill failed for %s", self._container_name)
        try:
            rc, _, stderr = await self._run_local_command(
                "docker",
                "rm",
                "-f",
                self._container_ref,
                timeout=self._STOP_TIMEOUT,
                capture=True,
            )
        except Exception as exc:
            raise RuntimeError(
                f"docker container {self._container_name} could not be proven removed"
            ) from exc
        final_verification = await self._verify_container_id()
        if final_verification != "absent":
            raise RuntimeError(
                "docker container "
                f"{self._container_ref} could not be proven removed: "
                f"{stderr or rc}"
            )
        self._mark_owned_container_absent()

    def _prepare_create_ownership(self) -> None:
        try:
            self._ownership_dir.mkdir(mode=0o700)
        except FileExistsError:
            pass
        directory_stat = self._ownership_dir.stat(follow_symlinks=False)
        if (
            not stat.S_ISDIR(directory_stat.st_mode)
            or directory_stat.st_uid != os.geteuid()
            or stat.S_IMODE(directory_stat.st_mode) != 0o700
        ):
            raise RuntimeError("docker ownership directory is not private")
        directory_fd = os.open(self._ownership_dir, _DIRECTORY_FLAGS)
        try:
            self._ownership_lock_fd = os.open(
                self._ownership_lock.name,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | os.O_NOFOLLOW,
                0o600,
                dir_fd=directory_fd,
            )
            try:
                os.stat(self._cidfile.name, dir_fd=directory_fd, follow_symlinks=False)
            except FileNotFoundError:
                pass
            else:
                raise RuntimeError("docker ownership cidfile already exists")
        except Exception:
            self._release_ownership_files(directory_fd=directory_fd)
            raise
        finally:
            os.close(directory_fd)

    def _read_created_container_id(self) -> str:
        directory_fd = os.open(self._ownership_dir, _DIRECTORY_FLAGS)
        descriptor = -1
        try:
            before = os.stat(
                self._cidfile.name,
                dir_fd=directory_fd,
                follow_symlinks=False,
            )
            descriptor = os.open(
                self._cidfile.name,
                os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW,
                dir_fd=directory_fd,
            )
            opened = os.fstat(descriptor)
            if (
                not stat.S_ISREG(opened.st_mode)
                or opened.st_uid != os.geteuid()
                or opened.st_nlink != 1
                or opened.st_size <= 0
                or opened.st_size > _CIDFILE_MAX_BYTES
                or _object_identity(before) != _object_identity(opened)
            ):
                raise RuntimeError("docker ownership cidfile is invalid")
            content = os.read(descriptor, opened.st_size + 1)
            after = os.fstat(descriptor)
            named_after = os.stat(
                self._cidfile.name,
                dir_fd=directory_fd,
                follow_symlinks=False,
            )
            if (
                len(content) > opened.st_size
                or _full_file_identity(opened) != _full_file_identity(after)
                or _full_file_identity(opened) != _full_file_identity(named_after)
            ):
                raise RuntimeError("docker ownership cidfile changed during read")
            try:
                container_id = content.decode("ascii").strip()
            except UnicodeDecodeError as exc:
                raise RuntimeError("docker ownership cidfile is not ASCII") from exc
            if not container_id or any(character.isspace() for character in container_id):
                raise RuntimeError("docker ownership cidfile has an invalid container ID")
            self._cidfile_identity = _object_identity(opened)
            return container_id
        finally:
            if descriptor >= 0:
                os.close(descriptor)
            os.close(directory_fd)

    async def _verify_container_id(self) -> Literal["present", "absent", "unknown"]:
        container_id = self._container_id
        if container_id is None:
            return "unknown"
        rc, stdout, stderr = await self._run_local_command(
            "docker",
            "container",
            "inspect",
            "--format",
            "{{.Id}}",
            container_id,
            timeout=self._STOP_TIMEOUT,
            capture=True,
        )
        if rc == 0:
            return "present" if str(stdout or "").strip() == container_id else "unknown"
        absent_message = (stderr or "").lower()
        if any(marker in absent_message for marker in ("no such object", "no such container")):
            return "absent"
        return "unknown"

    def _mark_no_ownership(self) -> None:
        self._ownership_state = "none"
        self._create_succeeded = False
        self._absence_proven = True
        self._destroyed = True
        self._release_ownership_files()

    def _mark_owned_container_absent(self) -> None:
        self._absence_proven = True
        self._destroyed = True
        self._release_ownership_files()

    def _release_ownership_files(self, *, directory_fd: int | None = None) -> None:
        opened_directory_fd = directory_fd
        if opened_directory_fd is None:
            try:
                opened_directory_fd = os.open(self._ownership_dir, _DIRECTORY_FLAGS)
            except FileNotFoundError:
                opened_directory_fd = -1
        try:
            if opened_directory_fd >= 0:
                _unlink_if_same_identity(
                    opened_directory_fd,
                    self._cidfile.name,
                    self._cidfile_identity,
                )
                lock_identity = None
                if self._ownership_lock_fd >= 0:
                    lock_identity = _object_identity(os.fstat(self._ownership_lock_fd))
                _unlink_if_same_identity(
                    opened_directory_fd,
                    self._ownership_lock.name,
                    lock_identity,
                )
        finally:
            if directory_fd is None and opened_directory_fd >= 0:
                os.close(opened_directory_fd)
            if self._ownership_lock_fd >= 0:
                os.close(self._ownership_lock_fd)
                self._ownership_lock_fd = -1
            self._cidfile_identity = None
        try:
            self._ownership_dir.rmdir()
        except OSError:
            pass

    async def _detect_chmod_needed(self) -> bool:
        """True unless the container's effective UID matches the host's."""
        rc, stdout, _ = await self._run_local_command(
            "docker", "exec", self._container_ref, "id", "-u",
            capture=True, timeout=self._STOP_TIMEOUT,
        )
        if rc != 0:
            return True
        try:
            return int(stdout.strip()) != os.getuid()
        except ValueError:
            return True

    async def exec(
        self,
        command: str,
        *,
        cwd: str | None = None,
        env: dict[str, str] | None = None,
        timeout_sec: float | None = None,
    ) -> ExecResult:
        args = ["docker", "exec"]
        effective_env = {**self.spec.env, **(env or {})}
        effective_workdir = cwd or self.spec.workdir or self.runtime_session_dir
        if effective_workdir:
            args.extend(["-w", effective_workdir])
        for key, value in effective_env.items():
            args.extend(["-e", f"{key}={value}"])
        shell_exports = []
        for key in ("HOME", "PATH"):
            if key in effective_env:
                shell_exports.append(
                    f"export {key}={shlex.quote(str(effective_env[key]))};"
                )
        wrapped_command = " ".join([*shell_exports, command])
        args.extend([self._container_ref, "bash", "-lc", wrapped_command])
        rc, stdout, stderr = await self._run_local_command(
            *args, timeout=timeout_sec, capture=True
        )
        return ExecResult(stdout=stdout, stderr=stderr, return_code=rc)

    async def upload_file(self, local_path: str, remote_path: str) -> None:
        try:
            if self._copy_to_bind_mount(local_path, remote_path):
                await self._make_runtime_path_writable(remote_path, recursive=False)
                return
        except PermissionError:
            pass
        parent = str(Path(remote_path).parent)
        await self._run_local_command(
            "docker", "exec", self._container_ref, "mkdir", "-p", parent
        )
        rc, _, _ = await self._run_local_command(
            "docker", "cp", local_path, f"{self._container_ref}:{remote_path}"
        )
        if rc != 0:
            raise RuntimeError(f"docker cp upload_file failed with exit code {rc}")
        await self._make_runtime_path_writable(remote_path, recursive=False)

    async def upload_dir(self, local_path: str, remote_path: str) -> None:
        try:
            if self._copy_to_bind_mount(local_path, remote_path):
                await self._make_runtime_path_writable(remote_path, recursive=True)
                return
        except PermissionError:
            pass
        await self._run_local_command(
            "docker", "exec", self._container_ref, "mkdir", "-p", remote_path
        )
        rc, _, _ = await self._run_local_command(
            "docker", "cp", f"{local_path}/.", f"{self._container_ref}:{remote_path}"
        )
        if rc != 0:
            raise RuntimeError(f"docker cp upload_dir failed with exit code {rc}")
        await self._make_runtime_path_writable(remote_path, recursive=True)

    async def _make_runtime_path_writable(
        self, remote_path: str, *, recursive: bool
    ) -> None:
        if self._chmod_needed is False:
            return
        chmod_args = ["chmod"]
        if recursive:
            chmod_args.append("-R")
        chmod_args.extend(["a+rwX", remote_path])
        rc, _, stderr = await self._run_local_command(
            "docker", "exec", "--user", "root",
            self._container_ref, *chmod_args,
            capture=True, timeout=self._STOP_TIMEOUT,
        )
        if rc != 0:
            raise RuntimeError(
                f"docker chmod failed for {remote_path} with exit code {rc}: {stderr}"
            )

    async def download_file(self, remote_path: str, local_path: str) -> None:
        try:
            if self._copy_from_bind_mount(remote_path, Path(local_path)):
                return
        except PermissionError:
            pass
        Path(local_path).parent.mkdir(parents=True, exist_ok=True)
        rc, _, _ = await self._run_local_command(
            "docker", "cp", f"{self._container_ref}:{remote_path}", local_path
        )
        if rc != 0:
            raise RuntimeError(f"docker cp download_file failed with exit code {rc}")

    async def download_dir(self, remote_path: str, local_path: str) -> None:
        try:
            if self._copy_from_bind_mount(remote_path, Path(local_path)):
                return
        except PermissionError:
            pass
        Path(local_path).parent.mkdir(parents=True, exist_ok=True)
        rc, _, _ = await self._run_local_command(
            "docker", "cp", f"{self._container_ref}:{remote_path}", local_path
        )
        if rc != 0:
            raise RuntimeError(f"docker cp download_dir failed with exit code {rc}")
