"""Isolated durable state for the Desktop Local API v2 provider.

The v2 store owns local system-OpenSSH profiles, non-connectable migration
records, validated local project drafts, bounded lifecycle operations/logs,
and their idempotency/migration receipts.  Remote Core authority is never
persisted here.
"""

from __future__ import annotations

import base64
import binascii
from collections.abc import Callable, Collection, Iterator, Mapping
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import fcntl
import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import secrets
import sqlite3
import stat
import threading
from typing import Annotated, Literal, TypeAlias, TypeVar, cast

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    TypeAdapter,
    ValidationError,
    model_validator,
)

from desktop.sidecar.contracts.v2 import models as m
from openevo.backend.contracts.v2.models import (
    ScienceProjectConfigV2,
    VersionResponseV2,
    project_config_sha256_for,
)
from openevo.deployment.host_keys import PendingSystemHostKeyReview


STORE_NAMESPACE = "openevo.desktop.provider.v2"
SCHEMA_VERSION = 3
DATABASE_FILENAME = "provider-v2.sqlite3"
JOURNAL_FILENAME = f"{DATABASE_FILENAME}-journal"
WAL_FILENAME = f"{DATABASE_FILENAME}-wal"
SHM_FILENAME = f"{DATABASE_FILENAME}-shm"
OWNER_LOCK_FILENAME = "provider-v2.lock"
LOCAL_PRINCIPAL = "desktop-local-v2"

MAX_DATABASE_BYTES = 67_108_864
MAX_JOURNAL_BYTES = 134_217_728
MAX_SCHEMA_OBJECTS = 32
MAX_SCHEMA_BYTES = 65_536
MAX_PROFILE_DOCUMENT_BYTES = 65_536
MAX_DRAFT_DOCUMENT_BYTES = 1_048_576
MAX_IDEMPOTENT_RESPONSE_BYTES = 1_048_576
MAX_RECOVERY_BYTES = 16_777_216
MAX_LIFECYCLE_REQUEST_BYTES = 1_048_576
MAX_LIFECYCLE_DOCUMENT_BYTES = 65_536
MAX_LIFECYCLE_LOG_ENTRY_BYTES = m.MAX_LIFECYCLE_LOG_ENTRY_BYTES
MAX_LIFECYCLE_LOG_ENTRIES = 4_096
MAX_LIFECYCLE_LOG_BYTES = 4 * 1_048_576
MAX_LIFECYCLE_GLOBAL_LOG_BYTES = 32 * 1_048_576
MAX_LIFECYCLE_AUTHORITY_RECOVERY_BYTES = 32 * 1_048_576
LIFECYCLE_TERMINAL_RETENTION = timedelta(days=7)
DEFAULT_MAX_PROFILES = 100
DEFAULT_MAX_DRAFTS = 100
DEFAULT_MAX_IDEMPOTENCY_RECORDS = 2_000
DEFAULT_MAX_MIGRATION_DIAGNOSTICS = 64
DEFAULT_MAX_LIFECYCLE_OPERATIONS = m.MAX_LIFECYCLE_OPERATION_COUNT

_TIMESTAMP_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{6}Z$")
_ETAG_RE = re.compile(r'^"[0-9a-f]{64}"$')
_DIGEST_RE = re.compile(r"^[0-9a-f]{64}$")
_PROFILE_ADAPTER = TypeAdapter(m.RemoteProfileV2)
_LIFECYCLE_RESOURCE_ADAPTER = TypeAdapter(m.LifecycleResourceRefV2)


class ProviderStoreV2Error(RuntimeError):
    """Base class for closed v2 persistence failures."""


class ProviderStateV2Error(ProviderStoreV2Error):
    """The private filesystem state cannot be proven safe."""


class ProviderSchemaV2Error(ProviderStoreV2Error):
    """The SQLite schema is absent, unsupported, or drifted."""


class ProviderDataV2Error(ProviderStoreV2Error):
    """Persisted data violates the closed v2 model or budgets."""


class ProviderCapacityV2Error(ProviderStoreV2Error):
    """A bounded provider resource is at capacity."""


class ProviderCapacityConfigurationV2Error(ProviderCapacityV2Error):
    """Configured capacity is below already-persisted usage."""


class ProviderIdempotencyConflictV2(ProviderStoreV2Error):
    """An idempotency key was reused for another canonical request."""


class ProviderPreconditionFailedV2(ProviderStoreV2Error):
    """A strong ETag no longer names the current local resource."""


class ProviderConflictV2(ProviderStoreV2Error):
    """A local migration or resource identity conflicts."""


class ProviderLifecycleResourceBusyV2(ProviderConflictV2):
    """A nonterminal lifecycle operation already owns the local resource."""

    def __init__(self, resource_id: str) -> None:
        super().__init__("local resource already has an active lifecycle operation")
        self.resource_id = resource_id


class ProviderNotFoundV2(ProviderStoreV2Error):
    """A local v2 resource does not exist."""


class ProviderContractV2Error(ProviderStoreV2Error):
    """Caller input is not an exact closed v2 model."""


class ProviderCursorExpiredV2(ProviderStoreV2Error):
    """A signed lifecycle log cursor names data outside the retained window."""


class _StrictModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        strict=True,
        validate_default=True,
    )


class LegacyProfileImportV2(_StrictModel):
    """Safe, path-free projection of one v1 explicit profile."""

    source_ref_sha256: m.Digest
    source_document_sha256: m.Digest
    display_name: m.DisplayName
    migration_state: Literal["rebind_required", "quarantined"]
    created_at: m.UtcTimestamp
    updated_at: m.UtcTimestamp


class LegacyDraftSourceV2(_StrictModel):
    """Opaque provenance needed before copying a validated v2 draft."""

    source_ref_sha256: m.Digest
    source_document_sha256: m.Digest
    display_name: m.DisplayName


class LocalProjectDraftV2(_StrictModel):
    schema_version: Literal["2"] = "2"
    draft_id: m.OpaqueId
    profile_id: m.OpaqueId
    display_name: m.DisplayName
    config: ScienceProjectConfigV2
    project_config_sha256: m.Digest
    legacy_source_ref_sha256: m.Digest
    legacy_source_document_sha256: m.Digest
    created_at: m.UtcTimestamp
    updated_at: m.UtcTimestamp
    etag: m.ETag

    @model_validator(mode="after")
    def _config_digest_matches(self) -> LocalProjectDraftV2:
        if project_config_sha256_for(self.config) != self.project_config_sha256:
            raise ValueError("draft config digest does not match canonical bytes")
        return self


MigrationDiagnosticCodeV2 = Literal[
    "legacy_store_oversized",
    "legacy_store_unsafe",
    "legacy_store_busy",
    "legacy_store_replaced",
    "legacy_schema_unsupported",
    "legacy_profile_corrupt",
    "legacy_profile_oversized",
    "legacy_project_corrupt",
    "legacy_project_oversized",
    "legacy_row_budget_exhausted",
    "legacy_source_changed",
]


class MigrationDiagnosticV2(_StrictModel):
    diagnostic_id: m.OpaqueId
    code: MigrationDiagnosticCodeV2
    source_kind: Literal["store", "profile", "project"]
    source_ref_sha256: m.Digest | None
    created_at: m.UtcTimestamp


class LifecycleProfileConnectRequestV2(_StrictModel):
    request_kind: Literal["profile_connect"]
    profile_id: m.OpaqueId
    request: m.ProfileConnectionActionV2
    resource_generation: int
    if_match: m.ETag


class LifecycleProfileDisconnectRequestV2(_StrictModel):
    request_kind: Literal["profile_disconnect"]
    profile_id: m.OpaqueId
    request: m.ProfileConnectionActionV2
    resource_generation: int
    if_match: m.ETag


class LifecycleHostKeyReviewRequestV2(_StrictModel):
    request_kind: Literal["host_key_review"]
    profile_id: m.OpaqueId
    request: m.HostKeyReviewRequestV2
    resource_generation: int
    if_match: m.ETag


class LifecycleNativeWorkspacePrepareRequestV2(_StrictModel):
    request_kind: Literal["native_workspace_prepare"]
    native_workspace_id: m.OpaqueId
    native_journal_sha256: m.Digest
    display_name: m.DisplayName


class LifecycleProjectCreateRequestV2(_StrictModel):
    request_kind: Literal["project_create"]
    project_id: m.OpaqueId
    action_id: Annotated[str, Field(min_length=16, max_length=256)]
    request: m.ProjectCreateV2
    resource_generation: int

    @model_validator(mode="after")
    def _validate_action_identity(self) -> LifecycleProjectCreateRequestV2:
        encoded = self.action_id.encode("utf-8")
        if (
            self.action_id != self.action_id.strip()
            or any(
                ord(character) < 0x20 or ord(character) == 0x7F
                for character in self.action_id
            )
            or not 16 <= len(encoded) <= 256
        ):
            raise ValueError("project-create action identity is invalid")
        return self


class LifecycleProjectActivateRequestV2(_StrictModel):
    request_kind: Literal["project_activate"]
    project_id: m.OpaqueId
    request: m.ProjectActionV2
    resource_generation: int
    if_match: m.ETag


LifecycleRequestV2: TypeAlias = Annotated[
    LifecycleProfileConnectRequestV2
    | LifecycleProfileDisconnectRequestV2
    | LifecycleHostKeyReviewRequestV2
    | LifecycleNativeWorkspacePrepareRequestV2
    | LifecycleProjectCreateRequestV2
    | LifecycleProjectActivateRequestV2,
    Field(discriminator="request_kind"),
]


class LifecycleOperationReservationV2(_StrictModel):
    kind: m.LifecycleOperationKindV2
    resource: m.LifecycleResourceRefV2
    request: LifecycleRequestV2

    @model_validator(mode="after")
    def _bind_request_identity(self) -> LifecycleOperationReservationV2:
        if self.kind != self.request.request_kind:
            raise ValueError("lifecycle reservation kind differs from its request")
        if self.resource.resource_kind == "profile":
            request_id = getattr(self.request, "profile_id", None)
        elif self.resource.resource_kind == "project":
            request_id = getattr(self.request, "project_id", None)
        else:
            request_id = getattr(self.request, "native_workspace_id", None)
        if request_id != self.resource.resource_id:
            raise ValueError("lifecycle request belongs to another resource")
        expected_resource_kind = {
            "profile_connect": "profile",
            "profile_disconnect": "profile",
            "host_key_review": "profile",
            "native_workspace_prepare": "native_workspace",
            "project_create": "project",
            "project_activate": "project",
        }[self.kind]
        if self.resource.resource_kind != expected_resource_kind:
            raise ValueError("lifecycle operation kind and resource kind differ")
        generation = getattr(self.request, "resource_generation", None)
        if generation is not None and (
            type(generation) is not int or not 0 <= generation <= m.MAX_JAVASCRIPT_SAFE_INTEGER
        ):
            raise ValueError("lifecycle request generation is outside bounds")
        if isinstance(self.request, LifecycleProjectCreateRequestV2):
            if (
                self.request.request.profile_connection_generation
                != self.request.resource_generation
            ):
                raise ValueError("project create generations differ")
        return self


class LifecycleOperationWorkV2(_StrictModel):
    operation: m.LifecycleOperationV2
    request: LifecycleRequestV2
    idempotency_key: Annotated[str, Field(min_length=16, max_length=256)]
    cancellation_requested: bool


class LifecycleOperationAdvanceV2(_StrictModel):
    operation_id: m.OpaqueId
    expected_etag: m.ETag
    phase: m.LifecyclePhaseV2
    progress: m.LifecycleProgressV2 | None
    cancellable: bool


class LifecycleLogAppendV2(_StrictModel):
    operation_id: m.OpaqueId
    source: Literal[
        "desktop",
        "ssh_stdout",
        "ssh_stderr",
        "daemon_stdout",
        "daemon_stderr",
    ]
    text: str
    truncated: bool

    @model_validator(mode="after")
    def _bounded_input(self) -> LifecycleLogAppendV2:
        if not self.text or len(self.text.encode("utf-8")) > MAX_LIFECYCLE_REQUEST_BYTES:
            raise ValueError("lifecycle log append input is outside its byte bound")
        return self


class LifecycleOperationCompletionV2(_StrictModel):
    operation_id: m.OpaqueId
    expected_etag: m.ETag
    status: Literal["succeeded", "failed", "cancelled"]
    result: m.LifecycleResultV2 | None
    failure: m.DesktopErrorV2 | None

    @model_validator(mode="after")
    def _terminal_shape(self) -> LifecycleOperationCompletionV2:
        if self.status == "succeeded":
            if self.result is None or self.failure is not None:
                raise ValueError("successful lifecycle completion requires only a result")
        elif self.status == "failed":
            if self.failure is None or self.result is not None:
                raise ValueError("failed lifecycle completion requires only a failure")
        elif self.result is not None or self.failure is not None:
            raise ValueError("cancelled lifecycle completion has no result or failure")
        return self


_LIFECYCLE_REQUEST_ADAPTER = TypeAdapter(LifecycleRequestV2)
_LIFECYCLE_PROGRESS_ADAPTER = TypeAdapter(m.LifecycleProgressV2 | None)
_LIFECYCLE_RESULT_ADAPTER = TypeAdapter(m.LifecycleResultV2 | None)


_SCHEMA_V1_STATEMENTS = (
    """
    CREATE TABLE schema_metadata (
        singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
        namespace TEXT NOT NULL CHECK (namespace = 'openevo.desktop.provider.v2'),
        schema_version INTEGER NOT NULL CHECK (schema_version BETWEEN 1 AND 2),
        schema_sha256 TEXT NOT NULL CHECK (length(schema_sha256) = 64),
        created_at TEXT NOT NULL CHECK (length(CAST(created_at AS BLOB)) = 27)
    ) STRICT
    """,
    """
    CREATE TABLE schema_migrations (
        version INTEGER PRIMARY KEY CHECK (version BETWEEN 1 AND 2),
        applied_at TEXT NOT NULL CHECK (length(CAST(applied_at AS BLOB)) = 27)
    ) STRICT
    """,
    f"""
    CREATE TABLE profiles (
        profile_id TEXT PRIMARY KEY
            CHECK (length(CAST(profile_id AS BLOB)) BETWEEN 1 AND 128),
        profile_kind TEXT NOT NULL CHECK (profile_kind IN ('system_openssh', 'legacy_explicit')),
        document_json BLOB NOT NULL
            CHECK (length(document_json) BETWEEN 2 AND {MAX_PROFILE_DOCUMENT_BYTES}),
        resource_version INTEGER NOT NULL CHECK (resource_version >= 1),
        legacy_source_ref_sha256 TEXT UNIQUE,
        legacy_source_document_sha256 TEXT,
        rebound_from_sha256 TEXT,
        created_at TEXT NOT NULL CHECK (length(CAST(created_at AS BLOB)) = 27),
        updated_at TEXT NOT NULL CHECK (length(CAST(updated_at AS BLOB)) = 27),
        CHECK (legacy_source_ref_sha256 IS NULL OR length(legacy_source_ref_sha256) = 64),
        CHECK (
            legacy_source_document_sha256 IS NULL OR
            length(legacy_source_document_sha256) = 64
        ),
        CHECK (rebound_from_sha256 IS NULL OR length(rebound_from_sha256) = 64),
        CHECK (
            (profile_kind = 'legacy_explicit' AND
             legacy_source_ref_sha256 IS NOT NULL AND
             legacy_source_document_sha256 IS NOT NULL AND
             rebound_from_sha256 IS NULL) OR
            (profile_kind = 'system_openssh' AND
             legacy_source_ref_sha256 IS NULL AND
             legacy_source_document_sha256 IS NULL)
        )
    ) STRICT
    """,
    f"""
    CREATE TABLE idempotency_records (
        principal TEXT NOT NULL CHECK (principal = 'desktop-local-v2'),
        operation TEXT NOT NULL CHECK (length(CAST(operation AS BLOB)) BETWEEN 1 AND 128),
        resource_scope TEXT NOT NULL
            CHECK (length(CAST(resource_scope AS BLOB)) BETWEEN 1 AND 128),
        idempotency_key TEXT NOT NULL
            CHECK (length(CAST(idempotency_key AS BLOB)) BETWEEN 16 AND 256),
        request_sha256 TEXT NOT NULL CHECK (length(request_sha256) = 64),
        response_kind TEXT NOT NULL CHECK (response_kind IN ('profile', 'draft')),
        response_resource_version INTEGER NOT NULL
            CHECK (response_resource_version BETWEEN 1 AND 9007199254740991),
        response_json BLOB NOT NULL
            CHECK (length(response_json) BETWEEN 2 AND {MAX_IDEMPOTENT_RESPONSE_BYTES}),
        created_at TEXT NOT NULL CHECK (length(CAST(created_at AS BLOB)) = 27),
        PRIMARY KEY (principal, operation, resource_scope, idempotency_key)
    ) STRICT
    """,
    "CREATE INDEX profiles_updated_idx ON profiles(updated_at, profile_id)",
    """
    CREATE UNIQUE INDEX profiles_rebound_source_idx
    ON profiles(rebound_from_sha256)
    WHERE rebound_from_sha256 IS NOT NULL
    """,
)

_SCHEMA_V2_ADDITIONS = (
    f"""
    CREATE TABLE project_drafts (
        draft_id TEXT PRIMARY KEY
            CHECK (length(CAST(draft_id AS BLOB)) BETWEEN 1 AND 128),
        profile_id TEXT NOT NULL
            CHECK (length(CAST(profile_id AS BLOB)) BETWEEN 1 AND 128),
        document_json BLOB NOT NULL
            CHECK (length(document_json) BETWEEN 2 AND {MAX_DRAFT_DOCUMENT_BYTES}),
        config_json BLOB NOT NULL
            CHECK (length(config_json) BETWEEN 2 AND {MAX_DRAFT_DOCUMENT_BYTES}),
        project_config_sha256 TEXT NOT NULL CHECK (length(project_config_sha256) = 64),
        legacy_source_ref_sha256 TEXT NOT NULL UNIQUE
            CHECK (length(legacy_source_ref_sha256) = 64),
        legacy_source_document_sha256 TEXT NOT NULL
            CHECK (length(legacy_source_document_sha256) = 64),
        resource_version INTEGER NOT NULL CHECK (resource_version >= 1),
        created_at TEXT NOT NULL CHECK (length(CAST(created_at AS BLOB)) = 27),
        updated_at TEXT NOT NULL CHECK (length(CAST(updated_at AS BLOB)) = 27),
        FOREIGN KEY (profile_id) REFERENCES profiles(profile_id) ON DELETE RESTRICT
    ) STRICT
    """,
    "CREATE INDEX project_drafts_profile_idx ON project_drafts(profile_id, draft_id)",
    """
    CREATE TABLE migration_diagnostics (
        diagnostic_id TEXT PRIMARY KEY
            CHECK (length(CAST(diagnostic_id AS BLOB)) BETWEEN 1 AND 128),
        code TEXT NOT NULL CHECK (length(CAST(code AS BLOB)) BETWEEN 1 AND 128),
        source_kind TEXT NOT NULL CHECK (source_kind IN ('store', 'profile', 'project')),
        source_ref_sha256 TEXT,
        created_at TEXT NOT NULL CHECK (length(CAST(created_at AS BLOB)) = 27),
        CHECK (source_ref_sha256 IS NULL OR length(source_ref_sha256) = 64)
    ) STRICT
    """,
    """
    CREATE UNIQUE INDEX migration_diagnostics_identity_idx
    ON migration_diagnostics(code, source_kind, coalesce(source_ref_sha256, ''))
    """,
)

