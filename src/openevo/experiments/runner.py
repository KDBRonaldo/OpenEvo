from __future__ import annotations

import json
import math
import re
import time
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any
from uuid import uuid4

from openevo.evolution.framework import EvolutionExecutionProfile, RegistrySnapshot
from openevo.evolution.framework.builtins import VerifiedExecutableRegistry
from openevo.evolution.worker import EvolutionWorkerClient, run_once

from .clients import EvolutionClientProtocol, EvolutionHttpClient
from .clients import RolloutClientProtocol, RolloutHttpClient
from .compiler import CompiledEvolutionMethodSpec, CompiledExperiment, compile_experiment
from .models import ExperimentConfig
from .promotion import (
    PromotionReviewer,
    artifact_hashes_from_review_packet,
    evaluate_promotion_gate,
    review_request_payload_from_packet,
    sanitize_review_text,
)

WorkerRunner = Callable[..., list[dict[str, Any]]]


def dry_run_experiment(
    config: ExperimentConfig,
    *,
    task_ids: Sequence[str] | None = None,
    rounds_override: int | None = None,
    registry_snapshot: RegistrySnapshot,
    execution_profile: EvolutionExecutionProfile,
) -> dict[str, Any]:
    compiled = compile_experiment(
        config,
        task_ids=task_ids,
        rounds_override=rounds_override,
        registry_snapshot=registry_snapshot,
        execution_profile=execution_profile,
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
            method_specs = compiled.evolution_methods_for_round(
                round_index,
                prior_dataset_artifact_ids=history_context_artifact_ids["dataset"],
                task_id=task.task_id,
            )
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
                history_context_artifact_ids.setdefault(spec.artifact_type, []).append(
                    artifact_placeholder
                )
                next_rollout_context_artifact_ids[spec.artifact_type] = [artifact_placeholder]
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
        "parametric_memory": [],
        "skill_bundle": [],
        "agent_system": [],
    }


def run_experiment(
    config: ExperimentConfig,
    *,
    run_id: str | None = None,
    task_ids: Sequence[str] | None = None,
    rounds_override: int | None = None,
    initial_context_artifact_ids: Mapping[str, Sequence[str]] | None = None,
    core_authoritative_successor: bool = False,
    managed_worker: bool = False,
    output_dir: Path | None = None,
    artifact_root: Path | None = None,
    rollout_client: RolloutClientProtocol | None = None,
    evolution_client: EvolutionClientProtocol | None = None,
    worker_runner: WorkerRunner | None = None,
    promotion_reviewer: PromotionReviewer | None = None,
    poll_interval_seconds: float = 2.0,
    max_poll_attempts: int = 1800,
    executable_registry: VerifiedExecutableRegistry,
    execution_profile: EvolutionExecutionProfile,
) -> dict[str, Any]:
    _validate_polling_options(
        poll_interval_seconds=poll_interval_seconds,
        max_poll_attempts=max_poll_attempts,
    )
    run_id = run_id if run_id is not None else uuid4().hex
    compiled = compile_experiment(
        config,
        task_ids=task_ids,
        rounds_override=rounds_override,
        run_id=run_id,
        registry_snapshot=executable_registry.snapshot,
        execution_profile=execution_profile,
    )
    output_root = (
        output_dir
        if output_dir is not None
        else Path(".openevo") / "runs" / _safe_path_component(compiled.experiment_id) / run_id
    )
    artifact_root = artifact_root if artifact_root is not None else output_root / "artifacts"
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
            output_root=output_root,
            promotion_reviewer=promotion_reviewer,
            poll_interval_seconds=poll_interval_seconds,
            max_poll_attempts=max_poll_attempts,
            executable_registry=executable_registry,
            initial_context_artifact_ids=initial_context_artifact_ids,
            core_authoritative_successor=core_authoritative_successor,
            managed_worker=managed_worker,
        )
    finally:
        if owns_rollout and hasattr(rollout, "close"):
            rollout.close()  # type: ignore[attr-defined]
        if owns_evolution and hasattr(evolution, "close"):
            evolution.close()  # type: ignore[attr-defined]

    summary_path = output_root / "summary.json"
    result["summary_path"] = str(summary_path)
    public_result = _strip_internal_fields(result)
    summary_path.write_text(
        json.dumps(public_result, indent=2, sort_keys=True, allow_nan=False) + "\n",
        encoding="utf-8",
    )
    return public_result


