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
from openevo.sidecar.api import (
    DesktopExecutionStatus,
    OpenEvoDesktopShellStatus,
    SidecarHealth,
    build_desktop_shell_status,
    create_sidecar_app,
    default_desktop_shell_status,
)

__all__ = [
    "DesktopExecutionStatus",
    "OpenEvoDesktopShellStatus",
    "ProxySettings",
    "RemoteProfileConfig",
    "SSHAuthConfig",
    "SidecarHealth",
    "SidecarSciencePlan",
    "WorkspacePreparationAction",
    "WorkspacePreparationPlan",
    "build_sidecar_science_plan",
    "build_desktop_shell_status",
    "create_sidecar_app",
    "default_desktop_shell_status",
    "load_remote_profile_config",
    "plan_workspace_preparation",
    "preflight_settings_for_project",
]
