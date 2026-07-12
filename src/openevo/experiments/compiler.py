from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from openevo.evolution.framework import (
    ProjectEvolutionTargetSelection,
    resolve_agent_system_method,
)

from .models import (
    ExperimentConfig,
    PROMOTION_SUPPORT_FIELDS,
    TaskConfig,
)

_EVOLUTION_ORDER = ("text_memory", "parametric_memory", "skill_bundle", "agent_system")
_ROLLOUT_CONTEXT_ARTIFACT_TYPES = _EVOLUTION_ORDER
_SUBSCRIPTION_AUTH_MODES = {"subscription", "chatgpt_subscription"}


ContextArtifactIds = Mapping[str, str | Sequence[str]] | Sequence[str] | None


@dataclass(frozen=True)
class CompiledEvolutionMethodSpec:
    artifact_type: str
    method: str
    requested_method: str
    prior_dataset_artifact_ids: tuple[str, ...]
    job_type: str
    config: dict[str, Any]

    def job_payload(self, input_artifact_ids: Sequence[str]) -> dict[str, Any]:
        return {
            "method": self.method,
            "job_type": self.job_type,
            "input_artifact_ids": list(input_artifact_ids),
            "config": dict(self.config),
            "priority": 100,
        }


@dataclass(frozen=True)
class CompiledTask:
    experiment_id: str
    experiment_name: str
    run_id: str | None
    task_id: str
    instruction: str
    workspace: str | None
    round_count: int
    agent: dict[str, Any]
    runtime: dict[str, Any] | None
    metadata: dict[str, Any]
    promotion_gate: dict[str, Any]

    def policy_version_for_round(self, round_index: int) -> str:
        self._validate_round_index(round_index)
        if self.run_id:
            return (
                f"openevo:{self.experiment_name}:{self.task_id}:"
                f"run-{self.run_id}:round-{round_index}"
            )
        return f"openevo:{self.experiment_name}:{self.task_id}:round-{round_index}"

    def rollout_payload_for_round(
        self,
        round_index: int,
        context_artifact_ids: ContextArtifactIds,
    ) -> dict[str, Any]:
        policy_version = self.policy_version_for_round(round_index)
        metadata = {
            **self.metadata,
            "experiment_id": self.experiment_id,
            "experiment_name": self.experiment_name,
            "task_id": self.task_id,
            "task_tags": self._task_tags(),
            "round_index": round_index,
            "policy_version": policy_version,
            "agent": _agent_metadata(self.agent),
        }
        flattened_context_ids = _all_context_artifact_ids(context_artifact_ids)
        existing_evolution = metadata.get("evolution")
        if not isinstance(existing_evolution, dict):
            existing_evolution = {}
        metadata["evolution"] = {
            **existing_evolution,
            "context_artifact_ids": flattened_context_ids,
        }

        payload = {
            "task_id": self.submitted_task_id_for_round(round_index),
            "instruction": self.instruction,
            "agent": dict(self.agent),
            "metadata": metadata,
        }
        if self.runtime is not None:
            payload["runtime"] = dict(self.runtime)
        return payload

    def dataset_payload_for_round(self, round_index: int) -> dict[str, Any]:
        policy_version = self.policy_version_for_round(round_index)
        return {
            "name": f"{self.experiment_name}:{self.task_id}:round-{round_index}",
            "purpose": "openevo_experiment_rollout",
            "query": {
                "event_types": ["openevo.session_completed"],
                "status": ["COMPLETED"],
                "policy_version": policy_version,
            },
            "limits": {"max_events": 10000, "max_traces": 50000},
        }

    def evolution_job_payloads_for_round(
        self,
        round_index: int,
        method_specs: Sequence[CompiledEvolutionMethodSpec],
        *,
        dataset_artifact_id: str,
        context_artifact_ids: ContextArtifactIds = None,
    ) -> list[dict[str, Any]]:
        policy_version = self.policy_version_for_round(round_index)
        jobs: list[dict[str, Any]] = []
        for spec in method_specs:
            context_ids = _job_context_artifact_ids(
                context_artifact_ids,
                spec,
            )
            input_artifact_ids = [dataset_artifact_id, *context_ids]
            payload = spec.job_payload(input_artifact_ids)
            compatibility = _job_compatibility(
                spec,
                payload["config"],
                task_tags=self._task_tags(),
                agent_harness=str(self.agent.get("harness") or ""),
            )
            payload["config"] = {
                **payload["config"],
                "name": f"{self.task_id}:{spec.artifact_type}:round-{round_index}",
                "experiment_id": self.experiment_id,
                "experiment_name": self.experiment_name,
                "promoted": not _promotion_gate_targets_artifact(
                    self.promotion_gate,
                    spec.artifact_type,
                ),
                "task_id": self.task_id,
                "task_tags": self._task_tags(),
                "round_index": round_index,
                "policy_version": policy_version,
                "lineage": {
                    "experiment_id": self.experiment_id,
                    "task_id": self.task_id,
                    "round_index": round_index,
                    "policy_version": policy_version,
                    "input_artifact_ids": input_artifact_ids,
                    "method_resolution": {
                        "requested_method": spec.requested_method,
                        "resolved_method": spec.method,
                        "prior_dataset_artifact_ids": list(
                            spec.prior_dataset_artifact_ids
                        ),
                    },
                },
                "compatibility": compatibility,
            }
            if spec.artifact_type == "parametric_memory":
                base_model = str(
                    payload["config"].get("base_model")
                    or self.agent.get("model_name")
                    or ""
                ).strip()
                if base_model:
                    if "base_model" not in payload["config"]:
                        payload["config"]["base_model"] = base_model
                    payload["config"]["compatibility"]["base_model"] = [base_model]
            if _promotion_gate_targets_artifact(self.promotion_gate, spec.artifact_type):
                payload["config"]["promotion_gate"] = _worker_visible_promotion_gate(
                    self.promotion_gate
                )
                payload["config"]["promotion_contract"] = {
                    "required": bool(self.promotion_gate.get("require_support", True)),
                    "fields": list(PROMOTION_SUPPORT_FIELDS),
                }
            jobs.append(payload)
        return jobs

    def _validate_round_index(self, round_index: int) -> None:
        if isinstance(round_index, bool) or round_index < 0 or round_index >= self.round_count:
            raise ValueError(
                f"round_index must be between 0 and {self.round_count - 1}, got {round_index}"
            )

    def submitted_task_id_for_round(self, round_index: int) -> str:
        self._validate_round_index(round_index)
        if not self.run_id:
            return self.task_id
        return f"{self.task_id}--run-{self.run_id}--round-{round_index}"

    def _task_tags(self) -> list[str]:
        if self.run_id:
            return [f"openevo_run_task:{self.run_id}:{self.task_id}"]
        return [f"openevo_task:{self.experiment_name}:{self.task_id}"]