_SCHEMA_V3_ADDITIONS = (
    "ALTER TABLE schema_metadata RENAME TO schema_metadata_before_v3",
    """
    CREATE TABLE schema_metadata (
        singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
        namespace TEXT NOT NULL CHECK (namespace = 'openevo.desktop.provider.v2'),
        schema_version INTEGER NOT NULL CHECK (schema_version BETWEEN 1 AND 3),
        schema_sha256 TEXT NOT NULL CHECK (length(schema_sha256) = 64),
        created_at TEXT NOT NULL CHECK (length(CAST(created_at AS BLOB)) = 27)
    ) STRICT
    """,
    """
    INSERT INTO schema_metadata(singleton, namespace, schema_version, schema_sha256, created_at)
    SELECT singleton, namespace, schema_version, schema_sha256, created_at
    FROM schema_metadata_before_v3
    """,
    "DROP TABLE schema_metadata_before_v3",
    "ALTER TABLE schema_migrations RENAME TO schema_migrations_before_v3",
    """
    CREATE TABLE schema_migrations (
        version INTEGER PRIMARY KEY CHECK (version BETWEEN 1 AND 3),
        applied_at TEXT NOT NULL CHECK (length(CAST(applied_at AS BLOB)) = 27)
    ) STRICT
    """,
    """
    INSERT INTO schema_migrations(version, applied_at)
    SELECT version, applied_at FROM schema_migrations_before_v3
    """,
    "DROP TABLE schema_migrations_before_v3",
    f"""
    CREATE TABLE lifecycle_operations (
        operation_id TEXT PRIMARY KEY
            CHECK (length(CAST(operation_id AS BLOB)) BETWEEN 1 AND 128),
        kind TEXT NOT NULL CHECK (kind IN (
            'profile_connect', 'profile_disconnect', 'host_key_review',
            'native_workspace_prepare', 'project_create', 'project_activate'
        )),
        resource_kind TEXT NOT NULL
            CHECK (resource_kind IN ('profile', 'native_workspace', 'project')),
        resource_id TEXT NOT NULL
            CHECK (length(CAST(resource_id AS BLOB)) BETWEEN 1 AND 128),
        request_sha256 TEXT NOT NULL CHECK (length(request_sha256) = 64),
        request_json BLOB NOT NULL
            CHECK (length(request_json) BETWEEN 2 AND {MAX_LIFECYCLE_REQUEST_BYTES}),
        phase_plan_json BLOB NOT NULL
            CHECK (length(phase_plan_json) BETWEEN 2 AND {MAX_LIFECYCLE_DOCUMENT_BYTES}),
        status TEXT NOT NULL
            CHECK (status IN ('queued', 'running', 'succeeded', 'failed', 'cancelled')),
        phase TEXT NOT NULL CHECK (phase IN (
            'validation', 'queued', 'resolving_system_openssh', 'connecting',
            'waiting_for_user', 'remote_preflight', 'transferring', 'verifying',
            'starting_daemon', 'waiting_for_daemon', 'opening_project_tunnel',
            'negotiating_core', 'preparing_native_workspace',
            'creating_remote_project', 'verifying_project', 'activating', 'finalizing'
        )),
        phase_index INTEGER NOT NULL CHECK (phase_index BETWEEN 0 AND 16),
        phase_total INTEGER NOT NULL CHECK (phase_total = 17),
        progress_json BLOB NOT NULL
            CHECK (length(progress_json) BETWEEN 2 AND {MAX_LIFECYCLE_DOCUMENT_BYTES}),
        cancellable INTEGER NOT NULL CHECK (cancellable IN (0, 1)),
        result_json BLOB
            CHECK (result_json IS NULL OR length(result_json) BETWEEN 2 AND {MAX_LIFECYCLE_DOCUMENT_BYTES}),
        failure_json BLOB
            CHECK (failure_json IS NULL OR length(failure_json) BETWEEN 2 AND {MAX_LIFECYCLE_DOCUMENT_BYTES}),
        log_sequence_high_watermark INTEGER NOT NULL
            CHECK (log_sequence_high_watermark BETWEEN 0 AND 9007199254740991),
        dropped_before_sequence INTEGER NOT NULL
            CHECK (dropped_before_sequence BETWEEN 0 AND log_sequence_high_watermark),
        log_byte_count INTEGER NOT NULL CHECK (log_byte_count >= 0),
        cancellation_requested INTEGER NOT NULL CHECK (cancellation_requested IN (0, 1)),
        resource_version INTEGER NOT NULL
            CHECK (resource_version BETWEEN 1 AND 9007199254740991),
        created_at TEXT NOT NULL CHECK (length(CAST(created_at AS BLOB)) = 27),
        started_at TEXT CHECK (
            started_at IS NULL OR length(CAST(started_at AS BLOB)) = 27
        ),
        updated_at TEXT NOT NULL CHECK (length(CAST(updated_at AS BLOB)) = 27),
        finished_at TEXT CHECK (
            finished_at IS NULL OR length(CAST(finished_at AS BLOB)) = 27
        ),
        etag TEXT NOT NULL CHECK (
            length(CAST(etag AS BLOB)) = 66 AND substr(etag, 1, 1) = '"' AND
            substr(etag, 66, 1) = '"'
        ),
        CHECK (
            (status = 'failed' AND failure_json IS NOT NULL AND result_json IS NULL) OR
            (status = 'succeeded' AND result_json IS NOT NULL AND failure_json IS NULL) OR
            (status IN ('queued', 'running', 'cancelled') AND
             result_json IS NULL AND failure_json IS NULL)
        ),
        CHECK ((status IN ('succeeded', 'failed', 'cancelled')) = (finished_at IS NOT NULL)),
        CHECK (status NOT IN ('succeeded', 'failed', 'cancelled') OR cancellable = 0)
    ) STRICT
    """,
    """
    CREATE INDEX lifecycle_operations_pending_idx
    ON lifecycle_operations(status, created_at, operation_id)
    """,
    f"""
    CREATE TABLE lifecycle_operation_logs (
        operation_id TEXT NOT NULL,
        sequence INTEGER NOT NULL CHECK (sequence BETWEEN 1 AND 9007199254740991),
        occurred_at TEXT NOT NULL CHECK (length(CAST(occurred_at AS BLOB)) = 27),
        source TEXT NOT NULL CHECK (source IN (
            'desktop', 'ssh_stdout', 'ssh_stderr', 'daemon_stdout', 'daemon_stderr'
        )),
        text BLOB NOT NULL CHECK (length(text) BETWEEN 1 AND {MAX_LIFECYCLE_LOG_ENTRY_BYTES}),
        text_bytes INTEGER NOT NULL CHECK (
            text_bytes BETWEEN 1 AND {MAX_LIFECYCLE_LOG_ENTRY_BYTES} AND
            text_bytes = length(text)
        ),
        truncated INTEGER NOT NULL CHECK (truncated IN (0, 1)),
        PRIMARY KEY (operation_id, sequence),
        FOREIGN KEY (operation_id) REFERENCES lifecycle_operations(operation_id)
            ON DELETE CASCADE
    ) STRICT
    """,
    """
    CREATE INDEX lifecycle_operation_logs_time_idx
    ON lifecycle_operation_logs(occurred_at, operation_id, sequence)
    """,
    """
    CREATE TABLE lifecycle_idempotency_records (
        principal TEXT NOT NULL CHECK (principal = 'desktop-local-v2'),
        action TEXT NOT NULL CHECK (action IN ('reserve', 'cancel')),
        resource_scope TEXT NOT NULL
            CHECK (length(CAST(resource_scope AS BLOB)) BETWEEN 1 AND 128),
        idempotency_key TEXT NOT NULL
            CHECK (length(CAST(idempotency_key AS BLOB)) BETWEEN 16 AND 256),
        request_sha256 TEXT NOT NULL CHECK (length(request_sha256) = 64),
        operation_id TEXT NOT NULL,
        created_at TEXT NOT NULL CHECK (length(CAST(created_at AS BLOB)) = 27),
        PRIMARY KEY (principal, action, resource_scope, idempotency_key),
        FOREIGN KEY (operation_id) REFERENCES lifecycle_operations(operation_id)
            ON DELETE CASCADE
    ) STRICT
    """,
    """
    CREATE UNIQUE INDEX lifecycle_reservation_operation_idx
    ON lifecycle_idempotency_records(operation_id)
    WHERE action = 'reserve'
    """,
    """
    CREATE TABLE lifecycle_reconciliation_acknowledgements (
        operation_id TEXT PRIMARY KEY,
        terminal_status TEXT NOT NULL
            CHECK (terminal_status IN ('succeeded', 'failed', 'cancelled')),
        terminal_etag TEXT NOT NULL CHECK (length(CAST(terminal_etag AS BLOB)) = 66),
        request_sha256 TEXT NOT NULL CHECK (length(request_sha256) = 64),
        idempotency_key TEXT NOT NULL
            CHECK (length(CAST(idempotency_key AS BLOB)) BETWEEN 16 AND 256),
        acknowledged_at TEXT NOT NULL CHECK (length(CAST(acknowledged_at AS BLOB)) = 27),
        FOREIGN KEY (operation_id) REFERENCES lifecycle_operations(operation_id)
            ON DELETE CASCADE
    ) STRICT
    """,
    """
    CREATE INDEX lifecycle_acknowledgements_time_idx
    ON lifecycle_reconciliation_acknowledgements(acknowledged_at, operation_id)
    """,
    """
    CREATE TABLE lifecycle_cursor_key (
        singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
        cursor_key BLOB NOT NULL CHECK (length(cursor_key) = 32),
        created_at TEXT NOT NULL CHECK (length(CAST(created_at AS BLOB)) = 27)
    ) STRICT
    """,
)


