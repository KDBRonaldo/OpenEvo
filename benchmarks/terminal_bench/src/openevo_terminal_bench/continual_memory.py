"""Terminal-Bench continual-memory evaluation through Core CodexHarness/Gateway."""

from __future__ import annotations

from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
import hashlib
import ipaddress
import json
import math
import os
from pathlib import Path
import signal
import socket
import subprocess
import sys
import time
from typing import Any

import httpx

import openevo
from openevo.evolution.framework.contracts import canonical_digest, canonical_json
from openevo.evolution.framework.execution import (
    MethodExecutionContext,
    MethodExecutionServices,
    ResolvedMethodInputBinding,
    build_execution_envelope,
    worker_input_artifact_digest,
)
from openevo.evolution.models import (
    ArtifactResponse,
    ArtifactType,
    DatasetCreateRequest,
    WorkerClaimInputArtifact,
    WorkerClaimedJob,
)
from openevo.evolution.parametric.contracts import SdLoraMethodConfig
from openevo.evolution.parametric.sd_lora import (
    _training_examples,
    parametric_memory_sd_lora,
)
from openevo.evolution.parametric.trainer_service import (
    SubprocessSdLoraTrainerService,
)
from openevo.evolution.store import EvolutionStore
from openevo_terminal_bench.bridge import (
    CodexGatewayTrainingContract,
    TerminalBenchBridgeError,
    build_terminal_bench_events,
)
from openevo_terminal_bench.ordinary_lora import train_ordinary_sequential_lora
from openevo_terminal_bench.per_task import (
    DEFAULT_TERMINAL_BENCH_PACKAGE_ROOT,
    _attempt_reward,
    _locate_evolved_attempt_trials,
    _read_trial_result,
    _terminal_bench_extra_docker_compose,
)


DEFAULT_LOCAL_MODEL = "Qwen/Qwen3-4B-Instruct-2507"
DEFAULT_VLLM_EXECUTABLE = "/root/evolab-vllm/bin/vllm"
DEFAULT_VLLM_PORT = 8000
DEFAULT_GATEWAY_PORT = 8100
DEFAULT_MAX_MODEL_LENGTH = 16384
DEFAULT_AGENT_TIMEOUT_SECONDS = 3600
_PROCESS_STOP_SECONDS = 30.0
_STARTUP_POLL_SECONDS = 0.5
_GATEWAY_CAPTURE_TIMEOUT_SECONDS = 30.0
_MAX_GATEWAY_COMPLETIONS_PER_TASK = 256
_MAX_TRIAL_FAILURE_TEXT = 512


@dataclass(frozen=True)
class ContinualTask:
    task_id: str
    training_trial_dir: Path


@dataclass(frozen=True)
class AdapterServingSpec:
    adapter_id: str
    adapter_path: Path
    maximum_rank: int


@dataclass(frozen=True)
class ManagedProcess:
    command: tuple[str, ...]
    pid: int
    stdout_path: Path
    stderr_path: Path


@dataclass(frozen=True)
class _ConditionEvaluation:
    report: dict[str, Any]
    contracts: dict[str, CodexGatewayTrainingContract]


CommandRunner = Callable[..., Any]


def parse_continual_tasks(
    task_ids: Sequence[str],
    training_trials: Sequence[str],
) -> list[ContinualTask]:
    ordered = [task_id.strip() for task_id in task_ids if task_id.strip()]
    if not ordered or len(set(ordered)) != len(ordered):
        raise ValueError("continual task IDs must be non-empty and unique")
    by_task: dict[str, Path] = {}
    for entry in training_trials:
        task_id, separator, raw_path = entry.partition("=")
        task_id = task_id.strip()
        raw_path = raw_path.strip()
        if not separator or not task_id or not raw_path:
            raise ValueError("training trials must use TASK_ID=TRIAL_DIR")
        if task_id in by_task:
            raise ValueError(f"duplicate training trial for task {task_id!r}")
        by_task[task_id] = Path(raw_path)
    if set(by_task) != set(ordered):
        missing = sorted(set(ordered).difference(by_task))
        unexpected = sorted(set(by_task).difference(ordered))
        raise ValueError(
            "training trials must exactly match the task stream; "
            f"missing={missing}, unexpected={unexpected}"
        )
    return [
        ContinualTask(task_id=task_id, training_trial_dir=by_task[task_id]) for task_id in ordered
    ]


def continual_learning_metrics(
    reward_matrix: Sequence[Sequence[float]],
    baseline_rewards: Sequence[float],
) -> dict[str, float]:
    rows = [list(map(float, row)) for row in reward_matrix]
    baseline = list(map(float, baseline_rewards))
    task_count = len(baseline)
    if task_count < 1 or len(rows) != task_count:
        raise ValueError("reward matrix must have one post-training row per task")
    if any(len(row) != task_count for row in rows):
        raise ValueError("reward matrix must be square")
    values = [*baseline, *(value for row in rows for value in row)]
    if any(not math.isfinite(value) for value in values):
        raise ValueError("continual rewards must be finite")

    final_average = sum(rows[-1]) / task_count
    seen_values = [
        rows[generation][task]
        for generation in range(task_count)
        for task in range(generation + 1)
    ]
    anytime_average = sum(seen_values) / len(seen_values)
    forward_transfer = (
        sum(rows[task - 1][task] - baseline[task] for task in range(1, task_count))
        / (task_count - 1)
        if task_count > 1
        else 0.0
    )
    backward_transfer = (
        sum(rows[-1][task] - rows[task][task] for task in range(task_count - 1)) / (task_count - 1)
        if task_count > 1
        else 0.0
    )
    forgetting = (
        sum(
            max(rows[generation][task] for generation in range(task, task_count)) - rows[-1][task]
            for task in range(task_count - 1)
        )
        / (task_count - 1)
        if task_count > 1
        else 0.0
    )
    return {
        "baseline_average": sum(baseline) / task_count,
        "final_average": final_average,
        "anytime_average": anytime_average,
        "forward_transfer": forward_transfer,
        "backward_transfer": backward_transfer,
        "forgetting": forgetting,
    }


