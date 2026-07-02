from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
import json
import os
from pathlib import Path
import signal
import subprocess
import time
from typing import Any

import httpx

from polar_evolution.terminal_bench_per_task import (
    DEFAULT_TERMINAL_BENCH_ENVIRONMENT_IMPORT_PATH,
    DEFAULT_TERMINAL_BENCH_EXTRA_DOCKER_COMPOSE,
)

DEFAULT_LOCAL_MODEL = "Qwen/Qwen3.6-35B-A3B"
DEFAULT_LOCAL_PARAMETRIC_ADAPTER_ID = "tb-parametric-memory"
DEFAULT_LOCAL_PARAMETRIC_DISABLED_ARTIFACTS = [
    "text_memory",
    "skill_bundle",
    "agent_system",
]
DEFAULT_VLLM_EXECUTABLE = "/root/evolab-vllm/bin/vllm"
DEFAULT_VLLM_GPUS = ["1", "2", "3", "4"]
_SECRET_ENV_MARKERS = (
    "key",
    "token",
    "secret",
    "password",
    "pass",
    "auth",
    "authorization",
    "cookie",
    "credential",
)


@dataclass(frozen=True)
class LocalParametricCondition:
    name: str
    model: str
    adapter_id: str | None = None
    adapter_path: Path | None = None


@dataclass(frozen=True)
class BuiltVLLMCommand:
    command: list[str]
    env: dict[str, str]


def build_local_harbor_command(
    *,
    job_name: str,
    task_root: Path,
    task_id: str,
    jobs_dir: Path,
    model: str,
    verifier_env: dict[str, str],
    n_attempts: int = 1,
    n_concurrent: int = 1,
    environment_import_path: str | None = DEFAULT_TERMINAL_BENCH_ENVIRONMENT_IMPORT_PATH,
    extra_docker_compose: list[Path] | None = None,
    preserve_environment: bool = True,
    cpu_policy: str | None = "ignore",
    memory_policy: str | None = "ignore",
) -> list[str]:
    command = [
        "harbor",
        "run",
        "--job-name",
        job_name,
        "--path",
        str(task_root),
        "--jobs-dir",
        str(jobs_dir),
        "--include-task-name",
        task_id,
        "--n-attempts",
        str(max(1, int(n_attempts))),
        "--n-concurrent",
        str(max(1, int(n_concurrent))),
        "--agent-import-path",
        "task_packages.terminal_bench_v1.harbor_agent:EvoLabHarborAgent",
        "--model",
        model,
        "--ak",
        "mode=evolab",
    ]
    if environment_import_path:
        command.extend(["--environment-import-path", environment_import_path])
    if preserve_environment:
        command.append("--no-delete")
    if cpu_policy:
        command.extend(["--cpus", cpu_policy])
    if memory_policy:
        command.extend(["--memory", memory_policy])

    compose_files = (
        list(extra_docker_compose)
        if extra_docker_compose is not None
        else list(DEFAULT_TERMINAL_BENCH_EXTRA_DOCKER_COMPOSE)
    )
    for compose_file in compose_files:
        command.extend(["--extra-docker-compose", str(compose_file)])
    for key in sorted(verifier_env):
        command.extend(["--verifier-env", f"{key}={verifier_env[key]}"])
    return command


def build_evolab_harbor_env(
    *,
    base_env: dict[str, str] | None = None,
    server_url: str,
    model: str,
) -> dict[str, str]:
    env = dict(os.environ if base_env is None else base_env)
    env["EVOLAB_TB_LLM_API"] = "openai-chat-completions"
    env["EVOLAB_TB_MODEL"] = model
    env["OPENAI_BASE_URL"] = server_url
    env["AIGOCODE_GPT_BASE_URL"] = server_url
    env["OPENAI_API_KEY"] = "dummy-local-key"
    return env


