"""Docker-backed rollout runtime."""

from __future__ import annotations

import asyncio
import fcntl
import json
import logging
import os
import re
import shlex
import signal
import stat
import shutil
from hashlib import sha256
from pathlib import Path
from typing import Final, Literal

from openevo.runtime.base import BaseRuntime, RuntimeDownloadOperation
from openevo.runtime.managed import (
    MANAGED_CODEX_HOME,
    ManagedCredentialMount,
    managed_runtime_image_release,
    require_immutable_managed_runtime_image,
    verified_managed_runtime_image_reference,
)
from openevo.runtime.models import ExecResult, RuntimeSpec

logger = logging.getLogger(__name__)

_CIDFILE_MAX_BYTES: Final[int] = 128
_CIDFILE_CREATE_PERMISSIONS: Final[int] = 0o666
_DIRECTORY_FLAGS: Final[int] = os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_DIRECTORY
_DEFAULT_OWNERSHIP_ROOT: Path = Path("/tmp") / f".openevo-core-docker-ownership-{os.geteuid()}"
_CONTAINER_ID_PATTERN: Final[re.Pattern[str]] = re.compile(r"^[0-9a-f]{64}$")
_OWNERSHIP_KEY_PATTERN: Final[re.Pattern[str]] = re.compile(r"^[0-9a-f]{64}$")
_OWNERSHIP_RECORD_LIMIT: Final[int] = 1024
_CREDENTIAL_VIEW_NAME: Final[str] = ".openevo-codex-home"
_IMAGE_INSPECT_MAX_BYTES: Final[int] = 1024 * 1024
_IMAGE_INSPECT_TIMEOUT_SECONDS: Final[float] = 10.0
_OwnershipState = Literal[
    "none",
    "create_pending",
    "unresolved",
    "candidate",
    "verified",
    "absent",
]


async def verify_managed_runtime_image_admission(spec: RuntimeSpec) -> None:
    """Synchronously verify the exact image authority carried by a managed request."""

    require_immutable_managed_runtime_image(
        profile=spec.profile,
        image=spec.image,
    )
    await _inspect_managed_runtime_image(
        profile=spec.profile,
        requested_image=spec.image,
    )


