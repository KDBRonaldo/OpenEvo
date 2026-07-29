"""Fixed-command subprocess boundary for Daemon-owned SD-LoRA training."""

from __future__ import annotations

import importlib.util
import os
import signal
import stat
import subprocess
import sys
import threading
from collections import deque
from functools import lru_cache
from pathlib import Path

from openevo.evolution.framework.contracts import canonical_json

from .contracts import SdLoraTrainingRequest, SdLoraTrainingResult


_MAX_DIAGNOSTIC_BYTES = 64 * 1024
_MAX_RESULT_BYTES = 1024 * 1024
_READ_CHUNK_BYTES = 16 * 1024
_ENV_ALLOWLIST = frozenset(
    {
        "CUDA_VISIBLE_DEVICES",
        "HF_HOME",
        "HF_HUB_CACHE",
        "HF_HUB_OFFLINE",
        "HF_TOKEN",
        "HUGGING_FACE_HUB_TOKEN",
        "HOME",
        "HTTPS_PROXY",
        "HTTP_PROXY",
        "LANG",
        "LC_ALL",
        "NO_PROXY",
        "PATH",
        "TRANSFORMERS_CACHE",
        "TRANSFORMERS_OFFLINE",
    }
)


def _stable_file_identity(value: os.stat_result) -> tuple[int, ...]:
    return (
        value.st_dev,
        value.st_ino,
        value.st_mode,
        value.st_nlink,
        value.st_uid,
        value.st_gid,
        value.st_size,
        value.st_mtime_ns,
        value.st_ctime_ns,
    )


@lru_cache(maxsize=1)
def sd_lora_trainer_available() -> bool:
    """Return true only when this Daemon can execute local CUDA training."""

    required_modules = ("accelerate", "peft", "safetensors", "torch", "transformers")
    if any(importlib.util.find_spec(module) is None for module in required_modules):
        return False
    try:
        import torch
    except (ImportError, OSError):
        return False
    return bool(torch.cuda.is_available())


class _BoundedTail:
    def __init__(self, maximum_bytes: int) -> None:
        self._chunks: deque[bytes] = deque()
        self._size = 0
        self._maximum_bytes = maximum_bytes

    def append(self, chunk: bytes) -> None:
        if len(chunk) >= self._maximum_bytes:
            self._chunks.clear()
            self._chunks.append(chunk[-self._maximum_bytes :])
            self._size = self._maximum_bytes
            return
        self._chunks.append(chunk)
        self._size += len(chunk)
        while self._size > self._maximum_bytes:
            excess = self._size - self._maximum_bytes
            first = self._chunks.popleft()
            if len(first) > excess:
                self._chunks.appendleft(first[excess:])
                self._size -= excess
            else:
                self._size -= len(first)

    def text(self) -> str:
        return b"".join(self._chunks).decode("utf-8", errors="replace").strip()


def _drain_pipe(pipe: object, tail: _BoundedTail) -> None:
    read = getattr(pipe, "read")
    try:
        while chunk := read(_READ_CHUNK_BYTES):
            tail.append(chunk)
    finally:
        getattr(pipe, "close")()


def _private_regular_file(path: Path, *, maximum_bytes: int) -> bytes:
    directory_fd = os.open(
        path.parent,
        os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_DIRECTORY,
    )
    try:
        fd = os.open(
            path.name,
            os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW,
            dir_fd=directory_fd,
        )
        try:
            before = os.fstat(fd)
            if (
                not stat.S_ISREG(before.st_mode)
                or before.st_uid != os.geteuid()
                or before.st_nlink != 1
                or stat.S_IMODE(before.st_mode) != 0o600
                or before.st_size < 1
                or before.st_size > maximum_bytes
            ):
                raise ValueError("trainer result must be a bounded private regular file")
            remaining = before.st_size
            chunks: list[bytes] = []
            while remaining:
                chunk = os.read(fd, min(_READ_CHUNK_BYTES, remaining))
                if not chunk:
                    raise ValueError("trainer result ended before its observed size")
                chunks.append(chunk)
                remaining -= len(chunk)
            if os.read(fd, 1):
                raise ValueError("trainer result grew while being read")
            after = os.fstat(fd)
            if _stable_file_identity(after) != _stable_file_identity(before):
                raise ValueError("trainer result identity changed while being read")
            return b"".join(chunks)
        finally:
            os.close(fd)
    finally:
        os.close(directory_fd)


def _unlink_private_protocol_files(directory: Path, *names: str) -> None:
    directory_fd = os.open(
        directory,
        os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_DIRECTORY,
    )
    try:
        for name in names:
            if Path(name).name != name:
                raise ValueError("trainer protocol filename must be a direct child")
            try:
                os.unlink(name, dir_fd=directory_fd)
            except FileNotFoundError:
                continue
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)


