from __future__ import annotations

import io
import json
import os
from pathlib import Path
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
from openevo.evolution.parametric.trainer_service import (
    SubprocessSdLoraTrainerService,
    _stable_file_identity as _service_file_identity,
)
from openevo.evolution.parametric.sd_lora_trainer import (
    _stable_file_identity as _trainer_file_identity,
)


_MODEL_REVISION = "0123456789abcdef0123456789abcdef01234567"


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
            "schema_version": SD_LORA_RESULT_SCHEMA,
            "state_manifest_path": "adapter/openevo_sd_lora_state.json",
            "state_weights_path": "adapter/openevo_sd_lora_state.safetensors",
            "steps_completed": 1,
            "target_module_names": ["model.layers.0.self_attn.q_proj"],
            "task_index": 0,
            "training_loss": 0.5,
            "training_record_count": 1,
        }
        response_path = Path(kwargs["cwd"]) / response_name
        response_path.write_text(json.dumps(response, sort_keys=True) + "\n", encoding="utf-8")
        response_path.chmod(0o600)

    def wait(self, timeout=None):
        return 0


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
    monkeypatch.setenv("OPENAI_API_KEY", "must-not-leak")
    monkeypatch.setenv("OPENEVO_UNTRUSTED", "must-not-leak")
    monkeypatch.setenv("CUDA_VISIBLE_DEVICES", "3")
    monkeypatch.setenv("HF_HUB_OFFLINE", "1")

    result = SubprocessSdLoraTrainerService(artifact_root).train_sd_lora(_request(work_dir))

    assert result.request_id == "sd-lora-test"
    assert not (work_dir / ".sd-lora-test.request.json").exists()
    assert not (work_dir / ".sd-lora-test.result.json").exists()
    assert len(observed) == 1
    process = observed[0]
    assert process.command[:4] == (
        sys.executable,
        "-I",
        "-m",
        "openevo.evolution.parametric.sd_lora_trainer",
    )
    assert process.kwargs["stdin"] is subprocess.DEVNULL
    assert process.kwargs["start_new_session"] is True
    assert process.kwargs["env"]["CUDA_VISIBLE_DEVICES"] == "3"
    assert process.kwargs["env"]["HF_HUB_OFFLINE"] == "1"
    assert "OPENAI_API_KEY" not in process.kwargs["env"]
    assert "OPENEVO_UNTRUSTED" not in process.kwargs["env"]


class _TimeoutProcess:
    def __init__(self, command, **kwargs) -> None:
        self.command = command
        self.kwargs = kwargs
        self.pid = 54321
        self.stdout = io.BytesIO(b"")
        self.stderr = io.BytesIO(b"")
        self._waits = 0

    def wait(self, timeout=None):
        self._waits += 1
        if self._waits == 1:
            raise subprocess.TimeoutExpired(self.command, timeout)
        return -9


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
    monkeypatch.setattr(os, "killpg", lambda pid, sig: killed.append((pid, sig)))

    with pytest.raises(TimeoutError, match="configured timeout"):
        SubprocessSdLoraTrainerService(artifact_root).train_sd_lora(
            _request(work_dir, timeout_seconds=0.01)
        )

    assert process is not None
    assert killed == [(process.pid, 9)]


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
