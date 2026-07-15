from __future__ import annotations

import base64
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
import ctypes
from dataclasses import dataclass
from datetime import datetime, timezone
import errno
import fcntl
from hashlib import sha256
import hmac
import json
import os
from pathlib import Path
import re
import secrets
import sqlite3
import stat
import struct
import sys
import threading
from typing import Any, Literal, TypeVar, cast
import unicodedata

from pydantic import BaseModel, ValidationError

from desktop.sidecar.contracts.v1.models import (
    ApiErrorV1,
    ConnectionOperationResultV1,
    CredentialSlotStatusV1,
    LocalOperationResultV1,
    LocalOperationV1,
    NormalizedCheckV1,
    OperationProgressV1,
    ProjectCreateV1,
    ProjectOperationResultV1,
    ProjectPageV1,
    ProjectPatchV1,
    ProjectSourceV1,
    ProjectV1,
    RemoteProjectStateV1,
    RemoteProfileCreateV1,
    RemoteProfilePageV1,
    RemoteProfilePatchV1,
    RemoteProfileV1,
    ResourceRefV1,
)
from desktop.sidecar.workspace_identity import project_id_for_native_import


SCHEMA_VERSION = 5
DATABASE_FILENAME = "provider.sqlite3"
JOURNAL_FILENAME = f"{DATABASE_FILENAME}-journal"
WAL_FILENAME = f"{DATABASE_FILENAME}-wal"
SHM_FILENAME = f"{DATABASE_FILENAME}-shm"
OWNER_LOCK_FILENAME = "provider.lock"
CURSOR_KEY_FILENAME = "cursor-signing.key"
LOCAL_PRINCIPAL = "desktop-local-v1"
MAX_DOCUMENT_BYTES = 136_314_880
MAX_REQUEST_BYTES = MAX_DOCUMENT_BYTES
MAX_RESPONSE_BYTES = MAX_DOCUMENT_BYTES
MAX_CURSOR_BYTES = 2_048
MAX_RENDERED_CURSOR_BYTES = 256
MAX_IDEMPOTENCY_KEY_BYTES = 256
MAX_IDENTITY_BYTES = 512
MAX_DATABASE_BYTES = 536_870_912
MAX_JOURNAL_BYTES = 1_073_741_824
MAX_RECOVERY_ROWS = 100_000
MAX_RECOVERY_BYTES = 402_653_184
MAX_SCHEMA_OBJECTS = 48
MAX_SCHEMA_BYTES = 98_304
STARTUP_OPERATION_BATCH_ROWS = 128
NORMAL_WRITE_CLEANUP_ROWS = 128
MAX_STARTUP_OPERATION_ROW_BYTES = MAX_DOCUMENT_BYTES + 16_384
# RemoteProjectStateV1 is a closed, scalar-heavy projection rather than a general
# provider document. Keep both one observation and the recovered history small.
MAX_REMOTE_PROJECT_STATE_BYTES = 262_144
MAX_REMOTE_PROJECT_STATE_RECOVERY_BYTES = 16_777_216
# Profile reservations always have empty progress/checks; this bounds their largest
# success, cancellation, or bounded ApiErrorV1 terminal document with ample margin.
PROFILE_RUNTIME_TERMINAL_SLOT_BYTES = 1_048_576
PROFILE_RUNTIME_TERMINAL_RESERVATION_BYTES = 2 * PROFILE_RUNTIME_TERMINAL_SLOT_BYTES + 16_384
PROJECT_RUNTIME_TERMINAL_SLOT_BYTES = 1_048_576
PROJECT_RUNTIME_TERMINAL_RESERVATION_BYTES = 2 * PROJECT_RUNTIME_TERMINAL_SLOT_BYTES + 16_384
DEFAULT_IDEMPOTENCY_RECORD_LIMIT = 10_000
DEFAULT_CURSOR_RECORD_LIMIT = 10_000
DEFAULT_IDEMPOTENCY_RETENTION_SECONDS = 7 * 24 * 60 * 60
DEFAULT_CURSOR_TTL_SECONDS = 15 * 60

_RESOURCE_ID_BYTES = 32
_CURSOR_KEY_BYTES = 32
_CURSOR_NONCE_BYTES = 16
_CURSOR_TOKEN_VERSION = 1
_CURSOR_TOKEN = struct.Struct(">BQQ32s16s")
_CURSOR_KEY_TEMP_PREFIX = f".{CURSOR_KEY_FILENAME}.tmp-"
_ACTION_AUTHORITY_DOMAIN = b"openevo.desktop.local-action-authority.v1\0"
_REMOTE_PAYLOAD_USAGE_AUTHORITY_DOMAIN = b"openevo.desktop.remote-payload-usage-authority.v1\0"
_REMOTE_PAYLOAD_CONTENT_AUTHORITY_DOMAIN = b"openevo.desktop.remote-payload-content-authority.v1\0"
_PROVIDER_STORAGE_USAGE_AUTHORITY_DOMAIN = b"openevo.desktop.provider-storage-usage-authority.v1\0"
_REMOTE_CONTENT_ACCUMULATOR_MODULUS = (1 << 61) - 1
_REMOTE_CONTENT_TOKEN_BYTES = 7
_PROVIDER_USAGE_ACCOUNTED_BYTES = 512
_RENAME_NOREPLACE = 1
_RENAME_EXCL = 0x00000004
_ETAG_RE = re.compile(r'^"[0-9a-f]{64}"$')
_B64_RE = re.compile(r"^[A-Za-z0-9_-]+$")
_DIGEST_RE = re.compile(r"^[0-9a-f]{64}$")
_TIMESTAMP_RE = re.compile(
    r"^\d{4}-(?:0[1-9]|1[0-2])-(?:0[1-9]|[12]\d|3[01])"
    r"T(?:[01]\d|2[0-3]):[0-5]\d:[0-5]\d\.\d{6}Z$"
)
_OPERATION_KINDS = {
    "profile_connect",
    "profile_disconnect",
    "host_key_accept",
    "project_activate",
    "project_doctor",
    "project_repair",
    "bootstrap",
    "workspace_sync",
    "service_restart",
    "service_stop",
    "diagnostics",
    "cache_cleanup",
}
_OPERATION_STATES = {"queued", "running", "succeeded", "failed", "cancelling", "cancelled"}
_PERSISTENCE_DENIED_CONFIG_KEYS = {
    "apikey",
    "apitoken",
    "authorization",
    "bearertoken",
    "clientsecret",
    "credential",
    "credentials",
    "credentialslot",
    "filesystempath",
    "hostpath",
    "homedir",
    "homedirectory",
    "keychainslot",
    "localpath",
    "password",
    "passwd",
    "privatekey",
    "processoutput",
    "processstderr",
    "processstdout",
    "rawdiagnostic",
    "rawdiagnostics",
    "rawlog",
    "rawlogs",
    "refreshtoken",
    "secret",
    "secretkey",
    "sessiontoken",
    "sshkey",
    "stacktrace",
    "stderr",
    "stdout",
    "token",
    "traceback",
    "workingdirectory",
    "workdir",
}
_PERSISTENCE_DENIED_CONFIG_SUFFIXES = tuple(sorted(_PERSISTENCE_DENIED_CONFIG_KEYS))
_ALLOWED_PROJECT_CONFIG_PATH_KEYS = {"targetpath"}
_SECRET_KEY_SUFFIXES = (
    "apikey",
    "credential",
    "credentials",
    "password",
    "passwd",
    "privatekey",
    "secret",
    "token",
)
_PROFILE_OPERATION_KINDS = {"profile_connect", "profile_disconnect", "host_key_accept"}
_PROJECT_OPERATION_KINDS = {
    "project_activate",
    "project_doctor",
    "project_repair",
    "bootstrap",
    "workspace_sync",
}
_ACTION_ROUTE_SUFFIXES = {
    "profile_connect": "connect",
    "profile_disconnect": "disconnect",
    "host_key_accept": "host-key/accept",
    "project_activate": "activate",
    "project_doctor": "doctor",
    "project_repair": "repair",
    "bootstrap": "bootstrap",
    "workspace_sync": "workspace-sync",
}

_PROVIDER_USAGE_COLUMNS = (
    "total_rows",
    "total_bytes",
    "remote_payload_count",
    "remote_payload_bytes",
    "remote_accumulator_0",
    "remote_accumulator_1",
    "remote_accumulator_2",
    "remote_accumulator_3",
    "profile_reservations",
    "project_reservations",
    "idempotency_record_count",
    "pagination_cursor_count",
    "generation",
    "authority_tag",
)
_RECOVERY_USAGE_SPECIFICATIONS = (
    (
        "remote_profiles",
        (
            "profile_id",
            "name",
            "document_json",
            "connection_state",
            "credential_slots_json",
            "host_key_fingerprint",
            "created_at",
            "updated_at",
        ),
    ),
    (
        "projects",
        (
            "project_id",
            "profile_id",
            "name",
            "document_json",
            "state",
            "current_revision_id",
            "created_at",
            "updated_at",
        ),
    ),
    (
        "idempotency_records",
        (
            "principal",
            "method",
            "route",
            "resource_scope",
            "idempotency_key",
            "request_digest",
            "operation_id",
            "response_type",
            "response_bytes",
        ),
    ),
    (
        "local_operations",
        (
            "operation_id",
            "operation_kind",
            "state",
            "resource_type",
            "resource_id",
            "action_identity_digest",
            "document_json",
            "created_at",
            "finished_at",
        ),
    ),
    (
        "pagination_cursors",
        ("cursor_digest", "query_digest", "anchor_id", "anchor_value"),
    ),
    ("schema_migrations", ("applied_at",)),
)
_LIVE_OPERATION_STATES_SQL = "'queued', 'running', 'cancelling'"
_PROFILE_OPERATION_KINDS_SQL = ", ".join(
    f"'{value}'" for value in sorted(_PROFILE_OPERATION_KINDS)
)
_PROJECT_OPERATION_KINDS_SQL = ", ".join(
    f"'{value}'" for value in sorted(_PROJECT_OPERATION_KINDS)
)


def _usage_length_sql(columns: tuple[str, ...], *, prefix: str) -> str:
    return " + ".join(
        f"coalesce(length(CAST({prefix}.{column} AS BLOB)), 0)" for column in columns
    )


def _operation_reservation_sql(*, prefix: str, kinds: str) -> str:
    return (
        f"CASE WHEN {prefix}.state IN ({_LIVE_OPERATION_STATES_SQL}) "
        f"AND {prefix}.operation_kind IN ({kinds}) THEN 1 ELSE 0 END"
    )


def _rename_noreplace(source: str, destination: str, *, directory_fd: int) -> None:
    libc = ctypes.CDLL(None, use_errno=True)
    if sys.platform == "linux":
        rename = getattr(libc, "renameat2", None)
        flags = _RENAME_NOREPLACE
    elif sys.platform == "darwin":
        rename = getattr(libc, "renameatx_np", None)
        flags = _RENAME_EXCL
    else:
        rename = None
        flags = 0
    if rename is None:
        raise OSError(errno.ENOSYS, "atomic no-replace rename is unavailable")
    rename.argtypes = (
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_uint,
    )
    rename.restype = ctypes.c_int
    result = rename(
        directory_fd,
        os.fsencode(source),
        directory_fd,
        os.fsencode(destination),
        flags,
    )
    if result != 0:
        error = ctypes.get_errno()
        raise OSError(error, os.strerror(error), destination)


class ProviderStoreError(Exception):
    """Base class for local provider persistence failures."""


class ProviderStateRootError(ProviderStoreError):
    """The private provider state root or one of its files is unsafe."""


class ProviderSchemaError(ProviderStoreError):
    """The SQLite schema cannot be safely opened or migrated."""


class ProviderDataCorruptionError(ProviderStoreError):
    """Persisted provider data no longer satisfies its closed contract."""


class ContractValidationError(ProviderStoreError):
    """A caller supplied data that does not satisfy a Desktop v1 model."""


class ResourceNotFoundError(ProviderStoreError):
    def __init__(self, resource_type: str, resource_id: str) -> None:
        super().__init__(f"{resource_type} resource was not found")
        self.resource_type = resource_type
        self.resource_id = resource_id


class ResourceInUseError(ProviderStoreError):
    def __init__(self, resource_type: str, resource_id: str) -> None:
        super().__init__(f"{resource_type} resource is active or in use")
        self.resource_type = resource_type
        self.resource_id = resource_id


class ETagConflictError(ProviderStoreError):
    def __init__(self, resource_type: str, resource_id: str, current_etag: str) -> None:
        super().__init__(f"{resource_type} resource ETag does not match")
        self.resource_type = resource_type
        self.resource_id = resource_id
        self.current_etag = current_etag


class IdempotencyConflictError(ProviderStoreError):
    """An idempotency key was reused with a different canonical request."""


class IdempotencyCapacityError(ProviderStoreError):
    """The bounded live idempotency record capacity is exhausted."""


class ProviderCapacityConfigurationError(ProviderStoreError):
    """A configured record limit is lower than authenticated persisted usage."""

    def __init__(
        self,
        record_type: Literal["idempotency", "cursor"],
        *,
        configured_limit: int,
        persisted_count: int,
    ) -> None:
        label = "idempotency record" if record_type == "idempotency" else "pagination cursor"
        super().__init__(f"configured {label} capacity is lower than persisted usage")
        self.record_type = record_type
        self.configured_limit = configured_limit
        self.persisted_count = persisted_count


class CursorInvalidError(ProviderStoreError):
    """A cursor is malformed, tampered with, or bound to another query."""


class CursorExpiredError(ProviderStoreError):
    """A valid provider cursor is outside its bounded replay window."""


@dataclass(frozen=True)
class IdempotencyResult:
    status_code: int
    response_bytes: bytes
    replayed: bool


@dataclass(frozen=True)
class ProfileRuntimeActionReservation:
    operation: LocalOperationV1
    profile: RemoteProfileV1 | None
    replayed: bool


@dataclass(frozen=True)
class ProjectRuntimeActionReservation:
    operation: LocalOperationV1
    project: ProjectV1 | None
    replayed: bool


_ModelT = TypeVar("_ModelT", bound=BaseModel)
_Direction = Literal["asc", "desc"]


_SCHEMA_V1 = (
    """
    CREATE TABLE schema_migrations (
        version INTEGER PRIMARY KEY CHECK (version = 1),
        applied_at TEXT NOT NULL CHECK (length(CAST(applied_at AS BLOB)) = 27)
    ) STRICT
    """,
    """
    CREATE TABLE remote_profiles (
        profile_id TEXT PRIMARY KEY,
        name TEXT NOT NULL,
        document_json BLOB NOT NULL,
        connection_state TEXT NOT NULL,
        credential_slots_json BLOB NOT NULL,
        host_key_fingerprint TEXT,
        resource_version INTEGER NOT NULL CHECK (resource_version >= 1),
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    ) STRICT
    """,
    """
    CREATE TABLE projects (
        project_id TEXT PRIMARY KEY,
        profile_id TEXT NOT NULL,
        name TEXT NOT NULL,
        document_json BLOB NOT NULL,
        state TEXT NOT NULL,
        current_revision_id TEXT,
        resource_version INTEGER NOT NULL CHECK (resource_version >= 1),
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        FOREIGN KEY (profile_id) REFERENCES remote_profiles(profile_id) ON DELETE RESTRICT
    ) STRICT
    """,
    """
    CREATE TABLE idempotency_records (
        principal TEXT NOT NULL,
        method TEXT NOT NULL,
        route TEXT NOT NULL,
        resource_scope TEXT NOT NULL,
        idempotency_key TEXT NOT NULL,
        request_digest TEXT NOT NULL,
        response_type TEXT NOT NULL,
        status_code INTEGER NOT NULL CHECK (status_code BETWEEN 100 AND 599),
        response_bytes BLOB NOT NULL,
        created_at_epoch INTEGER NOT NULL,
        expires_at_epoch INTEGER NOT NULL,
        PRIMARY KEY (principal, method, route, resource_scope, idempotency_key),
        CHECK (principal = 'desktop-local-v1'),
        CHECK (length(CAST(method AS BLOB)) BETWEEN 1 AND 512),
        CHECK (length(CAST(route AS BLOB)) BETWEEN 1 AND 512),
        CHECK (length(CAST(resource_scope AS BLOB)) BETWEEN 1 AND 512),
        CHECK (length(CAST(idempotency_key AS BLOB)) BETWEEN 16 AND 256),
        CHECK (length(request_digest) = 64),
        CHECK (response_type IN ('ProjectV1', 'RemoteProfileV1')),
        CHECK (length(response_bytes) <= 136314880),
        CHECK (expires_at_epoch > created_at_epoch)
    ) STRICT
    """,
    "CREATE INDEX remote_profiles_updated_idx ON remote_profiles(updated_at, profile_id)",
    "CREATE INDEX remote_profiles_created_idx ON remote_profiles(created_at, profile_id)",
    "CREATE INDEX remote_profiles_name_idx ON remote_profiles(name, profile_id)",
    "CREATE INDEX projects_updated_idx ON projects(updated_at, project_id)",
    "CREATE INDEX projects_created_idx ON projects(created_at, project_id)",
    "CREATE INDEX projects_name_idx ON projects(name, project_id)",
    "CREATE INDEX projects_profile_idx ON projects(profile_id, project_id)",
    "CREATE UNIQUE INDEX projects_single_active_idx ON projects((1)) WHERE state = 'active'",
    "CREATE INDEX idempotency_expiry_idx ON idempotency_records(expires_at_epoch)",
)

_SCHEMA_V2 = (
    """
    CREATE TABLE schema_migrations (
        version INTEGER PRIMARY KEY CHECK (version BETWEEN 1 AND 2),
        applied_at TEXT NOT NULL CHECK (length(CAST(applied_at AS BLOB)) = 27)
    ) STRICT
    """,
    _SCHEMA_V1[1],
    _SCHEMA_V1[2],
    """
    CREATE TABLE local_operations (
        operation_id TEXT PRIMARY KEY,
        operation_kind TEXT NOT NULL,
        state TEXT NOT NULL,
        resource_type TEXT NOT NULL,
        resource_id TEXT NOT NULL,
        action_identity_digest TEXT UNIQUE,
        document_json BLOB NOT NULL,
        resource_version INTEGER NOT NULL CHECK (resource_version >= 1),
        created_at TEXT NOT NULL,
        finished_at TEXT,
        CHECK (operation_kind IN (
            'profile_connect', 'profile_disconnect', 'host_key_accept',
            'project_activate', 'project_doctor', 'project_repair', 'bootstrap',
            'workspace_sync', 'service_restart', 'service_stop', 'diagnostics',
            'cache_cleanup'
        )),
        CHECK (state IN (
            'queued', 'running', 'succeeded', 'failed', 'cancelling', 'cancelled'
        )),
        CHECK (resource_type IN (
            'profile', 'project', 'operation', 'run', 'artifact', 'service',
            'diagnostic', 'maintenance'
        )),
        CHECK (
            action_identity_digest IS NULL OR length(action_identity_digest) = 64
        )
    ) STRICT
    """,
    """
    CREATE TABLE idempotency_records (
        principal TEXT NOT NULL,
        method TEXT NOT NULL,
        route TEXT NOT NULL,
        resource_scope TEXT NOT NULL,
        idempotency_key TEXT NOT NULL,
        request_digest TEXT NOT NULL,
        operation_id TEXT UNIQUE,
        response_type TEXT NOT NULL,
        status_code INTEGER NOT NULL CHECK (status_code BETWEEN 100 AND 599),
        response_bytes BLOB NOT NULL,
        created_at_epoch INTEGER NOT NULL,
        expires_at_epoch INTEGER NOT NULL,
        PRIMARY KEY (principal, method, route, resource_scope, idempotency_key),
        CHECK (principal = 'desktop-local-v1'),
        CHECK (length(CAST(method AS BLOB)) BETWEEN 1 AND 512),
        CHECK (length(CAST(route AS BLOB)) BETWEEN 1 AND 512),
        CHECK (length(CAST(resource_scope AS BLOB)) BETWEEN 1 AND 512),
        CHECK (length(CAST(idempotency_key AS BLOB)) BETWEEN 16 AND 256),
        CHECK (length(request_digest) = 64),
        CHECK (
            operation_id IS NULL OR
            length(CAST(operation_id AS BLOB)) BETWEEN 1 AND 512
        ),
        CHECK (response_type IN ('ProjectV1', 'RemoteProfileV1', 'LocalOperationV1')),
        CHECK (length(response_bytes) <= 136314880),
        CHECK (expires_at_epoch > created_at_epoch),
        CHECK (
            (response_type = 'LocalOperationV1' AND operation_id IS NOT NULL) OR
            (response_type != 'LocalOperationV1' AND operation_id IS NULL)
        ),
        FOREIGN KEY (operation_id) REFERENCES local_operations(operation_id) ON DELETE RESTRICT
    ) STRICT
    """,
    """
    CREATE TABLE pagination_cursors (
        cursor_digest TEXT PRIMARY KEY CHECK (length(cursor_digest) = 64),
        query_digest TEXT NOT NULL CHECK (length(query_digest) = 64),
        anchor_id TEXT NOT NULL,
        anchor_value TEXT NOT NULL,
        created_at_epoch INTEGER NOT NULL,
        expires_at_epoch INTEGER NOT NULL,
        CHECK (length(CAST(anchor_id AS BLOB)) BETWEEN 1 AND 256),
        CHECK (length(CAST(anchor_value AS BLOB)) <= 4096),
        CHECK (expires_at_epoch > created_at_epoch)
    ) STRICT
    """,
    *_SCHEMA_V1[4:12],
    "CREATE INDEX local_operations_resource_idx ON local_operations(resource_type, resource_id)",
    "CREATE INDEX local_operations_state_idx ON local_operations(state, operation_id)",
    _SCHEMA_V1[12],
    "CREATE INDEX pagination_cursors_expiry_idx ON pagination_cursors(expires_at_epoch)",
)

_SCHEMA_V3 = (
    """
    CREATE TABLE schema_migrations (
        version INTEGER PRIMARY KEY CHECK (version BETWEEN 1 AND 3),
        applied_at TEXT NOT NULL CHECK (length(CAST(applied_at AS BLOB)) = 27)
    ) STRICT
    """,
    _SCHEMA_V2[1],
    f"""
    CREATE TABLE projects (
        project_id TEXT PRIMARY KEY,
        profile_id TEXT NOT NULL,
        name TEXT NOT NULL,
        document_json BLOB NOT NULL,
        state TEXT NOT NULL,
        current_revision_id TEXT,
        remote_state_json BLOB,
        resource_version INTEGER NOT NULL CHECK (resource_version >= 1),
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        FOREIGN KEY (profile_id) REFERENCES remote_profiles(profile_id) ON DELETE RESTRICT,
        CHECK (
            remote_state_json IS NULL OR
            length(remote_state_json) <= {MAX_REMOTE_PROJECT_STATE_BYTES}
        )
    ) STRICT
    """,
    *_SCHEMA_V2[3:],
)

_REMOTE_PAYLOAD_USAGE_TABLE_V4 = f"""
CREATE TABLE remote_payload_usage (
    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
    payload_count INTEGER NOT NULL
        CHECK (payload_count BETWEEN 0 AND {MAX_RECOVERY_ROWS}),
    payload_bytes INTEGER NOT NULL
        CHECK (payload_bytes BETWEEN 0 AND {MAX_REMOTE_PROJECT_STATE_RECOVERY_BYTES}),
    authority_tag BLOB NOT NULL CHECK (length(authority_tag) IN (0, 32)),
    CHECK (
        (payload_count = 0 AND payload_bytes = 0) OR
        (payload_count > 0 AND payload_bytes >= payload_count)
    )
) STRICT
"""

_REMOTE_PAYLOAD_INSERT_TRIGGER_V4 = """
CREATE TRIGGER projects_remote_payload_insert
AFTER INSERT ON projects
WHEN NEW.remote_state_json IS NOT NULL
BEGIN
    UPDATE remote_payload_usage
    SET payload_count = payload_count + 1,
        payload_bytes = payload_bytes + length(CAST(NEW.remote_state_json AS BLOB)),
        authority_tag = X''
    WHERE singleton = 1;
END
"""

_REMOTE_PAYLOAD_UPDATE_TRIGGER_V4 = """
CREATE TRIGGER projects_remote_payload_update
AFTER UPDATE OF remote_state_json ON projects
WHEN OLD.remote_state_json IS NOT NEW.remote_state_json
BEGIN
    UPDATE remote_payload_usage
    SET payload_count = payload_count
            + CASE WHEN NEW.remote_state_json IS NULL THEN 0 ELSE 1 END
            - CASE WHEN OLD.remote_state_json IS NULL THEN 0 ELSE 1 END,
        payload_bytes = payload_bytes
            + coalesce(length(CAST(NEW.remote_state_json AS BLOB)), 0)
            - coalesce(length(CAST(OLD.remote_state_json AS BLOB)), 0),
        authority_tag = X''
    WHERE singleton = 1;
END
"""

_REMOTE_PAYLOAD_DELETE_TRIGGER_V4 = """
CREATE TRIGGER projects_remote_payload_delete
AFTER DELETE ON projects
WHEN OLD.remote_state_json IS NOT NULL
BEGIN
    UPDATE remote_payload_usage
    SET payload_count = payload_count - 1,
        payload_bytes = payload_bytes - length(CAST(OLD.remote_state_json AS BLOB)),
        authority_tag = X''
    WHERE singleton = 1;
END
"""

