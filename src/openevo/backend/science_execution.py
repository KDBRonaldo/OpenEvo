"""Deterministic Core project projection into the existing experiment runner."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from openevo.backend.contracts.v1 import models as m
from openevo.backend.service_supervisor import ServiceRunBinding
from openevo.evolution.framework import EvolutionExecutionProfile
from openevo.evolution.framework.profiles import execution_profile_for_release_mode
from openevo.experiments.models import ExperimentConfig
from openevo.projects.science.compiler import MANAGED_RUNTIME_IMAGES


_RUNTIME_WORKDIR = "/openevo/session/workspace"


@dataclass(frozen=True, slots=True)
class CompiledScienceExecution:
    config: ExperimentConfig
    execution_profile: EvolutionExecutionProfile
    task_id: str
    submitted_task_id: str


def compile_science_execution(
    project: m.ProjectV1,
    *,
    run_id: str,
    binding: ServiceRunBinding,
    workspace_path: Path | None,
) -> CompiledScienceExecution:
    if project.status is not m.ProjectStatus.READY or project.active_revision is None:
        raise ValueError("science execution requires a ready project revision")
    if project.registry_digest != binding.registry_digest:
        raise ValueError("project and service generation registry digests differ")
    if not run_id or len(run_id) > 128 or any(ord(char) < 0x21 for char in run_id):
        raise ValueError("run_id is outside the closed execution identity policy")
    workspace = _workspace_path(project, workspace_path)
    task_id = f"science-{project.current_task_snapshot.content_sha256[:24]}"
    subscription = (
        project.spec.execution_mode
        is m.ExecutionMode.CODEX_SUBSCRIPTION_TRANSCRIPT
    )
    agent = {
        "preset": project.spec.harness_id,
        "model": project.spec.agent_model_ref,
        "auth": "subscription" if subscription else "proxy",
        "provider": "codex_cli",
        "settings": {
            "auth_mode": "subscription" if subscription else "proxy",
            "capture_mode": "transcript",
        },
    }
    runtime_env = {}
    if not subscription:
        runtime_env["OPENEVO_MANAGED_HF_MODEL"] = project.spec.agent_model_ref
    config = ExperimentConfig.model_validate(
        {
            "version": 1,
            "experiment": {"name": project.name},
            "agent": agent,
            "tasks": [
                {
                    "id": task_id,
                    "instruction": project.task.objective,
                    "workspace": str(workspace) if workspace is not None else None,
                    "metadata": {
                        "openevo": {
                            "project_id": project.id,
                            "project_snapshot_id": project.current_project_snapshot.id,
                            "public_run_id": run_id,
                            "revision_id": project.active_revision.id,
                            "task_snapshot_id": project.current_task_snapshot.id,
                            "workspace_snapshot_id": project.current_workspace_snapshot.id,
                        }
                    },
                }
            ],
            "runtime": {
                "kind": "docker",
                "image": MANAGED_RUNTIME_IMAGES["managed_science"],
                "workdir": _RUNTIME_WORKDIR,
                "env": runtime_env,
            },
            "rollout": {"url": binding.rollout_url},
            "evolution": {
                "backend_url": binding.evolution_backend_url,
                "rounds": 1,
                "targets": project.spec.evolution.model_dump(mode="json")["targets"],
            },
        }
    )
    profile = execution_profile_for_release_mode(
        project.spec.execution_mode,
        runtime_capabilities=() if subscription else ("adapter_serving",),
    )
    return CompiledScienceExecution(
        config=config,
        execution_profile=profile,
        task_id=task_id,
        submitted_task_id=f"{task_id}--run-{run_id}--round-0",
    )


def _workspace_path(project: m.ProjectV1, workspace_path: Path | None) -> Path | None:
    if project.workspace_kind is m.WorkspaceSourceKind.SCRATCH:
        if workspace_path is not None:
            raise ValueError("scratch project must not bind an imported workspace path")
        return None
    if workspace_path is None:
        raise ValueError("imported project requires its verified workspace snapshot path")
    path = Path(workspace_path)
    if not path.is_absolute():
        raise ValueError("workspace snapshot path must be absolute")
    return path


__all__ = ["CompiledScienceExecution", "compile_science_execution"]