def _run_compiled_experiment(
    compiled: CompiledExperiment,
    *,
    rollout_client: RolloutClientProtocol,
    evolution_client: EvolutionClientProtocol,
    worker_runner: WorkerRunner | None,
    artifact_root: Path,
    output_root: Path,
    promotion_reviewer: PromotionReviewer | None,
    poll_interval_seconds: float,
    max_poll_attempts: int,
    executable_registry: VerifiedExecutableRegistry,
    initial_context_artifact_ids: Mapping[str, Sequence[str]] | None,
    core_authoritative_successor: bool,
    managed_worker: bool,
) -> dict[str, Any]:
    task_results: list[dict[str, Any]] = []
    any_failure = False
    any_pending_review = False
    run_id = compiled.run_id or uuid4().hex
    initial_context = _validated_initial_context_artifact_ids(initial_context_artifact_ids)
    for task in compiled.tasks:
        history_context_artifact_ids = _snapshot_context_artifact_ids(initial_context)
        rollout_context_artifact_ids = _runtime_context_artifact_ids(initial_context)
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
                "artifact_ids": _snapshot_context_artifact_ids(history_context_artifact_ids),
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
            next_rollout_context_artifact_ids = _empty_context_artifact_ids()
            round_failed = False
            method_specs = compiled.evolution_methods_for_round(
                round_index,
                prior_dataset_artifact_ids=prior_context_artifact_ids["dataset"],
                task_id=task.task_id,
            )
            for spec in method_specs:
                job_payload = task.evolution_job_payloads_for_round(
                    round_index,
                    [spec],
                    dataset_artifact_id=dataset_artifact_id,
                    context_artifact_ids=prior_context_artifact_ids,
                )[0]
                claim_capability = (
                    spec.method
                    if managed_worker
                    else _claim_capability_for_job(
                        run_id=run_id,
                        task_id=task.task_id,
                        round_index=round_index,
                        method=spec.method,
                    )
                )
                job_payload["job_type"] = claim_capability
                created_job = evolution_client.create_plan_bound_job(
                    _plan_bound_job_payload(spec, job_payload)
                )
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
                    executable_registry=executable_registry,
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
                    artifacts = [
                        evolution_client.get_artifact(artifact_id) for artifact_id in artifact_ids
                    ]
                    for expected_artifact_id, artifact in zip(
                        artifact_ids,
                        artifacts,
                        strict=True,
                    ):
                        actual_artifact_id = _required_text(
                            artifact,
                            "artifact_id",
                            "artifact response",
                        )
                        if actual_artifact_id != expected_artifact_id:
                            raise ValueError(
                                "artifact response identity does not match worker output: "
                                f"expected {expected_artifact_id!r}, got {actual_artifact_id!r}"
                            )
                    target_artifacts = [
                        artifact
                        for artifact in artifacts
                        if artifact.get("type") == spec.artifact_type
                    ]
                    target_artifact_ids = [
                        _required_text(artifact, "artifact_id", "artifact response")
                        for artifact in target_artifacts
                    ]
                    approved_artifact_ids: list[str]
                    promotion_reviews: list[dict[str, Any]] = []
                    if _promotion_gate_targets_artifact(
                        compiled.promotion_gate,
                        spec.artifact_type,
                    ):
                        _demote_already_promoted_artifacts(
                            evolution_client,
                            target_artifacts,
                        )
                        promotion_result = evaluate_promotion_gate(
                            gate_config=compiled.promotion_gate,
                            artifact_type=spec.artifact_type,
                            method=spec.method,
                            task_id=task.task_id,
                            round_index=round_index,
                            job_id=created_job_id,
                            job_payload=job_payload,
                            artifacts=target_artifacts,
                            output_root=output_root,
                            content_roots=[artifact_root],
                            reviewer=promotion_reviewer,
                        )
                        approved_artifact_ids = list(promotion_result["approved_artifact_ids"])
                        promotion_status = str(promotion_result["status"])
                        promotion_reviews = list(promotion_result["reviews"])
                        _create_backend_promotion_reviews(
                            evolution_client,
                            promotion_reviews,
                        )
                        if promotion_status in {"approved", "partially_approved"}:
                            for artifact_id in approved_artifact_ids:
                                evolution_client.update_artifact_promotion(
                                    artifact_id,
                                    promoted=True,
                                )
                        elif promotion_status == "pending_review":
                            any_pending_review = True
                            round_failed = True
                        else:
                            any_failure = True
                            round_failed = True
                    else:
                        approved_artifact_ids = (
                            list(target_artifact_ids)
                            if core_authoritative_successor
                            else [
                                _required_text(
                                    artifact,
                                    "artifact_id",
                                    "artifact response",
                                )
                                for artifact in target_artifacts
                                if artifact.get("promoted") is True
                            ]
                        )
                        if approved_artifact_ids:
                            promotion_status = (
                                "core_selected" if core_authoritative_successor else "skipped"
                            )
                        else:
                            promotion_status = "missing_promoted_target_artifact"
                            any_failure = True
                            round_failed = True

                    history_context_artifact_ids.setdefault(spec.artifact_type, []).extend(
                        approved_artifact_ids
                    )
                    next_rollout_context_artifact_ids[spec.artifact_type] = list(
                        approved_artifact_ids
                    )
                else:
                    target_artifact_ids = []
                    approved_artifact_ids = []
                    promotion_status = "not_applicable"
                    promotion_reviews = []
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
                        "target_artifact_ids": target_artifact_ids,
                        "approved_artifact_ids": approved_artifact_ids,
                        "promotion_status": promotion_status,
                        "promotion_reviews": promotion_reviews,
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
        "status": (
            "failed" if any_failure else "pending_review" if any_pending_review else "completed"
        ),
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


