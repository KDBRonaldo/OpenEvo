from __future__ import annotations

import json
import math
import re
import time
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any
from uuid import uuid4

from polar_evolution.worker import EvolutionWorkerClient, run_once

from openevo.experiment.clients import EvolutionClientProtocol, EvolutionHttpClient
from openevo.experiment.clients import RolloutClientProtocol, RolloutHttpClient
from openevo.experiment.compiler import CompiledExperiment, compile_experiment
from openevo.experiment.models import ExperimentConfig

WorkerRunner = Callable[..., list[dict[str, Any]]]


def dry_run_experiment(
    config: ExperimentConfig,
    *,
    task_ids: Sequence[str] | None = None,
    rounds_override: int | None = None,
) -> dict[str, Any]:
    compiled = compile_experiment(
        config,
        task_ids=task_ids,
        rounds_override=rounds_override,
    )
    return compiled_experiment_plan(compiled)


def compiled_experiment_plan(compiled: CompiledExperiment) -> dict[str, Any]:
    tasks: list[dict[str, Any]] = []
    for task in compiled.tasks:
        rounds: list[dict[str, Any]] = []
        history_context_artifact_ids = _empty_context_artifact_ids()
        rollout_context_artifact_ids = _empty_context_artifact_ids()
        for round_index in range(compiled.round_count):
            dataset_placeholder = f"<dataset_artifact:{task.task_id}:round-{round_index}>"
            method_specs = compiled.evolution_methods_for_round(round_index)
            rounds.append(
                {
                    "round_index": round_index,
                    "policy_version": task.policy_version_for_round(round_index),
                    "rollout_payload": task.rollout_payload_for_round(
                        round_index,
                        context_artifact_ids=rollout_context_artifact_ids,
                    ),
                    "dataset_payload": task.dataset_payload_for_round(round_index),
                    "evolution_jobs": task.evolution_job_payloads_for_round(
                        round_index,
                        method_specs,
                        dataset_artifact_id=dataset_placeholder,
                        context_artifact_ids=history_context_artifact_ids,
                    ),
                }
            )
            next_rollout_context_artifact_ids = _empty_context_artifact_ids()
            for spec in method_specs:
                artifact_placeholder = (
                    f"<{spec.artifact_type}_artifact:{task.task_id}:round-{round_index}>"
                )
                history_context_artifact_ids[spec.artifact_type].append(
                    artifact_placeholder
                )
                next_rollout_context_artifact_ids[spec.artifact_type] = [
                    artifact_placeholder
                ]
            history_context_artifact_ids["dataset"].append(dataset_placeholder)
            rollout_context_artifact_ids = next_rollout_context_artifact_ids
        tasks.append({"task_id": task.task_id, "rounds": rounds})
    return {
        "mode": "dry_run",
        "experiment_id": compiled.experiment_id,
        "experiment_name": compiled.experiment_name,
        "run_id": compiled.run_id,
        "round_count": compiled.round_count,
        "rollout_url": compiled.rollout_url,
        "evolution_backend_url": compiled.evolution_backend_url,
        "tasks": tasks,
    }


def _empty_context_artifact_ids() -> dict[str, list[str]]:
    return {
        "dataset": [],
        "text_memory": [],
        "skill_bundle": [],
        "agent_system": [],
    }


