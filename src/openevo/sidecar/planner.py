from __future__ import annotations

import math
from collections.abc import Mapping
from types import MappingProxyType
from typing import Any

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    field_serializer,
    field_validator,
)

from openevo.remote.preflight import RemotePreflightSettings
from openevo.science import ScienceProjectConfig, compile_science_project
from openevo.sidecar.models import RemoteProfileConfig
from openevo.sidecar.workspace import (
    WorkspacePreparationPlan,
    plan_workspace_preparation,
)


class _StrictFrozenModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        strict=True,
        validate_default=True,
    )


class SidecarSciencePlan(_StrictFrozenModel):
    project_name: str
    task_id: str
    remote_profile_id: str
    proxy_env: Mapping[str, str] = Field(default_factory=dict)
    preflight: RemotePreflightSettings
    workspace: WorkspacePreparationPlan
    experiment: Mapping[str, Any]

    @field_validator("project_name", "task_id", "remote_profile_id")
    @classmethod
    def _strip_required_text(cls, value: str, info) -> str:
        text = value.strip()
        if not text:
            raise ValueError(f"{info.field_name} must be a non-empty string")
        return text

    @field_validator("proxy_env")
    @classmethod
    def _validate_proxy_env(cls, value: Mapping[str, str]) -> Mapping[str, str]:
        env: dict[str, str] = {}
        for key, item in value.items():
            env_key = key.strip()
            if not env_key:
                raise ValueError("proxy_env key must be a non-empty string")
            env_value = item.strip()
            if not env_value:
                raise ValueError(f"proxy_env.{env_key} must be a non-empty string")
            env[env_key] = env_value
        return MappingProxyType(env)

    @field_validator("experiment")
    @classmethod
    def _freeze_experiment(cls, value: Mapping[str, Any]) -> Mapping[str, Any]:
        frozen = _freeze_json(value)
        if not isinstance(frozen, Mapping):
            raise ValueError("experiment must be a JSON object")
        return frozen

    @field_serializer("proxy_env", "experiment")
    def _serialize_json_snapshot(self, value: object) -> object:
        return _thaw_json(value)


def preflight_settings_for_project(
    project: ScienceProjectConfig,
    profile: RemoteProfileConfig,
) -> RemotePreflightSettings:
    return RemotePreflightSettings(
        require_codex_subscription=(
            project.execution.mode == "codex_subscription_transcript"
        ),
        min_home_available_kb=profile.min_home_available_kb,
    )


def build_sidecar_science_plan(
    project: ScienceProjectConfig,
    profile: RemoteProfileConfig,
) -> SidecarSciencePlan:
    if project.remote_profile != profile.id:
        raise ValueError(
            "project.remote_profile must match remote profile id: "
            f"{project.remote_profile!r} != {profile.id!r}"
        )

    workspace = plan_workspace_preparation(project, profile)
    compiled_experiment = compile_science_project(
        project,
        prepared_workspaces=workspace.to_prepared_workspaces(),
    )
    return SidecarSciencePlan(
        project_name=project.project.name,
        task_id=project.task.id,
        remote_profile_id=profile.id,
        proxy_env=profile.proxy.to_env(),
        preflight=preflight_settings_for_project(project, profile),
        workspace=workspace,
        experiment=compiled_experiment.model_dump(mode="json", exclude={"path"}),
    )


def _freeze_json(value: object) -> object:
    if isinstance(value, Mapping):
        frozen: dict[str, object] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise ValueError("JSON object keys must be strings")
            frozen[key] = _freeze_json(item)
        return MappingProxyType(frozen)
    if isinstance(value, list | tuple):
        return tuple(_freeze_json(item) for item in value)
    if value is None or isinstance(value, str | bool | int):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("JSON numbers must be finite")
        return value
    raise ValueError("experiment must contain only JSON-serializable values")


def _thaw_json(value: object) -> object:
    if isinstance(value, Mapping):
        return {key: _thaw_json(item) for key, item in value.items()}
    if isinstance(value, tuple | list):
        return [_thaw_json(item) for item in value]
    return value