def build_core_codex_harbor_command(
    *,
    job_name: str,
    task_root: Path,
    task_id: str,
    jobs_dir: Path,
    model: str,
    gateway_url: str,
    codex_version: str,
    terminal_bench_package_root: Path = DEFAULT_TERMINAL_BENCH_PACKAGE_ROOT,
    agent_timeout_seconds: int = DEFAULT_AGENT_TIMEOUT_SECONDS,
    verifier_env: dict[str, str] | None = None,
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
        "1",
        "--n-concurrent",
        "1",
        "--no-delete",
        "--agent-import-path",
        "openevo_terminal_bench.core_codex_agent:OpenEvoCoreCodexAgent",
        "--model",
        model,
        "--ak",
        f"gateway_url={gateway_url.rstrip('/')}",
        "--ak",
        f"timeout_sec={int(agent_timeout_seconds)}",
        "--ak",
        "reasoning_effort=high",
        "--ak",
        f"version={codex_version}",
        "--environment-import-path",
        "task_packages.terminal_bench_v1.harbor_environment:DockerCpHarborEnvironment",
        "--cpus",
        "ignore",
        "--memory",
        "ignore",
    ]
    for compose_file in _terminal_bench_extra_docker_compose(terminal_bench_package_root):
        command.extend(["--extra-docker-compose", str(compose_file)])
    for key, value in sorted((verifier_env or {}).items()):
        command.extend(["--verifier-env", f"{key}={value}"])
    return command


def build_vllm_command(
    *,
    model: str,
    model_revision: str,
    port: int,
    maximum_model_length: int,
    vllm_executable: str,
    adapter: AdapterServingSpec | None,
) -> tuple[str, ...]:
    command = [
        vllm_executable,
        "serve",
        model,
        "--host",
        "127.0.0.1",
        "--port",
        str(port),
        "--served-model-name",
        model,
        "--revision",
        model_revision,
        "--tensor-parallel-size",
        "1",
        "--max-model-len",
        str(maximum_model_length),
        "--gpu-memory-utilization",
        "0.75",
        "--dtype",
        "bfloat16",
        "--generation-config",
        "vllm",
        "--enforce-eager",
        "--enable-auto-tool-choice",
        "--tool-call-parser",
        "hermes",
        "--reasoning-parser",
        "qwen3",
    ]
    if adapter is not None:
        command.extend(
            [
                "--enable-lora",
                "--max-loras",
                "1",
                "--max-lora-rank",
                str(_supported_vllm_lora_rank(adapter.maximum_rank)),
                "--lora-modules",
                f"{adapter.adapter_id}={adapter.adapter_path}",
            ]
        )
    return tuple(command)


def run_continual_memory_eval_dry_run(
    *,
    tasks: Sequence[ContinualTask],
    task_root: Path,
    run_root: Path,
    model: str,
    model_revision: str,
    gpu: str,
    codex_version: str,
    config: SdLoraMethodConfig,
    terminal_bench_package_root: Path = DEFAULT_TERMINAL_BENCH_PACKAGE_ROOT,
    vllm_executable: str = DEFAULT_VLLM_EXECUTABLE,
    vllm_port: int = DEFAULT_VLLM_PORT,
    gateway_port: int = DEFAULT_GATEWAY_PORT,
    gateway_advertise_host: str | None = None,
    maximum_model_length: int = DEFAULT_MAX_MODEL_LENGTH,
    agent_timeout_seconds: int = DEFAULT_AGENT_TIMEOUT_SECONDS,
    include_ordinary_control: bool = True,
) -> dict[str, Any]:
    if config.base_model != model or config.model_revision != model_revision:
        raise ValueError("training config must pin the planned base model and revision")
    return {
        "dry_run": True,
        "benchmark": "terminal-bench-2.1",
        "task_root": str(task_root),
        "run_root": str(run_root),
        "task_order": [task.task_id for task in tasks],
        "training_trials": {task.task_id: str(task.training_trial_dir) for task in tasks},
        "base_model": model,
        "model_revision": model_revision,
        "gpu": gpu,
        "codex_version": codex_version,
        "training_config": config.model_dump(mode="json"),
        "terminal_bench_package_root": str(terminal_bench_package_root),
        "vllm_executable": vllm_executable,
        "vllm_port": vllm_port,
        "gateway_port": gateway_port,
        "gateway_advertise_host": gateway_advertise_host or "auto",
        "maximum_model_length": maximum_model_length,
        "agent_timeout_seconds": agent_timeout_seconds,
        "inference_path": "OpenEvo Core CodexHarness -> OpenEvo Gateway -> local vLLM",
        "conditions": [
            "base",
            *(["ordinary_sequential_lora"] if include_ordinary_control else []),
            "sd_lora",
        ],
        "enabled_evolution_targets": ["parametric_memory"],
        "disabled_evolution_targets": [
            "text_memory",
            "skill_bundle",
            "agent_system",
        ],
        "evaluation_schedule": {
            "base_rows": 1,
            "post_training_rows_per_method": len(tasks),
            "tasks_per_row": len(tasks),
            "attempts_per_task": 1,
        },
    }