def run_experiment(
    config: ExperimentConfig,
    *,
    task_ids: Sequence[str] | None = None,
    rounds_override: int | None = None,
    output_dir: Path | None = None,
    rollout_client: RolloutClientProtocol | None = None,
    evolution_client: EvolutionClientProtocol | None = None,
    worker_runner: WorkerRunner | None = None,
    poll_interval_seconds: float = 2.0,
    max_poll_attempts: int = 1800,
) -> dict[str, Any]:
    _validate_polling_options(
        poll_interval_seconds=poll_interval_seconds,
        max_poll_attempts=max_poll_attempts,
    )
    run_id = uuid4().hex
    compiled = compile_experiment(
        config,
        task_ids=task_ids,
        rounds_override=rounds_override,
        run_id=run_id,
    )
    output_root = (
        output_dir
        if output_dir is not None
        else Path(".openevo") / "runs" / _safe_path_component(compiled.experiment_id) / run_id
    )
    artifact_root = output_root / "artifacts"
    output_root.mkdir(parents=True, exist_ok=True)

    owns_rollout = rollout_client is None
    owns_evolution = evolution_client is None
    rollout = rollout_client or RolloutHttpClient(compiled.rollout_url)
    evolution = evolution_client or EvolutionHttpClient(compiled.evolution_backend_url)
    try:
        result = _run_compiled_experiment(
            compiled,
            rollout_client=rollout,
            evolution_client=evolution,
            worker_runner=worker_runner,
            artifact_root=artifact_root,
            poll_interval_seconds=poll_interval_seconds,
            max_poll_attempts=max_poll_attempts,
        )
    finally:
        if owns_rollout and hasattr(rollout, "close"):
            rollout.close()  # type: ignore[attr-defined]
        if owns_evolution and hasattr(evolution, "close"):
            evolution.close()  # type: ignore[attr-defined]

    summary_path = output_root / "summary.json"
    result["summary_path"] = str(summary_path)
    summary_path.write_text(
        json.dumps(result, indent=2, sort_keys=True, allow_nan=False) + "\n",
        encoding="utf-8",
    )
    return result