_SCHEMA_V4 = (
    """
    CREATE TABLE schema_migrations (
        version INTEGER PRIMARY KEY CHECK (version BETWEEN 1 AND 4),
        applied_at TEXT NOT NULL CHECK (length(CAST(applied_at AS BLOB)) = 27)
    ) STRICT
    """,
    *_SCHEMA_V3[1:],
    _REMOTE_PAYLOAD_USAGE_TABLE_V4,
    _REMOTE_PAYLOAD_INSERT_TRIGGER_V4,
    _REMOTE_PAYLOAD_UPDATE_TRIGGER_V4,
    _REMOTE_PAYLOAD_DELETE_TRIGGER_V4,
)

_PROJECTS_TABLE_V5 = f"""
CREATE TABLE projects (
    project_id TEXT PRIMARY KEY,
    profile_id TEXT NOT NULL,
    name TEXT NOT NULL,
    document_json BLOB NOT NULL,
    state TEXT NOT NULL,
    current_revision_id TEXT,
    remote_state_json BLOB,
    remote_state_token_0 INTEGER,
    remote_state_token_1 INTEGER,
    remote_state_token_2 INTEGER,
    remote_state_token_3 INTEGER,
    resource_version INTEGER NOT NULL CHECK (resource_version >= 1),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    FOREIGN KEY (profile_id) REFERENCES remote_profiles(profile_id) ON DELETE RESTRICT,
    CHECK (
        remote_state_json IS NULL OR
        length(remote_state_json) <= {MAX_REMOTE_PROJECT_STATE_BYTES}
    ),
    CHECK (
        (remote_state_json IS NULL AND
         remote_state_token_0 IS NULL AND remote_state_token_1 IS NULL AND
         remote_state_token_2 IS NULL AND remote_state_token_3 IS NULL) OR
        (remote_state_json IS NOT NULL AND
         remote_state_token_0 BETWEEN 0 AND {_REMOTE_CONTENT_ACCUMULATOR_MODULUS - 1} AND
         remote_state_token_1 BETWEEN 0 AND {_REMOTE_CONTENT_ACCUMULATOR_MODULUS - 1} AND
         remote_state_token_2 BETWEEN 0 AND {_REMOTE_CONTENT_ACCUMULATOR_MODULUS - 1} AND
         remote_state_token_3 BETWEEN 0 AND {_REMOTE_CONTENT_ACCUMULATOR_MODULUS - 1})
    )
) STRICT
"""

_IDEMPOTENCY_RECORDS_TABLE_V5 = """
CREATE TABLE idempotency_records (
    principal TEXT NOT NULL,
    method TEXT NOT NULL,
    route TEXT NOT NULL,
    resource_scope TEXT NOT NULL,
    idempotency_key TEXT NOT NULL,
    request_digest TEXT NOT NULL,
    operation_id TEXT UNIQUE,
    response_type TEXT NOT NULL,
    status_code INTEGER NOT NULL CHECK (status_code BETWEEN 100 AND 599),
    response_bytes BLOB NOT NULL,
    cleanup_eligible INTEGER NOT NULL CHECK (cleanup_eligible IN (0, 1)),
    created_at_epoch INTEGER NOT NULL,
    expires_at_epoch INTEGER NOT NULL,
    PRIMARY KEY (principal, method, route, resource_scope, idempotency_key),
    CHECK (principal = 'desktop-local-v1'),
    CHECK (length(CAST(method AS BLOB)) BETWEEN 1 AND 512),
    CHECK (length(CAST(route AS BLOB)) BETWEEN 1 AND 512),
    CHECK (length(CAST(resource_scope AS BLOB)) BETWEEN 1 AND 512),
    CHECK (length(CAST(idempotency_key AS BLOB)) BETWEEN 16 AND 256),
    CHECK (length(request_digest) = 64),
    CHECK (
        operation_id IS NULL OR
        length(CAST(operation_id AS BLOB)) BETWEEN 1 AND 512
    ),
    CHECK (response_type IN ('ProjectV1', 'RemoteProfileV1', 'LocalOperationV1')),
    CHECK (length(response_bytes) <= 136314880),
    CHECK (expires_at_epoch > created_at_epoch),
    CHECK (
        (response_type = 'LocalOperationV1' AND operation_id IS NOT NULL) OR
        (response_type != 'LocalOperationV1' AND operation_id IS NULL)
    ),
    CHECK (response_type = 'LocalOperationV1' OR cleanup_eligible = 1),
    FOREIGN KEY (operation_id) REFERENCES local_operations(operation_id) ON DELETE RESTRICT
) STRICT
"""

_PROVIDER_STORAGE_USAGE_TABLE_V5 = f"""
CREATE TABLE provider_storage_usage (
    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
    total_rows INTEGER NOT NULL CHECK (total_rows >= 1),
    total_bytes INTEGER NOT NULL CHECK (total_bytes >= 0),
    remote_payload_count INTEGER NOT NULL CHECK (remote_payload_count >= 0),
    remote_payload_bytes INTEGER NOT NULL CHECK (remote_payload_bytes >= 0),
    remote_accumulator_0 INTEGER NOT NULL
        CHECK (remote_accumulator_0 BETWEEN 0 AND {_REMOTE_CONTENT_ACCUMULATOR_MODULUS - 1}),
    remote_accumulator_1 INTEGER NOT NULL
        CHECK (remote_accumulator_1 BETWEEN 0 AND {_REMOTE_CONTENT_ACCUMULATOR_MODULUS - 1}),
    remote_accumulator_2 INTEGER NOT NULL
        CHECK (remote_accumulator_2 BETWEEN 0 AND {_REMOTE_CONTENT_ACCUMULATOR_MODULUS - 1}),
    remote_accumulator_3 INTEGER NOT NULL
        CHECK (remote_accumulator_3 BETWEEN 0 AND {_REMOTE_CONTENT_ACCUMULATOR_MODULUS - 1}),
    profile_reservations INTEGER NOT NULL CHECK (profile_reservations >= 0),
    project_reservations INTEGER NOT NULL CHECK (project_reservations >= 0),
    idempotency_record_count INTEGER NOT NULL CHECK (idempotency_record_count >= 0),
    pagination_cursor_count INTEGER NOT NULL CHECK (pagination_cursor_count >= 0),
    generation INTEGER NOT NULL CHECK (generation BETWEEN 0 AND 9223372036854775806),
    authority_tag BLOB NOT NULL CHECK (length(authority_tag) IN (0, 32)),
    CHECK (
        (remote_payload_count = 0 AND remote_payload_bytes = 0) OR
        (remote_payload_count > 0 AND remote_payload_bytes >= remote_payload_count)
    )
) STRICT, WITHOUT ROWID
"""


def _provider_usage_trigger_v5(table: str, columns: tuple[str, ...], operation: str) -> str:
    operation_lower = operation.lower()
    if operation == "INSERT":
        row_delta = "1"
        byte_delta = _usage_length_sql(columns, prefix="NEW")
    elif operation == "DELETE":
        row_delta = "-1"
        byte_delta = f"-({_usage_length_sql(columns, prefix='OLD')})"
    else:
        row_delta = "0"
        byte_delta = (
            f"({_usage_length_sql(columns, prefix='NEW')}) - "
            f"({_usage_length_sql(columns, prefix='OLD')})"
        )
    extra_assignments = ""
    if table == "projects":
        if operation == "INSERT":
            count_delta = "CASE WHEN NEW.remote_state_json IS NULL THEN 0 ELSE 1 END"
            remote_byte_delta = "coalesce(length(CAST(NEW.remote_state_json AS BLOB)), 0)"
            token_expression = tuple(
                f"coalesce(NEW.remote_state_token_{index}, 0)" for index in range(4)
            )
        elif operation == "DELETE":
            count_delta = "-CASE WHEN OLD.remote_state_json IS NULL THEN 0 ELSE 1 END"
            remote_byte_delta = "-coalesce(length(CAST(OLD.remote_state_json AS BLOB)), 0)"
            token_expression = tuple(
                f"-coalesce(OLD.remote_state_token_{index}, 0)" for index in range(4)
            )
        else:
            count_delta = (
                "CASE WHEN NEW.remote_state_json IS NULL THEN 0 ELSE 1 END - "
                "CASE WHEN OLD.remote_state_json IS NULL THEN 0 ELSE 1 END"
            )
            remote_byte_delta = (
                "coalesce(length(CAST(NEW.remote_state_json AS BLOB)), 0) - "
                "coalesce(length(CAST(OLD.remote_state_json AS BLOB)), 0)"
            )
            token_expression = tuple(
                f"-coalesce(OLD.remote_state_token_{index}, 0) + "
                f"coalesce(NEW.remote_state_token_{index}, 0)"
                for index in range(4)
            )
        extra_assignments += (
            f", remote_payload_count = remote_payload_count + ({count_delta})"
            f", remote_payload_bytes = remote_payload_bytes + ({remote_byte_delta})"
        )
        extra_assignments += "".join(
            f", remote_accumulator_{index} = "
            f"(remote_accumulator_{index} + ({expression}) + "
            f"{_REMOTE_CONTENT_ACCUMULATOR_MODULUS}) % "
            f"{_REMOTE_CONTENT_ACCUMULATOR_MODULUS}"
            for index, expression in enumerate(token_expression)
        )
    if table == "local_operations":
        profile_new = _operation_reservation_sql(prefix="NEW", kinds=_PROFILE_OPERATION_KINDS_SQL)
        profile_old = _operation_reservation_sql(prefix="OLD", kinds=_PROFILE_OPERATION_KINDS_SQL)
        project_new = _operation_reservation_sql(prefix="NEW", kinds=_PROJECT_OPERATION_KINDS_SQL)
        project_old = _operation_reservation_sql(prefix="OLD", kinds=_PROJECT_OPERATION_KINDS_SQL)
        if operation == "INSERT":
            profile_delta, project_delta = profile_new, project_new
        elif operation == "DELETE":
            profile_delta, project_delta = f"-({profile_old})", f"-({project_old})"
        else:
            profile_delta = f"({profile_new}) - ({profile_old})"
            project_delta = f"({project_new}) - ({project_old})"
        extra_assignments += (
            f", profile_reservations = profile_reservations + ({profile_delta})"
            f", project_reservations = project_reservations + ({project_delta})"
        )
    if table == "idempotency_records":
        extra_assignments += (
            f", idempotency_record_count = idempotency_record_count + ({row_delta})"
        )
    if table == "pagination_cursors":
        extra_assignments += f", pagination_cursor_count = pagination_cursor_count + ({row_delta})"
    return f"""
CREATE TRIGGER provider_usage_{table}_{operation_lower}
AFTER {operation} ON {table}
BEGIN
    UPDATE provider_storage_usage
    SET total_rows = total_rows + ({row_delta}),
        total_bytes = total_bytes + ({byte_delta}),
        generation = generation + 1,
        authority_tag = X''
        {extra_assignments}
    WHERE singleton = 1;
    SELECT CASE WHEN changes() != 1
        THEN RAISE(ABORT, 'provider storage usage authority is missing') END;
END
"""


_PROVIDER_USAGE_MAINTENANCE_TRIGGERS_V5 = tuple(
    _provider_usage_trigger_v5(table, columns, operation)
    for table, columns in _RECOVERY_USAGE_SPECIFICATIONS
    for operation in ("INSERT", "UPDATE", "DELETE")
)
_PROVIDER_USAGE_INSERT_GUARD_V5 = """
CREATE TRIGGER provider_storage_usage_no_insert
BEFORE INSERT ON provider_storage_usage
WHEN EXISTS (SELECT 1 FROM provider_storage_usage)
  OR (SELECT count(*) FROM schema_migrations) >= 5
BEGIN
    SELECT RAISE(ABORT, 'provider storage usage authority cannot be inserted');
END
"""
_PROVIDER_USAGE_DELETE_GUARD_V5 = """
CREATE TRIGGER provider_storage_usage_no_delete
BEFORE DELETE ON provider_storage_usage
BEGIN
    SELECT RAISE(ABORT, 'provider storage usage authority cannot be deleted');
END
"""
_SCHEMA_MIGRATION_INSERT_GUARD_V5 = """
CREATE TRIGGER schema_migrations_no_insert
BEFORE INSERT ON schema_migrations
WHEN (SELECT count(*) FROM schema_migrations) >= 5
BEGIN
    SELECT RAISE(ABORT, 'provider migration ledger is immutable');
END
"""
_SCHEMA_MIGRATION_UPDATE_GUARD_V5 = """
CREATE TRIGGER schema_migrations_no_update
BEFORE UPDATE ON schema_migrations
BEGIN
    SELECT RAISE(ABORT, 'provider migration ledger is immutable');
END
"""
_SCHEMA_MIGRATION_DELETE_GUARD_V5 = """
CREATE TRIGGER schema_migrations_no_delete
BEFORE DELETE ON schema_migrations
BEGIN
    SELECT RAISE(ABORT, 'provider migration ledger is immutable');
END
"""

_SCHEMA_V5 = (
    """
    CREATE TABLE schema_migrations (
        version INTEGER PRIMARY KEY CHECK (version BETWEEN 1 AND 5),
        applied_at TEXT NOT NULL CHECK (length(CAST(applied_at AS BLOB)) = 27)
    ) STRICT
    """,
    _SCHEMA_V3[1],
    _PROJECTS_TABLE_V5,
    _SCHEMA_V3[3],
    _IDEMPOTENCY_RECORDS_TABLE_V5,
    *_SCHEMA_V3[5:16],
    "CREATE INDEX idempotency_expiry_idx ON "
    "idempotency_records(cleanup_eligible, expires_at_epoch)",
    _SCHEMA_V3[17],
    _PROVIDER_STORAGE_USAGE_TABLE_V5,
    *_PROVIDER_USAGE_MAINTENANCE_TRIGGERS_V5,
    _PROVIDER_USAGE_INSERT_GUARD_V5,
    _PROVIDER_USAGE_DELETE_GUARD_V5,
    _SCHEMA_MIGRATION_INSERT_GUARD_V5,
    _SCHEMA_MIGRATION_UPDATE_GUARD_V5,
    _SCHEMA_MIGRATION_DELETE_GUARD_V5,
)