def run_continual_memory_eval(
    *,
    tasks: Sequence[ContinualTask],
    task_root: Path,
    run_root: Path,
    model: str,
    model_revision: str,
    gpu: str,
    codex_version: str,
    config: SdLoraMethodConfig,
    terminal_bench_package_root: Path = DEFAULT_TERMINAL_BENCH_PACKAGE_ROOT,
    vllm_executable: str = DEFAULT_VLLM_EXECUTABLE,
    vllm_port: int = DEFAULT_VLLM_PORT,
    gateway_port: int = DEFAULT_GATEWAY_PORT,
    gateway_advertise_host: str | None = None,
    maximum_model_length: int = DEFAULT_MAX_MODEL_LENGTH,
    agent_timeout_seconds: int = DEFAULT_AGENT_TIMEOUT_SECONDS,
    verifier_env: dict[str, str] | None = None,
    command_runner: CommandRunner = subprocess.run,
    include_ordinary_control: bool = True,
) -> dict[str, Any]:
    if not tasks:
        raise ValueError("continual-memory evaluation requires at least one task")
    if config.base_model != model or config.model_revision != model_revision:
        raise ValueError("training config must pin the evaluated base model and revision")
    if config.rank * len(tasks) > 256:
        raise ValueError("task stream exceeds the SD-LoRA effective-rank limit")
    run_root.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(run_root, 0o700, follow_symlinks=False)
    effective_gateway_host = resolve_gateway_advertise_host(gateway_advertise_host)
    previous_cuda_visible = os.environ.get("CUDA_VISIBLE_DEVICES")
    os.environ["CUDA_VISIBLE_DEVICES"] = gpu
    try:
        store = EvolutionStore(
            db_path=run_root / "evolution.db",
            artifact_root=run_root / "artifacts",
        )
        store.initialize()
        baseline_evaluation = _evaluate_condition(
            condition="base",
            generation=-1,
            tasks=tasks,
            task_root=task_root,
            run_root=run_root,
            model=model,
            model_revision=model_revision,
            adapter=None,
            gpu=gpu,
            codex_version=codex_version,
            terminal_bench_package_root=terminal_bench_package_root,
            vllm_executable=vllm_executable,
            vllm_port=vllm_port,
            gateway_port=gateway_port,
            gateway_advertise_host=effective_gateway_host,
            maximum_model_length=maximum_model_length,
            agent_timeout_seconds=agent_timeout_seconds,
            verifier_env=verifier_env or {},
            command_runner=command_runner,
        )
        baseline = baseline_evaluation.report
        datasets = [
            _prepare_training_dataset(
                store,
                task,
                maximum_traces=config.max_records,
                codex_gateway_contract=baseline_evaluation.contracts[task.task_id],
            )
            for task in tasks
        ]
        baseline_rewards = [float(task["reward"]) for task in baseline["tasks"]]

        ordinary_result: dict[str, Any] = {
            "skipped": True,
            "reason": "disabled_by_benchmark_invocation",
        }
        if include_ordinary_control:
            ordinary_generations: list[dict[str, Any]] = []
            ordinary_matrix: list[list[float]] = []
            prior_ordinary: Path | None = None
            for generation, (task, dataset) in enumerate(zip(tasks, datasets, strict=True)):
                examples = _training_examples(
                    (dataset,),
                    maximum=config.max_records,
                    minimum_reward=config.minimum_reward,
                )
                output_dir = run_root / "ordinary_adapters" / f"generation-{generation}"
                training = train_ordinary_sequential_lora(
                    config=config,
                    examples=examples,
                    output_dir=output_dir,
                    prior_adapter=prior_ordinary,
                )
                adapter = AdapterServingSpec(
                    adapter_id=f"ordinary-lora-g{generation}",
                    adapter_path=training.adapter_path,
                    maximum_rank=config.rank,
                )
                condition_evaluation = _evaluate_condition(
                    condition="ordinary-sequential-lora",
                    generation=generation,
                    tasks=tasks,
                    task_root=task_root,
                    run_root=run_root,
                    model=model,
                    model_revision=model_revision,
                    adapter=adapter,
                    gpu=gpu,
                    codex_version=codex_version,
                    terminal_bench_package_root=terminal_bench_package_root,
                    vllm_executable=vllm_executable,
                    vllm_port=vllm_port,
                    gateway_port=gateway_port,
                    gateway_advertise_host=effective_gateway_host,
                    maximum_model_length=maximum_model_length,
                    agent_timeout_seconds=agent_timeout_seconds,
                    verifier_env=verifier_env or {},
                    command_runner=command_runner,
                    expected_contracts=baseline_evaluation.contracts,
                )
                evaluation = condition_evaluation.report
                row = [float(value["reward"]) for value in evaluation["tasks"]]
                ordinary_matrix.append(row)
                ordinary_generations.append(
                    {
                        "generation": generation,
                        "training_task_id": task.task_id,
                        "adapter_id": adapter.adapter_id,
                        "adapter_path": str(adapter.adapter_path),
                        "adapter_bytes": _directory_size(adapter.adapter_path),
                        "training_record_count": training.training_record_count,
                        "steps_completed": training.steps_completed,
                        "training_loss": training.training_loss,
                        "training_time_seconds": training.training_time_seconds,
                        "gpu_peak_memory_bytes": training.gpu_peak_memory_bytes,
                        "evaluation": evaluation,
                    }
                )
                prior_ordinary = training.adapter_path
            ordinary_result = {
                "skipped": False,
                "reward_matrix": ordinary_matrix,
                "metrics": continual_learning_metrics(
                    ordinary_matrix,
                    baseline_rewards,
                ),
                "generations": ordinary_generations,
            }

        sd_generations: list[dict[str, Any]] = []
        sd_matrix: list[list[float]] = []
        prior_sd: ArtifactResponse | None = None
        with SubprocessSdLoraTrainerService(store.files.root) as trainer:
            for generation, (task, dataset) in enumerate(zip(tasks, datasets, strict=True)):
                artifact = _train_sd_lora_generation(
                    store=store,
                    trainer=trainer,
                    dataset=dataset,
                    prior=prior_sd,
                    config=config,
                    generation=generation,
                )
                adapter_id = str(artifact.manifest["adapter_id"])
                adapter_path = Path(artifact.uri.removeprefix("file://"))
                adapter = AdapterServingSpec(
                    adapter_id=adapter_id,
                    adapter_path=adapter_path,
                    maximum_rank=int(artifact.manifest["effective_rank"]),
                )
                condition_evaluation = _evaluate_condition(
                    condition="sd-lora",
                    generation=generation,
                    tasks=tasks,
                    task_root=task_root,
                    run_root=run_root,
                    model=model,
                    model_revision=model_revision,
                    adapter=adapter,
                    gpu=gpu,
                    codex_version=codex_version,
                    terminal_bench_package_root=terminal_bench_package_root,
                    vllm_executable=vllm_executable,
                    vllm_port=vllm_port,
                    gateway_port=gateway_port,
                    gateway_advertise_host=effective_gateway_host,
                    maximum_model_length=maximum_model_length,
                    agent_timeout_seconds=agent_timeout_seconds,
                    verifier_env=verifier_env or {},
                    command_runner=command_runner,
                    expected_contracts=baseline_evaluation.contracts,
                )
                evaluation = condition_evaluation.report
                row = [float(value["reward"]) for value in evaluation["tasks"]]
                sd_matrix.append(row)
                sd_generations.append(
                    {
                        "generation": generation,
                        "training_task_id": task.task_id,
                        "artifact_id": artifact.artifact_id,
                        "adapter_id": adapter.adapter_id,
                        "adapter_path": str(adapter.adapter_path),
                        "adapter_bytes": _directory_size(adapter.adapter_path),
                        "component_count": artifact.manifest["component_count"],
                        "effective_rank": artifact.manifest["effective_rank"],
                        "training_record_count": artifact.manifest["training_record_count"],
                        "replay_training_record_count": artifact.manifest[
                            "replay_training_record_count"
                        ],
                        "optimizer_training_record_count": artifact.manifest[
                            "optimizer_training_record_count"
                        ],
                        "replay_buffer_record_count": artifact.manifest[
                            "replay_buffer_record_count"
                        ],
                        "steps_completed": artifact.manifest["steps_completed"],
                        "training_loss": artifact.manifest["training_loss"],
                        "training_time_seconds": artifact.manifest["training_time_seconds"],
                        "gpu_peak_memory_bytes": artifact.manifest["gpu_peak_memory_bytes"],
                        "evaluation": evaluation,
                    }
                )
                prior_sd = artifact

        report = {
            "dry_run": False,
            "benchmark": "terminal-bench-2.1",
            "task_order": [task.task_id for task in tasks],
            "training_trials": {task.task_id: str(task.training_trial_dir) for task in tasks},
            "base_model": model,
            "model_revision": model_revision,
            "gpu": gpu,
            "codex_version": codex_version,
            "training_config": config.model_dump(mode="json"),
            "terminal_bench_package_root": str(terminal_bench_package_root),
            "vllm_executable": vllm_executable,
            "vllm_port": vllm_port,
            "gateway_port": gateway_port,
            "gateway_advertise_host": effective_gateway_host,
            "maximum_model_length": maximum_model_length,
            "agent_timeout_seconds": agent_timeout_seconds,
            "inference_path": ("OpenEvo Core CodexHarness -> OpenEvo Gateway -> local vLLM"),
            "attempts_per_task": 1,
            "enabled_evolution_targets": ["parametric_memory"],
            "disabled_evolution_targets": [
                "text_memory",
                "skill_bundle",
                "agent_system",
            ],
            "base": baseline,
            "ordinary_sequential_lora": ordinary_result,
            "sd_lora": {
                "reward_matrix": sd_matrix,
                "metrics": continual_learning_metrics(sd_matrix, baseline_rewards),
                "generations": sd_generations,
            },
        }
        summary_path = run_root / "summary.json"
        summary_path.write_text(
            json.dumps(report, indent=2, sort_keys=True, allow_nan=False) + "\n",
            encoding="utf-8",
        )
        return report
    finally:
        if previous_cuda_visible is None:
            os.environ.pop("CUDA_VISIBLE_DEVICES", None)
        else:
            os.environ["CUDA_VISIBLE_DEVICES"] = previous_cuda_visible


