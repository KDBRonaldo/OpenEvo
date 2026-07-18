from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
import threading
from typing import Any
from weakref import WeakKeyDictionary

from openevo.evolution.framework import (
    EvolutionExecutionProfile,
    EvolutionMethodDescriptor,
    EvolutionPlan,
    EvolutionTargetSelection,
    InputBindingSource,
    MethodInputBinding,
    ProjectEvolutionTargetSelection,
    ProjectEvolutionTargetMap,
    ProjectConfigInjectionSource,
    RegistrySnapshot,
    ResolvedEvolutionSelection,
    canonical_digest,
    resolve_agent_system_method,
)

from .models import (
    ExperimentConfig,
    PROMOTION_SUPPORT_FIELDS,
    TaskConfig,
)

_EVOLUTION_ORDER = ("text_memory", "parametric_memory", "skill_bundle", "agent_system")
_LEGACY_AGENT_SYSTEM_HISTORY_METHODS = frozenset(
    {
        "agent_system_history_reflector",
        "agent_system_pareto_reflector",
        "agent_system_gepa_reflector",
    }
)
_SUBSCRIPTION_AUTH_MODES = {"subscription", "chatgpt_subscription"}
_CORE_PROJECT_SCOPE_SEAL = object()


class _CoreProjectScopeAuthority:
    __slots__ = ("_project_id", "_run_id", "__weakref__")

    def __init__(self, *, project_id: str, run_id: str, _seal: object) -> None:
        if _seal is not _CORE_PROJECT_SCOPE_SEAL:
            raise TypeError("Core project scope authority cannot be constructed directly")
        object.__setattr__(self, "_project_id", project_id)
        object.__setattr__(self, "_run_id", run_id)

    @property
    def project_id(self) -> str:
        return self._project_id

    @property
    def run_id(self) -> str:
        return self._run_id

    def __setattr__(self, _name: str, _value: object) -> None:
        raise AttributeError("Core project scope authority is immutable")


_CORE_PROJECT_SCOPE_AUTHORITIES: WeakKeyDictionary[
    _CoreProjectScopeAuthority,
    tuple[str, str],
] = WeakKeyDictionary()
_CORE_PROJECT_SCOPE_LOCK = threading.Lock()


ContextArtifactIds = Mapping[str, str | Sequence[str]] | Sequence[str] | None


class ProjectEvolutionValidationError(ValueError):
    """A project selection rejected before any run process is launched."""

    def __init__(
        self,
        *,
        target_id: str,
        selection: str | None,
        reason_code: str,
    ) -> None:
        super().__init__(f"invalid project evolution selection for {target_id!r}")
        self.target_id = target_id
        self.selection = selection
        self.reason_code = reason_code


@dataclass(frozen=True)
class CompiledEvolutionMethodSpec:
    artifact_type: str
    target_id: str
    handler_id: str
    method: str
    requested_method: str
    prior_dataset_artifact_ids: tuple[str, ...]
    job_type: str
    config: dict[str, Any]
    plan: EvolutionPlan
    selection: ResolvedEvolutionSelection
    input_bindings: tuple[MethodInputBinding, ...]

    @property
    def plan_id(self) -> str:
        return self.plan.plan_id

    @property
    def registry_snapshot_digest(self) -> str:
        return self.plan.registry_snapshot_digest

    @property
    def target_identity_digest(self) -> str:
        return self.selection.target_identity_digest

    @property
    def handler_identity_digest(self) -> str:
        return self.selection.handler_identity_digest

    @property
    def method_identity_digest(self) -> str:
        return self.selection.method_identity_digest

    def job_payload(self, input_artifact_ids: Sequence[str]) -> dict[str, Any]:
        return {
            "target_id": self.target_id,
            "method": self.method,
            "job_type": self.job_type,
            "input_artifact_ids": list(input_artifact_ids),
            "config": dict(self.config),
            "plan": self.plan.model_dump(mode="json"),
            "plan_selection": self.selection.model_dump(mode="json"),
            "priority": 100,
        }