def _job_compatibility(
    spec: CompiledEvolutionMethodSpec,
    config: Mapping[str, Any],
    *,
    task_tags: list[str],
    agent_harness: str,
) -> dict[str, Any]:
    compatibility: dict[str, Any] = {}
    if spec.artifact_type == "parametric_memory":
        configured = config.get("compatibility")
        if isinstance(configured, Mapping):
            compatibility.update(configured)

    compatibility.update(
        {
            "task_tags": task_tags,
            "agent_harness": [agent_harness],
        }
    )
    if spec.artifact_type == "parametric_memory":
        base_model = config.get("base_model")
        if isinstance(base_model, str) and base_model:
            compatibility["base_model"] = [base_model]
    return compatibility


@dataclass(frozen=True)
class CompiledExperiment:
    experiment_id: str
    experiment_name: str
    run_id: str | None
    round_count: int
    rollout_url: str
    evolution_backend_url: str
    tasks: list[CompiledTask]
    reflector_llm: dict[str, str]
    promotion_gate: dict[str, Any]
    _target_selections_json: tuple[tuple[str, str], ...]

    def evolution_methods_for_round(
        self,
        round_index: int,
        *,
        prior_dataset_artifact_ids: Sequence[str],
    ) -> list[CompiledEvolutionMethodSpec]:
        _validate_round_index(round_index, self.round_count)
        normalized_prior_dataset_ids = _prior_dataset_artifact_ids(
            prior_dataset_artifact_ids
        )
        selections = {
            target_id: ProjectEvolutionTargetSelection.model_validate_json(encoded)
            for target_id, encoded in self._target_selections_json
        }
        specs: list[CompiledEvolutionMethodSpec] = []
        for artifact_type in _EVOLUTION_ORDER:
            selection = selections.get(artifact_type)
            if selection is None:
                continue
            spec = _compile_method_spec(
                selection,
                artifact_type=artifact_type,
                prior_dataset_artifact_ids=normalized_prior_dataset_ids,
                reflector_llm=self.reflector_llm,
            )
            if spec is not None:
                specs.append(spec)
        return specs

    def evolution_job_payloads_for_round(
        self,
        round_index: int,
        *,
        dataset_artifact_id: str,
        context_artifact_ids: ContextArtifactIds = None,
        task_id: str | None = None,
        prior_dataset_artifact_ids: Sequence[str] | None = None,
    ) -> list[dict[str, Any]]:
        task = self._select_task(task_id)
        if prior_dataset_artifact_ids is None:
            prior_dataset_artifact_ids = (
                _context_artifact_ids_for_type(context_artifact_ids, "dataset")
                if isinstance(context_artifact_ids, Mapping)
                else ()
            )
        return task.evolution_job_payloads_for_round(
            round_index,
            self.evolution_methods_for_round(
                round_index,
                prior_dataset_artifact_ids=prior_dataset_artifact_ids,
            ),
            dataset_artifact_id=dataset_artifact_id,
            context_artifact_ids=context_artifact_ids,
        )

    def _select_task(self, task_id: str | None) -> CompiledTask:
        if task_id is None:
            if len(self.tasks) != 1:
                raise ValueError("task_id is required when the experiment has multiple tasks")
            return self.tasks[0]
        for task in self.tasks:
            if task.task_id == task_id:
                return task
        raise ValueError(f"Unknown task_id: {task_id}")