def _prepare_training_dataset(
    store: EvolutionStore,
    task: ContinualTask,
    *,
    maximum_traces: int,
    codex_gateway_contract: CodexGatewayTrainingContract,
) -> WorkerClaimInputArtifact:
    if maximum_traces < 1:
        raise ValueError("maximum training traces must be positive")
    events = build_terminal_bench_events(
        task.training_trial_dir,
        include_atif_traces=True,
        max_atif_agent_turns=maximum_traces,
        codex_gateway_contract=codex_gateway_contract,
    )
    if len(events) != 1:
        raise ValueError(f"training trial for {task.task_id!r} must contain exactly one trial")
    event = events[0]
    if event.task_id != task.task_id:
        legacy_trial_name = task.training_trial_dir.name.startswith(f"{task.task_id}__")
        if event.task_id != "terminal-bench-task" or not legacy_trial_name:
            raise ValueError(f"training trial identity does not match task {task.task_id!r}")
        event = event.model_copy(update={"task_id": task.task_id})
    if event.reward is None or event.reward < 1.0:
        raise ValueError(f"training trial for {task.task_id!r} must have reward 1")
    store.ingest_event(event)
    dataset = store.create_dataset(
        DatasetCreateRequest(
            idempotency_key=f"continual-{_safe_name(task.task_id)}",
            name=f"Terminal-Bench continual training: {task.task_id}",
            purpose="parametric_memory_continual_training",
            query={
                "source": event.source,
                "event_types": [event.event_type],
                "source_event_id": event.source_event_id,
                "reward_min": 1.0,
            },
            limits={"max_events": 1, "max_traces": maximum_traces},
        )
    )
    artifact = store.get_artifact(dataset.artifact_id)
    manifest = artifact.manifest
    records_size = manifest.get("records_byte_size")
    records_digest = manifest.get("records_sha256")
    if type(records_size) is not int or not isinstance(records_digest, str):
        raise ValueError("Core dataset did not publish exact record receipts")
    return WorkerClaimInputArtifact(
        artifact_id=artifact.artifact_id,
        type=artifact.type,
        uri=artifact.uri,
        name=artifact.name,
        manifest_sha256=canonical_digest(manifest),
        records_byte_size=records_size,
        records_sha256=records_digest,
    )