@dataclass(frozen=True)
class CompiledTask:
    experiment_id: str
    experiment_name: str
    run_id: str | None
    core_project_scope: _CoreProjectScopeAuthority | None
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
            payload = spec.job_payload(())
            planned_input_bindings = _planned_input_bindings(
                spec,
                dataset_artifact_id=dataset_artifact_id,
                context_artifact_ids=context_artifact_ids,
            )
            payload["input_bindings"] = planned_input_bindings
            payload["input_artifact_ids"] = [
                artifact_id
                for binding in planned_input_bindings
                for artifact_id in binding["artifact_ids"]
            ]
            input_artifact_ids = payload["input_artifact_ids"]
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
                        "prior_dataset_artifact_ids": list(spec.prior_dataset_artifact_ids),
                    },
                },
                "compatibility": compatibility,
            }
            if spec.target_id == "parametric_memory":
                base_model = str(payload["config"].get("base_model") or "").strip()
                if base_model:
                    payload["config"]["compatibility"]["base_model"] = [base_model]
            if _promotion_gate_targets_artifact(self.promotion_gate, spec.artifact_type):
                payload["config"]["promoted"] = False
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
            tags = [f"openevo_run_task:{self.run_id}:{self.task_id}"]
        else:
            tags = [f"openevo_task:{self.experiment_name}:{self.task_id}"]
        project_scope_id = _core_project_scope_id_for_run(
            self.core_project_scope,
            run_id=self.run_id,
        )
        if project_scope_id is not None:
            tags.append(f"openevo_project:{project_scope_id}")
        return tags


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
    _registry_snapshot: RegistrySnapshot
    _execution_profile: EvolutionExecutionProfile

    def evolution_plan_for_round(
        self,
        round_index: int,
        *,
        prior_dataset_artifact_ids: Sequence[str],
        task_id: str | None = None,
    ) -> EvolutionPlan:
        _validate_round_index(round_index, self.round_count)
        task = self._select_task(task_id)
        prior_dataset_ids = _prior_dataset_artifact_ids(prior_dataset_artifact_ids)
        selections = _plan_selections(
            self._target_selections_json,
            prior_dataset_artifact_ids=prior_dataset_ids,
            agent_model=str(task.agent.get("model_name") or ""),
            reflector_llm=self.reflector_llm,
            registry_snapshot=self._registry_snapshot,
        )
        probe = self._registry_snapshot.compile_plan(
            plan_id="plan-identity-probe",
            selections=selections,
            profile=self._execution_profile,
        )
        plan_identity = {
            "experiment_id": self.experiment_id,
            "run_id": self.run_id,
            "task_id": task.task_id,
            "round_index": round_index,
            "prior_dataset_artifact_ids": list(prior_dataset_ids),
            "resolved_plan": probe.model_dump(
                mode="json",
                exclude={"plan_id"},
            ),
        }
        return self._registry_snapshot.compile_plan(
            plan_id=f"plan-{canonical_digest(plan_identity)}",
            selections=selections,
            profile=self._execution_profile,
        )

    def evolution_methods_for_round(
        self,
        round_index: int,
        *,
        prior_dataset_artifact_ids: Sequence[str],
        task_id: str | None = None,
    ) -> list[CompiledEvolutionMethodSpec]:
        normalized_prior_dataset_ids = _prior_dataset_artifact_ids(prior_dataset_artifact_ids)
        plan = self.evolution_plan_for_round(
            round_index,
            prior_dataset_artifact_ids=normalized_prior_dataset_ids,
            task_id=task_id,
        )
        specs: list[CompiledEvolutionMethodSpec] = []
        for resolved in _ordered_plan_selections(plan.selections):
            requested = _requested_selection(
                self._target_selections_json,
                resolved.target_id,
            )
            target_descriptor = self._registry_snapshot.targets[resolved.target_id]
            method_descriptor = self._registry_snapshot.methods[resolved.method_id]
            specs.append(
                _compile_method_spec(
                    plan,
                    resolved,
                    requested_selection=requested,
                    prior_dataset_artifact_ids=normalized_prior_dataset_ids,
                    artifact_type=target_descriptor.artifact_type,
                    input_bindings=method_descriptor.input_bindings,
                )
            )
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
                task_id=task.task_id,
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
    *,
    registry_snapshot: RegistrySnapshot,
    execution_profile: EvolutionExecutionProfile,
) -> CompiledExperiment:
    return _compile_experiment_with_scope(
        config,
        task_ids=task_ids,
        rounds_override=rounds_override,
        run_id=run_id,
        core_project_scope=None,
        registry_snapshot=registry_snapshot,
        execution_profile=execution_profile,
    )