def _run_compiled_experiment(
    compiled: CompiledExperiment,
    *,
    rollout_client: RolloutClientProtocol,
    evolution_client: EvolutionClientProtocol,
    worker_runner: WorkerRunner | None,
    artifact_root: Path,
    poll_interval_seconds: float,
    max_poll_attempts: int,
) -> dict[str, Any]:
    task_results: list[dict[str, Any]] = []
    any_failure = False
    run_id = compiled.run_id or uuid4().hex
    for task in compiled.tasks:
        history_context_artifact_ids: dict[str, list[str]] = {
            "dataset": [],
            "text_memory": [],
            "skill_bundle": [],
            "agent_system": [],
        }
        rollout_context_artifact_ids: dict[str, list[str]] = {
            "dataset": [],
            "text_memory": [],
            "skill_bundle": [],
            "agent_system": [],
        }
        round_results: list[dict[str, Any]] = []
        for round_index in range(compiled.round_count):
            rollout_payload = task.rollout_payload_for_round(
                round_index,
                context_artifact_ids=rollout_context_artifact_ids,
            )
            submitted_task_id = rollout_client.submit_task(rollout_payload)
            rollout_result = _poll_rollout_task(
                rollout_client,
                submitted_task_id,
                poll_interval_seconds=poll_interval_seconds,
                max_poll_attempts=max_poll_attempts,
            )
            round_result: dict[str, Any] = {
                "round_index": round_index,
                "policy_version": task.policy_version_for_round(round_index),
                "rollout_task_id": submitted_task_id,
                "rollout_status": rollout_result.get("status"),
                "dataset": None,
                "dataset_status": None,
                "jobs": [],
                "artifact_ids": _snapshot_context_artifact_ids(
                    history_context_artifact_ids
                ),
            }
            if rollout_result.get("status") != "completed":
                any_failure = True
                round_results.append(round_result)
                break

            dataset_payload = task.dataset_payload_for_round(round_index)
            dataset = evolution_client.create_dataset(dataset_payload)
            round_result["dataset"] = dataset
            dataset_artifact_id = _required_text(dataset, "artifact_id", "dataset response")
            if not _dataset_has_trajectories(dataset):
                any_failure = True
                round_result["dataset_status"] = "empty"
                round_results.append(round_result)
                break
            round_result["dataset_status"] = "ready"
            prior_context_artifact_ids = {
                key: list(value) for key, value in history_context_artifact_ids.items()
            }
            next_rollout_context_artifact_ids: dict[str, list[str]] = {
                "dataset": [],
                "text_memory": [],
                "skill_bundle": [],
                "agent_system": [],
            }
            round_failed = False
            for spec in compiled.evolution_methods_for_round(round_index):
                job_payload = task.evolution_job_payloads_for_round(
                    round_index,
                    [spec],
                    dataset_artifact_id=dataset_artifact_id,
                    context_artifact_ids=prior_context_artifact_ids,
                )[0]
                claim_capability = _claim_capability_for_job(
                    run_id=run_id,
                    task_id=task.task_id,
                    round_index=round_index,
                    method=spec.method,
                )
                job_payload["job_type"] = claim_capability
                created_job = evolution_client.create_job(job_payload)
                created_job_id = _required_text(
                    created_job,
                    "job_id",
                    "evolution job response",
                )
                worker_results = _run_worker_for_job(
                    compiled,
                    worker_runner=worker_runner,
                    artifact_root=artifact_root,
                    capability=claim_capability,
                    expected_job_id=created_job_id,
                )
                artifact_ids, unexpected_job_ids = _artifact_ids_from_worker_results(
                    worker_results,
                    expected_job_id=created_job_id,
                )
                worker_error = _worker_error_from_results(
                    worker_results,
                    expected_job_id=created_job_id,
                )
                if unexpected_job_ids or worker_error or not artifact_ids:
                    any_failure = True
                    round_failed = True
                worker_status = (
                    "unexpected_job"
                    if unexpected_job_ids
                    else "failed"
                    if worker_error
                    else "succeeded"
                    if artifact_ids
                    else "missing_artifacts"
                )
                if worker_status == "succeeded":
                    history_context_artifact_ids.setdefault(spec.artifact_type, []).extend(
                        artifact_ids
                    )
                    next_rollout_context_artifact_ids[spec.artifact_type] = list(
                        artifact_ids
                    )
                round_result["jobs"].append(
                    {
                        "artifact_type": spec.artifact_type,
                        "method": spec.method,
                        "job": created_job,
                        "worker_results": worker_results,
                        "worker_status": worker_status,
                        "worker_error": worker_error,
                        "unexpected_job_ids": unexpected_job_ids,
                        "artifact_ids": artifact_ids,
                    }
                )
                if round_failed:
                    break
            history_context_artifact_ids["dataset"].append(dataset_artifact_id)
            round_result["artifact_ids"] = _snapshot_context_artifact_ids(
                history_context_artifact_ids
            )
            round_results.append(round_result)
            if round_failed:
                break
            rollout_context_artifact_ids = next_rollout_context_artifact_ids
        task_results.append({"task_id": task.task_id, "rounds": round_results})
    return {
        "mode": "run",
        "status": "failed" if any_failure else "completed",
        "experiment_id": compiled.experiment_id,
        "experiment_name": compiled.experiment_name,
        "run_id": compiled.run_id,
        "round_count": compiled.round_count,
        "tasks": task_results,
    }


def _poll_rollout_task(
    client: RolloutClientProtocol,
    task_id: str,
    *,
    poll_interval_seconds: float,
    max_poll_attempts: int,
) -> dict[str, Any]:
    interval = poll_interval_seconds
    attempts = max_poll_attempts
    for attempt in range(attempts):
        if attempt > 0 and interval:
            time.sleep(interval)
        result = client.get_task(task_id)
        if str(result.get("status")) in {"completed", "failed"}:
            return result
    raise TimeoutError(f"rollout task {task_id} did not finish after {attempts} polls")


def _validate_polling_options(
    *,
    poll_interval_seconds: float,
    max_poll_attempts: int,
) -> None:
    if isinstance(poll_interval_seconds, bool) or not isinstance(
        poll_interval_seconds,
        int | float,
    ):
        raise ValueError("poll_interval_seconds must be a number")
    if not math.isfinite(poll_interval_seconds) or poll_interval_seconds < 0:
        raise ValueError("poll_interval_seconds must be non-negative")
    if isinstance(max_poll_attempts, bool) or not isinstance(max_poll_attempts, int):
        raise ValueError("max_poll_attempts must be an integer")
    if max_poll_attempts < 1:
        raise ValueError("max_poll_attempts must be at least 1")


