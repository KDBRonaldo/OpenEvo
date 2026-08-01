"""Fixed-command subprocess boundary for Daemon-owned SD-LoRA training."""

from __future__ import annotations

import fcntl
import importlib.util
import json
import os
import re
import signal
import shutil
import stat
import subprocess
import sys
import threading
import time
from collections import deque
from functools import lru_cache
from pathlib import Path

from openevo.evolution.framework.contracts import canonical_json

from .contracts import (
    SdLoraTrainingRequest,
    SdLoraTrainingResult,
    TrainerCancellationSignal,
)


_MAX_DIAGNOSTIC_BYTES = 64 * 1024
_MAX_RESULT_BYTES = 1024 * 1024
_READ_CHUNK_BYTES = 16 * 1024
_ACTIVE_RECEIPT_SCHEMA = "openevo.sd_lora_active_process.v1"
_ACTIVE_RECEIPT_RE = re.compile(r"\.sd-lora-[0-9a-f]{24}\.active\.json")
_BOOT_ID_RE = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
_TRAINER_LOCK_NAME = ".sd-lora-trainer.lock"
_PARENT_PID_ENV = "OPENEVO_SD_LORA_PARENT_PID"
_PROCESS_POLL_SECONDS = 0.1
_PROCESS_STOP_SECONDS = 5.0
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
    return bool(torch.cuda.is_available() and torch.cuda.device_count() == 1)


def _boot_id() -> str:
    try:
        value = Path("/proc/sys/kernel/random/boot_id").read_text(encoding="ascii").strip()
    except (OSError, UnicodeError) as exc:
        raise RuntimeError("trainer process boot identity is unavailable") from exc
    if _BOOT_ID_RE.fullmatch(value) is None:
        raise RuntimeError("trainer process boot identity is invalid")
    return value


def _process_stat(pid: int) -> tuple[int, int, int]:
    try:
        payload = (Path("/proc") / str(pid) / "stat").read_text(encoding="ascii")
    except FileNotFoundError as exc:
        raise ProcessLookupError(pid) from exc
    except (OSError, UnicodeError) as exc:
        raise RuntimeError("trainer process identity cannot be read") from exc
    end = payload.rfind(")")
    fields = payload[end + 1 :].split() if end >= 0 else []
    try:
        process_group_id = int(fields[2])
        session_id = int(fields[3])
        start_time_ticks = int(fields[19])
    except (IndexError, ValueError) as exc:
        raise RuntimeError("trainer process identity is malformed") from exc
    if min(process_group_id, session_id, start_time_ticks) <= 0:
        raise RuntimeError("trainer process identity is malformed")
    return process_group_id, session_id, start_time_ticks


def _capture_process_receipt(pid: int, request_id: str) -> dict[str, object]:
    process_group_id, session_id, start_time_ticks = _process_stat(pid)
    if process_group_id != pid or session_id != pid:
        raise RuntimeError("trainer subprocess does not own an independent process group")
    return {
        "schema_version": _ACTIVE_RECEIPT_SCHEMA,
        "request_id": request_id,
        "pid": pid,
        "process_group_id": process_group_id,
        "session_id": session_id,
        "boot_id": _boot_id(),
        "start_time_ticks": start_time_ticks,
    }


def _validate_process_receipt(value: object) -> dict[str, object]:
    required = {
        "schema_version",
        "request_id",
        "pid",
        "process_group_id",
        "session_id",
        "boot_id",
        "start_time_ticks",
    }
    if not isinstance(value, dict) or set(value) != required:
        raise ValueError("trainer active-process receipt has an open or invalid shape")
    request_id = value.get("request_id")
    if (
        value.get("schema_version") != _ACTIVE_RECEIPT_SCHEMA
        or not isinstance(request_id, str)
        or re.fullmatch(r"sd-lora-[0-9a-f]{24}", request_id) is None
        or not isinstance(value.get("boot_id"), str)
        or _BOOT_ID_RE.fullmatch(str(value["boot_id"])) is None
    ):
        raise ValueError("trainer active-process receipt identity is invalid")
    for key in ("pid", "process_group_id", "session_id", "start_time_ticks"):
        item = value.get(key)
        if not isinstance(item, int) or isinstance(item, bool) or item <= 0:
            raise ValueError("trainer active-process receipt identity is invalid")
    if value["process_group_id"] != value["pid"] or value["session_id"] != value["pid"]:
        raise ValueError("trainer active-process receipt topology is invalid")
    return value