def _compile_core_experiment(
    config: ExperimentConfig,
    task_ids: Sequence[str] | None = None,
    rounds_override: int | None = None,
    *,
    run_id: str,
    core_project_scope: _CoreProjectScopeAuthority,
    registry_snapshot: RegistrySnapshot,
    execution_profile: EvolutionExecutionProfile,
) -> CompiledExperiment:
    if not isinstance(core_project_scope, _CoreProjectScopeAuthority):
        raise ValueError("Core project scope authority is required")
    normalized_run_id = _normalize_core_run_id(run_id)
    return _compile_experiment_with_scope(
        config,
        task_ids=task_ids,
        rounds_override=rounds_override,
        run_id=normalized_run_id,
        core_project_scope=core_project_scope,
        registry_snapshot=registry_snapshot,
        execution_profile=execution_profile,
    )


def _compile_experiment_with_scope(
    config: ExperimentConfig,
    task_ids: Sequence[str] | None,
    rounds_override: int | None,
    run_id: str | None,
    *,
    core_project_scope: _CoreProjectScopeAuthority | None,
    registry_snapshot: RegistrySnapshot,
    execution_profile: EvolutionExecutionProfile,
) -> CompiledExperiment:
    round_count = rounds_override if rounds_override is not None else config.evolution.rounds
    if isinstance(round_count, bool) or round_count < 1:
        raise ValueError(f"round_count must be at least 1, got {round_count}")

    normalized_run_id = _normalize_run_id(run_id)
    _core_project_scope_id_for_run(
        core_project_scope,
        run_id=normalized_run_id,
    )
    selected_tasks = _select_tasks(config.tasks, task_ids)
    target_selections = _target_selections(config)
    agent = _agent_payload(config)
    runtime = _runtime_payload(config)
    config_path = config.path
    experiment_id = config.experiment.name
    reflector_llm = _reflector_llm(config)
    registry_snapshot.compile_plan(
        plan_id="plan-validation-probe",
        selections=_plan_selections(
            target_selections,
            prior_dataset_artifact_ids=(),
            agent_model=config.agent.model,
            reflector_llm=reflector_llm,
            registry_snapshot=registry_snapshot,
        ),
        profile=execution_profile,
    )
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
                core_project_scope=core_project_scope,
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
        reflector_llm=reflector_llm,
        promotion_gate=_promotion_gate(config),
        _target_selections_json=target_selections,
        _registry_snapshot=registry_snapshot,
        _execution_profile=execution_profile,
    )


def validate_project_evolution_selections(
    targets: ProjectEvolutionTargetMap,
    *,
    agent_model: str,
    reflector_llm: Mapping[str, str],
    registry_snapshot: RegistrySnapshot,
    execution_profile: EvolutionExecutionProfile,
) -> None:
    """Validate direct, hidden, and resolver selections against one registry."""

    for target_id, requested in targets.items():
        if not requested.enabled:
            continue
        try:
            target = registry_snapshot.targets[target_id]
        except KeyError as exc:
            raise ProjectEvolutionValidationError(
                target_id=target_id,
                selection=requested.method,
                reason_code="unknown_target",
            ) from exc
        try:
            resolved_method_id = _resolve_project_method_id(
                target_id,
                requested.method,
                prior_dataset_artifact_ids=(),
                registry_snapshot=registry_snapshot,
            )
        except ValueError as exc:
            raise ProjectEvolutionValidationError(
                target_id=target_id,
                selection=requested.method,
                reason_code="unknown_or_unsupported_selection",
            ) from exc
        resolver = next(
            (
                candidate
                for candidate in target.selection_resolvers
                if candidate.selection_value == requested.method
            ),
            None,
        )
        method_ids = (
            resolver.resolved_method_ids if resolver is not None else (resolved_method_id,)
        )
        for method_id in method_ids:
            method = registry_snapshot.methods.get(method_id)
            if method is None:
                raise ProjectEvolutionValidationError(
                    target_id=target_id,
                    selection=requested.method,
                    reason_code="unknown_resolved_method",
                )
            config = _project_method_config(
                requested.config,
                method_descriptor=method,
                agent_model=agent_model,
                reflector_llm=reflector_llm,
            )
            try:
                registry_snapshot.resolve_selection(
                    EvolutionTargetSelection(
                        target_id=target_id,
                        enabled=True,
                        method_id=method_id,
                        config=config,
                    ),
                    execution_profile,
                )
            except ValueError as exc:
                raise ProjectEvolutionValidationError(
                    target_id=target_id,
                    selection=requested.method,
                    reason_code="invalid_method_config_or_profile",
                ) from exc