def _snapshot_context_artifact_ids(
    context_artifact_ids: dict[str, list[str]],
) -> dict[str, list[str]]:
    return {
        artifact_type: list(artifact_ids)
        for artifact_type, artifact_ids in context_artifact_ids.items()
    }


def _safe_path_component(value: str) -> str:
    slug = re.sub(r"[^A-Za-z0-9_.-]+", "-", value.strip()).strip(".-")
    return slug or "experiment"


def _claim_capability_for_job(
    *,
    run_id: str,
    task_id: str,
    round_index: int,
    method: str,
) -> str:
    return f"openevo:{run_id}:{task_id}:round-{round_index}:{method}"


def _run_worker_for_job(
    compiled: CompiledExperiment,
    *,
    worker_runner: WorkerRunner | None,
    artifact_root: Path,
    capability: str,
    expected_job_id: str,
) -> list[dict[str, Any]]:
    if worker_runner is not None:
        return worker_runner(
            base_url=compiled.evolution_backend_url,
            artifact_root=artifact_root,
            capabilities=[capability],
            expected_job_id=expected_job_id,
            expected_jobs=1,
        )
    return _run_local_worker_once(
        base_url=compiled.evolution_backend_url,
        artifact_root=artifact_root,
        capabilities=[capability],
    )


def _run_local_worker_once(
    *,
    base_url: str,
    artifact_root: Path,
    capabilities: list[str],
) -> list[dict[str, Any]]:
    with _RecordingEvolutionWorkerClient(base_url) as client:
        claimed = run_once(
            client,
            worker_id="openevo-local-worker",
            capabilities=capabilities,
            artifact_root=artifact_root,
            lease_seconds=600,
        )
        if not claimed:
            return [{"claimed": False, "artifact_ids": []}]
        return (
            client.completed_responses
            or client.failed_responses
            or [{"claimed": True, "artifact_ids": []}]
        )


class _RecordingEvolutionWorkerClient(EvolutionWorkerClient):
    def __init__(self, base_url: str) -> None:
        super().__init__(base_url)
        self.completed_responses: list[dict[str, Any]] = []
        self.failed_responses: list[dict[str, Any]] = []

    def complete(
        self,
        job_id: str,
        lease_id: str,
        artifacts: list[dict[str, Any]],
        *,
        report: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        response = super().complete(job_id, lease_id, artifacts, report=report)
        self.completed_responses.append(response)
        return response

    def fail(
        self,
        job_id: str,
        lease_id: str,
        error: str,
        *,
        retryable: bool = True,
    ) -> dict[str, Any]:
        response = super().fail(job_id, lease_id, error, retryable=retryable)
        self.failed_responses.append(response)
        return response


def _artifact_ids_from_worker_results(
    results: list[dict[str, Any]],
    *,
    expected_job_id: str,
) -> tuple[list[str], list[str]]:
    artifact_ids: list[str] = []
    unexpected_job_ids: list[str] = []
    for result in results:
        values = result.get("artifact_ids")
        if not isinstance(values, list):
            continue
        result_artifact_ids = [item for item in values if isinstance(item, str) and item]
        if not result_artifact_ids:
            continue
        job_id = result.get("job_id")
        if job_id != expected_job_id:
            unexpected_job_ids.append(job_id if isinstance(job_id, str) else "<missing>")
            continue
        artifact_ids.extend(result_artifact_ids)
    return artifact_ids, unexpected_job_ids


def _worker_error_from_results(
    results: list[dict[str, Any]],
    *,
    expected_job_id: str,
) -> str | None:
    for result in results:
        if result.get("job_id") != expected_job_id:
            continue
        error = result.get("error")
        if isinstance(error, str) and error.strip():
            return error.strip()
    return None


def _required_text(payload: dict[str, Any], key: str, source: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value:
        raise ValueError(f"{source} did not include {key}")
    return value


def _dataset_has_trajectories(dataset: dict[str, Any]) -> bool:
    return _positive_int(dataset.get("event_count")) and _positive_int(
        dataset.get("trace_count")
    )


def _positive_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value > 0