def build_vllm_command(
    *,
    model: str = DEFAULT_LOCAL_MODEL,
    served_model_name: str = DEFAULT_LOCAL_MODEL,
    port: int = 8000,
    tensor_parallel_size: int | None = None,
    gpu_memory_utilization: float = 0.75,
    max_model_len: int = 16384,
    vllm_executable: str = DEFAULT_VLLM_EXECUTABLE,
    gpus: list[str] | None = None,
    adapter_id: str | None = None,
    adapter_path: Path | None = None,
) -> BuiltVLLMCommand:
    if adapter_id is not None or adapter_path is not None:
        if not adapter_id or adapter_path is None:
            raise ValueError("adapter_id and adapter_path must be provided together")

    selected_gpus = list(DEFAULT_VLLM_GPUS if gpus is None else gpus)
    if not selected_gpus:
        raise ValueError("at least one GPU must be selected")
    effective_tensor_parallel_size = (
        len(selected_gpus) if tensor_parallel_size is None else int(tensor_parallel_size)
    )
    if effective_tensor_parallel_size < 1:
        raise ValueError("tensor_parallel_size must be at least 1")
    if len(selected_gpus) < effective_tensor_parallel_size:
        raise ValueError(
            "tensor_parallel_size cannot exceed selected GPU count: "
            f"tensor_parallel_size={effective_tensor_parallel_size}, "
            f"gpus={len(selected_gpus)}"
        )

    visible_gpus = ",".join(selected_gpus)
    command = [
        vllm_executable,
        "serve",
        model,
        "--host",
        "127.0.0.1",
        "--port",
        str(port),
        "--served-model-name",
        served_model_name,
        "--tensor-parallel-size",
        str(effective_tensor_parallel_size),
        "--gpu-memory-utilization",
        str(gpu_memory_utilization),
        "--max-model-len",
        str(max_model_len),
        "--dtype",
        "bfloat16",
        "--reasoning-parser",
        "qwen3",
        "--language-model-only",
        "--enforce-eager",
        "--enable-auto-tool-choice",
        "--tool-call-parser",
        "qwen3_xml",
    ]
    if adapter_id and adapter_path is not None:
        command.extend(
            [
                "--enable-lora",
                "--max-loras",
                "1",
                "--max-lora-rank",
                "64",
                "--lora-modules",
                f"{adapter_id}={adapter_path}",
            ]
        )
    return BuiltVLLMCommand(command=command, env={"CUDA_VISIBLE_DEVICES": visible_gpus})


def _redacted_env(env: dict[str, str]) -> dict[str, str]:
    redacted: dict[str, str] = {}
    for key in sorted(env):
        normalized_key = key.lower()
        if any(marker in normalized_key for marker in _SECRET_ENV_MARKERS):
            redacted[key] = "<redacted>"
        else:
            redacted[key] = env[key]
    return redacted


def _raise_if_process_exited(
    *,
    process_poll: Callable[[], int | None] | None,
    process_exit_message: Callable[[int], str] | None,
) -> None:
    if process_poll is None:
        return
    return_code = process_poll()
    if return_code is None:
        return
    if process_exit_message is None:
        raise RuntimeError(f"process exited during startup with return code {return_code}")
    raise RuntimeError(process_exit_message(return_code))


def wait_for_openai_server(
    *,
    server_url: str,
    expected_model: str,
    timeout_seconds: float,
    poll_interval_seconds: float = 1.0,
    process_poll: Callable[[], int | None] | None = None,
    process_exit_message: Callable[[int], str] | None = None,
) -> None:
    deadline = time.monotonic() + timeout_seconds
    last_error: Exception | None = None

    while True:
        _raise_if_process_exited(
            process_poll=process_poll,
            process_exit_message=process_exit_message,
        )
        try:
            with httpx.Client(base_url=server_url, timeout=5.0) as client:
                models_response = client.get("/models")
                models_response.raise_for_status()
                model_ids = {
                    model["id"]
                    for model in models_response.json().get("data", [])
                    if isinstance(model, dict) and isinstance(model.get("id"), str)
                }
                if expected_model not in model_ids:
                    raise ValueError(
                        f"OpenAI server did not expose expected model "
                        f"{expected_model!r}; available models: {sorted(model_ids)!r}"
                    )

                _raise_if_process_exited(
                    process_poll=process_poll,
                    process_exit_message=process_exit_message,
                )
                completion_response = client.post(
                    "/chat/completions",
                    json={
                        "model": expected_model,
                        "messages": [{"role": "user", "content": "ping"}],
                        "max_tokens": 1,
                    },
                )
                completion_response.raise_for_status()
                return
        except ValueError:
            raise
        except (httpx.HTTPError, OSError) as exc:
            last_error = exc
            _raise_if_process_exited(
                process_poll=process_poll,
                process_exit_message=process_exit_message,
            )
            if time.monotonic() >= deadline:
                raise TimeoutError(
                    f"Timed out waiting for OpenAI server at {server_url!r}"
                ) from last_error
            time.sleep(min(poll_interval_seconds, max(0.0, deadline - time.monotonic())))