def compile_experiment(
    config: ExperimentConfig,
    task_ids: Sequence[str] | None = None,
    rounds_override: int | None = None,
    run_id: str | None = None,
) -> CompiledExperiment:
    round_count = rounds_override if rounds_override is not None else config.evolution.rounds
    if isinstance(round_count, bool) or round_count < 1:
        raise ValueError(f"round_count must be at least 1, got {round_count}")

    normalized_run_id = _normalize_run_id(run_id)
    selected_tasks = _select_tasks(config.tasks, task_ids)
    target_selections = _target_selections(config)
    agent = _agent_payload(config)
    runtime = _runtime_payload(config)
    config_path = config.path
    experiment_id = config.experiment.name
    return CompiledExperiment(
        experiment_id=experiment_id,
        experiment_name=config.experiment.name,
        run_id=normalized_run_id,
        round_count=round_count,
        rollout_url=config.rollout.url,
        evolution_backend_url=config.evolution.backend_url,
        tasks=[
            CompiledTask(
                experiment_id=experiment_id,
                experiment_name=config.experiment.name,
                run_id=normalized_run_id,
                task_id=task.id,
                instruction=task.instruction,
                workspace=_workspace_source(task, config_path),
                round_count=round_count,
                agent=agent,
                runtime=_runtime_for_task(runtime, task, config_path=config_path),
                metadata=dict(task.metadata),
                promotion_gate=_promotion_gate(config),
            )
            for task in selected_tasks
        ],
        reflector_llm=_reflector_llm(config),
        promotion_gate=_promotion_gate(config),
        _target_selections_json=target_selections,
    )


def _normalize_run_id(run_id: str | None) -> str | None:
    if run_id is None:
        return None
    text = str(run_id).strip()
    if not text:
        raise ValueError("run_id must be a non-empty string when provided")
    return text


