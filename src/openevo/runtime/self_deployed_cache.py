"""Bounded, content-verified cache preparation for release model profiles."""

from __future__ import annotations

from collections.abc import Callable
import ctypes
import errno
import hashlib
import os
from pathlib import Path
import secrets
import stat
import threading
import time
from typing import Protocol
from urllib.parse import quote
from urllib.request import Request, urlopen

from openevo import __version__

from .self_deployed import SelfDeployedModelFile, SelfDeployedModelProfile


_DIRECTORY_MODE = 0o700
_FILE_MODE = 0o600
_RENAME_NOREPLACE = 1
_READ_CHUNK_BYTES = 4 * 1024 * 1024
_PROGRESS_STEP_BYTES = 32 * 1024 * 1024


class SelfDeployedModelCacheError(RuntimeError):
    pass


class _HttpResponse(Protocol):
    headers: object

    def read(self, size: int = -1) -> bytes: ...

    def close(self) -> None: ...


class _Digest(Protocol):
    def update(self, payload: bytes) -> None: ...

    def hexdigest(self) -> str: ...


ModelDownloadOpener = Callable[[Request, float], _HttpResponse]
ModelPreparationProgress = Callable[[str], None]


def prepare_release_model_snapshot(
    *,
    cache_root: Path,
    profile: SelfDeployedModelProfile,
    deadline: float,
    cancellation: threading.Event | None = None,
    progress: ModelPreparationProgress | None = None,
    opener: ModelDownloadOpener | None = None,
) -> Path:
    """Return one exact private snapshot, downloading it once when absent."""

    profile.__post_init__()
    root = Path(os.path.abspath(os.fspath(cache_root)))
    _require_private_directory(root, "Self-Deployed model cache root")
    final = root / f"{profile.profile_id}-{profile.model_snapshot_manifest_sha256[:24]}"
    if final.exists():
        verify_release_model_snapshot(final, profile)
        _emit(progress, f"Model snapshot {profile.profile_id} is already verified.")
        return final

    staging = root / f".staging-{profile.profile_id}-{secrets.token_hex(12)}"
    staging.mkdir(mode=_DIRECTORY_MODE)
    _require_private_directory(staging, "Self-Deployed model staging root")
    try:
        total_bytes = sum(item.byte_size for item in profile.required_files)
        completed_bytes = 0
        fetch = opener or _open_model_download
        for index, item in enumerate(profile.required_files, start=1):
            _check_deadline(deadline, cancellation)
            _emit(
                progress,
                f"Downloading model file {index}/{len(profile.required_files)}: {item.path}",
            )
            destination = _prepare_destination(staging, item.path)
            downloaded = _download_exact_file(
                destination=destination,
                profile=profile,
                item=item,
                deadline=deadline,
                cancellation=cancellation,
                opener=fetch,
                progress=(
                    lambda observed, base=completed_bytes: _emit(
                        progress,
                        "Model download progress: "
                        f"{min(100, ((base + observed) * 100) // total_bytes)}% "
                        f"({base + observed}/{total_bytes} bytes).",
                    )
                ),
            )
            completed_bytes += downloaded
        _fsync_tree(staging)
        verify_release_model_snapshot(staging, profile)
        try:
            _rename_noreplace(root, staging.name, final.name)
        except FileExistsError:
            verify_release_model_snapshot(final, profile)
            _discard_created_tree(staging, profile)
        _fsync_directory(root)
        verify_release_model_snapshot(final, profile)
        _emit(progress, f"Model snapshot {profile.profile_id} is ready and verified.")
        return final
    except BaseException:
        _discard_created_tree(staging, profile)
        raise


def verify_release_model_snapshot(
    snapshot_root: Path,
    profile: SelfDeployedModelProfile,
) -> None:
    """Verify exact paths, ownership, modes, sizes, and release digests."""

    profile.__post_init__()
    root = Path(os.path.abspath(os.fspath(snapshot_root)))
    _require_private_directory(root, "Self-Deployed model snapshot")
    expected_files = {item.path: item for item in profile.required_files}
    observed_files: set[str] = set()
    observed_directories: set[str] = set()
    for current, directories, files in os.walk(root, topdown=True, followlinks=False):
        current_path = Path(current)
        _require_private_directory(current_path, "Self-Deployed model directory")
        for name in directories:
            child = current_path / name
            _require_private_directory(child, "Self-Deployed model directory")
            observed_directories.add(child.relative_to(root).as_posix())
        for name in files:
            child = current_path / name
            relative = child.relative_to(root).as_posix()
            identity = expected_files.get(relative)
            if identity is None:
                raise SelfDeployedModelCacheError(
                    "Self-Deployed model snapshot contains an unexpected file"
                )
            verify_release_model_file(child, identity)
            observed_files.add(relative)
    if observed_files != set(expected_files):
        raise SelfDeployedModelCacheError(
            "Self-Deployed model snapshot is missing a required file"
        )
    expected_directories = {
        parent.as_posix()
        for item in profile.required_files
        for parent in _relative_parents(Path(item.path))
    }
    if observed_directories != expected_directories:
        raise SelfDeployedModelCacheError(
            "Self-Deployed model snapshot directory inventory changed"
        )


