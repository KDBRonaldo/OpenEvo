"""Desktop Local API v2 system-OpenSSH contract."""

from .app import desktop_local_v2_contract_app, create_desktop_local_v2_contract_app
from .canonical import (
    DESKTOP_EVENTS_SCHEMA_SHA256,
    DESKTOP_OPENAPI_SHA256,
    desktop_events_schema_document,
    desktop_openapi_document,
)
from .models import (
    LegacyExplicitProfileV2,
    RemoteWorkspaceProfileV2,
    SshHostCatalogV2,
    SshHostHintV2,
    SshPromptStateV2,
    SshTrustStateV2,
    SystemOpenSshProfileCreateV2,
)

__all__ = [
    "DESKTOP_EVENTS_SCHEMA_SHA256",
    "DESKTOP_OPENAPI_SHA256",
    "LegacyExplicitProfileV2",
    "RemoteWorkspaceProfileV2",
    "SshHostCatalogV2",
    "SshHostHintV2",
    "SshPromptStateV2",
    "SshTrustStateV2",
    "SystemOpenSshProfileCreateV2",
    "create_desktop_local_v2_contract_app",
    "desktop_events_schema_document",
    "desktop_local_v2_contract_app",
    "desktop_openapi_document",
]