def _normalize_run_id(run_id: str | None) -> str | None:
    if run_id is None:
        return None
    text = str(run_id).strip()
    if not text:
        raise ValueError("run_id must be a non-empty string when provided")
    return text


def _normalize_core_project_scope_id(project_id: str | None) -> str | None:
    if project_id is None:
        return None
    if (
        not isinstance(project_id, str)
        or not project_id
        or project_id != project_id.strip()
        or len(project_id) > 128
        or any(ord(char) < 0x20 or ord(char) == 0x7F for char in project_id)
    ):
        raise ValueError("core_project_scope_id is outside the Core opaque ID policy")
    return project_id


def _normalize_core_run_id(run_id: object) -> str:
    if (
        not isinstance(run_id, str)
        or not run_id
        or run_id != run_id.strip()
        or len(run_id) > 128
        or any(ord(char) < 0x20 or ord(char) == 0x7F for char in run_id)
    ):
        raise ValueError("Core run_id is outside the Core opaque ID policy")
    return run_id


def _issue_core_project_scope_authority(
    *,
    project_id: str,
    run_id: str,
) -> _CoreProjectScopeAuthority:
    normalized_project_id = _normalize_core_project_scope_id(project_id)
    normalized_run_id = _normalize_core_run_id(run_id)
    if normalized_project_id is None:
        raise ValueError("Core project scope authority requires project and run identities")
    authority = _CoreProjectScopeAuthority(
        project_id=normalized_project_id,
        run_id=normalized_run_id,
        _seal=_CORE_PROJECT_SCOPE_SEAL,
    )
    with _CORE_PROJECT_SCOPE_LOCK:
        _CORE_PROJECT_SCOPE_AUTHORITIES[authority] = (
            normalized_project_id,
            normalized_run_id,
        )
    return authority


def _core_project_scope_id_for_run(
    authority: _CoreProjectScopeAuthority | None,
    *,
    run_id: str | None,
) -> str | None:
    if authority is None:
        return None
    if not isinstance(authority, _CoreProjectScopeAuthority):
        raise ValueError("Core project scope authority is invalid")
    with _CORE_PROJECT_SCOPE_LOCK:
        issued_binding = _CORE_PROJECT_SCOPE_AUTHORITIES.get(authority)
    if issued_binding is None:
        raise ValueError("Core project scope authority was not issued by Core")
    project_id, issued_run_id = issued_binding
    if run_id is None or issued_run_id != run_id:
        raise ValueError("Core project scope authority belongs to another run")
    return project_id


def _compile_method_spec(
    plan: EvolutionPlan,
    selection: ResolvedEvolutionSelection,
    *,
    requested_selection: ProjectEvolutionTargetSelection,
    prior_dataset_artifact_ids: Sequence[str],
    artifact_type: str,
    input_bindings: tuple[MethodInputBinding, ...],
) -> CompiledEvolutionMethodSpec:
    base_config = selection.config()
    if selection.target_id == "parametric_memory":
        compatibility = requested_selection.config.get("compatibility")
        if isinstance(compatibility, Mapping):
            base_config["compatibility"] = dict(compatibility)

    return CompiledEvolutionMethodSpec(
        artifact_type=artifact_type,
        target_id=selection.target_id,
        handler_id=selection.handler_id,
        method=selection.method_id,
        requested_method=requested_selection.method or selection.method_id,
        prior_dataset_artifact_ids=tuple(prior_dataset_artifact_ids),
        job_type=selection.method_id,
        config=base_config,
        plan=plan,
        selection=selection,
        input_bindings=input_bindings,
    )


