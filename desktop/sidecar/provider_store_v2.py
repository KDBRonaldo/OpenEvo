"""Isolated durable state for the Desktop Local API v2 provider.

The v2 store is intentionally small: it owns only local system-OpenSSH
profiles, non-connectable migration records, validated local project drafts,
and their idempotency/migration receipts.  Remote Core authority is never
persisted here.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from datetime import datetime, timezone
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
from typing import Literal, TypeVar, cast

from pydantic import BaseModel, ConfigDict, TypeAdapter, ValidationError, model_validator

from desktop.sidecar.contracts.v2 import models as m
from openevo.backend.contracts.v2.models import (
    ScienceProjectConfigV2,
    project_config_sha256_for,
)


STORE_NAMESPACE = "openevo.desktop.provider.v2"
SCHEMA_VERSION = 2
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
DEFAULT_MAX_PROFILES = 100
DEFAULT_MAX_DRAFTS = 100
DEFAULT_MAX_IDEMPOTENCY_RECORDS = 2_000
DEFAULT_MAX_MIGRATION_DIAGNOSTICS = 64

_TIMESTAMP_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{6}Z$")
_ETAG_RE = re.compile(r'^"[0-9a-f]{64}"$')
_DIGEST_RE = re.compile(r"^[0-9a-f]{64}$")
_PROFILE_ADAPTER = TypeAdapter(m.RemoteProfileV2)


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


class ProviderNotFoundV2(ProviderStoreV2Error):
    """A local v2 resource does not exist."""


class ProviderContractV2Error(ProviderStoreV2Error):
    """Caller input is not an exact closed v2 model."""


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
EXPECTED_SCHEMA_V1_SHA256 = "d2ae490ad5b98ca03548570a8d56a6a5ea349694ed647102a69eb5b69e3dac34"
EXPECTED_SCHEMA_V2_SHA256 = "7314032a52da83b70a43f36f161984bef8bf03274848bf62ab1963a039279c06"
if (
    _COMPUTED_SCHEMA_V1_SHA256 != EXPECTED_SCHEMA_V1_SHA256
    or _COMPUTED_SCHEMA_V2_SHA256 != EXPECTED_SCHEMA_V2_SHA256
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


def _migration_checkpoint(_stage: str) -> None:
    """Private crash-injection boundary used by durability tests."""


def _post_commit_checkpoint(_operation: str) -> None:
    """Private process-loss boundary after an authoritative commit."""


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
    ) -> None:
        self._require_secure_platform()
        for label, value in (
            ("max_profiles", max_profiles),
            ("max_drafts", max_drafts),
            ("max_idempotency_records", max_idempotency_records),
            ("max_migration_diagnostics", max_migration_diagnostics),
        ):
            if type(value) is not int or value < 1:
                raise ValueError(f"{label} must be a positive integer")
        if max_profiles > DEFAULT_MAX_PROFILES:
            raise ValueError("max_profiles exceeds the public v2 profile bound")

        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._max_profiles = max_profiles
        self._max_drafts = max_drafts
        self._max_idempotency_records = max_idempotency_records
        self._max_migration_diagnostics = max_migration_diagnostics
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
        return EXPECTED_SCHEMA_V2_SHA256

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
                for statement in (*_SCHEMA_V1_STATEMENTS, *_SCHEMA_V2_ADDITIONS):
                    connection.execute(statement)
                _migration_checkpoint("fresh_after_ddl")
                connection.execute(
                    "INSERT INTO schema_metadata VALUES (1, ?, 2, ?, ?)",
                    (STORE_NAMESPACE, EXPECTED_SCHEMA_V2_SHA256, timestamp),
                )
                connection.executemany(
                    "INSERT INTO schema_migrations(version, applied_at) VALUES (?, ?)",
                    ((1, timestamp), (2, timestamp)),
                )
                connection.execute("PRAGMA user_version = 2")
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
            elif version != SCHEMA_VERSION:
                raise ProviderSchemaV2Error(f"unsupported v2 provider schema version {version}")
            self._validate_schema_version(
                connection,
                expected_rows=_EXPECTED_SCHEMA_V2_ROWS,
                expected_sha256=EXPECTED_SCHEMA_V2_SHA256,
                version=2,
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
        with self._transaction(write=False, operation="recoverProviderV2") as connection:
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
        elif operation == "renameProfileV2":
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
    "DEFAULT_MAX_MIGRATION_DIAGNOSTICS",
    "DEFAULT_MAX_PROFILES",
    "DesktopProviderStoreV2",
    "EXPECTED_SCHEMA_V1_SHA256",
    "EXPECTED_SCHEMA_V2_SHA256",
    "LegacyDraftSourceV2",
    "LegacyProfileImportV2",
    "LocalProjectDraftV2",
    "MAX_DATABASE_BYTES",
    "MAX_PROFILE_DOCUMENT_BYTES",
    "MigrationDiagnosticV2",
    "ProviderCapacityConfigurationV2Error",
    "ProviderCapacityV2Error",
    "ProviderConflictV2",
    "ProviderContractV2Error",
    "ProviderDataV2Error",
    "ProviderIdempotencyConflictV2",
    "ProviderNotFoundV2",
    "ProviderPreconditionFailedV2",
    "ProviderSchemaV2Error",
    "ProviderStateV2Error",
    "ProviderStoreV2Error",
    "SCHEMA_VERSION",
    "STORE_NAMESPACE",
]
