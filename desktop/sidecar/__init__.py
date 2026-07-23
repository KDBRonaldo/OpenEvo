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
    CoreBootstrapTunnelConnectionV1,
    CoreClientErrorV1,
    CoreClientLocalErrorCodeV1,
    CoreClientLocalErrorV1,
    CoreControlClientV1,
    CoreProjectBootstrapClientV1,
    CoreProjectBootstrapResultV1,
    CoreSseStreamV1,
    CoreTunnelConnectionV1,
)
from desktop.sidecar.contracts.v1 import WorkspaceImportRefV1
from desktop.sidecar.release_app import create_release_desktop_local_api_app
from desktop.sidecar.release_provider import DesktopReleaseProvider
from desktop.sidecar.system_ssh_session import (
    AskpassHelperAuthority,
    SystemOpenSshSession,
    SystemOpenSshSessionError,
    SystemOpenSshSessionOwner,
    SystemOpenSshSessionSnapshot,
)
from desktop.sidecar.workspace_imports import (
    WorkspaceArchiveValidationError,
    WorkspaceImportError,
    WorkspaceImportIntegrityError,
    WorkspaceImportNotFoundError,
    WorkspaceImportStore,
    WorkspaceImportStoreConfigurationError,
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
    "DesktopReleaseProvider",
    "AskpassHelperAuthority",
    "BackendClient",
    "BackendConnection",
    "DesktopBackendError",
    "CoreBootstrapTunnelConnectionV1",
    "CoreClientErrorV1",
    "CoreClientLocalErrorCodeV1",
    "CoreClientLocalErrorV1",
    "CoreControlClientV1",
    "CoreProjectBootstrapClientV1",
    "CoreProjectBootstrapResultV1",
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
    "SystemOpenSshSession",
    "SystemOpenSshSessionError",
    "SystemOpenSshSessionOwner",
    "SystemOpenSshSessionSnapshot",
    "WorkspacePreparationAction",
    "WorkspacePreparationPlan",
    "WorkspaceArchiveValidationError",
    "WorkspaceImportError",
    "WorkspaceImportIntegrityError",
    "WorkspaceImportNotFoundError",
    "WorkspaceImportRefV1",
    "WorkspaceImportStore",
    "WorkspaceImportStoreConfigurationError",
    "build_desktop_project_configs",
    "build_sidecar_science_plan",
    "build_desktop_shell_status",
    "create_sidecar_app",
    "create_sidecar_app_for_project",
    "create_release_desktop_local_api_app",
    "default_desktop_shell_status",
    "list_desktop_project_configs",
    "load_desktop_project_config",
    "load_remote_profile_config",
    "plan_workspace_preparation",
    "preflight_settings_for_project",
    "save_desktop_project_config",
]