def _compile_method_spec(
    selection: ProjectEvolutionTargetSelection,
    *,
    artifact_type: str,
    prior_dataset_artifact_ids: Sequence[str],
    reflector_llm: dict[str, str],
) -> CompiledEvolutionMethodSpec | None:
    if not selection.enabled:
        return None
    requested_method = selection.method
    if requested_method is None:  # Project validation makes this unreachable.
        raise ValueError(f"enabled evolution target {artifact_type!r} requires method")

    method = requested_method
    if requested_method == "auto":
        if artifact_type != "agent_system":
            raise ValueError(
                f"automatic method resolution is unsupported for target {artifact_type!r}"
            )
        method = resolve_agent_system_method(
            requested_method,
            prior_dataset_artifact_ids,
        )
    _validate_method_target(method, artifact_type)

    base_config = dict(selection.config)
    if artifact_type == "parametric_memory":
        base_config.pop("reflector_llm", None)
    else:
        base_config["reflector_llm"] = dict(reflector_llm)
    if artifact_type == "agent_system":
        base_config.setdefault("target_path", "AGENTS.md")

    return CompiledEvolutionMethodSpec(
        artifact_type=artifact_type,
        method=method,
        requested_method=requested_method,
        prior_dataset_artifact_ids=tuple(prior_dataset_artifact_ids),
        job_type=method,
        config=base_config,
    )


def _target_selections(
    config: ExperimentConfig,
) -> tuple[tuple[str, str], ...]:
    unknown_targets = set(config.evolution.targets).difference(_EVOLUTION_ORDER)
    for target_id in sorted(unknown_targets):
        if config.evolution.targets[target_id].enabled:
            raise ValueError(f"Unsupported evolution target: {target_id}")
    return tuple(
        (
            target_id,
            config.evolution.targets[target_id].model_dump_json(),
        )
        for target_id in _EVOLUTION_ORDER
        if target_id in config.evolution.targets
    )


def _validate_method_target(method_id: str, target_id: str) -> None:
    """Temporary A2 guard until planning consumes the verified frozen registry."""

    from openevo.evolution.methods import METHOD_METADATA

    metadata = METHOD_METADATA.get(method_id)
    if metadata is None:
        raise ValueError(f"Unknown evolution method: {method_id}")
    method_target = str(metadata.get("artifact_type") or "")
    if method_target != target_id:
        raise ValueError(
            f"method {method_id!r} belongs to target {method_target!r}, "
            f"not {target_id!r}"
        )


def _prior_dataset_artifact_ids(value: Sequence[str]) -> tuple[str, ...]:
    if isinstance(value, str) or not isinstance(value, Sequence):
        raise TypeError("prior_dataset_artifact_ids must be a sequence of strings")
    normalized: list[str] = []
    for artifact_id in value:
        if not isinstance(artifact_id, str):
            raise TypeError("prior_dataset_artifact_ids must contain only strings")
        if not artifact_id:
            raise ValueError("prior_dataset_artifact_ids must not contain empty IDs")
        normalized.append(artifact_id)
    return tuple(normalized)


def _select_tasks(
    tasks: Sequence[TaskConfig],
    task_ids: Sequence[str] | None,
) -> list[TaskConfig]:
    if task_ids is None:
        return list(tasks)
    requested = list(dict.fromkeys(task_ids))
    if not requested:
        raise ValueError("task_ids must select at least one task")
    known = {task.id for task in tasks}
    missing = [task_id for task_id in requested if task_id not in known]
    if missing:
        raise ValueError(f"Unknown task_id(s): {', '.join(missing)}")
    requested_set = set(requested)
    return [task for task in tasks if task.id in requested_set]


def _agent_payload(config: ExperimentConfig) -> dict[str, Any]:
    settings = dict(config.agent.settings)
    settings.setdefault("auth_mode", config.agent.auth)

    payload: dict[str, Any] = {
        "harness": config.agent.preset,
        "model_name": config.agent.model,
        "settings": settings,
    }
    if config.agent.env:
        payload["env"] = dict(config.agent.env)
    return payload


def _agent_metadata(agent: Mapping[str, Any]) -> dict[str, str]:
    return {
        "harness": str(agent.get("harness") or ""),
        "model_name": str(agent.get("model_name") or ""),
    }


def _runtime_payload(config: ExperimentConfig) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "backend": config.runtime.kind,
        "workdir": config.runtime.workdir,
    }
    if config.runtime.image is not None:
        payload["image"] = config.runtime.image
    if config.runtime.env:
        payload["env"] = dict(config.runtime.env)
    if config.runtime.prepare:
        payload["prepare"] = [
            action.model_dump(mode="json")
            for action in config.runtime.prepare
        ]
    return payload