def _train_sd_lora_generation(
    *,
    store: EvolutionStore,
    trainer: SubprocessSdLoraTrainerService,
    dataset: WorkerClaimInputArtifact,
    prior: ArtifactResponse | None,
    config: SdLoraMethodConfig,
    generation: int,
) -> ArtifactResponse:
    prior_input = (
        WorkerClaimInputArtifact(
            artifact_id=prior.artifact_id,
            type=prior.type,
            uri=prior.uri,
            name=prior.name,
        )
        if prior is not None
        else None
    )
    artifacts = [dataset, *([prior_input] if prior_input is not None else [])]
    bindings = (
        ResolvedMethodInputBinding(
            binding_id="current_dataset",
            artifact_ids=(dataset.artifact_id,),
            artifact_digests=(worker_input_artifact_digest(dataset),),
        ),
        ResolvedMethodInputBinding(
            binding_id="prior_target_artifacts",
            artifact_ids=((prior_input.artifact_id,) if prior_input is not None else ()),
            artifact_digests=(
                (worker_input_artifact_digest(prior_input),) if prior_input is not None else ()
            ),
        ),
    )
    user_config = config.model_dump(mode="json")
    identity = canonical_digest(
        {
            "benchmark_invocation": "method_context_v1",
            "method": "parametric_memory_sd_lora",
        }
    )
    envelope = build_execution_envelope(
        plan_id=f"tb21-continual-sd-lora-{generation}",
        plan_digest=canonical_digest(
            {"generation": generation, "method": "parametric_memory_sd_lora"}
        ),
        registry_snapshot_digest=canonical_digest(
            {"scope": "terminal-bench-maintainer-automation"}
        ),
        target_id="parametric_memory",
        method_id="parametric_memory_sd_lora",
        method_identity_digest=identity,
        user_config=user_config,
        core_config={
            "name": f"Terminal-Bench SD-LoRA generation {generation}",
            "promoted": True,
            "compatibility": {
                "agent_harnesses": ["codex"],
                "base_model": [config.base_model],
            },
            "lineage": {"benchmark": "terminal-bench-2.1"},
            "tags": ["terminal-bench", "continual-learning-control"],
        },
        input_bindings=bindings,
        output_artifact_types=(ArtifactType.PARAMETRIC_MEMORY.value,),
    )
    context = MethodExecutionContext(
        job=WorkerClaimedJob(
            job_id=f"tb21-sd-lora-job-{generation}",
            lease_id=f"tb21-sd-lora-lease-{generation}",
            job_type="openevo:benchmark:parametric_memory",
            method="parametric_memory_sd_lora",
            input_artifacts=artifacts,
            config=user_config,
        ),
        artifact_root=store.files.root,
        envelope=envelope,
        services=MethodExecutionServices(
            harness=_NoInferenceService(),
            parametric_trainer=trainer,
        ),
    )
    [request] = parametric_memory_sd_lora(context)
    return store.register_artifact(request)


class _NoInferenceService:
    def infer(self, request: object) -> object:
        raise AssertionError(f"SD-LoRA training unexpectedly requested inference: {request!r}")