def _target_selections(
    config: ExperimentConfig,
) -> tuple[tuple[str, str], ...]:
    return tuple(
        (
            target_id,
            selection.model_dump_json(),
        )
        for target_id, selection in sorted(config.evolution.targets.items())
    )


def _requested_selection(
    selections_json: tuple[tuple[str, str], ...],
    target_id: str,
) -> ProjectEvolutionTargetSelection:
    for candidate_target, encoded in selections_json:
        if candidate_target == target_id:
            return ProjectEvolutionTargetSelection.model_validate_json(encoded)
    raise ValueError(f"missing requested selection for resolved target {target_id!r}")


def _plan_selections(
    selections_json: tuple[tuple[str, str], ...],
    *,
    prior_dataset_artifact_ids: tuple[str, ...],
    agent_model: str,
    reflector_llm: Mapping[str, str],
    registry_snapshot: RegistrySnapshot,
) -> tuple[EvolutionTargetSelection, ...]:
    selections: list[EvolutionTargetSelection] = []
    for target_id, encoded in selections_json:
        requested = ProjectEvolutionTargetSelection.model_validate_json(encoded)
        if not requested.enabled:
            continue
        if target_id not in registry_snapshot.targets:
            raise ValueError(f"unknown target {target_id!r}")
        method_id = _resolve_project_method_id(
            target_id,
            requested.method,
            prior_dataset_artifact_ids=prior_dataset_artifact_ids,
            registry_snapshot=registry_snapshot,
        )
        method_descriptor = registry_snapshot.methods.get(method_id)
        if method_descriptor is None:
            raise ValueError(f"unknown method {method_id!r}")
        config = _project_method_config(
            requested.config,
            method_descriptor=method_descriptor,
            agent_model=agent_model,
            reflector_llm=reflector_llm,
        )
        selections.append(
            EvolutionTargetSelection(
                target_id=target_id,
                enabled=True,
                method_id=method_id,
                config=config,
            )
        )
    return tuple(selections)


def _planned_input_bindings(
    spec: CompiledEvolutionMethodSpec,
    *,
    dataset_artifact_id: str,
    context_artifact_ids: ContextArtifactIds,
) -> list[dict[str, Any]]:
    if not spec.input_bindings:
        return []
    planned: list[dict[str, Any]] = []
    for binding in spec.input_bindings:
        matching = _input_artifact_ids_for_binding(
            binding,
            spec=spec,
            dataset_artifact_id=dataset_artifact_id,
            context_artifact_ids=context_artifact_ids,
        )
        planned.append(
            {
                "binding_id": binding.binding_id,
                "artifact_ids": matching,
            }
        )
    return planned


def _input_artifact_ids_for_binding(
    binding: MethodInputBinding,
    *,
    spec: CompiledEvolutionMethodSpec,
    dataset_artifact_id: str,
    context_artifact_ids: ContextArtifactIds,
) -> list[str]:
    if binding.source is InputBindingSource.CURRENT_DATASET:
        return [dataset_artifact_id] if binding.artifact_type == "dataset" else []
    if binding.source is InputBindingSource.HISTORY_DATASETS:
        return list(spec.prior_dataset_artifact_ids) if binding.artifact_type == "dataset" else []
    if binding.source is InputBindingSource.CURRENT_TARGET_ARTIFACTS:
        return _context_artifact_ids_for_type(
            context_artifact_ids,
            binding.artifact_type,
        )
    if binding.source is InputBindingSource.EXPLICIT_INPUTS:
        if binding.artifact_type == "dataset":
            artifact_ids = [dataset_artifact_id]
            # Preserve the pre-framework experiment projection exactly. Other
            # methods can request history through a HISTORY_DATASETS binding.
            if (
                spec.target_id == "agent_system"
                and spec.method in _LEGACY_AGENT_SYSTEM_HISTORY_METHODS
            ):
                artifact_ids.extend(spec.prior_dataset_artifact_ids)
            return artifact_ids
        return _context_artifact_ids_for_type(
            context_artifact_ids,
            binding.artifact_type,
        )
    raise ValueError(f"unsupported input binding source {binding.source!r}")


