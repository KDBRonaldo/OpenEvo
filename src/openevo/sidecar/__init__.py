"""OpenEvo desktop sidecar profile models."""

from __future__ import annotations

from openevo.sidecar.models import (
    ProxySettings,
    RemoteProfileConfig,
    SSHAuthConfig,
    load_remote_profile_config,
)
from openevo.sidecar.planner import (
    SidecarSciencePlan,
    build_sidecar_science_plan,
    preflight_settings_for_project,
)
from openevo.sidecar.workspace import (
    WorkspacePreparationAction,
    WorkspacePreparationPlan,
    plan_workspace_preparation,
)

__all__ = [
    "ProxySettings",
    "RemoteProfileConfig",
    "SSHAuthConfig",
    "SidecarSciencePlan",
    "WorkspacePreparationAction",
    "WorkspacePreparationPlan",
    "build_sidecar_science_plan",
    "load_remote_profile_config",
    "plan_workspace_preparation",
    "preflight_settings_for_project",
]
