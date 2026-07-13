from __future__ import annotations

import ast
from dataclasses import dataclass, field
import json
from pathlib import Path
import re
import subprocess
from typing import Any
from urllib.parse import unquote, urlparse

import openevo.evolution.agent_system_gepa_kernel as gepa_kernel
from openevo.evolution.methods import run_method
from openevo.evolution.models import (
    WorkerClaimRequest,
    WorkerCompleteRequest,
    WorkerFailRequest,
    WorkerHeartbeatRequest,
)
from openevo.evolution.store import EvolutionStore
from openevo.evolution.terminal_bench_bridge import (
    TerminalBenchBridgeError,
    build_terminal_bench_events,
)

LOCAL_WORKER_LEASE_SECONDS = 24 * 60 * 60
DEFAULT_TERMINAL_BENCH_PACKAGE_ROOT = Path("/root/EvoLabCore-terminal-bench-task-package")
DEFAULT_TERMINAL_BENCH_ENVIRONMENT_IMPORT_PATH = (
    "task_packages.terminal_bench_v1.harbor_environment:DockerCpHarborEnvironment"
)
DEFAULT_TERMINAL_BENCH_EXTRA_DOCKER_COMPOSE = [
    DEFAULT_TERMINAL_BENCH_PACKAGE_ROOT
    / "task_packages"
    / "terminal_bench_v1"
    / "harbor"
    / "pull-never.yaml",
    DEFAULT_TERMINAL_BENCH_PACKAGE_ROOT
    / "task_packages"
    / "terminal_bench_v1"
    / "harbor"
    / "docker-cp-host-network.yaml",
]


@dataclass(frozen=True)
class TerminalBenchTaskGroup:
    group_id: str
    task_ids: list[str]
    objective: str = "macro_mean_reward"


@dataclass(frozen=True)
class EvolutionArtifact:
    artifact_type: str
    artifact_id: str
    path: Path
    task_id: str
    round: int
    method: str
    source_dataset_artifact_ids: list[str] = field(default_factory=list)
    candidate_index: int | None = None
    candidate_strategy: str | None = None


class ArtifactMaterializer:
    def __init__(self) -> None:
        self.skipped: list[dict[str, str]] = []

    def materialize(self, artifact: EvolutionArtifact) -> dict[str, str]:
        if artifact.artifact_type == "agent_system":
            return {"agent_system_path": str(artifact.path)}
        if artifact.artifact_type in {"text_memory", "memory"}:
            return {"memory_path": str(artifact.path)}
        if artifact.artifact_type in {
            "skill_bundle",
            "parametric_memory",
        }:
            self.skipped.append(
                {
                    "artifact_id": artifact.artifact_id,
                    "artifact_type": artifact.artifact_type,
                    "reason": f"{artifact.artifact_type} materialization is not implemented for Harbor Codex runs",
                }
            )
            return {}
        raise ValueError(f"unsupported evolution artifact type: {artifact.artifact_type}")


def build_harbor_command(
    *,
    job_name: str,
    task_root: Path,
    task_id: str,
    jobs_dir: Path | None = None,
    model: str,
    env_json: dict[str, str],
    agent_kwargs: dict[str, str],
    verifier_env: dict[str, str],
    n_concurrent: int,
    n_attempts: int = 1,
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
    ]
    if jobs_dir is not None:
        command.extend(["--jobs-dir", str(jobs_dir)])
    command.extend(
        [
            "--include-task-name",
            task_id,
            "--n-attempts",
            str(max(1, int(n_attempts))),
            "--n-concurrent",
            str(n_concurrent),
            "--agent-import-path",
            "task_packages.terminal_bench_v1.harbor_agent:EvoLabHarborAgent",
            "--model",
            model,
            "--ak",
            "mode=codex_subscription",
            "--ak",
            f"env_json={json.dumps(env_json, sort_keys=True, separators=(',', ':'))}",
        ]
    )
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
    for key in sorted(agent_kwargs):
        command.extend(["--ak", f"{key}={agent_kwargs[key]}"])
    for key in sorted(verifier_env):
        command.extend(["--verifier-env", f"{key}={verifier_env[key]}"])
    return command


def _normalize_terminal_bench_artifact_type(artifact_type: str) -> str:
    normalized = artifact_type.strip()
    if normalized == "memory":
        return "text_memory"
    return normalized


def _single_live_artifact_type(artifact_types: list[str] | None) -> str:
    normalized = [
        _normalize_terminal_bench_artifact_type(artifact_type)
        for artifact_type in (artifact_types or ["agent_system"])
    ]
    if any(artifact_type == "parametric_memory" for artifact_type in normalized):
        raise ValueError("Terminal Bench Codex subscription runs do not support parametric_memory")
    if len(normalized) != 1:
        raise ValueError("live Terminal Bench evolution requires exactly one artifact type")
    if normalized[0] not in {"agent_system", "text_memory"}:
        raise ValueError(
            "live Terminal Bench evolution currently supports only agent_system or text_memory"
        )
    return normalized[0]


def _terminal_bench_extra_docker_compose(package_root: Path) -> list[Path]:
    harbor_root = package_root / "task_packages" / "terminal_bench_v1" / "harbor"
    return [
        harbor_root / "pull-never.yaml",
        harbor_root / "docker-cp-host-network.yaml",
    ]


def _agent_init_supports_kwarg(agent_path: Path, keyword: str) -> bool:
    try:
        module = ast.parse(agent_path.read_text(encoding="utf-8"), filename=str(agent_path))
    except (OSError, SyntaxError):
        return False
    for node in module.body:
        if not isinstance(node, ast.ClassDef) or node.name != "EvoLabHarborAgent":
            continue
        for item in node.body:
            if not isinstance(item, ast.FunctionDef) or item.name != "__init__":
                continue
            args = item.args
            if args.kwarg is not None:
                return True
            parameters = [*args.posonlyargs, *args.args, *args.kwonlyargs]
            return any(parameter.arg == keyword for parameter in parameters)
    return False


def _ensure_terminal_bench_package_supports_artifact_type(
    package_root: Path,
    artifact_type: str,
) -> None:
    if artifact_type != "text_memory":
        return
    agent_path = package_root / "task_packages" / "terminal_bench_v1" / "harbor_agent.py"
    if not _agent_init_supports_kwarg(agent_path, "memory_path"):
        raise ValueError(
            "Terminal Bench text_memory runs require EvoLabHarborAgent to accept "
            f"`memory_path`; checked {agent_path}"
        )


def _should_preflight_terminal_bench_package(
    *,
    terminal_bench_package_root: Path,
    command_runner,
) -> bool:
    return (
        command_runner is _default_command_runner
        or terminal_bench_package_root != DEFAULT_TERMINAL_BENCH_PACKAGE_ROOT
    )


def _default_command_runner(command: list[str], cwd: Path | None = None) -> dict[str, Any]:
    subprocess.run(command, cwd=cwd, check=True)
    return {}