def _evaluate_condition(
    *,
    condition: str,
    generation: int,
    tasks: Sequence[ContinualTask],
    task_root: Path,
    run_root: Path,
    model: str,
    model_revision: str,
    adapter: AdapterServingSpec | None,
    gpu: str,
    codex_version: str,
    terminal_bench_package_root: Path,
    vllm_executable: str,
    vllm_port: int,
    gateway_port: int,
    gateway_advertise_host: str,
    maximum_model_length: int,
    agent_timeout_seconds: int,
    verifier_env: dict[str, str],
    command_runner: CommandRunner,
    expected_contracts: Mapping[str, CodexGatewayTrainingContract] | None = None,
) -> _ConditionEvaluation:
    generation_name = "initial" if generation < 0 else f"generation-{generation}"
    evaluation_root = run_root / "evaluations" / condition / generation_name
    jobs_dir = evaluation_root / "harbor_jobs"
    evaluation_root.mkdir(mode=0o700, parents=True, exist_ok=True)
    serving_model = adapter.adapter_id if adapter is not None else model
    vllm_command = build_vllm_command(
        model=model,
        model_revision=model_revision,
        port=vllm_port,
        maximum_model_length=maximum_model_length,
        vllm_executable=vllm_executable,
        adapter=adapter,
    )
    vllm_url = f"http://127.0.0.1:{vllm_port}"
    gateway_url = f"http://{gateway_advertise_host}:{gateway_port}/v1"
    vllm_env, gateway_env = _evaluation_process_environments(gpu=gpu)
    with _managed_process(
        name="vllm",
        command=vllm_command,
        cwd=evaluation_root,
        env=vllm_env,
        readiness=lambda process: _wait_for_model(
            base_url=vllm_url,
            expected_model=serving_model,
            process=process,
            timeout_seconds=900.0,
        ),
    ) as vllm_process:
        gateway_config = _write_gateway_topology(
            evaluation_root,
            gateway_port=gateway_port,
            gateway_advertise_host=gateway_advertise_host,
            vllm_url=vllm_url,
            served_model=serving_model,
        )
        with _managed_process(
            name="gateway",
            command=(
                sys.executable,
                "-m",
                "openevo.gateway.server",
                "--config",
                str(gateway_config),
                "--node-id",
                "tb21-local",
                "--log-level",
                "warning",
            ),
            cwd=evaluation_root,
            env=gateway_env,
            readiness=lambda process: _wait_for_health(
                url=f"http://127.0.0.1:{gateway_port}/health",
                process=process,
                timeout_seconds=120.0,
            ),
        ) as gateway_process:
            gateway_management_url = f"http://127.0.0.1:{gateway_port}"
            observed_session_ids = _gateway_session_ids(gateway_management_url)
            task_results: list[dict[str, Any]] = []
            contracts: dict[str, CodexGatewayTrainingContract] = {}
            for task in tasks:
                task_result = _run_harbor_task(
                    condition=condition,
                    generation_name=generation_name,
                    task=task,
                    task_root=task_root,
                    jobs_dir=jobs_dir,
                    model=model,
                    gateway_url=gateway_url,
                    codex_version=codex_version,
                    terminal_bench_package_root=terminal_bench_package_root,
                    agent_timeout_seconds=agent_timeout_seconds,
                    verifier_env=verifier_env,
                    command_runner=command_runner,
                )
                contract, capture = _capture_gateway_training_contract(
                    gateway_management_url=gateway_management_url,
                    known_session_ids=observed_session_ids,
                )
                observed_session_ids.update(capture["session_ids"])
                expected = (
                    expected_contracts.get(task.task_id)
                    if expected_contracts is not None
                    else None
                )
                if expected is not None and contract.digest != expected.digest:
                    raise ValueError(
                        "Codex Gateway harness contract drifted between base and "
                        f"{condition} evaluation for task {task.task_id!r}"
                    )
                receipt_path = _write_gateway_contract_receipt(
                    evaluation_root=evaluation_root,
                    task=task,
                    condition=condition,
                    codex_version=codex_version,
                    contract=contract,
                    capture=capture,
                )
                contracts[task.task_id] = contract
                task_results.append(
                    {
                        **task_result,
                        "harness_contract": {
                            "digest": contract.digest,
                            "message_count": len(contract.messages),
                            "path": str(receipt_path),
                            "request_count": capture["request_count"],
                            "session_count": len(capture["session_ids"]),
                            "tool_names": list(contract.tool_names),
                        },
                    }
                )
    report = {
        "condition": condition,
        "generation": generation,
        "model": model,
        "served_model": serving_model,
        "adapter_id": adapter.adapter_id if adapter is not None else None,
        "adapter_path": str(adapter.adapter_path) if adapter is not None else None,
        "tasks": task_results,
        "mean_reward": sum(float(task["reward"]) for task in task_results) / len(task_results),
        "vllm": _process_metadata(vllm_process),
        "gateway": _process_metadata(gateway_process),
    }
    return _ConditionEvaluation(report=report, contracts=contracts)


def _gateway_json(base_url: str, path: str) -> dict[str, Any]:
    try:
        with httpx.Client(
            timeout=_GATEWAY_CAPTURE_TIMEOUT_SECONDS,
            trust_env=False,
        ) as client:
            response = client.get(f"{base_url.rstrip('/')}{path}")
            response.raise_for_status()
            payload = response.json()
    except (httpx.HTTPError, ValueError) as exc:
        raise RuntimeError("failed to read the live Gateway evaluation contract") from exc
    if not isinstance(payload, dict):
        raise ValueError("Gateway contract endpoint returned a non-object payload")
    return payload


def _gateway_session_ids(base_url: str) -> set[str]:
    payload = _gateway_json(base_url, "/sessions?limit=1000")
    rows = payload.get("sessions")
    if not isinstance(rows, list) or len(rows) > 1000:
        raise ValueError("Gateway session inventory has an invalid shape")
    session_ids: set[str] = set()
    for row in rows:
        session_id = row.get("session_id") if isinstance(row, dict) else None
        if not isinstance(session_id, str) or not session_id or session_id in session_ids:
            raise ValueError("Gateway session inventory has an invalid identity")
        session_ids.add(session_id)
    return session_ids


def _capture_gateway_training_contract(
    *,
    gateway_management_url: str,
    known_session_ids: set[str],
) -> tuple[CodexGatewayTrainingContract, dict[str, Any]]:
    current_session_ids = _gateway_session_ids(gateway_management_url)
    new_session_ids = sorted(current_session_ids.difference(known_session_ids))
    if not new_session_ids or len(new_session_ids) > _MAX_GATEWAY_COMPLETIONS_PER_TASK:
        raise ValueError("Gateway did not expose a bounded task completion set")

    captured: list[tuple[str, str, int, dict[str, Any]]] = []
    empty_session_ids: list[str] = []
    for session_id in new_session_ids:
        payload = _gateway_json(
            gateway_management_url,
            f"/sessions/{session_id}/completions",
        )
        if payload.get("session_id") != session_id:
            raise ValueError("Gateway completion payload changed session identity")
        completions = payload.get("completions")
        if not isinstance(completions, list):
            raise ValueError("Gateway task session has an invalid completion inventory")
        if not completions:
            empty_session_ids.append(session_id)
            continue
        for index, completion in enumerate(completions):
            if not isinstance(completion, dict):
                raise ValueError("Gateway completion record has an invalid shape")
            timestamp = completion.get("timestamp")
            request = completion.get("request")
            if not isinstance(timestamp, str) or not timestamp or not isinstance(request, dict):
                raise ValueError("Gateway completion record lacks request provenance")
            captured.append((timestamp, session_id, index, request))
            if len(captured) > _MAX_GATEWAY_COMPLETIONS_PER_TASK:
                raise ValueError("Gateway task completion set exceeds its request budget")

    if not captured:
        raise ValueError("Gateway task sessions contain no captured completion")
    captured.sort(key=lambda item: item[:3])
    try:
        contract = CodexGatewayTrainingContract.from_gateway_request(captured[0][3])
        for _, _, _, request in captured:
            contract.validate_request_extension(request)
    except TerminalBenchBridgeError as exc:
        raise ValueError("Gateway task requests do not share one Codex harness contract") from exc
    return contract, {
        "first_completion_timestamp": captured[0][0],
        "first_session_id": captured[0][1],
        "request_count": len(captured),
        "session_ids": new_session_ids,
        "empty_session_ids": empty_session_ids,
    }