def _process_receipt_matches(receipt: dict[str, object]) -> bool:
    if receipt["boot_id"] != _boot_id():
        return False
    try:
        process_group_id, session_id, start_time_ticks = _process_stat(int(receipt["pid"]))
    except ProcessLookupError:
        return False
    return (
        process_group_id == receipt["process_group_id"]
        and session_id == receipt["session_id"]
        and start_time_ticks == receipt["start_time_ticks"]
    )


def _kill_receipted_process(receipt: dict[str, object]) -> None:
    if not _process_receipt_matches(receipt):
        return
    process_group_id = int(receipt["process_group_id"])
    try:
        os.killpg(process_group_id, signal.SIGKILL)
    except ProcessLookupError:
        return
    deadline = time.monotonic() + _PROCESS_STOP_SECONDS
    while _process_receipt_matches(receipt):
        if time.monotonic() >= deadline:
            raise RuntimeError("abandoned SD-LoRA trainer did not terminate")
        time.sleep(_PROCESS_POLL_SECONDS)


def _discard_private_work_dir(parent: Path, name: str) -> None:
    if not shutil.rmtree.avoids_symlink_attacks:
        raise RuntimeError("safe recursive trainer recovery is unavailable")
    parent_fd = os.open(
        parent,
        os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_DIRECTORY,
    )
    try:
        shutil.rmtree(name, dir_fd=parent_fd)
        os.fsync(parent_fd)
    finally:
        os.close(parent_fd)


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
    """Run and recover the verified in-package trainer under one owned GPU slot."""

    def __init__(self, artifact_root: Path, *, python_executable: str | None = None) -> None:
        root = Path(os.path.abspath(artifact_root))
        root_stat = os.stat(root, follow_symlinks=False)
        if not stat.S_ISDIR(root_stat.st_mode) or root_stat.st_uid != os.geteuid():
            raise ValueError("parametric trainer artifact root must be an owned directory")
        self._artifact_root = root
        self._python_executable = python_executable or sys.executable
        self._training_guard = threading.Lock()
        self._closed = True
        self._workers_root = root / "workers"
        self._workers_root.mkdir(mode=0o700, exist_ok=True)
        workers_stat = os.stat(self._workers_root, follow_symlinks=False)
        if (
            not stat.S_ISDIR(workers_stat.st_mode)
            or workers_stat.st_uid != os.geteuid()
            or stat.S_IMODE(workers_stat.st_mode) != 0o700
        ):
            raise ValueError("parametric trainer workers root must be private and owned")
        lock_path = self._workers_root / _TRAINER_LOCK_NAME
        lock_fd = os.open(
            lock_path,
            os.O_RDWR | os.O_CREAT | os.O_CLOEXEC | os.O_NOFOLLOW,
            0o600,
        )
        try:
            lock_stat = os.fstat(lock_fd)
            if (
                not stat.S_ISREG(lock_stat.st_mode)
                or lock_stat.st_uid != os.geteuid()
                or lock_stat.st_nlink != 1
                or stat.S_IMODE(lock_stat.st_mode) != 0o600
            ):
                raise ValueError("parametric trainer lock must be a private owned file")
            try:
                fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise RuntimeError(
                    "another SD-LoRA trainer service owns this artifact root"
                ) from exc
            self._lock_fd = lock_fd
            self._recover_abandoned_processes()
            self._closed = False
        except Exception:
            os.close(lock_fd)
            raise

    def close(self) -> None:
        with self._training_guard:
            if self._closed:
                return
            self._closed = True
            fcntl.flock(self._lock_fd, fcntl.LOCK_UN)
            os.close(self._lock_fd)

    def __enter__(self) -> "SubprocessSdLoraTrainerService":
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()

    def __del__(self) -> None:
        if not getattr(self, "_closed", True):
            try:
                self.close()
            except OSError:
                pass

    def _recover_abandoned_processes(self) -> None:
        abandoned: list[tuple[Path, Path]] = []
        with os.scandir(self._workers_root) as entries:
            for entry in entries:
                observed = entry.stat(follow_symlinks=False)
                if not stat.S_ISDIR(observed.st_mode):
                    continue
                if re.fullmatch(r"sd-lora-[0-9a-f]{32}", entry.name) is None:
                    continue
                if observed.st_uid != os.geteuid() or stat.S_IMODE(observed.st_mode) != 0o700:
                    raise ValueError("abandoned trainer work directory is not private")
                work_dir = Path(entry.path)
                receipts = [
                    child
                    for child in work_dir.iterdir()
                    if _ACTIVE_RECEIPT_RE.fullmatch(child.name)
                ]
                if len(receipts) > 1:
                    raise ValueError("trainer work directory has multiple active receipts")
                if receipts:
                    abandoned.append((work_dir, receipts[0]))
        for work_dir, receipt_path in abandoned:
            try:
                receipt_bytes = _private_regular_file(
                    receipt_path,
                    maximum_bytes=16 * 1024,
                )
                receipt = _validate_process_receipt(json.loads(receipt_bytes.decode("utf-8")))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise ValueError("trainer active-process receipt is invalid") from exc
            if receipt_bytes != (canonical_json(receipt) + "\n").encode("utf-8"):
                raise ValueError("trainer active-process receipt bytes are not canonical")
            if receipt_path.name != f".{receipt['request_id']}.active.json":
                raise ValueError("trainer active-process receipt filename is invalid")
            _kill_receipted_process(receipt)
            _discard_private_work_dir(self._workers_root, work_dir.name)

    def train_sd_lora(
        self,
        request: SdLoraTrainingRequest,
        *,
        cancellation: TrainerCancellationSignal | None = None,
    ) -> SdLoraTrainingResult:
        if cancellation is not None and cancellation.is_set():
            raise RuntimeError("SD-LoRA trainer was cancelled before launch")
        with self._training_guard:
            if self._closed:
                raise RuntimeError("parametric trainer service is closed")
            return self._train_sd_lora_locked(request, cancellation=cancellation)

    def _train_sd_lora_locked(
        self,
        request: SdLoraTrainingRequest,
        *,
        cancellation: TrainerCancellationSignal | None,
    ) -> SdLoraTrainingResult:
        request = SdLoraTrainingRequest.model_validate(request)
        work_dir = Path(request.work_dir)
        self._validate_work_dir(work_dir)
        request_path = work_dir / f".{request.request_id}.request.json"
        result_path = work_dir / f".{request.request_id}.result.json"
        active_path = work_dir / f".{request.request_id}.active.json"
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
        environment[_PARENT_PID_ENV] = str(os.getpid())
        process: subprocess.Popen[bytes] | None = None
        readers: tuple[threading.Thread, ...] = ()
        stdout_tail = _BoundedTail(_MAX_DIAGNOSTIC_BYTES)
        stderr_tail = _BoundedTail(_MAX_DIAGNOSTIC_BYTES)
        try:
            process = subprocess.Popen(
                command,
                cwd=work_dir,
                env=environment,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                close_fds=True,
                start_new_session=True,
            )
            if process.stdout is None or process.stderr is None:
                raise RuntimeError("parametric trainer did not expose diagnostic pipes")
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
            _write_private_file(
                active_path,
                (
                    canonical_json(_capture_process_receipt(process.pid, request.request_id))
                    + "\n"
                ).encode("utf-8"),
            )
            return_code = self._wait_for_process(
                process,
                cancellation=cancellation,
                timeout_seconds=request.config.timeout_seconds,
            )
            for reader in readers:
                reader.join(timeout=_PROCESS_STOP_SECONDS)
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
            return result
        finally:
            if process is not None and process.poll() is None:
                self._kill_process_group(process)
            for reader in readers:
                reader.join(timeout=_PROCESS_STOP_SECONDS)
            _unlink_private_protocol_files(
                work_dir,
                request_path.name,
                result_path.name,
                active_path.name,
            )

    @staticmethod
    def _wait_for_process(
        process: subprocess.Popen[bytes],
        *,
        cancellation: TrainerCancellationSignal | None,
        timeout_seconds: float,
    ) -> int:
        deadline = time.monotonic() + timeout_seconds
        while True:
            return_code = process.poll()
            if return_code is not None:
                return return_code
            if cancellation is not None and cancellation.is_set():
                SubprocessSdLoraTrainerService._kill_process_group(process)
                raise RuntimeError("SD-LoRA trainer was cancelled")
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                SubprocessSdLoraTrainerService._kill_process_group(process)
                raise TimeoutError(
                    "SD-LoRA trainer exceeded its configured timeout of "
                    f"{timeout_seconds:g} seconds"
                )
            wait_seconds = min(_PROCESS_POLL_SECONDS, remaining)
            if cancellation is None:
                time.sleep(wait_seconds)
            else:
                cancellation.wait(wait_seconds)

    @staticmethod
    def _kill_process_group(process: subprocess.Popen[bytes]) -> None:
        if process.poll() is not None:
            return
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        process.wait(timeout=_PROCESS_STOP_SECONDS)

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