def _canonical_json_bytes(value: object) -> bytes:
    if isinstance(value, BaseModel):
        value = value.model_dump(mode="json")
    try:
        return json.dumps(
            value,
            ensure_ascii=True,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise ProviderContractV2Error("value is not canonical JSON data") from exc


def _restart_profile_reconnect_key(
    parent_operation_id: str,
    invalidated_connection_generation: int,
) -> str:
    digest = hashlib.sha256(
        b"openevo-desktop-restart-profile-reconnect-v2\0"
        + parent_operation_id.encode("utf-8", errors="strict")
        + b"\0"
        + str(invalidated_connection_generation).encode("ascii")
    ).hexdigest()
    return f"desktop-v2-{digest}"


def _schema_rows(connection: sqlite3.Connection) -> tuple[tuple[object, ...], ...]:
    row = connection.execute(
        """
        SELECT count(*), coalesce(sum(
            length(CAST(type AS BLOB)) + length(CAST(name AS BLOB)) +
            length(CAST(tbl_name AS BLOB)) + coalesce(length(CAST(sql AS BLOB)), 0)
        ), 0)
        FROM sqlite_schema
        WHERE name NOT LIKE 'sqlite_%'
        """
    ).fetchone()
    assert row is not None
    count, byte_count = cast(tuple[int, int], row)
    if count > MAX_SCHEMA_OBJECTS or byte_count > MAX_SCHEMA_BYTES:
        raise ProviderSchemaV2Error("v2 provider schema exceeds fingerprint bounds")
    return tuple(
        tuple(item)
        for item in connection.execute(
            """
            SELECT type, name, tbl_name, sql
            FROM sqlite_schema
            WHERE name NOT LIKE 'sqlite_%'
            ORDER BY type, name, tbl_name
            """
        )
    )


def _expected_schema(
    statements: tuple[str, ...],
) -> tuple[tuple[tuple[object, ...], ...], str]:
    connection = sqlite3.connect(":memory:")
    try:
        for statement in statements:
            connection.execute(statement)
        rows = _schema_rows(connection)
    finally:
        connection.close()
    return rows, hashlib.sha256(_canonical_json_bytes(rows)).hexdigest()


_EXPECTED_SCHEMA_V1_ROWS, _COMPUTED_SCHEMA_V1_SHA256 = _expected_schema(_SCHEMA_V1_STATEMENTS)
_EXPECTED_SCHEMA_V2_ROWS, _COMPUTED_SCHEMA_V2_SHA256 = _expected_schema(
    (*_SCHEMA_V1_STATEMENTS, *_SCHEMA_V2_ADDITIONS)
)
_EXPECTED_SCHEMA_V3_ROWS, _COMPUTED_SCHEMA_V3_SHA256 = _expected_schema(
    (*_SCHEMA_V1_STATEMENTS, *_SCHEMA_V2_ADDITIONS, *_SCHEMA_V3_ADDITIONS)
)
EXPECTED_SCHEMA_V1_SHA256 = "d2ae490ad5b98ca03548570a8d56a6a5ea349694ed647102a69eb5b69e3dac34"
EXPECTED_SCHEMA_V2_SHA256 = "7314032a52da83b70a43f36f161984bef8bf03274848bf62ab1963a039279c06"
EXPECTED_SCHEMA_V3_SHA256 = "fa2284e9374ed21bdeaa318565c81692314430ce9a1bd43251bccb886c31c5c6"
if (
    _COMPUTED_SCHEMA_V1_SHA256 != EXPECTED_SCHEMA_V1_SHA256
    or _COMPUTED_SCHEMA_V2_SHA256 != EXPECTED_SCHEMA_V2_SHA256
    or _COMPUTED_SCHEMA_V3_SHA256 != EXPECTED_SCHEMA_V3_SHA256
):
    raise RuntimeError("Desktop provider v2 schema changed without a migration")

_PROFILE_SELECT_COLUMNS = f"""
    rowid, profile_id, profile_kind,
    CASE WHEN length(CAST(document_json AS BLOB))
                   BETWEEN 2 AND {MAX_PROFILE_DOCUMENT_BYTES}
         THEN document_json END AS document_json,
    length(CAST(document_json AS BLOB)) AS document_json_bytes,
    resource_version, legacy_source_ref_sha256,
    legacy_source_document_sha256, rebound_from_sha256,
    created_at, updated_at
"""
_DRAFT_SELECT_COLUMNS = f"""
    rowid, draft_id, profile_id,
    CASE WHEN length(CAST(document_json AS BLOB))
                   BETWEEN 2 AND {MAX_DRAFT_DOCUMENT_BYTES}
         THEN document_json END AS document_json,
    length(CAST(document_json AS BLOB)) AS document_json_bytes,
    CASE WHEN length(CAST(config_json AS BLOB))
                   BETWEEN 2 AND {MAX_DRAFT_DOCUMENT_BYTES}
         THEN config_json END AS config_json,
    length(CAST(config_json AS BLOB)) AS config_json_bytes,
    project_config_sha256, legacy_source_ref_sha256,
    legacy_source_document_sha256, resource_version,
    created_at, updated_at
"""
_IDEMPOTENCY_SELECT_COLUMNS = f"""
    rowid, principal, operation, resource_scope, idempotency_key,
    request_sha256, response_kind, response_resource_version,
    CASE WHEN length(CAST(response_json AS BLOB))
                   BETWEEN 2 AND {MAX_IDEMPOTENT_RESPONSE_BYTES}
         THEN response_json END AS response_json,
    length(CAST(response_json AS BLOB)) AS response_json_bytes,
    created_at
"""
_LIFECYCLE_OPERATION_SELECT_COLUMNS = f"""
    rowid, operation_id, kind, resource_kind, resource_id, request_sha256,
    CASE WHEN length(CAST(request_json AS BLOB))
                   BETWEEN 2 AND {MAX_LIFECYCLE_REQUEST_BYTES}
         THEN request_json END AS request_json,
    length(CAST(request_json AS BLOB)) AS request_json_bytes,
    CASE WHEN length(CAST(phase_plan_json AS BLOB))
                   BETWEEN 2 AND {MAX_LIFECYCLE_DOCUMENT_BYTES}
         THEN phase_plan_json END AS phase_plan_json,
    length(CAST(phase_plan_json AS BLOB)) AS phase_plan_json_bytes,
    status, phase, phase_index, phase_total,
    CASE WHEN length(CAST(progress_json AS BLOB))
                   BETWEEN 2 AND {MAX_LIFECYCLE_DOCUMENT_BYTES}
         THEN progress_json END AS progress_json,
    length(CAST(progress_json AS BLOB)) AS progress_json_bytes,
    cancellable,
    CASE WHEN result_json IS NULL THEN NULL
         WHEN length(CAST(result_json AS BLOB))
                   BETWEEN 2 AND {MAX_LIFECYCLE_DOCUMENT_BYTES}
         THEN result_json END AS result_json,
    CASE WHEN result_json IS NULL THEN 0
         ELSE length(CAST(result_json AS BLOB)) END AS result_json_bytes,
    CASE WHEN failure_json IS NULL THEN NULL
         WHEN length(CAST(failure_json AS BLOB))
                   BETWEEN 2 AND {MAX_LIFECYCLE_DOCUMENT_BYTES}
         THEN failure_json END AS failure_json,
    CASE WHEN failure_json IS NULL THEN 0
         ELSE length(CAST(failure_json AS BLOB)) END AS failure_json_bytes,
    log_sequence_high_watermark, dropped_before_sequence, log_byte_count,
    cancellation_requested, resource_version, created_at, started_at,
    updated_at, finished_at, etag
"""
_LIFECYCLE_LOG_SELECT_COLUMNS = f"""
    operation_id, sequence, occurred_at, source,
    CASE WHEN length(CAST(text AS BLOB))
                   BETWEEN 1 AND {MAX_LIFECYCLE_LOG_ENTRY_BYTES}
         THEN text END AS text,
    length(CAST(text AS BLOB)) AS text_bytes_actual,
    text_bytes, truncated
"""


def _migration_checkpoint(_stage: str) -> None:
    """Private crash-injection boundary used by durability tests."""


def _post_commit_checkpoint(_operation: str) -> None:
    """Private process-loss boundary after an authoritative commit."""


def _lifecycle_reservation_checkpoint(_stage: str) -> None:
    """Private crash-injection boundary for atomic lifecycle reservation tests."""


_ModelT = TypeVar("_ModelT", bound=BaseModel)


class DesktopProviderStoreV2:
    """Owner-private, schema-fingerprinted local v2 provider persistence."""

    def __init__(
        self,
        state_root: Path | str,
        *,
        clock: Callable[[], datetime] | None = None,
        max_profiles: int = DEFAULT_MAX_PROFILES,
        max_drafts: int = DEFAULT_MAX_DRAFTS,
        max_idempotency_records: int = DEFAULT_MAX_IDEMPOTENCY_RECORDS,
        max_migration_diagnostics: int = DEFAULT_MAX_MIGRATION_DIAGNOSTICS,
        max_lifecycle_operations: int = DEFAULT_MAX_LIFECYCLE_OPERATIONS,
        max_lifecycle_log_entries: int = MAX_LIFECYCLE_LOG_ENTRIES,
        max_lifecycle_log_bytes: int = MAX_LIFECYCLE_LOG_BYTES,
        max_lifecycle_global_log_bytes: int = MAX_LIFECYCLE_GLOBAL_LOG_BYTES,
    ) -> None:
        self._require_secure_platform()
        for label, value in (
            ("max_profiles", max_profiles),
            ("max_drafts", max_drafts),
            ("max_idempotency_records", max_idempotency_records),
            ("max_migration_diagnostics", max_migration_diagnostics),
            ("max_lifecycle_operations", max_lifecycle_operations),
            ("max_lifecycle_log_entries", max_lifecycle_log_entries),
            ("max_lifecycle_log_bytes", max_lifecycle_log_bytes),
            ("max_lifecycle_global_log_bytes", max_lifecycle_global_log_bytes),
        ):
            if type(value) is not int or value < 1:
                raise ValueError(f"{label} must be a positive integer")
        if max_profiles > DEFAULT_MAX_PROFILES:
            raise ValueError("max_profiles exceeds the public v2 profile bound")
        if max_lifecycle_operations > DEFAULT_MAX_LIFECYCLE_OPERATIONS:
            raise ValueError("max_lifecycle_operations exceeds the public v2 bound")
        if max_lifecycle_log_entries > MAX_LIFECYCLE_LOG_ENTRIES:
            raise ValueError("max_lifecycle_log_entries exceeds the public v2 bound")
        if max_lifecycle_log_bytes > MAX_LIFECYCLE_LOG_BYTES:
            raise ValueError("max_lifecycle_log_bytes exceeds the public v2 bound")
        if max_lifecycle_global_log_bytes > MAX_LIFECYCLE_GLOBAL_LOG_BYTES:
            raise ValueError("max_lifecycle_global_log_bytes exceeds the public v2 bound")
        if max_lifecycle_global_log_bytes < max_lifecycle_log_bytes:
            raise ValueError("global lifecycle log capacity is below per-operation capacity")

        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._max_profiles = max_profiles
        self._max_drafts = max_drafts
        self._max_idempotency_records = max_idempotency_records
        self._max_migration_diagnostics = max_migration_diagnostics
        self._max_lifecycle_operations = max_lifecycle_operations
        self._max_lifecycle_log_entries = max_lifecycle_log_entries
        self._max_lifecycle_log_bytes = max_lifecycle_log_bytes
        self._max_lifecycle_global_log_bytes = max_lifecycle_global_log_bytes
        self._closed = False
        self._lock = threading.RLock()
        root = Path(os.path.abspath(os.fspath(Path(state_root).expanduser())))
        self._create_or_validate_root(root)
        self._state_root = root
        flags = os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW
        try:
            self._root_fd = os.open(root, flags)
        except OSError as exc:
            raise ProviderStateV2Error("v2 provider root could not be opened") from exc
        root_stat = os.fstat(self._root_fd)
        self._root_identity = (root_stat.st_dev, root_stat.st_ino)
        try:
            self._ensure_private_file(OWNER_LOCK_FILENAME)
            self._acquire_owner_lock()
            self._ensure_private_file(DATABASE_FILENAME)
            database_stat = self._verify_private_file(DATABASE_FILENAME)
            self._database_identity = (database_stat.st_dev, database_stat.st_ino)
            self._verify_storage_files()
            self._connection = self._open_database()
            self._migrate()
            self._recover_and_validate()
            self._verify_storage_files()
        except BaseException:
            self._close_resources()
            raise

    @property
    def state_root(self) -> Path:
        return self._state_root

    @property
    def database_path(self) -> Path:
        return self._state_root / DATABASE_FILENAME

    @property
    def schema_fingerprint(self) -> str:
        return EXPECTED_SCHEMA_V3_SHA256

    def close(self) -> None:
        with self._lock:
            if not self._closed:
                self._close_resources()

    def __enter__(self) -> DesktopProviderStoreV2:
        self._verify_storage_files()
        return self

    def __exit__(self, *args: object) -> None:
        del args
        self.close()

    def __del__(self) -> None:
        if getattr(self, "_closed", True) is False:
            try:
                self.close()
            except OSError:
                pass

    def reserve_lifecycle_operation(
        self,
        request: LifecycleOperationReservationV2 | Mapping[str, object],
        *,
        idempotency_key: str,
    ) -> m.LifecycleOperationV2:
        """Atomically reserve one closed long operation and its local authority."""

        validated = self._validate_model(LifecycleOperationReservationV2, request)
        with self._transaction(
            write=True,
            operation="reserveLifecycleOperationV2",
        ) as connection:
            return self._reserve_lifecycle_operation_in_transaction(
                connection,
                validated,
                idempotency_key=idempotency_key,
            )

    def _reserve_lifecycle_operation_in_transaction(
        self,
        connection: sqlite3.Connection,
        request: LifecycleOperationReservationV2,
        *,
        idempotency_key: str,
    ) -> m.LifecycleOperationV2:
        """Reserve closed lifecycle authority inside an existing write transaction."""

        self._validate_idempotency_key(idempotency_key)
        request_document = _canonical_json_bytes(request.request)
        if len(request_document) > MAX_LIFECYCLE_REQUEST_BYTES:
            raise ProviderCapacityV2Error("lifecycle request exceeds its byte bound")
        request_sha256 = hashlib.sha256(_canonical_json_bytes(request)).hexdigest()
        resource_scope = "lifecycle_operations"
        replay = connection.execute(
            """
            SELECT request_sha256, operation_id
            FROM lifecycle_idempotency_records
            WHERE principal = ? AND action = 'reserve'
              AND resource_scope = ? AND idempotency_key = ?
            """,
            (LOCAL_PRINCIPAL, resource_scope, idempotency_key),
        ).fetchone()
        if replay is not None:
            if not hmac.compare_digest(replay["request_sha256"], request_sha256):
                raise ProviderIdempotencyConflictV2(
                    "lifecycle idempotency key was reused for another request"
                )
            return self._lifecycle_operation_from_row(
                self._require_lifecycle_operation_row(
                    connection,
                    cast(str, replay["operation_id"]),
                )
            )

        recoverable_count = cast(
            int,
            connection.execute(
                """
                SELECT count(*)
                FROM lifecycle_operations AS operation
                LEFT JOIN lifecycle_reconciliation_acknowledgements AS acknowledgement
                  ON acknowledgement.operation_id = operation.operation_id
                WHERE operation.status IN ('queued', 'running')
                   OR acknowledgement.operation_id IS NULL
                """
            ).fetchone()[0],
        )
        if recoverable_count >= self._max_lifecycle_operations:
            raise ProviderCapacityV2Error("lifecycle operation capacity is full")

        active_resource = connection.execute(
            """
            SELECT operation_id
            FROM lifecycle_operations
            WHERE resource_kind = ? AND resource_id = ?
              AND status IN ('queued', 'running')
            LIMIT 1
            """,
            (
                request.resource.resource_kind,
                request.resource.resource_id,
            ),
        ).fetchone()
        if active_resource is not None:
            raise ProviderLifecycleResourceBusyV2(request.resource.resource_id)

        self._require_lifecycle_dependencies_available(connection, request)

        if request.kind in {
            "profile_connect",
            "profile_disconnect",
            "host_key_review",
        }:
            self._transition_profile_for_lifecycle(connection, request.request)
            _lifecycle_reservation_checkpoint("after_profile_transition")

        operation_id = self._new_id("operation")
        timestamp = self._timestamp()
        version = 1
        etag = self._etag("lifecycle_operation", operation_id, version)
        phase_plan = _canonical_json_bytes(list(m.LIFECYCLE_PHASES))
        progress = _canonical_json_bytes({"kind": "indeterminate"})
        connection.execute(
            """
            INSERT INTO lifecycle_operations(
                operation_id, kind, resource_kind, resource_id,
                request_sha256, request_json, phase_plan_json,
                status, phase, phase_index, phase_total, progress_json,
                cancellable, result_json, failure_json,
                log_sequence_high_watermark, dropped_before_sequence,
                log_byte_count, cancellation_requested, resource_version,
                created_at, started_at, updated_at, finished_at, etag
            ) VALUES (
                ?, ?, ?, ?, ?, ?, ?, 'queued', 'queued', 1, 17, ?,
                ?, NULL, NULL, 0, 0, 0, 0, ?, ?, NULL, ?, NULL, ?
            )
            """,
            (
                operation_id,
                request.kind,
                request.resource.resource_kind,
                request.resource.resource_id,
                request_sha256,
                request_document,
                phase_plan,
                progress,
                int(request.kind != "profile_disconnect"),
                version,
                timestamp,
                timestamp,
                etag,
            ),
        )
        connection.execute(
            """
            INSERT INTO lifecycle_idempotency_records(
                principal, action, resource_scope, idempotency_key,
                request_sha256, operation_id, created_at
            ) VALUES (?, 'reserve', ?, ?, ?, ?, ?)
            """,
            (
                LOCAL_PRINCIPAL,
                resource_scope,
                idempotency_key,
                request_sha256,
                operation_id,
                timestamp,
            ),
        )
        return self._lifecycle_operation_from_row(
            self._require_lifecycle_operation_row(connection, operation_id)
        )

    def get_lifecycle_operation(self, operation_id: str) -> m.LifecycleOperationV2:
        self._validate_profile_id(operation_id)
        with self._transaction(write=False, operation="getLifecycleOperationV2") as connection:
            return self._lifecycle_operation_from_row(
                self._require_lifecycle_operation_row(connection, operation_id)
            )

    def get_lifecycle_operation_by_action(self, action_id: str) -> m.LifecycleOperationV2:
        """Resolve a reserved lifecycle operation from its durable action identity."""

        self._validate_idempotency_key(action_id)
        with self._transaction(
            write=False,
            operation="getLifecycleOperationByActionV2",
        ) as connection:
            row = connection.execute(
                """
                SELECT operation_id
                FROM lifecycle_idempotency_records
                WHERE principal = ? AND action = 'reserve'
                  AND resource_scope = 'lifecycle_operations'
                  AND idempotency_key = ?
                """,
                (LOCAL_PRINCIPAL, action_id),
            ).fetchone()
            if row is None:
                raise ProviderNotFoundV2("lifecycle action was not found")
            return self._lifecycle_operation_from_row(
                self._require_lifecycle_operation_row(
                    connection,
                    cast(str, row["operation_id"]),
                )
            )

    def get_lifecycle_operation_work(
        self,
        operation_id: str,
    ) -> LifecycleOperationWorkV2:
        self._validate_profile_id(operation_id)
        with self._transaction(
            write=False,
            operation="getLifecycleOperationWorkV2",
        ) as connection:
            return self._lifecycle_work_from_row(
                connection,
                self._require_lifecycle_operation_row(connection, operation_id),
            )

    def list_pending_lifecycle_operations(
        self,
    ) -> tuple[m.LifecycleOperationRefV2, ...]:
        with self._transaction(
            write=False,
            operation="listPendingLifecycleOperationsV2",
        ) as connection:
            rows = connection.execute(
                f"""
                SELECT {_LIFECYCLE_OPERATION_SELECT_COLUMNS}
                FROM lifecycle_operations AS operation
                WHERE operation.status IN ('queued', 'running')
                   OR NOT EXISTS (
                       SELECT 1
                       FROM lifecycle_reconciliation_acknowledgements AS acknowledgement
                       WHERE acknowledgement.operation_id = operation.operation_id
                   )
                ORDER BY operation.created_at, operation.operation_id
                """
            ).fetchall()
            if len(rows) > self._max_lifecycle_operations:
                raise ProviderCapacityConfigurationV2Error(
                    "persisted lifecycle operations exceed configured capacity"
                )
            return tuple(
                m.LifecycleOperationRefV2.from_operation(
                    self._lifecycle_operation_from_row(cast(sqlite3.Row, row))
                )
                for row in rows
            )

    def claim_next_lifecycle_operation(
        self,
        *,
        exclude_running_operation_ids: Collection[str] = (),
    ) -> LifecycleOperationWorkV2 | None:
        excluded = tuple(sorted(set(exclude_running_operation_ids)))
        if (
            len(excluded) > self._max_lifecycle_operations
            or any(type(operation_id) is not str for operation_id in excluded)
        ):
            raise ProviderContractV2Error("lifecycle claim exclusions are invalid")
        for operation_id in excluded:
            self._validate_profile_id(operation_id)
        with self._transaction(
            write=True,
            operation="claimLifecycleOperationV2",
        ) as connection:
            exclusion_clause = ""
            parameters: tuple[object, ...] = ()
            if excluded:
                placeholders = ", ".join("?" for _operation_id in excluded)
                exclusion_clause = (
                    " AND (cancellation_requested = 1 "
                    f"OR operation_id NOT IN ({placeholders}))"
                )
                parameters = cast(tuple[object, ...], excluded)
            row = connection.execute(
                f"""
                SELECT {_LIFECYCLE_OPERATION_SELECT_COLUMNS}
                FROM lifecycle_operations
                WHERE status = 'running'
                {exclusion_clause}
                ORDER BY created_at, operation_id
                LIMIT 1
                """,
                parameters,
            ).fetchone()
            if row is None:
                row = connection.execute(
                    f"""
                    SELECT {_LIFECYCLE_OPERATION_SELECT_COLUMNS}
                    FROM lifecycle_operations
                    WHERE status = 'queued'
                    ORDER BY created_at, operation_id
                    LIMIT 1
                    """
                ).fetchone()
                if row is None:
                    return None
                typed_row = cast(sqlite3.Row, row)
                version = self._next_lifecycle_version(typed_row)
                timestamp = self._timestamp()
                etag = self._etag(
                    "lifecycle_operation",
                    cast(str, typed_row["operation_id"]),
                    version,
                )
                connection.execute(
                    """
                    UPDATE lifecycle_operations
                    SET status = 'running', started_at = ?, updated_at = ?,
                        resource_version = ?, etag = ?
                    WHERE operation_id = ?
                    """,
                    (
                        timestamp,
                        timestamp,
                        version,
                        etag,
                        typed_row["operation_id"],
                    ),
                )
                row = self._require_lifecycle_operation_row(
                    connection,
                    cast(str, typed_row["operation_id"]),
                )
            return self._lifecycle_work_from_row(connection, cast(sqlite3.Row, row))

    def advance_lifecycle_operation(
        self,
        update: LifecycleOperationAdvanceV2 | Mapping[str, object],
    ) -> m.LifecycleOperationV2:
        validated = self._validate_model(LifecycleOperationAdvanceV2, update)
        with self._transaction(
            write=True,
            operation="advanceLifecycleOperationV2",
        ) as connection:
            row = self._require_lifecycle_operation_row(connection, validated.operation_id)
            current = self._lifecycle_operation_from_row(row)
            if (
                current.phase == validated.phase
                and current.progress == validated.progress
                and current.cancellable == validated.cancellable
            ):
                return current
            if current.status != "running":
                raise ProviderConflictV2("only a running lifecycle operation can advance")
            if not hmac.compare_digest(current.etag, validated.expected_etag):
                raise ProviderPreconditionFailedV2("lifecycle operation ETag changed")
            if current.kind == "profile_disconnect" and validated.cancellable:
                raise ProviderPreconditionFailedV2(
                    "profile disconnect cancellation barrier cannot reopen"
                )
            if not current.cancellable and validated.cancellable:
                raise ProviderPreconditionFailedV2(
                    "lifecycle cancellation barrier cannot reopen"
                )
            next_phase_index = m.LIFECYCLE_PHASES.index(validated.phase)
            if next_phase_index < current.phase_index:
                raise ProviderPreconditionFailedV2("lifecycle phase cannot regress")
            self._validate_lifecycle_progress_advance(
                current=current.progress,
                updated=validated.progress,
                same_phase=next_phase_index == current.phase_index,
            )
            version = self._next_lifecycle_version(row)
            timestamp = self._timestamp()
            etag = self._etag("lifecycle_operation", current.operation_id, version)
            connection.execute(
                """
                UPDATE lifecycle_operations
                SET phase = ?, phase_index = ?, progress_json = ?, cancellable = ?,
                    updated_at = ?, resource_version = ?, etag = ?
                WHERE operation_id = ?
                """,
                (
                    validated.phase,
                    next_phase_index,
                    _canonical_json_bytes(validated.progress),
                    int(validated.cancellable),
                    timestamp,
                    version,
                    etag,
                    current.operation_id,
                ),
            )
            return self._lifecycle_operation_from_row(
                self._require_lifecycle_operation_row(connection, current.operation_id)
            )

    def append_lifecycle_log(
        self,
        entry: LifecycleLogAppendV2 | Mapping[str, object],
    ) -> m.LifecycleOperationV2:
        validated = self._validate_model(LifecycleLogAppendV2, entry)
        safe_text, truncated = self._truncate_lifecycle_log_text(
            validated.text,
            already_truncated=validated.truncated,
        )
        with self._transaction(write=True, operation="appendLifecycleLogV2") as connection:
            row = self._require_lifecycle_operation_row(connection, validated.operation_id)
            current = self._lifecycle_operation_from_row(row)
            if current.status in {"succeeded", "failed", "cancelled"}:
                raise ProviderConflictV2("terminal lifecycle operation logs are immutable")
            sequence = current.log_sequence_high_watermark + 1
            occurred_at = self._timestamp()
            try:
                public_entry = m.LifecycleLogEntryV2(
                    operation_id=current.operation_id,
                    sequence=sequence,
                    occurred_at=occurred_at,
                    source=validated.source,
                    text=safe_text,
                    truncated=truncated,
                )
            except ValidationError as exc:
                raise ProviderContractV2Error("lifecycle log entry is unsafe") from exc
            text_bytes = public_entry.text.encode("utf-8")
            connection.execute(
                """
                INSERT INTO lifecycle_operation_logs(
                    operation_id, sequence, occurred_at, source,
                    text, text_bytes, truncated
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    public_entry.operation_id,
                    public_entry.sequence,
                    public_entry.occurred_at,
                    public_entry.source,
                    text_bytes,
                    len(text_bytes),
                    int(public_entry.truncated),
                ),
            )
            version = self._next_lifecycle_version(row)
            etag = self._etag("lifecycle_operation", current.operation_id, version)
            connection.execute(
                """
                UPDATE lifecycle_operations
                SET log_sequence_high_watermark = ?, log_byte_count = log_byte_count + ?,
                    updated_at = ?, resource_version = ?, etag = ?
                WHERE operation_id = ?
                """,
                (
                    sequence,
                    len(text_bytes),
                    occurred_at,
                    version,
                    etag,
                    current.operation_id,
                ),
            )
            self._enforce_lifecycle_log_budgets(connection, current.operation_id)
            return self._lifecycle_operation_from_row(
                self._require_lifecycle_operation_row(connection, current.operation_id)
            )

    def read_lifecycle_logs(
        self,
        operation_id: str,
        *,
        limit: int,
        after: str | None,
        after_sequence: int | None = None,
    ) -> m.LifecycleLogPageV2:
        self._validate_profile_id(operation_id)
        if type(limit) is not int or not 1 <= limit <= m.MAX_LIFECYCLE_LOG_PAGE_ENTRIES:
            raise ProviderContractV2Error("lifecycle log page limit is invalid")
        if after is not None and after_sequence is not None:
            raise ProviderContractV2Error("lifecycle log positions are mutually exclusive")
        if after_sequence is not None and (
            type(after_sequence) is not int
            or not 0 <= after_sequence <= m.MAX_JAVASCRIPT_SAFE_INTEGER
        ):
            raise ProviderContractV2Error("lifecycle log sequence is invalid")
        with self._transaction(write=False, operation="readLifecycleLogsV2") as connection:
            operation_row = self._require_lifecycle_operation_row(connection, operation_id)
            dropped_before = cast(int, operation_row["dropped_before_sequence"])
            next_sequence = dropped_before + 1
            if after is not None:
                cursor = self._decode_lifecycle_cursor(connection, after)
                if cursor["operation_id"] != operation_id:
                    raise ProviderContractV2Error("lifecycle cursor belongs to another operation")
                cursor_dropped = cast(int, cursor["dropped_before_sequence"])
                if cursor_dropped > dropped_before:
                    raise ProviderContractV2Error("lifecycle cursor has an invalid boundary")
                next_sequence = cast(int, cursor["next_sequence"])
                if next_sequence <= dropped_before:
                    raise ProviderCursorExpiredV2("lifecycle log cursor was evicted")
            elif after_sequence is not None:
                next_sequence = max(dropped_before + 1, after_sequence + 1)
            rows = connection.execute(
                f"""
                SELECT {_LIFECYCLE_LOG_SELECT_COLUMNS}
                FROM lifecycle_operation_logs
                WHERE operation_id = ? AND sequence >= ?
                ORDER BY sequence
                LIMIT ?
                """,
                (operation_id, next_sequence, limit + 1),
            ).fetchall()
            has_more = len(rows) > limit
            visible = rows[:limit]
            items = [self._lifecycle_log_from_row(cast(sqlite3.Row, row)) for row in visible]
            next_cursor = None
            if has_more and items:
                next_cursor = self._encode_lifecycle_cursor(
                    connection,
                    operation_id=operation_id,
                    next_sequence=items[-1].sequence + 1,
                    dropped_before_sequence=dropped_before,
                )
            return m.LifecycleLogPageV2(
                operation_id=operation_id,
                dropped_before_sequence=dropped_before,
                items=items,
                next_cursor=next_cursor,
                has_more=has_more,
            )

    def request_lifecycle_cancellation(
        self,
        operation_id: str,
        *,
        if_match: str,
        idempotency_key: str,
    ) -> m.LifecycleOperationV2:
        self._validate_profile_id(operation_id)
        self._validate_etag(if_match)
        self._validate_idempotency_key(idempotency_key)
        # The durable action means "cancel this exact operation".  If-Match
        # fences the first successful commit, but is not part of the replay
        # identity: after an ambiguous response a relaunched renderer must be
        # able to use the latest observed ETag with the same action key.
        request_sha256 = hashlib.sha256(
            _canonical_json_bytes({"operation_id": operation_id})
        ).hexdigest()
        with self._transaction(
            write=True,
            operation="cancelLifecycleOperationV2",
        ) as connection:
            replay = connection.execute(
                """
                SELECT request_sha256, operation_id
                FROM lifecycle_idempotency_records
                WHERE principal = ? AND action = 'cancel'
                  AND resource_scope = ? AND idempotency_key = ?
                """,
                (LOCAL_PRINCIPAL, operation_id, idempotency_key),
            ).fetchone()
            if replay is not None:
                if replay["operation_id"] != operation_id:
                    raise ProviderDataV2Error(
                        "lifecycle cancellation replay belongs to another operation"
                    )
                if not hmac.compare_digest(replay["request_sha256"], request_sha256):
                    raise ProviderDataV2Error(
                        "lifecycle cancellation replay digest is invalid"
                    )
                operation_row = self._require_lifecycle_operation_row(
                    connection,
                    operation_id,
                )
                if cast(int, operation_row["cancellation_requested"]) != 1:
                    raise ProviderDataV2Error(
                        "lifecycle cancellation replay lacks operation state"
                    )
                return self._lifecycle_operation_from_row(
                    operation_row
                )
            row = self._require_lifecycle_operation_row(connection, operation_id)
            current = self._lifecycle_operation_from_row(row)
            if not hmac.compare_digest(current.etag, if_match):
                raise ProviderPreconditionFailedV2("lifecycle operation ETag changed")
            if current.status in {"succeeded", "failed", "cancelled"}:
                raise ProviderConflictV2("terminal lifecycle operation cannot be cancelled")
            if not current.cancellable:
                raise ProviderConflictV2("lifecycle operation is not safely cancellable")
            version = self._next_lifecycle_version(row)
            timestamp = self._timestamp()
            etag = self._etag("lifecycle_operation", operation_id, version)
            if current.status == "queued":
                self._cancel_profile_lifecycle_transition(
                    connection,
                    row,
                    timestamp=timestamp,
                )
                connection.execute(
                    """
                    UPDATE lifecycle_operations
                    SET status = 'cancelled', cancellable = 0,
                        cancellation_requested = 1, updated_at = ?, finished_at = ?,
                        resource_version = ?, etag = ?
                    WHERE operation_id = ?
                    """,
                    (timestamp, timestamp, version, etag, operation_id),
                )
            else:
                connection.execute(
                    """
                    UPDATE lifecycle_operations
                    SET cancellable = 0, cancellation_requested = 1,
                        updated_at = ?, resource_version = ?, etag = ?
                    WHERE operation_id = ?
                    """,
                    (timestamp, version, etag, operation_id),
                )
            connection.execute(
                """
                INSERT INTO lifecycle_idempotency_records(
                    principal, action, resource_scope, idempotency_key,
                    request_sha256, operation_id, created_at
                ) VALUES (?, 'cancel', ?, ?, ?, ?, ?)
                """,
                (
                    LOCAL_PRINCIPAL,
                    operation_id,
                    idempotency_key,
                    request_sha256,
                    operation_id,
                    timestamp,
                ),
            )
            return self._lifecycle_operation_from_row(
                self._require_lifecycle_operation_row(connection, operation_id)
            )

    def finish_lifecycle_operation(
        self,
        completion: LifecycleOperationCompletionV2 | Mapping[str, object],
    ) -> m.LifecycleOperationV2:
        validated = self._validate_model(LifecycleOperationCompletionV2, completion)
        with self._transaction(write=True, operation="finishLifecycleOperationV2") as connection:
            row = self._require_lifecycle_operation_row(connection, validated.operation_id)
            current = self._lifecycle_operation_from_row(row)
            if current.status in {"succeeded", "failed", "cancelled"}:
                if (
                    current.status == validated.status
                    and current.result == validated.result
                    and current.failure == validated.failure
                ):
                    return current
                raise ProviderConflictV2("terminal lifecycle operation is immutable")
            if not hmac.compare_digest(current.etag, validated.expected_etag):
                raise ProviderPreconditionFailedV2("lifecycle operation ETag changed")
            if current.status != "running":
                raise ProviderConflictV2("only a running lifecycle operation can finish")
            if current.kind == "profile_disconnect" and validated.status == "cancelled":
                raise ProviderPreconditionFailedV2(
                    "profile disconnect cannot finish as cancelled"
                )
            cancellation_requested = cast(int, row["cancellation_requested"]) == 1
            if validated.status == "cancelled" and not cancellation_requested:
                raise ProviderPreconditionFailedV2(
                    "lifecycle cancellation was not requested"
                )
            if (
                cancellation_requested
                and validated.status != "cancelled"
            ):
                raise ProviderPreconditionFailedV2(
                    "lifecycle cancellation changed the terminal outcome"
                )
            version = self._next_lifecycle_version(row)
            timestamp = self._timestamp()
            phase = "finalizing" if validated.status == "succeeded" else current.phase
            phase_index = 16 if validated.status == "succeeded" else current.phase_index
            result_json = (
                _canonical_json_bytes(validated.result) if validated.result is not None else None
            )
            failure_json = (
                _canonical_json_bytes(validated.failure) if validated.failure is not None else None
            )
            etag = self._etag("lifecycle_operation", current.operation_id, version)
            if validated.status == "cancelled":
                self._cancel_profile_lifecycle_transition(
                    connection,
                    row,
                    timestamp=timestamp,
                )
            connection.execute(
                """
                UPDATE lifecycle_operations
                SET status = ?, phase = ?, phase_index = ?, cancellable = 0,
                    result_json = ?, failure_json = ?, updated_at = ?, finished_at = ?,
                    resource_version = ?, etag = ?
                WHERE operation_id = ?
                """,
                (
                    validated.status,
                    phase,
                    phase_index,
                    result_json,
                    failure_json,
                    timestamp,
                    timestamp,
                    version,
                    etag,
                    current.operation_id,
                ),
            )
            return self._lifecycle_operation_from_row(
                self._require_lifecycle_operation_row(connection, current.operation_id)
            )

    def acknowledge_lifecycle_operation(
        self,
        operation_id: str,
        request: m.LifecycleAcknowledgeV2 | Mapping[str, object],
        *,
        if_match: str,
        idempotency_key: str,
    ) -> None:
        self._validate_profile_id(operation_id)
        validated = self._validate_model(m.LifecycleAcknowledgeV2, request)
        self._validate_etag(if_match)
        self._validate_idempotency_key(idempotency_key)
        request_sha256 = hashlib.sha256(_canonical_json_bytes(validated)).hexdigest()
        with self._transaction(
            write=True,
            operation="acknowledgeLifecycleOperationV2",
        ) as connection:
            existing = connection.execute(
                """
                SELECT terminal_status, terminal_etag, request_sha256, idempotency_key
                FROM lifecycle_reconciliation_acknowledgements
                WHERE operation_id = ?
                """,
                (operation_id,),
            ).fetchone()
            if existing is not None:
                if (
                    existing["terminal_status"] == validated.expected_terminal_status
                    and hmac.compare_digest(existing["terminal_etag"], if_match)
                    and hmac.compare_digest(existing["request_sha256"], request_sha256)
                    and hmac.compare_digest(existing["idempotency_key"], idempotency_key)
                ):
                    return
                raise ProviderIdempotencyConflictV2(
                    "lifecycle acknowledgement differs from the recorded request"
                )
            row = self._require_lifecycle_operation_row(connection, operation_id)
            operation = self._lifecycle_operation_from_row(row)
            if validated.expected_operation_id != operation_id:
                raise ProviderPreconditionFailedV2(
                    "lifecycle acknowledgement operation identity changed"
                )
            if (
                operation.status != validated.expected_terminal_status
                or operation.status not in {"succeeded", "failed", "cancelled"}
                or not hmac.compare_digest(operation.etag, if_match)
            ):
                raise ProviderPreconditionFailedV2(
                    "lifecycle terminal authority changed before acknowledgement"
                )
            connection.execute(
                """
                INSERT INTO lifecycle_reconciliation_acknowledgements(
                    operation_id, terminal_status, terminal_etag,
                    request_sha256, idempotency_key, acknowledged_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    operation_id,
                    operation.status,
                    operation.etag,
                    request_sha256,
                    idempotency_key,
                    self._timestamp(),
                ),
            )

    def reconcile_lifecycle_operations(self) -> tuple[LifecycleOperationWorkV2, ...]:
        with self._transaction(
            write=True,
            operation="reconcileLifecycleOperationsV2",
        ) as connection:
            self._cleanup_acknowledged_lifecycle_operations(connection)
            rows = connection.execute(
                f"""
                SELECT {_LIFECYCLE_OPERATION_SELECT_COLUMNS}
                FROM lifecycle_operations
                WHERE status IN ('queued', 'running')
                ORDER BY created_at, operation_id
                """
            ).fetchall()
            if len(rows) > self._max_lifecycle_operations:
                raise ProviderCapacityConfigurationV2Error(
                    "persisted lifecycle operations exceed configured capacity"
                )
            return tuple(
                self._lifecycle_work_from_row(connection, cast(sqlite3.Row, row))
                for row in rows
            )

    def create_system_profile(
        self,
        request: m.SystemOpenSshProfileCreateV2 | Mapping[str, object],
        *,
        catalog_generation: int,
        idempotency_key: str,
    ) -> m.RemoteWorkspaceProfileV2:
        validated = self._validate_model(m.SystemOpenSshProfileCreateV2, request)
        if (
            type(catalog_generation) is not int
            or not 0 <= catalog_generation <= m.MAX_JAVASCRIPT_SAFE_INTEGER
        ):
            raise ProviderContractV2Error("catalog generation is outside v2 bounds")
        request_value = {
            "request": validated.model_dump(mode="json"),
            "catalog_generation": catalog_generation,
        }

        def mutation(connection: sqlite3.Connection) -> BaseModel:
            self._require_profile_capacity(connection)
            timestamp = self._timestamp()
            profile = self._new_system_profile(
                request=validated,
                catalog_generation=catalog_generation,
                timestamp=timestamp,
            )
            self._insert_profile(connection, profile)
            return profile

        result = self._execute_idempotent(
            operation="createSystemOpenSshProfileV2",
            resource_scope="profiles",
            idempotency_key=idempotency_key,
            request_value=request_value,
            response_kind="profile",
            mutation=mutation,
        )
        if not isinstance(result, m.RemoteWorkspaceProfileV2):
            raise ProviderDataV2Error("profile creation replay has the wrong type")
        return result

    def rename_profile(
        self,
        profile_id: str,
        patch: m.ProfileDisplayNamePatchV2 | Mapping[str, object],
        *,
        if_match: str,
        idempotency_key: str,
    ) -> m.RemoteProfileV2:
        self._validate_profile_id(profile_id)
        validated = self._validate_model(m.ProfileDisplayNamePatchV2, patch)
        self._validate_etag(if_match)

        def mutation(connection: sqlite3.Connection) -> BaseModel:
            row = self._require_profile_row(connection, profile_id)
            current = self._profile_from_row(row)
            if not hmac.compare_digest(current.etag, if_match):
                raise ProviderPreconditionFailedV2("profile ETag changed")
            current_version = cast(int, row["resource_version"])
            if current_version >= m.MAX_JAVASCRIPT_SAFE_INTEGER:
                raise ProviderCapacityV2Error("profile resource version is exhausted")
            version = current_version + 1
            payload = current.model_dump(mode="json")
            payload.update(
                {
                    "display_name": validated.display_name,
                    "updated_at": self._timestamp(),
                    "etag": self._etag("profile", profile_id, version),
                }
            )
            updated = self._validate_remote_profile(payload)
            self._update_profile(connection, updated, version=version)
            return updated

        return cast(
            m.RemoteProfileV2,
            self._execute_idempotent(
                operation="renameProfileV2",
                resource_scope=profile_id,
                idempotency_key=idempotency_key,
                request_value={
                    "profile_id": profile_id,
                    "patch": validated.model_dump(mode="json"),
                    "if_match": if_match,
                },
                response_kind="profile",
                mutation=mutation,
            ),
        )

    def import_legacy_profile(
        self,
        source: LegacyProfileImportV2 | Mapping[str, object],
    ) -> m.LegacyExplicitProfileV2:
        validated = self._validate_model(LegacyProfileImportV2, source)
        with self._transaction(write=True, operation="importLegacyProfileV2") as connection:
            existing = connection.execute(
                f"SELECT {_PROFILE_SELECT_COLUMNS} FROM profiles "
                "WHERE legacy_source_ref_sha256 = ?",
                (validated.source_ref_sha256,),
            ).fetchone()
            if existing is not None:
                profile = self._profile_from_row(cast(sqlite3.Row, existing))
                if (
                    not isinstance(profile, m.LegacyExplicitProfileV2)
                    or cast(str, existing["legacy_source_document_sha256"])
                    != validated.source_document_sha256
                    or profile.migration_state != validated.migration_state
                ):
                    raise ProviderConflictV2("legacy profile source changed after import")
                return profile
            self._require_profile_capacity(connection)
            profile_id = (
                "legacy-"
                + hashlib.sha256(
                    b"openevo-desktop-legacy-profile-v2\0"
                    + validated.source_ref_sha256.encode("ascii")
                ).hexdigest()[:48]
            )
            collision = connection.execute(
                "SELECT 1 FROM profiles WHERE profile_id = ?", (profile_id,)
            ).fetchone()
            if collision is not None:
                raise ProviderConflictV2("legacy profile identity collides")
            profile = m.LegacyExplicitProfileV2(
                profile_id=profile_id,
                display_name=validated.display_name,
                migration_state=validated.migration_state,
                created_at=validated.created_at,
                updated_at=validated.updated_at,
                etag=self._etag("profile", profile_id, 1),
            )
            self._insert_profile(
                connection,
                profile,
                legacy_source_ref_sha256=validated.source_ref_sha256,
                legacy_source_document_sha256=validated.source_document_sha256,
            )
            return profile

    def rebind_legacy_profile(
        self,
        profile_id: str,
        request: m.ProfileRebindV2 | Mapping[str, object],
        *,
        display_name: str,
        if_match: str,
        idempotency_key: str,
    ) -> m.RemoteWorkspaceProfileV2:
        self._validate_profile_id(profile_id)
        validated = self._validate_model(m.ProfileRebindV2, request)
        display_request = self._validate_model(
            m.SystemOpenSshProfileCreateV2,
            {
                "display_name": display_name,
                "connection_authority": "system_openssh",
                "ssh_host_alias": validated.ssh_host_alias,
            },
        )
        self._validate_etag(if_match)

        def mutation(connection: sqlite3.Connection) -> BaseModel:
            legacy_row = self._require_profile_row(connection, profile_id)
            legacy = self._profile_from_row(legacy_row)
            if not isinstance(legacy, m.LegacyExplicitProfileV2):
                raise ProviderConflictV2("only a legacy explicit profile can be rebound")
            if not hmac.compare_digest(legacy.etag, if_match):
                raise ProviderPreconditionFailedV2("legacy profile ETag changed")
            source_ref = cast(str, legacy_row["legacy_source_ref_sha256"])
            prior = connection.execute(
                "SELECT 1 FROM profiles WHERE rebound_from_sha256 = ?", (source_ref,)
            ).fetchone()
            if prior is not None:
                raise ProviderConflictV2("legacy profile was already rebound")
            self._require_profile_capacity(connection)
            timestamp = self._timestamp()
            profile = self._new_system_profile(
                request=display_request,
                catalog_generation=validated.catalog_generation,
                timestamp=timestamp,
            )
            self._insert_profile(
                connection,
                profile,
                rebound_from_sha256=source_ref,
            )
            return profile

        result = self._execute_idempotent(
            operation="rebindLegacyProfileV2",
            resource_scope=profile_id,
            idempotency_key=idempotency_key,
            request_value={
                "profile_id": profile_id,
                "request": validated.model_dump(mode="json"),
                "display_name": display_request.display_name,
                "if_match": if_match,
            },
            response_kind="profile",
            mutation=mutation,
        )
        if not isinstance(result, m.RemoteWorkspaceProfileV2):
            raise ProviderDataV2Error("legacy rebind replay has the wrong type")
        return result

    def begin_profile_action(
        self,
        profile_id: str,
        request: m.ProfileConnectionActionV2 | m.HostKeyReviewRequestV2,
        *,
        action: Literal["connect", "disconnect", "host_key_review"],
        resource_generation: int,
        if_match: str,
        idempotency_key: str,
    ) -> m.RemoteWorkspaceProfileV2:
        """Durably reserve one generation-replacing system-SSH action."""

        self._validate_profile_id(profile_id)
        request_type: type[m.ProfileConnectionActionV2]
        if action == "host_key_review":
            request_type = m.HostKeyReviewRequestV2
        elif action in {"connect", "disconnect"}:
            request_type = m.ProfileConnectionActionV2
        else:
            raise ProviderContractV2Error("profile action is outside the closed v2 set")
        validated = self._validate_model(request_type, request)
        self._validate_etag(if_match)
        if (
            type(resource_generation) is not int
            or not 1 <= resource_generation <= m.MAX_JAVASCRIPT_SAFE_INTEGER
        ):
            raise ProviderContractV2Error("profile generation is outside v2 bounds")

        operation = {
            "connect": "connectProfileV2",
            "disconnect": "disconnectProfileV2",
            "host_key_review": "reviewHostKeyV2",
        }[action]

        def mutation(connection: sqlite3.Connection) -> BaseModel:
            row = self._require_profile_row(connection, profile_id)
            current = self._profile_from_row(row)
            if not isinstance(current, m.RemoteWorkspaceProfileV2):
                raise ProviderConflictV2("legacy profiles must be rebound before connection")
            if (
                current.connection_generation != resource_generation
                or validated.expected_connection_generation != resource_generation
            ):
                raise ProviderPreconditionFailedV2("profile connection generation changed")
            if not hmac.compare_digest(current.etag, if_match):
                raise ProviderPreconditionFailedV2("profile ETag changed")
            if action == "host_key_review":
                if not isinstance(validated, m.HostKeyReviewRequestV2):
                    raise ProviderContractV2Error("host-key review request is invalid")
                self._require_current_host_key_review(current, validated)
            current_version = cast(int, row["resource_version"])
            if (
                current_version >= m.MAX_JAVASCRIPT_SAFE_INTEGER
                or current.connection_generation >= m.MAX_JAVASCRIPT_SAFE_INTEGER
            ):
                raise ProviderCapacityV2Error("profile generation is exhausted")
            version = current_version + 1
            generation = current.connection_generation + 1
            timestamp = self._timestamp()
            trust_state: Literal["unverified", "trusted", "repairing"]
            if action == "host_key_review":
                trust_state = "repairing"
            elif current.trust.state == "trusted":
                trust_state = "trusted"
            else:
                trust_state = "unverified"
            updated = m.RemoteWorkspaceProfileV2(
                profile_id=current.profile_id,
                display_name=current.display_name,
                ssh_host_alias=current.ssh_host_alias,
                catalog_generation=current.catalog_generation,
                connection_generation=generation,
                connection_state=("disconnecting" if action == "disconnect" else "connecting"),
                prompt=None,
                trust=m.SshTrustStateV2(
                    connection_generation=generation,
                    state=trust_state,
                    review_id=None,
                    review_sha256=None,
                    key_fingerprints=[],
                    repair_support="not_needed",
                ),
                failure=None,
                active_project_id=current.active_project_id,
                core_api_major=None,
                core_openapi_sha256=None,
                core_event_schema_sha256=None,
                core_registry_sha256=None,
                created_at=current.created_at,
                updated_at=timestamp,
                etag=self._etag("profile", profile_id, version),
            )
            self._update_profile(connection, updated, version=version)
            return updated

        result = self._execute_idempotent(
            operation=operation,
            resource_scope=profile_id,
            idempotency_key=idempotency_key,
            request_value={
                "profile_id": profile_id,
                "request": validated.model_dump(mode="json"),
                "resource_generation": resource_generation,
                "if_match": if_match,
            },
            response_kind="profile",
            mutation=mutation,
        )
        if not isinstance(result, m.RemoteWorkspaceProfileV2):
            raise ProviderDataV2Error("profile action replay has the wrong type")
        return result

    def complete_profile_connection(
        self,
        profile_id: str,
        *,
        connection_generation: int,
        core_version: VersionResponseV2,
    ) -> m.RemoteWorkspaceProfileV2:
        """Publish exact compatible Core identity for one connected generation."""

        self._validate_profile_id(profile_id)
        if type(core_version) is not VersionResponseV2:
            raise ProviderContractV2Error("Core version authority has the wrong type")
        offer = next(
            (item for item in core_version.contracts if item.api_major == 2),
            None,
        )
        if offer is None or not offer.mutation_compatible:
            raise ProviderContractV2Error("Core v2 mutation authority is unavailable")
        with self._transaction(write=True, operation="completeProfileConnectionV2") as connection:
            row = self._require_profile_row(connection, profile_id)
            current = self._profile_from_row(row)
            if not isinstance(current, m.RemoteWorkspaceProfileV2):
                raise ProviderConflictV2("legacy profile cannot own a connection")
            if current.connection_generation != connection_generation:
                raise ProviderPreconditionFailedV2("profile connection generation changed")
            expected_identity = (
                2,
                offer.openapi_sha256,
                offer.event_schema_sha256,
                core_version.registry_sha256,
            )
            current_identity = (
                current.core_api_major,
                current.core_openapi_sha256,
                current.core_event_schema_sha256,
                current.core_registry_sha256,
            )
            if current.connection_state == "connected":
                if current_identity != expected_identity:
                    raise ProviderConflictV2("connected Core identity changed")
                return current
            if current.connection_state not in {"connecting", "bootstrapping", "negotiating"}:
                raise ProviderConflictV2("profile is not completing a connection")
            current_version = cast(int, row["resource_version"])
            if current_version >= m.MAX_JAVASCRIPT_SAFE_INTEGER:
                raise ProviderCapacityV2Error("profile resource version is exhausted")
            version = current_version + 1
            updated = m.RemoteWorkspaceProfileV2(
                profile_id=current.profile_id,
                display_name=current.display_name,
                ssh_host_alias=current.ssh_host_alias,
                catalog_generation=current.catalog_generation,
                connection_generation=current.connection_generation,
                connection_state="connected",
                prompt=None,
                trust=m.SshTrustStateV2(
                    connection_generation=current.connection_generation,
                    state="trusted",
                    review_id=None,
                    review_sha256=None,
                    key_fingerprints=[],
                    repair_support="not_needed",
                ),
                failure=None,
                active_project_id=current.active_project_id,
                core_api_major=2,
                core_openapi_sha256=offer.openapi_sha256,
                core_event_schema_sha256=offer.event_schema_sha256,
                core_registry_sha256=core_version.registry_sha256,
                created_at=current.created_at,
                updated_at=self._timestamp(),
                etag=self._etag("profile", profile_id, version),
            )
            self._update_profile(connection, updated, version=version)
            return updated

    def publish_profile_host_key_review(
        self,
        profile_id: str,
        *,
        connection_generation: int,
        review: PendingSystemHostKeyReview,
    ) -> m.RemoteWorkspaceProfileV2:
        """Persist only the path-free, digest-bound part of a changed-key review."""

        self._validate_profile_id(profile_id)
        if type(review) is not PendingSystemHostKeyReview or (
            review.profile_id != profile_id
            or review.connection_generation != connection_generation
        ):
            raise ProviderContractV2Error("host-key review authority changed")
        try:
            fingerprints = [
                m.SshHostKeyFingerprintV2(
                    algorithm=algorithm,
                    sha256_fingerprint=fingerprint,
                    role="presented",
                )
                for algorithm, fingerprint in review.key_fingerprints
            ]
        except (TypeError, ValueError, ValidationError) as exc:
            raise ProviderContractV2Error("host-key review fingerprints are invalid") from exc
        trust = m.SshTrustStateV2(
            connection_generation=connection_generation,
            state="changed_key_blocked",
            review_id=review.review_id,
            review_sha256=review.review_sha256,
            key_fingerprints=fingerprints,
            repair_support=review.repair_support,
        )
        with self._transaction(write=True, operation="publishHostKeyReviewV2") as connection:
            row = self._require_profile_row(connection, profile_id)
            current = self._profile_from_row(row)
            if not isinstance(current, m.RemoteWorkspaceProfileV2):
                raise ProviderConflictV2("legacy profile cannot own a host-key review")
            if current.connection_generation != connection_generation:
                raise ProviderPreconditionFailedV2("profile connection generation changed")
            if current.connection_state == "host_key_review":
                if current.trust != trust:
                    raise ProviderConflictV2("host-key review authority changed")
                return current
            if current.connection_state != "connecting":
                raise ProviderConflictV2("profile is not awaiting a host-key review")
            current_version = cast(int, row["resource_version"])
            if current_version >= m.MAX_JAVASCRIPT_SAFE_INTEGER:
                raise ProviderCapacityV2Error("profile resource version is exhausted")
            version = current_version + 1
            updated = m.RemoteWorkspaceProfileV2(
                profile_id=current.profile_id,
                display_name=current.display_name,
                ssh_host_alias=current.ssh_host_alias,
                catalog_generation=current.catalog_generation,
                connection_generation=current.connection_generation,
                connection_state="host_key_review",
                prompt=None,
                trust=trust,
                failure=None,
                active_project_id=current.active_project_id,
                core_api_major=None,
                core_openapi_sha256=None,
                core_event_schema_sha256=None,
                core_registry_sha256=None,
                created_at=current.created_at,
                updated_at=self._timestamp(),
                etag=self._etag("profile", profile_id, version),
            )
            self._update_profile(connection, updated, version=version)
            return updated

    def observe_profile_prompt(
        self,
        profile_id: str,
        *,
        connection_generation: int,
        kind: Literal["password", "passphrase", "host_confirmation"],
        state: Literal["pending", "completed", "rejected", "cancelled"],
    ) -> m.RemoteWorkspaceProfileV2 | None:
        """Project a text-free native askpass observation into the live generation."""

        self._validate_profile_id(profile_id)
        if kind not in {"password", "passphrase", "host_confirmation"} or state not in {
            "pending",
            "completed",
            "rejected",
            "cancelled",
        }:
            raise ProviderContractV2Error("SSH prompt observation is invalid")
        projected_kind: Literal["password", "passphrase", "confirmation"] = (
            "confirmation" if kind == "host_confirmation" else kind
        )
        with self._transaction(write=True, operation="observeProfilePromptV2") as connection:
            row = self._require_profile_row(connection, profile_id)
            current = self._profile_from_row(row)
            if not isinstance(current, m.RemoteWorkspaceProfileV2):
                return None
            if current.connection_generation != connection_generation:
                return None
            if state == "pending":
                if current.connection_state == "prompt_pending":
                    if current.prompt is None or current.prompt.kind != projected_kind:
                        raise ProviderConflictV2("SSH prompt authority changed")
                    return current
                if current.connection_state != "connecting":
                    return None
                timestamp = self._timestamp()
                prompt = m.SshPromptStateV2(
                    connection_generation=connection_generation,
                    kind=projected_kind,
                    state="pending",
                    requested_at=timestamp,
                )
                connection_state: m.ConnectionStateV2 = "prompt_pending"
            else:
                if current.connection_state != "prompt_pending":
                    return current if current.connection_state == "connecting" else None
                if current.prompt is None or current.prompt.kind != projected_kind:
                    raise ProviderConflictV2("SSH prompt authority changed")
                timestamp = self._timestamp()
                prompt = None
                connection_state = "connecting"
            current_version = cast(int, row["resource_version"])
            if current_version >= m.MAX_JAVASCRIPT_SAFE_INTEGER:
                raise ProviderCapacityV2Error("profile resource version is exhausted")
            version = current_version + 1
            updated = m.RemoteWorkspaceProfileV2(
                profile_id=current.profile_id,
                display_name=current.display_name,
                ssh_host_alias=current.ssh_host_alias,
                catalog_generation=current.catalog_generation,
                connection_generation=current.connection_generation,
                connection_state=connection_state,
                prompt=prompt,
                trust=current.trust,
                failure=None,
                active_project_id=current.active_project_id,
                core_api_major=None,
                core_openapi_sha256=None,
                core_event_schema_sha256=None,
                core_registry_sha256=None,
                created_at=current.created_at,
                updated_at=timestamp,
                etag=self._etag("profile", profile_id, version),
            )
            self._update_profile(connection, updated, version=version)
            return updated

    def fail_profile_connection(
        self,
        profile_id: str,
        *,
        connection_generation: int,
        failure: m.DesktopErrorV2,
    ) -> m.RemoteWorkspaceProfileV2:
        """Close one live connection generation with a renderer-safe failure."""

        self._validate_profile_id(profile_id)
        if type(failure) is not m.DesktopErrorV2:
            raise ProviderContractV2Error("profile failure has the wrong type")
        with self._transaction(write=True, operation="failProfileConnectionV2") as connection:
            row = self._require_profile_row(connection, profile_id)
            current = self._profile_from_row(row)
            if not isinstance(current, m.RemoteWorkspaceProfileV2):
                raise ProviderConflictV2("legacy profile cannot own a connection failure")
            if current.connection_generation != connection_generation:
                raise ProviderPreconditionFailedV2("profile connection generation changed")
            if current.connection_state == "failed":
                if current.failure != failure:
                    raise ProviderConflictV2("profile failure authority changed")
                return current
            if current.connection_state not in {
                "connecting",
                "prompt_pending",
                "bootstrapping",
                "negotiating",
            }:
                raise ProviderConflictV2("profile cannot fail from its current state")
            current_version = cast(int, row["resource_version"])
            if current_version >= m.MAX_JAVASCRIPT_SAFE_INTEGER:
                raise ProviderCapacityV2Error("profile resource version is exhausted")
            version = current_version + 1
            updated = m.RemoteWorkspaceProfileV2(
                profile_id=current.profile_id,
                display_name=current.display_name,
                ssh_host_alias=current.ssh_host_alias,
                catalog_generation=current.catalog_generation,
                connection_generation=current.connection_generation,
                connection_state="failed",
                prompt=None,
                trust=m.SshTrustStateV2(
                    connection_generation=current.connection_generation,
                    state="unverified",
                    review_id=None,
                    review_sha256=None,
                    key_fingerprints=[],
                    repair_support="not_needed",
                ),
                failure=failure,
                active_project_id=current.active_project_id,
                core_api_major=None,
                core_openapi_sha256=None,
                core_event_schema_sha256=None,
                core_registry_sha256=None,
                created_at=current.created_at,
                updated_at=self._timestamp(),
                etag=self._etag("profile", profile_id, version),
            )
            self._update_profile(connection, updated, version=version)
            return updated

    def complete_profile_rejection(
        self,
        profile_id: str,
        *,
        connection_generation: int,
    ) -> m.RemoteWorkspaceProfileV2:
        """Consume an exact review as a disconnected, rejected generation."""

        self._validate_profile_id(profile_id)
        with self._transaction(write=True, operation="completeProfileRejectionV2") as connection:
            row = self._require_profile_row(connection, profile_id)
            current = self._profile_from_row(row)
            if not isinstance(current, m.RemoteWorkspaceProfileV2):
                raise ProviderConflictV2("legacy profile cannot reject host trust")
            if current.connection_generation != connection_generation:
                raise ProviderPreconditionFailedV2("profile connection generation changed")
            if current.connection_state != "connecting" or current.trust.state != "repairing":
                raise ProviderConflictV2("profile is not completing a host-key rejection")
            current_version = cast(int, row["resource_version"])
            if current_version >= m.MAX_JAVASCRIPT_SAFE_INTEGER:
                raise ProviderCapacityV2Error("profile resource version is exhausted")
            version = current_version + 1
            updated = m.RemoteWorkspaceProfileV2(
                profile_id=current.profile_id,
                display_name=current.display_name,
                ssh_host_alias=current.ssh_host_alias,
                catalog_generation=current.catalog_generation,
                connection_generation=current.connection_generation,
                connection_state="disconnected",
                prompt=None,
                trust=m.SshTrustStateV2(
                    connection_generation=current.connection_generation,
                    state="rejected",
                    review_id=None,
                    review_sha256=None,
                    key_fingerprints=[],
                    repair_support="not_needed",
                ),
                failure=None,
                active_project_id=current.active_project_id,
                core_api_major=None,
                core_openapi_sha256=None,
                core_event_schema_sha256=None,
                core_registry_sha256=None,
                created_at=current.created_at,
                updated_at=self._timestamp(),
                etag=self._etag("profile", profile_id, version),
            )
            self._update_profile(connection, updated, version=version)
            return updated

    def complete_profile_disconnect(
        self,
        profile_id: str,
        *,
        connection_generation: int,
    ) -> m.RemoteWorkspaceProfileV2:
        self._validate_profile_id(profile_id)
        with self._transaction(write=True, operation="completeProfileDisconnectV2") as connection:
            row = self._require_profile_row(connection, profile_id)
            current = self._profile_from_row(row)
            if not isinstance(current, m.RemoteWorkspaceProfileV2):
                raise ProviderConflictV2("legacy profile cannot own a connection")
            if current.connection_generation != connection_generation:
                raise ProviderPreconditionFailedV2("profile connection generation changed")
            if current.connection_state == "disconnected":
                return current
            if current.connection_state != "disconnecting":
                raise ProviderConflictV2("profile is not completing a disconnect")
            current_version = cast(int, row["resource_version"])
            if current_version >= m.MAX_JAVASCRIPT_SAFE_INTEGER:
                raise ProviderCapacityV2Error("profile resource version is exhausted")
            version = current_version + 1
            updated = m.RemoteWorkspaceProfileV2(
                profile_id=current.profile_id,
                display_name=current.display_name,
                ssh_host_alias=current.ssh_host_alias,
                catalog_generation=current.catalog_generation,
                connection_generation=current.connection_generation,
                connection_state="disconnected",
                prompt=None,
                trust=m.SshTrustStateV2(
                    connection_generation=current.connection_generation,
                    state="unverified",
                    review_id=None,
                    review_sha256=None,
                    key_fingerprints=[],
                    repair_support="not_needed",
                ),
                failure=None,
                active_project_id=current.active_project_id,
                core_api_major=None,
                core_openapi_sha256=None,
                core_event_schema_sha256=None,
                core_registry_sha256=None,
                created_at=current.created_at,
                updated_at=self._timestamp(),
                etag=self._etag("profile", profile_id, version),
            )
            self._update_profile(connection, updated, version=version)
            return updated

    def reconcile_process_restart(self) -> tuple[m.RemoteWorkspaceProfileV2, ...]:
        """Invalidate process authority and durably reclaim interrupted project work."""

        recovered: list[m.RemoteWorkspaceProfileV2] = []
        with self._transaction(write=True, operation="reconcileProcessRestartV2") as connection:
            project_rows = connection.execute(
                f"""
                SELECT {_LIFECYCLE_OPERATION_SELECT_COLUMNS}
                FROM lifecycle_operations
                WHERE kind IN ('project_create', 'project_activate')
                  AND status IN ('queued', 'running')
                ORDER BY created_at, operation_id
                """
            ).fetchall()
            project_work = tuple(
                self._lifecycle_work_from_row(connection, cast(sqlite3.Row, row))
                for row in project_rows
            )
            lifecycle_owned_profiles = {
                cast(str, row[0])
                for row in connection.execute(
                    """
                    SELECT resource_id
                    FROM lifecycle_operations
                    WHERE resource_kind = 'profile'
                      AND status IN ('queued', 'running')
                    """
                ).fetchall()
            }
            rows = connection.execute(
                f"SELECT {_PROFILE_SELECT_COLUMNS} FROM profiles ORDER BY profile_id"
            ).fetchall()
            for raw_row in rows:
                row = cast(sqlite3.Row, raw_row)
                current = self._profile_from_row(row)
                if (
                    not isinstance(current, m.RemoteWorkspaceProfileV2)
                    or current.connection_state == "disconnected"
                    or current.profile_id in lifecycle_owned_profiles
                ):
                    continue
                current_version = cast(int, row["resource_version"])
                if (
                    current_version >= m.MAX_JAVASCRIPT_SAFE_INTEGER
                    or current.connection_generation >= m.MAX_JAVASCRIPT_SAFE_INTEGER
                ):
                    raise ProviderCapacityV2Error("profile restart generation is exhausted")
                version = current_version + 1
                generation = current.connection_generation + 1
                recovered_profile = m.RemoteWorkspaceProfileV2(
                    profile_id=current.profile_id,
                    display_name=current.display_name,
                    ssh_host_alias=current.ssh_host_alias,
                    catalog_generation=current.catalog_generation,
                    connection_generation=generation,
                    connection_state="disconnected",
                    prompt=None,
                    trust=m.SshTrustStateV2(
                        connection_generation=generation,
                        state="unverified",
                        review_id=None,
                        review_sha256=None,
                        key_fingerprints=[],
                        repair_support="not_needed",
                    ),
                    failure=None,
                    active_project_id=current.active_project_id,
                    core_api_major=None,
                    core_openapi_sha256=None,
                    core_event_schema_sha256=None,
                    core_registry_sha256=None,
                    created_at=current.created_at,
                    updated_at=self._timestamp(),
                    etag=self._etag("profile", current.profile_id, version),
                )
                self._update_profile(connection, recovered_profile, version=version)
                recovered.append(recovered_profile)
                parent = self._restart_project_reconnect_parent(
                    recovered_profile,
                    project_work,
                )
                if parent is not None:
                    reconnect = LifecycleOperationReservationV2(
                        kind="profile_connect",
                        resource={
                            "resource_kind": "profile",
                            "resource_id": recovered_profile.profile_id,
                        },
                        request=LifecycleProfileConnectRequestV2(
                            request_kind="profile_connect",
                            profile_id=recovered_profile.profile_id,
                            request=m.ProfileConnectionActionV2(
                                expected_connection_generation=(
                                    recovered_profile.connection_generation
                                )
                            ),
                            resource_generation=recovered_profile.connection_generation,
                            if_match=recovered_profile.etag,
                        ),
                    )
                    self._reserve_lifecycle_operation_in_transaction(
                        connection,
                        reconnect,
                        idempotency_key=_restart_profile_reconnect_key(
                            parent.operation.operation_id,
                            recovered_profile.connection_generation,
                        ),
                    )
        return tuple(recovered)

    @staticmethod
    def _restart_project_reconnect_parent(
        profile: m.RemoteWorkspaceProfileV2,
        pending: tuple[LifecycleOperationWorkV2, ...],
    ) -> LifecycleOperationWorkV2 | None:
        for work in pending:
            if work.cancellation_requested:
                continue
            request = work.request
            if isinstance(request, LifecycleProjectCreateRequestV2):
                if request.request.profile_id == profile.profile_id:
                    return work
                continue
            if (
                isinstance(request, LifecycleProjectActivateRequestV2)
                and profile.active_project_id == request.project_id
            ):
                return work
        return None

    def bind_active_project(
        self,
        profile_id: str,
        *,
        connection_generation: int,
        project_id: str,
    ) -> m.RemoteWorkspaceProfileV2:
        """Bind one remote Core project to the connected profile generation."""

        self._validate_profile_id(profile_id)
        self._validate_profile_id(project_id)
        if (
            type(connection_generation) is not int
            or not 1 <= connection_generation <= m.MAX_JAVASCRIPT_SAFE_INTEGER
        ):
            raise ProviderContractV2Error("profile generation is outside v2 bounds")
        with self._transaction(write=True, operation="bindActiveProjectV2") as connection:
            row = self._require_profile_row(connection, profile_id)
            current = self._profile_from_row(row)
            if not isinstance(current, m.RemoteWorkspaceProfileV2):
                raise ProviderConflictV2("legacy profile cannot own an active project")
            if (
                current.connection_generation != connection_generation
                or current.connection_state != "connected"
            ):
                raise ProviderPreconditionFailedV2("profile connection generation changed")
            if current.active_project_id == project_id:
                return current
            current_version = cast(int, row["resource_version"])
            if current_version >= m.MAX_JAVASCRIPT_SAFE_INTEGER:
                raise ProviderCapacityV2Error("profile resource version is exhausted")
            version = current_version + 1
            updated = m.RemoteWorkspaceProfileV2(
                profile_id=current.profile_id,
                display_name=current.display_name,
                ssh_host_alias=current.ssh_host_alias,
                catalog_generation=current.catalog_generation,
                connection_generation=current.connection_generation,
                connection_state=current.connection_state,
                prompt=current.prompt,
                trust=current.trust,
                failure=current.failure,
                active_project_id=project_id,
                core_api_major=current.core_api_major,
                core_openapi_sha256=current.core_openapi_sha256,
                core_event_schema_sha256=current.core_event_schema_sha256,
                core_registry_sha256=current.core_registry_sha256,
                created_at=current.created_at,
                updated_at=self._timestamp(),
                etag=self._etag("profile", profile_id, version),
            )
            self._update_profile(connection, updated, version=version)
            return updated

    def copy_legacy_draft(
        self,
        source: LegacyDraftSourceV2 | Mapping[str, object],
        *,
        profile_id: str,
        config: ScienceProjectConfigV2 | Mapping[str, object],
        idempotency_key: str,
    ) -> LocalProjectDraftV2:
        validated_source = self._validate_model(LegacyDraftSourceV2, source)
        validated_config = self._validate_model(ScienceProjectConfigV2, config)
        self._validate_profile_id(profile_id)

        def mutation(connection: sqlite3.Connection) -> BaseModel:
            profile = self._profile_from_row(self._require_profile_row(connection, profile_id))
            if not isinstance(profile, m.RemoteWorkspaceProfileV2):
                raise ProviderConflictV2("legacy draft requires a rebound v2 profile")
            prior = connection.execute(
                "SELECT 1 FROM project_drafts WHERE legacy_source_ref_sha256 = ?",
                (validated_source.source_ref_sha256,),
            ).fetchone()
            if prior is not None:
                raise ProviderConflictV2("legacy draft was already copied")
            self._require_draft_capacity(connection)
            timestamp = self._timestamp()
            draft_id = self._new_id("draft")
            draft = LocalProjectDraftV2(
                draft_id=draft_id,
                profile_id=profile_id,
                display_name=validated_source.display_name,
                config=validated_config,
                project_config_sha256=project_config_sha256_for(validated_config),
                legacy_source_ref_sha256=validated_source.source_ref_sha256,
                legacy_source_document_sha256=(validated_source.source_document_sha256),
                created_at=timestamp,
                updated_at=timestamp,
                etag=self._etag("draft", draft_id, 1),
            )
            self._insert_draft(connection, draft)
            return draft

        result = self._execute_idempotent(
            operation="copyLegacyDraftV2",
            resource_scope=validated_source.source_ref_sha256,
            idempotency_key=idempotency_key,
            request_value={
                "source": validated_source.model_dump(mode="json"),
                "profile_id": profile_id,
                "config": validated_config.model_dump(mode="json"),
            },
            response_kind="draft",
            mutation=mutation,
        )
        if not isinstance(result, LocalProjectDraftV2):
            raise ProviderDataV2Error("draft copy replay has the wrong type")
        return result

    def record_migration_diagnostic(
        self,
        *,
        code: MigrationDiagnosticCodeV2,
        source_kind: Literal["store", "profile", "project"],
        source_ref_sha256: str | None,
    ) -> MigrationDiagnosticV2:
        identity = _canonical_json_bytes(
            {
                "code": code,
                "source_kind": source_kind,
                "source_ref_sha256": source_ref_sha256,
            }
        )
        digest = hashlib.sha256(
            b"openevo-desktop-migration-diagnostic-v2\0" + identity
        ).hexdigest()
        diagnostic = MigrationDiagnosticV2(
            diagnostic_id="migration-" + digest[:48],
            code=code,
            source_kind=source_kind,
            source_ref_sha256=source_ref_sha256,
            created_at=self._timestamp(),
        )
        with self._transaction(write=True, operation="recordMigrationDiagnosticV2") as connection:
            existing = connection.execute(
                "SELECT * FROM migration_diagnostics WHERE diagnostic_id = ?",
                (diagnostic.diagnostic_id,),
            ).fetchone()
            if existing is not None:
                return self._diagnostic_from_row(cast(sqlite3.Row, existing))
            count = cast(
                int,
                connection.execute("SELECT count(*) FROM migration_diagnostics").fetchone()[0],
            )
            if count >= self._max_migration_diagnostics:
                return diagnostic
            connection.execute(
                """
                INSERT INTO migration_diagnostics(
                    diagnostic_id, code, source_kind, source_ref_sha256, created_at
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (
                    diagnostic.diagnostic_id,
                    diagnostic.code,
                    diagnostic.source_kind,
                    diagnostic.source_ref_sha256,
                    diagnostic.created_at,
                ),
            )
            return diagnostic

    def get_profile(self, profile_id: str) -> m.RemoteProfileV2:
        self._validate_profile_id(profile_id)
        with self._transaction(write=False, operation="getProfileV2") as connection:
            return self._profile_from_row(self._require_profile_row(connection, profile_id))

    def list_profiles(self) -> tuple[m.RemoteProfileV2, ...]:
        with self._transaction(write=False, operation="listProfilesV2") as connection:
            rows = connection.execute(
                f"SELECT {_PROFILE_SELECT_COLUMNS} FROM profiles "
                "ORDER BY updated_at DESC, profile_id"
            ).fetchall()
            if len(rows) > self._max_profiles:
                raise ProviderCapacityConfigurationV2Error(
                    "persisted profiles exceed configured capacity"
                )
            return tuple(self._profile_from_row(cast(sqlite3.Row, row)) for row in rows)

    def list_drafts(self) -> tuple[LocalProjectDraftV2, ...]:
        with self._transaction(write=False, operation="listDraftsV2") as connection:
            rows = connection.execute(
                f"SELECT {_DRAFT_SELECT_COLUMNS} FROM project_drafts "
                "ORDER BY updated_at DESC, draft_id"
            ).fetchall()
            if len(rows) > self._max_drafts:
                raise ProviderCapacityConfigurationV2Error(
                    "persisted drafts exceed configured capacity"
                )
            return tuple(self._draft_from_row(cast(sqlite3.Row, row)) for row in rows)

    def list_migration_diagnostics(self) -> tuple[MigrationDiagnosticV2, ...]:
        with self._transaction(write=False, operation="listMigrationDiagnosticsV2") as connection:
            rows = connection.execute(
                "SELECT * FROM migration_diagnostics ORDER BY diagnostic_id"
            ).fetchall()
            if len(rows) > self._max_migration_diagnostics:
                raise ProviderCapacityConfigurationV2Error(
                    "persisted migration diagnostics exceed configured capacity"
                )
            return tuple(self._diagnostic_from_row(cast(sqlite3.Row, row)) for row in rows)

    @staticmethod
    def _require_secure_platform() -> None:
        if (
            not hasattr(os, "O_NOFOLLOW")
            or not hasattr(os, "O_DIRECTORY")
            or os.open not in os.supports_dir_fd
            or os.stat not in os.supports_dir_fd
        ):
            raise ProviderStateV2Error("platform lacks descriptor-relative no-follow v2 storage")

    @staticmethod
    def _create_or_validate_root(root: Path) -> None:
        try:
            metadata = os.lstat(root)
        except FileNotFoundError:
            root.parent.mkdir(parents=True, exist_ok=True)
            try:
                os.mkdir(root, 0o700)
            except FileExistsError:
                metadata = os.lstat(root)
            else:
                os.chmod(root, 0o700, follow_symlinks=False)
                metadata = os.lstat(root)
        if not stat.S_ISDIR(metadata.st_mode) or stat.S_ISLNK(metadata.st_mode):
            raise ProviderStateV2Error("v2 provider root must be a real directory")
        if hasattr(os, "getuid") and metadata.st_uid != os.getuid():
            raise ProviderStateV2Error("v2 provider root has the wrong owner")
        if stat.S_IMODE(metadata.st_mode) != 0o700:
            raise ProviderStateV2Error("v2 provider root mode must be 0700")

    def _verify_root(self) -> None:
        if self._closed:
            raise ProviderStateV2Error("v2 provider store is closed")
        try:
            path_stat = os.lstat(self._state_root)
            fd_stat = os.fstat(self._root_fd)
        except OSError as exc:
            raise ProviderStateV2Error("v2 provider root is unavailable") from exc
        if (
            not stat.S_ISDIR(path_stat.st_mode)
            or stat.S_ISLNK(path_stat.st_mode)
            or (path_stat.st_dev, path_stat.st_ino) != self._root_identity
            or (fd_stat.st_dev, fd_stat.st_ino) != self._root_identity
            or stat.S_IMODE(path_stat.st_mode) != 0o700
            or (hasattr(os, "getuid") and path_stat.st_uid != os.getuid())
        ):
            raise ProviderStateV2Error("v2 provider root identity changed")

    def _ensure_private_file(self, name: str) -> None:
        self._verify_root()
        flags = os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | os.O_NOFOLLOW
        try:
            descriptor = os.open(name, flags, 0o600, dir_fd=self._root_fd)
        except FileExistsError:
            self._verify_private_file(name)
            return
        except OSError as exc:
            raise ProviderStateV2Error(f"could not create private v2 file {name}") from exc
        try:
            os.fchmod(descriptor, 0o600)
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        os.fsync(self._root_fd)

    @staticmethod
    def _validate_private_file(name: str, metadata: os.stat_result) -> os.stat_result:
        if (
            not stat.S_ISREG(metadata.st_mode)
            or stat.S_ISLNK(metadata.st_mode)
            or metadata.st_nlink != 1
            or stat.S_IMODE(metadata.st_mode) != 0o600
            or (hasattr(os, "getuid") and metadata.st_uid != os.getuid())
        ):
            raise ProviderStateV2Error(f"private v2 file {name} is unsafe")
        return metadata

    def _verify_private_file(self, name: str) -> os.stat_result:
        self._verify_root()
        try:
            metadata = os.stat(name, dir_fd=self._root_fd, follow_symlinks=False)
        except OSError as exc:
            raise ProviderStateV2Error(f"private v2 file {name} is unavailable") from exc
        return self._validate_private_file(name, metadata)

    def _optional_private_file(self, name: str) -> os.stat_result | None:
        self._verify_root()
        try:
            metadata = os.stat(name, dir_fd=self._root_fd, follow_symlinks=False)
        except FileNotFoundError:
            return None
        except OSError as exc:
            raise ProviderStateV2Error(f"SQLite side file {name} is unavailable") from exc
        return self._validate_private_file(name, metadata)

    def _acquire_owner_lock(self) -> None:
        expected = self._verify_private_file(OWNER_LOCK_FILENAME)
        try:
            descriptor = os.open(
                OWNER_LOCK_FILENAME,
                os.O_RDWR | os.O_CLOEXEC | os.O_NOFOLLOW,
                dir_fd=self._root_fd,
            )
        except OSError as exc:
            raise ProviderStateV2Error("v2 owner lock could not be opened") from exc
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            os.close(descriptor)
            raise ProviderStateV2Error("v2 provider root is already owned") from exc
        actual = os.fstat(descriptor)
        if (actual.st_dev, actual.st_ino) != (expected.st_dev, expected.st_ino):
            fcntl.flock(descriptor, fcntl.LOCK_UN)
            os.close(descriptor)
            raise ProviderStateV2Error("v2 owner lock identity changed")
        self._owner_lock_fd = descriptor

    def _verify_storage_files(self) -> None:
        self._verify_root()
        self._verify_private_file(OWNER_LOCK_FILENAME)
        database = self._verify_private_file(DATABASE_FILENAME)
        if (
            hasattr(self, "_database_identity")
            and (
                database.st_dev,
                database.st_ino,
            )
            != self._database_identity
        ):
            raise ProviderStateV2Error("v2 database pathname identity changed")
        if database.st_size > MAX_DATABASE_BYTES:
            raise ProviderStateV2Error("v2 provider database exceeds its byte budget")
        journal = self._optional_private_file(JOURNAL_FILENAME)
        if journal is not None and journal.st_size > MAX_JOURNAL_BYTES:
            raise ProviderStateV2Error("v2 provider journal exceeds its byte budget")
        for name in (WAL_FILENAME, SHM_FILENAME):
            if self._optional_private_file(name) is not None:
                raise ProviderStateV2Error(f"SQLite side file {name} is forbidden")

    def _open_database(self) -> sqlite3.Connection:
        connection = sqlite3.connect(
            self.database_path,
            timeout=30,
            isolation_level=None,
            check_same_thread=False,
        )
        connection.row_factory = sqlite3.Row
        try:
            rows = connection.execute("PRAGMA database_list").fetchall()
            if len(rows) != 1:
                raise ProviderStateV2Error("SQLite opened an unexpected database set")
            self._verify_sqlite_identity(rows[0][2])
            connection.execute("PRAGMA foreign_keys = ON")
            connection.execute("PRAGMA busy_timeout = 30000")
            mode = connection.execute("PRAGMA journal_mode = DELETE").fetchone()[0]
            if mode != "delete":
                raise ProviderStateV2Error("SQLite rollback journal mode is unavailable")
            connection.execute("PRAGMA synchronous = FULL")
            connection.execute("PRAGMA temp_store = MEMORY")
            connection.execute("PRAGMA trusted_schema = OFF")
            journal_limit = cast(
                int,
                connection.execute(f"PRAGMA journal_size_limit = {MAX_JOURNAL_BYTES}").fetchone()[
                    0
                ],
            )
            if journal_limit != MAX_JOURNAL_BYTES:
                raise ProviderStateV2Error("SQLite journal size limit could not be enforced")
            page_size = cast(int, connection.execute("PRAGMA page_size").fetchone()[0])
            if not 512 <= page_size <= 65_536:
                raise ProviderStateV2Error("SQLite page size is outside v2 bounds")
            max_pages = MAX_DATABASE_BYTES // page_size
            configured = connection.execute(f"PRAGMA max_page_count = {max_pages}").fetchone()[0]
            if configured != max_pages:
                raise ProviderStateV2Error("SQLite max page count could not be enforced")
            self._verify_sqlite_identity(rows[0][2])
        except BaseException:
            connection.close()
            raise
        return connection

    def _verify_sqlite_identity(self, opened_path: object) -> None:
        if type(opened_path) is not str or not os.path.isabs(opened_path):
            raise ProviderStateV2Error("SQLite returned an invalid v2 database path")
        try:
            opened = os.stat(opened_path, follow_symlinks=False)
        except OSError as exc:
            raise ProviderStateV2Error("SQLite database identity is unavailable") from exc
        self._validate_private_file(DATABASE_FILENAME, opened)
        managed = self._verify_private_file(DATABASE_FILENAME)
        expected = self._database_identity
        if (opened.st_dev, opened.st_ino) != expected or (
            managed.st_dev,
            managed.st_ino,
        ) != expected:
            raise ProviderStateV2Error("SQLite opened an unexpected v2 database inode")

    def _migrate(self) -> None:
        connection = self._connection
        try:
            connection.execute("BEGIN EXCLUSIVE")
            version = cast(int, connection.execute("PRAGMA user_version").fetchone()[0])
            timestamp = self._timestamp()
            if version == 0:
                if _schema_rows(connection):
                    raise ProviderSchemaV2Error("unversioned v2 database is not empty")
                for statement in (
                    *_SCHEMA_V1_STATEMENTS,
                    *_SCHEMA_V2_ADDITIONS,
                    *_SCHEMA_V3_ADDITIONS,
                ):
                    connection.execute(statement)
                _migration_checkpoint("fresh_after_ddl")
                connection.execute(
                    "INSERT INTO schema_metadata VALUES (1, ?, 3, ?, ?)",
                    (STORE_NAMESPACE, EXPECTED_SCHEMA_V3_SHA256, timestamp),
                )
                connection.executemany(
                    "INSERT INTO schema_migrations(version, applied_at) VALUES (?, ?)",
                    ((1, timestamp), (2, timestamp), (3, timestamp)),
                )
                connection.execute(
                    "INSERT INTO lifecycle_cursor_key VALUES (1, ?, ?)",
                    (secrets.token_bytes(32), timestamp),
                )
                connection.execute("PRAGMA user_version = 3")
            elif version == 1:
                self._validate_schema_version(
                    connection,
                    expected_rows=_EXPECTED_SCHEMA_V1_ROWS,
                    expected_sha256=EXPECTED_SCHEMA_V1_SHA256,
                    version=1,
                )
                for statement in _SCHEMA_V2_ADDITIONS:
                    connection.execute(statement)
                _migration_checkpoint("v1_to_v2_after_ddl")
                connection.execute(
                    "UPDATE schema_metadata SET schema_version = 2, schema_sha256 = ?",
                    (EXPECTED_SCHEMA_V2_SHA256,),
                )
                connection.execute(
                    "INSERT INTO schema_migrations(version, applied_at) VALUES (2, ?)",
                    (timestamp,),
                )
                connection.execute("PRAGMA user_version = 2")
                self._validate_schema_version(
                    connection,
                    expected_rows=_EXPECTED_SCHEMA_V2_ROWS,
                    expected_sha256=EXPECTED_SCHEMA_V2_SHA256,
                    version=2,
                )
                version = 2
            if version == 2:
                self._validate_schema_version(
                    connection,
                    expected_rows=_EXPECTED_SCHEMA_V2_ROWS,
                    expected_sha256=EXPECTED_SCHEMA_V2_SHA256,
                    version=2,
                )
                for statement in _SCHEMA_V3_ADDITIONS:
                    connection.execute(statement)
                _migration_checkpoint("v2_to_v3_after_ddl")
                connection.execute(
                    "UPDATE schema_metadata SET schema_version = 3, schema_sha256 = ?",
                    (EXPECTED_SCHEMA_V3_SHA256,),
                )
                connection.execute(
                    "INSERT INTO schema_migrations(version, applied_at) VALUES (3, ?)",
                    (timestamp,),
                )
                connection.execute(
                    "INSERT INTO lifecycle_cursor_key VALUES (1, ?, ?)",
                    (secrets.token_bytes(32), timestamp),
                )
                connection.execute("PRAGMA user_version = 3")
            elif version not in {0, SCHEMA_VERSION}:
                raise ProviderSchemaV2Error(f"unsupported v2 provider schema version {version}")
            self._validate_schema_version(
                connection,
                expected_rows=_EXPECTED_SCHEMA_V3_ROWS,
                expected_sha256=EXPECTED_SCHEMA_V3_SHA256,
                version=3,
            )
            self._verify_storage_files()
            connection.commit()
        except (ProviderStoreV2Error, RuntimeError):
            connection.rollback()
            raise
        except sqlite3.DatabaseError as exc:
            connection.rollback()
            raise ProviderSchemaV2Error("v2 schema migration failed") from exc
        except BaseException:
            connection.rollback()
            raise
        os.fsync(self._root_fd)

    def _validate_schema_version(
        self,
        connection: sqlite3.Connection,
        *,
        expected_rows: tuple[tuple[object, ...], ...],
        expected_sha256: str,
        version: int,
    ) -> None:
        actual = _schema_rows(connection)
        actual_sha256 = hashlib.sha256(_canonical_json_bytes(actual)).hexdigest()
        if actual != expected_rows or actual_sha256 != expected_sha256:
            raise ProviderSchemaV2Error("v2 provider schema fingerprint changed")
        metadata = connection.execute(
            "SELECT namespace, schema_version, schema_sha256, created_at FROM schema_metadata"
        ).fetchall()
        if (
            len(metadata) != 1
            or metadata[0][0] != STORE_NAMESPACE
            or metadata[0][1] != version
            or metadata[0][2] != expected_sha256
            or _TIMESTAMP_RE.fullmatch(metadata[0][3]) is None
        ):
            raise ProviderSchemaV2Error("v2 schema metadata is invalid")
        migrations = connection.execute(
            "SELECT version, applied_at FROM schema_migrations ORDER BY version"
        ).fetchall()
        if [row[0] for row in migrations] != list(range(1, version + 1)) or any(
            _TIMESTAMP_RE.fullmatch(row[1]) is None for row in migrations
        ):
            raise ProviderSchemaV2Error("v2 migration history is invalid")

    def _recover_and_validate(self) -> None:
        with self._transaction(write=True, operation="recoverProviderV2") as connection:
            log_summary = connection.execute(
                """
                SELECT count(*), coalesce(max(length(CAST(text AS BLOB))), 0),
                       coalesce(sum(length(CAST(text AS BLOB))), 0),
                       coalesce(sum(text_bytes), 0)
                FROM lifecycle_operation_logs
                """
            ).fetchone()
            if log_summary is None or any(
                type(value) is not int for value in cast(sqlite3.Row, log_summary)
            ):
                raise ProviderDataV2Error("lifecycle log summary is invalid")
            log_count = cast(int, log_summary[0])
            maximum_log_bytes = cast(int, log_summary[1])
            actual_log_bytes = cast(int, log_summary[2])
            recorded_log_bytes = cast(int, log_summary[3])
            if (
                log_count < 0
                or maximum_log_bytes > MAX_LIFECYCLE_LOG_ENTRY_BYTES
                or actual_log_bytes != recorded_log_bytes
                or actual_log_bytes > MAX_LIFECYCLE_GLOBAL_LOG_BYTES
            ):
                raise ProviderDataV2Error("lifecycle log bytes exceed recovery bounds")
            if actual_log_bytes > self._max_lifecycle_global_log_bytes:
                raise ProviderCapacityConfigurationV2Error(
                    "persisted lifecycle logs exceed configured global capacity"
                )
            integrity = connection.execute("PRAGMA integrity_check(1)").fetchone()
            if integrity is None or integrity[0] != "ok":
                raise ProviderDataV2Error("v2 provider integrity check failed")
            counts = {
                table: cast(
                    int,
                    connection.execute(f"SELECT count(*) FROM {table}").fetchone()[0],
                )
                for table in (
                    "profiles",
                    "project_drafts",
                    "idempotency_records",
                    "migration_diagnostics",
                )
            }
            limits = {
                "profiles": self._max_profiles,
                "project_drafts": self._max_drafts,
                "idempotency_records": self._max_idempotency_records,
                "migration_diagnostics": self._max_migration_diagnostics,
            }
            for table, count in counts.items():
                if count > limits[table]:
                    raise ProviderCapacityConfigurationV2Error(
                        f"persisted {table} exceeds configured capacity"
                    )
            recoverable_lifecycle_count = cast(
                int,
                connection.execute(
                    """
                    SELECT count(*)
                    FROM lifecycle_operations AS operation
                    LEFT JOIN lifecycle_reconciliation_acknowledgements AS acknowledgement
                      ON acknowledgement.operation_id = operation.operation_id
                    WHERE operation.status IN ('queued', 'running')
                       OR acknowledgement.operation_id IS NULL
                    """
                ).fetchone()[0],
            )
            if recoverable_lifecycle_count > self._max_lifecycle_operations:
                raise ProviderCapacityConfigurationV2Error(
                    "persisted lifecycle operations exceed configured capacity"
                )
            aggregate = cast(
                int,
                connection.execute(
                    """
                    SELECT
                        coalesce((SELECT sum(
                            length(CAST(profile_id AS BLOB)) +
                            length(CAST(profile_kind AS BLOB)) +
                            length(CAST(document_json AS BLOB)) +
                            coalesce(length(CAST(legacy_source_ref_sha256 AS BLOB)), 0) +
                            coalesce(length(CAST(legacy_source_document_sha256 AS BLOB)), 0) +
                            coalesce(length(CAST(rebound_from_sha256 AS BLOB)), 0) +
                            length(CAST(created_at AS BLOB)) +
                            length(CAST(updated_at AS BLOB))
                        ) FROM profiles), 0) +
                        coalesce((SELECT sum(
                            length(CAST(draft_id AS BLOB)) +
                            length(CAST(profile_id AS BLOB)) +
                            length(CAST(document_json AS BLOB)) +
                            length(CAST(config_json AS BLOB)) +
                            length(CAST(project_config_sha256 AS BLOB)) +
                            length(CAST(legacy_source_ref_sha256 AS BLOB)) +
                            length(CAST(legacy_source_document_sha256 AS BLOB)) +
                            length(CAST(created_at AS BLOB)) +
                            length(CAST(updated_at AS BLOB))
                        ) FROM project_drafts), 0) +
                        coalesce((SELECT sum(
                            length(CAST(principal AS BLOB)) +
                            length(CAST(operation AS BLOB)) +
                            length(CAST(resource_scope AS BLOB)) +
                            length(CAST(idempotency_key AS BLOB)) +
                            length(CAST(request_sha256 AS BLOB)) +
                            length(CAST(response_kind AS BLOB)) +
                            length(CAST(response_json AS BLOB)) +
                            length(CAST(created_at AS BLOB))
                        ) FROM idempotency_records), 0) +
                        coalesce((SELECT sum(
                            length(CAST(diagnostic_id AS BLOB)) +
                            length(CAST(code AS BLOB)) +
                            length(CAST(source_kind AS BLOB)) +
                            coalesce(length(CAST(source_ref_sha256 AS BLOB)), 0) +
                            length(CAST(created_at AS BLOB))
                        ) FROM migration_diagnostics), 0)
                    """
                ).fetchone()[0],
            )
            if aggregate > MAX_RECOVERY_BYTES:
                raise ProviderDataV2Error("v2 provider rows exceed recovery byte budget")
            lifecycle_aggregate = cast(
                int,
                connection.execute(
                    """
                    SELECT
                        coalesce((SELECT sum(
                            length(CAST(operation_id AS BLOB)) +
                            length(CAST(kind AS BLOB)) +
                            length(CAST(resource_kind AS BLOB)) +
                            length(CAST(resource_id AS BLOB)) +
                            length(CAST(request_sha256 AS BLOB)) +
                            length(CAST(request_json AS BLOB)) +
                            length(CAST(phase_plan_json AS BLOB)) +
                            length(CAST(status AS BLOB)) +
                            length(CAST(phase AS BLOB)) +
                            length(CAST(progress_json AS BLOB)) +
                            coalesce(length(CAST(result_json AS BLOB)), 0) +
                            coalesce(length(CAST(failure_json AS BLOB)), 0) +
                            length(CAST(created_at AS BLOB)) +
                            coalesce(length(CAST(started_at AS BLOB)), 0) +
                            length(CAST(updated_at AS BLOB)) +
                            coalesce(length(CAST(finished_at AS BLOB)), 0) +
                            length(CAST(etag AS BLOB))
                        ) FROM lifecycle_operations), 0) +
                        coalesce((SELECT sum(
                            length(CAST(principal AS BLOB)) +
                            length(CAST(action AS BLOB)) +
                            length(CAST(resource_scope AS BLOB)) +
                            length(CAST(idempotency_key AS BLOB)) +
                            length(CAST(request_sha256 AS BLOB)) +
                            length(CAST(operation_id AS BLOB)) +
                            length(CAST(created_at AS BLOB))
                        ) FROM lifecycle_idempotency_records), 0) +
                        coalesce((SELECT sum(
                            length(CAST(operation_id AS BLOB)) +
                            length(CAST(terminal_status AS BLOB)) +
                            length(CAST(terminal_etag AS BLOB)) +
                            length(CAST(request_sha256 AS BLOB)) +
                            length(CAST(idempotency_key AS BLOB)) +
                            length(CAST(acknowledged_at AS BLOB))
                        ) FROM lifecycle_reconciliation_acknowledgements), 0)
                    """
                ).fetchone()[0],
            )
            if lifecycle_aggregate > MAX_LIFECYCLE_AUTHORITY_RECOVERY_BYTES:
                raise ProviderDataV2Error("lifecycle operation rows exceed recovery byte budget")
            for row in connection.execute(
                f"SELECT {_PROFILE_SELECT_COLUMNS} FROM profiles ORDER BY rowid"
            ):
                self._profile_from_row(cast(sqlite3.Row, row))
            for row in connection.execute(
                f"SELECT {_DRAFT_SELECT_COLUMNS} FROM project_drafts ORDER BY rowid"
            ):
                self._draft_from_row(cast(sqlite3.Row, row))
            invalid_draft_owner = connection.execute(
                """
                SELECT draft.draft_id
                FROM project_drafts AS draft
                LEFT JOIN profiles AS profile ON profile.profile_id = draft.profile_id
                WHERE profile.profile_id IS NULL
                   OR profile.profile_kind != 'system_openssh'
                LIMIT 1
                """
            ).fetchone()
            if invalid_draft_owner is not None:
                raise ProviderDataV2Error("v2 draft is not bound to a system OpenSSH profile")
            for row in connection.execute(
                f"SELECT {_IDEMPOTENCY_SELECT_COLUMNS} FROM idempotency_records ORDER BY rowid"
            ):
                self._idempotent_response_from_row(
                    connection,
                    cast(sqlite3.Row, row),
                )
            for row in connection.execute(
                "SELECT * FROM migration_diagnostics ORDER BY diagnostic_id"
            ):
                self._diagnostic_from_row(cast(sqlite3.Row, row))
            self._validate_lifecycle_recovery(connection)
            self._cleanup_acknowledged_lifecycle_operations(connection)

    def _validate_lifecycle_recovery(self, connection: sqlite3.Connection) -> None:
        operation_rows: dict[str, sqlite3.Row] = {}
        operations: dict[str, m.LifecycleOperationV2] = {}
        active_resources: set[tuple[str, str]] = set()
        for raw_row in connection.execute(
            f"""
            SELECT {_LIFECYCLE_OPERATION_SELECT_COLUMNS}
            FROM lifecycle_operations
            ORDER BY rowid
            """
        ):
            row = cast(sqlite3.Row, raw_row)
            operation = self._lifecycle_operation_from_row(row)
            if operation.operation_id in operations:
                raise ProviderDataV2Error("duplicate lifecycle operation identity")
            if operation.kind == "profile_disconnect" and (
                operation.cancellable
                or cast(int, row["cancellation_requested"]) != 0
                or operation.status == "cancelled"
            ):
                raise ProviderDataV2Error(
                    "profile disconnect crossed an invalid cancellation boundary"
                )
            if operation.status in {"queued", "running"}:
                resource = (
                    operation.resource.resource_kind,
                    operation.resource.resource_id,
                )
                if resource in active_resources:
                    raise ProviderDataV2Error(
                        "multiple lifecycle operations own one active resource"
                    )
                active_resources.add(resource)
            operation_rows[operation.operation_id] = row
            operations[operation.operation_id] = operation

        active_disconnects = {
            operation.resource.resource_id
            for operation in operations.values()
            if operation.status in {"queued", "running"}
            and operation.kind == "profile_disconnect"
        }
        if active_disconnects:
            active_project_profiles: dict[str, set[str]] = {}
            for raw_profile in connection.execute(
                f"SELECT {_PROFILE_SELECT_COLUMNS} FROM profiles ORDER BY profile_id"
            ):
                profile = self._profile_from_row(cast(sqlite3.Row, raw_profile))
                if (
                    isinstance(profile, m.RemoteWorkspaceProfileV2)
                    and profile.active_project_id is not None
                ):
                    active_project_profiles.setdefault(profile.active_project_id, set()).add(
                        profile.profile_id
                    )
            for operation_id, operation in operations.items():
                if operation.status not in {"queued", "running"} or operation.kind not in {
                    "project_create",
                    "project_activate",
                }:
                    continue
                request = self._lifecycle_request_from_row(operation_rows[operation_id])
                profile_ids = (
                    {request.request.profile_id}
                    if isinstance(request, LifecycleProjectCreateRequestV2)
                    else active_project_profiles.get(request.project_id, set())
                )
                if profile_ids & active_disconnects:
                    raise ProviderDataV2Error(
                        "profile disconnect conflicts with dependent lifecycle work"
                    )

        grouped_logs: dict[str, tuple[int, int, int, int, int]] = {}
        for raw_row in connection.execute(
            """
            SELECT operation_id, count(*) AS entry_count,
                   min(sequence) AS minimum_sequence,
                   max(sequence) AS maximum_sequence,
                   coalesce(sum(text_bytes), 0) AS recorded_bytes,
                   coalesce(sum(length(CAST(text AS BLOB))), 0) AS actual_bytes
            FROM lifecycle_operation_logs
            GROUP BY operation_id
            ORDER BY operation_id
            """
        ):
            row = cast(sqlite3.Row, raw_row)
            operation_id = row["operation_id"]
            values = (
                row["entry_count"],
                row["minimum_sequence"],
                row["maximum_sequence"],
                row["recorded_bytes"],
                row["actual_bytes"],
            )
            if (
                type(operation_id) is not str
                or operation_id not in operations
                or any(type(value) is not int for value in values)
            ):
                raise ProviderDataV2Error("lifecycle log ownership is invalid")
            grouped_logs[operation_id] = cast(tuple[int, int, int, int, int], values)

        grouped_count = 0
        grouped_bytes = 0
        for operation_id, operation in operations.items():
            row = operation_rows[operation_id]
            dropped = cast(int, row["dropped_before_sequence"])
            byte_count = cast(int, row["log_byte_count"])
            stats = grouped_logs.get(operation_id)
            if stats is None:
                if dropped != operation.log_sequence_high_watermark or byte_count != 0:
                    raise ProviderDataV2Error("lifecycle empty-log authority differs")
                continue
            entry_count, minimum, maximum, recorded_bytes, actual_bytes = stats
            grouped_count += entry_count
            grouped_bytes += actual_bytes
            if entry_count > self._max_lifecycle_log_entries:
                raise ProviderCapacityConfigurationV2Error(
                    "persisted lifecycle log entries exceed configured capacity"
                )
            if actual_bytes > self._max_lifecycle_log_bytes:
                raise ProviderCapacityConfigurationV2Error(
                    "persisted lifecycle log bytes exceed configured capacity"
                )
            if (
                entry_count < 1
                or minimum != dropped + 1
                or maximum != operation.log_sequence_high_watermark
                or entry_count != maximum - dropped
                or recorded_bytes != actual_bytes
                or byte_count != actual_bytes
            ):
                raise ProviderDataV2Error("lifecycle log sequence authority differs")
        if grouped_count != cast(
            int,
            connection.execute("SELECT count(*) FROM lifecycle_operation_logs").fetchone()[0],
        ) or grouped_bytes != cast(
            int,
            connection.execute(
                "SELECT coalesce(sum(text_bytes), 0) FROM lifecycle_operation_logs"
            ).fetchone()[0],
        ):
            raise ProviderDataV2Error("lifecycle aggregate log authority differs")

        for raw_row in connection.execute(
            f"""
            SELECT {_LIFECYCLE_LOG_SELECT_COLUMNS}
            FROM lifecycle_operation_logs
            ORDER BY operation_id, sequence
            """
        ):
            row = cast(sqlite3.Row, raw_row)
            entry = self._lifecycle_log_from_row(row)
            operation = operations.get(entry.operation_id)
            if operation is None or not (
                operation.created_at <= entry.occurred_at <= operation.updated_at
            ):
                raise ProviderDataV2Error("lifecycle log timestamp is outside its operation")

        reservation_counts = {operation_id: 0 for operation_id in operations}
        cancellation_counts = {operation_id: 0 for operation_id in operations}
        for raw_row in connection.execute(
            """
            SELECT principal, action, resource_scope, idempotency_key,
                   request_sha256, operation_id, created_at
            FROM lifecycle_idempotency_records
            ORDER BY rowid
            """
        ):
            row = cast(sqlite3.Row, raw_row)
            operation_id = row["operation_id"]
            operation = operations.get(operation_id)
            if (
                operation is None
                or row["principal"] != LOCAL_PRINCIPAL
                or row["action"] not in {"reserve", "cancel"}
                or not self._is_digest(row["request_sha256"])
                or type(row["created_at"]) is not str
                or _TIMESTAMP_RE.fullmatch(row["created_at"]) is None
                or not operation.created_at <= row["created_at"] <= operation.updated_at
            ):
                raise ProviderDataV2Error("stored lifecycle idempotency identity is invalid")
            try:
                self._validate_idempotency_key(row["idempotency_key"])
            except ProviderContractV2Error as exc:
                raise ProviderDataV2Error("stored lifecycle idempotency key is invalid") from exc
            if row["action"] == "reserve":
                if (
                    row["resource_scope"] != "lifecycle_operations"
                    or row["request_sha256"] != operation.request_sha256
                ):
                    raise ProviderDataV2Error(
                        "lifecycle reservation idempotency authority differs"
                    )
                reservation_counts[operation_id] += 1
            else:
                expected_cancellation_sha256 = hashlib.sha256(
                    _canonical_json_bytes({"operation_id": operation_id})
                ).hexdigest()
                if (
                    row["resource_scope"] != operation_id
                    or not hmac.compare_digest(
                        row["request_sha256"],
                        expected_cancellation_sha256,
                    )
                ):
                    raise ProviderDataV2Error(
                        "lifecycle cancellation idempotency authority differs"
                    )
                cancellation_counts[operation_id] += 1
        if any(count != 1 for count in reservation_counts.values()):
            raise ProviderDataV2Error("lifecycle operation lacks one reservation authority")
        if any(
            cancellation_counts[operation_id]
            != cast(int, operation_rows[operation_id]["cancellation_requested"])
            for operation_id in operations
        ):
            raise ProviderDataV2Error(
                "lifecycle cancellation record differs from operation state"
            )

        for raw_row in connection.execute(
            """
            SELECT operation_id, terminal_status, terminal_etag,
                   request_sha256, idempotency_key, acknowledged_at
            FROM lifecycle_reconciliation_acknowledgements
            ORDER BY operation_id
            """
        ):
            row = cast(sqlite3.Row, raw_row)
            operation = operations.get(row["operation_id"])
            if operation is None:
                raise ProviderDataV2Error("lifecycle acknowledgement is orphaned")
            try:
                self._validate_idempotency_key(row["idempotency_key"])
            except ProviderContractV2Error as exc:
                raise ProviderDataV2Error(
                    "stored lifecycle acknowledgement key is invalid"
                ) from exc
            if operation.status not in {"succeeded", "failed", "cancelled"}:
                raise ProviderDataV2Error(
                    "lifecycle acknowledgement belongs to a nonterminal operation"
                )
            expected_request = m.LifecycleAcknowledgeV2(
                expected_operation_id=operation.operation_id,
                expected_terminal_status=cast(
                    Literal["succeeded", "failed", "cancelled"],
                    operation.status,
                ),
            )
            expected_sha256 = hashlib.sha256(_canonical_json_bytes(expected_request)).hexdigest()
            if (
                row["terminal_status"] != operation.status
                or row["terminal_etag"] != operation.etag
                or not hmac.compare_digest(row["request_sha256"], expected_sha256)
                or type(row["acknowledged_at"]) is not str
                or _TIMESTAMP_RE.fullmatch(row["acknowledged_at"]) is None
                or operation.finished_at is None
                or row["acknowledged_at"] < operation.finished_at
            ):
                raise ProviderDataV2Error("lifecycle acknowledgement authority differs")

        self._lifecycle_cursor_key(connection)

    @contextmanager
    def _transaction(
        self,
        *,
        write: bool,
        operation: str,
    ) -> Iterator[sqlite3.Connection]:
        with self._lock:
            self._verify_storage_files()
            connection = self._connection
            try:
                connection.execute("BEGIN IMMEDIATE" if write else "BEGIN")
                yield connection
                connection.commit()
                self._verify_storage_files()
            except BaseException:
                connection.rollback()
                raise
        if write:
            _post_commit_checkpoint(operation)

    def _execute_idempotent(
        self,
        *,
        operation: str,
        resource_scope: str,
        idempotency_key: str,
        request_value: object,
        response_kind: Literal["profile", "draft"],
        mutation: Callable[[sqlite3.Connection], BaseModel],
    ) -> BaseModel:
        self._validate_idempotency_key(idempotency_key)
        request_sha256 = hashlib.sha256(_canonical_json_bytes(request_value)).hexdigest()
        with self._transaction(write=True, operation=operation) as connection:
            existing = connection.execute(
                f"""
                SELECT {_IDEMPOTENCY_SELECT_COLUMNS} FROM idempotency_records
                WHERE principal = ? AND operation = ? AND resource_scope = ?
                  AND idempotency_key = ?
                """,
                (LOCAL_PRINCIPAL, operation, resource_scope, idempotency_key),
            ).fetchone()
            if existing is not None:
                row = cast(sqlite3.Row, existing)
                if (
                    not hmac.compare_digest(row["request_sha256"], request_sha256)
                    or row["response_kind"] != response_kind
                ):
                    raise ProviderIdempotencyConflictV2(
                        "idempotency key was reused for another v2 request"
                    )
                return self._idempotent_response_from_row(connection, row)
            count = cast(
                int,
                connection.execute("SELECT count(*) FROM idempotency_records").fetchone()[0],
            )
            if count >= self._max_idempotency_records:
                raise ProviderCapacityV2Error("v2 idempotency capacity is full")
            response = mutation(connection)
            response_bytes = _canonical_json_bytes(response)
            if len(response_bytes) > MAX_IDEMPOTENT_RESPONSE_BYTES:
                raise ProviderCapacityV2Error("v2 idempotent response exceeds its byte bound")
            response_resource_version = self._response_resource_version(
                connection,
                response=response,
                response_kind=response_kind,
            )
            connection.execute(
                """
                INSERT INTO idempotency_records(
                    principal, operation, resource_scope, idempotency_key,
                    request_sha256, response_kind, response_resource_version,
                    response_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    LOCAL_PRINCIPAL,
                    operation,
                    resource_scope,
                    idempotency_key,
                    request_sha256,
                    response_kind,
                    response_resource_version,
                    response_bytes,
                    self._timestamp(),
                ),
            )
            return response

    def _idempotent_response_from_row(
        self,
        connection: sqlite3.Connection,
        row: sqlite3.Row,
    ) -> BaseModel:
        size = self._bounded_blob_size(
            row,
            "response_json",
            maximum=MAX_IDEMPOTENT_RESPONSE_BYTES,
        )
        raw = bytes(row["response_json"])
        if len(raw) != size:
            raise ProviderDataV2Error("idempotent response length changed")
        try:
            if row["response_kind"] == "profile":
                value = _PROFILE_ADAPTER.validate_json(raw, strict=True)
            elif row["response_kind"] == "draft":
                value = LocalProjectDraftV2.model_validate_json(raw)
            else:
                raise ProviderDataV2Error("idempotent response kind is invalid")
        except ValidationError as exc:
            raise ProviderDataV2Error("idempotent response is invalid") from exc
        if _canonical_json_bytes(value) != raw:
            raise ProviderDataV2Error("idempotent response is not canonical")
        self._validate_idempotency_authority(connection, row=row, response=value)
        return value

    def _response_resource_version(
        self,
        connection: sqlite3.Connection,
        *,
        response: BaseModel,
        response_kind: Literal["profile", "draft"],
    ) -> int:
        if response_kind == "profile" and isinstance(
            response,
            (m.RemoteWorkspaceProfileV2, m.LegacyExplicitProfileV2),
        ):
            row = connection.execute(
                "SELECT resource_version FROM profiles WHERE profile_id = ?",
                (response.profile_id,),
            ).fetchone()
            resource_id = response.profile_id
        elif response_kind == "draft" and isinstance(response, LocalProjectDraftV2):
            row = connection.execute(
                "SELECT resource_version FROM project_drafts WHERE draft_id = ?",
                (response.draft_id,),
            ).fetchone()
            resource_id = response.draft_id
        else:
            raise ProviderStoreV2Error(
                "idempotent mutation returned the wrong closed response type"
            )
        if row is None:
            raise ProviderStoreV2Error("idempotent response has no authoritative local resource")
        version = row[0]
        if (
            type(version) is not int
            or not 1 <= version <= m.MAX_JAVASCRIPT_SAFE_INTEGER
            or response.etag != self._etag(response_kind, resource_id, version)
        ):
            raise ProviderStoreV2Error("idempotent response differs from its resource version")
        return version

    def _validate_idempotency_authority(
        self,
        connection: sqlite3.Connection,
        *,
        row: sqlite3.Row,
        response: BaseModel,
    ) -> None:
        operation = row["operation"]
        expected_kinds = {
            "createSystemOpenSshProfileV2": "profile",
            "renameProfileV2": "profile",
            "rebindLegacyProfileV2": "profile",
            "connectProfileV2": "profile",
            "disconnectProfileV2": "profile",
            "reviewHostKeyV2": "profile",
            "copyLegacyDraftV2": "draft",
        }
        if (
            row["principal"] != LOCAL_PRINCIPAL
            or type(operation) is not str
            or operation not in expected_kinds
            or row["response_kind"] != expected_kinds[operation]
            or not self._is_digest(row["request_sha256"])
            or type(row["created_at"]) is not str
            or _TIMESTAMP_RE.fullmatch(row["created_at"]) is None
        ):
            raise ProviderDataV2Error("stored idempotency identity is invalid")
        try:
            self._validate_idempotency_key(row["idempotency_key"])
        except ProviderContractV2Error as exc:
            raise ProviderDataV2Error("stored idempotency key is invalid") from exc

        version = row["response_resource_version"]
        response_kind = cast(Literal["profile", "draft"], row["response_kind"])
        if type(version) is not int or not 1 <= version <= m.MAX_JAVASCRIPT_SAFE_INTEGER:
            raise ProviderDataV2Error("stored idempotency resource version is invalid")
        if response_kind == "profile" and isinstance(
            response,
            (m.RemoteWorkspaceProfileV2, m.LegacyExplicitProfileV2),
        ):
            resource_id = response.profile_id
            current_row = connection.execute(
                f"SELECT {_PROFILE_SELECT_COLUMNS} FROM profiles WHERE profile_id = ?",
                (resource_id,),
            ).fetchone()
            if current_row is None:
                raise ProviderDataV2Error(
                    "idempotent profile response has no authoritative resource"
                )
            typed_current_row = cast(sqlite3.Row, current_row)
            current = self._profile_from_row(typed_current_row)
        elif response_kind == "draft" and isinstance(response, LocalProjectDraftV2):
            resource_id = response.draft_id
            current_row = connection.execute(
                f"SELECT {_DRAFT_SELECT_COLUMNS} FROM project_drafts WHERE draft_id = ?",
                (resource_id,),
            ).fetchone()
            if current_row is None:
                raise ProviderDataV2Error(
                    "idempotent draft response has no authoritative resource"
                )
            typed_current_row = cast(sqlite3.Row, current_row)
            current = self._draft_from_row(typed_current_row)
        else:
            raise ProviderDataV2Error("stored idempotency response has the wrong closed type")
        current_version = typed_current_row["resource_version"]
        if (
            response.etag != self._etag(response_kind, resource_id, version)
            or type(current_version) is not int
            or current_version < version
            or (current_version == version and current != response)
        ):
            raise ProviderDataV2Error(
                "stored idempotency response differs from resource authority"
            )

        scope = row["resource_scope"]
        if operation == "createSystemOpenSshProfileV2":
            valid_binding = (
                scope == "profiles"
                and isinstance(response, m.RemoteWorkspaceProfileV2)
                and typed_current_row["rebound_from_sha256"] is None
            )
        elif operation in {
            "renameProfileV2",
            "connectProfileV2",
            "disconnectProfileV2",
            "reviewHostKeyV2",
        }:
            valid_binding = scope == resource_id
        elif operation == "rebindLegacyProfileV2":
            legacy_row = connection.execute(
                f"SELECT {_PROFILE_SELECT_COLUMNS} FROM profiles WHERE profile_id = ?",
                (scope,),
            ).fetchone()
            if legacy_row is None:
                valid_binding = False
            else:
                typed_legacy_row = cast(sqlite3.Row, legacy_row)
                legacy = self._profile_from_row(typed_legacy_row)
                valid_binding = (
                    isinstance(legacy, m.LegacyExplicitProfileV2)
                    and isinstance(response, m.RemoteWorkspaceProfileV2)
                    and typed_current_row["rebound_from_sha256"]
                    == typed_legacy_row["legacy_source_ref_sha256"]
                )
        else:
            valid_binding = (
                isinstance(response, LocalProjectDraftV2)
                and scope == response.legacy_source_ref_sha256
            )
        if not valid_binding:
            raise ProviderDataV2Error(
                "stored idempotency response differs from its operation scope"
            )

    def _transition_profile_for_lifecycle(
        self,
        connection: sqlite3.Connection,
        request: LifecycleRequestV2,
    ) -> m.RemoteWorkspaceProfileV2:
        if not isinstance(
            request,
            (
                LifecycleProfileConnectRequestV2,
                LifecycleProfileDisconnectRequestV2,
                LifecycleHostKeyReviewRequestV2,
            ),
        ):
            raise ProviderContractV2Error("lifecycle request is not a profile action")
        row = self._require_profile_row(connection, request.profile_id)
        current = self._profile_from_row(row)
        if not isinstance(current, m.RemoteWorkspaceProfileV2):
            raise ProviderConflictV2("legacy profiles must be rebound before connection")
        if (
            request.resource_generation < 1
            or current.connection_generation != request.resource_generation
            or request.request.expected_connection_generation != request.resource_generation
        ):
            raise ProviderPreconditionFailedV2("profile connection generation changed")
        if not hmac.compare_digest(current.etag, request.if_match):
            raise ProviderPreconditionFailedV2("profile ETag changed")
        if isinstance(request, LifecycleHostKeyReviewRequestV2):
            self._require_current_host_key_review(current, request.request)
        current_version = row["resource_version"]
        if (
            type(current_version) is not int
            or current_version >= m.MAX_JAVASCRIPT_SAFE_INTEGER
            or current.connection_generation >= m.MAX_JAVASCRIPT_SAFE_INTEGER
        ):
            raise ProviderCapacityV2Error("profile generation is exhausted")
        version = current_version + 1
        generation = current.connection_generation + 1
        timestamp = self._timestamp()
        trust_state: Literal["unverified", "trusted", "repairing"]
        if isinstance(request, LifecycleHostKeyReviewRequestV2):
            trust_state = "repairing"
        elif current.trust.state == "trusted":
            trust_state = "trusted"
        else:
            trust_state = "unverified"
        updated = m.RemoteWorkspaceProfileV2(
            profile_id=current.profile_id,
            display_name=current.display_name,
            ssh_host_alias=current.ssh_host_alias,
            catalog_generation=current.catalog_generation,
            connection_generation=generation,
            connection_state=(
                "disconnecting"
                if isinstance(request, LifecycleProfileDisconnectRequestV2)
                else "connecting"
            ),
            prompt=None,
            trust=m.SshTrustStateV2(
                connection_generation=generation,
                state=trust_state,
                review_id=None,
                review_sha256=None,
                key_fingerprints=[],
                repair_support="not_needed",
            ),
            failure=None,
            active_project_id=current.active_project_id,
            core_api_major=None,
            core_openapi_sha256=None,
            core_event_schema_sha256=None,
            core_registry_sha256=None,
            created_at=current.created_at,
            updated_at=timestamp,
            etag=self._etag("profile", current.profile_id, version),
        )
        self._update_profile(connection, updated, version=version)
        return updated

    def _require_lifecycle_dependencies_available(
        self,
        connection: sqlite3.Connection,
        reservation: LifecycleOperationReservationV2,
    ) -> None:
        request = reservation.request
        if isinstance(request, LifecycleProfileDisconnectRequestV2):
            profile_row = self._require_profile_row(connection, request.profile_id)
            profile = self._profile_from_row(profile_row)
            if not isinstance(profile, m.RemoteWorkspaceProfileV2):
                return
            for raw_row in connection.execute(
                f"""
                SELECT {_LIFECYCLE_OPERATION_SELECT_COLUMNS}
                FROM lifecycle_operations
                WHERE kind IN ('project_create', 'project_activate')
                  AND status IN ('queued', 'running')
                ORDER BY created_at, operation_id
                """
            ):
                project_request = self._lifecycle_request_from_row(
                    cast(sqlite3.Row, raw_row)
                )
                depends_on_profile = (
                    isinstance(project_request, LifecycleProjectCreateRequestV2)
                    and project_request.request.profile_id == profile.profile_id
                ) or (
                    isinstance(project_request, LifecycleProjectActivateRequestV2)
                    and profile.active_project_id == project_request.project_id
                )
                if depends_on_profile:
                    raise ProviderLifecycleResourceBusyV2(profile.profile_id)
            return

        profile_ids: set[str]
        if isinstance(request, LifecycleProjectCreateRequestV2):
            profile_ids = {request.request.profile_id}
        elif isinstance(request, LifecycleProjectActivateRequestV2):
            profile_ids = {
                profile.profile_id
                for raw_profile in connection.execute(
                    f"SELECT {_PROFILE_SELECT_COLUMNS} FROM profiles ORDER BY profile_id"
                )
                if isinstance(
                    profile := self._profile_from_row(cast(sqlite3.Row, raw_profile)),
                    m.RemoteWorkspaceProfileV2,
                )
                and profile.active_project_id == request.project_id
            }
        else:
            return
        if not profile_ids:
            return
        active_disconnects = {
            cast(str, row[0])
            for row in connection.execute(
                """
                SELECT resource_id
                FROM lifecycle_operations
                WHERE kind = 'profile_disconnect'
                  AND status IN ('queued', 'running')
                """
            ).fetchall()
        }
        conflict = profile_ids & active_disconnects
        if conflict:
            raise ProviderLifecycleResourceBusyV2(sorted(conflict)[0])

    def _cancel_profile_lifecycle_transition(
        self,
        connection: sqlite3.Connection,
        operation_row: sqlite3.Row,
        *,
        timestamp: str,
    ) -> None:
        request = self._lifecycle_request_from_row(operation_row)
        if not isinstance(
            request,
            (LifecycleProfileConnectRequestV2, LifecycleHostKeyReviewRequestV2),
        ):
            return
        row = self._require_profile_row(connection, request.profile_id)
        current = self._profile_from_row(row)
        if not isinstance(current, m.RemoteWorkspaceProfileV2):
            raise ProviderConflictV2("legacy profile cannot own lifecycle cancellation")
        if (
            operation_row["resource_kind"] != "profile"
            or operation_row["resource_id"] != request.profile_id
            or current.connection_generation != request.resource_generation + 1
        ):
            raise ProviderPreconditionFailedV2(
                "profile cancellation generation changed"
            )
        if current.connection_state == "disconnected":
            return
        if current.connection_state not in {
            "connecting",
            "prompt_pending",
            "bootstrapping",
            "negotiating",
            # A runner can observe a transport failure immediately before the
            # executor observes the durable cancellation request.  The exact
            # operation/resource/generation checks above prove that this
            # transient failure belongs to the lifecycle being cancelled, so
            # cancellation remains the authoritative terminal outcome.
            "failed",
        }:
            raise ProviderConflictV2(
                "profile cannot be cancelled from its current state"
            )
        current_version = cast(int, row["resource_version"])
        if current_version >= m.MAX_JAVASCRIPT_SAFE_INTEGER:
            raise ProviderCapacityV2Error("profile resource version is exhausted")
        version = current_version + 1
        updated = m.RemoteWorkspaceProfileV2(
            profile_id=current.profile_id,
            display_name=current.display_name,
            ssh_host_alias=current.ssh_host_alias,
            catalog_generation=current.catalog_generation,
            connection_generation=current.connection_generation,
            connection_state="disconnected",
            prompt=None,
            trust=m.SshTrustStateV2(
                connection_generation=current.connection_generation,
                state="unverified",
                review_id=None,
                review_sha256=None,
                key_fingerprints=[],
                repair_support="not_needed",
            ),
            failure=None,
            active_project_id=current.active_project_id,
            core_api_major=None,
            core_openapi_sha256=None,
            core_event_schema_sha256=None,
            core_registry_sha256=None,
            created_at=current.created_at,
            updated_at=timestamp,
            etag=self._etag("profile", current.profile_id, version),
        )
        self._update_profile(connection, updated, version=version)

    @staticmethod
    def _require_current_host_key_review(
        current: m.RemoteWorkspaceProfileV2,
        request: m.HostKeyReviewRequestV2,
    ) -> None:
        if (
            current.connection_state != "host_key_review"
            or current.trust.state != "changed_key_blocked"
            or current.trust.review_id is None
            or current.trust.review_sha256 is None
            or request.action not in {"replace_changed_key", "reject"}
            or request.review_id != current.trust.review_id
            or not hmac.compare_digest(
                request.review_sha256,
                current.trust.review_sha256,
            )
            or (
                request.action == "replace_changed_key"
                and current.trust.repair_support
                != "automatic_replacement_available"
            )
        ):
            raise ProviderConflictV2("host-key review authority changed")

    def _require_lifecycle_operation_row(
        self,
        connection: sqlite3.Connection,
        operation_id: str,
    ) -> sqlite3.Row:
        row = connection.execute(
            f"""
            SELECT {_LIFECYCLE_OPERATION_SELECT_COLUMNS}
            FROM lifecycle_operations
            WHERE operation_id = ?
            """,
            (operation_id,),
        ).fetchone()
        if row is None:
            raise ProviderNotFoundV2("lifecycle operation was not found")
        return cast(sqlite3.Row, row)

    def _lifecycle_request_from_row(
        self,
        row: sqlite3.Row,
    ) -> LifecycleRequestV2:
        size = self._bounded_blob_size(
            row,
            "request_json",
            maximum=MAX_LIFECYCLE_REQUEST_BYTES,
        )
        raw = bytes(row["request_json"])
        if len(raw) != size:
            raise ProviderDataV2Error("lifecycle request length changed")
        try:
            request = _LIFECYCLE_REQUEST_ADAPTER.validate_json(raw, strict=True)
        except ValidationError as exc:
            raise ProviderDataV2Error("stored lifecycle request is invalid") from exc
        if _canonical_json_bytes(request) != raw:
            raise ProviderDataV2Error("stored lifecycle request is not canonical")
        return request

    def _optional_lifecycle_json(
        self,
        row: sqlite3.Row,
        column: Literal["result_json", "failure_json"],
    ) -> bytes | None:
        size = row[f"{column}_bytes"]
        value = row[column]
        if value is None:
            if size != 0:
                raise ProviderDataV2Error(f"stored {column} null length is invalid")
            return None
        if (
            type(size) is not int
            or not 2 <= size <= MAX_LIFECYCLE_DOCUMENT_BYTES
            or not isinstance(value, (bytes, bytearray, memoryview))
        ):
            raise ProviderDataV2Error(f"stored {column} exceeds its byte bound")
        raw = bytes(value)
        if len(raw) != size:
            raise ProviderDataV2Error(f"stored {column} length changed")
        return raw

    def _lifecycle_operation_from_row(
        self,
        row: sqlite3.Row,
    ) -> m.LifecycleOperationV2:
        request = self._lifecycle_request_from_row(row)
        phase_plan_size = self._bounded_blob_size(
            row,
            "phase_plan_json",
            maximum=MAX_LIFECYCLE_DOCUMENT_BYTES,
        )
        phase_plan = bytes(row["phase_plan_json"])
        if len(phase_plan) != phase_plan_size or phase_plan != _canonical_json_bytes(
            list(m.LIFECYCLE_PHASES)
        ):
            raise ProviderDataV2Error("stored lifecycle phase plan is invalid")
        progress_size = self._bounded_blob_size(
            row,
            "progress_json",
            maximum=MAX_LIFECYCLE_DOCUMENT_BYTES,
        )
        progress_raw = bytes(row["progress_json"])
        if len(progress_raw) != progress_size:
            raise ProviderDataV2Error("stored lifecycle progress length changed")
        try:
            progress = _LIFECYCLE_PROGRESS_ADAPTER.validate_json(
                progress_raw,
                strict=True,
            )
        except ValidationError as exc:
            raise ProviderDataV2Error("stored lifecycle progress is invalid") from exc
        if _canonical_json_bytes(progress) != progress_raw:
            raise ProviderDataV2Error("stored lifecycle progress is not canonical")

        result_raw = self._optional_lifecycle_json(row, "result_json")
        failure_raw = self._optional_lifecycle_json(row, "failure_json")
        try:
            result = (
                _LIFECYCLE_RESULT_ADAPTER.validate_json(result_raw, strict=True)
                if result_raw is not None
                else None
            )
            failure = (
                m.DesktopErrorV2.model_validate_json(failure_raw, strict=True)
                if failure_raw is not None
                else None
            )
        except ValidationError as exc:
            raise ProviderDataV2Error("stored lifecycle terminal document is invalid") from exc
        if (result_raw is not None and _canonical_json_bytes(result) != result_raw) or (
            failure_raw is not None and _canonical_json_bytes(failure) != failure_raw
        ):
            raise ProviderDataV2Error("stored lifecycle terminal document is not canonical")

        flags = (row["cancellable"], row["cancellation_requested"])
        if any(type(value) is not int or value not in (0, 1) for value in flags):
            raise ProviderDataV2Error("stored lifecycle flags are invalid")
        version = row["resource_version"]
        watermark = row["log_sequence_high_watermark"]
        dropped = row["dropped_before_sequence"]
        log_bytes = row["log_byte_count"]
        if (
            type(version) is not int
            or not 1 <= version <= m.MAX_JAVASCRIPT_SAFE_INTEGER
            or type(watermark) is not int
            or not 0 <= watermark <= m.MAX_JAVASCRIPT_SAFE_INTEGER
            or type(dropped) is not int
            or not 0 <= dropped <= watermark
            or type(log_bytes) is not int
            or log_bytes < 0
        ):
            raise ProviderDataV2Error("stored lifecycle scalar authority is invalid")
        if row["etag"] != self._etag("lifecycle_operation", row["operation_id"], version):
            raise ProviderDataV2Error("stored lifecycle ETag differs from its version")
        if row["status"] == "queued" and row["cancellation_requested"]:
            raise ProviderDataV2Error("queued lifecycle operation requests cancellation")
        if row["status"] == "cancelled" and not row["cancellation_requested"]:
            raise ProviderDataV2Error("cancelled lifecycle operation lacks cancellation intent")
        if (
            row["cancellation_requested"]
            and row["status"] in {"succeeded", "failed"}
        ):
            raise ProviderDataV2Error(
                "terminal lifecycle operation conflicts with cancellation intent"
            )
        if row["cancellation_requested"] and row["cancellable"]:
            raise ProviderDataV2Error(
                "lifecycle cancellation intent remained cancellable"
            )

        try:
            resource = _LIFECYCLE_RESOURCE_ADAPTER.validate_python(
                {
                    "resource_kind": row["resource_kind"],
                    "resource_id": row["resource_id"],
                },
                strict=True,
            )
            reservation = LifecycleOperationReservationV2(
                kind=row["kind"],
                resource=resource,
                request=request,
            )
            operation = m.LifecycleOperationV2(
                operation_id=row["operation_id"],
                kind=row["kind"],
                resource=resource,
                request_sha256=row["request_sha256"],
                status=row["status"],
                phase=row["phase"],
                phase_index=row["phase_index"],
                phase_total=row["phase_total"],
                progress=progress,
                cancellable=bool(row["cancellable"]),
                result=result,
                failure=failure,
                log_sequence_high_watermark=watermark,
                created_at=row["created_at"],
                started_at=row["started_at"],
                updated_at=row["updated_at"],
                finished_at=row["finished_at"],
                etag=row["etag"],
            )
        except ValidationError as exc:
            raise ProviderDataV2Error("stored lifecycle operation is invalid") from exc
        expected_request_sha256 = hashlib.sha256(_canonical_json_bytes(reservation)).hexdigest()
        if not hmac.compare_digest(operation.request_sha256, expected_request_sha256):
            raise ProviderDataV2Error("stored lifecycle request digest changed")
        return operation

    def _lifecycle_work_from_row(
        self,
        connection: sqlite3.Connection,
        row: sqlite3.Row,
    ) -> LifecycleOperationWorkV2:
        idempotency = connection.execute(
            """
            SELECT CASE WHEN length(CAST(idempotency_key AS BLOB)) BETWEEN 16 AND 256
                        THEN idempotency_key END AS idempotency_key,
                   length(CAST(idempotency_key AS BLOB)) AS idempotency_key_bytes,
                   request_sha256
            FROM lifecycle_idempotency_records
            WHERE principal = ? AND action = 'reserve'
              AND resource_scope = 'lifecycle_operations' AND operation_id = ?
            """,
            (LOCAL_PRINCIPAL, row["operation_id"]),
        ).fetchone()
        if (
            idempotency is None
            or type(idempotency["idempotency_key_bytes"]) is not int
            or not 16 <= idempotency["idempotency_key_bytes"] <= 256
            or type(idempotency["idempotency_key"]) is not str
            or len(idempotency["idempotency_key"].encode("utf-8"))
            != idempotency["idempotency_key_bytes"]
            or not hmac.compare_digest(idempotency["request_sha256"], row["request_sha256"])
        ):
            raise ProviderDataV2Error("lifecycle reservation replay authority is invalid")
        return LifecycleOperationWorkV2(
            operation=self._lifecycle_operation_from_row(row),
            request=self._lifecycle_request_from_row(row),
            idempotency_key=idempotency["idempotency_key"],
            cancellation_requested=bool(row["cancellation_requested"]),
        )

    @staticmethod
    def _next_lifecycle_version(row: sqlite3.Row) -> int:
        version = row["resource_version"]
        if type(version) is not int or not 1 <= version < m.MAX_JAVASCRIPT_SAFE_INTEGER:
            raise ProviderCapacityV2Error("lifecycle operation version is exhausted")
        return version + 1

    @staticmethod
    def _validate_lifecycle_progress_advance(
        *,
        current: m.LifecycleProgressV2 | None,
        updated: m.LifecycleProgressV2 | None,
        same_phase: bool,
    ) -> None:
        if not same_phase or current is None:
            return
        if updated is None:
            raise ProviderPreconditionFailedV2("lifecycle progress cannot regress")
        if isinstance(current, m.LifecycleProgressIndeterminateV2):
            return
        if isinstance(updated, m.LifecycleProgressIndeterminateV2):
            raise ProviderPreconditionFailedV2("lifecycle progress cannot regress")
        if type(current) is not type(updated) or current.total != updated.total:
            raise ProviderPreconditionFailedV2(
                "lifecycle progress kind or total cannot change within a phase"
            )
        if updated.completed < current.completed:
            raise ProviderPreconditionFailedV2("lifecycle progress cannot regress")

    @staticmethod
    def _truncate_lifecycle_log_text(
        value: str,
        *,
        already_truncated: bool,
    ) -> tuple[str, bool]:
        raw = value.encode("utf-8")
        if len(raw) <= MAX_LIFECYCLE_LOG_ENTRY_BYTES:
            return value, already_truncated
        prefix = raw[:MAX_LIFECYCLE_LOG_ENTRY_BYTES]
        try:
            text = prefix.decode("utf-8", errors="strict")
        except UnicodeDecodeError as exc:
            text = prefix[: exc.start].decode("utf-8", errors="strict")
        return text, True

    def _enforce_lifecycle_log_budgets(
        self,
        connection: sqlite3.Connection,
        operation_id: str,
    ) -> None:
        while True:
            count, byte_count = cast(
                tuple[int, int],
                connection.execute(
                    """
                    SELECT count(*), coalesce(sum(text_bytes), 0)
                    FROM lifecycle_operation_logs
                    WHERE operation_id = ?
                    """,
                    (operation_id,),
                ).fetchone(),
            )
            if (
                count <= self._max_lifecycle_log_entries
                and byte_count <= self._max_lifecycle_log_bytes
            ):
                break
            oldest = connection.execute(
                """
                SELECT sequence, text_bytes
                FROM lifecycle_operation_logs
                WHERE operation_id = ?
                ORDER BY sequence
                LIMIT 1
                """,
                (operation_id,),
            ).fetchone()
            if oldest is None:
                raise ProviderDataV2Error("lifecycle log budget accounting changed")
            self._evict_lifecycle_log(
                connection,
                operation_id=operation_id,
                sequence=cast(int, oldest["sequence"]),
                text_bytes=cast(int, oldest["text_bytes"]),
            )

        while True:
            global_bytes = cast(
                int,
                connection.execute(
                    "SELECT coalesce(sum(text_bytes), 0) FROM lifecycle_operation_logs"
                ).fetchone()[0],
            )
            if global_bytes <= self._max_lifecycle_global_log_bytes:
                return
            oldest = connection.execute(
                """
                SELECT log.operation_id, log.sequence, log.text_bytes
                FROM lifecycle_operation_logs AS log
                JOIN lifecycle_operations AS operation
                  ON operation.operation_id = log.operation_id
                LEFT JOIN lifecycle_reconciliation_acknowledgements AS acknowledgement
                  ON acknowledgement.operation_id = operation.operation_id
                ORDER BY
                    CASE
                        WHEN acknowledgement.operation_id IS NOT NULL
                         AND operation.status IN ('succeeded', 'failed', 'cancelled')
                        THEN 0 ELSE 1
                    END,
                    log.occurred_at, log.operation_id, log.sequence
                LIMIT 1
                """
            ).fetchone()
            if oldest is None:
                raise ProviderDataV2Error("global lifecycle log accounting changed")
            self._evict_lifecycle_log(
                connection,
                operation_id=cast(str, oldest["operation_id"]),
                sequence=cast(int, oldest["sequence"]),
                text_bytes=cast(int, oldest["text_bytes"]),
            )

    @staticmethod
    def _evict_lifecycle_log(
        connection: sqlite3.Connection,
        *,
        operation_id: str,
        sequence: int,
        text_bytes: int,
    ) -> None:
        deleted = connection.execute(
            """
            DELETE FROM lifecycle_operation_logs
            WHERE operation_id = ? AND sequence = ? AND text_bytes = ?
            """,
            (operation_id, sequence, text_bytes),
        ).rowcount
        if deleted != 1:
            raise ProviderDataV2Error("lifecycle log changed during bounded eviction")
        connection.execute(
            """
            UPDATE lifecycle_operations
            SET dropped_before_sequence = max(dropped_before_sequence, ?),
                log_byte_count = log_byte_count - ?
            WHERE operation_id = ?
            """,
            (sequence, text_bytes, operation_id),
        )

    def _lifecycle_cursor_key(self, connection: sqlite3.Connection) -> bytes:
        row = connection.execute(
            """
            SELECT CASE WHEN length(cursor_key) = 32 THEN cursor_key END AS cursor_key,
                   length(cursor_key) AS cursor_key_bytes,
                   created_at
            FROM lifecycle_cursor_key
            WHERE singleton = 1
            """
        ).fetchone()
        if (
            row is None
            or row["cursor_key_bytes"] != 32
            or not isinstance(row["cursor_key"], (bytes, bytearray, memoryview))
            or type(row["created_at"]) is not str
            or _TIMESTAMP_RE.fullmatch(row["created_at"]) is None
        ):
            raise ProviderDataV2Error("lifecycle cursor authority is invalid")
        key = bytes(row["cursor_key"])
        if len(key) != 32:
            raise ProviderDataV2Error("lifecycle cursor key length changed")
        return key

    def _encode_lifecycle_cursor(
        self,
        connection: sqlite3.Connection,
        *,
        operation_id: str,
        next_sequence: int,
        dropped_before_sequence: int,
    ) -> str:
        payload = _canonical_json_bytes(
            {
                "dropped_before_sequence": dropped_before_sequence,
                "namespace": STORE_NAMESPACE,
                "next_sequence": next_sequence,
                "operation_id": operation_id,
                "schema_version": SCHEMA_VERSION,
            }
        )
        signature = hmac.new(
            self._lifecycle_cursor_key(connection),
            b"openevo-desktop-lifecycle-cursor-v2\0" + payload,
            hashlib.sha256,
        ).digest()
        encoded_payload = base64.urlsafe_b64encode(payload).rstrip(b"=").decode("ascii")
        encoded_signature = base64.urlsafe_b64encode(signature).rstrip(b"=").decode("ascii")
        cursor = f"{encoded_payload}.{encoded_signature}"
        if len(cursor.encode("utf-8")) > 512:
            raise ProviderCapacityV2Error("lifecycle cursor exceeds its public bound")
        return cursor

    def _decode_lifecycle_cursor(
        self,
        connection: sqlite3.Connection,
        cursor: str,
    ) -> dict[str, object]:
        if type(cursor) is not str or not 1 <= len(cursor.encode("utf-8")) <= 512:
            raise ProviderContractV2Error("lifecycle cursor is invalid")
        parts = cursor.split(".")
        if len(parts) != 2 or not all(parts):
            raise ProviderContractV2Error("lifecycle cursor is invalid")
        try:
            payload = base64.b64decode(
                parts[0] + "=" * (-len(parts[0]) % 4),
                altchars=b"-_",
                validate=True,
            )
            signature = base64.b64decode(
                parts[1] + "=" * (-len(parts[1]) % 4),
                altchars=b"-_",
                validate=True,
            )
        except (binascii.Error, ValueError) as exc:
            raise ProviderContractV2Error("lifecycle cursor encoding is invalid") from exc
        expected = hmac.new(
            self._lifecycle_cursor_key(connection),
            b"openevo-desktop-lifecycle-cursor-v2\0" + payload,
            hashlib.sha256,
        ).digest()
        if len(signature) != 32 or not hmac.compare_digest(signature, expected):
            raise ProviderContractV2Error("lifecycle cursor signature is invalid")
        try:
            value = json.loads(payload)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ProviderContractV2Error("lifecycle cursor payload is invalid") from exc
        if type(value) is not dict or set(value) != {
            "dropped_before_sequence",
            "namespace",
            "next_sequence",
            "operation_id",
            "schema_version",
        }:
            raise ProviderContractV2Error("lifecycle cursor payload is not closed")
        if _canonical_json_bytes(value) != payload:
            raise ProviderContractV2Error("lifecycle cursor payload is not canonical")
        if value["namespace"] != STORE_NAMESPACE or value["schema_version"] != SCHEMA_VERSION:
            raise ProviderContractV2Error("lifecycle cursor authority differs")
        try:
            self._validate_profile_id(cast(str, value["operation_id"]))
        except ProviderContractV2Error as exc:
            raise ProviderContractV2Error("lifecycle cursor operation is invalid") from exc
        next_sequence = value["next_sequence"]
        dropped = value["dropped_before_sequence"]
        if (
            type(next_sequence) is not int
            or not 1 <= next_sequence <= m.MAX_JAVASCRIPT_SAFE_INTEGER
            or type(dropped) is not int
            or not 0 <= dropped <= m.MAX_JAVASCRIPT_SAFE_INTEGER
            or next_sequence <= dropped
        ):
            raise ProviderContractV2Error("lifecycle cursor sequence is invalid")
        return cast(dict[str, object], value)

    def _lifecycle_log_from_row(self, row: sqlite3.Row) -> m.LifecycleLogEntryV2:
        actual = row["text_bytes_actual"]
        recorded = row["text_bytes"]
        raw = row["text"]
        truncated = row["truncated"]
        if (
            type(actual) is not int
            or not 1 <= actual <= MAX_LIFECYCLE_LOG_ENTRY_BYTES
            or recorded != actual
            or not isinstance(raw, (bytes, bytearray, memoryview))
            or type(truncated) is not int
            or truncated not in (0, 1)
        ):
            raise ProviderDataV2Error("stored lifecycle log exceeds its byte bound")
        raw_bytes = bytes(raw)
        if len(raw_bytes) != actual:
            raise ProviderDataV2Error("stored lifecycle log length changed")
        try:
            text = raw_bytes.decode("utf-8", errors="strict")
            return m.LifecycleLogEntryV2(
                operation_id=row["operation_id"],
                sequence=row["sequence"],
                occurred_at=row["occurred_at"],
                source=row["source"],
                text=text,
                truncated=bool(truncated),
            )
        except (UnicodeDecodeError, ValidationError) as exc:
            raise ProviderDataV2Error("stored lifecycle log entry is invalid") from exc

    def _cleanup_acknowledged_lifecycle_operations(
        self,
        connection: sqlite3.Connection,
    ) -> None:
        now = self._clock()
        if not isinstance(now, datetime) or now.tzinfo is None:
            raise ProviderStoreV2Error("v2 provider clock must be timezone-aware")
        cutoff = (
            (now.astimezone(timezone.utc) - LIFECYCLE_TERMINAL_RETENTION)
            .isoformat(timespec="microseconds")
            .replace("+00:00", "Z")
        )
        connection.execute(
            """
            DELETE FROM lifecycle_operations
            WHERE operation_id IN (
                SELECT acknowledgement.operation_id
                FROM lifecycle_reconciliation_acknowledgements AS acknowledgement
                JOIN lifecycle_operations AS operation
                  ON operation.operation_id = acknowledgement.operation_id
                WHERE acknowledgement.acknowledged_at <= ?
                  AND operation.status IN ('succeeded', 'failed', 'cancelled')
            )
            """,
            (cutoff,),
        )

    def _new_system_profile(
        self,
        *,
        request: m.SystemOpenSshProfileCreateV2,
        catalog_generation: int,
        timestamp: str,
    ) -> m.RemoteWorkspaceProfileV2:
        profile_id = self._new_id("profile")
        return m.RemoteWorkspaceProfileV2(
            profile_id=profile_id,
            display_name=request.display_name,
            ssh_host_alias=request.ssh_host_alias,
            catalog_generation=catalog_generation,
            connection_generation=1,
            connection_state="disconnected",
            prompt=None,
            trust=m.SshTrustStateV2(
                connection_generation=1,
                state="unverified",
                review_id=None,
                review_sha256=None,
                key_fingerprints=[],
                repair_support="not_needed",
            ),
            failure=None,
            active_project_id=None,
            core_api_major=None,
            core_openapi_sha256=None,
            core_event_schema_sha256=None,
            core_registry_sha256=None,
            created_at=timestamp,
            updated_at=timestamp,
            etag=self._etag("profile", profile_id, 1),
        )

    def _insert_profile(
        self,
        connection: sqlite3.Connection,
        profile: m.RemoteProfileV2,
        *,
        legacy_source_ref_sha256: str | None = None,
        legacy_source_document_sha256: str | None = None,
        rebound_from_sha256: str | None = None,
    ) -> None:
        raw = _canonical_json_bytes(profile)
        if len(raw) > MAX_PROFILE_DOCUMENT_BYTES:
            raise ProviderCapacityV2Error("v2 profile document exceeds its byte bound")
        connection.execute(
            """
            INSERT INTO profiles(
                profile_id, profile_kind, document_json, resource_version,
                legacy_source_ref_sha256, legacy_source_document_sha256,
                rebound_from_sha256, created_at, updated_at
            ) VALUES (?, ?, ?, 1, ?, ?, ?, ?, ?)
            """,
            (
                profile.profile_id,
                profile.profile_kind,
                raw,
                legacy_source_ref_sha256,
                legacy_source_document_sha256,
                rebound_from_sha256,
                profile.created_at,
                profile.updated_at,
            ),
        )

    def _update_profile(
        self,
        connection: sqlite3.Connection,
        profile: m.RemoteProfileV2,
        *,
        version: int,
    ) -> None:
        raw = _canonical_json_bytes(profile)
        if len(raw) > MAX_PROFILE_DOCUMENT_BYTES:
            raise ProviderCapacityV2Error("v2 profile document exceeds its byte bound")
        connection.execute(
            """
            UPDATE profiles
            SET document_json = ?, resource_version = ?, updated_at = ?
            WHERE profile_id = ?
            """,
            (raw, version, profile.updated_at, profile.profile_id),
        )

    def _insert_draft(
        self,
        connection: sqlite3.Connection,
        draft: LocalProjectDraftV2,
    ) -> None:
        document = _canonical_json_bytes(draft)
        config = _canonical_json_bytes(draft.config)
        if max(len(document), len(config)) > MAX_DRAFT_DOCUMENT_BYTES:
            raise ProviderCapacityV2Error("v2 draft exceeds its byte bound")
        connection.execute(
            """
            INSERT INTO project_drafts(
                draft_id, profile_id, document_json, config_json,
                project_config_sha256, legacy_source_ref_sha256,
                legacy_source_document_sha256, resource_version,
                created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, 1, ?, ?)
            """,
            (
                draft.draft_id,
                draft.profile_id,
                document,
                config,
                draft.project_config_sha256,
                draft.legacy_source_ref_sha256,
                draft.legacy_source_document_sha256,
                draft.created_at,
                draft.updated_at,
            ),
        )

    def _profile_from_row(self, row: sqlite3.Row) -> m.RemoteProfileV2:
        size = self._bounded_blob_size(
            row,
            "document_json",
            maximum=MAX_PROFILE_DOCUMENT_BYTES,
        )
        raw = bytes(row["document_json"])
        if len(raw) != size:
            raise ProviderDataV2Error("v2 profile document length changed")
        try:
            profile = _PROFILE_ADAPTER.validate_json(raw, strict=True)
        except ValidationError as exc:
            raise ProviderDataV2Error("stored v2 profile is invalid") from exc
        version = row["resource_version"]
        if (
            type(version) is not int
            or not 1 <= version <= m.MAX_JAVASCRIPT_SAFE_INTEGER
            or row["profile_id"] != profile.profile_id
            or row["profile_kind"] != profile.profile_kind
            or row["created_at"] != profile.created_at
            or row["updated_at"] != profile.updated_at
            or profile.etag != self._etag("profile", profile.profile_id, version)
            or _canonical_json_bytes(profile) != raw
        ):
            raise ProviderDataV2Error("stored v2 profile authority differs from its row")
        if isinstance(profile, m.LegacyExplicitProfileV2):
            if (
                not self._is_digest(row["legacy_source_ref_sha256"])
                or not self._is_digest(row["legacy_source_document_sha256"])
                or row["rebound_from_sha256"] is not None
            ):
                raise ProviderDataV2Error("legacy profile provenance is invalid")
        elif (
            row["legacy_source_ref_sha256"] is not None
            or row["legacy_source_document_sha256"] is not None
            or (
                row["rebound_from_sha256"] is not None
                and not self._is_digest(row["rebound_from_sha256"])
            )
        ):
            raise ProviderDataV2Error("system profile provenance is invalid")
        return profile

    def _draft_from_row(self, row: sqlite3.Row) -> LocalProjectDraftV2:
        size = self._bounded_blob_size(
            row,
            "document_json",
            maximum=MAX_DRAFT_DOCUMENT_BYTES,
        )
        config_size = self._bounded_blob_size(
            row,
            "config_json",
            maximum=MAX_DRAFT_DOCUMENT_BYTES,
        )
        raw = bytes(row["document_json"])
        config_raw = bytes(row["config_json"])
        if len(raw) != size or len(config_raw) != config_size:
            raise ProviderDataV2Error("v2 draft document length changed")
        try:
            draft = LocalProjectDraftV2.model_validate_json(raw)
            config = ScienceProjectConfigV2.model_validate_json(config_raw)
        except ValidationError as exc:
            raise ProviderDataV2Error("stored v2 draft is invalid") from exc
        version = row["resource_version"]
        if (
            type(version) is not int
            or not 1 <= version <= m.MAX_JAVASCRIPT_SAFE_INTEGER
            or draft.config != config
            or row["draft_id"] != draft.draft_id
            or row["profile_id"] != draft.profile_id
            or row["project_config_sha256"] != draft.project_config_sha256
            or row["legacy_source_ref_sha256"] != draft.legacy_source_ref_sha256
            or row["legacy_source_document_sha256"] != draft.legacy_source_document_sha256
            or row["created_at"] != draft.created_at
            or row["updated_at"] != draft.updated_at
            or draft.etag != self._etag("draft", draft.draft_id, version)
            or _canonical_json_bytes(draft) != raw
            or _canonical_json_bytes(config) != config_raw
        ):
            raise ProviderDataV2Error("stored v2 draft authority differs from its row")
        return draft

    def _diagnostic_from_row(self, row: sqlite3.Row) -> MigrationDiagnosticV2:
        try:
            return MigrationDiagnosticV2.model_validate(
                {
                    "diagnostic_id": row["diagnostic_id"],
                    "code": row["code"],
                    "source_kind": row["source_kind"],
                    "source_ref_sha256": row["source_ref_sha256"],
                    "created_at": row["created_at"],
                }
            )
        except ValidationError as exc:
            raise ProviderDataV2Error("stored migration diagnostic is invalid") from exc

    def _require_profile_row(
        self,
        connection: sqlite3.Connection,
        profile_id: str,
    ) -> sqlite3.Row:
        row = connection.execute(
            f"SELECT {_PROFILE_SELECT_COLUMNS} FROM profiles WHERE profile_id = ?",
            (profile_id,),
        ).fetchone()
        if row is None:
            raise ProviderNotFoundV2("v2 profile was not found")
        return cast(sqlite3.Row, row)

    def _require_profile_capacity(self, connection: sqlite3.Connection) -> None:
        count = cast(int, connection.execute("SELECT count(*) FROM profiles").fetchone()[0])
        if count >= self._max_profiles:
            raise ProviderCapacityV2Error("v2 profile capacity is full")

    def _require_draft_capacity(self, connection: sqlite3.Connection) -> None:
        count = cast(
            int,
            connection.execute("SELECT count(*) FROM project_drafts").fetchone()[0],
        )
        if count >= self._max_drafts:
            raise ProviderCapacityV2Error("v2 draft capacity is full")

    @staticmethod
    def _bounded_blob_size(
        row: sqlite3.Row,
        column: str,
        *,
        maximum: int,
    ) -> int:
        size = row[f"{column}_bytes"]
        if type(size) is not int or not 2 <= size <= maximum:
            raise ProviderDataV2Error(f"stored {column} exceeds its byte bound")
        value = row[column]
        if not isinstance(value, (bytes, bytearray, memoryview)):
            raise ProviderDataV2Error(f"stored {column} is not a blob")
        if len(value) != size:
            raise ProviderDataV2Error(f"stored {column} length changed")
        return size

    @staticmethod
    def _validate_model(model: type[_ModelT], value: object) -> _ModelT:
        try:
            if type(value) is model:
                return cast(_ModelT, value)
            return model.model_validate(value)
        except ValidationError as exc:
            raise ProviderContractV2Error(f"{model.__name__} validation failed") from exc

    @staticmethod
    def _validate_remote_profile(value: object) -> m.RemoteProfileV2:
        try:
            return _PROFILE_ADAPTER.validate_python(value, strict=True)
        except ValidationError as exc:
            raise ProviderContractV2Error("remote profile validation failed") from exc

    @staticmethod
    def _validate_profile_id(value: str) -> None:
        try:
            TypeAdapter(m.OpaqueId).validate_python(value, strict=True)
        except ValidationError as exc:
            raise ProviderContractV2Error("profile ID is invalid") from exc

    @staticmethod
    def _validate_etag(value: str) -> None:
        if type(value) is not str or _ETAG_RE.fullmatch(value) is None:
            raise ProviderContractV2Error("If-Match is not a strong v2 ETag")

    @staticmethod
    def _validate_idempotency_key(value: str) -> None:
        if type(value) is not str or value != value.strip() or any(ord(c) < 0x20 for c in value):
            raise ProviderContractV2Error("idempotency key is invalid")
        if not 16 <= len(value.encode("utf-8")) <= 256:
            raise ProviderContractV2Error("idempotency key is outside its byte bound")

    @staticmethod
    def _is_digest(value: object) -> bool:
        return type(value) is str and _DIGEST_RE.fullmatch(value) is not None

    def _timestamp(self) -> str:
        value = self._clock()
        if not isinstance(value, datetime) or value.tzinfo is None:
            raise ProviderStoreV2Error("v2 provider clock must be timezone-aware")
        return (
            value.astimezone(timezone.utc)
            .isoformat(timespec="microseconds")
            .replace("+00:00", "Z")
        )

    @staticmethod
    def _new_id(prefix: str) -> str:
        return f"{prefix}-{secrets.token_hex(24)}"

    @staticmethod
    def _etag(resource_type: str, resource_id: str, version: int) -> str:
        digest = hashlib.sha256(
            b"openevo-desktop-local-etag-v2\0"
            + _canonical_json_bytes(
                {
                    "resource_type": resource_type,
                    "resource_id": resource_id,
                    "version": version,
                }
            )
        ).hexdigest()
        return f'"{digest}"'

    def _close_resources(self) -> None:
        connection = getattr(self, "_connection", None)
        if connection is not None:
            try:
                connection.close()
            finally:
                del self._connection
        owner_lock = getattr(self, "_owner_lock_fd", None)
        if owner_lock is not None:
            try:
                fcntl.flock(owner_lock, fcntl.LOCK_UN)
            finally:
                os.close(owner_lock)
                del self._owner_lock_fd
        root_fd = getattr(self, "_root_fd", None)
        if root_fd is not None:
            os.close(root_fd)
            del self._root_fd
        self._closed = True


__all__ = [
    "DATABASE_FILENAME",
    "DEFAULT_MAX_DRAFTS",
    "DEFAULT_MAX_IDEMPOTENCY_RECORDS",
    "DEFAULT_MAX_LIFECYCLE_OPERATIONS",
    "DEFAULT_MAX_MIGRATION_DIAGNOSTICS",
    "DEFAULT_MAX_PROFILES",
    "DesktopProviderStoreV2",
    "EXPECTED_SCHEMA_V1_SHA256",
    "EXPECTED_SCHEMA_V2_SHA256",
    "EXPECTED_SCHEMA_V3_SHA256",
    "LegacyDraftSourceV2",
    "LegacyProfileImportV2",
    "LifecycleHostKeyReviewRequestV2",
    "LifecycleLogAppendV2",
    "LifecycleNativeWorkspacePrepareRequestV2",
    "LifecycleOperationAdvanceV2",
    "LifecycleOperationCompletionV2",
    "LifecycleOperationReservationV2",
    "LifecycleOperationWorkV2",
    "LifecycleProfileConnectRequestV2",
    "LifecycleProfileDisconnectRequestV2",
    "LifecycleProjectActivateRequestV2",
    "LifecycleProjectCreateRequestV2",
    "LocalProjectDraftV2",
    "MAX_DATABASE_BYTES",
    "MAX_LIFECYCLE_LOG_BYTES",
    "MAX_LIFECYCLE_LOG_ENTRIES",
    "MAX_LIFECYCLE_LOG_ENTRY_BYTES",
    "MAX_LIFECYCLE_GLOBAL_LOG_BYTES",
    "MAX_PROFILE_DOCUMENT_BYTES",
    "MigrationDiagnosticV2",
    "ProviderCapacityConfigurationV2Error",
    "ProviderCapacityV2Error",
    "ProviderConflictV2",
    "ProviderContractV2Error",
    "ProviderCursorExpiredV2",
    "ProviderDataV2Error",
    "ProviderIdempotencyConflictV2",
    "ProviderLifecycleResourceBusyV2",
    "ProviderNotFoundV2",
    "ProviderPreconditionFailedV2",
    "ProviderSchemaV2Error",
    "ProviderStateV2Error",
    "ProviderStoreV2Error",
    "SCHEMA_VERSION",
    "STORE_NAMESPACE",
]