def verify_release_model_file(path: Path, identity: SelfDeployedModelFile) -> None:
    descriptor = -1
    try:
        descriptor = os.open(
            path,
            os.O_RDONLY | os.O_CLOEXEC | getattr(os, "O_NOFOLLOW", 0),
        )
        initial = os.fstat(descriptor)
        if (
            not stat.S_ISREG(initial.st_mode)
            or initial.st_uid != os.geteuid()
            or initial.st_nlink != 1
            or stat.S_IMODE(initial.st_mode) != _FILE_MODE
            or initial.st_size != identity.byte_size
        ):
            raise SelfDeployedModelCacheError(
                "Self-Deployed model file metadata differs from the release manifest"
            )
        digest = _file_digest(descriptor, identity)
        final = os.fstat(descriptor)
        named = os.stat(path, follow_symlinks=False)
        if (
            _file_identity(initial) != _file_identity(final)
            or _file_identity(final) != _file_identity(named)
            or digest != identity.digest
        ):
            raise SelfDeployedModelCacheError(
                "Self-Deployed model file content or binding changed"
            )
    except OSError as exc:
        raise SelfDeployedModelCacheError(
            "Self-Deployed model file could not be opened safely"
        ) from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _download_exact_file(
    *,
    destination: Path,
    profile: SelfDeployedModelProfile,
    item: SelfDeployedModelFile,
    deadline: float,
    cancellation: threading.Event | None,
    opener: ModelDownloadOpener,
    progress: Callable[[int], None],
) -> int:
    url = (
        "https://huggingface.co/"
        f"{quote(profile.model_id, safe='/')}/resolve/{profile.model_revision}/"
        f"{quote(item.path, safe='/')}?download=true"
    )
    request = Request(
        url,
        method="GET",
        headers={
            "Accept": "application/octet-stream",
            "Accept-Encoding": "identity",
            "User-Agent": f"EvoLab/{__version__}",
        },
    )
    response: _HttpResponse | None = None
    descriptor = -1
    try:
        _check_deadline(deadline, cancellation)
        response = opener(request, max(1.0, min(30.0, deadline - time.monotonic())))
        descriptor = os.open(
            destination,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | getattr(os, "O_NOFOLLOW", 0),
            _FILE_MODE,
        )
        digest = _new_digest(item)
        observed = 0
        next_progress = _PROGRESS_STEP_BYTES
        while observed < item.byte_size:
            _check_deadline(deadline, cancellation)
            chunk = response.read(min(_READ_CHUNK_BYTES, item.byte_size - observed + 1))
            if not chunk:
                break
            observed += len(chunk)
            if observed > item.byte_size:
                raise SelfDeployedModelCacheError(
                    "Self-Deployed model download exceeded its declared size"
                )
            _write_all(descriptor, chunk)
            digest.update(chunk)
            if observed >= next_progress:
                progress(observed)
                next_progress += _PROGRESS_STEP_BYTES
        if observed != item.byte_size or response.read(1):
            raise SelfDeployedModelCacheError(
                "Self-Deployed model download size differs from the release manifest"
            )
        if digest.hexdigest() != item.digest:
            raise SelfDeployedModelCacheError(
                "Self-Deployed model download digest differs from the release manifest"
            )
        os.fsync(descriptor)
        progress(observed)
        return observed
    except OSError as exc:
        raise SelfDeployedModelCacheError("Self-Deployed model download failed") from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        if response is not None:
            response.close()


def _open_model_download(request: Request, timeout: float) -> _HttpResponse:
    return urlopen(request, timeout=timeout)


def _new_digest(item: SelfDeployedModelFile) -> _Digest:
    if item.digest_algorithm == "sha256":
        return hashlib.sha256()
    digest = hashlib.sha1()
    digest.update(f"blob {item.byte_size}\0".encode("ascii"))
    return digest


def _file_digest(descriptor: int, identity: SelfDeployedModelFile) -> str:
    digest = _new_digest(identity)
    observed = 0
    while observed < identity.byte_size:
        chunk = os.pread(
            descriptor,
            min(_READ_CHUNK_BYTES, identity.byte_size - observed),
            observed,
        )
        if not chunk:
            break
        observed += len(chunk)
        digest.update(chunk)
    if observed != identity.byte_size or os.pread(descriptor, 1, identity.byte_size):
        raise SelfDeployedModelCacheError("Self-Deployed model file size changed")
    return digest.hexdigest()