def _runtime_context_artifact_ids(
    context_artifact_ids: dict[str, list[str]],
) -> dict[str, list[str]]:
    runtime_context = _snapshot_context_artifact_ids(context_artifact_ids)
    runtime_context["dataset"] = []
    return runtime_context


def _validated_initial_context_artifact_ids(
    value: Mapping[str, Sequence[str]] | None,
) -> dict[str, list[str]]:
    result = _empty_context_artifact_ids()
    if value is None:
        return result
    if len(value) > 128:
        raise ValueError("initial context has too many artifact types")
    total = 0
    for artifact_type, artifact_ids in value.items():
        if not isinstance(artifact_type, str) or not artifact_type or len(artifact_type) > 128:
            raise ValueError("initial context artifact type is invalid")
        if isinstance(artifact_ids, str) or not isinstance(artifact_ids, Sequence):
            raise TypeError("initial context artifact IDs must be a sequence")
        normalized: list[str] = []
        for artifact_id in artifact_ids:
            if (
                not isinstance(artifact_id, str)
                or not artifact_id
                or len(artifact_id.encode("utf-8")) > 256
            ):
                raise ValueError("initial context artifact ID is invalid")
            normalized.append(artifact_id)
        if len(normalized) > 256 or len(set(normalized)) != len(normalized):
            raise ValueError("initial context artifact IDs exceed their closed bound")
        total += len(normalized)
        if total > 1024:
            raise ValueError("initial context has too many artifact IDs")
        result[artifact_type] = normalized
    return result


def _promotion_gate_targets_artifact(
    promotion_gate: dict[str, Any],
    artifact_type: str,
) -> bool:
    if promotion_gate.get("mode") == "none":
        return False
    artifact_types = promotion_gate.get("artifact_types")
    if not isinstance(artifact_types, list):
        return False
    return artifact_type in artifact_types


def _demote_already_promoted_artifacts(
    evolution_client: EvolutionClientProtocol,
    artifacts: list[dict[str, Any]],
) -> None:
    for artifact in artifacts:
        artifact_id = artifact.get("artifact_id")
        if artifact.get("promoted") is not True or not isinstance(artifact_id, str):
            continue
        demoted_artifact = evolution_client.update_artifact_promotion(
            artifact_id,
            promoted=False,
        )
        artifact.update(demoted_artifact)