def _project_method_config(
    requested_config: Mapping[str, Any],
    *,
    method_descriptor: EvolutionMethodDescriptor,
    agent_model: str,
    reflector_llm: Mapping[str, str],
) -> dict[str, Any]:
    config = dict(requested_config)
    config.pop("compatibility", None)
    for injection in method_descriptor.project_config_injections:
        config.pop(injection.field_name, None)
        if injection.source is ProjectConfigInjectionSource.REFLECTOR_LLM:
            config[injection.field_name] = dict(reflector_llm)
        elif injection.source is ProjectConfigInjectionSource.AGENT_MODEL:
            config[injection.field_name] = agent_model
        else:  # pragma: no cover - the descriptor enum is closed.
            raise ValueError(f"unsupported project config injection source {injection.source!r}")
    return config


def _resolve_project_method_id(
    target_id: str,
    requested_method: str | None,
    *,
    prior_dataset_artifact_ids: tuple[str, ...],
    registry_snapshot: RegistrySnapshot,
) -> str:
    target = registry_snapshot.targets[target_id]
    resolver = next(
        (
            candidate
            for candidate in target.selection_resolvers
            if candidate.selection_value == requested_method
        ),
        None,
    )
    if resolver is None:
        if requested_method is None:
            raise ValueError(f"target {target_id!r} has no selected method")
        return requested_method
    if target_id != "agent_system" or requested_method != "auto":
        raise ValueError(
            f"selection resolver {requested_method!r} is unsupported for target {target_id!r}"
        )
    resolved = resolve_agent_system_method(
        requested_method,
        prior_dataset_artifact_ids,
    )
    if resolved not in resolver.resolved_method_ids:
        raise ValueError(
            f"selection resolver {requested_method!r} returned undeclared method {resolved!r}"
        )
    return resolved


def _ordered_plan_selections(
    selections: Sequence[ResolvedEvolutionSelection],
) -> list[ResolvedEvolutionSelection]:
    builtin_order = {target_id: index for index, target_id in enumerate(_EVOLUTION_ORDER)}
    return sorted(
        selections,
        key=lambda selection: (
            0 if selection.target_id in builtin_order else 1,
            builtin_order.get(selection.target_id, 0),
            selection.target_id if selection.target_id not in builtin_order else "",
        ),
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
    if config.runtime.profile is not None:
        payload["profile"] = config.runtime.profile
    if config.runtime.container_user != "image":
        payload["container_user"] = config.runtime.container_user
    if config.runtime.image is not None:
        payload["image"] = config.runtime.image
    if config.runtime.env:
        payload["env"] = dict(config.runtime.env)
    if config.runtime.prepare:
        payload["prepare"] = [action.model_dump(mode="json") for action in config.runtime.prepare]
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
        if (config.agent.auth in _SUBSCRIPTION_AUTH_MODES or config.agent.preset == "codex")
        else "openai_chat"
    )
    return {"provider": provider, "model": config.agent.model}


def _promotion_gate(config: ExperimentConfig) -> dict[str, Any]:
    gate = config.evolution.promotion_gate.model_dump(mode="json")
    if not gate.get("llm"):
        gate["llm"] = _reflector_llm(config)
    return gate


def _worker_visible_promotion_gate(promotion_gate: Mapping[str, Any]) -> dict[str, Any]:
    return {str(key): value for key, value in promotion_gate.items() if key != "llm"}


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
        for artifact_type, artifact_ids in context_artifact_ids.items():
            if artifact_type != "dataset":
                ids.extend(_string_list(artifact_ids))
        return ids
    return _string_list(context_artifact_ids)


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