def _terminate_process_group(
    process: subprocess.Popen[Any],
    *,
    wait_timeout_seconds: float = 10.0,
) -> None:
    if process.poll() is not None:
        return

    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        pass

    try:
        process.wait(timeout=wait_timeout_seconds)
        return
    except subprocess.TimeoutExpired:
        pass

    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass

    process.wait(timeout=wait_timeout_seconds)


@contextmanager
def managed_vllm_server(
    *,
    spec: BuiltVLLMCommand,
    run_root: Path,
    server_url: str,
    expected_model: str,
    timeout_seconds: float,
) -> Iterator[dict[str, Any]]:
    vllm_root = run_root / "vllm"
    vllm_root.mkdir(parents=True, exist_ok=True)
    stdout_path = vllm_root / "stdout.log"
    stderr_path = vllm_root / "stderr.log"
    metadata_path = vllm_root / "server.json"

    env = dict(os.environ)
    env.update(spec.env)
    stdout_handle = stdout_path.open("w")
    stderr_handle = stderr_path.open("w")
    process: subprocess.Popen[Any] | None = None

    try:
        process = subprocess.Popen(
            spec.command,
            cwd=run_root,
            env=env,
            stdout=stdout_handle,
            stderr=stderr_handle,
            start_new_session=True,
        )
        metadata: dict[str, Any] = {
            "pid": process.pid,
            "command": list(spec.command),
            "env": _redacted_env(spec.env),
            "server_url": server_url,
            "expected_model": expected_model,
            "stdout_path": str(stdout_path),
            "stderr_path": str(stderr_path),
        }
        metadata_path.write_text(json.dumps(metadata, indent=2, sort_keys=True) + "\n")

        wait_for_openai_server(
            server_url=server_url,
            expected_model=expected_model,
            timeout_seconds=timeout_seconds,
            process_poll=process.poll,
            process_exit_message=lambda return_code: (
                "vLLM server exited during startup with return code "
                f"{return_code}; stdout={stdout_path}; stderr={stderr_path}"
            ),
        )
        yield metadata
    finally:
        stdout_handle.close()
        stderr_handle.close()
        if process is not None:
            _terminate_process_group(process)


def run_local_parametric_memory_eval_dry_run(
    *,
    task_root: Path,
    task_ids: list[str],
    run_root: Path,
    model: str,
    adapter_path: Path,
    adapter_id: str,
    server_url: str,
    n_attempts: int,
    manage_server: bool,
) -> dict[str, Any]:
    conditions = [
        LocalParametricCondition(name="baseline", model=model),
        LocalParametricCondition(
            name="parametric_memory",
            model=adapter_id,
            adapter_id=adapter_id,
            adapter_path=adapter_path,
        ),
    ]
    return {
        "dry_run": True,
        "benchmark": "terminal-bench-2.1",
        "auth_mode": "local",
        "task_root": str(task_root),
        "run_root": str(run_root),
        "base_model": model,
        "server_url": server_url,
        "n_attempts": max(1, int(n_attempts)),
        "manage_server": manage_server,
        "enabled_artifacts": ["parametric_memory"],
        "disabled_artifacts": list(DEFAULT_LOCAL_PARAMETRIC_DISABLED_ARTIFACTS),
        "conditions": [
            {
                "name": condition.name,
                "model": condition.model,
                "adapter_id": condition.adapter_id,
                "adapter_path": str(condition.adapter_path) if condition.adapter_path else None,
                "task_ids": list(task_ids),
            }
            for condition in conditions
        ],
    }
