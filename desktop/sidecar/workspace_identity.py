"""Deterministic private identities for native workspace imports."""

from __future__ import annotations

from hashlib import sha256
import re

from desktop.sidecar.contracts.v1 import WorkspaceImportRefV1
from desktop.sidecar.workspace_imports import WorkspaceImportOwnership


_ACTION_ID_RE = re.compile(r"^[^\x00-\x20\x7f](?:[^\x00-\x1f\x7f]*[^\x00-\x20\x7f])?$")
_IMPORT_ID_RE = re.compile(r"^workspace-import-[0-9a-f]{48}$")
_PROJECT_DOMAIN = b"openevo.desktop.native-project.v1\0"
_IMPORT_DOMAIN = b"openevo.desktop.native-import.v1\0"


def native_import_id_for_action(action_id: str) -> str:
    """Map one renderer action to one opaque private import identity."""

    if (
        type(action_id) is not str
        or not 16 <= len(action_id) <= 256
        or _ACTION_ID_RE.fullmatch(action_id) is None
    ):
        raise ValueError("native workspace action identity is invalid")
    digest = sha256(_IMPORT_DOMAIN + action_id.encode("utf-8")).hexdigest()
    return f"workspace-import-{digest[:48]}"


def project_id_for_native_import(import_id: str) -> str:
    """Derive the project identity used when a snapshot creates a new draft."""

    if type(import_id) is not str or _IMPORT_ID_RE.fullmatch(import_id) is None:
        raise ValueError("native workspace import identity is invalid")
    digest = sha256(_PROJECT_DOMAIN + import_id.encode("ascii")).hexdigest()
    return f"project-{digest[:48]}"


def ownership_for_native_import(
    import_ref: WorkspaceImportRefV1,
    *,
    project_id: str | None = None,
) -> WorkspaceImportOwnership:
    """Build the exact store ownership from public, non-secret identities."""

    if not isinstance(import_ref, WorkspaceImportRefV1):
        raise TypeError("native workspace ownership requires WorkspaceImportRefV1")
    owner = project_id or project_id_for_native_import(import_ref.import_id)
    operation_id = f"workspace-source-{import_ref.content_sha256}"
    return WorkspaceImportOwnership(
        project_id=owner,
        operation_id=operation_id,
        idempotency_key=operation_id,
    )


__all__ = (
    "native_import_id_for_action",
    "ownership_for_native_import",
    "project_id_for_native_import",
)