def _runtime_for_task(
    runtime: dict[str, Any],
    task: TaskConfig,
    *,
    config_path: Path | None,
) -> dict[str, Any] | None:
    if not task.workspace and "image" not in runtime:
        return None

    payload = dict(runtime)
    if task.workspace:
        if "image" not in payload:
            raise ValueError("runtime.image is required when tasks[].workspace is set")
        payload["prepare"] = [
            {
                "type": "upload_dir",
                "source": _workspace_source(task, config_path),
                "target": payload["workdir"],
            },
            *payload.get("prepare", []),
        ]
    return payload


def _workspace_source(task: TaskConfig, config_path: Path | None) -> str | None:
    if task.workspace is None:
        return None
    workspace = Path(task.workspace).expanduser()
    if workspace.is_absolute() or config_path is None:
        return str(workspace)
    return str((config_path.resolve().parent / workspace).resolve())


def _reflector_llm(config: ExperimentConfig) -> dict[str, str]:
    if config.agent.provider:
        return {"provider": config.agent.provider, "model": config.agent.model}
    provider = (
        "codex_cli"
        if (
            config.agent.auth in _SUBSCRIPTION_AUTH_MODES
            or config.agent.preset == "codex"
        )
        else "openai_chat"
    )
    return {"provider": provider, "model": config.agent.model}


def _promotion_gate(config: ExperimentConfig) -> dict[str, Any]:
    gate = config.evolution.promotion_gate.model_dump(mode="json")
    if not gate.get("llm"):
        gate["llm"] = _reflector_llm(config)
    return gate


def _worker_visible_promotion_gate(promotion_gate: Mapping[str, Any]) -> dict[str, Any]:
    return {
        str(key): value
        for key, value in promotion_gate.items()
        if key != "llm"
    }


def _promotion_gate_targets_artifact(
    promotion_gate: Mapping[str, Any],
    artifact_type: str,
) -> bool:
    if promotion_gate.get("mode") == "none":
        return False
    artifact_types = promotion_gate.get("artifact_types")
    if not isinstance(artifact_types, Sequence) or isinstance(artifact_types, str):
        return False
    return artifact_type in artifact_types


def _validate_round_index(round_index: int, round_count: int) -> None:
    if isinstance(round_index, bool) or round_index < 0 or round_index >= round_count:
        raise ValueError(f"round_index must be between 0 and {round_count - 1}, got {round_index}")


def _context_artifact_ids_for_type(
    context_artifact_ids: ContextArtifactIds,
    artifact_type: str,
) -> list[str]:
    if context_artifact_ids is None:
        return []
    if isinstance(context_artifact_ids, Mapping):
        return _string_list(context_artifact_ids.get(artifact_type))
    return _string_list(context_artifact_ids)


def _all_context_artifact_ids(context_artifact_ids: ContextArtifactIds) -> list[str]:
    if context_artifact_ids is None:
        return []
    if isinstance(context_artifact_ids, Mapping):
        ids: list[str] = []
        for artifact_type in _ROLLOUT_CONTEXT_ARTIFACT_TYPES:
            ids.extend(_string_list(context_artifact_ids.get(artifact_type)))
        return ids
    return _string_list(context_artifact_ids)


def _job_context_artifact_ids(
    context_artifact_ids: ContextArtifactIds,
    spec: CompiledEvolutionMethodSpec,
) -> list[str]:
    context_ids = _context_artifact_ids_for_type(context_artifact_ids, spec.artifact_type)
    if (
        spec.artifact_type == "agent_system"
        and spec.method
        in {
            "agent_system_history_reflector",
            "agent_system_pareto_reflector",
            "agent_system_gepa_reflector",
        }
    ):
        return [*spec.prior_dataset_artifact_ids, *context_ids]
    return context_ids


def _string_list(value: object) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    if not isinstance(value, Sequence):
        raise TypeError("context artifact ids must be strings or sequences of strings")
    result: list[str] = []
    for item in value:
        if not isinstance(item, str):
            raise TypeError("context artifact ids must be strings")
        result.append(item)
    return result