def _create_backend_promotion_reviews(
    evolution_client: EvolutionClientProtocol,
    promotion_reviews: list[dict[str, Any]],
) -> None:
    create_review_request = getattr(evolution_client, "create_review_request", None)
    if not callable(create_review_request):
        return
    for review in promotion_reviews:
        if review.get("status") != "pending_review":
            continue
        review_path = review.get("_review_path") or review.get("review_path")
        if not isinstance(review_path, str) or not review_path:
            continue
        packet = json.loads(Path(review_path).read_text(encoding="utf-8"))
        if not isinstance(packet, dict):
            raise ValueError(f"promotion review packet was not a JSON object: {review_path}")
        artifact_hashes = artifact_hashes_from_review_packet(packet)
        review_payload = review_request_payload_from_packet(
            packet,
            artifact_hashes=artifact_hashes,
        )
        review_payload["query_decision"] = _human_query_decision_payload_from_review_payload(
            review_payload
        )
        try:
            backend_review = create_review_request(review_payload)
            if not isinstance(backend_review, dict):
                raise ValueError("evolution review response was not a JSON object")
        except Exception as exc:  # noqa: BLE001
            review["backend_review_status"] = "failed"
            review["backend_review_error"] = sanitize_review_text(exc, limit=500)
            continue
        review["backend_review_status"] = "created"
        for key in ("review_id", "packet_id", "packet_hash"):
            value = backend_review.get(key)
            if isinstance(value, str) and value:
                review[key] = value
        query_decision_id = backend_review.get("query_decision_id")
        if isinstance(query_decision_id, str) and query_decision_id:
            review["query_decision_id"] = query_decision_id
            review["backend_query_decision_status"] = "created"
        else:
            review["backend_query_decision_status"] = "unavailable"


def _strip_internal_fields(value: object) -> object:
    if isinstance(value, dict):
        return {
            key: _strip_internal_fields(child)
            for key, child in value.items()
            if not str(key).startswith("_")
        }
    if isinstance(value, list):
        return [_strip_internal_fields(item) for item in value]
    return value


def _human_query_decision_payload_from_review_payload(
    review_payload: dict[str, Any],
) -> dict[str, Any]:
    return {
        "artifact_ids": _string_list(review_payload.get("artifact_ids")),
        "candidate_ids": _string_list(review_payload.get("candidate_ids")),
        "task_id": review_payload.get("task_id"),
        "round_index": review_payload.get("round_index"),
        "method": review_payload.get("method"),
        "decision": "ask_human",
        "reason_codes": ["promotion_gate_targeted", "human_gate"],
        "estimated_value_of_information": None,
        "estimated_human_cost": None,
        "budget_context": {},
    }


def _string_list(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, str) and item]


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


def _plan_bound_job_payload(
    spec: CompiledEvolutionMethodSpec,
    legacy_job_payload: dict[str, Any],
) -> dict[str, Any]:
    user_config = spec.selection.config()
    legacy_config = legacy_job_payload.get("config")
    if not isinstance(legacy_config, dict):
        raise ValueError("compiled evolution job config must be a JSON object")
    for key, value in user_config.items():
        if legacy_config.get(key) != value:
            raise ValueError(
                f"compiled evolution job changed normalized method config field {key!r}"
            )
    core_config = {key: value for key, value in legacy_config.items() if key not in user_config}
    return {
        "plan": spec.plan.model_dump(mode="json"),
        "target_id": spec.target_id,
        "job_type": legacy_job_payload["job_type"],
        "input_bindings": legacy_job_payload["input_bindings"],
        "core_config": core_config,
        "priority": legacy_job_payload.get("priority", 100),
    }


def _run_worker_for_job(
    compiled: CompiledExperiment,
    *,
    worker_runner: WorkerRunner | None,
    artifact_root: Path,
    capability: str,
    expected_job_id: str,
    executable_registry: VerifiedExecutableRegistry,
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
        executable_registry=executable_registry,
    )


def _run_local_worker_once(
    *,
    base_url: str,
    artifact_root: Path,
    capabilities: list[str],
    executable_registry: VerifiedExecutableRegistry,
) -> list[dict[str, Any]]:
    with _RecordingEvolutionWorkerClient(base_url) as client:
        claimed = run_once(
            client,
            worker_id="openevo-local-worker",
            capabilities=capabilities,
            artifact_root=artifact_root,
            lease_seconds=600,
            executable_registry=executable_registry,
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
    return _positive_int(dataset.get("event_count")) and _positive_int(dataset.get("trace_count"))


def _positive_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value > 0
