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
    OpenEvoDesktopBootstrapResponse,
    OpenEvoDesktopShellStatus,
    OpenEvoDesktopWorkspaceResponse,
    OpenEvoSidecarSession,
    SidecarHealth,
    build_desktop_shell_status,
    create_sidecar_app,
    create_sidecar_app_for_project,
    default_desktop_shell_status,
)

__all__ = [
    "DesktopExecutionStatus",
    "OpenEvoDesktopBootstrapResponse",
    "OpenEvoDesktopShellStatus",
    "OpenEvoDesktopWorkspaceResponse",
    "OpenEvoSidecarSession",
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
    "create_sidecar_app_for_project",
    "default_desktop_shell_status",
    "load_remote_profile_config",
    "plan_workspace_preparation",
    "preflight_settings_for_project",
]
