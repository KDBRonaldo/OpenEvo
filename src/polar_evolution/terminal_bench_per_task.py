from __future__ import annotations

from dataclasses import dataclass, field
import json
from pathlib import Path
import re
import subprocess
from typing import Any
from urllib.parse import unquote, urlparse

from polar_evolution.methods import run_method
from polar_evolution.models import (
    WorkerClaimRequest,
    WorkerCompleteRequest,
    WorkerFailRequest,
    WorkerHeartbeatRequest,
)
from polar_evolution.store import EvolutionStore
from polar_evolution.terminal_bench_bridge import build_terminal_bench_events

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
        if artifact.artifact_type in {
            "skill_bundle",
            "text_memory",
            "parametric_memory",
            "memory",
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
            "1",
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


def _terminal_bench_extra_docker_compose(package_root: Path) -> list[Path]:
    harbor_root = package_root / "task_packages" / "terminal_bench_v1" / "harbor"
    return [
        harbor_root / "pull-never.yaml",
        harbor_root / "docker-cp-host-network.yaml",
    ]


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
        "polar-evolution",
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
    gepa_candidate_count: int,
    gepa_generations: int,
    rounds: int,
    artifact_types: list[str],
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
        "gepa_candidate_count": gepa_candidate_count,
        "gepa_generations": gepa_generations,
        "tasks": [
            {"task_id": task_id, "rounds": rounds, "artifact_types": artifact_types}
            for task_id in task_ids
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
    gepa_candidate_count: int = 1,
    gepa_generations: int = 1,
    rounds: int,
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
        "gepa_candidate_count": gepa_candidate_count,
        "gepa_generations": gepa_generations,
        "tasks": [],
    }

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
        previous_dataset_artifact_ids: list[str] = []
        effective_gepa_generations = (
            max(1, gepa_generations) if agent_system_method == "agent_system_gepa_reflector" else 1
        )

        for round_number in range(1, rounds + 1):
            round_root = run_root / "tasks" / task_id / f"r{round_number}"
            round_root.mkdir(parents=True, exist_ok=True)
            generation_input_trials = [input_trial_dir]
            round_dataset_artifact_ids: list[str] = []
            round_generation_summaries: list[dict[str, Any]] = []
            round_candidate_results: list[dict[str, Any]] = []
            round_history_dataset_ids = list(previous_dataset_artifact_ids)

            for generation_number in range(1, effective_gepa_generations + 1):
                generation_root = (
                    round_root
                    if effective_gepa_generations == 1
                    else round_root / f"g{generation_number}"
                )
                generation_root.mkdir(parents=True, exist_ok=True)
                job_output_path = generation_root / "agent_system_job.json"
                job_command = _create_agent_system_job_command(
                    task_id=task_id,
                    round_number=round_number,
                    input_trial_dirs=generation_input_trials,
                    previous_dataset_artifact_ids=round_history_dataset_ids,
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
                        round_history_dataset_ids.append(dataset_artifact_id)
                        round_dataset_artifact_ids.append(dataset_artifact_id)

                completed_artifacts = worker_runner(db_path=db_path, artifact_root=artifact_root)
                agent_system_artifacts = discover_agent_system_artifact_paths(
                    completed_artifacts,
                    task_id=task_id,
                    round_number=round_number,
                    job_payload=job_payload,
                )
                generation_candidate_results: list[dict[str, Any]] = []
                for candidate_position, artifact in enumerate(
                    agent_system_artifacts,
                    start=1,
                ):
                    materializer = ArtifactMaterializer()
                    agent_kwargs = materializer.materialize(artifact)
                    candidate_index = _artifact_candidate_index(
                        artifact,
                        candidate_position,
                    )
                    generation_suffix = (
                        "" if effective_gepa_generations == 1 else f"-g{generation_number}"
                    )
                    candidate_suffix = (
                        "" if len(agent_system_artifacts) == 1 else f"-c{candidate_index}"
                    )
                    harbor_command = build_harbor_command(
                        job_name=(
                            f"{task_id}-r{round_number}{generation_suffix}{candidate_suffix}"
                        ),
                        task_root=task_root,
                        task_id=task_id,
                        jobs_dir=generation_root / "harbor_jobs",
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
                    evolved_trial = evolved_trial_locator(task_id, round_number, run_root)
                    reward = _trial_reward(evolved_trial)
                    generation_candidate_results.append(
                        {
                            "artifact": artifact,
                            "materializer": materializer,
                            "trial_dir": evolved_trial,
                            "reward": reward,
                            "generation": generation_number,
                        }
                    )

                round_candidate_results.extend(generation_candidate_results)
                round_generation_summaries.append(
                    {
                        "generation": generation_number,
                        "input_trials": [str(path) for path in generation_input_trials],
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
                generation_input_trials = [
                    result["trial_dir"] for result in generation_candidate_results
                ]

            best_candidate = _select_best_candidate_result(round_candidate_results)
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
            if len(round_candidate_results) > 1:
                round_summary["candidate_trials"] = [
                    _candidate_trial_summary(
                        result,
                        position,
                        include_generation=effective_gepa_generations > 1,
                    )
                    for position, result in enumerate(round_candidate_results, start=1)
                ]
            if round_dataset_artifact_ids:
                round_summary["dataset_artifact_id"] = round_dataset_artifact_ids[0]
                round_summary["dataset_artifact_ids"] = list(round_dataset_artifact_ids)
                previous_dataset_artifact_ids = list(round_history_dataset_ids)
            if effective_gepa_generations > 1:
                round_summary["gepa_generations"] = len(round_generation_summaries)
                round_summary["generations"] = round_generation_summaries

            task_summary["rounds"].append(round_summary)
            input_trial_dir = evolved_trial
            previous_reward = reward

        summary["tasks"].append(task_summary)

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
        if artifact.get("type") != "agent_system":
            continue
        uri = artifact.get("uri")
        if not isinstance(uri, str):
            raise ValueError(f"agent_system artifact has unsupported uri: {uri!r}")
        parsed = urlparse(uri)
        if parsed.scheme != "file":
            raise ValueError(f"agent_system artifact has unsupported uri: {uri!r}")
        if parsed.netloc not in {"", "localhost"}:
            raise ValueError(f"agent_system artifact has unsupported file URI host: {uri!r}")
        path_text = unquote(parsed.path)
        if not path_text:
            raise ValueError(f"agent_system artifact has empty path in uri: {uri!r}")
        manifest = artifact.get("manifest")
        if not isinstance(manifest, dict):
            manifest = {}
        artifact_id = artifact.get("artifact_id")
        if not isinstance(artifact_id, str) or not artifact_id.strip():
            raise ValueError("completed agent_system artifact missing required artifact_id")
        discovered.append(
            EvolutionArtifact(
                artifact_type="agent_system",
                artifact_id=artifact_id,
                path=Path(path_text),
                task_id=task_id,
                round=round_number,
                method=str(manifest.get("method") or "agent_system_reflector"),
                source_dataset_artifact_ids=list(input_artifact_ids),
                candidate_index=_manifest_candidate_index(manifest),
                candidate_strategy=_manifest_candidate_strategy(manifest),
            )
        )
    if discovered:
        return discovered
    raise ValueError("completed job did not produce an agent_system artifact")


def _select_best_candidate_result(candidate_results: list[dict[str, Any]]) -> dict[str, Any]:
    if not candidate_results:
        raise ValueError("no candidate trials were evaluated")
    return max(
        candidate_results,
        key=lambda result: (
            float("-inf") if result["reward"] is None else float(result["reward"]),
            int(result.get("generation", 0)),
            -_artifact_candidate_index(result["artifact"], 0),
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
        "candidate_index": _artifact_candidate_index(artifact, position),
        "artifact_id": artifact.artifact_id,
        "strategy": _artifact_candidate_strategy(artifact),
        "reward": result["reward"],
        "trial_dir": str(result["trial_dir"]),
    }
    if include_generation:
        summary["generation"] = int(result.get("generation", 0))
    return summary


def _artifact_candidate_index(artifact: EvolutionArtifact, fallback: int) -> int:
    if artifact.candidate_index is not None:
        return artifact.candidate_index
    match = re.search(r"(?:^|[-_])c?(\d+)(?:$|[-_])", artifact.artifact_id)
    if match:
        return int(match.group(1))
    return fallback


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