async def _inspect_managed_runtime_image(
    *,
    profile: str | None,
    requested_image: str,
) -> str:
    docker = shutil.which("docker", path=os.environ.get("PATH", os.defpath))
    if docker is None:
        raise RuntimeError("managed runtime image authority is unavailable")
    process = await asyncio.create_subprocess_exec(
        docker,
        "image",
        "inspect",
        requested_image,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env={
            key: os.environ[key]
            for key in ("HOME", "LANG", "LC_ALL", "PATH")
            if key in os.environ
        },
        cwd="/",
        start_new_session=True,
    )
    assert process.stdout is not None and process.stderr is not None
    aggregate = [0]
    stdout = bytearray()
    stderr = bytearray()

    async def drain(reader: asyncio.StreamReader, destination: bytearray) -> None:
        while chunk := await reader.read(64 * 1024):
            aggregate[0] += len(chunk)
            if aggregate[0] > _IMAGE_INSPECT_MAX_BYTES:
                raise RuntimeError("managed runtime image inspect output exceeded its limit")
            destination.extend(chunk)

    tasks = [
        asyncio.create_task(drain(process.stdout, stdout)),
        asyncio.create_task(drain(process.stderr, stderr)),
    ]
    try:
        await asyncio.wait_for(
            asyncio.gather(process.wait(), *tasks),
            timeout=_IMAGE_INSPECT_TIMEOUT_SECONDS,
        )
    except BaseException:
        if process.returncode is None:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            await process.wait()
        await asyncio.gather(*tasks, return_exceptions=True)
        raise
    if process.returncode != 0:
        raise RuntimeError("managed runtime image inspect failed")
    try:
        payload = json.loads(bytes(stdout).decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RuntimeError("managed runtime image inspect returned invalid JSON") from exc
    if not isinstance(payload, list) or len(payload) != 1 or not isinstance(payload[0], dict):
        raise RuntimeError("managed runtime image inspect was not singular")
    record = payload[0]
    config = record.get("Config")
    labels = config.get("Labels") if isinstance(config, dict) else None
    try:
        return verified_managed_runtime_image_reference(
            profile=profile,
            image=requested_image,
            image_id=record.get("Id"),
            repo_digests=record.get("RepoDigests"),
            labels=labels,
        )
    except ValueError as exc:
        raise RuntimeError(str(exc)) from exc


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


def _open_private_ownership_directory(path: Path) -> int:
    parts = path.parts
    if not parts or parts[0] != os.sep or any(part in {"", ".", ".."} for part in parts[1:]):
        raise RuntimeError("docker ownership directory is not private")
    descriptor = os.open(os.sep, _DIRECTORY_FLAGS)
    try:
        for part in parts[1:]:
            before = os.stat(part, dir_fd=descriptor, follow_symlinks=False)
            if not stat.S_ISDIR(before.st_mode):
                raise RuntimeError("docker ownership directory is not private")
            child_fd = os.open(part, _DIRECTORY_FLAGS, dir_fd=descriptor)
            opened_child = os.fstat(child_fd)
            if _object_identity(before) != _object_identity(opened_child):
                os.close(child_fd)
                raise RuntimeError("docker ownership directory is not private")
            os.close(descriptor)
            descriptor = child_fd

        opened = os.fstat(descriptor)
        if (
            not stat.S_ISDIR(opened.st_mode)
            or opened.st_uid != os.geteuid()
            or stat.S_IMODE(opened.st_mode) != 0o700
        ):
            raise RuntimeError("docker ownership directory is not private")
        result = descriptor
        descriptor = -1
        return result
    except OSError as exc:
        raise RuntimeError("docker ownership directory is not private") from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _require_private_lock(value: os.stat_result) -> None:
    if (
        not stat.S_ISREG(value.st_mode)
        or value.st_uid != os.geteuid()
        or value.st_nlink != 1
        or stat.S_IMODE(value.st_mode) != 0o600
        or value.st_size != 0
    ):
        raise RuntimeError("docker ownership lock is invalid")


def _acquire_ownership_lock(descriptor: int) -> None:
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as exc:
        raise RuntimeError("docker ownership lock is held by another process") from exc


def _require_container_id(container_id: str) -> None:
    if not _CONTAINER_ID_PATTERN.fullmatch(container_id):
        raise RuntimeError("docker ownership container ID is invalid")


def _paths_overlap(left: Path, right: Path) -> bool:
    try:
        left.relative_to(right)
        return True
    except ValueError:
        pass
    try:
        right.relative_to(left)
        return True
    except ValueError:
        return False


class DockerRuntime(BaseRuntime):
    """Long-lived Docker container used across init, run, and post-run."""

    def __init__(
        self,
        spec: RuntimeSpec,
        session_id: str,
        session_dir: Path,
        *,
        credential_mount: ManagedCredentialMount | None = None,
        ownership_root: Path | None = None,
    ) -> None:
        super().__init__(spec, session_id, session_dir)
        credential_dir = credential_mount.root if credential_mount is not None else None
        credential_docker_source = credential_dir
        if credential_dir is not None:
            if not credential_dir.is_absolute():
                raise ValueError("credential_dir must be absolute")
            if credential_docker_source is None or not credential_docker_source.is_absolute():
                raise ValueError("credential docker source must be absolute")
            try:
                credential_dir.relative_to(session_dir)
            except ValueError:
                pass
            else:
                raise ValueError("credential_dir must be outside the session tree")
        self._credential_dir = credential_dir
        self._credential_docker_source = credential_docker_source
        self._credential_mount = credential_mount
        self._credential_root_fd = -1
        self._credential_view_fd = -1
        self._credential_target_fd = -1
        self._credential_auth_fd = -1
        self._credential_view_identity: tuple[int, int, int, int] | None = None
        self._credential_target_identity: tuple[int, int, int, int, int, int, int, int] | None = (
            None
        )
        # Use enough of the session_id to preserve the "-eval" suffix used by
        # fresh evaluator runtimes, avoiding collisions with the agent runtime.
        safe_name = session_id.replace("/", "-")[:55]
        self._container_name = f"openevo-{safe_name}"
        self._container_id: str | None = None
        ownership_key = sha256(
            f"{session_id}\0{session_dir.absolute()}".encode("utf-8")
        ).hexdigest()
        if ownership_root is None:
            ownership_root = _DEFAULT_OWNERSHIP_ROOT
        if not ownership_root.is_absolute():
            raise ValueError("ownership_root must be absolute")
        absolute_session_dir = session_dir.absolute()
        if _paths_overlap(ownership_root, absolute_session_dir):
            raise ValueError("ownership_root must be outside the session tree")
        if credential_dir is not None and _paths_overlap(ownership_root, credential_dir):
            raise ValueError("ownership_root must be outside the credential tree")
        self._ownership_dir = ownership_root
        self._ownership_key = ownership_key
        self._cidfile = self._ownership_dir / f"{self._ownership_key}.cid"
        self._ownership_lock = self._ownership_dir / f"{self._ownership_key}.lock"
        self._ownership_lock_fd = -1
        self._ownership_root_identity: tuple[int, int] | None = None
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

    def _pin_credential_mount(self) -> None:
        authority = self._credential_mount
        if authority is None:
            return
        if any(
            descriptor >= 0
            for descriptor in (
                self._credential_root_fd,
                self._credential_view_fd,
                self._credential_target_fd,
                self._credential_auth_fd,
            )
        ):
            raise RuntimeError("managed credential mount is already pinned")

        root_fd = -1
        view_fd = -1
        target_fd = -1
        auth_fd = -1
        try:
            root_fd = _open_private_ownership_directory(authority.root)
            root_state = os.fstat(root_fd)
            if (
                root_state.st_dev,
                root_state.st_ino,
                root_state.st_uid,
            ) != authority.root_identity:
                raise RuntimeError("managed credential root identity changed")
            named_auth = os.stat(
                "auth.json",
                dir_fd=root_fd,
                follow_symlinks=False,
            )
            auth_fd = os.open(
                "auth.json",
                os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_NONBLOCK,
                dir_fd=root_fd,
            )
            opened_auth = os.fstat(auth_fd)
            if (
                _full_file_identity(named_auth) != authority.auth_identity
                or _full_file_identity(opened_auth) != authority.auth_identity
                or not stat.S_ISREG(opened_auth.st_mode)
                or opened_auth.st_uid != os.geteuid()
                or opened_auth.st_nlink != 1
                or stat.S_IMODE(opened_auth.st_mode) != 0o600
            ):
                raise RuntimeError("managed credential file identity changed")
            os.mkdir(_CREDENTIAL_VIEW_NAME, mode=0o700, dir_fd=root_fd)
            view_fd = os.open(_CREDENTIAL_VIEW_NAME, _DIRECTORY_FLAGS, dir_fd=root_fd)
            target_fd = os.open(
                "auth.json",
                os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | os.O_NOFOLLOW,
                0o600,
                dir_fd=view_fd,
            )
            os.fchmod(target_fd, 0o600)
            os.fsync(target_fd)
            os.fsync(view_fd)
            os.fsync(root_fd)
            view_state = os.fstat(view_fd)
            target_state = os.fstat(target_fd)
            named_view = os.stat(
                _CREDENTIAL_VIEW_NAME,
                dir_fd=root_fd,
                follow_symlinks=False,
            )
            named_target = os.stat(
                "auth.json",
                dir_fd=view_fd,
                follow_symlinks=False,
            )
            view_identity = (
                view_state.st_dev,
                view_state.st_ino,
                view_state.st_mode,
                view_state.st_uid,
            )
            if (
                not stat.S_ISDIR(view_state.st_mode)
                or view_state.st_uid != authority.root_identity[2]
                or stat.S_IMODE(view_state.st_mode) != 0o700
                or (
                    named_view.st_dev,
                    named_view.st_ino,
                    named_view.st_mode,
                    named_view.st_uid,
                )
                != view_identity
            ):
                raise RuntimeError("managed credential view is invalid")
            target_identity = _full_file_identity(target_state)
            if (
                not stat.S_ISREG(target_state.st_mode)
                or target_state.st_uid != authority.root_identity[2]
                or target_state.st_nlink != 1
                or stat.S_IMODE(target_state.st_mode) != 0o600
                or target_state.st_size != 0
                or _full_file_identity(named_target) != target_identity
            ):
                raise RuntimeError("managed credential target is invalid")
            self._credential_root_fd = root_fd
            self._credential_view_fd = view_fd
            self._credential_target_fd = target_fd
            self._credential_auth_fd = auth_fd
            self._credential_view_identity = view_identity
            self._credential_target_identity = target_identity
            root_fd = -1
            view_fd = -1
            target_fd = -1
            auth_fd = -1
            try:
                self._verify_credential_mount_pins()
            except Exception:
                self._release_credential_mount_pins()
                raise
        except OSError as exc:
            raise RuntimeError("managed credential mount could not be pinned") from exc
        finally:
            if target_fd >= 0:
                os.close(target_fd)
            if view_fd >= 0:
                os.close(view_fd)
            if auth_fd >= 0:
                os.close(auth_fd)
            if root_fd >= 0:
                os.close(root_fd)

    def _verify_credential_mount_pins(self) -> None:
        authority = self._credential_mount
        if (
            authority is None
            or self._credential_root_fd < 0
            or self._credential_view_fd < 0
            or self._credential_target_fd < 0
            or self._credential_auth_fd < 0
            or self._credential_view_identity is None
            or self._credential_target_identity is None
        ):
            raise RuntimeError("managed credential mount authority is incomplete")
        root_state = os.fstat(self._credential_root_fd)
        named_root = os.stat(authority.root, follow_symlinks=False)
        view_state = os.fstat(self._credential_view_fd)
        named_view = os.stat(
            _CREDENTIAL_VIEW_NAME,
            dir_fd=self._credential_root_fd,
            follow_symlinks=False,
        )
        target_state = os.fstat(self._credential_target_fd)
        named_target = os.stat(
            "auth.json",
            dir_fd=self._credential_view_fd,
            follow_symlinks=False,
        )
        auth_state = os.fstat(self._credential_auth_fd)
        named_auth = os.stat(
            "auth.json",
            dir_fd=self._credential_root_fd,
            follow_symlinks=False,
        )
        if (
            (root_state.st_dev, root_state.st_ino, root_state.st_uid) != authority.root_identity
            or (named_root.st_dev, named_root.st_ino, named_root.st_uid) != authority.root_identity
            or stat.S_IMODE(root_state.st_mode) != 0o700
            or stat.S_IMODE(named_root.st_mode) != 0o700
            or (
                view_state.st_dev,
                view_state.st_ino,
                view_state.st_mode,
                view_state.st_uid,
            )
            != self._credential_view_identity
            or (
                named_view.st_dev,
                named_view.st_ino,
                named_view.st_mode,
                named_view.st_uid,
            )
            != self._credential_view_identity
            or _full_file_identity(target_state) != self._credential_target_identity
            or _full_file_identity(named_target) != self._credential_target_identity
            or _full_file_identity(auth_state) != authority.auth_identity
            or _full_file_identity(named_auth) != authority.auth_identity
        ):
            raise RuntimeError("managed credential mount authority changed")

    def _release_credential_mount_pins(self) -> None:
        for name in (
            "_credential_auth_fd",
            "_credential_target_fd",
            "_credential_view_fd",
            "_credential_root_fd",
        ):
            descriptor = getattr(self, name)
            if descriptor >= 0:
                os.close(descriptor)
                setattr(self, name, -1)
        self._credential_view_identity = None
        self._credential_target_identity = None

    def _credential_mount_sources(self) -> tuple[str, str]:
        self._verify_credential_mount_pins()
        source = self._credential_docker_source
        if source is None or "," in str(source):
            raise RuntimeError("managed credential Docker source is invalid")
        auth_source = source / "auth.json"
        view_source = source / _CREDENTIAL_VIEW_NAME
        if "," in str(auth_source) or "," in str(view_source):
            raise RuntimeError("managed credential Docker auth source is invalid")
        return str(view_source), str(auth_source)

    async def _verify_created_credential_mount(self) -> None:
        authority = self._credential_mount
        if authority is None:
            return
        root_source, auth_source = self._credential_mount_sources()
        rc, stdout, _ = await self._run_local_command(
            "docker",
            "container",
            "inspect",
            "--format",
            "{{json .Mounts}}",
            self._container_ref,
            capture=True,
            timeout=self._STOP_TIMEOUT,
        )
        try:
            mounts = json.loads(str(stdout or ""))
        except json.JSONDecodeError as exc:
            raise RuntimeError("managed credential mount inspect returned invalid JSON") from exc
        destinations = {
            MANAGED_CODEX_HOME: root_source,
            f"{MANAGED_CODEX_HOME}/auth.json": auth_source,
        }
        matches = (
            [
                mount
                for mount in mounts
                if isinstance(mount, dict) and mount.get("Destination") in destinations
            ]
            if isinstance(mounts, list)
            else []
        )
        if (
            rc != 0
            or len(matches) != len(destinations)
            or {mount.get("Destination") for mount in matches} != set(destinations)
        ):
            raise RuntimeError("managed credential mount inspect is invalid")
        for mount in matches:
            destination = mount.get("Destination")
            if (
                mount.get("Type") != "bind"
                or mount.get("Source") != destinations[destination]
                or mount.get("RW") is not False
            ):
                raise RuntimeError("managed credential mount configuration changed")
        self._verify_credential_mount_pins()

    async def start(self) -> None:
        if self._destroyed:
            raise RuntimeError("docker runtime was already destroyed")
        create_image = await self._verified_create_image()
        if self._credential_mount is not None:
            self._pin_credential_mount()
        try:
            self._prepare_create_ownership()
        except Exception:
            self._release_credential_mount_pins()
            raise
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
        if self._credential_mount is not None:
            try:
                root_source, auth_source = self._credential_mount_sources()
            except Exception:
                self._mark_no_ownership()
                raise
            create_args.extend(
                [
                    "--mount",
                    f"type=bind,source={root_source},target={MANAGED_CODEX_HOME},readonly",
                    "--mount",
                    "type=bind,"
                    f"source={auth_source},"
                    f"target={MANAGED_CODEX_HOME}/auth.json,"
                    "readonly",
                ]
            )
        # Additional volumes from kwargs (e.g., Docker socket for agents that need DinD)
        for vol in self.spec.kwargs.get("volumes", []):
            create_args.extend(["-v", vol])
        create_args.extend(["--restart", "no", create_image, "sleep", "infinity"])
        try:
            rc, _, stderr = await self._run_local_command(
                *create_args,
                capture=True,
                timeout=self._START_TIMEOUT,
            )
        except asyncio.CancelledError:
            await self._reconcile_create_after_interruption(explicit_failure=False)
            raise
        except Exception:
            await self._reconcile_create_after_interruption(explicit_failure=False)
            raise

        self._create_succeeded = rc == 0
        try:
            await self._reconcile_create_ownership(explicit_failure=rc != 0)
        except asyncio.CancelledError:
            await self._reconcile_create_after_interruption(explicit_failure=rc != 0)
            raise
        except Exception as exc:
            raise RuntimeError(
                "docker create succeeded but container ownership could not be verified; "
                "cleanup/recovery state was retained"
            ) from exc
        if rc != 0:
            raise RuntimeError(f"docker create failed with exit code {rc}: {stderr}")
        if self._ownership_state != "verified":
            raise RuntimeError(
                "docker create succeeded but container ownership could not be verified; "
                "cleanup/recovery state was retained"
            )
        if self._credential_mount is not None:
            try:
                await self._verify_created_credential_mount()
            except Exception as exc:
                await self.stop()
                raise RuntimeError(
                    "docker did not preserve the verified credential mount configuration"
                ) from exc
            self._verify_credential_mount_pins()
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
        if self._credential_mount is not None:
            try:
                await self._verify_adopted_credential_mount()
            except Exception as exc:
                await self.stop()
                raise RuntimeError(
                    "docker did not adopt the verified credential mount authority"
                ) from exc
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

    async def _verified_create_image(self) -> str:
        release = managed_runtime_image_release(
            profile=self.spec.profile,
            image=self.spec.image,
        )
        if release is None:
            return self.spec.image
        rc, stdout, _ = await self._run_local_command(
            "docker",
            "image",
            "inspect",
            self.spec.image,
            capture=True,
            timeout=self._START_TIMEOUT,
        )
        if rc != 0:
            raise RuntimeError("managed runtime image inspect failed")
        try:
            payload = json.loads(str(stdout or ""))
        except json.JSONDecodeError as exc:
            raise RuntimeError("managed runtime image inspect returned invalid JSON") from exc
        if not isinstance(payload, list) or len(payload) != 1:
            raise RuntimeError("managed runtime image inspect was not singular")
        record = payload[0]
        if not isinstance(record, dict):
            raise RuntimeError("managed runtime image inspect record is invalid")
        config = record.get("Config")
        labels = config.get("Labels") if isinstance(config, dict) else None
        try:
            return verified_managed_runtime_image_reference(
                profile=self.spec.profile,
                image=self.spec.image,
                image_id=record.get("Id"),
                repo_digests=record.get("RepoDigests"),
                labels=labels,
            )
        except ValueError as exc:
            raise RuntimeError(str(exc)) from exc

    async def _verify_adopted_credential_mount(self) -> None:
        authority = self._credential_mount
        if authority is None:
            return
        self._verify_credential_mount_pins()
        first = await self._credential_container_process_identity()
        rc, stdout, _ = await self._run_local_command(
            "docker",
            "exec",
            self._container_ref,
            "/usr/bin/stat",
            "-Lc",
            "%d %i %f %u %h %s",
            MANAGED_CODEX_HOME,
            f"{MANAGED_CODEX_HOME}/auth.json",
            capture=True,
            timeout=self._STOP_TIMEOUT,
        )
        lines = str(stdout or "").strip().splitlines()
        if rc != 0 or len(lines) != 2:
            raise RuntimeError("managed credential mount stat failed")
        try:
            root_fields = lines[0].split()
            auth_fields = lines[1].split()
            if len(root_fields) != 6 or len(auth_fields) != 6:
                raise ValueError("unexpected stat field count")
            root_identity = (
                int(root_fields[0]),
                int(root_fields[1]),
                int(root_fields[2], 16),
                int(root_fields[3]),
            )
            auth_identity = (
                int(auth_fields[0]),
                int(auth_fields[1]),
                int(auth_fields[2], 16),
                int(auth_fields[3]),
                int(auth_fields[4]),
                int(auth_fields[5]),
            )
        except ValueError as exc:
            raise RuntimeError("managed credential mount stat is invalid") from exc
        view_identity = self._credential_view_identity
        if view_identity is None:
            raise RuntimeError("managed credential view authority is missing")
        if root_identity != view_identity or auth_identity != authority.auth_identity[:6]:
            raise RuntimeError("managed credential mount identity changed")
        self._verify_credential_mount_pins()
        if await self._credential_container_process_identity() != first:
            raise RuntimeError("managed credential container process changed")
        # A process restart can only re-adopt the held descriptor sources. This
        # final pathname check closes a replacement triggered by the last
        # process inspect without moving the adoption authority back to paths.
        self._verify_credential_mount_pins()

    async def _credential_container_process_identity(
        self,
    ) -> tuple[str, int, str, bool, int]:
        rc, stdout, _ = await self._run_local_command(
            "docker",
            "container",
            "inspect",
            "--format",
            "{{.Id}}|{{.State.Pid}}|{{.State.StartedAt}}|{{.State.Running}}|{{.RestartCount}}",
            self._container_ref,
            capture=True,
            timeout=self._STOP_TIMEOUT,
        )
        fields = str(stdout or "").strip().split("|")
        if (
            rc != 0
            or len(fields) != 5
            or fields[0] != self._container_ref
            or not fields[1].isdigit()
            or int(fields[1]) <= 0
            or not fields[2]
            or fields[3] != "true"
            or not fields[4].isdigit()
        ):
            raise RuntimeError("managed credential container process is invalid")
        return fields[0], int(fields[1]), fields[2], True, int(fields[4])

    _START_TIMEOUT = 600.0  # seconds for docker create / start under high rollout load
    _STOP_TIMEOUT = 30.0  # seconds per cleanup command
    _OWNERSHIP_RECONCILE_TIMEOUT = 35.0
    _RECOVERY_TIMEOUT = 125.0

    async def _reconcile_create_after_interruption(
        self,
        *,
        explicit_failure: bool,
    ) -> None:
        if self._ownership_state == "create_pending":
            self._ownership_state = "unresolved"
        reconciliation = asyncio.create_task(
            self._reconcile_create_ownership(explicit_failure=explicit_failure)
        )
        try:
            await asyncio.wait_for(
                reconciliation,
                timeout=self._OWNERSHIP_RECONCILE_TIMEOUT,
            )
        except asyncio.CancelledError:
            logger.warning(
                "docker create ownership reconciliation was cancelled for %s; "
                "private recovery state was retained",
                self._container_name,
            )
        except Exception as exc:
            if self._credential_mount is not None:
                logger.warning(
                    "docker create ownership reconciliation failed for %s; "
                    "private recovery state was retained [%s]",
                    self._container_name,
                    type(exc).__name__,
                )
            else:
                logger.warning(
                    "docker create ownership reconciliation failed for %s; "
                    "private recovery state was retained",
                    self._container_name,
                    exc_info=(type(exc), exc, exc.__traceback__),
                )

    async def _reconcile_create_ownership(self, *, explicit_failure: bool) -> None:
        try:
            container_id = self._read_created_container_id()
        except FileNotFoundError:
            if explicit_failure:
                self._mark_no_ownership()
            else:
                self._ownership_state = "unresolved"
            return
        except Exception:
            self._ownership_state = "unresolved"
            raise

        if self._container_id is not None and self._container_id != container_id:
            self._ownership_state = "unresolved"
            raise RuntimeError("docker ownership container ID changed")
        self._container_id = container_id
        self._ownership_state = "candidate"
        verification = await self._verify_container_id()
        if verification == "present":
            self._ownership_state = "verified"
        elif verification == "absent":
            self._mark_owned_container_absent()

    async def stop(self) -> None:
        if self._destroyed:
            return
        if self._container_id is None:
            if self._create_succeeded or self._ownership_state != "none":
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
        self._ownership_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        directory_fd = _open_private_ownership_directory(self._ownership_dir)
        self._ownership_root_identity = _object_identity(os.fstat(directory_fd))
        try:
            self._ownership_lock_fd = os.open(
                self._ownership_lock.name,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | os.O_NOFOLLOW,
                0o600,
                dir_fd=directory_fd,
            )
            os.fchmod(self._ownership_lock_fd, 0o600)
            _require_private_lock(os.fstat(self._ownership_lock_fd))
            _acquire_ownership_lock(self._ownership_lock_fd)
            self._ownership_state = "create_pending"
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
        directory_fd = _open_private_ownership_directory(self._ownership_dir)
        descriptor = -1
        cidfile_observed = False
        try:
            if (
                self._ownership_root_identity is None
                or _object_identity(os.fstat(directory_fd)) != self._ownership_root_identity
            ):
                raise RuntimeError("docker ownership directory identity changed")
            before = os.stat(
                self._cidfile.name,
                dir_fd=directory_fd,
                follow_symlinks=False,
            )
            cidfile_observed = True
            descriptor = os.open(
                self._cidfile.name,
                os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_NONBLOCK,
                dir_fd=directory_fd,
            )
            opened = os.fstat(descriptor)
            opened_mode = stat.S_IMODE(opened.st_mode)
            if (
                not stat.S_ISREG(opened.st_mode)
                or opened.st_uid != os.geteuid()
                or opened.st_nlink != 1
                or opened.st_size <= 0
                or opened.st_size > _CIDFILE_MAX_BYTES
                or _object_identity(before) != _object_identity(opened)
                or opened_mode & ~_CIDFILE_CREATE_PERMISSIONS
            ):
                raise RuntimeError("docker ownership cidfile is invalid")
            if opened_mode != 0o600:
                os.fchmod(descriptor, 0o600)
            trusted = os.fstat(descriptor)
            named_trusted = os.stat(
                self._cidfile.name,
                dir_fd=directory_fd,
                follow_symlinks=False,
            )
            if (
                not stat.S_ISREG(trusted.st_mode)
                or trusted.st_uid != os.geteuid()
                or trusted.st_nlink != 1
                or stat.S_IMODE(trusted.st_mode) != 0o600
                or trusted.st_size != opened.st_size
                or _object_identity(opened) != _object_identity(trusted)
                or _full_file_identity(trusted) != _full_file_identity(named_trusted)
            ):
                raise RuntimeError("docker ownership cidfile mode or identity is invalid")
            chunks: list[bytes] = []
            remaining = trusted.st_size
            while remaining:
                chunk = os.read(descriptor, remaining)
                if not chunk:
                    break
                chunks.append(chunk)
                remaining -= len(chunk)
            content = b"".join(chunks)
            after = os.fstat(descriptor)
            named_after = os.stat(
                self._cidfile.name,
                dir_fd=directory_fd,
                follow_symlinks=False,
            )
            if (
                remaining
                or os.read(descriptor, 1)
                or _full_file_identity(trusted) != _full_file_identity(after)
                or _full_file_identity(trusted) != _full_file_identity(named_after)
            ):
                raise RuntimeError("docker ownership cidfile changed during read")
            try:
                raw_container_id = content.decode("ascii")
            except UnicodeDecodeError as exc:
                raise RuntimeError("docker ownership cidfile is not ASCII") from exc
            container_id = (
                raw_container_id[:-1] if raw_container_id.endswith("\n") else raw_container_id
            )
            _require_container_id(container_id)
            self._cidfile_identity = _object_identity(trusted)
            return container_id
        except FileNotFoundError as exc:
            if not cidfile_observed:
                raise
            raise RuntimeError("docker ownership cidfile disappeared during verification") from exc
        except OSError as exc:
            raise RuntimeError("docker ownership cidfile is invalid") from exc
        finally:
            if descriptor >= 0:
                os.close(descriptor)
            os.close(directory_fd)

    @classmethod
    async def recover_ownership_root(cls, ownership_root: Path) -> None:
        """Remove containers authorized by complete private cidfile records."""

        if not ownership_root.is_absolute():
            raise ValueError("ownership_root must be absolute")
        try:
            os.stat(ownership_root, follow_symlinks=False)
        except FileNotFoundError:
            return

        directory_fd = _open_private_ownership_directory(ownership_root)
        try:
            root_identity = _object_identity(os.fstat(directory_fd))
            names = os.listdir(directory_fd)
            if len(names) > _OWNERSHIP_RECORD_LIMIT * 2:
                raise RuntimeError("docker ownership recovery exceeds the record limit")
            records: dict[str, set[str]] = {}
            for name in names:
                key, separator, suffix = name.rpartition(".")
                if (
                    separator != "."
                    or not _OWNERSHIP_KEY_PATTERN.fullmatch(key)
                    or suffix not in {"cid", "lock"}
                ):
                    raise RuntimeError("docker ownership recovery found an invalid record")
                records.setdefault(key, set()).add(suffix)
            if len(records) > _OWNERSHIP_RECORD_LIMIT:
                raise RuntimeError("docker ownership recovery exceeds the record limit")
            incomplete = [key for key, suffixes in records.items() if suffixes != {"cid", "lock"}]
            if incomplete:
                raise RuntimeError("docker ownership recovery found an incomplete record")
            named_after = os.stat(ownership_root, follow_symlinks=False)
            if _object_identity(named_after) != root_identity:
                raise RuntimeError("docker ownership directory changed during recovery")
        finally:
            os.close(directory_fd)

        for ownership_key in sorted(records):
            recovery_session_dir = (
                ownership_root.parent / f".openevo-docker-recovery-{ownership_key}"
            )
            runtime = cls(
                RuntimeSpec(
                    image="openevo-ownership-recovery",
                    container_user="host",
                ),
                f"recovered-{ownership_key[:24]}",
                recovery_session_dir,
                ownership_root=ownership_root,
            )
            runtime._set_ownership_key(ownership_key)
            try:
                runtime._claim_existing_ownership(expected_root_identity=root_identity)
                await asyncio.wait_for(runtime.stop(), timeout=cls._RECOVERY_TIMEOUT)
            finally:
                runtime._close_ownership_lock()

    def _set_ownership_key(self, ownership_key: str) -> None:
        if not _OWNERSHIP_KEY_PATTERN.fullmatch(ownership_key):
            raise RuntimeError("docker ownership key is invalid")
        self._ownership_key = ownership_key
        self._cidfile = self._ownership_dir / f"{ownership_key}.cid"
        self._ownership_lock = self._ownership_dir / f"{ownership_key}.lock"

    def _claim_existing_ownership(
        self,
        *,
        expected_root_identity: tuple[int, int],
    ) -> None:
        directory_fd = _open_private_ownership_directory(self._ownership_dir)
        try:
            if _object_identity(os.fstat(directory_fd)) != expected_root_identity:
                raise RuntimeError("docker ownership directory changed during recovery")
            self._ownership_root_identity = expected_root_identity
            before = os.stat(
                self._ownership_lock.name,
                dir_fd=directory_fd,
                follow_symlinks=False,
            )
            descriptor = os.open(
                self._ownership_lock.name,
                os.O_RDWR | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_NONBLOCK,
                dir_fd=directory_fd,
            )
            try:
                opened = os.fstat(descriptor)
                named_after = os.stat(
                    self._ownership_lock.name,
                    dir_fd=directory_fd,
                    follow_symlinks=False,
                )
                _require_private_lock(opened)
                if _object_identity(before) != _object_identity(opened) or _object_identity(
                    opened
                ) != _object_identity(named_after):
                    raise RuntimeError("docker ownership lock changed while it was opened")
                _acquire_ownership_lock(descriptor)
            except Exception:
                os.close(descriptor)
                raise
            self._ownership_lock_fd = descriptor
        finally:
            os.close(directory_fd)

        self._ownership_state = "create_pending"
        try:
            self._container_id = self._read_created_container_id()
        except Exception:
            self._ownership_state = "unresolved"
            raise
        self._create_succeeded = True
        self._ownership_state = "candidate"

    def _close_ownership_lock(self) -> None:
        if self._ownership_lock_fd >= 0:
            os.close(self._ownership_lock_fd)
            self._ownership_lock_fd = -1

    async def _verify_container_id(self) -> Literal["present", "absent", "unknown"]:
        container_id = self._container_id
        if container_id is None:
            return "unknown"
        _require_container_id(container_id)
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
        self._ownership_state = "absent"
        self._absence_proven = True
        self._destroyed = True
        self._release_ownership_files()

    def _release_ownership_files(self, *, directory_fd: int | None = None) -> None:
        opened_directory_fd = directory_fd
        if opened_directory_fd is None:
            try:
                opened_directory_fd = _open_private_ownership_directory(self._ownership_dir)
                if (
                    self._ownership_root_identity is None
                    or _object_identity(os.fstat(opened_directory_fd))
                    != self._ownership_root_identity
                ):
                    os.close(opened_directory_fd)
                    opened_directory_fd = -1
            except FileNotFoundError:
                opened_directory_fd = -1
            except RuntimeError:
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
            self._release_credential_mount_pins()

    async def _detect_chmod_needed(self) -> bool:
        """True unless the container's effective UID matches the host's."""
        rc, stdout, _ = await self._run_local_command(
            "docker",
            "exec",
            self._container_ref,
            "id",
            "-u",
            capture=True,
            timeout=self._STOP_TIMEOUT,
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
                shell_exports.append(f"export {key}={shlex.quote(str(effective_env[key]))};")
        wrapped_command = " ".join([*shell_exports, command])
        args.extend([self._container_ref, "bash", "-lc", wrapped_command])
        rc, stdout, stderr = await self._run_local_command(
            *args, timeout=timeout_sec, capture=True
        )
        return ExecResult(stdout=stdout, stderr=stderr, return_code=rc)

    async def upload_file(self, local_path: str, remote_path: str) -> None:
        if self._copy_to_bind_mount(local_path, remote_path):
            await self._make_runtime_path_writable(remote_path, recursive=False)
            return
        parent = str(Path(remote_path).parent)
        await self._run_local_command("docker", "exec", self._container_ref, "mkdir", "-p", parent)
        rc, _, _ = await self._run_local_command(
            "docker", "cp", local_path, f"{self._container_ref}:{remote_path}"
        )
        if rc != 0:
            raise RuntimeError(f"docker cp upload_file failed with exit code {rc}")
        await self._make_runtime_path_writable(remote_path, recursive=False)

    async def upload_dir(self, local_path: str, remote_path: str) -> None:
        if self._copy_to_bind_mount(local_path, remote_path):
            await self._make_runtime_path_writable(remote_path, recursive=True)
            return
        await self._run_local_command(
            "docker", "exec", self._container_ref, "mkdir", "-p", remote_path
        )
        rc, _, _ = await self._run_local_command(
            "docker", "cp", f"{local_path}/.", f"{self._container_ref}:{remote_path}"
        )
        if rc != 0:
            raise RuntimeError(f"docker cp upload_dir failed with exit code {rc}")
        await self._make_runtime_path_writable(remote_path, recursive=True)

    async def _make_runtime_path_writable(self, remote_path: str, *, recursive: bool) -> None:
        if self._chmod_needed is False:
            return
        chmod_args = ["chmod"]
        if recursive:
            chmod_args.append("-R")
        chmod_args.extend(["a+rwX", remote_path])
        rc, _, stderr = await self._run_local_command(
            "docker",
            "exec",
            "--user",
            "root",
            self._container_ref,
            *chmod_args,
            capture=True,
            timeout=self._STOP_TIMEOUT,
        )
        if rc != 0:
            raise RuntimeError(
                f"docker chmod failed for {remote_path} with exit code {rc}: {stderr}"
            )

    async def download_file(self, remote_path: str, local_path: str) -> None:
        if self._copy_from_bind_mount(remote_path, Path(local_path)):
            return
        Path(local_path).parent.mkdir(parents=True, exist_ok=True)
        rc, _, _ = await self._run_local_command(
            "docker", "cp", f"{self._container_ref}:{remote_path}", local_path
        )
        if rc != 0:
            raise RuntimeError(f"docker cp download_file failed with exit code {rc}")

    async def download_dir(self, remote_path: str, local_path: str) -> None:
        if self._copy_from_bind_mount(remote_path, Path(local_path)):
            return
        Path(local_path).parent.mkdir(parents=True, exist_ok=True)
        rc, _, _ = await self._run_local_command(
            "docker", "cp", f"{self._container_ref}:{remote_path}", local_path
        )
        if rc != 0:
            raise RuntimeError(f"docker cp download_dir failed with exit code {rc}")

    def _start_download_dir_operation(
        self, remote_path: str, local_path: str
    ) -> RuntimeDownloadOperation:
        return RuntimeDownloadOperation(self.download_dir(remote_path, local_path))
