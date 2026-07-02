from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
from typing import Any

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
    env = dict(base_env or os.environ)
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
    tensor_parallel_size: int = 4,
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

    visible_gpus = ",".join(gpus or DEFAULT_VLLM_GPUS)
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
        str(tensor_parallel_size),
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
