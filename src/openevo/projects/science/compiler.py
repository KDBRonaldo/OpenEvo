from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from ...experiments.models import ExperimentConfig
from openevo.projects.science.models import ScienceProjectConfig
from openevo.runtime.managed import (
    MANAGED_HOME,
    MANAGED_PATH,
    MANAGED_RUNTIME_IMAGES as MANAGED_RUNTIME_IMAGES,
    MANAGED_RUNTIME_RELEASES,
    MANAGED_SUBSCRIPTION_PREPARE_COMMAND,
)


_WORKDIR = "/openevo/session/workspace"
_MANAGED_PROXY_CODEX_HOME = f"{MANAGED_HOME}/.codex"


@dataclass(frozen=True)
class PreparedWorkspace:
    path: str
    source_fingerprint: str | None = None


def compile_science_project(
    project: ScienceProjectConfig,
    *,
    prepared_workspaces: Mapping[str, PreparedWorkspace] | None = None,
) -> ExperimentConfig:
    workspace = _task_workspace(project, prepared_workspaces)
    metadata = _task_metadata(project, prepared_workspaces)

    return ExperimentConfig.model_validate(
        {
            "version": 1,
            "experiment": {"name": project.project.name},
            "agent": _agent_payload(project),
            "tasks": [
                {
                    "id": project.task.id,
                    "instruction": project.task.objective,
                    "workspace": workspace,
                    "metadata": metadata,
                }
            ],
            "runtime": _runtime_payload(project),
            "evolution": {
                "targets": project.evolution.model_dump(mode="json")["targets"],
            },
        }
    )


def _agent_payload(project: ScienceProjectConfig) -> dict[str, Any]:
    managed_env = (
        {"CODEX_HOME": _MANAGED_PROXY_CODEX_HOME}
        if project.environment.profile != "custom_image"
        and project.execution.mode != "codex_subscription_transcript"
        else {}
    )
    if project.execution.mode == "codex_subscription_transcript":
        return {
            "preset": "codex",
            "model": project.execution.codex_model,
            "auth": "subscription",
            "settings": {
                "auth_mode": "subscription",
                "capture_mode": "transcript",
            },
            "env": managed_env,
        }

    return {
        "preset": "codex",
        "model": project.execution.hf_model,
        "auth": "proxy",
        "provider": "codex_cli",
        "settings": {
            "auth_mode": "proxy",
            "capture_mode": "transcript",
        },
        "env": managed_env,
    }


def _runtime_payload(project: ScienceProjectConfig) -> dict[str, Any]:
    env = dict(project.environment.env)
    if project.execution.mode == "self-deployed":
        env["OPENEVO_MANAGED_HF_MODEL"] = str(project.execution.hf_model)
    if project.environment.profile != "custom_image":
        env.update({"HOME": MANAGED_HOME, "PATH": MANAGED_PATH})

    return {
        "profile": (
            None
            if project.environment.profile == "custom_image"
            else project.environment.profile
        ),
        "image": _runtime_image(project),
        "container_user": (
            "image" if project.environment.profile == "custom_image" else "host"
        ),
        "workdir": _WORKDIR,
        "env": env,
        "prepare": [
            {
                "type": "exec",
                "command": _runtime_directory_prepare_command(project),
            },
            *[
                {
                    "type": "exec",
                    "command": command,
                    "cwd": _WORKDIR,
                }
                for command in project.task.setup_commands
            ],
        ],
    }


def _runtime_directory_prepare_command(project: ScienceProjectConfig) -> str:
    if project.environment.profile == "custom_image":
        return f"mkdir -p {_WORKDIR}"
    return MANAGED_SUBSCRIPTION_PREPARE_COMMAND


def _runtime_image(project: ScienceProjectConfig) -> str:
    if project.environment.profile == "custom_image":
        return str(project.environment.custom_image)
    return MANAGED_RUNTIME_RELEASES[
        project.environment.profile
    ].immutable_reference


def _task_workspace(
    project: ScienceProjectConfig,
    prepared_workspaces: Mapping[str, PreparedWorkspace] | None,
) -> str | None:
    source = project.task.source
    if source.type == "remote_path":
        return source.path
    if source.type == "scratch":
        return None
    return _required_prepared_workspace(project, prepared_workspaces).path


def _task_metadata(
    project: ScienceProjectConfig,
    prepared_workspaces: Mapping[str, PreparedWorkspace] | None,
) -> dict[str, Any]:
    metadata = dict(project.task.metadata)
    existing_openevo_metadata = metadata.get("openevo")
    openevo_metadata: dict[str, Any] = (
        dict(existing_openevo_metadata)
        if isinstance(existing_openevo_metadata, dict)
        else {}
    )
    openevo_metadata.update(
        {
            "project_name": project.project.name,
            "remote_profile": project.remote_profile,
            "source_type": project.task.source.type,
            "environment_profile": project.environment.profile,
            "execution_mode": project.execution.mode,
        }
    )
    prepared_workspace = _prepared_workspace(project, prepared_workspaces)
    if prepared_workspace is not None and prepared_workspace.source_fingerprint:
        openevo_metadata["source_fingerprint"] = prepared_workspace.source_fingerprint
    metadata["openevo"] = openevo_metadata
    return metadata


def _required_prepared_workspace(
    project: ScienceProjectConfig,
    prepared_workspaces: Mapping[str, PreparedWorkspace] | None,
) -> PreparedWorkspace:
    prepared_workspace = _prepared_workspace(project, prepared_workspaces)
    if prepared_workspace is None:
        raise ValueError(
            "prepared workspace is required for "
            f"{project.task.source.type} task {project.task.id!r}"
        )
    return prepared_workspace


def _prepared_workspace(
    project: ScienceProjectConfig,
    prepared_workspaces: Mapping[str, PreparedWorkspace] | None,
) -> PreparedWorkspace | None:
    if prepared_workspaces is None:
        return None
    return prepared_workspaces.get(project.task.id)