def _write_private_file(path: Path, payload: bytes) -> None:
    fd = os.open(
        path,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | os.O_NOFOLLOW,
        0o600,
    )
    try:
        pending = memoryview(payload)
        while pending:
            written = os.write(fd, pending)
            if written <= 0:
                raise OSError("trainer request write made no progress")
            pending = pending[written:]
        os.fsync(fd)
    finally:
        os.close(fd)


class SubprocessSdLoraTrainerService:
    """Run the verified in-package trainer without a shell or command config."""

    def __init__(self, artifact_root: Path, *, python_executable: str | None = None) -> None:
        root = Path(os.path.abspath(artifact_root))
        root_stat = os.stat(root, follow_symlinks=False)
        if not stat.S_ISDIR(root_stat.st_mode) or root_stat.st_uid != os.geteuid():
            raise ValueError("parametric trainer artifact root must be an owned directory")
        self._artifact_root = root
        self._python_executable = python_executable or sys.executable

    def train_sd_lora(self, request: SdLoraTrainingRequest) -> SdLoraTrainingResult:
        request = SdLoraTrainingRequest.model_validate(request)
        work_dir = Path(request.work_dir)
        self._validate_work_dir(work_dir)
        request_path = work_dir / f".{request.request_id}.request.json"
        result_path = work_dir / f".{request.request_id}.result.json"
        _write_private_file(
            request_path,
            (canonical_json(request.model_dump(mode="json")) + "\n").encode("utf-8"),
        )

        command = (
            self._python_executable,
            "-I",
            "-m",
            "openevo.evolution.parametric.sd_lora_trainer",
            "--request",
            request_path.name,
            "--response",
            result_path.name,
        )
        environment = {
            key: value for key, value in os.environ.items() if key in _ENV_ALLOWLIST and value
        }
        environment.setdefault("LANG", "C.UTF-8")
        process = subprocess.Popen(
            command,
            cwd=work_dir,
            env=environment,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
        )
        if process.stdout is None or process.stderr is None:
            raise RuntimeError("parametric trainer did not expose diagnostic pipes")
        stdout_tail = _BoundedTail(_MAX_DIAGNOSTIC_BYTES)
        stderr_tail = _BoundedTail(_MAX_DIAGNOSTIC_BYTES)
        readers = (
            threading.Thread(
                target=_drain_pipe,
                args=(process.stdout, stdout_tail),
                daemon=True,
            ),
            threading.Thread(
                target=_drain_pipe,
                args=(process.stderr, stderr_tail),
                daemon=True,
            ),
        )
        for reader in readers:
            reader.start()
        try:
            return_code = process.wait(timeout=request.config.timeout_seconds)
        except subprocess.TimeoutExpired as exc:
            os.killpg(process.pid, signal.SIGKILL)
            process.wait()
            for reader in readers:
                reader.join(timeout=5.0)
            raise TimeoutError(
                "SD-LoRA trainer exceeded its configured timeout of "
                f"{request.config.timeout_seconds:g} seconds"
            ) from exc
        for reader in readers:
            reader.join(timeout=5.0)
        if any(reader.is_alive() for reader in readers):
            raise RuntimeError("SD-LoRA trainer diagnostic stream did not close")
        if return_code != 0:
            diagnostic = stderr_tail.text() or stdout_tail.text() or "no diagnostic output"
            raise RuntimeError(
                f"SD-LoRA trainer failed with exit code {return_code}: {diagnostic}"
            )

        self._validate_work_dir(work_dir)
        try:
            result = SdLoraTrainingResult.model_validate_json(
                _private_regular_file(result_path, maximum_bytes=_MAX_RESULT_BYTES)
            )
        except (ValueError, UnicodeDecodeError) as exc:
            raise ValueError("SD-LoRA trainer returned an invalid closed result") from exc
        if result.request_id != request.request_id:
            raise ValueError("SD-LoRA trainer result request identity does not match")
        if result.adapter_path != request.output_adapter_path:
            raise ValueError("SD-LoRA trainer returned an unexpected adapter path")
        expected_prefix = request.output_adapter_path + "/"
        if not result.state_manifest_path.startswith(expected_prefix) or not (
            result.state_weights_path.startswith(expected_prefix)
        ):
            raise ValueError("SD-LoRA state files must be contained in the adapter output")
        _unlink_private_protocol_files(
            work_dir,
            request_path.name,
            result_path.name,
        )
        return result

    def _validate_work_dir(self, work_dir: Path) -> None:
        normalized = Path(os.path.abspath(work_dir))
        try:
            if os.path.commonpath((self._artifact_root, normalized)) != str(self._artifact_root):
                raise ValueError("parametric trainer work directory escapes artifact root")
        except ValueError as exc:
            raise ValueError("parametric trainer work directory is invalid") from exc
        observed = os.stat(normalized, follow_symlinks=False)
        if (
            not stat.S_ISDIR(observed.st_mode)
            or observed.st_uid != os.geteuid()
            or stat.S_IMODE(observed.st_mode) != 0o700
        ):
            raise ValueError("parametric trainer work directory must be private and owned")


__all__ = ["SubprocessSdLoraTrainerService", "sd_lora_trainer_available"]
