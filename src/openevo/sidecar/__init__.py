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
    OpenEvoDesktopProjectConfigResponse,
    OpenEvoDesktopRunResponse,
    OpenEvoDesktopRunStatus,
    OpenEvoDesktopShellStatus,
    OpenEvoDesktopWorkspaceResponse,
    OpenEvoSidecarSession,
    SidecarHealth,
    build_desktop_shell_status,
    create_sidecar_app,
    create_sidecar_app_for_project,
    default_desktop_shell_status,
)
from openevo.sidecar.config import (
    DesktopProjectConfigDraft,
    DesktopProjectConfigPaths,
    DesktopProjectConfigSummary,
    build_desktop_project_configs,
    list_desktop_project_configs,
    load_desktop_project_config,
    save_desktop_project_config,
)

__all__ = [
    "DesktopExecutionStatus",
    "DesktopProjectConfigDraft",
    "DesktopProjectConfigPaths",
    "DesktopProjectConfigSummary",
    "OpenEvoDesktopBootstrapResponse",
    "OpenEvoDesktopProjectConfigResponse",
    "OpenEvoDesktopRunResponse",
    "OpenEvoDesktopRunStatus",
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
    "build_desktop_project_configs",
    "build_sidecar_science_plan",
    "build_desktop_shell_status",
    "create_sidecar_app",
    "create_sidecar_app_for_project",
    "default_desktop_shell_status",
    "list_desktop_project_configs",
    "load_desktop_project_config",
    "load_remote_profile_config",
    "plan_workspace_preparation",
    "preflight_settings_for_project",
    "save_desktop_project_config",
]