def _schema_rows(connection: sqlite3.Connection) -> tuple[tuple[object, ...], ...]:
    count, byte_count = connection.execute(
        """
        SELECT count(*), coalesce(sum(
            length(CAST(type AS BLOB)) + length(CAST(name AS BLOB)) +
            length(CAST(tbl_name AS BLOB)) + coalesce(length(CAST(sql AS BLOB)), 0)
        ), 0)
        FROM sqlite_schema
        WHERE name NOT LIKE 'sqlite_%'
        """
    ).fetchone()
    if count > MAX_SCHEMA_OBJECTS or byte_count > MAX_SCHEMA_BYTES:
        raise ProviderSchemaError("provider schema exceeds its fingerprint bounds")
    return tuple(
        tuple(row)
        for row in connection.execute(
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
    digest = sha256(_canonical_json_bytes(rows)).hexdigest()
    return rows, digest


def _canonical_json_bytes(value: Any) -> bytes:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise ContractValidationError("value is not canonical JSON data") from exc


_EXPECTED_SCHEMA_V1_ROWS, _EXPECTED_SCHEMA_V1_DIGEST = _expected_schema(_SCHEMA_V1)
_EXPECTED_SCHEMA_V2_ROWS, _EXPECTED_SCHEMA_V2_DIGEST = _expected_schema(_SCHEMA_V2)
_EXPECTED_SCHEMA_V3_ROWS, _EXPECTED_SCHEMA_V3_DIGEST = _expected_schema(_SCHEMA_V3)
_EXPECTED_SCHEMA_V4_ROWS, _EXPECTED_SCHEMA_V4_DIGEST = _expected_schema(_SCHEMA_V4)
_EXPECTED_SCHEMA_ROWS, _EXPECTED_SCHEMA_DIGEST = _expected_schema(_SCHEMA_V5)


def _decode_json_object(raw: bytes, *, label: str) -> dict[str, Any]:
    try:
        value = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ProviderDataCorruptionError(f"stored {label} is not valid JSON") from exc
    if type(value) is not dict:
        raise ProviderDataCorruptionError(f"stored {label} is not a JSON object")
    try:
        canonical = _canonical_json_bytes(value)
    except ContractValidationError as exc:
        raise ProviderDataCorruptionError(f"stored {label} is not canonical JSON") from exc
    if canonical != raw:
        raise ProviderDataCorruptionError(f"stored {label} is not canonical JSON")
    return cast(dict[str, Any], value)


def _validate_model(model_type: type[_ModelT], value: object) -> _ModelT:
    try:
        return model_type.model_validate(value)
    except ValidationError as exc:
        raise ContractValidationError(f"{model_type.__name__} validation failed") from exc


def _validate_json_model(model_type: type[_ModelT], value: object) -> _ModelT:
    try:
        encoded = _canonical_json_bytes(value)
        return model_type.model_validate_json(encoded)
    except (ContractValidationError, ValidationError) as exc:
        raise ProviderDataCorruptionError(f"stored data violates {model_type.__name__}") from exc


class ProviderMutation:
    """Restricted state changes available inside an idempotent store transaction."""

    __slots__ = ("_connection", "_created_operation_ids", "_if_match", "_store")

    def __init__(
        self,
        store: DesktopProviderStore,
        connection: sqlite3.Connection,
        *,
        if_match: str | None = None,
    ) -> None:
        self._store = store
        self._connection = connection
        self._if_match = if_match
        self._created_operation_ids: list[str] = []

    def _require_bound_if_match(self, if_match: str) -> None:
        if self._if_match is not None and not hmac.compare_digest(self._if_match, if_match):
            raise ContractValidationError(
                "action mutation If-Match differs from its idempotency envelope"
            )

    def require_profile_authority(
        self,
        profile_id: str,
        *,
        if_match: str,
    ) -> RemoteProfileV1:
        """Validate profile authority before an external idempotent action."""

        self._store._validate_resource_id(profile_id)
        self._store._validate_if_match(if_match)
        self._require_bound_if_match(if_match)
        row = self._store._require_profile_row(self._connection, profile_id)
        self._store._require_etag("profile", profile_id, row, if_match)
        return self._store._profile_from_row(row)

    def require_project_authority(
        self,
        project_id: str,
        *,
        if_match: str,
    ) -> ProjectV1:
        """Validate project authority before an external idempotent action."""

        self._store._validate_resource_id(project_id)
        self._store._validate_if_match(if_match)
        self._require_bound_if_match(if_match)
        row = self._store._require_project_row(self._connection, project_id)
        self._store._require_etag("project", project_id, row, if_match)
        return self._store._project_from_row(row)

    def set_project_state(
        self,
        project_id: str,
        *,
        if_match: str,
        state: Literal["draft", "active", "archived", "blocked"],
        remote_state: RemoteProjectStateV1 | None = None,
        _reservation_operation_id: str | None = None,
    ) -> ProjectV1:
        self._store._validate_resource_id(project_id)
        self._store._validate_if_match(if_match)
        self._require_bound_if_match(if_match)
        if state not in {"draft", "active", "archived", "blocked"}:
            raise ContractValidationError("project state is not a Desktop v1 state")
        row = self._store._require_project_row(self._connection, project_id)
        self._store._require_etag("project", project_id, row, if_match)
        if state == "active":
            if remote_state is None:
                raise ContractValidationError(
                    "project activation requires a ready remote project state"
                )
            try:
                validated_remote = _validate_model(RemoteProjectStateV1, remote_state)
            except ContractValidationError as exc:
                raise ContractValidationError(
                    "project activation requires a valid remote project state"
                ) from exc
            self._store._validate_activation_remote_state(validated_remote)
            active_revision = validated_remote.active_revision
            if active_revision is None:
                raise ContractValidationError(
                    "project activation requires a ready remote project state"
                )
            remote_state_bytes = self._store._encode_remote_project_state(validated_remote)
            remote_state_token = self._store._remote_payload_content_token(
                project_id=project_id,
                payload=remote_state_bytes,
            )
            self._store._require_project_operation_reservation_available(
                self._connection,
                project_id,
                operation_kind="project_activate",
                excluded_operation_id=_reservation_operation_id,
            )
            self._connection.execute(
                """
                UPDATE projects
                SET state = 'draft', current_revision_id = NULL,
                    resource_version = resource_version + 1, updated_at = ?
                WHERE state = 'active' AND project_id != ?
                """,
                (self._store._timestamp(), project_id),
            )
            self._connection.execute(
                """
                UPDATE projects
                SET state = 'active', current_revision_id = ?, remote_state_json = ?,
                    remote_state_token_0 = ?, remote_state_token_1 = ?,
                    remote_state_token_2 = ?, remote_state_token_3 = ?,
                    resource_version = resource_version + 1, updated_at = ?
                WHERE project_id = ?
                """,
                (
                    active_revision.id,
                    remote_state_bytes,
                    *remote_state_token,
                    self._store._timestamp(),
                    project_id,
                ),
            )
        else:
            if remote_state is not None:
                raise ContractValidationError(
                    "non-activation project state cannot publish remote project state"
                )
            self._connection.execute(
                """
                UPDATE projects
                SET state = ?, current_revision_id = NULL,
                    resource_version = resource_version + 1, updated_at = ?
                WHERE project_id = ?
                """,
                (state, self._store._timestamp(), project_id),
            )
        return self._store._project_from_row(
            self._store._require_project_row(self._connection, project_id)
        )

    def set_profile_runtime_state(
        self,
        profile_id: str,
        *,
        if_match: str,
        connection_state: Literal[
            "disconnected",
            "connecting",
            "host_key_required",
            "connected",
            "failed",
        ],
        credential_slots: tuple[CredentialSlotStatusV1, ...],
        host_key_fingerprint: str | None,
    ) -> RemoteProfileV1:
        self._store._validate_resource_id(profile_id)
        self._store._validate_if_match(if_match)
        self._require_bound_if_match(if_match)
        if connection_state not in {
            "disconnected",
            "connecting",
            "host_key_required",
            "connected",
            "failed",
        }:
            raise ContractValidationError("profile connection state is not a Desktop v1 state")
        row = self._store._require_profile_row(self._connection, profile_id)
        self._store._require_etag("profile", profile_id, row, if_match)
        if connection_state in {"connecting", "host_key_required", "connected"}:
            self.disconnect_other_profiles(profile_id)
        timestamp = self._store._timestamp()
        current = self._store._profile_from_row(row)
        proposed = _validate_model(
            RemoteProfileV1,
            {
                **current.model_dump(mode="python"),
                "connection_state": connection_state,
                "credential_slots": credential_slots,
                "host_key_fingerprint": host_key_fingerprint,
                "etag": self._store._etag(
                    "profile", profile_id, cast(int, row["resource_version"]) + 1
                ),
                "updated_at": timestamp,
            },
        )
        self._connection.execute(
            """
            UPDATE remote_profiles
            SET connection_state = ?, credential_slots_json = ?,
                host_key_fingerprint = ?, resource_version = resource_version + 1,
                updated_at = ?
            WHERE profile_id = ?
            """,
            (
                connection_state,
                _canonical_json_bytes(
                    [slot.model_dump(mode="json") for slot in proposed.credential_slots]
                ),
                proposed.host_key_fingerprint,
                timestamp,
                profile_id,
            ),
        )
        if connection_state == "disconnected":
            self._store._reconcile_profile_operations(self._connection, profile_id)
        return self._store._profile_from_row(
            self._store._require_profile_row(self._connection, profile_id)
        )

    def disconnect_other_profiles(self, owner_profile_id: str) -> None:
        self._store._validate_resource_id(owner_profile_id)
        rows = self._connection.execute(
            """
            SELECT profile_id
            FROM remote_profiles
            WHERE profile_id != ? AND connection_state != 'disconnected'
            ORDER BY profile_id
            """,
            (owner_profile_id,),
        ).fetchall()
        if not rows:
            return
        timestamp = self._store._timestamp()
        profile_ids = [cast(str, row["profile_id"]) for row in rows]
        self._connection.execute(
            """
            UPDATE remote_profiles
            SET connection_state = 'disconnected',
                resource_version = resource_version + 1, updated_at = ?
            WHERE profile_id != ? AND connection_state != 'disconnected'
            """,
            (timestamp, owner_profile_id),
        )
        for profile_id in profile_ids:
            self._store._reconcile_profile_operations(self._connection, profile_id)

    def cancel_nonterminal_profile_operations(self, profile_id: str) -> None:
        self._store._validate_resource_id(profile_id)
        rows = self._connection.execute(
            """
            SELECT operation_id
            FROM local_operations
            WHERE resource_type = 'profile' AND resource_id = ?
              AND state IN ('queued', 'running', 'cancelling')
            ORDER BY operation_id
            """,
            (profile_id,),
        ).fetchall()
        for row in rows:
            self._store._cancel_operation_with_authority(
                self._connection,
                self._store._require_operation_row(
                    self._connection, cast(str, row["operation_id"])
                ),
            )

    def create_local_operation(
        self,
        *,
        operation_kind: str,
        resource: ResourceRefV1,
        state: Literal["queued", "running", "succeeded", "failed", "cancelling", "cancelled"],
        progress: OperationProgressV1 | None = None,
        checks: tuple[NormalizedCheckV1, ...] = (),
        result: LocalOperationResultV1 | None = None,
        error: ApiErrorV1 | None = None,
    ) -> LocalOperationV1:
        if operation_kind not in _OPERATION_KINDS or state not in _OPERATION_STATES:
            raise ContractValidationError("local operation kind or state is invalid")
        operation_id = self._store._new_id()
        timestamp = self._store._timestamp()
        terminal = state in {"succeeded", "failed", "cancelled"}
        operation = _validate_model(
            LocalOperationV1,
            {
                "operation_id": operation_id,
                "operation_kind": operation_kind,
                "state": state,
                "resource": resource,
                "progress": progress,
                "checks": checks,
                "result": result,
                "error": error,
                "created_at": timestamp,
                "started_at": None if state == "queued" else timestamp,
                "finished_at": timestamp if terminal else None,
                "etag": self._store._etag("operation", operation_id, 1),
            },
        )
        self._store._validate_operation_authority(self._connection, operation)
        document = _canonical_json_bytes(operation.model_dump(mode="json"))
        if not terminal:
            if operation_kind in _PROJECT_OPERATION_KINDS:
                self._store._require_project_operation_reservation_available(
                    self._connection,
                    resource.resource_id,
                    operation_kind=operation_kind,
                )
            self._store._validate_nonterminal_operation_terminal_capacity(
                self._connection,
                operation,
            )
        self._connection.execute(
            """
            INSERT INTO local_operations(
                operation_id, operation_kind, state, resource_type, resource_id,
                document_json, resource_version, created_at, finished_at
            ) VALUES (?, ?, ?, ?, ?, ?, 1, ?, ?)
            """,
            (
                operation.operation_id,
                operation.operation_kind,
                operation.state,
                operation.resource.resource_type,
                operation.resource.resource_id,
                document,
                operation.created_at,
                operation.finished_at,
            ),
        )
        self._created_operation_ids.append(operation.operation_id)
        return operation

    def _validate_created_operations_bound(self) -> None:
        for operation_id in self._created_operation_ids:
            row = self._store._require_operation_row(self._connection, operation_id)
            if (
                type(row["action_identity_digest"]) is not str
                or _DIGEST_RE.fullmatch(row["action_identity_digest"]) is None
            ):
                raise ProviderDataCorruptionError(
                    "operation action authority digest is missing or invalid"
                )
            operation = self._store._operation_from_row(row)
            self._store._idempotency_rows_for_operation(
                self._connection,
                operation,
                bytes(row["document_json"]),
            )

    def _create_profile(self, request: RemoteProfileCreateV1) -> RemoteProfileV1:
        profile_id = self._store._new_id()
        timestamp = self._store._timestamp()
        document = _canonical_json_bytes(request.model_dump(mode="json"))
        self._connection.execute(
            """
            INSERT INTO remote_profiles(
                profile_id, name, document_json, connection_state,
                credential_slots_json, host_key_fingerprint, resource_version,
                created_at, updated_at
            ) VALUES (?, ?, ?, 'disconnected', ?, NULL, 1, ?, ?)
            """,
            (profile_id, request.name, document, b"[]", timestamp, timestamp),
        )
        return self._store._profile_from_row(
            self._store._require_profile_row(self._connection, profile_id)
        )

    def _create_project(self, request: ProjectCreateV1) -> ProjectV1:
        self._store._require_profile_row(self._connection, request.profile_id)
        project_id = (
            project_id_for_native_import(request.source.import_ref.import_id)
            if request.source.kind == "native_folder_snapshot"
            and request.source.import_ref is not None
            else self._store._new_id()
        )
        timestamp = self._store._timestamp()
        self._connection.execute(
            """
            INSERT INTO projects(
                project_id, profile_id, name, document_json, state,
                current_revision_id, remote_state_json, resource_version, created_at, updated_at
            ) VALUES (?, ?, ?, ?, 'draft', NULL, NULL, 1, ?, ?)
            """,
            (
                project_id,
                request.profile_id,
                request.name,
                _canonical_json_bytes(request.model_dump(mode="json")),
                timestamp,
                timestamp,
            ),
        )
        return self._store._project_from_row(
            self._store._require_project_row(self._connection, project_id)
        )


class DesktopProviderStore:
    """Durable, local-only persistence for Desktop Local API v1 resources."""

    def __init__(
        self,
        state_root: Path | str,
        *,
        clock: Callable[[], datetime] | None = None,
        cursor_ttl_seconds: int = DEFAULT_CURSOR_TTL_SECONDS,
        idempotency_retention_seconds: int = DEFAULT_IDEMPOTENCY_RETENTION_SECONDS,
        max_idempotency_records: int = DEFAULT_IDEMPOTENCY_RECORD_LIMIT,
        max_cursor_records: int = DEFAULT_CURSOR_RECORD_LIMIT,
    ) -> None:
        self._require_secure_platform()
        if type(cursor_ttl_seconds) is not int or cursor_ttl_seconds < 1:
            raise ValueError("cursor_ttl_seconds must be a positive integer")
        if type(idempotency_retention_seconds) is not int or idempotency_retention_seconds < 1:
            raise ValueError("idempotency_retention_seconds must be a positive integer")
        if type(max_idempotency_records) is not int or max_idempotency_records < 1:
            raise ValueError("max_idempotency_records must be a positive integer")
        if type(max_cursor_records) is not int or max_cursor_records < 1:
            raise ValueError("max_cursor_records must be a positive integer")

        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._cursor_ttl_seconds = cursor_ttl_seconds
        self._idempotency_retention_seconds = idempotency_retention_seconds
        self._max_idempotency_records = max_idempotency_records
        self._max_cursor_records = max_cursor_records
        self._closed = False
        self._transaction_lock = threading.RLock()

        root = Path(os.path.abspath(os.fspath(Path(state_root).expanduser())))
        self._create_or_validate_root(root)
        self._state_root = root
        flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
        try:
            self._root_fd = os.open(root, flags)
        except OSError as exc:
            raise ProviderStateRootError(
                "provider state root could not be securely opened"
            ) from exc
        root_stat = os.fstat(self._root_fd)
        self._root_identity = (root_stat.st_dev, root_stat.st_ino)

        try:
            self._ensure_empty_private_file(OWNER_LOCK_FILENAME)
            self._acquire_owner_lock()
            self._ensure_empty_private_file(DATABASE_FILENAME)
            self._cursor_key = self._load_or_create_cursor_key()
            self._verify_storage_files()
            self._connection = self._open_database_connection()
            self._migrate()
            self._recover_and_validate()
            self._verify_storage_files()
        except BaseException:
            self._close_resources()
            raise

    @property
    def database_path(self) -> Path:
        return self._state_root / DATABASE_FILENAME

    @property
    def state_root(self) -> Path:
        return self._state_root

    def close(self) -> None:
        with self._transaction_lock:
            if not self._closed:
                self._close_resources()

    @contextmanager
    def workspace_import_reference_guard(self) -> Iterator[None]:
        """Serialize durable project references before taking the import-store lock."""

        with self._transaction_lock:
            self._verify_storage_files()
            yield

    def _close_resources(self) -> None:
        connection = getattr(self, "_connection", None)
        if connection is not None:
            try:
                connection.close()
            finally:
                del self._connection
        owner_lock_fd = getattr(self, "_owner_lock_fd", None)
        if owner_lock_fd is not None:
            try:
                fcntl.flock(owner_lock_fd, fcntl.LOCK_UN)
            finally:
                os.close(owner_lock_fd)
                del self._owner_lock_fd
        root_fd = getattr(self, "_root_fd", None)
        if root_fd is not None:
            os.close(root_fd)
            del self._root_fd
        self._closed = True

    def __enter__(self) -> DesktopProviderStore:
        self._verify_root()
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

    @staticmethod
    def _require_secure_platform() -> None:
        if (
            not hasattr(os, "O_NOFOLLOW")
            or not hasattr(os, "O_DIRECTORY")
            or os.open not in os.supports_dir_fd
            or os.stat not in os.supports_dir_fd
            or os.stat not in os.supports_follow_symlinks
        ):
            raise ProviderStateRootError(
                "platform lacks no-follow descriptor-relative provider storage"
            )

    @staticmethod
    def _create_or_validate_root(root: Path) -> None:
        try:
            root_stat = os.lstat(root)
        except FileNotFoundError:
            root.parent.mkdir(parents=True, exist_ok=True)
            try:
                os.mkdir(root, 0o700)
            except FileExistsError:
                root_stat = os.lstat(root)
            else:
                os.chmod(root, 0o700, follow_symlinks=False)
                root_stat = os.lstat(root)
        if not stat.S_ISDIR(root_stat.st_mode) or stat.S_ISLNK(root_stat.st_mode):
            raise ProviderStateRootError("provider state root must be a real directory")
        if hasattr(os, "getuid") and root_stat.st_uid != os.getuid():
            raise ProviderStateRootError("provider state root must be owned by this user")
        if stat.S_IMODE(root_stat.st_mode) != 0o700:
            raise ProviderStateRootError("provider state root mode must be 0700")

    def _verify_root(self) -> None:
        if self._closed:
            raise ProviderStateRootError("provider store is closed")
        try:
            path_stat = os.lstat(self._state_root)
            fd_stat = os.fstat(self._root_fd)
        except OSError as exc:
            raise ProviderStateRootError("provider state root is unavailable") from exc
        if (
            not stat.S_ISDIR(path_stat.st_mode)
            or stat.S_ISLNK(path_stat.st_mode)
            or (path_stat.st_dev, path_stat.st_ino) != self._root_identity
            or (fd_stat.st_dev, fd_stat.st_ino) != self._root_identity
        ):
            raise ProviderStateRootError("provider state root identity changed")
        if hasattr(os, "getuid") and path_stat.st_uid != os.getuid():
            raise ProviderStateRootError("provider state root ownership changed")
        if stat.S_IMODE(path_stat.st_mode) != 0o700:
            raise ProviderStateRootError("provider state root mode changed")

    def _ensure_empty_private_file(self, name: str) -> None:
        self._verify_root()
        flags = os.O_RDWR | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
        try:
            fd = os.open(name, flags, 0o600, dir_fd=self._root_fd)
        except FileExistsError:
            self._verify_private_file(name)
            return
        except OSError as exc:
            raise ProviderStateRootError(f"could not create private provider file {name}") from exc
        try:
            os.fchmod(fd, 0o600)
            os.fsync(fd)
        finally:
            os.close(fd)
        os.fsync(self._root_fd)

    def _verify_private_file(self, name: str) -> os.stat_result:
        self._verify_root()
        try:
            file_stat = os.stat(name, dir_fd=self._root_fd, follow_symlinks=False)
        except OSError as exc:
            raise ProviderStateRootError(f"private provider file {name} is unavailable") from exc
        return self._validate_private_file_stat(name, file_stat)

    @staticmethod
    def _validate_private_file_stat(name: str, file_stat: os.stat_result) -> os.stat_result:
        if not stat.S_ISREG(file_stat.st_mode) or stat.S_ISLNK(file_stat.st_mode):
            raise ProviderStateRootError(f"private provider file {name} must be regular")
        if file_stat.st_nlink != 1:
            raise ProviderStateRootError(f"private provider file {name} must have one link")
        if hasattr(os, "getuid") and file_stat.st_uid != os.getuid():
            raise ProviderStateRootError(f"private provider file {name} has the wrong owner")
        if stat.S_IMODE(file_stat.st_mode) != 0o600:
            raise ProviderStateRootError(f"private provider file {name} mode must be 0600")
        return file_stat

    def _optional_private_file(self, name: str) -> os.stat_result | None:
        self._verify_root()
        try:
            file_stat = os.stat(name, dir_fd=self._root_fd, follow_symlinks=False)
        except FileNotFoundError:
            return None
        except OSError as exc:
            raise ProviderStateRootError(
                f"SQLite side file {name} could not be inspected"
            ) from exc
        return self._validate_private_file_stat(name, file_stat)

    def _acquire_owner_lock(self) -> None:
        flags = os.O_RDWR | getattr(os, "O_NOFOLLOW", 0)
        expected_stat = self._verify_private_file(OWNER_LOCK_FILENAME)
        try:
            descriptor = os.open(OWNER_LOCK_FILENAME, flags, dir_fd=self._root_fd)
        except OSError as exc:
            raise ProviderStateRootError("provider owner lock could not be opened") from exc
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            os.close(descriptor)
            raise ProviderStateRootError(
                "provider state root is already owned by another process"
            ) from exc
        except OSError:
            os.close(descriptor)
            raise
        descriptor_stat = os.fstat(descriptor)
        if (descriptor_stat.st_dev, descriptor_stat.st_ino) != (
            expected_stat.st_dev,
            expected_stat.st_ino,
        ):
            fcntl.flock(descriptor, fcntl.LOCK_UN)
            os.close(descriptor)
            raise ProviderStateRootError("provider owner lock identity changed")
        self._owner_lock_fd = descriptor

    def _open_database_connection(self) -> sqlite3.Connection:
        self._verify_storage_files()
        connection = sqlite3.connect(
            self.database_path,
            timeout=30,
            isolation_level=None,
            check_same_thread=False,
        )
        connection.row_factory = sqlite3.Row
        try:
            database_rows = connection.execute("PRAGMA database_list").fetchall()
            if len(database_rows) != 1:
                raise ProviderStateRootError("SQLite opened an unexpected database set")
            opened_path = os.path.abspath(cast(str, database_rows[0][2]))
            if opened_path != os.path.abspath(self.database_path):
                raise ProviderStateRootError("SQLite opened an unexpected provider database")
            connection.execute("PRAGMA foreign_keys = ON")
            connection.execute("PRAGMA busy_timeout = 30000")
            journal_mode = connection.execute("PRAGMA journal_mode = DELETE").fetchone()[0]
            if journal_mode != "delete":
                raise ProviderStateRootError("SQLite rollback journal mode is unavailable")
            connection.execute("PRAGMA synchronous = FULL")
            connection.execute("PRAGMA temp_store = MEMORY")
            connection.execute("PRAGMA trusted_schema = OFF")
            page_size = cast(int, connection.execute("PRAGMA page_size").fetchone()[0])
            if page_size < 512 or page_size > 65_536:
                raise ProviderStateRootError("SQLite page size is outside provider bounds")
            self._max_page_count = MAX_DATABASE_BYTES // page_size
            configured_pages = cast(
                int,
                connection.execute(f"PRAGMA max_page_count = {self._max_page_count}").fetchone()[
                    0
                ],
            )
            if configured_pages != self._max_page_count:
                raise ProviderStateRootError("SQLite max_page_count could not be enforced")
            self._journal_size_limit = MAX_JOURNAL_BYTES
            configured_journal = cast(
                int,
                connection.execute(
                    f"PRAGMA journal_size_limit = {self._journal_size_limit}"
                ).fetchone()[0],
            )
            if configured_journal != self._journal_size_limit:
                raise ProviderStateRootError("SQLite journal_size_limit could not be enforced")
            self._verify_storage_files()
        except BaseException:
            connection.close()
            raise
        return connection

    def _load_or_create_cursor_key(self) -> bytes:
        self._verify_root()
        try:
            key_stat = os.stat(
                CURSOR_KEY_FILENAME,
                dir_fd=self._root_fd,
                follow_symlinks=False,
            )
        except FileNotFoundError:
            if not self._database_is_never_initialized():
                raise ProviderStateRootError(
                    "cursor signing key is missing from an initialized provider store"
                )
        except OSError as exc:
            raise ProviderStateRootError("could not inspect cursor signing key") from exc
        else:
            self._validate_private_file_stat(CURSOR_KEY_FILENAME, key_stat)
            if key_stat.st_size == _CURSOR_KEY_BYTES:
                return self._read_cursor_key()
            if not self._database_is_never_initialized():
                raise ProviderStateRootError("cursor signing key has an invalid size")
            self._unlink_invalid_cursor_key(key_stat)
        return self._create_cursor_key()

    def _database_is_never_initialized(self) -> bool:
        database_stat = self._verify_private_file(DATABASE_FILENAME)
        if database_stat.st_size != 0:
            return False
        return all(
            self._optional_private_file(name) is None
            for name in (JOURNAL_FILENAME, WAL_FILENAME, SHM_FILENAME)
        )

    def _unlink_invalid_cursor_key(self, expected_stat: os.stat_result) -> None:
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        try:
            descriptor = os.open(CURSOR_KEY_FILENAME, flags, dir_fd=self._root_fd)
        except OSError as exc:
            raise ProviderStateRootError("could not open invalid cursor signing key") from exc
        try:
            descriptor_stat = os.fstat(descriptor)
            if (descriptor_stat.st_dev, descriptor_stat.st_ino) != (
                expected_stat.st_dev,
                expected_stat.st_ino,
            ):
                raise ProviderStateRootError("cursor signing key identity changed")
            os.unlink(CURSOR_KEY_FILENAME, dir_fd=self._root_fd)
        finally:
            os.close(descriptor)

    def _create_cursor_key(self) -> bytes:
        key = secrets.token_bytes(_CURSOR_KEY_BYTES)
        temporary_name = f"{_CURSOR_KEY_TEMP_PREFIX}{secrets.token_hex(16)}"
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
        descriptor: int | None = None
        try:
            descriptor = os.open(temporary_name, flags, 0o600, dir_fd=self._root_fd)
            os.fchmod(descriptor, 0o600)
            remaining = memoryview(key)
            while remaining:
                written = os.write(descriptor, remaining)
                if written <= 0:
                    raise OSError(errno.EIO, "cursor signing key write made no progress")
                remaining = remaining[written:]
            os.fsync(descriptor)
            os.close(descriptor)
            descriptor = None
            try:
                _rename_noreplace(
                    temporary_name,
                    CURSOR_KEY_FILENAME,
                    directory_fd=self._root_fd,
                )
            except FileExistsError:
                return self._read_cursor_key()
            os.fsync(self._root_fd)
            self._verify_private_file(CURSOR_KEY_FILENAME)
            return key
        except ProviderStoreError:
            raise
        except OSError as exc:
            action = "publish" if descriptor is None else "create"
            raise ProviderStateRootError(f"could not {action} cursor signing key") from exc
        finally:
            cleanup_error: OSError | None = None
            if descriptor is not None:
                try:
                    os.close(descriptor)
                except OSError as exc:
                    cleanup_error = exc
            removed_temporary = False
            try:
                os.unlink(temporary_name, dir_fd=self._root_fd)
            except FileNotFoundError:
                pass
            except OSError as exc:
                cleanup_error = cleanup_error or exc
            else:
                removed_temporary = True
            if removed_temporary:
                try:
                    os.fsync(self._root_fd)
                except OSError as exc:
                    cleanup_error = cleanup_error or exc
            if cleanup_error is not None:
                raise ProviderStateRootError(
                    "could not clean up cursor signing key temporary file"
                ) from cleanup_error

    def _read_cursor_key(self) -> bytes:
        self._verify_private_file(CURSOR_KEY_FILENAME)
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        try:
            fd = os.open(CURSOR_KEY_FILENAME, flags, dir_fd=self._root_fd)
        except OSError as exc:
            raise ProviderStateRootError("could not open cursor signing key") from exc
        try:
            key = os.read(fd, _CURSOR_KEY_BYTES + 1)
        finally:
            os.close(fd)
        if len(key) != _CURSOR_KEY_BYTES:
            raise ProviderStateRootError("cursor signing key has an invalid size")
        return key

    def _verify_storage_files(self) -> None:
        self._verify_root()
        for name in (DATABASE_FILENAME, OWNER_LOCK_FILENAME, CURSOR_KEY_FILENAME):
            self._verify_private_file(name)
        for name in (JOURNAL_FILENAME, WAL_FILENAME, SHM_FILENAME):
            self._optional_private_file(name)
        for name in (WAL_FILENAME, SHM_FILENAME):
            if self._optional_private_file(name) is not None:
                raise ProviderStateRootError(f"SQLite side file {name} is forbidden")
        self._verify_storage_budget()

    def _verify_storage_budget(self) -> None:
        database_stat = self._verify_private_file(DATABASE_FILENAME)
        if database_stat.st_size > MAX_DATABASE_BYTES:
            raise ProviderStateRootError("provider database exceeds its byte budget")
        journal_stat = self._optional_private_file(JOURNAL_FILENAME)
        if journal_stat is None:
            return
        if journal_stat.st_size > MAX_JOURNAL_BYTES:
            raise ProviderStateRootError("provider journal exceeds its byte budget")

    def _migrate(self) -> None:
        connection = self._connection
        try:
            connection.execute("BEGIN EXCLUSIVE")
            version = cast(int, connection.execute("PRAGMA user_version").fetchone()[0])
            if version == 0:
                existing = {
                    row[0]
                    for row in connection.execute(
                        "SELECT name FROM sqlite_master WHERE type = 'table'"
                    )
                }
                if existing:
                    raise ProviderSchemaError("unversioned provider database is not empty")
                for statement in _SCHEMA_V5:
                    connection.execute(statement)
                timestamp = self._timestamp()
                self._insert_provider_storage_usage(
                    connection,
                    total_rows=1,
                    total_bytes=_PROVIDER_USAGE_ACCOUNTED_BYTES,
                    remote_payload_count=0,
                    remote_payload_bytes=0,
                    remote_accumulators=(0, 0, 0, 0),
                    profile_reservations=0,
                    project_reservations=0,
                    idempotency_record_count=0,
                    pagination_cursor_count=0,
                    generation=0,
                )
                connection.executemany(
                    "INSERT INTO schema_migrations(version, applied_at) VALUES (?, ?)",
                    (
                        (1, timestamp),
                        (2, timestamp),
                        (3, timestamp),
                        (4, timestamp),
                        (5, timestamp),
                    ),
                )
                self._validate_unsealed_write_budget(connection)
                self._seal_provider_storage_usage(connection)
                connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
            elif version == 1:
                self._validate_schema_version(
                    connection,
                    rows=_EXPECTED_SCHEMA_V1_ROWS,
                    digest=_EXPECTED_SCHEMA_V1_DIGEST,
                    label="v1",
                )
                self._validate_migration_rows(connection, expected_version=1)
                self._migrate_v1_to_v2(connection)
                self._validate_schema_version(
                    connection,
                    rows=_EXPECTED_SCHEMA_V2_ROWS,
                    digest=_EXPECTED_SCHEMA_V2_DIGEST,
                    label="v2",
                )
                self._validate_migration_rows(connection, expected_version=2)
                self._migrate_v2_to_v3(connection)
                self._migrate_v3_to_v4(connection)
                self._migrate_v4_to_v5(connection)
            elif version == 2:
                self._validate_schema_version(
                    connection,
                    rows=_EXPECTED_SCHEMA_V2_ROWS,
                    digest=_EXPECTED_SCHEMA_V2_DIGEST,
                    label="v2",
                )
                self._validate_migration_rows(connection, expected_version=2)
                self._migrate_v2_to_v3(connection)
                self._migrate_v3_to_v4(connection)
                self._migrate_v4_to_v5(connection)
            elif version == 3:
                self._validate_schema_version(
                    connection,
                    rows=_EXPECTED_SCHEMA_V3_ROWS,
                    digest=_EXPECTED_SCHEMA_V3_DIGEST,
                    label="v3",
                )
                self._validate_migration_rows(connection, expected_version=3)
                self._migrate_v3_to_v4(connection)
                self._migrate_v4_to_v5(connection)
            elif version == 4:
                self._validate_schema_version(
                    connection,
                    rows=_EXPECTED_SCHEMA_V4_ROWS,
                    digest=_EXPECTED_SCHEMA_V4_DIGEST,
                    label="v4",
                )
                self._validate_migration_rows(connection, expected_version=4)
                self._migrate_v4_to_v5(connection)
            elif version != SCHEMA_VERSION:
                raise ProviderSchemaError(f"unsupported provider schema version {version}")
            self._validate_schema(connection)
            self._verify_storage_files()
            connection.commit()
        except ProviderStoreError:
            connection.rollback()
            raise
        except sqlite3.DatabaseError as exc:
            connection.rollback()
            raise ProviderSchemaError("provider schema migration validation failed") from exc
        except BaseException:
            connection.rollback()
            raise
        os.fsync(self._root_fd)

    def _migrate_v1_to_v2(self, connection: sqlite3.Connection) -> None:
        timestamp = self._timestamp()
        connection.execute("DROP INDEX idempotency_expiry_idx")
        connection.execute("ALTER TABLE idempotency_records RENAME TO idempotency_records_v1")
        connection.execute("ALTER TABLE schema_migrations RENAME TO schema_migrations_v1")
        connection.execute(_SCHEMA_V2[0])
        connection.execute(_SCHEMA_V2[3])
        connection.execute(_SCHEMA_V2[4])
        connection.execute(_SCHEMA_V2[5])
        connection.execute(
            """
            INSERT INTO schema_migrations(version, applied_at)
            SELECT version, applied_at FROM schema_migrations_v1
            """
        )
        connection.execute(
            "INSERT INTO schema_migrations(version, applied_at) VALUES (2, ?)",
            (timestamp,),
        )
        connection.execute(
            """
            INSERT INTO idempotency_records(
                principal, method, route, resource_scope, idempotency_key,
                request_digest, operation_id, response_type, status_code, response_bytes,
                created_at_epoch, expires_at_epoch
            )
            SELECT principal, method, route, resource_scope, idempotency_key,
                   request_digest, NULL, response_type, status_code, response_bytes,
                   created_at_epoch, expires_at_epoch
            FROM idempotency_records_v1
            """
        )
        connection.execute("DROP TABLE idempotency_records_v1")
        connection.execute("DROP TABLE schema_migrations_v1")
        for statement in _SCHEMA_V2[14:18]:
            connection.execute(statement)
        connection.execute("PRAGMA user_version = 2")

    def _migrate_v2_to_v3(self, connection: sqlite3.Connection) -> None:
        timestamp = self._timestamp()
        for index_name in (
            "projects_updated_idx",
            "projects_created_idx",
            "projects_name_idx",
            "projects_profile_idx",
            "projects_single_active_idx",
        ):
            connection.execute(f"DROP INDEX {index_name}")
        connection.execute("ALTER TABLE projects RENAME TO projects_v2")
        connection.execute("ALTER TABLE schema_migrations RENAME TO schema_migrations_v2")
        connection.execute(_SCHEMA_V3[0])
        connection.execute(_SCHEMA_V3[2])
        connection.execute(
            """
            INSERT INTO schema_migrations(version, applied_at)
            SELECT version, applied_at FROM schema_migrations_v2
            """
        )
        connection.execute(
            "INSERT INTO schema_migrations(version, applied_at) VALUES (3, ?)",
            (timestamp,),
        )
        connection.execute(
            """
            INSERT INTO projects(
                project_id, profile_id, name, document_json, state,
                current_revision_id, remote_state_json, resource_version,
                created_at, updated_at
            )
            SELECT project_id, profile_id, name, document_json,
                   CASE WHEN state = 'active' THEN 'draft' ELSE state END,
                   NULL, NULL,
                   resource_version + CASE
                       WHEN state = 'active' OR current_revision_id IS NOT NULL THEN 1 ELSE 0
                   END,
                   created_at,
                   CASE
                       WHEN state = 'active' OR current_revision_id IS NOT NULL THEN ?
                       ELSE updated_at
                   END
            FROM projects_v2
            """,
            (timestamp,),
        )
        connection.execute("DROP TABLE projects_v2")
        connection.execute("DROP TABLE schema_migrations_v2")
        for statement in _SCHEMA_V3[9:14]:
            connection.execute(statement)
        connection.execute("PRAGMA user_version = 3")

    def _migrate_v3_to_v4(self, connection: sqlite3.Connection) -> None:
        self._validate_recovery_budget_v4(
            connection,
            include_remote_payload_usage=False,
        )
        payload_count, maximum_bytes, payload_bytes = self._remote_state_recovery_usage(connection)
        self._validate_remote_state_recovery_usage(
            payload_count=payload_count,
            maximum_bytes=maximum_bytes,
            payload_bytes=payload_bytes,
        )
        timestamp = self._timestamp()
        connection.execute("ALTER TABLE schema_migrations RENAME TO schema_migrations_v3")
        connection.execute(_SCHEMA_V4[0])
        connection.execute(
            """
            INSERT INTO schema_migrations(version, applied_at)
            SELECT version, applied_at FROM schema_migrations_v3
            """
        )
        connection.execute(
            "INSERT INTO schema_migrations(version, applied_at) VALUES (4, ?)",
            (timestamp,),
        )
        connection.execute("DROP TABLE schema_migrations_v3")
        connection.execute(_REMOTE_PAYLOAD_USAGE_TABLE_V4)
        self._insert_remote_payload_usage_v4(
            connection,
            payload_count=payload_count,
            payload_bytes=payload_bytes,
        )
        connection.execute(_REMOTE_PAYLOAD_INSERT_TRIGGER_V4)
        connection.execute(_REMOTE_PAYLOAD_UPDATE_TRIGGER_V4)
        connection.execute(_REMOTE_PAYLOAD_DELETE_TRIGGER_V4)
        connection.execute("PRAGMA user_version = 4")

    def _migrate_v4_to_v5(self, connection: sqlite3.Connection) -> None:
        self._validate_remote_payload_usage_authority_v4(connection)
        self._validate_recovery_budget_v4(connection)
        self._reconcile_remote_payload_usage_v4(connection)

        for trigger_name in (
            "projects_remote_payload_insert",
            "projects_remote_payload_update",
            "projects_remote_payload_delete",
        ):
            connection.execute(f"DROP TRIGGER {trigger_name}")
        for index_name in (
            "projects_updated_idx",
            "projects_created_idx",
            "projects_name_idx",
            "projects_profile_idx",
            "projects_single_active_idx",
        ):
            connection.execute(f"DROP INDEX {index_name}")
        connection.execute("ALTER TABLE projects RENAME TO projects_v4")
        connection.execute(_PROJECTS_TABLE_V5)
        connection.execute(
            """
            INSERT INTO projects(
                project_id, profile_id, name, document_json, state,
                current_revision_id, remote_state_json,
                remote_state_token_0, remote_state_token_1,
                remote_state_token_2, remote_state_token_3,
                resource_version, created_at, updated_at
            )
            SELECT project_id, profile_id, name, document_json, state,
                   current_revision_id, remote_state_json,
                   CASE WHEN remote_state_json IS NULL THEN NULL ELSE 0 END,
                   CASE WHEN remote_state_json IS NULL THEN NULL ELSE 0 END,
                   CASE WHEN remote_state_json IS NULL THEN NULL ELSE 0 END,
                   CASE WHEN remote_state_json IS NULL THEN NULL ELSE 0 END,
                   resource_version, created_at, updated_at
            FROM projects_v4
            """
        )
        for row in connection.execute(
            """
            SELECT project_id, remote_state_json
            FROM projects_v4
            WHERE remote_state_json IS NOT NULL
            ORDER BY project_id
            """
        ):
            project_id = cast(str, row["project_id"])
            payload = bytes(row["remote_state_json"])
            token = self._remote_payload_content_token(project_id=project_id, payload=payload)
            updated = connection.execute(
                """
                UPDATE projects
                SET remote_state_token_0 = ?, remote_state_token_1 = ?,
                    remote_state_token_2 = ?, remote_state_token_3 = ?
                WHERE project_id = ?
                """,
                (*token, project_id),
            )
            if updated.rowcount != 1:
                raise ProviderDataCorruptionError(
                    "remote project state identity migration changed unexpectedly"
                )
        connection.execute("DROP TABLE projects_v4")
        for statement in _SCHEMA_V3:
            if " INDEX projects_" in statement:
                connection.execute(statement)

        connection.execute("DROP INDEX idempotency_expiry_idx")
        connection.execute("ALTER TABLE idempotency_records RENAME TO idempotency_records_v4")
        connection.execute(_IDEMPOTENCY_RECORDS_TABLE_V5)
        connection.execute(
            """
            INSERT INTO idempotency_records(
                principal, method, route, resource_scope, idempotency_key,
                request_digest, operation_id, response_type, status_code,
                response_bytes, cleanup_eligible, created_at_epoch, expires_at_epoch
            )
            SELECT principal, method, route, resource_scope, idempotency_key,
                   request_digest, operation_id, response_type, status_code,
                   response_bytes,
                   CASE
                       WHEN response_type != 'LocalOperationV1' THEN 1
                       WHEN EXISTS (
                           SELECT 1 FROM local_operations
                           WHERE local_operations.operation_id = idempotency_records_v4.operation_id
                             AND state NOT IN ('queued', 'running', 'cancelling')
                       ) THEN 1
                       ELSE 0
                   END,
                   created_at_epoch, expires_at_epoch
            FROM idempotency_records_v4
            """
        )
        connection.execute("DROP TABLE idempotency_records_v4")
        connection.execute(
            "CREATE INDEX idempotency_expiry_idx ON "
            "idempotency_records(cleanup_eligible, expires_at_epoch)"
        )

        connection.execute("ALTER TABLE schema_migrations RENAME TO schema_migrations_v4")
        connection.execute(_SCHEMA_V5[0])
        connection.execute(
            """
            INSERT INTO schema_migrations(version, applied_at)
            SELECT version, applied_at FROM schema_migrations_v4 ORDER BY version
            """
        )
        connection.execute("DROP TABLE schema_migrations_v4")
        connection.execute("DROP TABLE remote_payload_usage")
        connection.execute(_PROVIDER_STORAGE_USAGE_TABLE_V5)

        total_rows, total_bytes = self._recovery_usage(connection)
        remote_count, maximum_bytes, remote_bytes = self._remote_state_recovery_usage(connection)
        self._validate_remote_state_recovery_usage(
            payload_count=remote_count,
            maximum_bytes=maximum_bytes,
            payload_bytes=remote_bytes,
        )
        remote_accumulators = self._remote_content_accumulators(connection)
        profile_reservations, project_reservations = self._validate_live_action_authorities(
            connection
        )
        idempotency_record_count = self._table_record_count(connection, "idempotency_records")
        pagination_cursor_count = self._table_record_count(connection, "pagination_cursors")
        self._insert_provider_storage_usage(
            connection,
            total_rows=total_rows,
            total_bytes=total_bytes,
            remote_payload_count=remote_count,
            remote_payload_bytes=remote_bytes,
            remote_accumulators=remote_accumulators,
            profile_reservations=profile_reservations,
            project_reservations=project_reservations,
            idempotency_record_count=idempotency_record_count,
            pagination_cursor_count=pagination_cursor_count,
            generation=0,
        )
        for statement in (
            *_PROVIDER_USAGE_MAINTENANCE_TRIGGERS_V5,
            _PROVIDER_USAGE_INSERT_GUARD_V5,
            _PROVIDER_USAGE_DELETE_GUARD_V5,
            _SCHEMA_MIGRATION_INSERT_GUARD_V5,
            _SCHEMA_MIGRATION_UPDATE_GUARD_V5,
            _SCHEMA_MIGRATION_DELETE_GUARD_V5,
        ):
            connection.execute(statement)
        connection.execute(
            "INSERT INTO schema_migrations(version, applied_at) VALUES (5, ?)",
            (self._timestamp(),),
        )
        self._validate_unsealed_write_budget(connection)
        self._seal_provider_storage_usage(connection)
        connection.execute("PRAGMA user_version = 5")

    @staticmethod
    def _validate_schema(connection: sqlite3.Connection) -> None:
        DesktopProviderStore._validate_schema_version(
            connection,
            rows=_EXPECTED_SCHEMA_ROWS,
            digest=_EXPECTED_SCHEMA_DIGEST,
            label="v5",
        )

    @staticmethod
    def _validate_schema_version(
        connection: sqlite3.Connection,
        *,
        rows: tuple[tuple[object, ...], ...],
        digest: str,
        label: str,
    ) -> None:
        try:
            actual_rows = _schema_rows(connection)
        except sqlite3.DatabaseError as exc:
            raise ProviderSchemaError("provider schema could not be read") from exc
        actual_digest = sha256(_canonical_json_bytes(actual_rows)).hexdigest()
        if actual_digest != digest or actual_rows != rows:
            raise ProviderSchemaError(
                f"provider schema fingerprint does not match canonical {label}"
            )

    def _recover_and_validate(self) -> None:
        connection = self._connection
        try:
            connection.execute("BEGIN EXCLUSIVE")
            self._validate_schema(connection)
            usage_values, _ = self._validate_provider_storage_usage_authority(connection)
            self._validate_configured_record_capacities(usage_values)
            integrity = connection.execute("PRAGMA integrity_check(1)").fetchall()
            if [tuple(row) for row in integrity] != [("ok",)]:
                raise ProviderDataCorruptionError("provider SQLite integrity check failed")
            if connection.execute("PRAGMA foreign_key_check").fetchone() is not None:
                raise ProviderDataCorruptionError("provider foreign key check failed")
            self._validate_recovery_budget(connection)
            self._reconcile_provider_storage_usage(connection)
            self._validate_migration_rows(connection)
            for row in connection.execute("SELECT * FROM remote_profiles"):
                self._validate_profile_recovery_row(cast(sqlite3.Row, row))
            for identity_row in connection.execute(
                "SELECT project_id FROM projects ORDER BY project_id"
            ):
                project_id = cast(str, identity_row["project_id"])
                self._validate_project_recovery_row(
                    self._require_project_row(connection, project_id)
                )
            for row in connection.execute("SELECT * FROM local_operations"):
                self._validate_operation_recovery_row(cast(sqlite3.Row, row))
            for row in connection.execute("SELECT * FROM idempotency_records"):
                self._validate_idempotency_recovery_row(cast(sqlite3.Row, row))
            self._validate_write_budget(connection)
            for row in connection.execute("SELECT * FROM pagination_cursors"):
                self._validate_cursor_recovery_row(cast(sqlite3.Row, row))
            timestamp = self._timestamp()
            connection.execute(
                """
                UPDATE remote_profiles
                SET connection_state = 'disconnected',
                    host_key_fingerprint = CASE
                        WHEN connection_state IN ('connecting', 'host_key_required', 'failed')
                            THEN NULL
                        ELSE host_key_fingerprint
                    END,
                    resource_version = resource_version + 1, updated_at = ?
                WHERE connection_state != 'disconnected'
                """,
                (timestamp,),
            )
            connection.execute(
                """
                UPDATE projects
                SET state = CASE WHEN state = 'active' THEN 'draft' ELSE state END,
                    current_revision_id = NULL,
                    resource_version = resource_version + 1, updated_at = ?
                WHERE state = 'active' OR current_revision_id IS NOT NULL
                """,
                (timestamp,),
            )
            self._reconcile_operations_at_startup(connection)
            sealed_snapshot = self._seal_provider_storage_usage(connection)
            self._validate_write_budget(connection)
            self._verify_storage_files()
            connection.commit()
            self._provider_usage_snapshot = sealed_snapshot
        except ProviderStoreError:
            connection.rollback()
            raise
        except sqlite3.DatabaseError as exc:
            connection.rollback()
            raise ProviderDataCorruptionError("provider SQLite recovery failed") from exc
        except BaseException:
            connection.rollback()
            raise

    @staticmethod
    def _recovery_usage_v4(
        connection: sqlite3.Connection,
        *,
        include_remote_payload_usage: bool,
    ) -> tuple[int, int]:
        total_rows = 0
        total_bytes = 0
        specifications = list(_RECOVERY_USAGE_SPECIFICATIONS)
        if include_remote_payload_usage:
            specifications.append(
                ("remote_payload_usage", ("payload_count", "payload_bytes", "authority_tag"))
            )
        for table, columns in specifications:
            length_sum = _usage_length_sql(columns, prefix=table)
            row = connection.execute(
                f"SELECT count(*), coalesce(sum({length_sum}), 0) FROM {table}"
            ).fetchone()
            total_rows += cast(int, row[0])
            total_bytes += cast(int, row[1])
        return total_rows, total_bytes

    @staticmethod
    def _recovery_usage(connection: sqlite3.Connection) -> tuple[int, int]:
        total_rows = 1
        total_bytes = _PROVIDER_USAGE_ACCOUNTED_BYTES
        for table, columns in _RECOVERY_USAGE_SPECIFICATIONS:
            length_sum = _usage_length_sql(columns, prefix=table)
            row = connection.execute(
                f"SELECT count(*), coalesce(sum({length_sum}), 0) FROM {table}"
            ).fetchone()
            total_rows += cast(int, row[0])
            total_bytes += cast(int, row[1])
        return total_rows, total_bytes

    @staticmethod
    def _table_record_count(connection: sqlite3.Connection, table: str) -> int:
        if table not in {"idempotency_records", "pagination_cursors"}:
            raise ProviderStoreError("provider counter table is not allowlisted")
        count = connection.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
        if type(count) is not int or count < 0:
            raise ProviderDataCorruptionError("provider table count is invalid")
        return count

    @classmethod
    def _validate_recovery_budget_v4(
        cls,
        connection: sqlite3.Connection,
        *,
        include_remote_payload_usage: bool = True,
    ) -> None:
        total_rows, total_bytes = cls._recovery_usage_v4(
            connection,
            include_remote_payload_usage=include_remote_payload_usage,
        )
        if total_rows > MAX_RECOVERY_ROWS or total_bytes > MAX_RECOVERY_BYTES:
            raise ProviderDataCorruptionError("provider recovery budget exceeded")

    @classmethod
    def _validate_recovery_budget(cls, connection: sqlite3.Connection) -> None:
        total_rows, total_bytes = cls._recovery_usage(connection)
        if total_rows > MAX_RECOVERY_ROWS or total_bytes > MAX_RECOVERY_BYTES:
            raise ProviderDataCorruptionError("provider recovery budget exceeded")

    @staticmethod
    def _remote_state_recovery_usage(
        connection: sqlite3.Connection,
    ) -> tuple[int, int, int]:
        payload_count, maximum_bytes, total_bytes = connection.execute(
            """
            SELECT count(remote_state_json),
                   coalesce(max(length(CAST(remote_state_json AS BLOB))), 0),
                   coalesce(sum(length(CAST(remote_state_json AS BLOB))), 0)
            FROM projects
            WHERE remote_state_json IS NOT NULL
            """
        ).fetchone()
        if (
            type(payload_count) is not int
            or type(maximum_bytes) is not int
            or type(total_bytes) is not int
        ):
            raise ProviderDataCorruptionError("remote project state recovery usage is invalid")
        return payload_count, maximum_bytes, total_bytes

    @staticmethod
    def _validate_remote_state_recovery_usage(
        *,
        payload_count: int,
        maximum_bytes: int,
        payload_bytes: int,
    ) -> None:
        if (
            payload_count < 0
            or payload_count > MAX_RECOVERY_ROWS
            or maximum_bytes > MAX_REMOTE_PROJECT_STATE_BYTES
            or payload_bytes > MAX_REMOTE_PROJECT_STATE_RECOVERY_BYTES
            or (payload_count == 0) != (payload_bytes == 0)
            or payload_bytes < payload_count
        ):
            raise ProviderDataCorruptionError("remote project state recovery budget exceeded")

    def _remote_payload_usage_authority_tag(
        self,
        *,
        payload_count: int,
        payload_bytes: int,
    ) -> bytes:
        return hmac.digest(
            self._cursor_key,
            _REMOTE_PAYLOAD_USAGE_AUTHORITY_DOMAIN
            + struct.pack(">QQ", payload_count, payload_bytes),
            "sha256",
        )

    def _insert_remote_payload_usage_v4(
        self,
        connection: sqlite3.Connection,
        *,
        payload_count: int,
        payload_bytes: int,
    ) -> None:
        connection.execute(
            """
            INSERT INTO remote_payload_usage(
                singleton, payload_count, payload_bytes, authority_tag
            ) VALUES (1, ?, ?, ?)
            """,
            (
                payload_count,
                payload_bytes,
                self._remote_payload_usage_authority_tag(
                    payload_count=payload_count,
                    payload_bytes=payload_bytes,
                ),
            ),
        )

    @staticmethod
    def _remote_payload_usage_row_v4(connection: sqlite3.Connection) -> sqlite3.Row:
        row = connection.execute(
            """
            SELECT singleton, payload_count, payload_bytes, authority_tag
            FROM remote_payload_usage
            WHERE singleton = 1
            """
        ).fetchone()
        if row is None:
            raise ProviderDataCorruptionError("remote project state usage authority is missing")
        return cast(sqlite3.Row, row)

    @staticmethod
    def _validate_remote_payload_usage_scalars_v4(
        row: sqlite3.Row,
    ) -> tuple[int, int, bytes]:
        payload_count = row["payload_count"]
        payload_bytes = row["payload_bytes"]
        authority_tag = row["authority_tag"]
        if (
            row["singleton"] != 1
            or type(payload_count) is not int
            or type(payload_bytes) is not int
            or type(authority_tag) is not bytes
            or payload_count < 0
            or payload_count > MAX_RECOVERY_ROWS
            or payload_bytes < 0
            or payload_bytes > MAX_REMOTE_PROJECT_STATE_RECOVERY_BYTES
            or (payload_count == 0) != (payload_bytes == 0)
            or payload_bytes < payload_count
        ):
            raise ProviderDataCorruptionError("remote project state usage authority is invalid")
        return payload_count, payload_bytes, authority_tag

    def _validate_remote_payload_usage_authority_v4(
        self,
        connection: sqlite3.Connection,
    ) -> tuple[int, int]:
        payload_count, payload_bytes, authority_tag = (
            self._validate_remote_payload_usage_scalars_v4(
                self._remote_payload_usage_row_v4(connection)
            )
        )
        expected = self._remote_payload_usage_authority_tag(
            payload_count=payload_count,
            payload_bytes=payload_bytes,
        )
        if not hmac.compare_digest(authority_tag, expected):
            raise ProviderDataCorruptionError("remote project state usage authority is invalid")
        return payload_count, payload_bytes

    def _reconcile_remote_payload_usage_v4(self, connection: sqlite3.Connection) -> None:
        expected_count, expected_bytes = self._validate_remote_payload_usage_authority_v4(
            connection
        )
        payload_count, maximum_bytes, payload_bytes = self._remote_state_recovery_usage(connection)
        self._validate_remote_state_recovery_usage(
            payload_count=payload_count,
            maximum_bytes=maximum_bytes,
            payload_bytes=payload_bytes,
        )
        if payload_count != expected_count or payload_bytes != expected_bytes:
            raise ProviderDataCorruptionError(
                "remote project state usage authority differs from project rows"
            )

    def _remote_payload_content_token(
        self,
        *,
        project_id: str,
        payload: bytes,
    ) -> tuple[int, int, int, int]:
        project_bytes = project_id.encode("utf-8")
        digest = hmac.digest(
            self._cursor_key,
            _REMOTE_PAYLOAD_CONTENT_AUTHORITY_DOMAIN
            + struct.pack(">Q", len(project_bytes))
            + project_bytes
            + payload,
            "sha256",
        )
        return cast(
            tuple[int, int, int, int],
            tuple(
                int.from_bytes(
                    digest[
                        index * _REMOTE_CONTENT_TOKEN_BYTES : (index + 1)
                        * _REMOTE_CONTENT_TOKEN_BYTES
                    ],
                    "big",
                )
                for index in range(4)
            ),
        )

    @staticmethod
    def _remote_content_accumulators(
        connection: sqlite3.Connection,
    ) -> tuple[int, int, int, int]:
        accumulators = [0, 0, 0, 0]
        for row in connection.execute(
            """
            SELECT remote_state_token_0, remote_state_token_1,
                   remote_state_token_2, remote_state_token_3
            FROM projects
            WHERE remote_state_json IS NOT NULL
            ORDER BY project_id
            """
        ):
            for index in range(4):
                value = row[index]
                if type(value) is not int or not 0 <= value < _REMOTE_CONTENT_ACCUMULATOR_MODULUS:
                    raise ProviderDataCorruptionError(
                        "remote project state content authority is invalid"
                    )
                accumulators[index] = (
                    accumulators[index] + value
                ) % _REMOTE_CONTENT_ACCUMULATOR_MODULUS
        return cast(tuple[int, int, int, int], tuple(accumulators))

    def _provider_storage_usage_authority_tag(self, values: tuple[int, ...]) -> bytes:
        if len(values) != len(_PROVIDER_USAGE_COLUMNS) - 1:
            raise ProviderDataCorruptionError("provider storage usage authority is invalid")
        return hmac.digest(
            self._cursor_key,
            _PROVIDER_STORAGE_USAGE_AUTHORITY_DOMAIN + struct.pack(f">{len(values)}Q", *values),
            "sha256",
        )

    def _insert_provider_storage_usage(
        self,
        connection: sqlite3.Connection,
        *,
        total_rows: int,
        total_bytes: int,
        remote_payload_count: int,
        remote_payload_bytes: int,
        remote_accumulators: tuple[int, int, int, int],
        profile_reservations: int,
        project_reservations: int,
        idempotency_record_count: int,
        pagination_cursor_count: int,
        generation: int,
    ) -> None:
        values = (
            total_rows,
            total_bytes,
            remote_payload_count,
            remote_payload_bytes,
            *remote_accumulators,
            profile_reservations,
            project_reservations,
            idempotency_record_count,
            pagination_cursor_count,
            generation,
        )
        inserted = connection.execute(
            """
            INSERT INTO provider_storage_usage(
                singleton, total_rows, total_bytes,
                remote_payload_count, remote_payload_bytes,
                remote_accumulator_0, remote_accumulator_1,
                remote_accumulator_2, remote_accumulator_3,
                profile_reservations, project_reservations,
                idempotency_record_count, pagination_cursor_count,
                generation, authority_tag
            ) VALUES (1, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (*values, self._provider_storage_usage_authority_tag(values)),
        )
        if inserted.rowcount != 1:
            raise ProviderDataCorruptionError(
                "provider storage usage authority was not created exactly once"
            )

    @staticmethod
    def _provider_storage_usage_row(connection: sqlite3.Connection) -> sqlite3.Row:
        row = connection.execute(
            f"SELECT singleton, {', '.join(_PROVIDER_USAGE_COLUMNS)} "
            "FROM provider_storage_usage WHERE singleton = 1"
        ).fetchone()
        if row is None:
            raise ProviderDataCorruptionError("provider storage usage authority is missing")
        return cast(sqlite3.Row, row)

    @staticmethod
    def _provider_storage_usage_scalars(
        row: sqlite3.Row,
    ) -> tuple[tuple[int, ...], bytes]:
        values = tuple(row[column] for column in _PROVIDER_USAGE_COLUMNS[:-1])
        authority_tag = row["authority_tag"]
        if (
            row["singleton"] != 1
            or any(type(value) is not int for value in values)
            or type(authority_tag) is not bytes
            or len(authority_tag) not in {0, 32}
        ):
            raise ProviderDataCorruptionError("provider storage usage authority is invalid")
        typed_values = cast(tuple[int, ...], values)
        (
            total_rows,
            total_bytes,
            remote_count,
            remote_bytes,
            *remaining,
        ) = typed_values
        accumulators = remaining[:4]
        (
            profile_reservations,
            project_reservations,
            idempotency_record_count,
            pagination_cursor_count,
            generation,
        ) = remaining[4:]
        if (
            total_rows < 1
            or total_bytes < 0
            or remote_count < 0
            or remote_bytes < 0
            or (remote_count == 0) != (remote_bytes == 0)
            or remote_bytes < remote_count
            or any(not 0 <= value < _REMOTE_CONTENT_ACCUMULATOR_MODULUS for value in accumulators)
            or profile_reservations < 0
            or project_reservations < 0
            or idempotency_record_count < 0
            or pagination_cursor_count < 0
            or not 0 <= generation < 9223372036854775807
        ):
            raise ProviderDataCorruptionError("provider storage usage authority is invalid")
        return typed_values, authority_tag

    def _validate_provider_storage_usage_authority(
        self,
        connection: sqlite3.Connection,
    ) -> tuple[tuple[int, ...], bytes]:
        values, authority_tag = self._provider_storage_usage_scalars(
            self._provider_storage_usage_row(connection)
        )
        expected = self._provider_storage_usage_authority_tag(values)
        if not hmac.compare_digest(authority_tag, expected):
            raise ProviderDataCorruptionError("provider storage usage authority is invalid")
        return values, authority_tag

    def _validate_remote_payload_usage_authority(
        self,
        connection: sqlite3.Connection,
    ) -> tuple[int, int]:
        values, _ = self._validate_provider_storage_usage_authority(connection)
        return values[2], values[3]

    def _seal_provider_storage_usage(
        self,
        connection: sqlite3.Connection,
    ) -> tuple[tuple[int, ...], bytes]:
        row = self._provider_storage_usage_row(connection)
        values, authority_tag = self._provider_storage_usage_scalars(row)
        expected = self._provider_storage_usage_authority_tag(values)
        if hmac.compare_digest(authority_tag, expected):
            return values, authority_tag
        if authority_tag:
            raise ProviderDataCorruptionError(
                "provider storage usage authority changed unexpectedly"
            )
        updated = connection.execute(
            f"UPDATE provider_storage_usage SET authority_tag = ? "
            f"WHERE singleton = 1 AND {' AND '.join(f'{column} = ?' for column in _PROVIDER_USAGE_COLUMNS[:-1])} "
            "AND authority_tag = X''",
            (expected, *values),
        )
        if updated.rowcount != 1:
            raise ProviderDataCorruptionError(
                "provider storage usage authority changed during seal"
            )
        return values, expected

    def _reconcile_provider_storage_usage(self, connection: sqlite3.Connection) -> None:
        expected, _ = self._validate_provider_storage_usage_authority(connection)
        total_rows, total_bytes = self._recovery_usage(connection)
        payload_count, maximum_bytes, payload_bytes = self._remote_state_recovery_usage(connection)
        self._validate_remote_state_recovery_usage(
            payload_count=payload_count,
            maximum_bytes=maximum_bytes,
            payload_bytes=payload_bytes,
        )
        accumulators = self._remote_content_accumulators(connection)
        profile_reservations, project_reservations = self._validate_live_action_authorities(
            connection
        )
        idempotency_record_count = self._table_record_count(connection, "idempotency_records")
        pagination_cursor_count = self._table_record_count(connection, "pagination_cursors")
        actual = (
            total_rows,
            total_bytes,
            payload_count,
            payload_bytes,
            *accumulators,
            profile_reservations,
            project_reservations,
            idempotency_record_count,
            pagination_cursor_count,
        )
        if actual != expected[:-1]:
            raise ProviderDataCorruptionError(
                "provider storage usage authority differs from provider rows"
            )

    @staticmethod
    def _validate_write_budget_values(values: tuple[int, ...]) -> None:
        total_rows, total_bytes = values[:2]
        profile_reservations, project_reservations = values[8:10]
        reserved_bytes = (
            profile_reservations * PROFILE_RUNTIME_TERMINAL_RESERVATION_BYTES
            + project_reservations * PROJECT_RUNTIME_TERMINAL_RESERVATION_BYTES
        )
        if (
            total_rows > MAX_RECOVERY_ROWS
            or total_bytes > MAX_RECOVERY_BYTES
            or values[2] > MAX_RECOVERY_ROWS
            or values[3] > MAX_REMOTE_PROJECT_STATE_RECOVERY_BYTES
            or reserved_bytes > MAX_RECOVERY_BYTES - total_bytes
        ):
            raise ProviderDataCorruptionError("provider recovery budget exceeded")

    def _validate_unsealed_write_budget(self, connection: sqlite3.Connection) -> None:
        values, authority_tag = self._provider_storage_usage_scalars(
            self._provider_storage_usage_row(connection)
        )
        if authority_tag:
            raise ProviderDataCorruptionError(
                "provider storage usage authority was sealed before budget validation"
            )
        self._validate_write_budget_values(values)
        self._validate_configured_record_capacities(values)

    def _validate_write_budget(self, connection: sqlite3.Connection) -> None:
        values, _ = self._validate_provider_storage_usage_authority(connection)
        self._validate_write_budget_values(values)

    def _provider_record_counts(self, connection: sqlite3.Connection) -> tuple[int, int]:
        values, _ = self._provider_storage_usage_scalars(
            self._provider_storage_usage_row(connection)
        )
        return values[10], values[11]

    def _validate_configured_record_capacities(self, values: tuple[int, ...]) -> None:
        idempotency_record_count, pagination_cursor_count = values[10:12]
        if idempotency_record_count > self._max_idempotency_records:
            raise ProviderCapacityConfigurationError(
                "idempotency",
                configured_limit=self._max_idempotency_records,
                persisted_count=idempotency_record_count,
            )
        if pagination_cursor_count > self._max_cursor_records:
            raise ProviderCapacityConfigurationError(
                "cursor",
                configured_limit=self._max_cursor_records,
                persisted_count=pagination_cursor_count,
            )

    def _validate_live_action_authorities(self, connection: sqlite3.Connection) -> tuple[int, int]:
        invalid_digest = connection.execute(
            """
            SELECT operation_id
            FROM local_operations
            WHERE action_identity_digest IS NULL
               OR typeof(action_identity_digest) != 'text'
               OR length(action_identity_digest) != 64
               OR action_identity_digest GLOB '*[^0-9a-f]*'
            LIMIT 1
            """
        ).fetchone()
        if invalid_digest is not None:
            raise ProviderDataCorruptionError(
                "operation action authority digest is missing or invalid"
            )

        profile_reservations = 0
        project_reservations = 0
        after_operation_id = ""
        while True:
            operation_ids = connection.execute(
                """
                SELECT operation_id
                FROM local_operations
                WHERE state IN ('queued', 'running', 'cancelling')
                  AND operation_id > ?
                ORDER BY operation_id
                LIMIT ?
                """,
                (after_operation_id, STARTUP_OPERATION_BATCH_ROWS),
            ).fetchmany(STARTUP_OPERATION_BATCH_ROWS)
            if not operation_ids:
                return profile_reservations, project_reservations
            for operation_id_row in operation_ids:
                operation_id = cast(str, operation_id_row["operation_id"])
                row = self._require_operation_row(connection, operation_id)
                operation = self._operation_from_row(row)
                self._validate_operation_indexed_scalars(row, operation)
                self._idempotency_rows_for_operation(
                    connection,
                    operation,
                    bytes(row["document_json"]),
                )
                if operation.operation_kind in _PROFILE_OPERATION_KINDS:
                    reservation_label = "profile runtime"
                elif operation.operation_kind in _PROJECT_OPERATION_KINDS:
                    reservation_label = "project runtime"
                else:
                    raise ProviderDataCorruptionError(
                        "live operation kind has no fixed terminal reservation"
                    )
                try:
                    self._validate_nonterminal_operation_terminal_capacity(connection, operation)
                except (ContractValidationError, ResourceNotFoundError) as exc:
                    raise ProviderDataCorruptionError(
                        f"{reservation_label} cancellation exceeds its fixed terminal capacity"
                    ) from exc
                if operation.operation_kind in _PROFILE_OPERATION_KINDS:
                    profile_reservations += 1
                else:
                    project_reservations += 1
                after_operation_id = operation_id
            if len(operation_ids) < STARTUP_OPERATION_BATCH_ROWS:
                return profile_reservations, project_reservations

    def _validate_migration_rows(
        self,
        connection: sqlite3.Connection,
        *,
        expected_version: int = SCHEMA_VERSION,
    ) -> None:
        rows = connection.execute(
            "SELECT version, applied_at FROM schema_migrations ORDER BY version"
        ).fetchall()
        if [row["version"] for row in rows] != list(range(1, expected_version + 1)):
            raise ProviderSchemaError(
                f"provider migration ledger does not match schema v{expected_version}"
            )
        for row in rows:
            self._validate_persisted_timestamp(cast(str, row["applied_at"]))

    @staticmethod
    def _validate_persisted_timestamp(value: str) -> datetime:
        if type(value) is not str or _TIMESTAMP_RE.fullmatch(value) is None:
            raise ProviderDataCorruptionError("provider timestamp is not canonical UTC")
        try:
            return datetime.fromisoformat(f"{value[:-1]}+00:00")
        except ValueError as exc:
            raise ProviderDataCorruptionError("provider timestamp is invalid") from exc

    def _validate_common_resource_row(self, row: sqlite3.Row, id_column: str) -> None:
        self._validate_resource_id(cast(str, row[id_column]))
        if type(row["resource_version"]) is not int or row["resource_version"] < 1:
            raise ProviderDataCorruptionError("provider resource version is invalid")
        created_at = self._validate_persisted_timestamp(cast(str, row["created_at"]))
        updated_at = self._validate_persisted_timestamp(cast(str, row["updated_at"]))
        if updated_at < created_at:
            raise ProviderDataCorruptionError("provider resource timestamps are reversed")

    def _validate_profile_recovery_row(self, row: sqlite3.Row) -> None:
        self._validate_common_resource_row(row, "profile_id")
        document = _decode_json_object(bytes(row["document_json"]), label="profile")
        if document.get("name") != row["name"]:
            raise ProviderDataCorruptionError("profile name scalar differs from its document")
        slots_raw = bytes(row["credential_slots_json"])
        try:
            slots = json.loads(slots_raw)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ProviderDataCorruptionError("stored credential slots are invalid") from exc
        if _canonical_json_bytes(slots) != slots_raw:
            raise ProviderDataCorruptionError("stored credential slots are not canonical")
        self._profile_from_row(row)

    def _validate_project_recovery_row(self, row: sqlite3.Row) -> None:
        self._validate_common_resource_row(row, "project_id")
        document = _decode_json_object(bytes(row["document_json"]), label="project")
        if document.get("name") != row["name"] or document.get("profile_id") != row["profile_id"]:
            raise ProviderDataCorruptionError(
                "project indexed scalars differ from its canonical document"
            )
        self._project_from_row(row)
        try:
            self._validate_project_config_for_persistence(ProjectCreateV1.model_validate(document))
        except (ValidationError, ContractValidationError) as exc:
            raise ProviderDataCorruptionError(
                "stored project config contains a persistence-denied key"
            ) from exc

    @classmethod
    def _validate_activation_remote_state(
        cls,
        remote_state: RemoteProjectStateV1,
    ) -> None:
        if (
            remote_state.status != "ready"
            or remote_state.active_revision is None
            or remote_state.active_revision.project_id != remote_state.core_project_id
        ):
            raise ContractValidationError(
                "project activation requires a matching ready remote project state"
            )
        cls._validate_resource_id(remote_state.active_revision.id)

    @staticmethod
    def _encode_remote_project_state(remote_state: RemoteProjectStateV1) -> bytes:
        encoded = _canonical_json_bytes(remote_state.model_dump(mode="json"))
        if len(encoded) > MAX_REMOTE_PROJECT_STATE_BYTES:
            raise ContractValidationError("remote project state exceeds its byte bound")
        return encoded

    def _validate_operation_recovery_row(self, row: sqlite3.Row) -> None:
        operation = self._operation_from_row(row)
        action_identity_digest = row["action_identity_digest"]
        if (
            type(action_identity_digest) is not str
            or _DIGEST_RE.fullmatch(action_identity_digest) is None
        ):
            raise ProviderDataCorruptionError(
                "operation action authority digest is missing or invalid"
            )
        self._validate_operation_indexed_scalars(row, operation)

        try:
            self._validate_operation_authority(self._connection, operation, allow_historical=True)
        except (ContractValidationError, ResourceNotFoundError) as exc:
            raise ProviderDataCorruptionError(
                "stored operation differs from its authoritative resource"
            ) from exc

    @staticmethod
    def _validate_operation_indexed_scalars(row: sqlite3.Row, operation: LocalOperationV1) -> None:
        if (
            operation.operation_kind != row["operation_kind"]
            or operation.state != row["state"]
            or operation.resource.resource_type != row["resource_type"]
            or operation.resource.resource_id != row["resource_id"]
            or operation.created_at != row["created_at"]
            or operation.finished_at != row["finished_at"]
        ):
            raise ProviderDataCorruptionError(
                "operation indexed scalars differ from its canonical document"
            )

    def _validate_cursor_recovery_row(self, row: sqlite3.Row) -> None:
        if (
            _DIGEST_RE.fullmatch(cast(str, row["cursor_digest"])) is None
            or _DIGEST_RE.fullmatch(cast(str, row["query_digest"])) is None
        ):
            raise ProviderDataCorruptionError("provider cursor digest is invalid")
        try:
            self._validate_resource_id(cast(str, row["anchor_id"]))
        except ContractValidationError as exc:
            raise ProviderDataCorruptionError("provider cursor anchor is invalid") from exc
        if len(cast(str, row["anchor_value"]).encode("utf-8")) > 4096:
            raise ProviderDataCorruptionError("provider cursor value exceeds its byte bound")
        if (
            type(row["created_at_epoch"]) is not int
            or type(row["expires_at_epoch"]) is not int
            or row["expires_at_epoch"] <= row["created_at_epoch"]
        ):
            raise ProviderDataCorruptionError("provider cursor timestamps are invalid")

    def _validate_idempotency_recovery_row(self, row: sqlite3.Row) -> None:
        if row["principal"] != LOCAL_PRINCIPAL:
            raise ProviderDataCorruptionError("idempotency principal is not local")
        for label, column, maximum, minimum in (
            ("method", "method", MAX_IDENTITY_BYTES, 1),
            ("route", "route", MAX_IDENTITY_BYTES, 1),
            ("resource scope", "resource_scope", MAX_IDENTITY_BYTES, 1),
            (
                "idempotency key",
                "idempotency_key",
                MAX_IDEMPOTENCY_KEY_BYTES,
                16,
            ),
        ):
            try:
                self._bounded_identity(label, cast(str, row[column]), maximum, minimum=minimum)
            except ContractValidationError as exc:
                raise ProviderDataCorruptionError("idempotency identity is invalid") from exc
        if _DIGEST_RE.fullmatch(cast(str, row["request_digest"])) is None:
            raise ProviderDataCorruptionError("idempotency request digest is invalid")
        operation_id = row["operation_id"]
        if operation_id is not None:
            try:
                self._validate_resource_id(cast(str, operation_id))
            except ContractValidationError as exc:
                raise ProviderDataCorruptionError(
                    "idempotency operation identity is invalid"
                ) from exc
        if (
            type(row["created_at_epoch"]) is not int
            or type(row["expires_at_epoch"]) is not int
            or row["expires_at_epoch"] <= row["created_at_epoch"]
        ):
            raise ProviderDataCorruptionError("idempotency retention timestamps are invalid")
        response_model = self._response_model_for_name(cast(str, row["response_type"]))
        response_bytes = bytes(row["response_bytes"])
        response = self._model_from_response(response_model, response_bytes)
        if _canonical_json_bytes(response.model_dump(mode="json")) != response_bytes:
            raise ProviderDataCorruptionError("idempotency response is not canonical")
        if isinstance(response, LocalOperationV1):
            operation = self._operation_for_idempotency_record(self._connection, row)
            expected_cleanup_eligible = int(
                operation.state not in {"queued", "running", "cancelling"}
            )
        else:
            expected_cleanup_eligible = 1
        if row["cleanup_eligible"] != expected_cleanup_eligible:
            raise ProviderDataCorruptionError(
                "idempotency cleanup eligibility differs from its response"
            )

    @contextmanager
    def _transaction(self, *, write: bool) -> Iterator[sqlite3.Connection]:
        with self._transaction_lock:
            self._verify_storage_files()
            connection = self._connection
            try:
                connection.execute("BEGIN IMMEDIATE" if write else "BEGIN")
                self._validate_schema(connection)
                initial_snapshot = self._validate_provider_storage_usage_authority(connection)
                if initial_snapshot != self._provider_usage_snapshot:
                    raise ProviderDataCorruptionError(
                        "provider storage usage authority was replayed during this process"
                    )
                yield connection
                final_snapshot = initial_snapshot
                if write:
                    final_snapshot = self._seal_provider_storage_usage(connection)
                    self._validate_write_budget(connection)
                    self._validate_schema(connection)
                    self._verify_storage_files()
                connection.commit()
                self._provider_usage_snapshot = final_snapshot
            except BaseException:
                connection.rollback()
                raise

    def _now(self) -> datetime:
        value = self._clock()
        if not isinstance(value, datetime) or value.tzinfo is None:
            raise ProviderStoreError("provider clock must return a timezone-aware datetime")
        return value.astimezone(timezone.utc)

    def _timestamp(self) -> str:
        return self._now().isoformat(timespec="microseconds").replace("+00:00", "Z")

    @staticmethod
    def _new_id() -> str:
        return secrets.token_urlsafe(_RESOURCE_ID_BYTES)

    @staticmethod
    def _etag(resource_type: str, resource_id: str, version: int) -> str:
        digest = sha256(
            _canonical_json_bytes(
                {"resource_id": resource_id, "resource_type": resource_type, "version": version}
            )
        ).hexdigest()
        return f'"{digest}"'

    @staticmethod
    def _validate_if_match(if_match: str) -> None:
        if type(if_match) is not str or _ETAG_RE.fullmatch(if_match) is None:
            raise ContractValidationError("If-Match is not a Desktop v1 ETag")

    def create_profile(
        self,
        request: RemoteProfileCreateV1 | Mapping[str, object],
        *,
        idempotency_key: str,
    ) -> RemoteProfileV1:
        validated = _validate_model(RemoteProfileCreateV1, request)

        def mutation(transaction: ProviderMutation) -> tuple[int, BaseModel]:
            return 201, transaction._create_profile(validated)

        result = self._execute_idempotent(
            method="POST",
            route="/desktop/v1/profiles",
            resource_scope="profiles",
            key=idempotency_key,
            request_value=validated.model_dump(mode="json"),
            response_model=RemoteProfileV1,
            bound_if_match=None,
            mutation=mutation,
        )
        return self._model_from_response(RemoteProfileV1, result.response_bytes)

    def get_profile(self, profile_id: str) -> RemoteProfileV1:
        self._validate_resource_id(profile_id)
        with self._transaction(write=False) as connection:
            return self._profile_from_row(self._require_profile_row(connection, profile_id))

    def patch_profile(
        self,
        profile_id: str,
        patch: RemoteProfilePatchV1 | Mapping[str, object],
        *,
        if_match: str,
    ) -> RemoteProfileV1:
        self._validate_resource_id(profile_id)
        validated_patch = _validate_model(RemoteProfilePatchV1, patch)
        self._validate_if_match(if_match)
        with self._transaction(write=True) as connection:
            row = self._require_profile_row(connection, profile_id)
            self._require_etag("profile", profile_id, row, if_match)
            protected_connection_fields = {
                "host",
                "port",
                "user",
                "authentication_kind",
                "proxy",
            }
            if row["connection_state"] != "disconnected" and (
                validated_patch.model_fields_set & protected_connection_fields
            ):
                raise ResourceInUseError("profile", profile_id)
            current = _decode_json_object(bytes(row["document_json"]), label="profile")
            current.update(validated_patch.model_dump(mode="json", exclude_unset=True))
            validated = _validate_json_model(RemoteProfileCreateV1, current)
            version = cast(int, row["resource_version"]) + 1
            timestamp = self._timestamp()
            connection.execute(
                """
                UPDATE remote_profiles
                SET name = ?, document_json = ?, resource_version = ?, updated_at = ?
                WHERE profile_id = ?
                """,
                (
                    validated.name,
                    _canonical_json_bytes(validated.model_dump(mode="json")),
                    version,
                    timestamp,
                    profile_id,
                ),
            )
            return self._profile_from_row(self._require_profile_row(connection, profile_id))

    def delete_profile(self, profile_id: str, *, if_match: str) -> None:
        self._validate_resource_id(profile_id)
        self._validate_if_match(if_match)
        with self._transaction(write=True) as connection:
            row = self._require_profile_row(connection, profile_id)
            self._require_etag("profile", profile_id, row, if_match)
            referenced = connection.execute(
                "SELECT 1 FROM projects WHERE profile_id = ? LIMIT 1", (profile_id,)
            ).fetchone()
            operation_in_flight = connection.execute(
                """
                SELECT 1
                FROM local_operations
                WHERE resource_type = 'profile' AND resource_id = ?
                  AND state IN ('queued', 'running', 'cancelling')
                LIMIT 1
                """,
                (profile_id,),
            ).fetchone()
            if (
                row["connection_state"] != "disconnected"
                or referenced is not None
                or operation_in_flight is not None
            ):
                raise ResourceInUseError("profile", profile_id)
            connection.execute("DELETE FROM remote_profiles WHERE profile_id = ?", (profile_id,))

    def list_profiles(
        self,
        *,
        limit: int = 50,
        after: str | None = None,
        sort: str = "updated_at",
        direction: _Direction = "desc",
        filters: Mapping[str, str] | None = None,
    ) -> RemoteProfilePageV1:
        rows, next_cursor = self._list_rows(
            resource="profiles",
            table="remote_profiles",
            id_column="profile_id",
            limit=limit,
            after=after,
            sort=sort,
            direction=direction,
            filters=filters,
            filter_columns={"connection_state": "connection_state"},
        )
        return RemoteProfilePageV1(
            items=tuple(self._profile_from_row(row) for row in rows),
            next_cursor=next_cursor,
            has_more=next_cursor is not None,
        )

    def create_project(
        self,
        request: ProjectCreateV1 | Mapping[str, object],
        *,
        idempotency_key: str,
    ) -> ProjectV1:
        validated = _validate_model(ProjectCreateV1, request)
        self._validate_project_config_for_persistence(validated)

        def mutation(transaction: ProviderMutation) -> tuple[int, BaseModel]:
            return 201, transaction._create_project(validated)

        result = self._execute_idempotent(
            method="POST",
            route="/desktop/v1/projects",
            resource_scope="projects",
            key=idempotency_key,
            request_value=validated.model_dump(mode="json"),
            response_model=ProjectV1,
            bound_if_match=None,
            mutation=mutation,
        )
        return self._model_from_response(ProjectV1, result.response_bytes)

    def get_project(self, project_id: str) -> ProjectV1:
        self._validate_resource_id(project_id)
        with self._transaction(write=False) as connection:
            return self._project_from_row(self._require_project_row(connection, project_id))

    def native_workspace_sources(self) -> tuple[tuple[str, ProjectSourceV1], ...]:
        """Return the bounded persisted source set used for private-store recovery."""

        with self._transaction(write=False) as connection:
            identity_rows = connection.execute(
                "SELECT project_id FROM projects ORDER BY project_id ASC LIMIT ?",
                (MAX_RECOVERY_ROWS + 1,),
            ).fetchall()
            if len(identity_rows) > MAX_RECOVERY_ROWS:
                raise ProviderDataCorruptionError(
                    "project recovery row count exceeds the startup limit"
                )
            sources = []
            for identity_row in identity_rows:
                project = self._project_from_row(
                    self._require_project_row(connection, cast(str, identity_row["project_id"]))
                )
                if project.source.kind == "native_folder_snapshot":
                    sources.append((project.project_id, project.source))
            return tuple(sources)

    def get_local_operation(self, operation_id: str) -> LocalOperationV1:
        self._validate_resource_id(operation_id)
        with self._transaction(write=False) as connection:
            return self._operation_from_row(self._require_operation_row(connection, operation_id))

    def pending_operation_ids(self) -> tuple[str, ...]:
        """Return the bounded, stable set of operations that may still make progress."""

        with self._transaction(write=False) as connection:
            rows = connection.execute(
                """
                SELECT operation_id
                FROM local_operations
                WHERE state IN ('queued', 'running', 'cancelling')
                ORDER BY operation_id ASC
                LIMIT ?
                """,
                (MAX_RECOVERY_ROWS + 1,),
            ).fetchall()
            if len(rows) > MAX_RECOVERY_ROWS:
                raise ProviderDataCorruptionError(
                    "pending operation row count exceeds the recovery limit"
                )
            operation_ids = tuple(cast(str, row["operation_id"]) for row in rows)
            try:
                for operation_id in operation_ids:
                    self._validate_resource_id(operation_id)
            except ContractValidationError as exc:
                raise ProviderDataCorruptionError("pending operation identity is invalid") from exc
            return operation_ids

    def patch_project(
        self,
        project_id: str,
        patch: ProjectPatchV1 | Mapping[str, object],
        *,
        if_match: str,
    ) -> ProjectV1:
        self._validate_resource_id(project_id)
        validated_patch = _validate_model(ProjectPatchV1, patch)
        self._validate_if_match(if_match)
        with self._transaction(write=True) as connection:
            row = self._require_project_row(connection, project_id)
            self._require_etag("project", project_id, row, if_match)
            self._require_project_not_busy(connection, project_id)
            if row["state"] == "active":
                self._require_active_project_activation_authority_available(connection, project_id)
            current = _decode_json_object(bytes(row["document_json"]), label="project")
            current.update(validated_patch.model_dump(mode="json", exclude_unset=True))
            validated = _validate_json_model(ProjectCreateV1, current)
            self._validate_project_config_for_persistence(validated)
            self._require_profile_row(connection, validated.profile_id)
            version = cast(int, row["resource_version"]) + 1
            timestamp = self._timestamp()
            connection.execute(
                """
                UPDATE projects
                SET profile_id = ?, name = ?, document_json = ?,
                    state = CASE
                        WHEN state IN ('active', 'blocked') THEN 'draft'
                        ELSE state
                    END,
                    current_revision_id = NULL, remote_state_json = NULL,
                    remote_state_token_0 = NULL, remote_state_token_1 = NULL,
                    remote_state_token_2 = NULL, remote_state_token_3 = NULL,
                    resource_version = ?, updated_at = ?
                WHERE project_id = ?
                """,
                (
                    validated.profile_id,
                    validated.name,
                    _canonical_json_bytes(validated.model_dump(mode="json")),
                    version,
                    timestamp,
                    project_id,
                ),
            )
            return self._project_from_row(self._require_project_row(connection, project_id))

    def delete_project(self, project_id: str, *, if_match: str) -> None:
        self._validate_resource_id(project_id)
        self._validate_if_match(if_match)
        with self._transaction(write=True) as connection:
            row = self._require_project_row(connection, project_id)
            self._require_etag("project", project_id, row, if_match)
            self._require_project_not_busy(connection, project_id)
            if row["state"] == "active" or row["current_revision_id"] is not None:
                raise ResourceInUseError("project", project_id)
            connection.execute("DELETE FROM projects WHERE project_id = ?", (project_id,))

    def list_projects(
        self,
        *,
        limit: int = 50,
        after: str | None = None,
        sort: str = "updated_at",
        direction: _Direction = "desc",
        filters: Mapping[str, str] | None = None,
    ) -> ProjectPageV1:
        rows, next_cursor = self._list_rows(
            resource="projects",
            table="projects",
            id_column="project_id",
            limit=limit,
            after=after,
            sort=sort,
            direction=direction,
            filters=filters,
            filter_columns={"state": "state", "profile_id": "profile_id"},
        )
        return ProjectPageV1(
            items=tuple(self._project_from_row(row) for row in rows),
            next_cursor=next_cursor,
            has_more=next_cursor is not None,
        )

    def execute_idempotent_action(
        self,
        *,
        route: str,
        resource_scope: str,
        key: str,
        body: BaseModel | Mapping[str, object],
        if_match: str,
        semantic_headers: Mapping[str, str],
        response_model: type[_ModelT],
        mutation: Callable[[ProviderMutation], tuple[int, BaseModel]],
    ) -> IdempotencyResult:
        self._validate_if_match(if_match)
        headers = self._normalize_semantic_headers(semantic_headers)
        headers["if-match"] = if_match
        body_value = body.model_dump(mode="json") if isinstance(body, BaseModel) else dict(body)
        return self._execute_idempotent(
            method="POST",
            route=route,
            resource_scope=resource_scope,
            key=key,
            request_value={"body": body_value, "headers": headers},
            response_model=response_model,
            bound_if_match=if_match,
            mutation=mutation,
        )

    def begin_profile_runtime_action(
        self,
        *,
        route: str,
        operation_kind: str,
        profile_id: str,
        key: str,
        body: BaseModel | Mapping[str, object],
        if_match: str,
        displace_existing: bool,
    ) -> ProfileRuntimeActionReservation:
        """Atomically reserve idempotency capacity and persisted runtime ownership."""

        if type(displace_existing) is not bool:
            raise ContractValidationError("displace_existing must be a boolean")
        if operation_kind not in _PROFILE_OPERATION_KINDS:
            raise ContractValidationError("profile runtime operation kind is invalid")
        identity = self._profile_action_identity(
            route=route,
            operation_kind=operation_kind,
            profile_id=profile_id,
            key=key,
            body=body,
            if_match=if_match,
        )
        with self._transaction(write=True) as connection:
            now_epoch = identity[5]
            self._cleanup_expired_idempotency_records(connection, now_epoch)
            existing = self._profile_action_replay(connection, identity, discard_expired=True)
            if existing is not None:
                return ProfileRuntimeActionReservation(existing, None, True)
            self._validate_action_route_binding(
                route=identity[1],
                resource_scope=identity[2],
                resource_type="profile",
                operation_kind=operation_kind,
            )
            count, _ = self._provider_record_counts(connection)
            if count >= self._max_idempotency_records:
                raise IdempotencyCapacityError("live idempotency record capacity is exhausted")

            transaction = ProviderMutation(self, connection, if_match=if_match)
            profile = transaction.require_profile_authority(profile_id, if_match=if_match)
            transaction.cancel_nonterminal_profile_operations(profile_id)
            if operation_kind != "profile_disconnect":
                if displace_existing:
                    transaction.disconnect_other_profiles(profile_id)
                transaction.set_profile_runtime_state(
                    profile_id,
                    if_match=if_match,
                    connection_state="connecting",
                    credential_slots=profile.credential_slots,
                    host_key_fingerprint=profile.host_key_fingerprint,
                )
            operation = transaction.create_local_operation(
                operation_kind=operation_kind,
                resource=ResourceRefV1(resource_type="profile", resource_id=profile_id),
                state="running",
            )
            response_bytes = _canonical_json_bytes(operation.model_dump(mode="json"))
            self._insert_idempotency_record(
                connection,
                principal=LOCAL_PRINCIPAL,
                method=identity[0],
                route=identity[1],
                resource_scope=identity[2],
                key=identity[3],
                request_digest=identity[4],
                operation_id=operation.operation_id,
                response_type="LocalOperationV1",
                status_code=202,
                response_bytes=response_bytes,
                now_epoch=now_epoch,
            )
            transaction._validate_created_operations_bound()
            return ProfileRuntimeActionReservation(operation, profile, False)

    def complete_profile_runtime_action(
        self,
        *,
        reservation: ProfileRuntimeActionReservation,
        route: str,
        profile_id: str,
        key: str,
        body: BaseModel | Mapping[str, object],
        if_match: str,
        connection_state: Literal["connected", "disconnected", "host_key_required"],
        host_key_fingerprint: str | None,
    ) -> LocalOperationV1:
        identity = self._profile_action_identity(
            route=route,
            operation_kind=reservation.operation.operation_kind,
            profile_id=profile_id,
            key=key,
            body=body,
            if_match=if_match,
        )
        with self._transaction(write=True) as connection:
            operation, row = self._require_reserved_profile_action(
                connection, identity, reservation.operation.operation_id
            )
            if operation.state in {"succeeded", "failed", "cancelled"}:
                return operation
            if operation.state != "running":
                raise ProviderStoreError("profile runtime action is no longer completable")
            current = self._profile_from_row(self._require_profile_row(connection, profile_id))
            updated = ProviderMutation(self, connection).set_profile_runtime_state(
                profile_id,
                if_match=current.etag,
                connection_state=connection_state,
                credential_slots=current.credential_slots,
                host_key_fingerprint=host_key_fingerprint,
            )
            result = ConnectionOperationResultV1(
                profile_id=profile_id,
                connection_state=cast(
                    Literal["connected", "disconnected", "host_key_required"],
                    updated.connection_state,
                ),
            )
            return self._finish_reserved_profile_action(
                connection,
                identity,
                row,
                operation,
                state="succeeded",
                status_code=202,
                result=result,
                error=None,
            )

    def fail_profile_runtime_action(
        self,
        *,
        reservation: ProfileRuntimeActionReservation,
        route: str,
        profile_id: str,
        key: str,
        body: BaseModel | Mapping[str, object],
        if_match: str,
        error: ApiErrorV1,
    ) -> LocalOperationV1:
        """Persist a replayable failed operation and converge its owned profile state."""

        identity = self._profile_action_identity(
            route=route,
            operation_kind=reservation.operation.operation_kind,
            profile_id=profile_id,
            key=key,
            body=body,
            if_match=if_match,
        )
        with self._transaction(write=True) as connection:
            operation, row = self._require_reserved_profile_action(
                connection, identity, reservation.operation.operation_id
            )
            if operation.state in {"succeeded", "failed", "cancelled"}:
                return operation
            if operation.state != "running":
                raise ProviderStoreError("profile runtime action is no longer failable")
            current = self._profile_from_row(self._require_profile_row(connection, profile_id))
            ProviderMutation(self, connection).set_profile_runtime_state(
                profile_id,
                if_match=current.etag,
                connection_state="disconnected",
                credential_slots=current.credential_slots,
                host_key_fingerprint=current.host_key_fingerprint,
            )
            return self._finish_reserved_profile_action(
                connection,
                identity,
                row,
                operation,
                state="failed",
                status_code=error.http_status,
                result=None,
                error=error,
            )

    def observe_profile_runtime_action(
        self,
        *,
        reservation: ProfileRuntimeActionReservation,
        route: str,
        profile_id: str,
        key: str,
        body: BaseModel | Mapping[str, object],
        if_match: str,
    ) -> LocalOperationV1:
        """Read an exact reserved action without changing nonterminal state."""

        identity = self._profile_action_identity(
            route=route,
            operation_kind=reservation.operation.operation_kind,
            profile_id=profile_id,
            key=key,
            body=body,
            if_match=if_match,
        )
        with self._transaction(write=False) as connection:
            operation, _ = self._require_reserved_profile_action(
                connection, identity, reservation.operation.operation_id
            )
            return operation

    def _profile_action_identity(
        self,
        *,
        route: str,
        operation_kind: str,
        profile_id: str,
        key: str,
        body: BaseModel | Mapping[str, object],
        if_match: str,
    ) -> tuple[str, str, str, str, str, int]:
        if operation_kind not in _PROFILE_OPERATION_KINDS:
            raise ContractValidationError("profile runtime operation kind is invalid")
        self._validate_if_match(if_match)
        method = "POST"
        route = self._bounded_identity("route", route, MAX_IDENTITY_BYTES)
        profile_id = self._bounded_identity("resource_scope", profile_id, MAX_IDENTITY_BYTES)
        key = self._bounded_identity("idempotency key", key, MAX_IDEMPOTENCY_KEY_BYTES, minimum=16)
        body_value = body.model_dump(mode="json") if isinstance(body, BaseModel) else dict(body)
        request_value = {
            "operation_kind": operation_kind,
            "body": body_value,
            "headers": {"if-match": if_match},
        }
        self._reject_credential_bearing_keys(request_value)
        request_bytes = _canonical_json_bytes(request_value)
        if len(request_bytes) > MAX_REQUEST_BYTES:
            raise ContractValidationError("canonical idempotency request exceeds the byte limit")
        return (
            method,
            route,
            profile_id,
            key,
            sha256(request_bytes).hexdigest(),
            int(self._now().timestamp()),
        )

    def _profile_action_replay(
        self,
        connection: sqlite3.Connection,
        identity: tuple[str, str, str, str, str, int],
        *,
        discard_expired: bool = False,
    ) -> LocalOperationV1 | None:
        row = connection.execute(
            """
            SELECT *
            FROM idempotency_records
            WHERE principal = ? AND method = ? AND route = ?
              AND resource_scope = ? AND idempotency_key = ?
            """,
            (LOCAL_PRINCIPAL, *identity[:4]),
        ).fetchone()
        if row is None:
            return None
        row = cast(sqlite3.Row, row)
        if discard_expired and self._discard_expired_idempotency_record(
            connection, row, now_epoch=identity[5]
        ):
            return None
        if row["request_digest"] != identity[4]:
            raise IdempotencyConflictError(
                "idempotency key is already bound to a different request"
            )
        return self._operation_for_idempotency_record(connection, row)

    def _require_reserved_profile_action(
        self,
        connection: sqlite3.Connection,
        identity: tuple[str, str, str, str, str, int],
        operation_id: str,
    ) -> tuple[LocalOperationV1, sqlite3.Row]:
        replay = self._profile_action_replay(connection, identity)
        if replay is None or replay.operation_id != operation_id:
            raise ProviderStoreError("profile runtime action reservation is unavailable")
        row = self._require_operation_row(connection, operation_id)
        return self._operation_from_row(row), row

    def _finish_reserved_profile_action(
        self,
        connection: sqlite3.Connection,
        identity: tuple[str, str, str, str, str, int],
        row: sqlite3.Row,
        operation: LocalOperationV1,
        *,
        state: Literal["succeeded", "failed"],
        status_code: int,
        result: ConnectionOperationResultV1 | None,
        error: ApiErrorV1 | None,
    ) -> LocalOperationV1:
        version = cast(int, row["resource_version"]) + 1
        timestamp = self._timestamp()
        finished = _validate_model(
            LocalOperationV1,
            {
                **operation.model_dump(mode="python"),
                "state": state,
                "result": result,
                "error": error,
                "finished_at": timestamp,
                "etag": self._etag("operation", operation.operation_id, version),
            },
        )
        self._validate_operation_authority(connection, finished)
        response_bytes = _canonical_json_bytes(finished.model_dump(mode="json"))
        if len(response_bytes) > PROFILE_RUNTIME_TERMINAL_SLOT_BYTES:
            raise ProviderStoreError("profile runtime terminal response exceeds its reserved slot")
        connection.execute(
            """
            UPDATE local_operations
            SET state = ?, document_json = ?, resource_version = ?, finished_at = ?
            WHERE operation_id = ?
            """,
            (state, response_bytes, version, timestamp, operation.operation_id),
        )
        updated = connection.execute(
            """
            UPDATE idempotency_records
            SET status_code = ?, response_bytes = ?, cleanup_eligible = 1
            WHERE principal = ? AND method = ? AND route = ?
              AND resource_scope = ? AND idempotency_key = ? AND request_digest = ?
              AND operation_id = ?
            """,
            (
                status_code,
                response_bytes,
                LOCAL_PRINCIPAL,
                *identity[:5],
                operation.operation_id,
            ),
        )
        if updated.rowcount != 1:
            raise ProviderStoreError("profile action idempotency reservation changed")
        return finished

    def begin_project_runtime_action(
        self,
        *,
        route: str,
        operation_kind: str,
        project_id: str,
        key: str,
        body: BaseModel | Mapping[str, object],
        if_match: str,
        admission_guard: Callable[[ProjectV1], None] | None = None,
    ) -> ProjectRuntimeActionReservation:
        """Atomically admit and reserve one durable background action for a project.

        The guard may only inspect the validated project and raise. Existing
        idempotency identities invoke it before exact or conflicting replay
        resolution; new actions invoke it after ETag validation.
        """

        if operation_kind not in _PROJECT_OPERATION_KINDS:
            raise ContractValidationError("project runtime operation kind is invalid")
        identity = self._project_action_identity(
            route=route,
            operation_kind=operation_kind,
            project_id=project_id,
            key=key,
            body=body,
            if_match=if_match,
        )
        with self._transaction(write=True) as connection:
            now_epoch = identity[5]
            self._cleanup_expired_idempotency_records(connection, now_epoch)
            existing = self._project_action_replay(
                connection,
                identity,
                discard_expired=True,
                admission_guard=admission_guard,
            )
            if existing is not None:
                return ProjectRuntimeActionReservation(existing, None, True)
            self._validate_action_route_binding(
                route=identity[1],
                resource_scope=identity[2],
                resource_type="project",
                operation_kind=operation_kind,
            )
            count, _ = self._provider_record_counts(connection)
            if count >= self._max_idempotency_records:
                raise IdempotencyCapacityError("live idempotency record capacity is exhausted")

            transaction = ProviderMutation(self, connection, if_match=if_match)
            project = transaction.require_project_authority(project_id, if_match=if_match)
            if admission_guard is not None:
                admission_guard(project)
            self._require_project_not_busy(connection, project_id)
            result: LocalOperationResultV1 | None = None
            if operation_kind == "project_activate":
                result = ProjectOperationResultV1(
                    project_id=project.project_id,
                    project_etag=project.etag,
                    active=project.state == "active",
                )
            operation = transaction.create_local_operation(
                operation_kind=operation_kind,
                resource=ResourceRefV1(resource_type="project", resource_id=project_id),
                state="queued",
                result=result,
            )
            response_bytes = _canonical_json_bytes(operation.model_dump(mode="json"))
            self._insert_idempotency_record(
                connection,
                principal=LOCAL_PRINCIPAL,
                method=identity[0],
                route=identity[1],
                resource_scope=identity[2],
                key=identity[3],
                request_digest=identity[4],
                operation_id=operation.operation_id,
                response_type="LocalOperationV1",
                status_code=202,
                response_bytes=response_bytes,
                now_epoch=now_epoch,
            )
            transaction._validate_created_operations_bound()
            return ProjectRuntimeActionReservation(operation, project, False)

    def start_project_runtime_action(
        self,
        *,
        reservation: ProjectRuntimeActionReservation,
        route: str,
        operation_kind: str,
        project_id: str,
        key: str,
        body: BaseModel | Mapping[str, object],
        if_match: str,
    ) -> LocalOperationV1:
        identity = self._project_action_identity(
            route=route,
            operation_kind=operation_kind,
            project_id=project_id,
            key=key,
            body=body,
            if_match=if_match,
        )
        with self._transaction(write=True) as connection:
            operation, row = self._require_reserved_project_action(
                connection, identity, reservation.operation.operation_id
            )
            self._require_project_operation_kind(reservation, operation_kind)
            if operation.state in {"succeeded", "failed", "cancelled", "running"}:
                return operation
            if operation.state != "queued":
                raise ProviderStoreError("project runtime action is no longer startable")
            version = cast(int, row["resource_version"]) + 1
            timestamp = self._timestamp()
            started = _validate_model(
                LocalOperationV1,
                {
                    **operation.model_dump(mode="python"),
                    "state": "running",
                    "started_at": timestamp,
                    "etag": self._etag("operation", operation.operation_id, version),
                },
            )
            self._validate_operation_authority(connection, started)
            self._replace_reserved_project_action(
                connection,
                identity,
                row,
                started,
                status_code=202,
            )
            return started

    def complete_project_runtime_action(
        self,
        *,
        reservation: ProjectRuntimeActionReservation,
        route: str,
        operation_kind: str,
        project_id: str,
        key: str,
        body: BaseModel | Mapping[str, object],
        if_match: str,
        remote_state: RemoteProjectStateV1 | None,
    ) -> LocalOperationV1:
        validated_remote: RemoteProjectStateV1 | None = None
        if operation_kind == "project_activate":
            if remote_state is None:
                raise ContractValidationError(
                    "project activation completion requires a remote project state"
                )
            try:
                validated_remote = _validate_model(RemoteProjectStateV1, remote_state)
            except ContractValidationError as exc:
                raise ContractValidationError(
                    "project activation completion requires a valid remote project state"
                ) from exc
            self._validate_activation_remote_state(validated_remote)
        elif remote_state is not None:
            raise ContractValidationError(
                "non-activation project completion cannot publish remote project state"
            )
        identity = self._project_action_identity(
            route=route,
            operation_kind=operation_kind,
            project_id=project_id,
            key=key,
            body=body,
            if_match=if_match,
        )
        with self._transaction(write=True) as connection:
            operation, row = self._require_reserved_project_action(
                connection, identity, reservation.operation.operation_id
            )
            self._require_project_operation_kind(reservation, operation_kind)
            if operation.state in {"succeeded", "failed", "cancelled"}:
                return operation
            if operation.state != "running":
                raise ProviderStoreError("project runtime action is no longer completable")
            result = operation.result
            if operation_kind == "project_activate":
                if validated_remote is None:
                    raise ProviderStoreError(
                        "validated activation remote project state is unavailable"
                    )
                current = self._project_from_row(self._require_project_row(connection, project_id))
                active = ProviderMutation(self, connection).set_project_state(
                    project_id,
                    if_match=current.etag,
                    state="active",
                    remote_state=validated_remote,
                    _reservation_operation_id=operation.operation_id,
                )
                result = ProjectOperationResultV1(
                    project_id=project_id,
                    project_etag=active.etag,
                    active=True,
                )
            return self._finish_reserved_project_action(
                connection,
                identity,
                row,
                operation,
                state="succeeded",
                status_code=202,
                result=result,
                error=None,
            )

    def fail_project_runtime_action(
        self,
        *,
        reservation: ProjectRuntimeActionReservation,
        route: str,
        operation_kind: str,
        project_id: str,
        key: str,
        body: BaseModel | Mapping[str, object],
        if_match: str,
        error: ApiErrorV1,
    ) -> LocalOperationV1:
        identity = self._project_action_identity(
            route=route,
            operation_kind=operation_kind,
            project_id=project_id,
            key=key,
            body=body,
            if_match=if_match,
        )
        with self._transaction(write=True) as connection:
            operation, row = self._require_reserved_project_action(
                connection, identity, reservation.operation.operation_id
            )
            self._require_project_operation_kind(reservation, operation_kind)
            if operation.state in {"succeeded", "failed", "cancelled"}:
                return operation
            if operation.state not in {"queued", "running"}:
                raise ProviderStoreError("project runtime action is no longer failable")
            result = (
                self._authoritative_operation_result(connection, operation)
                if operation_kind == "project_activate"
                else operation.result
            )
            return self._finish_reserved_project_action(
                connection,
                identity,
                row,
                operation,
                state="failed",
                status_code=error.http_status,
                result=result,
                error=error,
            )

    def observe_project_runtime_action(
        self,
        *,
        reservation: ProjectRuntimeActionReservation,
        route: str,
        operation_kind: str,
        project_id: str,
        key: str,
        body: BaseModel | Mapping[str, object],
        if_match: str,
    ) -> LocalOperationV1:
        identity = self._project_action_identity(
            route=route,
            operation_kind=operation_kind,
            project_id=project_id,
            key=key,
            body=body,
            if_match=if_match,
        )
        with self._transaction(write=False) as connection:
            operation, _ = self._require_reserved_project_action(
                connection, identity, reservation.operation.operation_id
            )
            self._require_project_operation_kind(reservation, operation_kind)
            return operation

    def _project_action_identity(
        self,
        *,
        route: str,
        operation_kind: str,
        project_id: str,
        key: str,
        body: BaseModel | Mapping[str, object],
        if_match: str,
    ) -> tuple[str, str, str, str, str, int]:
        if operation_kind not in _PROJECT_OPERATION_KINDS:
            raise ContractValidationError("project runtime operation kind is invalid")
        self._validate_if_match(if_match)
        method = "POST"
        route = self._bounded_identity("route", route, MAX_IDENTITY_BYTES)
        project_id = self._bounded_identity("resource_scope", project_id, MAX_IDENTITY_BYTES)
        key = self._bounded_identity("idempotency key", key, MAX_IDEMPOTENCY_KEY_BYTES, minimum=16)
        body_value = body.model_dump(mode="json") if isinstance(body, BaseModel) else dict(body)
        request_value = {
            "operation_kind": operation_kind,
            "body": body_value,
            "headers": {"if-match": if_match},
        }
        self._reject_credential_bearing_keys(request_value)
        request_bytes = _canonical_json_bytes(request_value)
        if len(request_bytes) > MAX_REQUEST_BYTES:
            raise ContractValidationError("canonical idempotency request exceeds the byte limit")
        return (
            method,
            route,
            project_id,
            key,
            sha256(request_bytes).hexdigest(),
            int(self._now().timestamp()),
        )

    def _project_action_replay(
        self,
        connection: sqlite3.Connection,
        identity: tuple[str, str, str, str, str, int],
        *,
        discard_expired: bool = False,
        admission_guard: Callable[[ProjectV1], None] | None = None,
    ) -> LocalOperationV1 | None:
        row = connection.execute(
            """
            SELECT *
            FROM idempotency_records
            WHERE principal = ? AND method = ? AND route = ?
              AND resource_scope = ? AND idempotency_key = ?
            """,
            (LOCAL_PRINCIPAL, *identity[:4]),
        ).fetchone()
        if row is None:
            return None
        row = cast(sqlite3.Row, row)
        if discard_expired and self._discard_expired_idempotency_record(
            connection, row, now_epoch=identity[5]
        ):
            return None
        if admission_guard is not None:
            project = self._project_from_row(
                self._require_project_row(connection, identity[2])
            )
            admission_guard(project)
        if row["request_digest"] != identity[4]:
            raise IdempotencyConflictError(
                "idempotency key is already bound to a different request"
            )
        return self._operation_for_idempotency_record(connection, row)

    def _require_reserved_project_action(
        self,
        connection: sqlite3.Connection,
        identity: tuple[str, str, str, str, str, int],
        operation_id: str,
    ) -> tuple[LocalOperationV1, sqlite3.Row]:
        replay = self._project_action_replay(connection, identity)
        if replay is None or replay.operation_id != operation_id:
            raise ProviderStoreError("project runtime action reservation is unavailable")
        row = self._require_operation_row(connection, operation_id)
        return self._operation_from_row(row), row

    @staticmethod
    def _require_project_operation_kind(
        reservation: ProjectRuntimeActionReservation,
        operation_kind: str,
    ) -> None:
        if (
            operation_kind not in _PROJECT_OPERATION_KINDS
            or reservation.operation.operation_kind != operation_kind
        ):
            raise ContractValidationError("project runtime reservation kind differs")

    def _replace_reserved_project_action(
        self,
        connection: sqlite3.Connection,
        identity: tuple[str, str, str, str, str, int],
        row: sqlite3.Row,
        operation: LocalOperationV1,
        *,
        status_code: int,
    ) -> None:
        response_bytes = _canonical_json_bytes(operation.model_dump(mode="json"))
        if len(response_bytes) > PROJECT_RUNTIME_TERMINAL_SLOT_BYTES:
            raise ProviderStoreError("project runtime response exceeds its reserved slot")
        connection.execute(
            """
            UPDATE local_operations
            SET state = ?, document_json = ?, resource_version = ?, finished_at = ?
            WHERE operation_id = ?
            """,
            (
                operation.state,
                response_bytes,
                cast(int, row["resource_version"]) + 1,
                operation.finished_at,
                operation.operation_id,
            ),
        )
        updated = connection.execute(
            """
            UPDATE idempotency_records
            SET status_code = ?, response_bytes = ?, cleanup_eligible = ?
            WHERE principal = ? AND method = ? AND route = ?
              AND resource_scope = ? AND idempotency_key = ? AND request_digest = ?
              AND operation_id = ?
            """,
            (
                status_code,
                response_bytes,
                int(operation.state not in {"queued", "running", "cancelling"}),
                LOCAL_PRINCIPAL,
                *identity[:5],
                operation.operation_id,
            ),
        )
        if updated.rowcount != 1:
            raise ProviderStoreError("project action idempotency reservation changed")

    def _finish_reserved_project_action(
        self,
        connection: sqlite3.Connection,
        identity: tuple[str, str, str, str, str, int],
        row: sqlite3.Row,
        operation: LocalOperationV1,
        *,
        state: Literal["succeeded", "failed"],
        status_code: int,
        result: LocalOperationResultV1 | None,
        error: ApiErrorV1 | None,
    ) -> LocalOperationV1:
        version = cast(int, row["resource_version"]) + 1
        timestamp = self._timestamp()
        finished = _validate_model(
            LocalOperationV1,
            {
                **operation.model_dump(mode="python"),
                "state": state,
                "result": result,
                "error": error,
                "finished_at": timestamp,
                "etag": self._etag("operation", operation.operation_id, version),
            },
        )
        self._validate_operation_authority(connection, finished)
        self._replace_reserved_project_action(
            connection,
            identity,
            row,
            finished,
            status_code=status_code,
        )
        return finished

    def _execute_idempotent(
        self,
        *,
        method: str,
        route: str,
        resource_scope: str,
        key: str,
        request_value: object,
        response_model: type[_ModelT],
        bound_if_match: str | None,
        mutation: Callable[[ProviderMutation], tuple[int, BaseModel]],
    ) -> IdempotencyResult:
        response_type = self._response_type_name(response_model)
        method = self._bounded_identity("method", method, MAX_IDENTITY_BYTES)
        route = self._bounded_identity("route", route, MAX_IDENTITY_BYTES)
        resource_scope = self._bounded_identity(
            "resource_scope", resource_scope, MAX_IDENTITY_BYTES
        )
        key = self._bounded_identity("idempotency key", key, MAX_IDEMPOTENCY_KEY_BYTES, minimum=16)
        self._reject_credential_bearing_keys(request_value)
        request_bytes = _canonical_json_bytes(request_value)
        if len(request_bytes) > MAX_REQUEST_BYTES:
            raise ContractValidationError("canonical idempotency request exceeds the byte limit")
        request_digest = sha256(request_bytes).hexdigest()
        now_epoch = int(self._now().timestamp())

        with self._transaction(write=True) as connection:
            self._cleanup_expired_idempotency_records(connection, now_epoch)
            existing = connection.execute(
                """
                SELECT *
                FROM idempotency_records
                WHERE principal = ? AND method = ? AND route = ?
                  AND resource_scope = ? AND idempotency_key = ?
                """,
                (LOCAL_PRINCIPAL, method, route, resource_scope, key),
            ).fetchone()
            if existing is not None:
                existing = cast(sqlite3.Row, existing)
                if self._discard_expired_idempotency_record(
                    connection, existing, now_epoch=now_epoch
                ):
                    existing = None
            if existing is not None:
                if existing["request_digest"] != request_digest:
                    raise IdempotencyConflictError(
                        "idempotency key is already bound to a different request"
                    )
                if existing["response_type"] != response_type:
                    raise ProviderDataCorruptionError(
                        "idempotency replay response type differs from the typed route"
                    )
                response_bytes = bytes(existing["response_bytes"])
                replay_response = self._model_from_response(response_model, response_bytes)
                if (
                    _canonical_json_bytes(replay_response.model_dump(mode="json"))
                    != response_bytes
                ):
                    raise ProviderDataCorruptionError(
                        "stored idempotency replay response is not canonical"
                    )
                if isinstance(replay_response, LocalOperationV1):
                    replay_response = cast(
                        _ModelT,
                        self._operation_for_idempotency_record(
                            connection,
                            existing,
                        ),
                    )
                    response_bytes = _canonical_json_bytes(replay_response.model_dump(mode="json"))
                return IdempotencyResult(
                    status_code=cast(int, existing["status_code"]),
                    response_bytes=response_bytes,
                    replayed=True,
                )

            count, _ = self._provider_record_counts(connection)
            if count >= self._max_idempotency_records:
                raise IdempotencyCapacityError("live idempotency record capacity is exhausted")

            transaction = ProviderMutation(self, connection, if_match=bound_if_match)
            status_code, response = mutation(transaction)
            if type(status_code) is not int or not 100 <= status_code <= 599:
                raise ProviderStoreError("idempotent mutation returned an invalid status code")
            if (
                type(response) is not response_model
                or type(response).__module__ != "desktop.sidecar.contracts.v1.models"
                or response.model_config.get("extra") != "forbid"
            ):
                raise ProviderStoreError(
                    "idempotent mutation must return a closed Desktop v1 response model"
                )
            response_bytes = _canonical_json_bytes(response.model_dump(mode="json"))
            if len(response_bytes) > MAX_RESPONSE_BYTES:
                raise ProviderStoreError("idempotent response exceeds the byte limit")
            if isinstance(response, LocalOperationV1):
                if response.resource.resource_id != resource_scope:
                    raise ProviderStoreError(
                        "idempotent operation response differs from its resource scope"
                    )
                try:
                    self._validate_action_route_binding(
                        route=route,
                        resource_scope=resource_scope,
                        resource_type=response.resource.resource_type,
                        operation_kind=response.operation_kind,
                    )
                except ContractValidationError as exc:
                    raise ProviderStoreError(
                        "idempotent operation response differs from its action route"
                    ) from exc
            self._insert_idempotency_record(
                connection,
                principal=LOCAL_PRINCIPAL,
                method=method,
                route=route,
                resource_scope=resource_scope,
                key=key,
                request_digest=request_digest,
                operation_id=(
                    response.operation_id if isinstance(response, LocalOperationV1) else None
                ),
                response_type=response_type,
                status_code=status_code,
                response_bytes=response_bytes,
                now_epoch=now_epoch,
            )
            transaction._validate_created_operations_bound()
            return IdempotencyResult(status_code, response_bytes, False)

    @staticmethod
    def _normalize_semantic_headers(headers: Mapping[str, str]) -> dict[str, str]:
        if not isinstance(headers, Mapping):
            raise ContractValidationError("semantic headers must be a mapping")
        normalized: dict[str, str] = {}
        forbidden = {"authorization", "cookie", "idempotency-key", "if-match"}
        for key, value in headers.items():
            normalized_key = key.lower() if type(key) is str else ""
            if (
                type(key) is not str
                or re.fullmatch(r"[a-z0-9][a-z0-9-]{0,127}", normalized_key) is None
                or normalized_key in forbidden
                or "session-token" in normalized_key
                or normalized_key in normalized
            ):
                raise ContractValidationError(
                    "semantic header name is invalid or credential-bearing"
                )
            normalized[normalized_key] = DesktopProviderStore._bounded_identity(
                "semantic header value", value, MAX_IDENTITY_BYTES
            )
        return dict(sorted(normalized.items()))

    @staticmethod
    def _response_type_name(model_type: type[BaseModel]) -> str:
        allowed = {
            RemoteProfileV1: "RemoteProfileV1",
            ProjectV1: "ProjectV1",
            LocalOperationV1: "LocalOperationV1",
        }
        try:
            return allowed[model_type]
        except KeyError as exc:
            raise ContractValidationError(
                "idempotent response model is not a persisted Desktop resource type"
            ) from exc

    @staticmethod
    def _response_model_for_name(name: str) -> type[BaseModel]:
        allowed: dict[str, type[BaseModel]] = {
            "RemoteProfileV1": RemoteProfileV1,
            "ProjectV1": ProjectV1,
            "LocalOperationV1": LocalOperationV1,
        }
        try:
            return allowed[name]
        except KeyError as exc:
            raise ProviderDataCorruptionError("idempotency response type is invalid") from exc

    @staticmethod
    def _validate_action_route_binding(
        *,
        route: str,
        resource_scope: str,
        resource_type: str,
        operation_kind: str,
    ) -> None:
        if operation_kind in _PROFILE_OPERATION_KINDS:
            expected_resource_type = "profile"
            collection = "profiles"
        elif operation_kind in _PROJECT_OPERATION_KINDS:
            expected_resource_type = "project"
            collection = "projects"
        else:
            raise ContractValidationError("local action operation kind is not persistable")
        expected_route = (
            f"/desktop/v1/{collection}/{resource_scope}/{_ACTION_ROUTE_SUFFIXES[operation_kind]}"
        )
        if resource_type != expected_resource_type or route != expected_route:
            raise ContractValidationError(
                "local action route, resource scope, and operation kind differ"
            )

    def _operation_for_idempotency_record(
        self,
        connection: sqlite3.Connection,
        row: sqlite3.Row,
    ) -> LocalOperationV1:
        if row["response_type"] != "LocalOperationV1":
            raise ProviderDataCorruptionError(
                "operation idempotency response type is not LocalOperationV1"
            )
        response_bytes = bytes(row["response_bytes"])
        stored = self._model_from_response(LocalOperationV1, response_bytes)
        if _canonical_json_bytes(stored.model_dump(mode="json")) != response_bytes:
            raise ProviderDataCorruptionError("operation idempotency response is not canonical")
        if row["operation_id"] != stored.operation_id:
            raise ProviderDataCorruptionError(
                "operation idempotency authority references another operation"
            )
        try:
            operation_row = self._require_operation_row(connection, stored.operation_id)
        except ResourceNotFoundError as exc:
            raise ProviderDataCorruptionError(
                "operation idempotency response references a missing local operation"
            ) from exc
        operation_bytes = bytes(operation_row["document_json"])
        operation = self._operation_from_row(operation_row)
        self._validate_operation_indexed_scalars(operation_row, operation)
        if response_bytes != operation_bytes or stored != operation:
            raise ProviderDataCorruptionError(
                "operation idempotency response differs from its exact operation row"
            )
        expected_authority = self._action_authority_digest(
            principal=cast(str, row["principal"]),
            method=cast(str, row["method"]),
            route=cast(str, row["route"]),
            resource_scope=cast(str, row["resource_scope"]),
            key=cast(str, row["idempotency_key"]),
            request_digest=cast(str, row["request_digest"]),
            operation=operation,
        )
        if not hmac.compare_digest(
            cast(str | None, operation_row["action_identity_digest"]) or "",
            expected_authority,
        ):
            raise ProviderDataCorruptionError(
                "operation row differs from its exact idempotency action authority"
            )
        if (
            row["principal"] != LOCAL_PRINCIPAL
            or row["method"] != "POST"
            or row["resource_scope"] != operation.resource.resource_id
        ):
            raise ProviderDataCorruptionError(
                "operation idempotency identity differs from its operation"
            )
        try:
            self._validate_action_route_binding(
                route=cast(str, row["route"]),
                resource_scope=cast(str, row["resource_scope"]),
                resource_type=operation.resource.resource_type,
                operation_kind=operation.operation_kind,
            )
        except ContractValidationError as exc:
            raise ProviderDataCorruptionError(
                "operation idempotency route differs from its operation binding"
            ) from exc
        return operation

    @staticmethod
    def _action_authority_digest(
        *,
        principal: str,
        method: str,
        route: str,
        resource_scope: str,
        key: str,
        request_digest: str,
        operation: LocalOperationV1,
    ) -> str:
        identity_bytes = _canonical_json_bytes(
            {
                "principal": principal,
                "method": method,
                "route": route,
                "resource_scope": resource_scope,
                "idempotency_key": key,
                "request_digest": request_digest,
                "operation_id": operation.operation_id,
                "operation_kind": operation.operation_kind,
            }
        )
        return sha256(_ACTION_AUTHORITY_DOMAIN + identity_bytes).hexdigest()

    def _cleanup_expired_idempotency_records(
        self,
        connection: sqlite3.Connection,
        now_epoch: int,
    ) -> None:
        rows = connection.execute(
            """
            SELECT *
            FROM idempotency_records INDEXED BY idempotency_expiry_idx
            WHERE cleanup_eligible = 1 AND expires_at_epoch <= ?
            ORDER BY expires_at_epoch
            LIMIT ?
            """,
            (now_epoch, NORMAL_WRITE_CLEANUP_ROWS),
        ).fetchall()
        for candidate in rows:
            row = cast(sqlite3.Row, candidate)
            if not self._discard_expired_idempotency_record(connection, row, now_epoch=now_epoch):
                raise ProviderDataCorruptionError(
                    "selected idempotency record is not cleanup eligible"
                )

    def _discard_expired_idempotency_record(
        self,
        connection: sqlite3.Connection,
        row: sqlite3.Row,
        *,
        now_epoch: int,
    ) -> bool:
        if row["expires_at_epoch"] > now_epoch or row["cleanup_eligible"] != 1:
            return False
        if row["response_type"] == "LocalOperationV1":
            operation = self._operation_for_idempotency_record(connection, row)
            if operation.state in {"queued", "running", "cancelling"}:
                raise ProviderDataCorruptionError(
                    "live operation idempotency record is cleanup eligible"
                )
        deleted = connection.execute(
            """
                DELETE FROM idempotency_records
                WHERE principal = ? AND method = ? AND route = ?
                  AND resource_scope = ? AND idempotency_key = ?
                  AND request_digest = ?
                  AND operation_id IS ? AND response_type = ? AND response_bytes = ?
                  AND expires_at_epoch <= ?
                """,
            (
                row["principal"],
                row["method"],
                row["route"],
                row["resource_scope"],
                row["idempotency_key"],
                row["request_digest"],
                row["operation_id"],
                row["response_type"],
                row["response_bytes"],
                now_epoch,
            ),
        )
        if deleted.rowcount != 1:
            raise ProviderDataCorruptionError("expired idempotency record changed during cleanup")
        return True

    def _insert_idempotency_record(
        self,
        connection: sqlite3.Connection,
        *,
        principal: str,
        method: str,
        route: str,
        resource_scope: str,
        key: str,
        request_digest: str,
        operation_id: str | None,
        response_type: str,
        status_code: int,
        response_bytes: bytes,
        now_epoch: int,
    ) -> None:
        if (response_type == "LocalOperationV1") != (operation_id is not None):
            raise ProviderStoreError(
                "operation idempotency response requires an exact operation identity"
            )
        cleanup_eligible = 1
        if operation_id is not None:
            operation_row = self._require_operation_row(connection, operation_id)
            operation = self._operation_from_row(operation_row)
            cleanup_eligible = int(operation.state not in {"queued", "running", "cancelling"})
            if bytes(operation_row["document_json"]) != response_bytes:
                raise ProviderStoreError(
                    "operation idempotency response differs from the operation row"
                )
            authority_digest = self._action_authority_digest(
                principal=principal,
                method=method,
                route=route,
                resource_scope=resource_scope,
                key=key,
                request_digest=request_digest,
                operation=operation,
            )
            bound = connection.execute(
                """
                UPDATE local_operations
                SET action_identity_digest = ?
                WHERE operation_id = ?
                  AND (
                    action_identity_digest IS NULL OR action_identity_digest = ?
                  )
                """,
                (authority_digest, operation_id, authority_digest),
            )
            if bound.rowcount != 1:
                raise ProviderStoreError(
                    "operation is already bound to another idempotency action"
                )
        connection.execute(
            """
            INSERT INTO idempotency_records(
                principal, method, route, resource_scope, idempotency_key,
                request_digest, operation_id, response_type, status_code, response_bytes,
                cleanup_eligible, created_at_epoch, expires_at_epoch
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                principal,
                method,
                route,
                resource_scope,
                key,
                request_digest,
                operation_id,
                response_type,
                status_code,
                response_bytes,
                cleanup_eligible,
                now_epoch,
                now_epoch + self._idempotency_retention_seconds,
            ),
        )

    @staticmethod
    def _normalized_persistence_key(value: str) -> str:
        normalized = unicodedata.normalize("NFKC", value).casefold()
        return "".join(character for character in normalized if character.isalnum())

    @classmethod
    def _walk_persisted_keys(cls, value: object) -> Iterator[str]:
        pending = [value]
        while pending:
            current = pending.pop()
            if isinstance(current, Mapping):
                for key, child in current.items():
                    if type(key) is str:
                        yield cls._normalized_persistence_key(key)
                    pending.append(child)
            elif isinstance(current, (list, tuple)):
                pending.extend(current)

    @classmethod
    def _reject_credential_bearing_keys(cls, value: object) -> None:
        for key in cls._walk_persisted_keys(value):
            if key in _PERSISTENCE_DENIED_CONFIG_KEYS or key.endswith(_SECRET_KEY_SUFFIXES):
                if key in {
                    "filesystempath",
                    "hostpath",
                    "homedir",
                    "homedirectory",
                    "localpath",
                    "processoutput",
                    "processstderr",
                    "processstdout",
                    "rawdiagnostic",
                    "rawdiagnostics",
                    "rawlog",
                    "rawlogs",
                    "stacktrace",
                    "stderr",
                    "stdout",
                    "traceback",
                    "workingdirectory",
                    "workdir",
                }:
                    continue
                raise ContractValidationError(
                    "idempotency request contains a credential-bearing key"
                )

    @classmethod
    def _validate_project_config_for_persistence(cls, project: ProjectCreateV1) -> None:
        for selection in project.evolution.targets.root.values():
            for key in cls._walk_persisted_keys(selection.config.root):
                denied = key in _PERSISTENCE_DENIED_CONFIG_KEYS or key.endswith(
                    _PERSISTENCE_DENIED_CONFIG_SUFFIXES
                )
                host_path = key.endswith(("path", "directory", "workdir")) and (
                    key not in _ALLOWED_PROJECT_CONFIG_PATH_KEYS
                )
                if denied or host_path:
                    raise ContractValidationError(
                        "project method config contains a persistence-denied key"
                    )

    @staticmethod
    def _bounded_identity(label: str, value: str, maximum: int, *, minimum: int = 1) -> str:
        if (
            type(value) is not str
            or value != value.strip()
            or any(ord(char) < 0x20 for char in value)
        ):
            raise ContractValidationError(f"{label} must be trimmed text without controls")
        size = len(value.encode("utf-8"))
        if not minimum <= size <= maximum:
            raise ContractValidationError(f"{label} exceeds its byte bounds")
        return value

    @staticmethod
    def _validate_resource_id(resource_id: str) -> None:
        DesktopProviderStore._bounded_identity("resource id", resource_id, 256)

    @staticmethod
    def _model_from_response(model_type: type[_ModelT], response_bytes: bytes) -> _ModelT:
        try:
            return model_type.model_validate_json(response_bytes)
        except ValidationError as exc:
            raise ProviderDataCorruptionError(
                f"stored idempotent {model_type.__name__} response is invalid"
            ) from exc

    @staticmethod
    def _require_profile_row(connection: sqlite3.Connection, profile_id: str) -> sqlite3.Row:
        row = connection.execute(
            "SELECT * FROM remote_profiles WHERE profile_id = ?", (profile_id,)
        ).fetchone()
        if row is None:
            raise ResourceNotFoundError("profile", profile_id)
        return cast(sqlite3.Row, row)

    @staticmethod
    def _project_select_columns() -> str:
        return (
            "project_id, profile_id, name, document_json, state, current_revision_id, "
            "CASE WHEN remote_state_json IS NULL OR "
            f"length(CAST(remote_state_json AS BLOB)) <= {MAX_REMOTE_PROJECT_STATE_BYTES} "
            "THEN remote_state_json ELSE NULL END AS remote_state_json, "
            "length(CAST(remote_state_json AS BLOB)) AS remote_state_bytes, "
            "remote_state_token_0, remote_state_token_1, "
            "remote_state_token_2, remote_state_token_3, "
            "resource_version, created_at, updated_at"
        )

    def _require_project_row(self, connection: sqlite3.Connection, project_id: str) -> sqlite3.Row:
        row = connection.execute(
            f"SELECT {self._project_select_columns()} FROM projects WHERE project_id = ?",
            (project_id,),
        ).fetchone()
        if row is None:
            raise ResourceNotFoundError("project", project_id)
        guarded = cast(sqlite3.Row, row)
        self._validate_remote_state_cell(guarded)
        return guarded

    @staticmethod
    def _require_project_not_busy(
        connection: sqlite3.Connection,
        project_id: str,
        *,
        excluded_operation_id: str | None = None,
    ) -> None:
        row = connection.execute(
            """
            SELECT operation_id
            FROM local_operations
            WHERE resource_type = 'project' AND resource_id = ?
              AND state IN ('queued', 'running', 'cancelling')
              AND operation_id != ?
            ORDER BY operation_id
            LIMIT 1
            """,
            (project_id, excluded_operation_id or ""),
        ).fetchone()
        if row is not None:
            raise ResourceInUseError("project", project_id)

    def _require_project_operation_reservation_available(
        self,
        connection: sqlite3.Connection,
        project_id: str,
        *,
        operation_kind: str,
        excluded_operation_id: str | None = None,
    ) -> None:
        """Reserve every project row a nonterminal operation can mutate."""

        if operation_kind not in _PROJECT_OPERATION_KINDS:
            raise ContractValidationError("project runtime operation kind is invalid")
        if excluded_operation_id is not None:
            excluded = self._require_operation_row(connection, excluded_operation_id)
            if (
                excluded["operation_kind"] != operation_kind
                or excluded["resource_type"] != "project"
                or excluded["resource_id"] != project_id
                or excluded["state"] not in {"queued", "running", "cancelling"}
            ):
                raise ContractValidationError(
                    "project reservation exclusion differs from its operation authority"
                )
        self._require_project_not_busy(
            connection,
            project_id,
            excluded_operation_id=excluded_operation_id,
        )
        project = self._require_project_row(connection, project_id)
        if operation_kind == "project_activate":
            competing_activation = connection.execute(
                """
                SELECT resource_id
                FROM local_operations
                WHERE operation_kind = 'project_activate'
                  AND resource_type = 'project'
                  AND state IN ('queued', 'running', 'cancelling')
                  AND operation_id != ?
                ORDER BY operation_id
                LIMIT 1
                """,
                (excluded_operation_id or "",),
            ).fetchone()
            if competing_activation is not None:
                raise ResourceInUseError("project", cast(str, competing_activation["resource_id"]))

            active = connection.execute(
                """
                SELECT project_id
                FROM projects
                WHERE state = 'active' AND project_id != ?
                LIMIT 1
                """,
                (project_id,),
            ).fetchone()
            if active is not None:
                active_project_id = cast(str, active["project_id"])
                self._require_project_not_busy(
                    connection,
                    active_project_id,
                    excluded_operation_id=excluded_operation_id,
                )
            return

        if project["state"] == "active":
            self._require_active_project_activation_authority_available(
                connection,
                project_id,
                excluded_operation_id=excluded_operation_id,
            )

    @staticmethod
    def _require_active_project_activation_authority_available(
        connection: sqlite3.Connection,
        project_id: str,
        *,
        excluded_operation_id: str | None = None,
    ) -> None:
        displacing_activation = connection.execute(
            """
            SELECT operation_id
            FROM local_operations
            WHERE operation_kind = 'project_activate'
              AND resource_type = 'project'
              AND resource_id != ?
              AND state IN ('queued', 'running', 'cancelling')
              AND operation_id != ?
            ORDER BY operation_id
            LIMIT 1
            """,
            (project_id, excluded_operation_id or ""),
        ).fetchone()
        if displacing_activation is not None:
            raise ResourceInUseError("project", project_id)

    @staticmethod
    def _require_operation_row(connection: sqlite3.Connection, operation_id: str) -> sqlite3.Row:
        row = connection.execute(
            "SELECT * FROM local_operations WHERE operation_id = ?", (operation_id,)
        ).fetchone()
        if row is None:
            raise ResourceNotFoundError("operation", operation_id)
        return cast(sqlite3.Row, row)

    def _profile_from_row(self, row: sqlite3.Row) -> RemoteProfileV1:
        document = _decode_json_object(bytes(row["document_json"]), label="profile")
        try:
            slots = json.loads(bytes(row["credential_slots_json"]))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ProviderDataCorruptionError("stored credential slot status is invalid") from exc
        payload = {
            **document,
            "profile_id": row["profile_id"],
            "credential_slots": slots,
            "connection_state": row["connection_state"],
            "host_key_fingerprint": row["host_key_fingerprint"],
            "etag": self._etag("profile", row["profile_id"], row["resource_version"]),
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }
        return _validate_json_model(RemoteProfileV1, payload)

    def _project_from_row(self, row: sqlite3.Row) -> ProjectV1:
        document = _decode_json_object(bytes(row["document_json"]), label="project")
        remote_state: RemoteProjectStateV1 | None = None
        remote_state_raw = row["remote_state_json"]
        if remote_state_raw is not None:
            if type(remote_state_raw) is not bytes:
                raise ProviderDataCorruptionError(
                    "stored remote project state is not canonical bytes"
                )
            remote_document = _decode_json_object(
                remote_state_raw,
                label="remote project state",
            )
            try:
                remote_state = _validate_json_model(RemoteProjectStateV1, remote_document)
            except ProviderDataCorruptionError as exc:
                raise ProviderDataCorruptionError(
                    "stored remote project state violates RemoteProjectStateV1"
                ) from exc
        current_revision_id = row["current_revision_id"]
        if current_revision_id is not None:
            try:
                self._validate_resource_id(current_revision_id)
            except ContractValidationError as exc:
                raise ProviderDataCorruptionError(
                    "stored project revision identity is invalid"
                ) from exc
            if (
                remote_state is None
                or remote_state.active_revision is None
                or remote_state.active_revision.id != current_revision_id
            ):
                raise ProviderDataCorruptionError(
                    "stored remote project revision differs from current revision"
                )
        if row["state"] == "active":
            if current_revision_id is None or remote_state is None:
                raise ProviderDataCorruptionError(
                    "active project is missing its remote project projection"
                )
            try:
                self._validate_activation_remote_state(remote_state)
            except ContractValidationError as exc:
                raise ProviderDataCorruptionError(
                    "active project has an invalid remote project projection"
                ) from exc
        elif current_revision_id is not None:
            raise ProviderDataCorruptionError(
                "inactive project retains a current revision identity"
            )
        payload = {
            **document,
            "project_id": row["project_id"],
            "state": row["state"],
            "remote": (remote_state.model_dump(mode="json") if remote_state is not None else None),
            "etag": self._etag("project", row["project_id"], row["resource_version"]),
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }
        return _validate_json_model(ProjectV1, payload)

    def _validate_remote_state_cell(self, row: sqlite3.Row) -> None:
        remote_state_bytes = row["remote_state_bytes"]
        remote_state_raw = row["remote_state_json"]
        token = tuple(row[f"remote_state_token_{index}"] for index in range(4))
        if remote_state_bytes is None:
            if remote_state_raw is not None or token != (None, None, None, None):
                raise ProviderDataCorruptionError(
                    "stored remote project state has an invalid null identity"
                )
            return
        if (
            type(remote_state_bytes) is not int
            or remote_state_bytes < 1
            or remote_state_bytes > MAX_REMOTE_PROJECT_STATE_BYTES
        ):
            raise ProviderDataCorruptionError("stored remote project state exceeds its byte bound")
        if type(remote_state_raw) is not bytes or len(remote_state_raw) != remote_state_bytes:
            raise ProviderDataCorruptionError(
                "stored remote project state changed during guarded read"
            )
        expected_token = self._remote_payload_content_token(
            project_id=cast(str, row["project_id"]),
            payload=remote_state_raw,
        )
        if token != expected_token:
            raise ProviderDataCorruptionError(
                "stored remote project state content authority is invalid"
            )

    def _operation_from_row(self, row: sqlite3.Row) -> LocalOperationV1:
        document = _decode_json_object(bytes(row["document_json"]), label="operation")
        operation = _validate_json_model(LocalOperationV1, document)
        expected_etag = self._etag(
            "operation", operation.operation_id, cast(int, row["resource_version"])
        )
        if operation.etag != expected_etag:
            raise ProviderDataCorruptionError("operation ETag differs from its stored version")
        return operation

    def _validate_operation_authority(
        self,
        connection: sqlite3.Connection,
        operation: LocalOperationV1,
        *,
        allow_historical: bool = False,
    ) -> None:
        result = operation.result
        if (
            operation.operation_kind in _PROFILE_OPERATION_KINDS
            and operation.resource.resource_type != "profile"
        ) or (
            operation.operation_kind in _PROJECT_OPERATION_KINDS
            and operation.resource.resource_type != "project"
        ):
            raise ContractValidationError("local operation kind differs from its resource type")
        if operation.operation_kind in _PROFILE_OPERATION_KINDS:
            try:
                row = self._require_profile_row(connection, operation.resource.resource_id)
            except ResourceNotFoundError:
                if allow_historical and operation.state in {
                    "succeeded",
                    "failed",
                    "cancelled",
                }:
                    return
                raise
            if isinstance(result, ConnectionOperationResultV1):
                if result.profile_id != operation.resource.resource_id:
                    raise ContractValidationError(
                        "connection operation result differs from its resource"
                    )
                current_state = cast(str, row["connection_state"])
                expected_state = (
                    current_state
                    if current_state in {"connected", "disconnected", "host_key_required"}
                    else "disconnected"
                )
                if not allow_historical and result.connection_state != expected_state:
                    raise ContractValidationError(
                        "connection operation result differs from profile state"
                    )
            elif result is not None:
                raise ContractValidationError("profile operation has a non-connection result")
            elif operation.state == "succeeded":
                raise ContractValidationError(
                    "succeeded profile operation requires a connection result"
                )
        elif operation.operation_kind == "project_activate":
            try:
                row = self._require_project_row(connection, operation.resource.resource_id)
            except ResourceNotFoundError:
                if allow_historical and operation.state in {
                    "succeeded",
                    "failed",
                    "cancelled",
                }:
                    return
                raise
            if isinstance(result, ProjectOperationResultV1):
                expected_etag = self._etag(
                    "project", operation.resource.resource_id, row["resource_version"]
                )
                if result.project_id != operation.resource.resource_id or (
                    not allow_historical
                    and (
                        result.project_etag != expected_etag
                        or result.active != (row["state"] == "active")
                    )
                ):
                    raise ContractValidationError(
                        "project operation result differs from project state"
                    )
            elif result is not None:
                raise ContractValidationError("project operation has a non-project result")
            elif operation.state == "succeeded":
                raise ContractValidationError(
                    "succeeded project operation requires a project result"
                )

    @staticmethod
    def _operation_terminal_slot_bytes(operation_kind: str) -> int:
        if operation_kind in _PROFILE_OPERATION_KINDS:
            return PROFILE_RUNTIME_TERMINAL_SLOT_BYTES
        if operation_kind in _PROJECT_OPERATION_KINDS:
            return PROJECT_RUNTIME_TERMINAL_SLOT_BYTES
        raise ContractValidationError("local operation kind has no terminal capacity slot")

    def _validate_nonterminal_operation_terminal_capacity(
        self,
        connection: sqlite3.Connection,
        operation: LocalOperationV1,
    ) -> None:
        cancelled = _validate_model(
            LocalOperationV1,
            {
                **operation.model_dump(mode="python"),
                "state": "cancelled",
                "result": self._startup_cancellation_result(connection, operation),
                "error": None,
                "finished_at": operation.created_at,
                "etag": self._etag("operation", operation.operation_id, 2),
            },
        )
        terminal_bytes = _canonical_json_bytes(cancelled.model_dump(mode="json"))
        if len(terminal_bytes) > self._operation_terminal_slot_bytes(operation.operation_kind):
            raise ContractValidationError(
                "nonterminal local operation exceeds its fixed terminal slot"
            )

    def _startup_cancellation_result(
        self,
        connection: sqlite3.Connection,
        operation: LocalOperationV1,
    ) -> LocalOperationResultV1 | None:
        if operation.operation_kind in _PROFILE_OPERATION_KINDS:
            return ConnectionOperationResultV1(
                profile_id=operation.resource.resource_id,
                connection_state="disconnected",
            )
        if operation.operation_kind == "project_activate":
            row = self._require_project_row(connection, operation.resource.resource_id)
            return ProjectOperationResultV1(
                project_id=operation.resource.resource_id,
                project_etag=self._etag(
                    "project", operation.resource.resource_id, row["resource_version"] + 1
                ),
                active=False,
            )
        return operation.result

    def _authoritative_operation_result(
        self, connection: sqlite3.Connection, operation: LocalOperationV1
    ) -> LocalOperationResultV1 | None:
        if operation.operation_kind in _PROFILE_OPERATION_KINDS:
            try:
                row = self._require_profile_row(connection, operation.resource.resource_id)
            except ResourceNotFoundError:
                return ConnectionOperationResultV1(
                    profile_id=operation.resource.resource_id,
                    connection_state="disconnected",
                )
            state = cast(str, row["connection_state"])
            result_state = (
                state
                if state in {"connected", "disconnected", "host_key_required"}
                else "disconnected"
            )
            return ConnectionOperationResultV1(
                profile_id=operation.resource.resource_id,
                connection_state=cast(
                    Literal["connected", "disconnected", "host_key_required"],
                    result_state,
                ),
            )
        if operation.operation_kind == "project_activate":
            try:
                row = self._require_project_row(connection, operation.resource.resource_id)
            except ResourceNotFoundError:
                if isinstance(operation.result, ProjectOperationResultV1):
                    return operation.result.model_copy(update={"active": False})
                return operation.result
            return ProjectOperationResultV1(
                project_id=operation.resource.resource_id,
                project_etag=self._etag(
                    "project", operation.resource.resource_id, row["resource_version"]
                ),
                active=row["state"] == "active",
            )
        return operation.result

    def _cancel_operation_with_authority(
        self,
        connection: sqlite3.Connection,
        row: sqlite3.Row,
    ) -> LocalOperationV1:
        operation = self._operation_from_row(row)
        if operation.state in {"succeeded", "failed", "cancelled"}:
            return operation
        authoritative_result = self._authoritative_operation_result(connection, operation)
        version = cast(int, row["resource_version"]) + 1
        timestamp = self._timestamp()
        reconciled = _validate_model(
            LocalOperationV1,
            {
                **operation.model_dump(mode="python"),
                "state": "cancelled",
                "result": authoritative_result,
                "error": None,
                "finished_at": timestamp,
                "etag": self._etag("operation", operation.operation_id, version),
            },
        )
        response_bytes = _canonical_json_bytes(reconciled.model_dump(mode="json"))
        if len(response_bytes) > self._operation_terminal_slot_bytes(operation.operation_kind):
            label = (
                "profile runtime"
                if operation.operation_kind in _PROFILE_OPERATION_KINDS
                else "project runtime"
            )
            raise ProviderDataCorruptionError(f"{label} cancellation exceeds its reserved slot")
        previous_bytes = bytes(row["document_json"])
        idempotency_rows = self._idempotency_rows_for_operation(
            connection,
            operation,
            previous_bytes,
        )
        connection.execute(
            """
            UPDATE local_operations
            SET state = 'cancelled', document_json = ?, resource_version = ?,
                finished_at = ?
            WHERE operation_id = ?
            """,
            (
                response_bytes,
                version,
                reconciled.finished_at,
                reconciled.operation_id,
            ),
        )
        for idempotency_row in idempotency_rows:
            updated = connection.execute(
                """
                UPDATE idempotency_records
                SET response_bytes = ?, cleanup_eligible = 1
                WHERE principal = ? AND method = ? AND route = ?
                  AND resource_scope = ? AND idempotency_key = ?
                  AND request_digest = ? AND response_type = 'LocalOperationV1'
                  AND operation_id = ? AND response_bytes = ?
                """,
                (
                    response_bytes,
                    idempotency_row["principal"],
                    idempotency_row["method"],
                    idempotency_row["route"],
                    idempotency_row["resource_scope"],
                    idempotency_row["idempotency_key"],
                    idempotency_row["request_digest"],
                    operation.operation_id,
                    previous_bytes,
                ),
            )
            if updated.rowcount != 1:
                raise ProviderDataCorruptionError(
                    "operation idempotency response changed during reconciliation"
                )
        return reconciled

    def _idempotency_rows_for_operation(
        self,
        connection: sqlite3.Connection,
        operation: LocalOperationV1,
        response_bytes: bytes,
    ) -> tuple[sqlite3.Row, ...]:
        cursor = connection.execute(
            """
            SELECT *
            FROM idempotency_records
            WHERE response_type = 'LocalOperationV1' AND operation_id = ?
            """,
            (operation.operation_id,),
        )
        rows = tuple(cast(sqlite3.Row, row) for row in cursor.fetchmany(2))
        if len(rows) != 1:
            raise ProviderDataCorruptionError(
                "live operation action authority must have exactly one idempotency record"
            )
        for row in rows:
            referenced = self._operation_for_idempotency_record(connection, row)
            if (
                referenced.operation_id != operation.operation_id
                or bytes(row["response_bytes"]) != response_bytes
            ):
                raise ProviderDataCorruptionError(
                    "operation idempotency response references another operation"
                )
        return rows

    def _reconcile_profile_operations(
        self,
        connection: sqlite3.Connection,
        profile_id: str,
    ) -> None:
        after_operation_id = ""
        while True:
            operation_ids = connection.execute(
                """
                SELECT operation_id
                FROM local_operations
                WHERE resource_type = 'profile' AND resource_id = ?
                  AND operation_id > ?
                ORDER BY operation_id
                LIMIT ?
                """,
                (profile_id, after_operation_id, STARTUP_OPERATION_BATCH_ROWS),
            ).fetchall()
            if not operation_ids:
                return
            for operation_id_row in operation_ids:
                operation_id = cast(str, operation_id_row["operation_id"])
                self._cancel_operation_with_authority(
                    connection,
                    self._require_operation_row(connection, operation_id),
                )
                after_operation_id = operation_id
            if len(operation_ids) < STARTUP_OPERATION_BATCH_ROWS:
                return

    def _reconcile_operations_at_startup(self, connection: sqlite3.Connection) -> None:
        after_operation_id = ""
        while True:
            metadata_rows = connection.execute(
                """
                SELECT operation_id,
                       length(CAST(document_json AS BLOB)) AS document_bytes,
                       length(CAST(operation_id AS BLOB))
                         + length(CAST(operation_kind AS BLOB))
                         + length(CAST(state AS BLOB))
                         + length(CAST(resource_type AS BLOB))
                         + length(CAST(resource_id AS BLOB))
                         + length(CAST(document_json AS BLOB))
                         + length(CAST(created_at AS BLOB))
                         + coalesce(length(CAST(finished_at AS BLOB)), 0)
                         + 16 AS row_bytes
                FROM local_operations
                WHERE operation_id > ?
                ORDER BY operation_id
                LIMIT ?
                """,
                (after_operation_id, STARTUP_OPERATION_BATCH_ROWS),
            ).fetchmany(STARTUP_OPERATION_BATCH_ROWS)
            if not metadata_rows:
                return
            for metadata in metadata_rows:
                operation_id = cast(str, metadata["operation_id"])
                document_bytes = metadata["document_bytes"]
                row_bytes = metadata["row_bytes"]
                if (
                    type(document_bytes) is not int
                    or document_bytes < 1
                    or document_bytes > MAX_DOCUMENT_BYTES
                    or type(row_bytes) is not int
                    or row_bytes > MAX_STARTUP_OPERATION_ROW_BYTES
                ):
                    raise ProviderDataCorruptionError(
                        "startup operation row exceeds its recovery bound"
                    )
                row = connection.execute(
                    """
                    SELECT * FROM local_operations
                    WHERE operation_id = ?
                      AND length(CAST(document_json AS BLOB)) = ?
                    """,
                    (operation_id, document_bytes),
                ).fetchone()
                if row is None:
                    raise ProviderDataCorruptionError(
                        "startup operation row changed during recovery"
                    )
                self._cancel_operation_with_authority(
                    connection,
                    cast(sqlite3.Row, row),
                )
                after_operation_id = operation_id
            if len(metadata_rows) < STARTUP_OPERATION_BATCH_ROWS:
                return

    def _require_etag(
        self,
        resource_type: str,
        resource_id: str,
        row: sqlite3.Row,
        if_match: str,
    ) -> None:
        current = self._etag(resource_type, resource_id, row["resource_version"])
        if not hmac.compare_digest(current, if_match):
            raise ETagConflictError(resource_type, resource_id, current)

    def _list_rows(
        self,
        *,
        resource: Literal["profiles", "projects"],
        table: Literal["remote_profiles", "projects"],
        id_column: Literal["profile_id", "project_id"],
        limit: int,
        after: str | None,
        sort: str,
        direction: _Direction,
        filters: Mapping[str, str] | None,
        filter_columns: Mapping[str, str],
    ) -> tuple[list[sqlite3.Row], str | None]:
        if type(limit) is not int or not 1 <= limit <= 100:
            raise ContractValidationError("pagination limit must be between 1 and 100")
        sort_columns = {"created_at": "created_at", "updated_at": "updated_at", "name": "name"}
        if sort not in sort_columns:
            raise ContractValidationError("unsupported provider sort key")
        if direction not in {"asc", "desc"}:
            raise ContractValidationError("pagination direction must be asc or desc")
        normalized_filters = self._normalize_filters(filters, filter_columns)
        anchor: tuple[str, str] | None = None
        if after is not None:
            anchor = self._decode_cursor(
                after,
                resource=resource,
                filters=normalized_filters,
                sort=sort,
                direction=direction,
            )

        clauses: list[str] = []
        parameters: list[object] = []
        for key, value in normalized_filters.items():
            clauses.append(f"{filter_columns[key]} = ?")
            parameters.append(value)
        sort_column = sort_columns[sort]
        sql_direction = "ASC" if direction == "asc" else "DESC"
        with self._transaction(write=False) as connection:
            if anchor is not None:
                anchor_value, anchor_id = anchor
                operator = ">" if direction == "asc" else "<"
                clauses.append(
                    f"({sort_column} {operator} ? OR "
                    f"({sort_column} = ? AND {id_column} {operator} ?))"
                )
                parameters.extend((anchor_value, anchor_value, anchor_id))
            where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
            parameters.append(limit + 1)
            selected_columns = self._project_select_columns() if table == "projects" else "*"
            fetched = connection.execute(
                f"SELECT {selected_columns} FROM {table}{where} "
                f"ORDER BY {sort_column} {sql_direction}, {id_column} {sql_direction} LIMIT ?",
                tuple(parameters),
            ).fetchall()
            if table == "projects":
                for row in fetched:
                    self._validate_remote_state_cell(cast(sqlite3.Row, row))
        has_more = len(fetched) > limit
        rows = list(fetched[:limit])
        next_cursor = None
        if has_more and rows:
            last = rows[-1]
            next_cursor = self._encode_cursor(
                resource=resource,
                filters=normalized_filters,
                sort=sort,
                direction=direction,
                anchor_id=last[id_column],
                sort_value=last[sort_column],
            )
        return rows, next_cursor

    @staticmethod
    def _normalize_filters(
        filters: Mapping[str, str] | None, supported: Mapping[str, str]
    ) -> dict[str, str]:
        if filters is None:
            return {}
        normalized: dict[str, str] = {}
        for key, value in filters.items():
            if key not in supported or type(value) is not str:
                raise ContractValidationError("unsupported provider pagination filter")
            if len(value.encode("utf-8")) > 256 or "\x00" in value:
                raise ContractValidationError("provider pagination filter exceeds its bounds")
            normalized[key] = value
        return dict(sorted(normalized.items()))

    def _encode_cursor(
        self,
        *,
        resource: str,
        filters: Mapping[str, str],
        sort: str,
        direction: _Direction,
        anchor_id: str,
        sort_value: str,
    ) -> str:
        query_digest = sha256(
            _canonical_json_bytes(
                {
                    "direction": direction,
                    "filters": dict(filters),
                    "resource": resource,
                    "sort": sort,
                }
            )
        ).hexdigest()
        self._validate_resource_id(anchor_id)
        if len(sort_value.encode("utf-8")) > 4096:
            raise ProviderStoreError("provider cursor sort boundary exceeds its byte bound")
        now_epoch = int(self._now().timestamp())
        expires_at_epoch = now_epoch + self._cursor_ttl_seconds
        try:
            token = _CURSOR_TOKEN.pack(
                _CURSOR_TOKEN_VERSION,
                now_epoch,
                expires_at_epoch,
                bytes.fromhex(query_digest),
                secrets.token_bytes(_CURSOR_NONCE_BYTES),
            )
        except (OverflowError, struct.error, ValueError) as exc:
            raise ProviderStoreError(
                "provider cursor timestamps are outside token bounds"
            ) from exc
        cursor = (
            f"{self._b64encode(token)}."
            f"{self._b64encode(hmac.digest(self._cursor_key, token, 'sha256'))}"
        )
        if len(cursor.encode("ascii")) > MAX_RENDERED_CURSOR_BYTES:
            raise ProviderStoreError("provider cursor exceeds its byte limit")
        with self._transaction(write=True) as connection:
            connection.execute(
                """
                DELETE FROM pagination_cursors
                WHERE cursor_digest IN (
                    SELECT cursor_digest
                    FROM pagination_cursors INDEXED BY pagination_cursors_expiry_idx
                    WHERE expires_at_epoch <= ?
                    ORDER BY expires_at_epoch
                    LIMIT ?
                )
                """,
                (now_epoch, NORMAL_WRITE_CLEANUP_ROWS),
            )
            _, count = self._provider_record_counts(connection)
            if count >= self._max_cursor_records:
                raise ProviderStoreError("live provider cursor capacity is exhausted")
            connection.execute(
                """
                INSERT INTO pagination_cursors(
                    cursor_digest, query_digest, anchor_id, anchor_value,
                    created_at_epoch, expires_at_epoch
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    sha256(cursor.encode("ascii")).hexdigest(),
                    query_digest,
                    anchor_id,
                    sort_value,
                    now_epoch,
                    expires_at_epoch,
                ),
            )
        return cursor

    def _decode_cursor(
        self,
        cursor: str,
        *,
        resource: str,
        filters: Mapping[str, str],
        sort: str,
        direction: _Direction,
    ) -> tuple[str, str]:
        if type(cursor) is not str or not 1 <= len(cursor.encode("utf-8")) <= MAX_CURSOR_BYTES:
            raise CursorInvalidError("provider cursor is outside its byte bounds")
        parts = cursor.split(".")
        if len(parts) != 2:
            raise CursorInvalidError("provider cursor has an invalid envelope")
        try:
            token = self._b64decode(parts[0])
            signature = self._b64decode(parts[1])
        except ValueError as exc:
            raise CursorInvalidError("provider cursor encoding is invalid") from exc
        if len(token) != _CURSOR_TOKEN.size or len(signature) != sha256().digest_size:
            raise CursorInvalidError("provider cursor has an invalid token size")
        if not hmac.compare_digest(signature, hmac.digest(self._cursor_key, token, "sha256")):
            raise CursorInvalidError("provider cursor signature is invalid")
        try:
            version, issued_at_epoch, expires_at_epoch, bound_query_digest, _nonce = (
                _CURSOR_TOKEN.unpack(token)
            )
        except struct.error as exc:
            raise CursorInvalidError("provider cursor token is malformed") from exc
        if version != _CURSOR_TOKEN_VERSION or expires_at_epoch <= issued_at_epoch:
            raise CursorInvalidError("provider cursor token structure is invalid")
        query_digest = sha256(
            _canonical_json_bytes(
                {
                    "direction": direction,
                    "filters": dict(filters),
                    "resource": resource,
                    "sort": sort,
                }
            )
        ).hexdigest()
        if not hmac.compare_digest(bound_query_digest, bytes.fromhex(query_digest)):
            raise CursorInvalidError("provider cursor is bound to another query")
        if expires_at_epoch <= int(self._now().timestamp()):
            raise CursorExpiredError("provider cursor has expired")
        with self._transaction(write=False) as connection:
            row = connection.execute(
                """
                SELECT query_digest, anchor_id, anchor_value,
                       created_at_epoch, expires_at_epoch
                FROM pagination_cursors WHERE cursor_digest = ?
                """,
                (sha256(cursor.encode("ascii")).hexdigest(),),
            ).fetchone()
        if row is None:
            raise CursorInvalidError("provider cursor is unknown")
        if (
            row["query_digest"] != query_digest
            or row["created_at_epoch"] != issued_at_epoch
            or row["expires_at_epoch"] != expires_at_epoch
        ):
            raise CursorInvalidError("provider cursor record differs from its signed token")
        try:
            self._validate_resource_id(cast(str, row["anchor_id"]))
        except ContractValidationError as exc:
            raise CursorInvalidError("provider cursor resource boundary is invalid") from exc
        if len(cast(str, row["anchor_value"]).encode("utf-8")) > 4096:
            raise CursorInvalidError("provider cursor sort boundary exceeds its byte bound")
        return cast(str, row["anchor_value"]), cast(str, row["anchor_id"])

    @staticmethod
    def _b64encode(value: bytes) -> str:
        return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")

    @staticmethod
    def _b64decode(value: str) -> bytes:
        if not value or _B64_RE.fullmatch(value) is None:
            raise ValueError("invalid base64url")
        padding = "=" * (-len(value) % 4)
        decoded = base64.b64decode(value + padding, altchars=b"-_", validate=True)
        if DesktopProviderStore._b64encode(decoded) != value:
            raise ValueError("non-canonical base64url")
        return decoded


__all__ = (
    "ContractValidationError",
    "CursorExpiredError",
    "CursorInvalidError",
    "DesktopProviderStore",
    "ETagConflictError",
    "IdempotencyCapacityError",
    "IdempotencyConflictError",
    "IdempotencyResult",
    "ProviderDataCorruptionError",
    "ProviderMutation",
    "ProjectRuntimeActionReservation",
    "ProviderSchemaError",
    "ProviderStateRootError",
    "ProviderStoreError",
    "ResourceInUseError",
    "ResourceNotFoundError",
)
