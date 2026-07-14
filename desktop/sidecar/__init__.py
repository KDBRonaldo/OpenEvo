"""OpenEvo desktop sidecar profile models."""

from __future__ import annotations

from desktop.sidecar.api import (
    DesktopExecutionStatus,
    DesktopSidecarTransport,
    OpenEvoDesktopBootstrapResponse,
    OpenEvoDesktopProjectConfigResponse,
    OpenEvoDesktopRunResponse,
    OpenEvoDesktopRunStatus,
    OpenEvoDesktopShellStatus,
    OpenEvoDesktopWorkspaceResponse,
    OpenEvoSidecarSession,
    NativeSidecarInstance,
    SidecarHealth,
    build_desktop_shell_status,
    create_sidecar_app,
    create_sidecar_app_for_project,
    default_desktop_shell_status,
)
from desktop.sidecar.backend_client import (
    BackendClient,
    BackendConnection,
    DesktopBackendError,
)
from desktop.sidecar.config import (
    DesktopProjectConfigDraft,
    DesktopProjectConfigPaths,
    DesktopProjectConfigSummary,
    build_desktop_project_configs,
    list_desktop_project_configs,
    load_desktop_project_config,
    save_desktop_project_config,
)
from desktop.sidecar.core_client_v1 import (
    CoreClientErrorV1,
    CoreClientLocalErrorCodeV1,
    CoreClientLocalErrorV1,
    CoreControlClientV1,
    CoreSseStreamV1,
    CoreTunnelConnectionV1,
)
from openevo.deployment.profile import (
    ProxySettings,
    RemoteProfileConfig,
    SSHAuthConfig,
    load_remote_profile_config,
)
from openevo.deployment.planner import (
    SidecarSciencePlan,
    build_sidecar_science_plan,
    preflight_settings_for_project,
)
from openevo.deployment.workspace import (
    WorkspacePreparationAction,
    WorkspacePreparationPlan,
    plan_workspace_preparation,
)

__all__ = [
    "DesktopExecutionStatus",
    "BackendClient",
    "BackendConnection",
    "DesktopBackendError",
    "CoreClientErrorV1",
    "CoreClientLocalErrorCodeV1",
    "CoreClientLocalErrorV1",
    "CoreControlClientV1",
    "CoreSseStreamV1",
    "CoreTunnelConnectionV1",
    "DesktopProjectConfigDraft",
    "DesktopProjectConfigPaths",
    "DesktopProjectConfigSummary",
    "DesktopSidecarTransport",
    "OpenEvoDesktopBootstrapResponse",
    "OpenEvoDesktopProjectConfigResponse",
    "OpenEvoDesktopRunResponse",
    "OpenEvoDesktopRunStatus",
    "OpenEvoDesktopShellStatus",
    "OpenEvoDesktopWorkspaceResponse",
    "OpenEvoSidecarSession",
    "NativeSidecarInstance",
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