def _write_gateway_contract_receipt(
    *,
    evaluation_root: Path,
    task: ContinualTask,
    condition: str,
    codex_version: str,
    contract: CodexGatewayTrainingContract,
    capture: Mapping[str, Any],
) -> Path:
    contract_root = evaluation_root / "harness_contracts"
    contract_root.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(contract_root, 0o700, follow_symlinks=False)
    path = contract_root / f"{_safe_name(task.task_id)}.json"
    payload = {
        "schema_version": "openevo.terminal_bench.gateway_contract_receipt.v1",
        "condition": condition,
        "task_id": task.task_id,
        "codex_version": codex_version,
        "contract_digest": contract.digest,
        "capture": dict(capture),
        "contract": contract.to_payload(),
    }
    path.write_text(canonical_json(payload) + "\n", encoding="utf-8")
    os.chmod(path, 0o600, follow_symlinks=False)
    return path


def _run_harbor_task(
    *,
    condition: str,
    generation_name: str,
    task: ContinualTask,
    task_root: Path,
    jobs_dir: Path,
    model: str,
    gateway_url: str,
    codex_version: str,
    terminal_bench_package_root: Path,
    agent_timeout_seconds: int,
    verifier_env: dict[str, str],
    command_runner: CommandRunner,
) -> dict[str, Any]:
    job_name = _safe_name(f"{condition}-{generation_name}-{task.task_id}")
    command = build_core_codex_harbor_command(
        job_name=job_name,
        task_root=task_root,
        task_id=task.task_id,
        jobs_dir=jobs_dir,
        model=model,
        gateway_url=gateway_url,
        codex_version=codex_version,
        terminal_bench_package_root=terminal_bench_package_root,
        agent_timeout_seconds=agent_timeout_seconds,
        verifier_env=verifier_env,
    )
    env = dict(os.environ)
    env["PYTHONPATH"] = _source_pythonpath(env.get("PYTHONPATH"))
    started = time.monotonic()
    command_runner(
        command,
        cwd=terminal_bench_package_root,
        env=env,
        check=True,
    )
    latency = time.monotonic() - started
    trials = _locate_evolved_attempt_trials(
        task_id=task.task_id,
        job_root=jobs_dir / job_name,
    )
    if len(trials) != 1:
        raise ValueError("continual-memory evaluation requires exactly one Harbor attempt")
    reward = _attempt_reward(trials[0])
    agent_failure: dict[str, str] | None = None
    if reward is None or not math.isfinite(float(reward)):
        agent_failure = _agent_execution_failure(trials[0])
        if agent_failure is None:
            raise ValueError(f"Terminal-Bench task {task.task_id!r} has no finite reward")
        reward = 0.0
    result = {
        "task_id": task.task_id,
        "job_name": job_name,
        "trial_dir": str(trials[0]),
        "reward": float(reward),
        "passed": float(reward) >= 1.0,
        "latency_seconds": latency,
    }
    if agent_failure is not None:
        result["agent_failure"] = agent_failure
    return result


def _agent_execution_failure(trial_dir: Path) -> dict[str, str] | None:
    result = _read_trial_result(trial_dir / "result.json")
    execution = result.get("agent_execution")
    exception = result.get("exception_info")
    if not isinstance(execution, dict) or not execution.get("started_at"):
        return None
    if not isinstance(exception, dict):
        return None
    exception_type = exception.get("exception_type")
    exception_message = exception.get("exception_message")
    if not isinstance(exception_type, str) or not exception_type.strip():
        return None
    if not isinstance(exception_message, str) or not exception_message.strip():
        return None
    return {
        "type": exception_type.strip()[:_MAX_TRIAL_FAILURE_TEXT],
        "message": exception_message.strip()[:_MAX_TRIAL_FAILURE_TEXT],
    }


@contextmanager
def _managed_process(
    *,
    name: str,
    command: tuple[str, ...],
    cwd: Path,
    env: dict[str, str],
    readiness: Callable[[subprocess.Popen[bytes]], None],
) -> Iterator[ManagedProcess]:
    stdout_path = cwd / f"{name}.stdout.log"
    stderr_path = cwd / f"{name}.stderr.log"
    process: subprocess.Popen[bytes] | None = None
    stdout = stdout_path.open("wb")
    stderr = stderr_path.open("wb")
    original_error = False
    try:
        process = subprocess.Popen(
            command,
            cwd=cwd,
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=stdout,
            stderr=stderr,
            close_fds=True,
            start_new_session=True,
        )
        readiness(process)
        yield ManagedProcess(
            command=command,
            pid=process.pid,
            stdout_path=stdout_path,
            stderr_path=stderr_path,
        )
    except BaseException:
        original_error = True
        raise
    finally:
        if process is not None:
            try:
                _terminate_process_group(process)
            except Exception:
                if not original_error:
                    raise
        stdout.close()
        stderr.close()


def _wait_for_model(
    *,
    base_url: str,
    expected_model: str,
    process: subprocess.Popen[bytes],
    timeout_seconds: float,
) -> None:
    deadline = time.monotonic() + timeout_seconds
    with httpx.Client(timeout=2.0, trust_env=False) as client:
        while time.monotonic() < deadline:
            return_code = process.poll()
            if return_code is not None:
                raise RuntimeError(f"vLLM exited during startup with code {return_code}")
            try:
                response = client.get(f"{base_url}/v1/models")
                if response.is_success:
                    models = {
                        item.get("id")
                        for item in response.json().get("data", [])
                        if isinstance(item, dict)
                    }
                    if expected_model in models:
                        return
            except (httpx.HTTPError, ValueError):
                pass
            time.sleep(_STARTUP_POLL_SECONDS)
    raise TimeoutError(f"vLLM did not serve {expected_model!r} before timeout")


