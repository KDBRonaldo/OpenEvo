from __future__ import annotations

import io
import json
import os
from pathlib import Path
import resource
import subprocess
import sys
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from openevo.evolution.parametric.contracts import (
    SD_LORA_RESULT_SCHEMA,
    SdLoraMethodConfig,
    SdLoraTrainingRequest,
)
from openevo.evolution.framework.contracts import canonical_json
from openevo.evolution.parametric import sd_lora_trainer as trainer_module
from openevo.evolution.parametric.trainer_service import (
    SubprocessSdLoraTrainerService,
    _stable_file_identity as _service_file_identity,
)
from openevo.evolution.parametric.sd_lora_trainer import (
    _stable_file_identity as _trainer_file_identity,
)


_MODEL_REVISION = "0123456789abcdef0123456789abcdef01234567"
_BOOT_ID = "01234567-89ab-cdef-0123-456789abcdef"


def _process_receipt(pid: int, request_id: str) -> dict[str, object]:
    return {
        "schema_version": "openevo.sd_lora_active_process.v1",
        "request_id": request_id,
        "pid": pid,
        "process_group_id": pid,
        "session_id": pid,
        "boot_id": _BOOT_ID,
        "start_time_ticks": 123,
    }


@pytest.mark.parametrize(
    "identity",
    [_service_file_identity, _trainer_file_identity],
)
def test_file_identity_ignores_read_induced_atime_changes(identity) -> None:
    values = {
        "st_dev": 1,
        "st_ino": 2,
        "st_mode": 0o100600,
        "st_nlink": 1,
        "st_uid": os.geteuid(),
        "st_gid": os.getegid(),
        "st_size": 64,
        "st_mtime_ns": 10,
        "st_ctime_ns": 11,
    }
    before = SimpleNamespace(**values, st_atime_ns=12)
    after = SimpleNamespace(**values, st_atime_ns=13)

    assert identity(before) == identity(after)