def _prepare_destination(root: Path, relative: str) -> Path:
    current = root
    parts = Path(relative).parts
    for part in parts[:-1]:
        current = current / part
        try:
            current.mkdir(mode=_DIRECTORY_MODE)
        except FileExistsError:
            pass
        _require_private_directory(current, "Self-Deployed model directory")
    return current / parts[-1]


def _relative_parents(path: Path) -> tuple[Path, ...]:
    parents: list[Path] = []
    current = path.parent
    while current != Path("."):
        parents.append(current)
        current = current.parent
    return tuple(parents)


def _require_private_directory(path: Path, label: str) -> None:
    try:
        metadata = os.lstat(path)
    except OSError as exc:
        raise SelfDeployedModelCacheError(f"{label} is unavailable") from exc
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or metadata.st_uid != os.geteuid()
        or metadata.st_nlink < 2
        or stat.S_IMODE(metadata.st_mode) != _DIRECTORY_MODE
        or path.resolve(strict=True) != path
    ):
        raise SelfDeployedModelCacheError(f"{label} is not a private directory")


def _fsync_tree(root: Path) -> None:
    for current, directories, _files in os.walk(root, topdown=False, followlinks=False):
        for name in directories:
            _fsync_directory(Path(current) / name)
        _fsync_directory(Path(current))


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(
        path,
        os.O_RDONLY | os.O_CLOEXEC | getattr(os, "O_DIRECTORY", 0),
    )
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _rename_noreplace(root: Path, source: str, destination: str) -> None:
    descriptor = os.open(
        root,
        os.O_RDONLY | os.O_CLOEXEC | getattr(os, "O_DIRECTORY", 0),
    )
    try:
        libc = ctypes.CDLL(None, use_errno=True)
        renameat2 = getattr(libc, "renameat2", None)
        if renameat2 is None:
            raise SelfDeployedModelCacheError(
                "Self-Deployed model cache requires atomic no-replace rename"
            )
        result = renameat2(
            descriptor,
            os.fsencode(source),
            descriptor,
            os.fsencode(destination),
            _RENAME_NOREPLACE,
        )
        if result != 0:
            error_number = ctypes.get_errno()
            if error_number == errno.EEXIST:
                raise FileExistsError(destination)
            raise OSError(error_number, os.strerror(error_number), destination)
    finally:
        os.close(descriptor)


def _discard_created_tree(root: Path, profile: SelfDeployedModelProfile) -> None:
    if not root.exists():
        return
    try:
        _require_private_directory(root, "Self-Deployed model staging root")
        for item in reversed(profile.required_files):
            candidate = root / item.path
            try:
                metadata = os.lstat(candidate)
            except FileNotFoundError:
                continue
            if (
                not stat.S_ISREG(metadata.st_mode)
                or metadata.st_uid != os.geteuid()
                or metadata.st_nlink != 1
            ):
                raise SelfDeployedModelCacheError(
                    "Self-Deployed staging cleanup encountered unexpected state"
                )
            candidate.unlink()
        directories = sorted(
            {
                root / parent
                for item in profile.required_files
                for parent in _relative_parents(Path(item.path))
            },
            key=lambda value: len(value.parts),
            reverse=True,
        )
        for directory in directories:
            if directory.exists():
                _require_private_directory(directory, "Self-Deployed model directory")
                directory.rmdir()
        root.rmdir()
    except (OSError, SelfDeployedModelCacheError):
        quarantine = root.parent / f".quarantine-{secrets.token_hex(16)}"
        try:
            _rename_noreplace(root.parent, root.name, quarantine.name)
            _fsync_directory(root.parent)
        except (OSError, SelfDeployedModelCacheError, FileExistsError):
            pass


def _check_deadline(
    deadline: float,
    cancellation: threading.Event | None,
) -> None:
    if cancellation is not None and cancellation.is_set():
        raise SelfDeployedModelCacheError("Self-Deployed model preparation was cancelled")
    if time.monotonic() >= deadline:
        raise SelfDeployedModelCacheError("Self-Deployed model preparation timed out")


def _write_all(descriptor: int, payload: bytes) -> None:
    view = memoryview(payload)
    while view:
        written = os.write(descriptor, view)
        if written <= 0:
            raise OSError("Self-Deployed model cache write made no progress")
        view = view[written:]


def _file_identity(metadata: os.stat_result) -> tuple[int, int, int, int, int, int]:
    return (
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_mode,
        metadata.st_uid,
        metadata.st_nlink,
        metadata.st_size,
    )


def _emit(progress: ModelPreparationProgress | None, message: str) -> None:
    if progress is not None:
        progress(message)


__all__ = [
    "SelfDeployedModelCacheError",
    "prepare_release_model_snapshot",
    "verify_release_model_file",
    "verify_release_model_snapshot",
]