def _default_evolved_trial_locator(task_id: str, round_number: int, run_root: Path) -> Path:
    round_root = run_root / "tasks" / task_id / f"r{round_number}"
    candidates = [
        path
        for path in round_root.rglob(f"{task_id}__*")
        if path.is_dir() and (path / "result.json").is_file()
    ]
    if not candidates:
        raise FileNotFoundError(
            "no evolved Terminal Bench trial found under "
            f"{round_root} for task {task_id!r} round {round_number}"
        )
    return max(candidates, key=lambda path: (path.stat().st_mtime_ns, path.as_posix()))


def _locate_evolved_attempt_trials(*, task_id: str, job_root: Path) -> list[Path]:
    candidates = [
        path
        for path in job_root.glob(f"{task_id}__*")
        if path.is_dir() and (path / "result.json").is_file()
    ]
    if not candidates:
        raise FileNotFoundError(
            f"no evolved Terminal Bench attempts found under {job_root} for task {task_id!r}"
        )
    return sorted(candidates, key=_attempt_trial_sort_key)


def _attempt_trial_sort_key(path: Path) -> tuple[int, str | int, str]:
    result_path = path / "result.json"
    try:
        payload = json.loads(result_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        payload = {}
    started_at = payload.get("started_at") if isinstance(payload, dict) else None
    if isinstance(started_at, str) and started_at.strip():
        return (0, started_at, path.as_posix())
    return (1, path.stat().st_mtime_ns, path.as_posix())


def _default_group_evolved_trial_locator(
    group_id: str,
    task_id: str,
    round_number: int,
    run_root: Path,
    search_root: Path | None = None,
) -> Path:
    round_root = search_root or run_root / "groups" / group_id / f"r{round_number}"
    candidates = [
        path
        for path in round_root.rglob(f"{task_id}__*")
        if path.is_dir() and (path / "result.json").is_file()
    ]
    if not candidates:
        raise FileNotFoundError(
            "no evolved Terminal Bench trial found under "
            f"{round_root} for group {group_id!r} task {task_id!r} round {round_number}"
        )
    return max(candidates, key=lambda path: (path.stat().st_mtime_ns, path.as_posix()))


def _find_baseline_trial(baseline_root: Path, task_id: str) -> Path:
    if _is_matching_trial_dir(baseline_root, task_id):
        return baseline_root

    direct_trial = baseline_root / task_id
    if _is_matching_trial_dir(direct_trial, task_id):
        return direct_trial

    candidates = [
        path
        for path in baseline_root.glob(f"{task_id}__*")
        if path.is_dir() and (path / "result.json").is_file()
    ]
    if not candidates:
        raise FileNotFoundError(
            f"no baseline Terminal Bench trial found for task {task_id!r} under {baseline_root}"
        )
    return max(candidates, key=lambda path: (path.stat().st_mtime_ns, path.as_posix()))


def _trial_reward(trial_dir: Path) -> float | None:
    try:
        events = build_terminal_bench_events(trial_dir)
    except Exception as exc:
        fallback_reward = _fallback_trial_reward(trial_dir)
        if fallback_reward is not None:
            return fallback_reward
        raise exc
    if len(events) != 1:
        fallback_reward = _fallback_trial_reward(trial_dir)
        if fallback_reward is not None:
            return fallback_reward
        raise ValueError(
            f"expected exactly one Terminal Bench event for {trial_dir}, found {len(events)}"
        )
    reward = events[0].reward
    if reward is not None:
        return reward
    return _fallback_trial_reward(trial_dir)


def _attempt_reward(trial_dir: Path) -> float | None:
    try:
        return _trial_reward(trial_dir)
    except TerminalBenchBridgeError:
        return None


def _create_agent_system_job_command(
    *,
    task_id: str,
    round_number: int,
    input_trial_dirs: list[Path],
    previous_dataset_artifact_ids: list[str],
    agent_system_method: str,
    gepa_candidate_count: int,
    gepa_generation: int | None = None,
    db_path: Path,
    artifact_root: Path,
    reflector_model: str,
    reflector_provider: str,
    reflector_timeout_seconds: float | None,
    codex_home: str | None,
    output_path: Path,
) -> list[str]:
    if not input_trial_dirs:
        raise ValueError("agent system job command requires at least one input trial")
    input_round = round_number - 1
    generation_suffix = "" if gepa_generation is None else f"_g{gepa_generation}"
    if agent_system_method == "auto":
        method = (
            "agent_system_history_reflector"
            if previous_dataset_artifact_ids
            else "agent_system_reflector"
        )
    else:
        method = agent_system_method
    command = [
        "uv",
        "run",
        "python",
        "-m",
        "openevo.evolution.cli",
        "terminal-bench-agent-system-job",
        "--db",
        str(db_path),
        "--artifact-root",
        str(artifact_root),
        "--dataset-name",
        f"{task_id}_r{input_round}{generation_suffix}",
        "--policy-version",
        f"tb21-{task_id}-r{input_round}{generation_suffix}",
        "--method",
        method,
        "--job-name",
        f"tb21-{task_id}-r{round_number}{generation_suffix}",
        "--reflector-provider",
        reflector_provider,
        "--reflector-model",
        reflector_model,
    ]
    if reflector_timeout_seconds is not None:
        command.extend(["--reflector-timeout-seconds", str(reflector_timeout_seconds)])
    for input_trial_dir in input_trial_dirs:
        command.extend(["--input", str(input_trial_dir)])
    if method == "agent_system_gepa_reflector":
        command.extend(["--candidate-count", str(gepa_candidate_count)])
    if codex_home:
        command.extend(["--codex-home", codex_home])
    for artifact_id in previous_dataset_artifact_ids:
        command.extend(["--dataset-artifact-id", artifact_id])
    command.extend(["--output", str(output_path)])
    return command


def _create_text_memory_job_command(
    *,
    task_id: str,
    round_number: int,
    input_trial_dirs: list[Path],
    previous_dataset_artifact_ids: list[str],
    memory_method: str,
    db_path: Path,
    artifact_root: Path,
    reflector_model: str,
    reflector_provider: str,
    reflector_timeout_seconds: float | None,
    codex_home: str | None,
    output_path: Path,
) -> list[str]:
    if not input_trial_dirs:
        raise ValueError("text memory job command requires at least one input trial")
    input_round = round_number - 1
    command = [
        "uv",
        "run",
        "python",
        "-m",
        "openevo.evolution.cli",
        "terminal-bench-text-memory-job",
        "--db",
        str(db_path),
        "--artifact-root",
        str(artifact_root),
        "--dataset-name",
        f"{task_id}_r{input_round}",
        "--policy-version",
        f"tb21-{task_id}-r{input_round}",
        "--method",
        memory_method,
        "--job-name",
        f"tb21-{task_id}-r{round_number}",
        "--reflector-provider",
        reflector_provider,
        "--reflector-model",
        reflector_model,
    ]
    if reflector_timeout_seconds is not None:
        command.extend(["--reflector-timeout-seconds", str(reflector_timeout_seconds)])
    for input_trial_dir in input_trial_dirs:
        command.extend(["--input", str(input_trial_dir)])
    if codex_home:
        command.extend(["--codex-home", codex_home])
    for artifact_id in previous_dataset_artifact_ids:
        command.extend(["--dataset-artifact-id", artifact_id])
    command.extend(["--output", str(output_path)])
    return command


def _run_worker_once_local(*, db_path: Path, artifact_root: Path) -> list[dict[str, Any]]:
    store = EvolutionStore(db_path=db_path, artifact_root=artifact_root)
    store.initialize()
    claimed = store.claim_job(
        WorkerClaimRequest(
            worker_id="local-worker",
            lease_seconds=LOCAL_WORKER_LEASE_SECONDS,
        )
    )
    if claimed.job is None:
        raise FileNotFoundError(f"no local evolution job available in {db_path}")

    job = claimed.job
    try:
        store.heartbeat_job(
            job.job_id,
            WorkerHeartbeatRequest(
                lease_id=job.lease_id,
                progress=0.0,
                message="claimed",
            ),
        )
        artifacts = run_method(job, artifact_root=artifact_root)
        store.heartbeat_job(
            job.job_id,
            WorkerHeartbeatRequest(
                lease_id=job.lease_id,
                progress=1.0,
                message="completed",
            ),
        )
        completion = store.complete_job(
            job.job_id,
            WorkerCompleteRequest(
                lease_id=job.lease_id,
                artifacts=artifacts,
                report={"method": job.method, "artifact_count": len(artifacts)},
            ),
        )
    except Exception as exc:
        try:
            store.fail_job(
                job.job_id,
                WorkerFailRequest(lease_id=job.lease_id, error=str(exc), retryable=False),
            )
        except Exception as fail_exc:
            exc.add_note(f"local fail_job cleanup failed: {fail_exc}")
        raise

    artifact_ids = completion.get("artifact_ids")
    if not isinstance(artifact_ids, list) or len(artifact_ids) != len(artifacts):
        raise ValueError("completed local worker job returned invalid artifact_ids")

    completed_artifacts: list[dict[str, Any]] = []
    for artifact_id, artifact in zip(artifact_ids, artifacts):
        completed_artifacts.append(
            {
                "artifact_id": artifact_id,
                "type": str(artifact.type),
                "uri": artifact.uri,
                "manifest": dict(artifact.manifest),
            }
        )
    return completed_artifacts


def _is_matching_trial_dir(path: Path, task_id: str) -> bool:
    result_path = path / "result.json"
    if not result_path.is_file():
        return False
    if path.name == task_id or path.name.startswith(f"{task_id}__"):
        return True
    result = _read_trial_result(result_path)
    inferred_task_id = _infer_trial_task_id(result)
    return inferred_task_id == task_id


def _fallback_trial_reward(trial_dir: Path) -> float | None:
    result = _read_trial_result(trial_dir / "result.json")
    nested_reward = (
        result.get("verifier_result", {}).get("rewards", {}).get("reward")
        if isinstance(result.get("verifier_result"), dict)
        else None
    )
    if isinstance(nested_reward, int | float):
        return float(nested_reward)
    reward_text = (
        (trial_dir / "verifier" / "reward.txt")
        .read_text(
            encoding="utf-8",
            errors="replace",
        )
        .strip()
        if (trial_dir / "verifier" / "reward.txt").is_file()
        else ""
    )
    if reward_text:
        return float(reward_text)
    return None


def _read_trial_result(result_path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(result_path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except json.JSONDecodeError:
        return {}
    return payload if isinstance(payload, dict) else {}


def _infer_trial_task_id(result: dict[str, Any]) -> str | None:
    for key in ("task_name", "task_id"):
        value = result.get(key)
        if isinstance(value, str) and value.strip():
            return value
    agent_result = result.get("agent_result")
    if not isinstance(agent_result, dict):
        return None
    metadata = agent_result.get("metadata")
    if not isinstance(metadata, dict):
        return None
    harbor_agent = metadata.get("terminal_bench_harbor_agent")
    if not isinstance(harbor_agent, dict):
        return None
    task_id = harbor_agent.get("task_id")
    return task_id.strip() if isinstance(task_id, str) and task_id.strip() else None


def run_per_task_evolution_dry_run(
    *,
    task_root: Path,
    task_ids: list[str],
    run_root: Path,
    model: str,
    reflector_model: str,
    reflector_provider: str,
    reflector_timeout_seconds: float | None,
    terminal_bench_package_root: Path = DEFAULT_TERMINAL_BENCH_PACKAGE_ROOT,
    agent_system_method: str,
    memory_method: str = "text_memory_expel_reflector",
    gepa_candidate_count: int,
    gepa_generations: int,
    rounds: int,
    artifact_types: list[str],
    n_attempts: int = 1,
) -> dict[str, Any]:
    return {
        "dry_run": True,
        "task_root": str(task_root),
        "run_root": str(run_root),
        "model": model,
        "reflector_model": reflector_model,
        "reflector_provider": reflector_provider,
        "reflector_timeout_seconds": reflector_timeout_seconds,
        "terminal_bench_package_root": str(terminal_bench_package_root),
        "agent_system_method": agent_system_method,
        "memory_method": memory_method,
        "gepa_candidate_count": gepa_candidate_count,
        "gepa_generations": gepa_generations,
        "n_attempts": max(1, int(n_attempts)),
        "tasks": [
            {"task_id": task_id, "rounds": rounds, "artifact_types": artifact_types}
            for task_id in task_ids
        ],
    }


def run_group_evolution_dry_run(
    *,
    task_root: Path,
    groups: list[TerminalBenchTaskGroup],
    run_root: Path,
    model: str,
    reflector_model: str,
    reflector_provider: str,
    reflector_timeout_seconds: float | None,
    terminal_bench_package_root: Path = DEFAULT_TERMINAL_BENCH_PACKAGE_ROOT,
    agent_system_method: str,
    gepa_candidate_count: int,
    gepa_generations: int,
    rounds: int,
    artifact_types: list[str],
) -> dict[str, Any]:
    validated_groups = [_validate_task_group(group) for group in groups]
    return {
        "dry_run": True,
        "mode": "group",
        "task_root": str(task_root),
        "run_root": str(run_root),
        "model": model,
        "reflector_model": reflector_model,
        "reflector_provider": reflector_provider,
        "reflector_timeout_seconds": reflector_timeout_seconds,
        "terminal_bench_package_root": str(terminal_bench_package_root),
        "agent_system_method": agent_system_method,
        "gepa_candidate_count": gepa_candidate_count,
        "gepa_generations": gepa_generations,
        "groups": [
            {
                "group_id": group.group_id,
                "task_ids": list(group.task_ids),
                "objective": group.objective,
                "rounds": rounds,
                "artifact_types": artifact_types,
            }
            for group in validated_groups
        ],
    }


def run_per_task_evolution(
    *,
    task_root: Path,
    task_ids: list[str],
    run_root: Path,
    baseline_root: Path,
    model: str,
    reflector_model: str,
    reflector_provider: str = "codex_cli",
    reflector_timeout_seconds: float | None = None,
    codex_home: str | None = None,
    terminal_bench_package_root: Path = DEFAULT_TERMINAL_BENCH_PACKAGE_ROOT,
    agent_system_method: str = "auto",
    memory_method: str = "text_memory_expel_reflector",
    gepa_candidate_count: int = 1,
    gepa_generations: int = 1,
    rounds: int,
    artifact_types: list[str] | None = None,
    n_attempts: int = 1,
    env_json: dict[str, str],
    verifier_env: dict[str, str],
    command_runner=_default_command_runner,
    worker_runner=_run_worker_once_local,
    evolved_trial_locator=_default_evolved_trial_locator,
) -> dict[str, Any]:
    summary = {
        "dry_run": False,
        "task_root": str(task_root),
        "run_root": str(run_root),
        "baseline_root": str(baseline_root),
        "model": model,
        "reflector_model": reflector_model,
        "reflector_provider": reflector_provider,
        "reflector_timeout_seconds": reflector_timeout_seconds,
        "terminal_bench_package_root": str(terminal_bench_package_root),
        "agent_system_method": agent_system_method,
        "memory_method": memory_method,
        "gepa_candidate_count": gepa_candidate_count,
        "gepa_generations": gepa_generations,
        "n_attempts": max(1, int(n_attempts)),
        "artifact_types": [
            _normalize_terminal_bench_artifact_type(artifact_type)
            for artifact_type in (artifact_types or ["agent_system"])
        ],
        "tasks": [],
    }
    selected_artifact_type = _single_live_artifact_type(artifact_types)
    attempt_count = max(1, int(n_attempts))
    if _should_preflight_terminal_bench_package(
        terminal_bench_package_root=terminal_bench_package_root,
        command_runner=command_runner,
    ):
        _ensure_terminal_bench_package_supports_artifact_type(
            terminal_bench_package_root,
            selected_artifact_type,
        )

    for task_id in task_ids:
        baseline_trial = _find_baseline_trial(baseline_root, task_id)
        baseline_reward = _trial_reward(baseline_trial)
        task_summary = {
            "task_id": task_id,
            "baseline_trial": str(baseline_trial),
            "baseline_reward": baseline_reward,
            "rounds": [],
        }
        task_store_root = run_root / "tasks" / task_id / "evolution"
        db_path = task_store_root / "evolution.db"
        artifact_root = task_store_root / "artifacts"
        input_trial_dir = baseline_trial
        previous_reward = baseline_reward
        next_round_inputs = (baseline_trial,)
        dataset_history: tuple[str, ...] = ()
        effective_gepa_generations = (
            max(1, gepa_generations)
            if (
                selected_artifact_type == "agent_system"
                and agent_system_method == "agent_system_gepa_reflector"
            )
            else 1
        )

        for round_number in range(1, rounds + 1):
            round_root = run_root / "tasks" / task_id / f"r{round_number}"
            round_root.mkdir(parents=True, exist_ok=True)
            round_state = gepa_kernel.begin_round(
                generation_inputs=next_round_inputs,
                dataset_history=dataset_history,
            )
            round_generation_summaries: list[dict[str, Any]] = []
            round_candidate_results: list[dict[str, Any]] = []
            round_candidate_evaluations: list[
                gepa_kernel.CandidateEvaluation[Path]
            ] = []

            for generation_number in range(1, effective_gepa_generations + 1):
                generation_inputs = list(round_state.generation_inputs)
                generation_root = (
                    round_root
                    if effective_gepa_generations == 1
                    else round_root / f"g{generation_number}"
                )
                generation_root.mkdir(parents=True, exist_ok=True)
                if selected_artifact_type == "agent_system":
                    job_output_path = generation_root / "agent_system_job.json"
                    job_command = _create_agent_system_job_command(
                        task_id=task_id,
                        round_number=round_number,
                        input_trial_dirs=generation_inputs,
                        previous_dataset_artifact_ids=list(round_state.dataset_history),
                        agent_system_method=agent_system_method,
                        gepa_candidate_count=gepa_candidate_count,
                        gepa_generation=(
                            generation_number if effective_gepa_generations > 1 else None
                        ),
                        db_path=db_path,
                        artifact_root=artifact_root,
                        reflector_model=reflector_model,
                        reflector_provider=reflector_provider,
                        reflector_timeout_seconds=reflector_timeout_seconds,
                        codex_home=codex_home,
                        output_path=job_output_path,
                    )
                else:
                    job_output_path = generation_root / "text_memory_job.json"
                    job_command = _create_text_memory_job_command(
                        task_id=task_id,
                        round_number=round_number,
                        input_trial_dirs=generation_inputs,
                        previous_dataset_artifact_ids=list(round_state.dataset_history),
                        memory_method=memory_method,
                        db_path=db_path,
                        artifact_root=artifact_root,
                        reflector_model=reflector_model,
                        reflector_provider=reflector_provider,
                        reflector_timeout_seconds=reflector_timeout_seconds,
                        codex_home=codex_home,
                        output_path=job_output_path,
                    )
                command_runner(job_command)

                job_payload = json.loads(job_output_path.read_text(encoding="utf-8"))
                if not isinstance(job_payload, dict):
                    raise ValueError(f"expected job payload object JSON in {job_output_path}")

                dataset_payload = job_payload.get("dataset")
                dataset_artifact_id: str | None = None
                if isinstance(dataset_payload, dict):
                    raw_dataset_artifact_id = dataset_payload.get("artifact_id")
                    if (
                        isinstance(raw_dataset_artifact_id, str)
                        and raw_dataset_artifact_id.strip()
                    ):
                        dataset_artifact_id = raw_dataset_artifact_id
                round_state = gepa_kernel.record_dataset(
                    round_state,
                    dataset_artifact_id,
                )

                completed_artifacts = worker_runner(db_path=db_path, artifact_root=artifact_root)
                evolution_artifacts = discover_evolution_artifact_paths(
                    completed_artifacts,
                    artifact_type=selected_artifact_type,
                    task_id=task_id,
                    round_number=round_number,
                    job_payload=job_payload,
                )
                generation_candidate_results: list[dict[str, Any]] = []
                for candidate_position, artifact in enumerate(
                    evolution_artifacts,
                    start=1,
                ):
                    materializer = ArtifactMaterializer()
                    agent_kwargs = materializer.materialize(artifact)
                    candidate_index = gepa_kernel.resolve_candidate_index(
                        candidate_id=artifact.artifact_id,
                        explicit_candidate_index=artifact.candidate_index,
                        fallback_candidate_index=candidate_position,
                    )
                    generation_suffix = (
                        "" if effective_gepa_generations == 1 else f"-g{generation_number}"
                    )
                    candidate_suffix = (
                        "" if len(evolution_artifacts) == 1 else f"-c{candidate_index}"
                    )
                    harbor_command = build_harbor_command(
                        job_name=f"{task_id}-r{round_number}{generation_suffix}{candidate_suffix}",
                        task_root=task_root,
                        task_id=task_id,
                        jobs_dir=generation_root / "harbor_jobs",
                        model=model,
                        env_json=env_json,
                        agent_kwargs=agent_kwargs,
                        verifier_env=verifier_env,
                        n_concurrent=1,
                        n_attempts=attempt_count,
                        extra_docker_compose=_terminal_bench_extra_docker_compose(
                            terminal_bench_package_root
                        ),
                    )
                    command_runner(harbor_command, cwd=terminal_bench_package_root)
                    harbor_job_name = harbor_command[
                        harbor_command.index("--job-name") + 1
                    ]
                    if attempt_count == 1:
                        attempt_trials = [
                            evolved_trial_locator(task_id, round_number, run_root)
                        ]
                    else:
                        attempt_trials = _locate_evolved_attempt_trials(
                            task_id=task_id,
                            job_root=generation_root / "harbor_jobs" / harbor_job_name,
                        )
                    attempts = [
                        {
                            "attempt_index": attempt_index,
                            "trial_dir": attempt_trial,
                            "reward": _attempt_reward(attempt_trial),
                        }
                        for attempt_index, attempt_trial in enumerate(
                            attempt_trials,
                            start=1,
                        )
                    ]
                    best_attempt = _select_best_attempt(attempts)
                    evolved_trial = best_attempt["trial_dir"]
                    reward = best_attempt["reward"]
                    generation_candidate_results.append(
                        {
                            "artifact": artifact,
                            "materializer": materializer,
                            "trial_dir": evolved_trial,
                            "reward": reward,
                            "attempts": attempts,
                            "pass_at_k": any(
                                _reward_passed(attempt.get("reward"))
                                for attempt in attempts[:attempt_count]
                            ),
                            "generation": generation_number,
                        }
                    )

                generation_candidate_evaluations = [
                    gepa_kernel.per_task_candidate(
                        candidate_id=result["artifact"].artifact_id,
                        source_index=len(round_candidate_results) + position - 1,
                        explicit_candidate_index=result["artifact"].candidate_index,
                        fallback_candidate_index=0,
                        generation=generation_number,
                        reward=result["reward"],
                        trial=result["trial_dir"],
                    )
                    for position, result in enumerate(
                        generation_candidate_results,
                        start=1,
                    )
                ]
                round_candidate_results.extend(generation_candidate_results)
                round_candidate_evaluations.extend(generation_candidate_evaluations)
                round_generation_summaries.append(
                    {
                        "generation": generation_number,
                        "input_trials": [str(path) for path in generation_inputs],
                        "dataset_artifact_id": dataset_artifact_id,
                        "candidate_trials": [
                            _candidate_trial_summary(
                                result,
                                position,
                                include_generation=False,
                            )
                            for position, result in enumerate(
                                generation_candidate_results,
                                start=1,
                            )
                        ],
                    }
                )
                round_state = gepa_kernel.advance_generation(
                    round_state,
                    generation_candidate_evaluations,
                )

            round_transition = gepa_kernel.complete_round(
                round_state,
                round_candidate_evaluations,
            )
            best_candidate = round_candidate_results[round_transition.winner.source_index]
            artifact = best_candidate["artifact"]
            materializer = best_candidate["materializer"]
            evolved_trial = best_candidate["trial_dir"]
            reward = best_candidate["reward"]

            round_summary = {
                "round": round_number,
                "input_trial": str(input_trial_dir),
                "trial_dir": str(evolved_trial),
                "reward": reward,
                "transition": summarize_transition(previous_reward, reward),
                "artifact": {
                    "artifact_id": artifact.artifact_id,
                    "artifact_type": artifact.artifact_type,
                    "path": str(artifact.path),
                    "method": artifact.method,
                    "source_dataset_artifact_ids": artifact.source_dataset_artifact_ids,
                },
                "skipped_artifacts": list(materializer.skipped),
            }
            if len(best_candidate.get("attempts") or []) > 1:
                round_summary["attempt_count"] = len(best_candidate["attempts"])
                round_summary["attempts"] = [
                    {
                        "attempt_index": attempt["attempt_index"],
                        "reward": attempt["reward"],
                        "trial_dir": str(attempt["trial_dir"]),
                    }
                    for attempt in best_candidate["attempts"]
                ]
                round_summary["pass_at_k"] = bool(best_candidate.get("pass_at_k", False))
            if len(round_candidate_results) > 1:
                round_summary["candidate_trials"] = [
                    _candidate_trial_summary(
                        result,
                        position,
                        include_generation=effective_gepa_generations > 1,
                    )
                    for position, result in enumerate(round_candidate_results, start=1)
                ]
            if round_transition.round_dataset_ids:
                round_summary["dataset_artifact_id"] = round_transition.round_dataset_ids[0]
                round_summary["dataset_artifact_ids"] = list(
                    round_transition.round_dataset_ids
                )
            if effective_gepa_generations > 1:
                round_summary["gepa_generations"] = len(round_generation_summaries)
                round_summary["generations"] = round_generation_summaries

            task_summary["rounds"].append(round_summary)
            next_round_inputs = round_transition.next_round_inputs
            dataset_history = round_transition.dataset_history
            input_trial_dir = next_round_inputs[0]
            previous_reward = reward

        summary["tasks"].append(task_summary)

    if selected_artifact_type == "text_memory":
        summary["memory_benchmark"] = build_memory_benchmark_summary(
            model=model,
            auth_mode="subscription",
            memory_backend=memory_method,
            baseline_pass_at_1=_baseline_pass_at_1_from_tasks(summary["tasks"]),
            baseline_pass_at_5=None,
            attempts_by_task=_memory_attempts_by_task(summary["tasks"]),
            total_tasks=len(summary["tasks"]),
        )

    return summary


def run_group_evolution(
    *,
    task_root: Path,
    groups: list[TerminalBenchTaskGroup],
    run_root: Path,
    baseline_root: Path,
    model: str,
    reflector_model: str,
    reflector_provider: str = "codex_cli",
    reflector_timeout_seconds: float | None = None,
    codex_home: str | None = None,
    terminal_bench_package_root: Path = DEFAULT_TERMINAL_BENCH_PACKAGE_ROOT,
    agent_system_method: str = "auto",
    gepa_candidate_count: int = 1,
    gepa_generations: int = 1,
    rounds: int,
    env_json: dict[str, str],
    verifier_env: dict[str, str],
    command_runner=_default_command_runner,
    worker_runner=_run_worker_once_local,
    evolved_trial_locator=None,
) -> dict[str, Any]:
    validated_groups = [_validate_task_group(group) for group in groups]
    summary = {
        "dry_run": False,
        "mode": "group",
        "task_root": str(task_root),
        "run_root": str(run_root),
        "baseline_root": str(baseline_root),
        "model": model,
        "reflector_model": reflector_model,
        "reflector_provider": reflector_provider,
        "reflector_timeout_seconds": reflector_timeout_seconds,
        "terminal_bench_package_root": str(terminal_bench_package_root),
        "agent_system_method": agent_system_method,
        "gepa_candidate_count": gepa_candidate_count,
        "gepa_generations": gepa_generations,
        "groups": [],
    }

    for group in validated_groups:
        task_ids = list(group.task_ids)
        baseline_trials_by_task = {
            task_id: _find_baseline_trial(baseline_root, task_id) for task_id in task_ids
        }
        baseline_rewards_by_task = {
            task_id: _trial_reward(baseline_trials_by_task[task_id]) for task_id in task_ids
        }
        group_summary = {
            "group_id": group.group_id,
            "task_ids": task_ids,
            "objective": group.objective,
            "baseline_trials": {
                task_id: str(baseline_trials_by_task[task_id]) for task_id in task_ids
            },
            "baseline_rewards": dict(baseline_rewards_by_task),
            "rounds": [],
        }
        group_store_root = run_root / "groups" / group.group_id / "evolution"
        db_path = group_store_root / "evolution.db"
        artifact_root = group_store_root / "artifacts"
        next_round_inputs = tuple(baseline_trials_by_task[task_id] for task_id in task_ids)
        previous_rewards_by_task = dict(baseline_rewards_by_task)
        dataset_history: tuple[str, ...] = ()
        effective_gepa_generations = (
            max(1, gepa_generations) if agent_system_method == "agent_system_gepa_reflector" else 1
        )

        def locate_evolved_trial(
            task_id: str,
            round_number: int,
            search_root: Path,
        ) -> Path:
            if evolved_trial_locator is not None:
                return evolved_trial_locator(task_id, round_number, run_root)
            return _default_group_evolved_trial_locator(
                group.group_id,
                task_id,
                round_number,
                run_root,
                search_root=search_root,
            )

        for round_number in range(1, rounds + 1):
            round_root = run_root / "groups" / group.group_id / f"r{round_number}"
            round_root.mkdir(parents=True, exist_ok=True)
            round_input_trials_by_task = dict(
                zip(task_ids, next_round_inputs, strict=True)
            )
            round_state = gepa_kernel.begin_round(
                generation_inputs=next_round_inputs,
                dataset_history=dataset_history,
            )
            round_generation_summaries: list[dict[str, Any]] = []
            round_candidate_results: list[dict[str, Any]] = []
            round_candidate_evaluations: list[
                gepa_kernel.CandidateEvaluation[Path]
            ] = []

            for generation_number in range(1, effective_gepa_generations + 1):
                generation_inputs = list(round_state.generation_inputs)
                generation_root = (
                    round_root
                    if effective_gepa_generations == 1
                    else round_root / f"g{generation_number}"
                )
                generation_root.mkdir(parents=True, exist_ok=True)
                job_output_path = generation_root / "agent_system_job.json"
                job_command = _create_agent_system_job_command(
                    task_id=group.group_id,
                    round_number=round_number,
                    input_trial_dirs=generation_inputs,
                    previous_dataset_artifact_ids=list(round_state.dataset_history),
                    agent_system_method=agent_system_method,
                    gepa_candidate_count=gepa_candidate_count,
                    gepa_generation=(
                        generation_number if effective_gepa_generations > 1 else None
                    ),
                    db_path=db_path,
                    artifact_root=artifact_root,
                    reflector_model=reflector_model,
                    reflector_provider=reflector_provider,
                    reflector_timeout_seconds=reflector_timeout_seconds,
                    codex_home=codex_home,
                    output_path=job_output_path,
                )
                command_runner(job_command)

                job_payload = json.loads(job_output_path.read_text(encoding="utf-8"))
                if not isinstance(job_payload, dict):
                    raise ValueError(f"expected job payload object JSON in {job_output_path}")

                dataset_payload = job_payload.get("dataset")
                dataset_artifact_id: str | None = None
                if isinstance(dataset_payload, dict):
                    raw_dataset_artifact_id = dataset_payload.get("artifact_id")
                    if (
                        isinstance(raw_dataset_artifact_id, str)
                        and raw_dataset_artifact_id.strip()
                    ):
                        dataset_artifact_id = raw_dataset_artifact_id
                round_state = gepa_kernel.record_dataset(
                    round_state,
                    dataset_artifact_id,
                )

                completed_artifacts = worker_runner(db_path=db_path, artifact_root=artifact_root)
                agent_system_artifacts = discover_agent_system_artifact_paths(
                    completed_artifacts,
                    task_id=group.group_id,
                    round_number=round_number,
                    job_payload=job_payload,
                )
                generation_candidate_results: list[dict[str, Any]] = []
                generation_candidate_evaluations: list[
                    gepa_kernel.CandidateEvaluation[Path]
                ] = []
                for candidate_position, artifact in enumerate(
                    agent_system_artifacts,
                    start=1,
                ):
                    materializer = ArtifactMaterializer()
                    agent_kwargs = materializer.materialize(artifact)
                    candidate_index = gepa_kernel.resolve_candidate_index(
                        candidate_id=artifact.artifact_id,
                        explicit_candidate_index=artifact.candidate_index,
                        fallback_candidate_index=candidate_position,
                    )
                    generation_suffix = (
                        "" if effective_gepa_generations == 1 else f"-g{generation_number}"
                    )
                    candidate_suffix = (
                        "" if len(agent_system_artifacts) == 1 else f"-c{candidate_index}"
                    )
                    task_trials: dict[str, Path] = {}
                    task_rewards: dict[str, float | None] = {}
                    for task_position, task_id in enumerate(task_ids, start=1):
                        candidate_task_job_name = (
                            f"{group.group_id}-r{round_number}"
                            f"{generation_suffix}{candidate_suffix}"
                            f"-t{task_position}-{_safe_path_component(task_id)}"
                        )
                        candidate_task_jobs_dir = (
                            generation_root / "harbor_jobs" / candidate_task_job_name
                        )
                        harbor_command = build_harbor_command(
                            job_name=candidate_task_job_name,
                            task_root=task_root,
                            task_id=task_id,
                            jobs_dir=candidate_task_jobs_dir,
                            model=model,
                            env_json=env_json,
                            agent_kwargs=agent_kwargs,
                            verifier_env=verifier_env,
                            n_concurrent=1,
                            extra_docker_compose=_terminal_bench_extra_docker_compose(
                                terminal_bench_package_root
                            ),
                        )
                        command_runner(harbor_command, cwd=terminal_bench_package_root)
                        evolved_trial = locate_evolved_trial(
                            task_id,
                            round_number,
                            candidate_task_jobs_dir,
                        )
                        task_trials[task_id] = evolved_trial
                        task_rewards[task_id] = _trial_reward(evolved_trial)
                    candidate_result = {
                        "artifact": artifact,
                        "materializer": materializer,
                        "task_trials": task_trials,
                        "task_rewards": task_rewards,
                        "generation": generation_number,
                    }
                    candidate_evaluation = gepa_kernel.group_candidate(
                        candidate_id=artifact.artifact_id,
                        source_index=(
                            len(round_candidate_results) + candidate_position - 1
                        ),
                        explicit_candidate_index=artifact.candidate_index,
                        fallback_candidate_index=0,
                        generation=generation_number,
                        task_rewards=(task_rewards[task_id] for task_id in task_ids),
                        task_trials=(task_trials[task_id] for task_id in task_ids),
                    )
                    candidate_result["score"] = candidate_evaluation.objective
                    generation_candidate_results.append(candidate_result)
                    generation_candidate_evaluations.append(candidate_evaluation)

                round_candidate_results.extend(generation_candidate_results)
                round_candidate_evaluations.extend(generation_candidate_evaluations)
                round_generation_summaries.append(
                    {
                        "generation": generation_number,
                        "input_trials": [str(path) for path in generation_inputs],
                        "dataset_artifact_id": dataset_artifact_id,
                        "candidate_trials": [
                            _group_candidate_trial_summary(
                                result,
                                position,
                                task_ids=task_ids,
                                include_generation=False,
                            )
                            for position, result in enumerate(
                                generation_candidate_results,
                                start=1,
                            )
                        ],
                    }
                )
                round_state = gepa_kernel.advance_generation(
                    round_state,
                    generation_candidate_evaluations,
                )

            round_transition = gepa_kernel.complete_round(
                round_state,
                round_candidate_evaluations,
                empty_error="no group candidate trials were evaluated",
            )
            best_candidate = round_candidate_results[round_transition.winner.source_index]
            artifact = best_candidate["artifact"]
            materializer = best_candidate["materializer"]
            task_trials = best_candidate["task_trials"]
            task_rewards = best_candidate["task_rewards"]
            score = best_candidate["score"]

            round_summary = {
                "round": round_number,
                "input_trials": {
                    task_id: str(round_input_trials_by_task[task_id]) for task_id in task_ids
                },
                "task_trials": {
                    task_id: str(task_trials[task_id]) for task_id in task_ids
                },
                "task_rewards": dict(task_rewards),
                "score": score,
                "transitions": {
                    task_id: summarize_transition(
                        previous_rewards_by_task.get(task_id),
                        task_rewards.get(task_id),
                    )
                    for task_id in task_ids
                },
                "artifact": {
                    "artifact_id": artifact.artifact_id,
                    "artifact_type": artifact.artifact_type,
                    "path": str(artifact.path),
                    "method": artifact.method,
                    "source_dataset_artifact_ids": artifact.source_dataset_artifact_ids,
                },
                "skipped_artifacts": list(materializer.skipped),
                "candidate_trials": [
                    _group_candidate_trial_summary(
                        result,
                        position,
                        task_ids=task_ids,
                        include_generation=effective_gepa_generations > 1,
                    )
                    for position, result in enumerate(round_candidate_results, start=1)
                ],
            }
            if round_transition.round_dataset_ids:
                round_summary["dataset_artifact_id"] = round_transition.round_dataset_ids[0]
                round_summary["dataset_artifact_ids"] = list(
                    round_transition.round_dataset_ids
                )
            if effective_gepa_generations > 1:
                round_summary["gepa_generations"] = len(round_generation_summaries)
                round_summary["generations"] = round_generation_summaries

            group_summary["rounds"].append(round_summary)
            next_round_inputs = round_transition.next_round_inputs
            dataset_history = round_transition.dataset_history
            previous_rewards_by_task = dict(task_rewards)

        summary["groups"].append(group_summary)

    return summary


def discover_agent_system_artifact_path(
    completed_artifacts: list[dict[str, Any]],
    *,
    task_id: str,
    round_number: int,
    job_payload: dict[str, Any],
) -> EvolutionArtifact:
    return discover_agent_system_artifact_paths(
        completed_artifacts,
        task_id=task_id,
        round_number=round_number,
        job_payload=job_payload,
    )[0]


def discover_agent_system_artifact_paths(
    completed_artifacts: list[dict[str, Any]],
    *,
    task_id: str,
    round_number: int,
    job_payload: dict[str, Any],
) -> list[EvolutionArtifact]:
    return discover_evolution_artifact_paths(
        completed_artifacts,
        artifact_type="agent_system",
        task_id=task_id,
        round_number=round_number,
        job_payload=job_payload,
    )


def discover_evolution_artifact_paths(
    completed_artifacts: list[dict[str, Any]],
    *,
    artifact_type: str,
    task_id: str,
    round_number: int,
    job_payload: dict[str, Any],
) -> list[EvolutionArtifact]:
    normalized_artifact_type = _normalize_terminal_bench_artifact_type(artifact_type)
    job = job_payload.get("job")
    if not isinstance(job, dict):
        raise ValueError("job_payload['job'] must be a dict")
    input_artifact_ids = job.get("input_artifact_ids")
    if not isinstance(input_artifact_ids, list):
        raise ValueError("job_payload['job']['input_artifact_ids'] must be a list")
    if any(not isinstance(artifact_id, str) for artifact_id in input_artifact_ids):
        raise ValueError("job_payload['job']['input_artifact_ids'] must contain only strings")

    discovered: list[EvolutionArtifact] = []
    for artifact in completed_artifacts:
        if _normalize_terminal_bench_artifact_type(str(artifact.get("type") or "")) != (
            normalized_artifact_type
        ):
            continue
        uri = artifact.get("uri")
        if not isinstance(uri, str):
            raise ValueError(f"{normalized_artifact_type} artifact has unsupported uri: {uri!r}")
        parsed = urlparse(uri)
        if parsed.scheme != "file":
            raise ValueError(f"{normalized_artifact_type} artifact has unsupported uri: {uri!r}")
        if parsed.netloc not in {"", "localhost"}:
            raise ValueError(
                f"{normalized_artifact_type} artifact has unsupported file URI host: {uri!r}"
            )
        path_text = unquote(parsed.path)
        if not path_text:
            raise ValueError(
                f"{normalized_artifact_type} artifact has empty path in uri: {uri!r}"
            )
        manifest = artifact.get("manifest")
        if not isinstance(manifest, dict):
            manifest = {}
        artifact_id = artifact.get("artifact_id")
        if not isinstance(artifact_id, str) or not artifact_id.strip():
            raise ValueError(
                f"completed {normalized_artifact_type} artifact missing required artifact_id"
            )
        discovered.append(
            EvolutionArtifact(
                artifact_type=normalized_artifact_type,
                artifact_id=artifact_id,
                path=Path(path_text),
                task_id=task_id,
                round=round_number,
                method=str(
                    manifest.get("method")
                    or _default_terminal_bench_artifact_method(normalized_artifact_type)
                ),
                source_dataset_artifact_ids=list(input_artifact_ids),
                candidate_index=_manifest_candidate_index(manifest),
                candidate_strategy=_manifest_candidate_strategy(manifest),
            )
        )
    if discovered:
        return discovered
    raise ValueError(f"completed job did not produce a {normalized_artifact_type} artifact")


def _default_terminal_bench_artifact_method(artifact_type: str) -> str:
    if artifact_type == "text_memory":
        return "text_memory_expel_reflector"
    return "agent_system_reflector"


def _select_best_attempt(attempts: list[dict[str, Any]]) -> dict[str, Any]:
    if not attempts:
        raise ValueError("no Terminal Bench attempts were evaluated")
    return max(
        attempts,
        key=lambda attempt: (
            float("-inf") if attempt["reward"] is None else float(attempt["reward"]),
            -int(attempt["attempt_index"]),
        ),
    )


def _candidate_trial_summary(
    result: dict[str, Any],
    position: int,
    *,
    include_generation: bool = False,
) -> dict[str, Any]:
    artifact = result["artifact"]
    summary = {
        "candidate_index": gepa_kernel.resolve_candidate_index(
            candidate_id=artifact.artifact_id,
            explicit_candidate_index=artifact.candidate_index,
            fallback_candidate_index=position,
        ),
        "artifact_id": artifact.artifact_id,
        "strategy": _artifact_candidate_strategy(artifact),
        "reward": result["reward"],
        "trial_dir": str(result["trial_dir"]),
    }
    if include_generation:
        summary["generation"] = int(result.get("generation", 0))
    if len(result.get("attempts") or []) > 1:
        summary["attempt_count"] = len(result["attempts"])
        summary["attempts"] = [
            {
                "attempt_index": attempt["attempt_index"],
                "reward": attempt["reward"],
                "trial_dir": str(attempt["trial_dir"]),
            }
            for attempt in result["attempts"]
        ]
        summary["pass_at_k"] = bool(result.get("pass_at_k", False))
    return summary


def _group_candidate_trial_summary(
    result: dict[str, Any],
    position: int,
    *,
    task_ids: list[str],
    include_generation: bool = False,
) -> dict[str, Any]:
    artifact = result["artifact"]
    task_rewards = result["task_rewards"]
    task_trials = result["task_trials"]
    summary = {
        "candidate_index": gepa_kernel.resolve_candidate_index(
            candidate_id=artifact.artifact_id,
            explicit_candidate_index=artifact.candidate_index,
            fallback_candidate_index=position,
        ),
        "artifact_id": artifact.artifact_id,
        "strategy": _artifact_candidate_strategy(artifact),
        "score": result["score"],
        "task_rewards": {
            task_id: task_rewards[task_id] for task_id in task_ids
        },
        "task_trials": {
            task_id: str(task_trials[task_id]) for task_id in task_ids
        },
    }
    if include_generation:
        summary["generation"] = int(result.get("generation", 0))
    return summary


def _artifact_candidate_strategy(artifact: EvolutionArtifact) -> str | None:
    return artifact.candidate_strategy


def _manifest_candidate_index(manifest: dict[str, Any]) -> int | None:
    value = manifest.get("candidate_index")
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _manifest_candidate_strategy(manifest: dict[str, Any]) -> str | None:
    value = manifest.get("candidate_strategy")
    return value.strip() if isinstance(value, str) and value.strip() else None


def _validate_task_group(group: TerminalBenchTaskGroup) -> TerminalBenchTaskGroup:
    group_id = group.group_id.strip()
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", group_id):
        raise ValueError(
            "Terminal Bench group_id must start with an alphanumeric character "
            "and contain only alphanumeric characters, '.', '_' or '-'"
        )
    task_ids = [task_id.strip() for task_id in group.task_ids if task_id.strip()]
    if len(task_ids) < 2:
        raise ValueError(
            f"Terminal Bench group {group_id!r} requires at least two task_id values"
        )
    if len(set(task_ids)) != len(task_ids):
        raise ValueError(f"Terminal Bench group {group_id!r} contains duplicate task_id values")
    if group.objective != "macro_mean_reward":
        raise ValueError(f"unsupported Terminal Bench group objective: {group.objective!r}")
    return TerminalBenchTaskGroup(
        group_id=group_id,
        task_ids=task_ids,
        objective=group.objective,
    )


def _safe_path_component(value: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "-", value.strip()).strip(".-_")
    if not cleaned:
        raise ValueError(f"cannot derive a safe path component from {value!r}")
    return cleaned


def summarize_transition(before: float | None, after: float | None) -> str:
    before_passed = (before or 0.0) >= 1.0
    after_passed = (after or 0.0) >= 1.0
    if before_passed and after_passed:
        return "pass_to_pass"
    if before_passed and not after_passed:
        return "pass_to_fail"
    if not before_passed and after_passed:
        return "fail_to_pass"
    return "fail_to_fail"


def build_memory_benchmark_summary(
    *,
    model: str,
    auth_mode: str,
    memory_backend: str,
    baseline_pass_at_1: dict[str, int],
    baseline_pass_at_5: dict[str, int] | None,
    attempts_by_task: dict[str, list[dict[str, Any]]],
    total_tasks: int,
) -> dict[str, Any]:
    pass_at_1 = 0
    pass_at_5 = 0
    transitions: dict[str, dict[str, Any]] = {}
    for task_id, attempts in attempts_by_task.items():
        first_attempt = attempts[0] if attempts else {}
        first_reward = first_attempt.get("reward")
        baseline_reward = first_attempt.get("baseline_reward")
        best_reward = _best_attempt_reward(attempts[:5])
        if _reward_passed(first_reward):
            pass_at_1 += 1
        if any(_reward_passed(attempt.get("reward")) for attempt in attempts[:5]):
            pass_at_5 += 1
        transitions[task_id] = {
            "transition": summarize_transition(
                float(baseline_reward) if isinstance(baseline_reward, int | float) else None,
                best_reward,
            ),
            "artifact_ids": sorted(
                {
                    str(attempt["artifact_id"])
                    for attempt in attempts
                    if isinstance(attempt.get("artifact_id"), str)
                }
            ),
            "attempt_count": len(attempts),
            "rewards": [attempt.get("reward") for attempt in attempts],
        }
    return {
        "benchmark": "terminal-bench-2.1",
        "model": model,
        "auth_mode": auth_mode,
        "memory_backend": memory_backend,
        "enabled_artifacts": ["text_memory"],
        "disabled_artifacts": ["skill_bundle", "agent_system", "parametric_memory"],
        "baseline": {
            "pass_at_1": dict(baseline_pass_at_1),
            "pass_at_5": dict(baseline_pass_at_5) if baseline_pass_at_5 is not None else None,
        },
        "evolved": {
            "pass_at_1": {"passed": pass_at_1, "total": total_tasks},
            "pass_at_5": {"passed": pass_at_5, "total": total_tasks},
        },
        "task_transitions": transitions,
    }


def _baseline_pass_at_1_from_tasks(tasks: list[dict[str, Any]]) -> dict[str, int]:
    return {
        "passed": sum(1 for task in tasks if _reward_passed(task.get("baseline_reward"))),
        "total": len(tasks),
    }


def _memory_attempts_by_task(tasks: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    attempts_by_task: dict[str, list[dict[str, Any]]] = {}
    for task in tasks:
        task_id = str(task.get("task_id") or "")
        if not task_id:
            continue
        rounds = task.get("rounds")
        if not isinstance(rounds, list) or not rounds:
            attempts_by_task[task_id] = []
            continue
        latest_round = rounds[-1]
        baseline_reward = task.get("baseline_reward")
        artifact = latest_round.get("artifact") if isinstance(latest_round, dict) else {}
        raw_attempts = latest_round.get("attempts") if isinstance(latest_round, dict) else None
        if isinstance(raw_attempts, list) and raw_attempts:
            attempts_by_task[task_id] = [
                {
                    **attempt,
                    "baseline_reward": baseline_reward,
                    "artifact_id": artifact.get("artifact_id")
                    if isinstance(artifact, dict)
                    else None,
                    "artifact_path": artifact.get("path") if isinstance(artifact, dict) else None,
                }
                for attempt in raw_attempts
                if isinstance(attempt, dict)
            ]
        else:
            attempts_by_task[task_id] = [
                {
                    "attempt_index": 1,
                    "reward": latest_round.get("reward"),
                    "trial_dir": latest_round.get("trial_dir"),
                    "baseline_reward": baseline_reward,
                    "artifact_id": artifact.get("artifact_id")
                    if isinstance(artifact, dict)
                    else None,
                    "artifact_path": artifact.get("path") if isinstance(artifact, dict) else None,
                }
            ]
    return attempts_by_task


def _reward_passed(value: object) -> bool:
    return isinstance(value, int | float) and float(value) >= 1.0


def _best_attempt_reward(attempts: list[dict[str, Any]]) -> float | None:
    rewards = [
        float(reward)
        for attempt in attempts
        if isinstance((reward := attempt.get("reward")), int | float)
    ]
    return max(rewards) if rewards else None