def _request(work_dir: Path, *, timeout_seconds: float = 60.0) -> SdLoraTrainingRequest:
    training = work_dir / "training.jsonl"
    training.write_text(
        json.dumps(
            {
                "messages": [
                    {"content": "Question", "role": "user"},
                    {"content": "Answer", "role": "assistant"},
                ]
            },
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    training.chmod(0o600)
    return SdLoraTrainingRequest(
        request_id="sd-lora-test",
        work_dir=str(work_dir),
        training_data_path="training.jsonl",
        output_adapter_path="adapter",
        adapter_id="sd-lora-test",
        source_dataset_artifact_ids=("dataset-test",),
        training_record_count=1,
        config=SdLoraMethodConfig(
            base_model="Qwen/Qwen3-0.6B",
            model_revision=_MODEL_REVISION,
            timeout_seconds=timeout_seconds,
        ),
    )


def test_cuda_runtime_is_initialized_before_peak_memory_reset() -> None:
    calls: list[str] = []

    class _Cuda:
        @staticmethod
        def is_available() -> bool:
            return True

        @staticmethod
        def device_count() -> int:
            return 1

        @staticmethod
        def init() -> None:
            calls.append("init")

        @staticmethod
        def reset_peak_memory_stats() -> None:
            assert calls == ["init"]
            calls.append("reset")

    trainer_module._initialize_cuda_runtime(SimpleNamespace(cuda=_Cuda()))

    assert calls == ["init", "reset"]


class _SuccessfulProcess:
    def __init__(self, command, **kwargs) -> None:
        self.command = command
        self.kwargs = kwargs
        self.pid = 43210
        self.stdout = io.BytesIO(b"local trainer output\n")
        self.stderr = io.BytesIO(b"")
        response_name = command[command.index("--response") + 1]
        request_name = command[command.index("--request") + 1]
        request = json.loads((Path(kwargs["cwd"]) / request_name).read_text(encoding="utf-8"))
        response = {
            "adapter_path": "adapter",
            "coefficients": [0.8],
            "component_count": 1,
            "effective_rank": 8,
            "request_id": request["request_id"],
            "replay_buffer_record_count": 1,
            "replay_data_path": "adapter/openevo_sd_lora_replay.jsonl",
            "replay_training_record_count": 0,
            "schema_version": SD_LORA_RESULT_SCHEMA,
            "state_manifest_path": "adapter/openevo_sd_lora_state.json",
            "state_weights_path": "adapter/openevo_sd_lora_state.safetensors",
            "steps_completed": 1,
            "training_time_seconds": 1.0,
            "gpu_peak_memory_bytes": 1024,
            "target_module_names": ["model.layers.0.self_attn.q_proj"],
            "task_index": 0,
            "training_loss": 0.5,
            "optimizer_training_record_count": 1,
            "training_record_count": 1,
        }
        response_path = Path(kwargs["cwd"]) / response_name
        response_path.write_text(json.dumps(response, sort_keys=True) + "\n", encoding="utf-8")
        response_path.chmod(0o600)
        self.returncode = 0

    def poll(self):
        return self.returncode

    def wait(self, timeout=None):
        return self.returncode


def test_subprocess_trainer_uses_fixed_module_and_closed_environment(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    artifact_root = tmp_path / "artifacts"
    artifact_root.mkdir(mode=0o700)
    work_dir = artifact_root / "worker"
    work_dir.mkdir(mode=0o700)
    observed: list[_SuccessfulProcess] = []

    def popen(command, **kwargs):
        process = _SuccessfulProcess(command, **kwargs)
        observed.append(process)
        return process

    monkeypatch.setattr(subprocess, "Popen", popen)
    monkeypatch.setattr(
        "openevo.evolution.parametric.trainer_service._capture_process_receipt",
        _process_receipt,
    )
    monkeypatch.setenv("OPENAI_API_KEY", "must-not-leak")
    monkeypatch.setenv("OPENEVO_UNTRUSTED", "must-not-leak")
    monkeypatch.setenv("CUDA_VISIBLE_DEVICES", "3")
    monkeypatch.setenv("HF_HUB_OFFLINE", "1")

    with SubprocessSdLoraTrainerService(artifact_root) as trainer:
        result = trainer.train_sd_lora(_request(work_dir))

    assert result.request_id == "sd-lora-test"
    assert not (work_dir / ".sd-lora-test.request.json").exists()
    assert not (work_dir / ".sd-lora-test.result.json").exists()
    assert not (work_dir / ".sd-lora-test.active.json").exists()
    assert len(observed) == 1
    process = observed[0]
    assert process.command[:4] == (
        sys.executable,
        "-I",
        "-m",
        "openevo.evolution.parametric.sd_lora_trainer",
    )
    assert process.kwargs["stdin"] is subprocess.DEVNULL
    assert process.kwargs["close_fds"] is True
    assert process.kwargs["start_new_session"] is True
    assert process.kwargs["env"]["CUDA_VISIBLE_DEVICES"] == "3"
    assert process.kwargs["env"]["HF_HUB_OFFLINE"] == "1"
    assert process.kwargs["env"]["OPENEVO_SD_LORA_PARENT_PID"] == str(os.getpid())
    assert "OPENAI_API_KEY" not in process.kwargs["env"]
    assert "OPENEVO_UNTRUSTED" not in process.kwargs["env"]


class _TimeoutProcess:
    def __init__(self, command, **kwargs) -> None:
        self.command = command
        self.kwargs = kwargs
        self.pid = 54321
        self.stdout = io.BytesIO(b"")
        self.stderr = io.BytesIO(b"")
        self.returncode = None

    def poll(self):
        return self.returncode

    def wait(self, timeout=None):
        if self.returncode is None:
            raise subprocess.TimeoutExpired(self.command, timeout)
        return self.returncode


def test_subprocess_trainer_kills_the_process_group_on_timeout(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    artifact_root = tmp_path / "artifacts"
    artifact_root.mkdir(mode=0o700)
    work_dir = artifact_root / "worker"
    work_dir.mkdir(mode=0o700)
    process: _TimeoutProcess | None = None

    def popen(command, **kwargs):
        nonlocal process
        process = _TimeoutProcess(command, **kwargs)
        return process

    killed: list[tuple[int, int]] = []
    monkeypatch.setattr(subprocess, "Popen", popen)
    monkeypatch.setattr(
        "openevo.evolution.parametric.trainer_service._capture_process_receipt",
        _process_receipt,
    )

    def killpg(pid, sig):
        killed.append((pid, sig))
        assert process is not None
        process.returncode = -sig

    monkeypatch.setattr(os, "killpg", killpg)

    with pytest.raises(TimeoutError, match="configured timeout"):
        with SubprocessSdLoraTrainerService(artifact_root) as trainer:
            trainer.train_sd_lora(_request(work_dir, timeout_seconds=0.01))

    assert process is not None
    assert killed == [(process.pid, 9)]


def test_subprocess_trainer_kills_the_process_group_on_cancellation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    artifact_root = tmp_path / "artifacts"
    artifact_root.mkdir(mode=0o700)
    work_dir = artifact_root / "worker"
    work_dir.mkdir(mode=0o700)
    process: _TimeoutProcess | None = None

    def popen(command, **kwargs):
        nonlocal process
        process = _TimeoutProcess(command, **kwargs)
        return process

    class _Cancellation:
        calls = 0

        def is_set(self) -> bool:
            self.calls += 1
            return self.calls >= 2

        def wait(self, timeout=None) -> bool:
            del timeout
            return self.is_set()

    killed: list[tuple[int, int]] = []
    monkeypatch.setattr(subprocess, "Popen", popen)
    monkeypatch.setattr(
        "openevo.evolution.parametric.trainer_service._capture_process_receipt",
        _process_receipt,
    )

    def killpg(pid, sig):
        killed.append((pid, sig))
        assert process is not None
        process.returncode = -sig

    monkeypatch.setattr(os, "killpg", killpg)

    with pytest.raises(RuntimeError, match="was cancelled"):
        with SubprocessSdLoraTrainerService(artifact_root) as trainer:
            trainer.train_sd_lora(
                _request(work_dir),
                cancellation=_Cancellation(),
            )

    assert process is not None
    assert killed == [(process.pid, 9)]


def test_trainer_service_exclusively_owns_one_artifact_root(tmp_path: Path) -> None:
    artifact_root = tmp_path / "artifacts"
    artifact_root.mkdir(mode=0o700)

    first = SubprocessSdLoraTrainerService(artifact_root)
    try:
        with pytest.raises(RuntimeError, match="another SD-LoRA trainer service"):
            SubprocessSdLoraTrainerService(artifact_root)
    finally:
        first.close()

    with SubprocessSdLoraTrainerService(artifact_root):
        pass


def test_trainer_service_recovers_exact_receipted_process_and_workdir(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    artifact_root = tmp_path / "artifacts"
    artifact_root.mkdir(mode=0o700)
    workers_root = artifact_root / "workers"
    workers_root.mkdir(mode=0o700)
    work_dir = workers_root / f"sd-lora-{'a' * 32}"
    work_dir.mkdir(mode=0o700)
    request_id = f"sd-lora-{'b' * 24}"
    receipt = _process_receipt(65432, request_id)
    receipt_path = work_dir / f".{request_id}.active.json"
    receipt_path.write_text(canonical_json(receipt) + "\n", encoding="utf-8")
    receipt_path.chmod(0o600)
    killed: list[dict[str, object]] = []
    monkeypatch.setattr(
        "openevo.evolution.parametric.trainer_service._kill_receipted_process",
        lambda value: killed.append(value),
    )

    with SubprocessSdLoraTrainerService(artifact_root):
        pass

    assert killed == [receipt]
    assert not work_dir.exists()


def test_trainer_installs_linux_parent_death_signal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    parent_pid = 4321
    calls: list[tuple[int, ...]] = []

    class _LibC:
        def prctl(self, *args):
            calls.append(args)
            return 0

    monkeypatch.setenv("OPENEVO_SD_LORA_PARENT_PID", str(parent_pid))
    monkeypatch.setattr(os, "getppid", lambda: parent_pid)
    monkeypatch.setattr(trainer_module.ctypes, "CDLL", lambda *_args, **_kwargs: _LibC())

    trainer_module._install_parent_death_signal()

    assert calls == [(1, 9, 0, 0, 0)]
    assert "OPENEVO_SD_LORA_PARENT_PID" not in os.environ


def test_trainer_applies_closed_resource_limits(monkeypatch: pytest.MonkeyPatch) -> None:
    hard_limits = {
        resource.RLIMIT_FSIZE: resource.RLIM_INFINITY,
        resource.RLIMIT_NOFILE: resource.RLIM_INFINITY,
        resource.RLIMIT_CPU: resource.RLIM_INFINITY,
    }
    applied: list[tuple[int, tuple[int, int]]] = []
    monkeypatch.setattr(
        trainer_module.resource,
        "getrlimit",
        lambda kind: (0, hard_limits[kind]),
    )
    monkeypatch.setattr(
        trainer_module.resource,
        "setrlimit",
        lambda kind, value: applied.append((kind, value)),
    )

    trainer_module._apply_resource_limits(2.1)

    assert applied == [
        (resource.RLIMIT_CORE, (0, 0)),
        (resource.RLIMIT_FSIZE, (16 * 1024 * 1024 * 1024,) * 2),
        (resource.RLIMIT_NOFILE, (1024, 1024)),
        (resource.RLIMIT_CPU, (63, 63)),
    ]


def test_sd_lora_contract_rejects_commands_and_path_traversal(tmp_path: Path) -> None:
    with pytest.raises(ValidationError, match="trainer_command"):
        SdLoraMethodConfig.model_validate(
            {
                "base_model": "Qwen/Qwen3-0.6B",
                "model_revision": _MODEL_REVISION,
                "trainer_command": "python untrusted.py",
            }
        )

    work_dir = tmp_path / "work"
    work_dir.mkdir(mode=0o700)
    payload = _request(work_dir).model_dump(mode="python")
    payload["output_adapter_path"] = "../escape"
    with pytest.raises(ValidationError, match="normalized POSIX relative path"):
        SdLoraTrainingRequest.model_validate(payload)

    with pytest.raises(ValidationError, match="full immutable hexadecimal revision"):
        SdLoraMethodConfig(
            base_model="Qwen/Qwen3-0.6B",
            model_revision="main" * 10,
        )
    with pytest.raises(ValidationError, match="model ID, not a path or URI"):
        SdLoraMethodConfig(
            base_model="/srv/models/qwen",
            model_revision=_MODEL_REVISION,
        )