def _wait_for_health(
    *,
    url: str,
    process: subprocess.Popen[bytes],
    timeout_seconds: float,
) -> None:
    deadline = time.monotonic() + timeout_seconds
    with httpx.Client(timeout=2.0, trust_env=False) as client:
        while time.monotonic() < deadline:
            return_code = process.poll()
            if return_code is not None:
                raise RuntimeError(f"Gateway exited during startup with code {return_code}")
            try:
                if client.get(url).is_success:
                    return
            except httpx.HTTPError:
                pass
            time.sleep(_STARTUP_POLL_SECONDS)
    raise TimeoutError("OpenEvo Gateway did not become healthy before timeout")


def _terminate_process_group(process: subprocess.Popen[bytes]) -> None:
    if process.poll() is not None:
        process.wait(timeout=1)
        return
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    try:
        process.wait(timeout=_PROCESS_STOP_SECONDS)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        process.wait(timeout=_PROCESS_STOP_SECONDS)


def _write_gateway_topology(
    root: Path,
    *,
    gateway_port: int,
    gateway_advertise_host: str,
    vllm_url: str,
    served_model: str,
) -> Path:
    path = root / "gateway-topology.json"
    payload = {
        "rollout": {
            "host": "127.0.0.1",
            "port": 65530,
            "public_url": "http://127.0.0.1:65530",
            "save_dir": str(root / "unused-rollout-results"),
        },
        "gateway": {
            "heartbeat_interval_seconds": 30,
            "nodes": [
                {
                    "id": "tb21-local",
                    "host": "0.0.0.0",
                    "port": gateway_port,
                    "public_url": (f"http://{gateway_advertise_host}:{gateway_port}"),
                    "max_init_workers": 1,
                    "max_run_workers": 1,
                    "max_postrun_workers": 1,
                    "model_served": served_model,
                    "inference": {"engine": "vllm", "base_url": vllm_url},
                }
            ],
        },
    }
    path.write_text(canonical_json(payload) + "\n", encoding="utf-8")
    return path


def resolve_gateway_advertise_host(explicit_host: str | None = None) -> str:
    configured = explicit_host or os.environ.get("OPENEVO_TB_GATEWAY_ADVERTISE_HOST")
    if configured:
        address = ipaddress.ip_address(configured)
        if not isinstance(address, ipaddress.IPv4Address) or address.is_unspecified:
            raise ValueError("gateway advertise host must be a concrete IPv4 address")
        return str(address)

    addresses = {
        ipaddress.ip_address(item[4][0])
        for item in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET)
    }
    non_loopback = sorted(
        str(address)
        for address in addresses
        if isinstance(address, ipaddress.IPv4Address)
        and not address.is_loopback
        and not address.is_unspecified
    )
    if len(non_loopback) != 1:
        raise ValueError(
            "could not select one gateway address reachable by the Docker daemon; "
            "set --gateway-advertise-host"
        )
    return non_loopback[0]


def _directory_size(path: Path) -> int:
    total = 0
    for root, directories, filenames in os.walk(path, followlinks=False):
        if any((Path(root) / name).is_symlink() for name in directories):
            raise ValueError("adapter directory contains a symlink")
        for filename in filenames:
            child = Path(root) / filename
            if child.is_symlink() or not child.is_file():
                raise ValueError("adapter payload contains a non-regular file")
            total += child.stat().st_size
    return total


def _supported_vllm_lora_rank(required: int) -> int:
    for candidate in (8, 16, 32, 64, 128, 256, 320, 512):
        if required <= candidate:
            return candidate
    raise ValueError("adapter rank exceeds vLLM's configured LoRA limit")


def _safe_name(value: str) -> str:
    normalized = "".join(
        character if character.isalnum() or character in {"-", "_"} else "-" for character in value
    ).strip("-")
    if not normalized:
        normalized = hashlib.sha256(value.encode("utf-8")).hexdigest()[:16]
    return normalized[:120]


def _source_pythonpath(existing: str | None) -> str:
    entries = [
        str(Path(__file__).resolve().parents[1]),
        str(Path(openevo.__file__).resolve().parents[1]),
    ]
    if existing:
        entries.extend(
            str((Path.cwd() / entry).resolve()) if not Path(entry).is_absolute() else entry
            for entry in existing.split(os.pathsep)
            if entry
        )
    return os.pathsep.join(dict.fromkeys(entries))


def _evaluation_process_environments(
    *,
    gpu: str,
    inherited: dict[str, str] | None = None,
) -> tuple[dict[str, str], dict[str, str]]:
    base_env = dict(os.environ if inherited is None else inherited)
    base_env["CUDA_VISIBLE_DEVICES"] = gpu
    gateway_env = dict(base_env)
    gateway_env["PYTHONPATH"] = _source_pythonpath(gateway_env.get("PYTHONPATH"))
    vllm_env = dict(base_env)
    vllm_env.pop("PYTHONPATH", None)
    return vllm_env, gateway_env


def _process_metadata(process: ManagedProcess) -> dict[str, Any]:
    return {
        "command": list(process.command),
        "pid": process.pid,
        "stdout_path": str(process.stdout_path),
        "stderr_path": str(process.stderr_path),
    }


__all__ = [
    "AdapterServingSpec",
    "ContinualTask",
    "DEFAULT_AGENT_TIMEOUT_SECONDS",
    "DEFAULT_GATEWAY_PORT",
    "DEFAULT_LOCAL_MODEL",
    "DEFAULT_MAX_MODEL_LENGTH",
    "DEFAULT_VLLM_EXECUTABLE",
    "DEFAULT_VLLM_PORT",
    "build_core_codex_harbor_command",
    "build_vllm_command",
    "continual_learning_metrics",
    "parse_continual_tasks",
    "run_continual_memory_eval",
    "run_continual_memory_eval_dry_run",
    "resolve_gateway_advertise_host",
]
