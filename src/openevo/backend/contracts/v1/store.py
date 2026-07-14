from __future__ import annotations

import base64
from collections.abc import Callable, Mapping
import ctypes
from dataclasses import dataclass
from datetime import datetime, timezone
import errno
import fcntl
import hashlib
import hmac
import json
import os
from pathlib import Path
import secrets
import sqlite3
import stat
import sys
import threading
import time
from typing import Any, Literal, TypeVar

from pydantic import BaseModel, ValidationError

from . import models as m
from .workspace import (
    WorkspaceArchiveError,
    verify_and_materialize_workspace,
    verify_materialized_workspace,
)


_ModelT = TypeVar("_ModelT", bound=BaseModel)
_EMPTY_WORKSPACE_DIGEST = hashlib.sha256(b"\0" * 1024).hexdigest()
_IDEMPOTENCY_RETENTION_SECONDS = 7 * 24 * 60 * 60
_IDEMPOTENCY_LIMIT = 10_000
_CURSOR_TTL_SECONDS = 15 * 60
_MAX_DATABASE_BYTES = 256 * 1024 * 1024
_MAX_WAL_BYTES = 64 * 1024 * 1024
_MAX_MANAGED_WORKSPACE_BYTES = 4 * 1024 * 1024 * 1024
_MAX_STARTUP_ROWS = 128_000
_MAX_STARTUP_BLOB_BYTES = 128 * 1024 * 1024
_MAX_STARTUP_VALUE_BYTES = 16 * 1024 * 1024
_MAX_METADATA_BYTES = 4096
_MAX_SCHEMA_BYTES = 256 * 1024
_RECOVERY_PAGE_SIZE = 256
_MAX_PROJECTS = 10_000
_MAX_UPLOADS = 20_000
_MAX_PUBLICATION_OWNERS = _MAX_UPLOADS + _IDEMPOTENCY_LIMIT
_MAX_CLEANUP_INTENTS = _MAX_PROJECTS + (2 * _MAX_UPLOADS)
_MAX_RECOVERY_CLEANUP_NODES = 100_000
_MAX_RECOVERY_CLEANUP_NAME_BYTES = 16 * 1024 * 1024
_STORE_ID_BYTES = 32
_STORE_ID_HEX_LENGTH = _STORE_ID_BYTES * 2
_STORE_IDENTITY_MARKER = "provider.identity"
_MAX_STORE_IDENTITY_MARKER_BYTES = 512
_RENAME_NOREPLACE = 1
_IDEMPOTENCY_RESPONSE_MODELS: dict[str, type[BaseModel]] = {
    model.__name__: model
    for model in (
        m.ProjectV1,
        m.WorkspaceUploadSessionV1,
        m.WorkspaceUploadFinalizeResponseV1,
        m.ProjectValidationResponseV1,
    )
}
_IDEMPOTENCY_REQUEST_MODELS: dict[str, type[BaseModel] | None] = {
    "createCoreProjectV1": m.ProjectCreateV1,
    "patchCoreProjectV1": m.ProjectPatchV1,
    "deleteCoreProjectV1": None,
    "createCoreWorkspaceUploadV1": m.WorkspaceUploadCreateV1,
    "putCoreWorkspaceUploadChunkV1": m.WorkspaceUploadChunkV1,
    "finalizeCoreWorkspaceUploadV1": m.WorkspaceUploadFinalizeV1,
    "abortCoreWorkspaceUploadV1": m.WorkspaceUploadAbortV1,
    "validateCoreProjectV1": m.ProjectValidationRequestV1,
}
_IDEMPOTENCY_OPERATION_SPECS: dict[
    str, tuple[int, str, Literal["global", "project", "upload"]]
] = {
    "createCoreProjectV1": (201, "ProjectV1", "global"),
    "patchCoreProjectV1": (200, "ProjectV1", "project"),
    "deleteCoreProjectV1": (204, "NoContent", "project"),
    "createCoreWorkspaceUploadV1": (201, "WorkspaceUploadSessionV1", "project"),
    "putCoreWorkspaceUploadChunkV1": (200, "WorkspaceUploadSessionV1", "upload"),
    "finalizeCoreWorkspaceUploadV1": (
        201,
        "WorkspaceUploadFinalizeResponseV1",
        "upload",
    ),
    "abortCoreWorkspaceUploadV1": (200, "WorkspaceUploadSessionV1", "upload"),
    "validateCoreProjectV1": (200, "ProjectValidationResponseV1", "project"),
}


class CoreControlStoreError(Exception):
    pass


class StoreCorruptionError(CoreControlStoreError):
    pass


class PostCommitStoreError(StoreCorruptionError):
    """A committed transaction failed its final lifecycle verification."""


class CommitOutcomeUnknownError(PostCommitStoreError):
    """SQLite may have committed, so durable state must be reconciled before repair."""


class _DatabaseAttachRaceError(StoreCorruptionError):
    """The held database inode moved while SQLite resolved its descriptor path."""


class ResourceNotFoundError(CoreControlStoreError):
    def __init__(self, resource_type: str, resource_id: str) -> None:
        super().__init__(f"{resource_type} resource was not found")
        self.resource_type = resource_type
        self.resource_id = resource_id


class ETagPreconditionError(CoreControlStoreError):
    def __init__(self, resource_type: str) -> None:
        super().__init__(f"{resource_type} ETag precondition failed")
        self.resource_type = resource_type


class IdempotencyConflictError(CoreControlStoreError):
    pass


class IdempotencyCapacityError(CoreControlStoreError):
    pass


class ResourceConflictError(CoreControlStoreError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class CursorInvalidError(CoreControlStoreError):
    pass


class CursorExpiredError(CoreControlStoreError):
    pass


class EventCursorInvalidError(CoreControlStoreError):
    pass


class EventCursorExpiredError(CoreControlStoreError):
    pass


@dataclass(frozen=True)
class StoredResult:
    status_code: int
    model: BaseModel | None
    etag: str | None = None
    replayed: bool = False


@dataclass(frozen=True)
class _IdempotencyResourceScope:
    project_id: str | None
    upload_id: str | None


@dataclass(frozen=True)
class _IdempotencyRequestEnvelope:
    digest: str
    request_json: bytes
    semantic_headers_json: bytes


@dataclass(frozen=True)
class _WorkspaceChunkCommitExpectation:
    operation_id: str
    scope: str
    idempotency_key: str
    request_digest: str
    request_json: bytes
    semantic_headers_json: bytes
    project_id: str
    upload_id: str
    file_name: str
    created_at: str
    old_document_json: bytes
    old_resource_version: int
    old_updated_at: str
    new_document_json: bytes
    new_resource_version: int
    new_updated_at: str
    old_offset: int
    content: bytes
    result: StoredResult


@dataclass
class _ManagedCleanupBudget:
    nodes_remaining: int
    name_bytes_remaining: int

    def consume(self, name: str) -> bool:
        encoded_size = len(os.fsencode(name))
        if self.nodes_remaining < 1 or self.name_bytes_remaining < encoded_size:
            return False
        self.nodes_remaining -= 1
        self.name_bytes_remaining -= encoded_size
        return True


_LEGACY_SCHEMA = (
    """
    CREATE TABLE IF NOT EXISTS metadata (
        key TEXT PRIMARY KEY,
        value BLOB NOT NULL
    ) STRICT
    """,
    """
    CREATE TABLE IF NOT EXISTS projects (
        project_id TEXT PRIMARY KEY,
        identity_hmac TEXT NOT NULL,
        document_json BLOB NOT NULL,
        resource_version INTEGER NOT NULL CHECK (resource_version >= 1),
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    ) STRICT
    """,
    """
    CREATE TABLE IF NOT EXISTS workspace_uploads (
        upload_id TEXT PRIMARY KEY,
        project_id TEXT NOT NULL,
        identity_hmac TEXT NOT NULL,
        document_json BLOB NOT NULL,
        resource_version INTEGER NOT NULL CHECK (resource_version >= 1),
        file_name TEXT NOT NULL,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        FOREIGN KEY (project_id) REFERENCES projects(project_id) ON DELETE CASCADE
    ) STRICT
    """,
    """
    CREATE TABLE IF NOT EXISTS workspace_publication_owners (
        snapshot_id TEXT PRIMARY KEY,
        content_id TEXT NOT NULL UNIQUE,
        publication_sha256 TEXT NOT NULL UNIQUE,
        project_id TEXT NOT NULL,
        upload_id TEXT NOT NULL UNIQUE,
        identity_hmac TEXT NOT NULL,
        published_at TEXT NOT NULL
    ) STRICT
    """,
    """
    CREATE TABLE IF NOT EXISTS idempotency_records (
        operation_id TEXT NOT NULL,
        resource_scope TEXT NOT NULL,
        idempotency_key TEXT NOT NULL,
        request_digest TEXT NOT NULL,
        request_json BLOB NOT NULL,
        semantic_headers_json BLOB NOT NULL,
        status_code INTEGER NOT NULL,
        response_type TEXT NOT NULL,
        response_json BLOB,
        etag TEXT,
        created_at_epoch INTEGER NOT NULL,
        expires_at_epoch INTEGER NOT NULL,
        PRIMARY KEY (operation_id, resource_scope, idempotency_key)
    ) STRICT
    """,
    """
    CREATE TABLE IF NOT EXISTS managed_cleanup_intents (
        cleanup_id TEXT PRIMARY KEY,
        operation_id TEXT NOT NULL,
        resource_scope TEXT NOT NULL,
        idempotency_key TEXT NOT NULL,
        root_kind TEXT NOT NULL CHECK (root_kind IN ('upload', 'workspace')),
        entry_name TEXT NOT NULL,
        expected_dev INTEGER NOT NULL CHECK (expected_dev >= 0),
        expected_ino INTEGER NOT NULL CHECK (expected_ino > 0),
        expected_mode INTEGER NOT NULL CHECK (expected_mode > 0),
        expected_uid INTEGER NOT NULL CHECK (expected_uid >= 0),
        expected_nlink INTEGER NOT NULL CHECK (expected_nlink > 0),
        identity_hmac TEXT NOT NULL,
        created_at_epoch INTEGER NOT NULL,
        UNIQUE (
            operation_id,
            resource_scope,
            idempotency_key,
            root_kind,
            entry_name
        ),
        FOREIGN KEY (operation_id, resource_scope, idempotency_key)
            REFERENCES idempotency_records(
                operation_id,
                resource_scope,
                idempotency_key
            ) ON DELETE RESTRICT DEFERRABLE INITIALLY DEFERRED
    ) STRICT
    """,
    """
    CREATE TABLE IF NOT EXISTS failed_idempotency_records (
        operation_id TEXT NOT NULL,
        resource_scope TEXT NOT NULL,
        idempotency_key TEXT NOT NULL,
        request_digest TEXT NOT NULL,
        error_json BLOB NOT NULL,
        created_at_epoch INTEGER NOT NULL,
        expires_at_epoch INTEGER NOT NULL,
        PRIMARY KEY (operation_id, resource_scope, idempotency_key)
    ) STRICT
    """,
    """
    CREATE TABLE IF NOT EXISTS events (
        sequence INTEGER PRIMARY KEY AUTOINCREMENT,
        event_id TEXT UNIQUE,
        frame_json BLOB,
        created_at_epoch INTEGER NOT NULL
    ) STRICT
    """,
)

_STORE_IDENTITY_SCHEMA = """
    CREATE TABLE IF NOT EXISTS store_identity (
        singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
        store_id TEXT NOT NULL UNIQUE,
        binding_state TEXT NOT NULL CHECK (binding_state IN ('pending', 'bound')),
        root_dev INTEGER NOT NULL CHECK (root_dev >= 0),
        root_ino INTEGER NOT NULL CHECK (root_ino > 0),
        marker_dev INTEGER CHECK (marker_dev IS NULL OR marker_dev >= 0),
        marker_ino INTEGER CHECK (marker_ino IS NULL OR marker_ino > 0),
        CHECK (
            (binding_state = 'pending' AND marker_dev IS NULL AND marker_ino IS NULL)
            OR
            (binding_state = 'bound' AND marker_dev IS NOT NULL AND marker_ino IS NOT NULL)
        )
    ) STRICT
"""
_SCHEMA = (_STORE_IDENTITY_SCHEMA, *_LEGACY_SCHEMA)


class CoreControlStoreV1:
    """Single-owner durable state for the first Core Control v1 provider slice."""

    def __init__(
        self,
        state_root: str | Path,
        *,
        event_replay_limit: int = 10_000,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if not 1 <= event_replay_limit <= 10_000:
            raise ValueError("event replay limit must be between 1 and 10000")
        self._state_parent = Path(state_root).expanduser().resolve()
        self.root = self._state_parent / "core-control-v1"
        self.upload_root = self.root / "workspace-uploads"
        self.workspace_root = self.root / "workspace-snapshots"
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._event_replay_limit = event_replay_limit
        self._mutex = threading.RLock()
        self._closed = False
        self._database_fds: dict[str, int] = {}
        self._database_identities: dict[str, os.stat_result] = {}
        try:
            self._open_parent_anchor()
            self._prepare_root()
            self._open_lifecycle_storage()
            self._prepare_database_authority()
            self._connection = self._open_database_connection()
            self._initialize_or_verify_store_identity()
            self._prepare_managed_roots()
            self._configure_database_connection()
            with self._transaction():
                self._verify_schema_fingerprint()
                self._signing_key = self._load_or_create_signing_key()
            self._bind_database_sidecars()
            self._verify_database_integrity()
            self._recover_and_validate()
        except OSError as exc:
            connection = getattr(self, "_connection", None)
            if connection is not None:
                connection.close()
            self._close_lifecycle_storage()
            self._closed = True
            raise CoreControlStoreError(
                "Core Control provider startup filesystem is unavailable"
            ) from exc
        except sqlite3.Error as exc:
            connection = getattr(self, "_connection", None)
            if connection is not None:
                connection.close()
            self._close_lifecycle_storage()
            self._closed = True
            raise CoreControlStoreError(
                "Core Control provider startup database is unavailable"
            ) from exc
        except Exception:
            connection = getattr(self, "_connection", None)
            if connection is not None:
                connection.close()
            self._close_lifecycle_storage()
            self._closed = True
            raise

    def close(self) -> None:
        with self._mutex:
            if self._closed:
                return
            self._connection.close()
            self._close_lifecycle_storage()
            self._closed = True

    def create_project(
        self,
        request: m.ProjectCreateV1,
        *,
        idempotency_key: str,
        registry_digest: str | None,
    ) -> StoredResult:
        envelope = _idempotency_envelope("createCoreProjectV1", "projects", request, {})
        with self._mutex, self._transaction():
            replay = self._idempotency_replay(
                "createCoreProjectV1",
                "projects",
                idempotency_key,
                envelope,
                m.ProjectV1,
            )
            if replay is not None:
                return replay
            project_count = int(
                self._connection.execute("SELECT COUNT(*) FROM projects").fetchone()[0]
            )
            if project_count >= _MAX_PROJECTS:
                raise IdempotencyCapacityError("project capacity is exhausted")
            now = self._timestamp()
            project = self._new_project(request, now=now, registry_digest=registry_digest)
            self._connection.execute(
                "INSERT INTO projects(project_id, identity_hmac, document_json, "
                "resource_version, created_at, updated_at) VALUES (?, ?, ?, 1, ?, ?)",
                (
                    project.id,
                    self._resource_identity_hmac("project", project.id),
                    _model_bytes(project),
                    now,
                    now,
                ),
            )
            self._append_project_event(project, now=now)
            result = StoredResult(201, project, project.etag)
            self._store_idempotency(
                "createCoreProjectV1", "projects", idempotency_key, envelope, result
            )
            return result

    def get_project(self, project_id: str) -> m.ProjectV1:
        with self._mutex:
            self._verify_lifecycle_storage()
            row = self._connection.execute(
                "SELECT document_json FROM projects WHERE project_id = ?", (project_id,)
            ).fetchone()
            if row is None:
                raise ResourceNotFoundError("project", project_id)
            return _validate_bytes(m.ProjectV1, row["document_json"])

    def list_projects(
        self,
        *,
        limit: int,
        after: str | None,
        sort: Literal["created_at", "updated_at", "name"],
        direction: Literal["asc", "desc"],
    ) -> m.ProjectPageV1:
        with self._mutex:
            self._verify_lifecycle_storage()
            query_binding = f"projects:{sort}:{direction}"
            boundary: tuple[str, str] | None = None
            if after is not None:
                boundary = self._decode_cursor(after, query_binding)
            sort_expression = {
                "created_at": "created_at",
                "updated_at": "updated_at",
                "name": "json_extract(CAST(document_json AS TEXT), '$.name')",
            }[sort]
            comparison = ">" if direction == "asc" else "<"
            order = "ASC" if direction == "asc" else "DESC"
            parameters: list[object] = []
            where = ""
            if boundary is not None:
                where = (
                    f"WHERE ({sort_expression} {comparison} ? OR "
                    f"({sort_expression} = ? AND project_id {comparison} ?))"
                )
                parameters.extend((boundary[0], boundary[0], boundary[1]))
            parameters.append(limit + 1)
            cursor = self._connection.execute(
                f"SELECT document_json FROM projects {where} "
                f"ORDER BY {sort_expression} {order}, project_id {order} LIMIT ?",
                parameters,
            )
            rows = cursor.fetchmany(limit + 1)
            if cursor.fetchone() is not None:
                raise StoreCorruptionError("project page query exceeded its bound")
            selected = [_validate_bytes(m.ProjectV1, row["document_json"]) for row in rows]
        has_more = len(selected) > limit
        selected = selected[:limit]
        next_cursor = None
        if has_more and selected:
            final = selected[-1]
            next_cursor = self._encode_cursor(query_binding, (str(getattr(final, sort)), final.id))
        summaries = [
            m.ProjectSummaryV1.model_validate(
                item.model_dump(mode="python", exclude={"spec", "task", "workspace"})
            )
            for item in selected
        ]
        with self._mutex:
            self._verify_lifecycle_storage()
            return m.ProjectPageV1(items=summaries, next_cursor=next_cursor, has_more=has_more)

    def patch_project(
        self,
        project_id: str,
        request: m.ProjectPatchV1,
        *,
        if_match: str,
        idempotency_key: str,
        registry_digest: str | None,
    ) -> StoredResult:
        headers = {"if-match": if_match}
        envelope = _idempotency_envelope("patchCoreProjectV1", project_id, request, headers)
        with self._mutex, self._transaction():
            replay = self._idempotency_replay(
                "patchCoreProjectV1", project_id, idempotency_key, envelope, m.ProjectV1
            )
            if replay is not None:
                return replay
            row, current = self._project_row(project_id)
            self._require_etag(current.etag, if_match, "project")
            now = self._timestamp()
            updated = self._patched_project(
                current,
                request,
                now=now,
                version=int(row["resource_version"]) + 1,
                registry_digest=registry_digest,
            )
            self._connection.execute(
                "UPDATE projects SET document_json = ?, resource_version = ?, updated_at = ? "
                "WHERE project_id = ?",
                (_model_bytes(updated), int(row["resource_version"]) + 1, now, project_id),
            )
            self._append_project_event(updated, now=now)
            result = StoredResult(200, updated, updated.etag)
            self._store_idempotency(
                "patchCoreProjectV1", project_id, idempotency_key, envelope, result
            )
            return result

    def delete_project(
        self,
        project_id: str,
        *,
        if_match: str,
        idempotency_key: str,
    ) -> StoredResult:
        envelope = _idempotency_envelope(
            "deleteCoreProjectV1", project_id, None, {"if-match": if_match}
        )
        operation_id = "deleteCoreProjectV1"
        with self._mutex:
            with self._transaction():
                replay = self._idempotency_replay(
                    operation_id, project_id, idempotency_key, envelope, None
                )
                if replay is None:
                    _, current = self._project_row(project_id)
                    self._require_etag(current.etag, if_match, "project")
                    upload_cursor = self._connection.execute(
                        "SELECT upload_id, file_name, document_json FROM workspace_uploads "
                        "WHERE project_id = ? ORDER BY upload_id LIMIT ?",
                        (project_id, _MAX_UPLOADS + 1),
                    )
                    upload_rows = upload_cursor.fetchmany(_MAX_UPLOADS + 1)
                    if len(upload_rows) > _MAX_UPLOADS or upload_cursor.fetchone() is not None:
                        raise StoreCorruptionError("project upload cleanup quota is exceeded")

                    cleanup_entries: dict[
                        tuple[Literal["upload", "workspace"], str], os.stat_result
                    ] = {}
                    for upload_row in upload_rows:
                        upload = _validate_bytes(
                            m.WorkspaceUploadSessionV1,
                            upload_row["document_json"],
                        )
                        if (
                            upload.id != upload_row["upload_id"]
                            or upload.project_id != project_id
                            or upload_row["file_name"] != f"{upload.id}.part"
                        ):
                            raise StoreCorruptionError(
                                "project upload cleanup identity is invalid"
                            )
                        upload_identity = _managed_entry_identity(
                            self._upload_root_fd,
                            upload_row["file_name"],
                            expected_type="file",
                            required=upload.status
                            in {
                                m.WorkspaceUploadStatus.OPEN,
                                m.WorkspaceUploadStatus.FINALIZED,
                            },
                        )
                        if upload_identity is not None:
                            cleanup_entries[("upload", upload_row["file_name"])] = upload_identity
                        if upload.publication is not None:
                            self._validate_publication_identity(
                                upload.publication,
                                project_id=project_id,
                                upload_id=upload.id,
                            )
                            snapshot_name = upload.publication.workspace_snapshot.id
                            snapshot_identity = _managed_entry_identity(
                                self._workspace_root_fd,
                                snapshot_name,
                                expected_type="directory",
                                required=True,
                            )
                            assert snapshot_identity is not None
                            existing = cleanup_entries.setdefault(
                                ("workspace", snapshot_name), snapshot_identity
                            )
                            if not _same_identity(existing, snapshot_identity):
                                raise StoreCorruptionError(
                                    "project workspace cleanup identity is ambiguous"
                                )

                    self._connection.execute(
                        "DELETE FROM projects WHERE project_id = ?", (project_id,)
                    )
                    result = StoredResult(204, None)
                    self._store_idempotency(
                        operation_id,
                        project_id,
                        idempotency_key,
                        envelope,
                        result,
                    )
                    for (root_kind, entry_name), identity in cleanup_entries.items():
                        self._store_cleanup_intent(
                            operation_id=operation_id,
                            scope=project_id,
                            idempotency_key=idempotency_key,
                            root_kind=root_kind,
                            entry_name=entry_name,
                            identity=identity,
                        )
                else:
                    result = replay
            self._reconcile_cleanup_operation(
                operation_id,
                project_id,
                idempotency_key,
                error_message="committed project deletion could not reconcile managed state",
            )
            return result

    def create_upload(
        self,
        project_id: str,
        request: m.WorkspaceUploadCreateV1,
        *,
        if_match: str,
        idempotency_key: str,
    ) -> StoredResult:
        envelope = _idempotency_envelope(
            "createCoreWorkspaceUploadV1",
            project_id,
            request,
            {"if-match": if_match},
        )
        with self._mutex:
            transaction = self._transaction()
            file_fd: int | None = None
            file_name: str | None = None
            file_identity: os.stat_result | None = None
            try:
                with transaction:
                    replay = self._idempotency_replay(
                        "createCoreWorkspaceUploadV1",
                        project_id,
                        idempotency_key,
                        envelope,
                        m.WorkspaceUploadSessionV1,
                    )
                    if replay is not None:
                        return replay
                    upload_count = int(
                        self._connection.execute(
                            "SELECT COUNT(*) FROM workspace_uploads"
                        ).fetchone()[0]
                    )
                    reserved_bytes = int(
                        self._connection.execute(
                            "SELECT COALESCE(SUM(CAST(json_extract("
                            "CAST(document_json AS TEXT), '$.archive.byte_size') AS INTEGER)), "
                            "0) FROM workspace_uploads WHERE json_extract("
                            "CAST(document_json AS TEXT), '$.status') IN ('open', 'finalized')"
                        ).fetchone()[0]
                    )
                    if upload_count >= _MAX_UPLOADS:
                        raise IdempotencyCapacityError("workspace upload capacity is exhausted")
                    if reserved_bytes + request.archive.byte_size > _MAX_MANAGED_WORKSPACE_BYTES:
                        raise ResourceConflictError(
                            "provider_storage_quota_exceeded",
                            "The managed workspace storage quota would be exceeded.",
                        )
                    _, project = self._project_row(project_id)
                    self._require_etag(project.etag, if_match, "project")
                    if request.project_snapshot != project.current_project_snapshot:
                        raise ResourceConflictError(
                            "project_snapshot_changed",
                            "The upload project snapshot is not the current project snapshot.",
                        )
                    if not isinstance(project.workspace, m.ImportedWorkspaceSpecV1):
                        raise ResourceConflictError(
                            "workspace_upload_not_required",
                            "Scratch projects do not accept workspace archive uploads.",
                        )
                    if request.archive != project.workspace.archive:
                        raise ResourceConflictError(
                            "workspace_archive_declaration_changed",
                            "The upload archive differs from the project workspace declaration.",
                        )
                    if request.base_workspace_snapshot != project.current_workspace_snapshot:
                        raise ResourceConflictError(
                            "workspace_base_snapshot_changed",
                            "The upload base workspace snapshot is no longer current.",
                        )
                    now = self._timestamp()
                    upload_id = _new_id("upload")
                    file_name = f"{upload_id}.part"
                    file_fd = os.open(
                        file_name,
                        os.O_RDWR | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
                        0o600,
                        dir_fd=self._upload_root_fd,
                    )
                    file_identity = os.fstat(file_fd)
                    _require_private_regular_metadata(file_identity)
                    _require_entry_binding(self._upload_root_fd, file_name, file_identity)
                    os.fsync(file_fd)
                    os.fsync(self._upload_root_fd)
                    session_data = {
                        "id": upload_id,
                        "project_id": project_id,
                        "status": m.WorkspaceUploadStatus.OPEN,
                        "accepted_offset": 0,
                        "project_snapshot": request.project_snapshot,
                        "project_etag": project.etag,
                        "archive": request.archive,
                        "base_workspace_snapshot": request.base_workspace_snapshot,
                        "publication": None,
                        "created_at": now,
                        "updated_at": now,
                    }
                    session = _model_with_etag(m.WorkspaceUploadSessionV1, session_data, version=1)
                    self._connection.execute(
                        "INSERT INTO workspace_uploads(upload_id, project_id, identity_hmac, "
                        "document_json, resource_version, file_name, created_at, updated_at) "
                        "VALUES (?, ?, ?, ?, 1, ?, ?, ?)",
                        (
                            upload_id,
                            project_id,
                            self._resource_identity_hmac("upload", upload_id),
                            _model_bytes(session),
                            file_name,
                            now,
                            now,
                        ),
                    )
                    result = StoredResult(201, session, session.etag)
                    self._store_idempotency(
                        "createCoreWorkspaceUploadV1",
                        project_id,
                        idempotency_key,
                        envelope,
                        result,
                    )
                    return result
            except BaseException:
                if (
                    transaction.outcome == "rolled_back"
                    and file_fd is not None
                    and file_name is not None
                    and file_identity is not None
                ):
                    _remove_bound_entry_at(
                        self._upload_root_fd,
                        file_name,
                        file_fd,
                        file_identity,
                        root_kind="upload",
                    )
                    os.fsync(self._upload_root_fd)
                raise
            finally:
                if file_fd is not None:
                    os.close(file_fd)

    def get_upload(self, project_id: str, upload_id: str) -> m.WorkspaceUploadSessionV1:
        with self._mutex:
            _, upload = self._upload_row(project_id, upload_id)
            return upload

    def put_upload_chunk(
        self,
        project_id: str,
        upload_id: str,
        request: m.WorkspaceUploadChunkV1,
        *,
        if_match: str,
        idempotency_key: str,
    ) -> StoredResult:
        scope = f"{project_id}:{upload_id}"
        envelope = _idempotency_envelope(
            "putCoreWorkspaceUploadChunkV1", scope, request, {"if-match": if_match}
        )
        content = base64.b64decode(request.content_base64, validate=True)
        with self._mutex:
            old_offset = 0
            file_fd: int | None = None
            expectation: _WorkspaceChunkCommitExpectation | None = None
            transaction = self._transaction()
            try:
                with transaction:
                    replay = self._idempotency_replay(
                        "putCoreWorkspaceUploadChunkV1",
                        scope,
                        idempotency_key,
                        envelope,
                        m.WorkspaceUploadSessionV1,
                    )
                    if replay is not None:
                        return replay
                    row, upload = self._upload_row(project_id, upload_id)
                    self._require_etag(upload.etag, if_match, "workspace_upload")
                    if upload.status is not m.WorkspaceUploadStatus.OPEN:
                        raise ResourceConflictError(
                            "workspace_upload_not_open", "The workspace upload is not open."
                        )
                    if request.offset != upload.accepted_offset:
                        raise ResourceConflictError(
                            "workspace_chunk_out_of_order",
                            "The workspace chunk offset is not the current accepted offset.",
                        )
                    if request.offset + request.byte_length > upload.archive.byte_size:
                        raise ResourceConflictError(
                            "workspace_chunk_exceeds_declaration",
                            "The workspace chunk exceeds the declared archive size.",
                        )
                    old_offset = upload.accepted_offset
                    file_fd = os.open(
                        row["file_name"],
                        os.O_RDWR | getattr(os, "O_NOFOLLOW", 0),
                        dir_fd=self._upload_root_fd,
                    )
                    _require_bound_regular_entry(
                        self._upload_root_fd,
                        row["file_name"],
                        file_fd,
                        expected_size=old_offset,
                    )
                    os.lseek(file_fd, old_offset, os.SEEK_SET)
                    _write_all(file_fd, content)
                    os.fsync(file_fd)
                    _require_bound_regular_entry(
                        self._upload_root_fd,
                        row["file_name"],
                        file_fd,
                        expected_size=old_offset + len(content),
                    )
                    now = self._timestamp()
                    version = int(row["resource_version"]) + 1
                    updated_data = upload.model_dump(mode="python", exclude={"etag"})
                    updated_data.update(accepted_offset=old_offset + len(content), updated_at=now)
                    updated = _model_with_etag(
                        m.WorkspaceUploadSessionV1,
                        updated_data,
                        version=version,
                    )
                    updated_bytes = _model_bytes(updated)
                    result = StoredResult(200, updated, updated.etag)
                    expectation = _WorkspaceChunkCommitExpectation(
                        operation_id="putCoreWorkspaceUploadChunkV1",
                        scope=scope,
                        idempotency_key=idempotency_key,
                        request_digest=envelope.digest,
                        request_json=envelope.request_json,
                        semantic_headers_json=envelope.semantic_headers_json,
                        project_id=project_id,
                        upload_id=upload_id,
                        file_name=row["file_name"],
                        created_at=row["created_at"],
                        old_document_json=bytes(row["document_json"]),
                        old_resource_version=int(row["resource_version"]),
                        old_updated_at=row["updated_at"],
                        new_document_json=updated_bytes,
                        new_resource_version=version,
                        new_updated_at=now,
                        old_offset=old_offset,
                        content=content,
                        result=result,
                    )
                    self._connection.execute(
                        "UPDATE workspace_uploads SET document_json = ?, resource_version = ?, "
                        "updated_at = ? WHERE upload_id = ?",
                        (updated_bytes, version, now, upload_id),
                    )
                    self._store_idempotency(
                        "putCoreWorkspaceUploadChunkV1",
                        scope,
                        idempotency_key,
                        envelope,
                        result,
                    )
                    return result
            except CommitOutcomeUnknownError as exc:
                if file_fd is None or expectation is None:
                    raise
                if self._reconcile_unknown_workspace_chunk_commit(expectation, file_fd):
                    return expectation.result
                os.ftruncate(file_fd, old_offset)
                os.fsync(file_fd)
                _require_bound_regular_entry(
                    self._upload_root_fd,
                    expectation.file_name,
                    file_fd,
                    expected_size=old_offset,
                )
                raise CoreControlStoreError("Core Control transaction did not commit") from exc
            except Exception:
                if file_fd is not None and transaction.outcome in {"pending", "rolled_back"}:
                    os.ftruncate(file_fd, old_offset)
                    os.fsync(file_fd)
                raise
            finally:
                if file_fd is not None:
                    os.close(file_fd)

    def finalize_upload(
        self,
        project_id: str,
        upload_id: str,
        request: m.WorkspaceUploadFinalizeV1,
        *,
        if_match: str,
        if_project_match: str,
        idempotency_key: str,
    ) -> StoredResult:
        scope = f"{project_id}:{upload_id}"
        envelope = _idempotency_envelope(
            "finalizeCoreWorkspaceUploadV1",
            scope,
            request,
            {"if-match": if_match, "if-project-match": if_project_match},
        )
        with self._mutex:
            with self._transaction():
                replay = self._idempotency_replay(
                    "finalizeCoreWorkspaceUploadV1",
                    scope,
                    idempotency_key,
                    envelope,
                    m.WorkspaceUploadFinalizeResponseV1,
                )
                if replay is not None:
                    return replay
                upload_row, upload = self._upload_row(project_id, upload_id)
                project_row, project = self._project_row(project_id)
                self._validate_finalize_preconditions(
                    upload,
                    project,
                    request,
                    if_match=if_match,
                    if_project_match=if_project_match,
                )
                archive_name = upload_row["file_name"]

            now = self._timestamp()
            workspace_snapshot = _workspace_publication_snapshot(
                self._signing_key,
                project_id=project_id,
                upload_id=upload_id,
                archive_sha256=upload.archive.content_sha256,
                now=now,
            )
            snapshot_fd: int | None = None
            snapshot_identity: os.stat_result | None = None
            try:
                self._verify_lifecycle_storage()
                verify_and_materialize_workspace(
                    self.upload_root / archive_name,
                    upload.archive,
                    archive_root_fd=self._upload_root_fd,
                    archive_name=archive_name,
                    workspace_root_fd=self._workspace_root_fd,
                    snapshot_name=workspace_snapshot.id,
                )
                snapshot_fd = os.open(
                    workspace_snapshot.id,
                    os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0),
                    dir_fd=self._workspace_root_fd,
                )
                snapshot_identity = os.fstat(snapshot_fd)
                _require_private_directory_metadata(snapshot_identity)
                _require_entry_binding(
                    self._workspace_root_fd,
                    workspace_snapshot.id,
                    snapshot_identity,
                )
                self._verify_lifecycle_storage()
            except WorkspaceArchiveError as exc:
                raise ResourceConflictError(
                    "workspace_archive_invalid", "The workspace archive is not canonical."
                ) from exc
            except OSError as exc:
                raise StoreCorruptionError(
                    "published workspace snapshot could not be bound"
                ) from exc

            transaction = self._transaction()
            try:
                with transaction:
                    upload_row, upload = self._upload_row(project_id, upload_id)
                    project_row, project = self._project_row(project_id)
                    self._validate_finalize_preconditions(
                        upload,
                        project,
                        request,
                        if_match=if_match,
                        if_project_match=if_project_match,
                    )
                    content_ref = m.ContentRefV1(
                        content_id=_core_owned_id(
                            self._signing_key,
                            "workspace-content",
                            {
                                "project_id": project_id,
                                "upload_id": upload_id,
                                "sha256": upload.archive.content_sha256,
                                "byte_size": upload.archive.byte_size,
                                "published_at": now,
                            },
                        ),
                        sha256=upload.archive.content_sha256,
                        byte_size=upload.archive.byte_size,
                    )
                    publication = m.WorkspacePublicationV1(
                        archive=upload.archive,
                        content_ref=content_ref,
                        workspace_snapshot=workspace_snapshot,
                        published_at=now,
                    )
                    self._store_publication_owner(
                        project_id=project_id,
                        upload_id=upload_id,
                        publication=publication,
                    )
                    project_version = int(project_row["resource_version"]) + 1
                    project_data = project.model_dump(
                        mode="python", exclude={"etag", "current_project_snapshot"}
                    )
                    project_data.update(
                        current_workspace_snapshot=workspace_snapshot,
                        workspace_publication=publication,
                        updated_at=now,
                    )
                    project_snapshot = _snapshot(
                        self._signing_key,
                        m.SnapshotKind.PROJECT,
                        _project_snapshot_payload(project_data),
                        now,
                    )
                    project_data["current_project_snapshot"] = project_snapshot
                    updated_project = _model_with_etag(
                        m.ProjectV1,
                        project_data,
                        version=project_version,
                    )
                    upload_version = int(upload_row["resource_version"]) + 1
                    upload_data = upload.model_dump(mode="python", exclude={"etag"})
                    upload_data.update(
                        status=m.WorkspaceUploadStatus.FINALIZED,
                        publication=publication,
                        updated_at=now,
                    )
                    updated_upload = _model_with_etag(
                        m.WorkspaceUploadSessionV1,
                        upload_data,
                        version=upload_version,
                    )
                    self._connection.execute(
                        "UPDATE projects SET document_json = ?, resource_version = ?, "
                        "updated_at = ? WHERE project_id = ?",
                        (_model_bytes(updated_project), project_version, now, project_id),
                    )
                    self._connection.execute(
                        "UPDATE workspace_uploads SET document_json = ?, resource_version = ?, "
                        "updated_at = ? WHERE upload_id = ?",
                        (_model_bytes(updated_upload), upload_version, now, upload_id),
                    )
                    response = m.WorkspaceUploadFinalizeResponseV1(
                        project_id=project_id,
                        upload=updated_upload,
                        publication=publication,
                        project=updated_project,
                    )
                    self._append_project_event(updated_project, now=now)
                    result = StoredResult(201, response, updated_upload.etag)
                    self._store_idempotency(
                        "finalizeCoreWorkspaceUploadV1",
                        scope,
                        idempotency_key,
                        envelope,
                        result,
                    )
                    return result
            except BaseException:
                if (
                    transaction.outcome == "rolled_back"
                    and snapshot_fd is not None
                    and snapshot_identity is not None
                ):
                    _remove_bound_entry_at(
                        self._workspace_root_fd,
                        workspace_snapshot.id,
                        snapshot_fd,
                        snapshot_identity,
                        root_kind="workspace",
                    )
                    os.fsync(self._workspace_root_fd)
                raise
            finally:
                if snapshot_fd is not None:
                    os.close(snapshot_fd)

    def abort_upload(
        self,
        project_id: str,
        upload_id: str,
        request: m.WorkspaceUploadAbortV1,
        *,
        if_match: str,
        idempotency_key: str,
    ) -> StoredResult:
        scope = f"{project_id}:{upload_id}"
        envelope = _idempotency_envelope(
            "abortCoreWorkspaceUploadV1", scope, request, {"if-match": if_match}
        )
        operation_id = "abortCoreWorkspaceUploadV1"
        with self._mutex:
            with self._transaction():
                replay = self._idempotency_replay(
                    operation_id,
                    scope,
                    idempotency_key,
                    envelope,
                    m.WorkspaceUploadSessionV1,
                )
                if replay is None:
                    row, upload = self._upload_row(project_id, upload_id)
                    self._require_etag(upload.etag, if_match, "workspace_upload")
                    if upload.status is not m.WorkspaceUploadStatus.OPEN:
                        raise ResourceConflictError(
                            "workspace_upload_not_open", "The workspace upload is not open."
                        )
                    file_identity = _managed_entry_identity(
                        self._upload_root_fd,
                        row["file_name"],
                        expected_type="file",
                        required=True,
                    )
                    assert file_identity is not None
                    now = self._timestamp()
                    version = int(row["resource_version"]) + 1
                    data = upload.model_dump(mode="python", exclude={"etag"})
                    data.update(status=m.WorkspaceUploadStatus.ABORTED, updated_at=now)
                    updated = _model_with_etag(m.WorkspaceUploadSessionV1, data, version=version)
                    self._connection.execute(
                        "UPDATE workspace_uploads SET document_json = ?, resource_version = ?, "
                        "updated_at = ? WHERE upload_id = ?",
                        (_model_bytes(updated), version, now, upload_id),
                    )
                    result = StoredResult(200, updated, updated.etag)
                    self._store_idempotency(operation_id, scope, idempotency_key, envelope, result)
                    self._store_cleanup_intent(
                        operation_id=operation_id,
                        scope=scope,
                        idempotency_key=idempotency_key,
                        root_kind="upload",
                        entry_name=row["file_name"],
                        identity=file_identity,
                    )
                else:
                    result = replay
            self._reconcile_cleanup_operation(
                operation_id,
                scope,
                idempotency_key,
                error_message="committed upload abort could not reconcile managed state",
            )
            return result

    def store_validation_result(
        self,
        project_id: str,
        request: m.ProjectValidationRequestV1,
        *,
        idempotency_key: str,
        response_factory: Callable[[m.ProjectV1], m.ProjectValidationResponseV1],
    ) -> StoredResult:
        envelope = _idempotency_envelope("validateCoreProjectV1", project_id, request, {})
        with self._mutex, self._transaction():
            replay = self._idempotency_replay(
                "validateCoreProjectV1",
                project_id,
                idempotency_key,
                envelope,
                m.ProjectValidationResponseV1,
            )
            if replay is not None:
                return replay
            _, project = self._project_row(project_id)
            response = response_factory(project)
            result = StoredResult(200, response)
            self._store_idempotency(
                "validateCoreProjectV1", project_id, idempotency_key, envelope, result
            )
            return result

    def replay_failed_idempotency(
        self, operation_id: str, arguments: Mapping[str, object]
    ) -> m.ApiErrorV1 | None:
        identity = _failed_idempotency_identity(operation_id, arguments)
        if identity is None:
            return None
        scope, key, digest = identity
        with self._mutex:
            self._verify_lifecycle_storage()
            row = self._connection.execute(
                "SELECT request_digest, error_json FROM failed_idempotency_records "
                "WHERE operation_id = ? AND resource_scope = ? AND idempotency_key = ?",
                (operation_id, scope, key),
            ).fetchone()
            if row is None:
                return None
            if not hmac.compare_digest(row["request_digest"], digest):
                raise IdempotencyConflictError("idempotency key was reused")
            return _validate_bytes(m.ApiErrorV1, row["error_json"])

    def record_failed_idempotency(
        self,
        operation_id: str,
        arguments: Mapping[str, object],
        error: m.ApiErrorV1,
    ) -> None:
        identity = _failed_idempotency_identity(operation_id, arguments)
        if identity is None or error.code == "idempotency_key_reused":
            return
        scope, key, digest = identity
        now = int(time.time())
        with self._mutex, self._transaction():
            self._connection.execute(
                "DELETE FROM failed_idempotency_records WHERE expires_at_epoch <= ?", (now,)
            )
            row = self._connection.execute(
                "SELECT request_digest FROM failed_idempotency_records "
                "WHERE operation_id = ? AND resource_scope = ? AND idempotency_key = ?",
                (operation_id, scope, key),
            ).fetchone()
            if row is not None:
                if not hmac.compare_digest(row["request_digest"], digest):
                    raise IdempotencyConflictError("idempotency key was reused")
                return
            count = self._connection.execute(
                "SELECT COUNT(*) AS count FROM failed_idempotency_records"
            ).fetchone()["count"]
            if int(count) >= _IDEMPOTENCY_LIMIT:
                raise IdempotencyCapacityError("idempotency capacity is exhausted")
            self._connection.execute(
                "INSERT INTO failed_idempotency_records VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    operation_id,
                    scope,
                    key,
                    digest,
                    _model_bytes(error),
                    now,
                    now + _IDEMPOTENCY_RETENTION_SECONDS,
                ),
            )

    def append_heartbeat(self, active_run_count: int = 0) -> dict[str, object]:
        with self._mutex, self._transaction():
            now = self._timestamp()
            sequence = self._reserve_event_sequence()
            event_id = self.event_cursor(sequence)
            frame = m.SseFrameV1.model_validate(
                {
                    "id": event_id,
                    "event": "heartbeat.v1",
                    "data": {
                        "schema_version": "1",
                        "id": event_id,
                        "sequence": sequence,
                        "occurred_at": now,
                        "event": "heartbeat.v1",
                        "payload": {"active_run_count": active_run_count},
                    },
                }
            )
            self._finish_event(sequence, frame)
            return frame.model_dump(mode="json")

    def replay_events(self, last_event_id: str | None) -> list[dict[str, object]]:
        with self._mutex:
            self._verify_lifecycle_storage()
            after_sequence = 0
            if last_event_id is not None:
                after_sequence = self._decode_event_cursor(last_event_id)
            bounds = self._connection.execute(
                "SELECT MIN(sequence) AS minimum, MAX(sequence) AS maximum FROM events"
            ).fetchone()
            minimum = bounds["minimum"]
            maximum = bounds["maximum"]
            if last_event_id is not None:
                if minimum is None or after_sequence > int(maximum):
                    raise EventCursorInvalidError("event cursor is outside the stream")
                if after_sequence < int(minimum):
                    raise EventCursorExpiredError("event cursor expired")
            rows = self._connection.execute(
                "SELECT frame_json FROM events WHERE sequence > ? ORDER BY sequence ASC LIMIT ?",
                (after_sequence, self._event_replay_limit),
            ).fetchmany(self._event_replay_limit)
            frames: list[dict[str, object]] = []
            for row in rows:
                frame = _validate_bytes(m.SseFrameV1, row["frame_json"])
                frames.append(frame.model_dump(mode="json"))
            return frames

    def event_cursor(self, sequence: int) -> str:
        with self._mutex:
            self._verify_lifecycle_storage()
            body = f"evt.v1.{sequence}"
            signature = hmac.new(
                self._signing_key, body.encode("ascii"), hashlib.sha256
            ).hexdigest()[:24]
            return f"{body}.{signature}"

    def _new_project(
        self,
        request: m.ProjectCreateV1,
        *,
        now: str,
        registry_digest: str | None,
    ) -> m.ProjectV1:
        project_id = _new_id("project")
        task_snapshot = _snapshot(
            self._signing_key,
            m.SnapshotKind.TASK,
            request.task.model_dump(mode="json"),
            now,
        )
        workspace_snapshot = None
        if isinstance(request.workspace, m.ScratchWorkspaceSpecV1):
            workspace_snapshot = _snapshot_from_digest(
                self._signing_key,
                m.SnapshotKind.WORKSPACE,
                _EMPTY_WORKSPACE_DIGEST,
                now,
            )
        model_status = (
            m.ModelPreparationStatus.READY
            if request.spec.execution_mode is m.ExecutionMode.CODEX_SUBSCRIPTION_TRANSCRIPT
            else m.ModelPreparationStatus.UNRESOLVED
        )
        data: dict[str, Any] = {
            "id": project_id,
            "name": request.name,
            "description": request.description,
            "status": m.ProjectStatus.DRAFT,
            "execution_mode": request.spec.execution_mode,
            "workspace_kind": request.workspace.kind,
            "current_task_snapshot": task_snapshot,
            "current_workspace_snapshot": workspace_snapshot,
            "workspace_publication": None,
            "active_revision": None,
            "registry_digest": registry_digest,
            "model_preparation": m.ModelPreparationV1(
                model_ref=request.spec.agent_model_ref,
                status=model_status,
                updated_at=now,
            ),
            "created_at": now,
            "updated_at": now,
            "spec": request.spec,
            "task": request.task,
            "workspace": request.workspace,
        }
        project_snapshot = _snapshot(
            self._signing_key,
            m.SnapshotKind.PROJECT,
            _project_snapshot_payload(data),
            now,
        )
        data["current_project_snapshot"] = project_snapshot
        return _model_with_etag(m.ProjectV1, data, version=1)

    def _validate_project_snapshot_closure(
        self,
        project: m.ProjectV1,
        *,
        publication_owner: tuple[str, str] | None = None,
    ) -> None:
        expected_task = _snapshot(
            self._signing_key,
            m.SnapshotKind.TASK,
            project.task.model_dump(mode="json"),
            project.current_task_snapshot.created_at,
        )
        if project.current_task_snapshot != expected_task:
            raise StoreCorruptionError("project task snapshot binding is invalid")

        if isinstance(project.workspace, m.ScratchWorkspaceSpecV1):
            workspace = project.current_workspace_snapshot
            if workspace is None or workspace != _snapshot_from_digest(
                self._signing_key,
                m.SnapshotKind.WORKSPACE,
                _EMPTY_WORKSPACE_DIGEST,
                workspace.created_at,
            ):
                raise StoreCorruptionError("scratch workspace snapshot binding is invalid")
            if project.workspace_publication is not None:
                raise StoreCorruptionError("scratch project has a workspace publication")
        elif project.workspace_publication is None:
            if project.current_workspace_snapshot is not None:
                raise StoreCorruptionError("unpublished workspace snapshot binding is invalid")
        else:
            if publication_owner is None or publication_owner[0] != project.id:
                raise StoreCorruptionError("workspace publication owner is invalid")
            self._validate_publication_identity(
                project.workspace_publication,
                project_id=project.id,
                upload_id=publication_owner[1],
            )
            if (
                project.current_workspace_snapshot
                != project.workspace_publication.workspace_snapshot
            ):
                raise StoreCorruptionError("project workspace snapshot binding is invalid")

        project_payload = project.model_dump(
            mode="python", exclude={"etag", "current_project_snapshot"}
        )
        expected_project = _snapshot(
            self._signing_key,
            m.SnapshotKind.PROJECT,
            _project_snapshot_payload(project_payload),
            project.current_project_snapshot.created_at,
        )
        if project.current_project_snapshot != expected_project:
            raise StoreCorruptionError("project snapshot binding is invalid")

    def _validate_publication_identity(
        self,
        publication: m.WorkspacePublicationV1,
        *,
        project_id: str,
        upload_id: str,
    ) -> None:
        expected_workspace = _workspace_publication_snapshot(
            self._signing_key,
            project_id=project_id,
            upload_id=upload_id,
            archive_sha256=publication.archive.content_sha256,
            now=publication.workspace_snapshot.created_at,
        )
        expected_content_id = _core_owned_id(
            self._signing_key,
            "workspace-content",
            {
                "project_id": project_id,
                "upload_id": upload_id,
                "sha256": publication.archive.content_sha256,
                "byte_size": publication.archive.byte_size,
                "published_at": publication.published_at,
            },
        )
        if (
            publication.workspace_snapshot != expected_workspace
            or publication.content_ref.content_id != expected_content_id
        ):
            raise StoreCorruptionError("workspace publication Core identity is invalid")

    def _store_publication_owner(
        self,
        *,
        project_id: str,
        upload_id: str,
        publication: m.WorkspacePublicationV1,
    ) -> None:
        self._prune_expired_idempotency_records(int(time.time()))
        owner_count = int(
            self._connection.execute(
                "SELECT COUNT(*) FROM workspace_publication_owners"
            ).fetchone()[0]
        )
        if owner_count >= _MAX_PUBLICATION_OWNERS:
            raise IdempotencyCapacityError("workspace publication ownership capacity is exhausted")
        publication_sha256 = hashlib.sha256(_model_bytes(publication)).hexdigest()
        values = {
            "snapshot_id": publication.workspace_snapshot.id,
            "content_id": publication.content_ref.content_id,
            "publication_sha256": publication_sha256,
            "project_id": project_id,
            "upload_id": upload_id,
            "published_at": publication.published_at,
        }
        identity_hmac = hmac.new(
            self._signing_key,
            _canonical_bytes({"domain": "workspace-publication-owner.v1", "values": values}),
            hashlib.sha256,
        ).hexdigest()
        try:
            self._connection.execute(
                "INSERT INTO workspace_publication_owners("
                "snapshot_id, content_id, publication_sha256, project_id, upload_id, "
                "identity_hmac, published_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    publication.workspace_snapshot.id,
                    publication.content_ref.content_id,
                    publication_sha256,
                    project_id,
                    upload_id,
                    identity_hmac,
                    publication.published_at,
                ),
            )
        except sqlite3.IntegrityError as exc:
            raise StoreCorruptionError("workspace publication ownership is not unique") from exc

    def _validate_publication_owner_row(
        self,
        row: sqlite3.Row,
    ) -> tuple[str, str]:
        values = {
            "snapshot_id": row["snapshot_id"],
            "content_id": row["content_id"],
            "publication_sha256": row["publication_sha256"],
            "project_id": row["project_id"],
            "upload_id": row["upload_id"],
            "published_at": row["published_at"],
        }
        expected_hmac = hmac.new(
            self._signing_key,
            _canonical_bytes({"domain": "workspace-publication-owner.v1", "values": values}),
            hashlib.sha256,
        ).hexdigest()
        if (
            not _is_managed_resource_id(row["snapshot_id"], "workspace-snapshot")
            or not _is_managed_resource_id(row["content_id"], "workspace-content")
            or not _is_sha256(row["publication_sha256"])
            or not _is_managed_resource_id(row["project_id"], "project")
            or not _is_managed_resource_id(row["upload_id"], "upload")
            or not _is_timestamp(row["published_at"])
            or not hmac.compare_digest(row["identity_hmac"], expected_hmac)
        ):
            raise StoreCorruptionError("workspace publication owner row is invalid")
        return row["project_id"], row["upload_id"]

    def _publication_owner_for_snapshot(
        self,
        snapshot_id: str,
    ) -> tuple[str, str] | None:
        row = self._connection.execute(
            "SELECT * FROM workspace_publication_owners WHERE snapshot_id = ?",
            (snapshot_id,),
        ).fetchone()
        if row is None:
            return None
        return self._validate_publication_owner_row(row)

    def _patched_project(
        self,
        current: m.ProjectV1,
        patch: m.ProjectPatchV1,
        *,
        now: str,
        version: int,
        registry_digest: str | None,
    ) -> m.ProjectV1:
        fields = patch.model_fields_set
        spec = patch.spec if "spec" in fields else current.spec
        task = patch.task if "task" in fields else current.task
        workspace = patch.workspace if "workspace" in fields else current.workspace
        assert spec is not None and task is not None and workspace is not None
        task_snapshot = current.current_task_snapshot
        if "task" in fields:
            task_snapshot = _snapshot(
                self._signing_key,
                m.SnapshotKind.TASK,
                task.model_dump(mode="json"),
                now,
            )
        workspace_snapshot = current.current_workspace_snapshot
        publication = current.workspace_publication
        if "workspace" in fields and workspace != current.workspace:
            publication = None
            if isinstance(workspace, m.ScratchWorkspaceSpecV1):
                workspace_snapshot = _snapshot_from_digest(
                    self._signing_key,
                    m.SnapshotKind.WORKSPACE,
                    _EMPTY_WORKSPACE_DIGEST,
                    now,
                )
            else:
                workspace_snapshot = None
        model_preparation = current.model_preparation
        if "spec" in fields and spec != current.spec:
            model_preparation = m.ModelPreparationV1(
                model_ref=spec.agent_model_ref,
                status=(
                    m.ModelPreparationStatus.READY
                    if spec.execution_mode is m.ExecutionMode.CODEX_SUBSCRIPTION_TRANSCRIPT
                    else m.ModelPreparationStatus.UNRESOLVED
                ),
                updated_at=now,
            )
        data = current.model_dump(mode="python", exclude={"etag", "current_project_snapshot"})
        data.update(
            name=patch.name if "name" in fields else current.name,
            description=patch.description if "description" in fields else current.description,
            spec=spec,
            task=task,
            workspace=workspace,
            execution_mode=spec.execution_mode,
            workspace_kind=workspace.kind,
            current_task_snapshot=task_snapshot,
            current_workspace_snapshot=workspace_snapshot,
            workspace_publication=publication,
            registry_digest=registry_digest,
            model_preparation=model_preparation,
            updated_at=now,
        )
        data["current_project_snapshot"] = _snapshot(
            self._signing_key,
            m.SnapshotKind.PROJECT,
            _project_snapshot_payload(data),
            now,
        )
        return _model_with_etag(m.ProjectV1, data, version=version)

    def _validate_finalize_preconditions(
        self,
        upload: m.WorkspaceUploadSessionV1,
        project: m.ProjectV1,
        request: m.WorkspaceUploadFinalizeV1,
        *,
        if_match: str,
        if_project_match: str,
    ) -> None:
        self._require_etag(upload.etag, if_match, "workspace_upload")
        if project.etag != if_project_match or upload.project_etag != if_project_match:
            raise ETagPreconditionError("finalize_project")
        if upload.project_snapshot != project.current_project_snapshot:
            raise ResourceConflictError(
                "project_snapshot_changed",
                "The project changed after the workspace upload began.",
            )
        if upload.status is not m.WorkspaceUploadStatus.OPEN:
            raise ResourceConflictError(
                "workspace_upload_not_open", "The workspace upload is not open."
            )
        if upload.accepted_offset != upload.archive.byte_size:
            raise ResourceConflictError(
                "workspace_upload_incomplete", "The workspace upload is incomplete."
            )
        if request.content_sha256 != upload.archive.content_sha256:
            raise ResourceConflictError(
                "workspace_digest_mismatch",
                "The final workspace digest differs from the upload declaration.",
            )

    def _project_row(self, project_id: str) -> tuple[sqlite3.Row, m.ProjectV1]:
        self._verify_lifecycle_storage()
        row = self._connection.execute(
            "SELECT * FROM projects WHERE project_id = ?", (project_id,)
        ).fetchone()
        if row is None:
            raise ResourceNotFoundError("project", project_id)
        return row, _validate_bytes(m.ProjectV1, row["document_json"])

    def _upload_row(
        self, project_id: str, upload_id: str
    ) -> tuple[sqlite3.Row, m.WorkspaceUploadSessionV1]:
        self._verify_lifecycle_storage()
        row = self._connection.execute(
            "SELECT * FROM workspace_uploads WHERE upload_id = ? AND project_id = ?",
            (upload_id, project_id),
        ).fetchone()
        if row is None:
            raise ResourceNotFoundError("workspace_upload", upload_id)
        return row, _validate_bytes(m.WorkspaceUploadSessionV1, row["document_json"])

    def _idempotency_replay(
        self,
        operation_id: str,
        scope: str,
        key: str,
        envelope: _IdempotencyRequestEnvelope,
        response_model: type[_ModelT] | None,
    ) -> StoredResult | None:
        row = self._connection.execute(
            "SELECT * FROM idempotency_records WHERE operation_id = ? "
            "AND resource_scope = ? AND idempotency_key = ?",
            (operation_id, scope, key),
        ).fetchone()
        if row is None:
            return None
        _validate_idempotency_request_envelope(row)
        if (
            not hmac.compare_digest(row["request_digest"], envelope.digest)
            or bytes(row["request_json"]) != envelope.request_json
            or bytes(row["semantic_headers_json"]) != envelope.semantic_headers_json
        ):
            raise IdempotencyConflictError("idempotency key was reused")
        model = _validate_idempotency_row(
            row,
            signing_key=self._signing_key,
            publication_owner_lookup=self._publication_owner_for_snapshot,
        )
        if response_model is not None and not isinstance(model, response_model):
            raise StoreCorruptionError("idempotency response type is invalid")
        if response_model is None and model is not None:
            raise StoreCorruptionError("no-content idempotency response is invalid")
        return StoredResult(int(row["status_code"]), model, row["etag"], replayed=True)

    def _store_idempotency(
        self,
        operation_id: str,
        scope: str,
        key: str,
        envelope: _IdempotencyRequestEnvelope,
        result: StoredResult,
    ) -> None:
        now = int(time.time())
        self._prune_expired_idempotency_records(now)
        count = self._connection.execute(
            "SELECT COUNT(*) AS count FROM idempotency_records"
        ).fetchone()["count"]
        if int(count) >= _IDEMPOTENCY_LIMIT:
            raise IdempotencyCapacityError("idempotency capacity is exhausted")
        response_type = (
            result.model.__class__.__name__ if result.model is not None else "NoContent"
        )
        response_bytes = _model_bytes(result.model) if result.model is not None else None
        self._connection.execute(
            "INSERT INTO idempotency_records(operation_id, resource_scope, "
            "idempotency_key, request_digest, request_json, semantic_headers_json, "
            "status_code, response_type, response_json, etag, created_at_epoch, "
            "expires_at_epoch) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                operation_id,
                scope,
                key,
                envelope.digest,
                envelope.request_json,
                envelope.semantic_headers_json,
                result.status_code,
                response_type,
                response_bytes,
                result.etag,
                now,
                now + _IDEMPOTENCY_RETENTION_SECONDS,
            ),
        )

    def _prune_expired_idempotency_records(self, now: int) -> None:
        self._connection.execute(
            "DELETE FROM idempotency_records AS records WHERE expires_at_epoch <= ? "
            "AND NOT EXISTS (SELECT 1 FROM managed_cleanup_intents AS cleanup "
            "WHERE cleanup.operation_id = records.operation_id "
            "AND cleanup.resource_scope = records.resource_scope "
            "AND cleanup.idempotency_key = records.idempotency_key)",
            (now,),
        )
        self._connection.execute(
            "DELETE FROM workspace_publication_owners AS owners "
            "WHERE NOT EXISTS (SELECT 1 FROM workspace_uploads AS uploads "
            "WHERE uploads.upload_id = owners.upload_id) "
            "AND NOT EXISTS (SELECT 1 FROM idempotency_records AS records "
            "WHERE records.response_json IS NOT NULL AND ("
            "json_extract(CAST(records.response_json AS TEXT), "
            "'$.workspace_publication.workspace_snapshot.id') = owners.snapshot_id OR "
            "json_extract(CAST(records.response_json AS TEXT), "
            "'$.publication.workspace_snapshot.id') = owners.snapshot_id OR "
            "json_extract(CAST(records.response_json AS TEXT), "
            "'$.project.workspace_publication.workspace_snapshot.id') "
            "= owners.snapshot_id OR "
            "json_extract(CAST(records.response_json AS TEXT), "
            "'$.upload.publication.workspace_snapshot.id') = owners.snapshot_id))"
        )

    def _store_cleanup_intent(
        self,
        *,
        operation_id: str,
        scope: str,
        idempotency_key: str,
        root_kind: Literal["upload", "workspace"],
        entry_name: str,
        identity: os.stat_result,
    ) -> None:
        if not _is_safe_managed_entry_name(entry_name):
            raise StoreCorruptionError("managed cleanup entry name is invalid")
        cleanup_id = _new_id("cleanup")
        created_at_epoch = int(time.time())
        values = {
            "cleanup_id": cleanup_id,
            "operation_id": operation_id,
            "resource_scope": scope,
            "idempotency_key": idempotency_key,
            "root_kind": root_kind,
            "entry_name": entry_name,
            "expected_dev": int(identity.st_dev),
            "expected_ino": int(identity.st_ino),
            "expected_mode": int(identity.st_mode),
            "expected_uid": int(identity.st_uid),
            "expected_nlink": int(identity.st_nlink),
            "created_at_epoch": created_at_epoch,
        }
        if any(
            value < 0 or value > (2**63 - 1)
            for key, value in values.items()
            if key.startswith("expected_") and isinstance(value, int)
        ):
            raise StoreCorruptionError("managed cleanup entry identity is invalid")
        identity_hmac = self._managed_cleanup_hmac(values)
        self._connection.execute(
            "INSERT INTO managed_cleanup_intents("
            "cleanup_id, operation_id, resource_scope, idempotency_key, root_kind, "
            "entry_name, expected_dev, expected_ino, expected_mode, expected_uid, "
            "expected_nlink, identity_hmac, created_at_epoch) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                cleanup_id,
                operation_id,
                scope,
                idempotency_key,
                root_kind,
                entry_name,
                identity.st_dev,
                identity.st_ino,
                identity.st_mode,
                identity.st_uid,
                identity.st_nlink,
                identity_hmac,
                created_at_epoch,
            ),
        )

    def _managed_cleanup_hmac(self, values: Mapping[str, object]) -> str:
        return hmac.new(
            self._signing_key,
            _canonical_bytes({"domain": "managed-cleanup-intent.v1", "values": values}),
            hashlib.sha256,
        ).hexdigest()

    def _validate_cleanup_intent_row(self, row: sqlite3.Row) -> None:
        values = {
            "cleanup_id": row["cleanup_id"],
            "operation_id": row["operation_id"],
            "resource_scope": row["resource_scope"],
            "idempotency_key": row["idempotency_key"],
            "root_kind": row["root_kind"],
            "entry_name": row["entry_name"],
            "expected_dev": int(row["expected_dev"]),
            "expected_ino": int(row["expected_ino"]),
            "expected_mode": int(row["expected_mode"]),
            "expected_uid": int(row["expected_uid"]),
            "expected_nlink": int(row["expected_nlink"]),
            "created_at_epoch": int(row["created_at_epoch"]),
        }
        operation_id = row["operation_id"]
        if (
            not _is_managed_resource_id(row["cleanup_id"], "cleanup")
            or operation_id not in {"abortCoreWorkspaceUploadV1", "deleteCoreProjectV1"}
            or row["root_kind"] not in {"upload", "workspace"}
            or not _is_safe_managed_entry_name(row["entry_name"])
            or row["expected_dev"] < 0
            or row["expected_ino"] <= 0
            or row["expected_uid"] != os.geteuid()
            or row["expected_nlink"] <= 0
            or row["created_at_epoch"] < 0
            or not hmac.compare_digest(row["identity_hmac"], self._managed_cleanup_hmac(values))
        ):
            raise StoreCorruptionError("managed cleanup intent identity is invalid")
        expected_mode = int(row["expected_mode"])
        expected_type = stat.S_IFMT(expected_mode)
        if row["root_kind"] == "upload":
            if (
                expected_type != stat.S_IFREG
                or stat.S_IMODE(expected_mode) != 0o600
                or not _is_managed_upload_file_name(row["entry_name"])
                or operation_id not in {"abortCoreWorkspaceUploadV1", "deleteCoreProjectV1"}
            ):
                raise StoreCorruptionError("managed upload cleanup intent is invalid")
        elif (
            expected_type != stat.S_IFDIR
            or stat.S_IMODE(expected_mode) != 0o700
            or not _is_managed_resource_id(row["entry_name"], "workspace-snapshot")
            or operation_id != "deleteCoreProjectV1"
        ):
            raise StoreCorruptionError("managed workspace cleanup intent is invalid")

    def _live_managed_entry_names(self) -> tuple[set[str], set[str]]:
        upload_names: set[str] = set()
        snapshot_names: set[str] = set()
        cursor = self._connection.execute(
            "SELECT upload_id, project_id, file_name, document_json FROM workspace_uploads "
            "ORDER BY upload_id LIMIT ?",
            (_MAX_UPLOADS + 1,),
        )
        rows = cursor.fetchmany(_MAX_UPLOADS + 1)
        if len(rows) > _MAX_UPLOADS or cursor.fetchone() is not None:
            raise StoreCorruptionError("managed workspace live-state quota is exceeded")
        for row in rows:
            upload = _validate_bytes(m.WorkspaceUploadSessionV1, row["document_json"])
            if (
                upload.id != row["upload_id"]
                or upload.project_id != row["project_id"]
                or row["file_name"] != f"{upload.id}.part"
            ):
                raise StoreCorruptionError("managed workspace live-state identity is invalid")
            if upload.status in {
                m.WorkspaceUploadStatus.OPEN,
                m.WorkspaceUploadStatus.FINALIZED,
            }:
                upload_names.add(row["file_name"])
            if upload.status is m.WorkspaceUploadStatus.FINALIZED:
                if upload.publication is None:
                    raise StoreCorruptionError("finalized managed workspace has no publication")
                self._validate_publication_identity(
                    upload.publication,
                    project_id=upload.project_id,
                    upload_id=upload.id,
                )
                snapshot_names.add(upload.publication.workspace_snapshot.id)
        return upload_names, snapshot_names

    def _reconcile_cleanup_operation(
        self,
        operation_id: str,
        scope: str,
        idempotency_key: str,
        *,
        error_message: str,
    ) -> None:
        try:
            converged = False
            with self._transaction():
                rows = self._connection.execute(
                    "SELECT * FROM managed_cleanup_intents WHERE operation_id = ? "
                    "AND resource_scope = ? AND idempotency_key = ? ORDER BY cleanup_id "
                    "LIMIT ?",
                    (operation_id, scope, idempotency_key, _MAX_CLEANUP_INTENTS + 1),
                ).fetchmany(_MAX_CLEANUP_INTENTS + 1)
                if len(rows) > _MAX_CLEANUP_INTENTS:
                    raise StoreCorruptionError("managed cleanup intent quota is exceeded")
                if not rows:
                    return
                for row in rows:
                    self._validate_cleanup_intent_row(row)
                live_uploads, live_snapshots = self._live_managed_entry_names()
                for row in rows:
                    live_names = live_uploads if row["root_kind"] == "upload" else live_snapshots
                    if row["entry_name"] in live_names:
                        raise StoreCorruptionError(
                            "managed cleanup intent overlaps live owned state"
                        )
                budget = _ManagedCleanupBudget(
                    nodes_remaining=_MAX_RECOVERY_CLEANUP_NODES,
                    name_bytes_remaining=_MAX_RECOVERY_CLEANUP_NAME_BYTES,
                )
                converged = self._reconcile_managed_orphans(
                    live_uploads,
                    live_snapshots,
                    budget=budget,
                )
                if converged:
                    self._connection.execute(
                        "DELETE FROM managed_cleanup_intents WHERE operation_id = ? "
                        "AND resource_scope = ? AND idempotency_key = ?",
                        (operation_id, scope, idempotency_key),
                    )
            if not converged:
                raise PostCommitStoreError(error_message)
        except PostCommitStoreError:
            raise
        except Exception as exc:
            raise PostCommitStoreError(error_message) from exc

    def _reconcile_managed_orphans(
        self,
        live_uploads: set[str],
        live_snapshots: set[str],
        *,
        budget: _ManagedCleanupBudget,
    ) -> bool:
        self._verify_lifecycle_storage()
        uploads_complete = _reconcile_orphan_entries_at(
            "upload",
            self._upload_root_fd,
            live_uploads,
            budget=budget,
        )
        if not uploads_complete:
            return False
        snapshots_complete = _reconcile_orphan_entries_at(
            "workspace",
            self._workspace_root_fd,
            live_snapshots,
            budget=budget,
        )
        self._verify_lifecycle_storage()
        return snapshots_complete

    def _append_project_event(self, project: m.ProjectV1, *, now: str) -> None:
        sequence = self._reserve_event_sequence()
        event_id = self.event_cursor(sequence)
        summary = m.ProjectSummaryV1.model_validate(
            project.model_dump(mode="python", exclude={"spec", "task", "workspace"})
        )
        frame = m.SseFrameV1.model_validate_json(
            _canonical_bytes(
                {
                    "id": event_id,
                    "event": "project.updated.v1",
                    "data": {
                        "schema_version": "1",
                        "id": event_id,
                        "sequence": sequence,
                        "occurred_at": now,
                        "event": "project.updated.v1",
                        "change": {
                            "change_id": _new_id("change"),
                            "resource_type": "project",
                            "resource_id": project.id,
                            "resource_etag": project.etag,
                        },
                        "payload": summary.model_dump(mode="json"),
                    },
                }
            )
        )
        self._finish_event(sequence, frame)

    def _reserve_event_sequence(self) -> int:
        cursor = self._connection.execute(
            "INSERT INTO events(event_id, frame_json, created_at_epoch) VALUES (NULL, NULL, ?)",
            (int(time.time()),),
        )
        return int(cursor.lastrowid)

    def _finish_event(self, sequence: int, frame: m.SseFrameV1) -> None:
        self._connection.execute(
            "UPDATE events SET event_id = ?, frame_json = ? WHERE sequence = ?",
            (frame.id, _model_bytes(frame), sequence),
        )
        cutoff = sequence - self._event_replay_limit
        if cutoff > 0:
            self._connection.execute("DELETE FROM events WHERE sequence <= ?", (cutoff,))

    def _decode_event_cursor(self, value: str) -> int:
        parts = value.split(".")
        if len(parts) != 4 or parts[:2] != ["evt", "v1"] or not parts[2].isdigit():
            raise EventCursorInvalidError("event cursor is invalid")
        sequence = int(parts[2])
        if sequence < 1 or not hmac.compare_digest(value, self.event_cursor(sequence)):
            raise EventCursorInvalidError("event cursor is invalid")
        return sequence

    def _encode_cursor(self, binding: str, boundary: tuple[str, str]) -> str:
        payload = {
            "v": 1,
            "binding": binding,
            "boundary": list(boundary),
            "expires": int(time.time()) + _CURSOR_TTL_SECONDS,
        }
        raw = _canonical_bytes(payload)
        signature = hmac.new(self._signing_key, raw, hashlib.sha256).digest()
        return base64.urlsafe_b64encode(raw + signature).decode("ascii").rstrip("=")

    def _decode_cursor(self, value: str, binding: str) -> tuple[str, str]:
        try:
            padded = value + "=" * (-len(value) % 4)
            token = base64.urlsafe_b64decode(padded.encode("ascii"))
        except Exception as exc:
            raise CursorInvalidError("project cursor is invalid") from exc
        if len(token) <= 32:
            raise CursorInvalidError("project cursor is invalid")
        raw, signature = token[:-32], token[-32:]
        if not hmac.compare_digest(
            signature, hmac.new(self._signing_key, raw, hashlib.sha256).digest()
        ):
            raise CursorInvalidError("project cursor is invalid")
        try:
            payload = json.loads(raw)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise CursorInvalidError("project cursor is invalid") from exc
        if (
            type(payload) is not dict
            or payload.get("v") != 1
            or payload.get("binding") != binding
            or type(payload.get("expires")) is not int
            or type(payload.get("boundary")) is not list
            or len(payload["boundary"]) != 2
            or any(type(item) is not str for item in payload["boundary"])
        ):
            raise CursorInvalidError("project cursor is invalid")
        if payload["expires"] <= int(time.time()):
            raise CursorExpiredError("project cursor expired")
        return payload["boundary"][0], payload["boundary"][1]

    def _recovery_rows(
        self,
        table: Literal[
            "projects",
            "workspace_uploads",
            "workspace_publication_owners",
            "idempotency_records",
            "managed_cleanup_intents",
            "failed_idempotency_records",
            "events",
            "metadata",
        ],
        *,
        columns: tuple[str, ...],
        bounded_columns: tuple[str, ...],
        max_rows: int,
    ):
        totals = ["COUNT(*)"] + [
            f"COALESCE(SUM(length(CAST({column} AS BLOB))), 0)" for column in bounded_columns
        ]
        aggregate = self._connection.execute(f"SELECT {', '.join(totals)} FROM {table}").fetchone()
        assert aggregate is not None
        row_count = int(aggregate[0])
        blob_bytes = sum(int(aggregate[index]) for index in range(1, len(aggregate)))
        self._startup_scan_rows += row_count
        self._startup_scan_bytes += blob_bytes
        if row_count > max_rows or blob_bytes > _MAX_STARTUP_BLOB_BYTES:
            raise StoreCorruptionError(f"Core Control {table} recovery quota is exceeded")
        if (
            self._startup_scan_rows > _MAX_STARTUP_ROWS
            or self._startup_scan_bytes > _MAX_STARTUP_BLOB_BYTES
        ):
            raise StoreCorruptionError("Core Control aggregate startup quota is exceeded")
        for column in bounded_columns:
            oversized = self._connection.execute(
                f"SELECT 1 FROM {table} WHERE {column} IS NOT NULL AND "
                f"length(CAST({column} AS BLOB)) > {_MAX_STARTUP_VALUE_BYTES} LIMIT 1"
            ).fetchone()
            if oversized is not None:
                raise StoreCorruptionError(f"Core Control {table} recovery quota is exceeded")

        last_rowid = 0
        guarded_columns = []
        for column in columns:
            if column in bounded_columns:
                guarded_columns.append(
                    f"CASE WHEN length(CAST({column} AS BLOB)) "
                    f"<= {_MAX_STARTUP_VALUE_BYTES} "
                    f"THEN {column} END AS {column}"
                )
            else:
                guarded_columns.append(column)
        seen = 0
        while True:
            cursor = self._connection.execute(
                f"SELECT rowid AS _recovery_rowid, {', '.join(guarded_columns)} "
                f"FROM {table} WHERE rowid > ? ORDER BY rowid LIMIT ?",
                (last_rowid, _RECOVERY_PAGE_SIZE),
            )
            page = cursor.fetchmany(_RECOVERY_PAGE_SIZE)
            if cursor.fetchone() is not None:
                raise StoreCorruptionError(f"Core Control {table} page exceeded its bound")
            if not page:
                break
            for row in page:
                seen += 1
                if seen > max_rows:
                    raise StoreCorruptionError(f"Core Control {table} recovery quota is exceeded")
                last_rowid = int(row["_recovery_rowid"])
                yield row
        if seen != row_count:
            raise StoreCorruptionError(f"Core Control {table} recovery scan changed")

    def _recover_and_validate(self) -> None:
        with self._mutex, self._transaction():
            self._verify_schema_fingerprint()
            self._verify_database_integrity()
            self._startup_scan_rows = 0
            self._startup_scan_bytes = 0
            metadata_rows = list(
                self._recovery_rows(
                    "metadata",
                    columns=("key", "value"),
                    bounded_columns=("key", "value"),
                    max_rows=1,
                )
            )
            if (
                len(metadata_rows) != 1
                or metadata_rows[0]["key"] != "signing_key"
                or bytes(metadata_rows[0]["value"]) != self._signing_key
            ):
                raise StoreCorruptionError("Core Control metadata is invalid")
            publication_owner_rows: dict[str, sqlite3.Row] = {}
            publication_owner_uploads: dict[str, str] = {}
            for row in self._recovery_rows(
                "workspace_publication_owners",
                columns=(
                    "snapshot_id",
                    "content_id",
                    "publication_sha256",
                    "project_id",
                    "upload_id",
                    "identity_hmac",
                    "published_at",
                ),
                bounded_columns=(
                    "snapshot_id",
                    "content_id",
                    "publication_sha256",
                    "project_id",
                    "upload_id",
                    "identity_hmac",
                    "published_at",
                ),
                max_rows=_MAX_PUBLICATION_OWNERS,
            ):
                project_id, upload_id = self._validate_publication_owner_row(row)
                if row["snapshot_id"] in publication_owner_rows:
                    raise StoreCorruptionError("workspace publication snapshot owner is ambiguous")
                if upload_id in publication_owner_uploads:
                    raise StoreCorruptionError("workspace publication upload owner is ambiguous")
                publication_owner_rows[row["snapshot_id"]] = row
                publication_owner_uploads[upload_id] = project_id
            project_publications: dict[str, m.WorkspacePublicationV1] = {}
            publication_projects: dict[str, str] = {}
            publication_owners: dict[tuple[str, str], str] = {}
            for row in self._recovery_rows(
                "projects",
                columns=(
                    "project_id",
                    "identity_hmac",
                    "document_json",
                    "resource_version",
                    "created_at",
                    "updated_at",
                ),
                bounded_columns=(
                    "project_id",
                    "identity_hmac",
                    "document_json",
                    "created_at",
                    "updated_at",
                ),
                max_rows=_MAX_PROJECTS,
            ):
                project = _validate_bytes(m.ProjectV1, row["document_json"])
                version = int(row["resource_version"])
                project_data = project.model_dump(mode="python", exclude={"etag"})
                if (
                    project.id != row["project_id"]
                    or project.created_at != row["created_at"]
                    or project.updated_at != row["updated_at"]
                    or project.etag != _etag(project_data, version=version)
                    or not hmac.compare_digest(
                        row["identity_hmac"],
                        self._resource_identity_hmac("project", project.id),
                    )
                ):
                    raise StoreCorruptionError("project row identity is invalid")
                publication_owner: tuple[str, str] | None = None
                if project.workspace_publication is not None:
                    owner_row = publication_owner_rows.get(
                        project.workspace_publication.workspace_snapshot.id
                    )
                    if owner_row is not None:
                        publication_owner = (
                            owner_row["project_id"],
                            owner_row["upload_id"],
                        )
                self._validate_project_snapshot_closure(
                    project,
                    publication_owner=publication_owner,
                )
                if project.workspace_publication is not None:
                    publication = project.workspace_publication
                    assert publication_owner is not None
                    expected_digest = hashlib.sha256(
                        _canonical_bytes(
                            {
                                "project_id": project.id,
                                "upload_id": publication_owner[1],
                                "archive_sha256": publication.archive.content_sha256,
                            }
                        )
                    ).hexdigest()
                    if publication.workspace_snapshot.content_sha256 != expected_digest:
                        raise StoreCorruptionError("workspace snapshot digest binding is invalid")
                    existing = project_publications.setdefault(
                        publication.workspace_snapshot.id, publication
                    )
                    if existing != publication:
                        raise StoreCorruptionError("workspace snapshot publication is ambiguous")
                    ownership_keys = (
                        ("publication", hashlib.sha256(_model_bytes(publication)).hexdigest()),
                        ("snapshot", publication.workspace_snapshot.id),
                        ("content", publication.content_ref.content_id),
                    )
                    for ownership_key in ownership_keys:
                        owner = publication_owners.setdefault(ownership_key, project.id)
                        if owner != project.id:
                            raise StoreCorruptionError(
                                "workspace publication has more than one project owner"
                            )
                    publication_projects[publication.workspace_snapshot.id] = project.id
            referenced_files: set[str] = set()
            snapshot_sources: dict[str, tuple[m.WorkspacePublicationV1, str, str]] = {}
            for row in self._recovery_rows(
                "workspace_uploads",
                columns=(
                    "upload_id",
                    "project_id",
                    "identity_hmac",
                    "document_json",
                    "resource_version",
                    "file_name",
                    "created_at",
                    "updated_at",
                ),
                bounded_columns=(
                    "upload_id",
                    "project_id",
                    "identity_hmac",
                    "document_json",
                    "file_name",
                    "created_at",
                    "updated_at",
                ),
                max_rows=_MAX_UPLOADS,
            ):
                upload = _validate_bytes(m.WorkspaceUploadSessionV1, row["document_json"])
                version = int(row["resource_version"])
                upload_data = upload.model_dump(mode="python", exclude={"etag"})
                if (
                    upload.id != row["upload_id"]
                    or upload.project_id != row["project_id"]
                    or upload.created_at != row["created_at"]
                    or upload.updated_at != row["updated_at"]
                    or upload.etag != _etag(upload_data, version=version)
                    or not hmac.compare_digest(
                        row["identity_hmac"],
                        self._resource_identity_hmac("upload", upload.id),
                    )
                ):
                    raise StoreCorruptionError("workspace upload row identity is invalid")
                for snapshot in (
                    upload.project_snapshot,
                    upload.base_workspace_snapshot,
                ):
                    if snapshot is not None and snapshot != _snapshot_from_digest(
                        self._signing_key,
                        snapshot.kind,
                        snapshot.content_sha256,
                        snapshot.created_at,
                    ):
                        raise StoreCorruptionError(
                            "workspace upload snapshot Core identity is invalid"
                        )
                file_name = row["file_name"]
                if file_name != f"{upload.id}.part":
                    raise StoreCorruptionError("workspace upload file identity is invalid")
                if upload.status is m.WorkspaceUploadStatus.OPEN:
                    referenced_files.add(file_name)
                elif upload.status is m.WorkspaceUploadStatus.FINALIZED:
                    referenced_files.add(file_name)
                    publication = upload.publication
                    if publication is None:
                        raise StoreCorruptionError("finalized workspace upload has no publication")
                    snapshot_id = publication.workspace_snapshot.id
                    owner_row = publication_owner_rows.get(snapshot_id)
                    if owner_row is not None and owner_row["project_id"] != upload.project_id:
                        raise StoreCorruptionError(
                            "workspace publication is not bound to an upload from the same project"
                        )
                    self._validate_publication_identity(
                        publication,
                        project_id=upload.project_id,
                        upload_id=upload.id,
                    )
                    if (
                        owner_row is None
                        or owner_row["project_id"] != upload.project_id
                        or owner_row["upload_id"] != upload.id
                        or owner_row["content_id"] != publication.content_ref.content_id
                        or owner_row["publication_sha256"]
                        != hashlib.sha256(_model_bytes(publication)).hexdigest()
                        or owner_row["published_at"] != publication.published_at
                    ):
                        raise StoreCorruptionError(
                            "workspace publication owner binding is invalid"
                        )
                    existing = snapshot_sources.setdefault(
                        snapshot_id, (publication, file_name, upload.project_id)
                    )
                    if existing[0] != publication or existing[2] != upload.project_id:
                        raise StoreCorruptionError("workspace snapshot source is ambiguous")
            if not set(snapshot_sources).issubset(publication_owner_rows):
                raise StoreCorruptionError("workspace publication owner inventory is incomplete")
            for snapshot_id, publication in project_publications.items():
                source = snapshot_sources.get(snapshot_id)
                if source is None or source[0] != publication:
                    raise StoreCorruptionError(
                        "published project workspace has no authoritative upload"
                    )
                if source[2] != publication_projects[snapshot_id]:
                    raise StoreCorruptionError(
                        "workspace publication is not bound to an upload from the same project"
                    )
            for row in self._recovery_rows(
                "events",
                columns=("sequence", "event_id", "frame_json", "created_at_epoch"),
                bounded_columns=("event_id", "frame_json"),
                max_rows=self._event_replay_limit,
            ):
                frame = _validate_bytes(m.SseFrameV1, row["frame_json"])
                if (
                    _model_bytes(frame) != bytes(row["frame_json"])
                    or frame.id != row["event_id"]
                    or frame.data.root.sequence != row["sequence"]
                ):
                    raise StoreCorruptionError("event row identity is invalid")
                try:
                    cursor_sequence = self._decode_event_cursor(frame.id)
                except EventCursorInvalidError as exc:
                    raise StoreCorruptionError("event cursor authentication failed") from exc
                if cursor_sequence != int(row["sequence"]):
                    raise StoreCorruptionError("event cursor sequence is invalid")
            valid_successes: set[tuple[str, str, str]] = set()
            idempotency_publications: set[str] = set()
            for row in self._recovery_rows(
                "idempotency_records",
                columns=(
                    "operation_id",
                    "resource_scope",
                    "idempotency_key",
                    "request_digest",
                    "request_json",
                    "semantic_headers_json",
                    "status_code",
                    "response_type",
                    "response_json",
                    "etag",
                    "created_at_epoch",
                    "expires_at_epoch",
                ),
                bounded_columns=(
                    "operation_id",
                    "resource_scope",
                    "idempotency_key",
                    "request_digest",
                    "request_json",
                    "semantic_headers_json",
                    "response_type",
                    "response_json",
                    "etag",
                ),
                max_rows=_IDEMPOTENCY_LIMIT,
            ):
                idempotency_model = _validate_idempotency_row(
                    row,
                    signing_key=self._signing_key,
                    publication_owner_lookup=lambda snapshot_id: (
                        (
                            publication_owner_rows[snapshot_id]["project_id"],
                            publication_owner_rows[snapshot_id]["upload_id"],
                        )
                        if snapshot_id in publication_owner_rows
                        else None
                    ),
                )
                idempotency_publications.update(_publication_snapshot_ids(idempotency_model))
                valid_successes.add(
                    (row["operation_id"], row["resource_scope"], row["idempotency_key"])
                )
            for row in self._recovery_rows(
                "failed_idempotency_records",
                columns=(
                    "operation_id",
                    "resource_scope",
                    "idempotency_key",
                    "request_digest",
                    "error_json",
                    "created_at_epoch",
                    "expires_at_epoch",
                ),
                bounded_columns=(
                    "operation_id",
                    "resource_scope",
                    "idempotency_key",
                    "request_digest",
                    "error_json",
                ),
                max_rows=_IDEMPOTENCY_LIMIT,
            ):
                success_scope = _success_scope_for_failed_idempotency(row)
                if (
                    success_scope is not None
                    and (
                        row["operation_id"],
                        success_scope,
                        row["idempotency_key"],
                    )
                    in valid_successes
                ):
                    self._connection.execute(
                        "DELETE FROM failed_idempotency_records WHERE operation_id = ? "
                        "AND resource_scope = ? AND idempotency_key = ?",
                        (
                            row["operation_id"],
                            row["resource_scope"],
                            row["idempotency_key"],
                        ),
                    )
                    continue
                _validate_bytes(m.ApiErrorV1, row["error_json"])
                if not _is_sha256(row["request_digest"]):
                    raise StoreCorruptionError("failed idempotency request digest is invalid")

            retained_publication_owners = set(snapshot_sources) | idempotency_publications
            unknown_publication_owners = set(publication_owner_rows) - retained_publication_owners
            for snapshot_id in unknown_publication_owners:
                self._connection.execute(
                    "DELETE FROM workspace_publication_owners WHERE snapshot_id = ?",
                    (snapshot_id,),
                )

            cleanup_intent_count = 0
            for row in self._recovery_rows(
                "managed_cleanup_intents",
                columns=(
                    "cleanup_id",
                    "operation_id",
                    "resource_scope",
                    "idempotency_key",
                    "root_kind",
                    "entry_name",
                    "expected_dev",
                    "expected_ino",
                    "expected_mode",
                    "expected_uid",
                    "expected_nlink",
                    "identity_hmac",
                    "created_at_epoch",
                ),
                bounded_columns=(
                    "cleanup_id",
                    "operation_id",
                    "resource_scope",
                    "idempotency_key",
                    "root_kind",
                    "entry_name",
                    "identity_hmac",
                ),
                max_rows=_MAX_CLEANUP_INTENTS,
            ):
                self._validate_cleanup_intent_row(row)
                success_identity = (
                    row["operation_id"],
                    row["resource_scope"],
                    row["idempotency_key"],
                )
                if success_identity not in valid_successes:
                    raise StoreCorruptionError(
                        "managed cleanup intent has no durable success record"
                    )
                live_names = (
                    referenced_files if row["root_kind"] == "upload" else set(snapshot_sources)
                )
                if row["entry_name"] in live_names:
                    raise StoreCorruptionError("managed cleanup intent overlaps live owned state")
                cleanup_intent_count += 1

            upload_root_fd = self._upload_root_fd
            workspace_root_fd = self._workspace_root_fd
            for file_name in referenced_files:
                row = self._connection.execute(
                    "SELECT document_json FROM workspace_uploads WHERE file_name = ?",
                    (file_name,),
                ).fetchone()
                if row is None:
                    raise StoreCorruptionError("workspace upload source disappeared")
                upload = _validate_bytes(m.WorkspaceUploadSessionV1, row["document_json"])
                if upload.status is m.WorkspaceUploadStatus.OPEN:
                    self._recover_open_upload(
                        upload_root_fd,
                        file_name,
                        accepted_offset=upload.accepted_offset,
                    )
            for snapshot_id, (publication, archive_name, _project_id) in snapshot_sources.items():
                try:
                    archive_fd = os.open(
                        archive_name,
                        os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0),
                        dir_fd=upload_root_fd,
                    )
                    try:
                        _require_bound_regular_entry(
                            upload_root_fd,
                            archive_name,
                            archive_fd,
                            expected_size=publication.archive.byte_size,
                        )
                    finally:
                        os.close(archive_fd)
                    verify_materialized_workspace(
                        self.upload_root / archive_name,
                        publication.archive,
                        archive_root_fd=upload_root_fd,
                        archive_name=archive_name,
                        workspace_root_fd=workspace_root_fd,
                        snapshot_name=snapshot_id,
                    )
                except (OSError, WorkspaceArchiveError) as exc:
                    raise StoreCorruptionError(
                        "published workspace snapshot is missing or invalid"
                    ) from exc
            cleanup_budget = _ManagedCleanupBudget(
                nodes_remaining=_MAX_RECOVERY_CLEANUP_NODES,
                name_bytes_remaining=_MAX_RECOVERY_CLEANUP_NAME_BYTES,
            )
            cleanup_converged = self._reconcile_managed_orphans(
                referenced_files,
                set(snapshot_sources),
                budget=cleanup_budget,
            )
            if cleanup_converged and cleanup_intent_count:
                self._connection.execute("DELETE FROM managed_cleanup_intents")
            _verify_managed_disk_quota(
                upload_root_fd,
                referenced_files,
                max_entries=_MAX_UPLOADS,
                max_bytes=_MAX_MANAGED_WORKSPACE_BYTES,
            )
            _verify_managed_disk_quota(
                workspace_root_fd,
                set(snapshot_sources),
                max_entries=100_000 + _MAX_UPLOADS,
                max_bytes=_MAX_MANAGED_WORKSPACE_BYTES,
            )

    def _recover_open_upload(
        self,
        upload_root_fd: int,
        file_name: str,
        *,
        accepted_offset: int,
    ) -> None:
        try:
            fd = os.open(
                file_name,
                os.O_RDWR | getattr(os, "O_NOFOLLOW", 0),
                dir_fd=upload_root_fd,
            )
        except OSError as exc:
            raise StoreCorruptionError("workspace upload file is missing or unsafe") from exc
        try:
            metadata = os.fstat(fd)
            _require_private_regular_metadata(metadata)
            _require_entry_binding(upload_root_fd, file_name, metadata)
            if metadata.st_size < accepted_offset:
                raise StoreCorruptionError("workspace upload file is shorter than its offset")
            if metadata.st_size > accepted_offset:
                os.ftruncate(fd, accepted_offset)
                os.fsync(fd)
            _require_bound_regular_entry(
                upload_root_fd,
                file_name,
                fd,
                expected_size=accepted_offset,
            )
        finally:
            os.close(fd)

    def _reconcile_unknown_workspace_chunk_commit(
        self,
        expectation: _WorkspaceChunkCommitExpectation,
        file_fd: int,
    ) -> bool:
        fresh_connection: sqlite3.Connection | None = None
        try:
            self._verify_lifecycle_storage()
            fresh_connection = self._open_database_connection()
            outcome = self._workspace_chunk_commit_outcome(fresh_connection, expectation)
            self._verify_workspace_chunk_commit_file(expectation, file_fd)
            self._verify_lifecycle_storage()

            uncertain_connection = self._connection
            uncertain_connection.close()
            self._connection = fresh_connection
            fresh_connection = None

            confirmed_outcome = self._workspace_chunk_commit_outcome(self._connection, expectation)
            if confirmed_outcome != outcome:
                raise StoreCorruptionError(
                    "workspace chunk commit state changed during reconciliation"
                )
            self._verify_workspace_chunk_commit_file(expectation, file_fd)
            self._verify_lifecycle_storage()
            return outcome == "committed"
        except CommitOutcomeUnknownError:
            raise
        except Exception as exc:
            if fresh_connection is not None:
                try:
                    fresh_connection.close()
                except Exception:
                    pass
            raise CommitOutcomeUnknownError(
                "workspace chunk commit outcome could not be reconciled"
            ) from exc

    def _workspace_chunk_commit_outcome(
        self,
        connection: sqlite3.Connection,
        expectation: _WorkspaceChunkCommitExpectation,
    ) -> Literal["committed", "rolled_back"]:
        connection.execute("BEGIN")
        try:
            success_row = connection.execute(
                "SELECT * FROM idempotency_records WHERE operation_id = ? "
                "AND resource_scope = ? AND idempotency_key = ?",
                (
                    expectation.operation_id,
                    expectation.scope,
                    expectation.idempotency_key,
                ),
            ).fetchone()
            upload_row = connection.execute(
                "SELECT * FROM workspace_uploads WHERE upload_id = ?",
                (expectation.upload_id,),
            ).fetchone()
        except Exception:
            connection.execute("ROLLBACK")
            raise
        connection.execute("COMMIT")

        if upload_row is None:
            raise StoreCorruptionError(
                "workspace chunk upload disappeared during commit reconciliation"
            )
        common_upload_state = (
            upload_row["upload_id"] == expectation.upload_id
            and upload_row["project_id"] == expectation.project_id
            and upload_row["file_name"] == expectation.file_name
            and upload_row["created_at"] == expectation.created_at
        )
        old_upload_state = (
            common_upload_state
            and bytes(upload_row["document_json"]) == expectation.old_document_json
            and int(upload_row["resource_version"]) == expectation.old_resource_version
            and upload_row["updated_at"] == expectation.old_updated_at
        )
        new_upload_state = (
            common_upload_state
            and bytes(upload_row["document_json"]) == expectation.new_document_json
            and int(upload_row["resource_version"]) == expectation.new_resource_version
            and upload_row["updated_at"] == expectation.new_updated_at
        )

        if success_row is None:
            if old_upload_state:
                return "rolled_back"
            raise StoreCorruptionError(
                "workspace chunk commit has an ambiguous durable upload row"
            )

        _validate_idempotency_row(success_row)
        exact_success = (
            hmac.compare_digest(success_row["request_digest"], expectation.request_digest)
            and bytes(success_row["request_json"]) == expectation.request_json
            and bytes(success_row["semantic_headers_json"]) == expectation.semantic_headers_json
            and int(success_row["status_code"]) == expectation.result.status_code
            and success_row["response_type"] == m.WorkspaceUploadSessionV1.__name__
            and bytes(success_row["response_json"]) == expectation.new_document_json
            and success_row["etag"] == expectation.result.etag
        )
        if not exact_success or not new_upload_state:
            raise StoreCorruptionError(
                "workspace chunk committed state does not match the attempted transaction"
            )
        return "committed"

    def _verify_workspace_chunk_commit_file(
        self,
        expectation: _WorkspaceChunkCommitExpectation,
        file_fd: int,
    ) -> None:
        expected_size = expectation.old_offset + len(expectation.content)
        _require_bound_regular_entry(
            self._upload_root_fd,
            expectation.file_name,
            file_fd,
            expected_size=expected_size,
        )
        if _pread_exact(file_fd, len(expectation.content), expectation.old_offset) != (
            expectation.content
        ):
            raise StoreCorruptionError(
                "workspace chunk bytes do not match the attempted transaction"
            )
        _require_bound_regular_entry(
            self._upload_root_fd,
            expectation.file_name,
            file_fd,
            expected_size=expected_size,
        )

    def _open_parent_anchor(self) -> None:
        self._state_parent.mkdir(parents=True, exist_ok=True)
        directory_flags = (
            os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
        )
        try:
            self._parent_fd = os.open(self._state_parent, directory_flags)
            parent_identity = os.fstat(self._parent_fd)
            _require_owner_directory_metadata(parent_identity, label="state parent")
            _require_path_binding(self._state_parent, parent_identity)
            self._parent_identity = parent_identity
        except OSError as exc:
            self._close_lifecycle_storage()
            raise CoreControlStoreError("Core Control state parent is unavailable") from exc
        try:
            fcntl.flock(self._parent_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            self._close_lifecycle_storage()
            raise CoreControlStoreError("Core Control provider state is already owned") from exc

    def _open_lifecycle_storage(self) -> None:
        directory_flags = (
            os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
        )
        try:
            self._root_fd = os.open("core-control-v1", directory_flags, dir_fd=self._parent_fd)
            root_identity = os.fstat(self._root_fd)
            _require_private_directory_metadata(root_identity)
            _require_entry_binding(self._parent_fd, "core-control-v1", root_identity)
            self._root_identity = root_identity
        except OSError as exc:
            self._close_lifecycle_storage()
            raise CoreControlStoreError("Core Control provider root is unavailable") from exc
        try:
            fcntl.flock(self._root_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            self._close_lifecycle_storage()
            raise CoreControlStoreError("Core Control provider state is already owned") from exc

        lock_created = False
        try:
            try:
                self._lock_fd = os.open(
                    "provider.lock",
                    os.O_RDWR | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
                    0o600,
                    dir_fd=self._root_fd,
                )
                lock_created = True
            except FileExistsError:
                self._lock_fd = os.open(
                    "provider.lock",
                    os.O_RDWR | getattr(os, "O_NOFOLLOW", 0),
                    dir_fd=self._root_fd,
                )
            if lock_created:
                os.fchmod(self._lock_fd, 0o600)
                os.fsync(self._lock_fd)
                os.fsync(self._root_fd)
            lock_identity = os.fstat(self._lock_fd)
            _require_private_regular_metadata(lock_identity)
            _require_entry_binding(self._root_fd, "provider.lock", lock_identity)
            try:
                fcntl.flock(self._lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as exc:
                raise CoreControlStoreError(
                    "Core Control provider state is already owned"
                ) from exc
            self._lock_identity = lock_identity
            self._open_existing_identity_marker()
            self._verify_lifecycle_storage()
        except Exception:
            self._close_lifecycle_storage()
            raise

    def _verify_lifecycle_storage(self) -> None:
        if self._closed:
            raise StoreCorruptionError("Core Control store is closed")
        try:
            parent_identity = os.fstat(self._parent_fd)
            root_identity = os.fstat(self._root_fd)
            lock_identity = os.fstat(self._lock_fd)
            _require_owner_directory_metadata(parent_identity, label="state parent")
            _require_private_directory_metadata(root_identity)
            _require_private_regular_metadata(lock_identity)
            _require_same_identity(parent_identity, self._parent_identity, "state parent")
            _require_same_identity(root_identity, self._root_identity, "provider root")
            _require_same_identity(lock_identity, self._lock_identity, "provider lock")
            _require_path_binding(self._state_parent, self._parent_identity)
            _require_entry_binding(self._parent_fd, "core-control-v1", self._root_identity)
            _require_path_binding(self.root, self._root_identity)
            _require_entry_binding(self._root_fd, "provider.lock", self._lock_identity)
            marker_fd = getattr(self, "_marker_fd", None)
            if marker_fd is not None:
                marker_identity = os.fstat(marker_fd)
                _require_private_regular_metadata(marker_identity)
                _require_same_identity(
                    marker_identity, self._marker_identity, "store identity marker"
                )
                _require_entry_binding(
                    self._root_fd, _STORE_IDENTITY_MARKER, self._marker_identity
                )
                expected_marker = getattr(self, "_expected_marker_bytes", None)
                if expected_marker is not None:
                    self._verify_store_identity_marker(expected_marker)
            if hasattr(self, "_upload_root_fd"):
                upload_identity = os.fstat(self._upload_root_fd)
                workspace_identity = os.fstat(self._workspace_root_fd)
                _require_private_directory_metadata(upload_identity)
                _require_private_directory_metadata(workspace_identity)
                _require_same_identity(
                    upload_identity, self._upload_root_identity, "upload root"
                )
                _require_same_identity(
                    workspace_identity, self._workspace_root_identity, "workspace root"
                )
                _require_entry_binding(
                    self._root_fd, "workspace-uploads", self._upload_root_identity
                )
                _require_entry_binding(
                    self._root_fd, "workspace-snapshots", self._workspace_root_identity
                )
            self._verify_database_authority()
        except OSError as exc:
            raise StoreCorruptionError("Core Control lifecycle storage is unavailable") from exc

    def _close_lifecycle_storage(self) -> None:
        for descriptor in self._database_fds.values():
            try:
                os.close(descriptor)
            except OSError:
                pass
        self._database_fds.clear()
        self._database_identities.clear()
        for name in ("_workspace_root_fd", "_upload_root_fd"):
            descriptor = getattr(self, name, None)
            if descriptor is not None:
                try:
                    os.close(descriptor)
                except OSError:
                    pass
                delattr(self, name)
        marker_fd = getattr(self, "_marker_fd", None)
        if marker_fd is not None:
            try:
                os.close(marker_fd)
            except OSError:
                pass
            del self._marker_fd
        lock_fd = getattr(self, "_lock_fd", None)
        if lock_fd is not None:
            try:
                try:
                    fcntl.flock(lock_fd, fcntl.LOCK_UN)
                except OSError:
                    pass
            finally:
                try:
                    os.close(lock_fd)
                except OSError:
                    pass
                del self._lock_fd
        root_fd = getattr(self, "_root_fd", None)
        if root_fd is not None:
            try:
                try:
                    fcntl.flock(root_fd, fcntl.LOCK_UN)
                except OSError:
                    pass
            finally:
                try:
                    os.close(root_fd)
                except OSError:
                    pass
                del self._root_fd
        parent_fd = getattr(self, "_parent_fd", None)
        if parent_fd is not None:
            try:
                try:
                    fcntl.flock(parent_fd, fcntl.LOCK_UN)
                except OSError:
                    pass
            finally:
                try:
                    os.close(parent_fd)
                except OSError:
                    pass
                del self._parent_fd

    def _prepare_root(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        metadata = self.root.stat(follow_symlinks=False)
        if (
            not stat.S_ISDIR(metadata.st_mode)
            or metadata.st_uid != os.geteuid()
            or stat.S_IMODE(metadata.st_mode) != 0o700
        ):
            raise CoreControlStoreError("Core Control provider root is not privately owned")

    def _open_existing_identity_marker(self) -> None:
        try:
            marker_fd = os.open(
                _STORE_IDENTITY_MARKER,
                os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0),
                dir_fd=self._root_fd,
            )
        except FileNotFoundError:
            return
        try:
            marker_identity = os.fstat(marker_fd)
            _require_private_regular_metadata(marker_identity)
            if marker_identity.st_size > _MAX_STORE_IDENTITY_MARKER_BYTES:
                raise StoreCorruptionError("Core Control store identity marker is oversized")
            _require_entry_binding(self._root_fd, _STORE_IDENTITY_MARKER, marker_identity)
        except Exception:
            os.close(marker_fd)
            raise
        self._marker_fd = marker_fd
        self._marker_identity = marker_identity

    def _prepare_managed_roots(self) -> None:
        directory_flags = (
            os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
        )
        created = False
        for name in ("workspace-uploads", "workspace-snapshots"):
            try:
                os.mkdir(name, mode=0o700, dir_fd=self._root_fd)
                created = True
            except FileExistsError:
                pass
        if created:
            os.fsync(self._root_fd)
        try:
            self._upload_root_fd = os.open(
                "workspace-uploads", directory_flags, dir_fd=self._root_fd
            )
            self._upload_root_identity = os.fstat(self._upload_root_fd)
            _require_private_directory_metadata(self._upload_root_identity)
            _require_entry_binding(
                self._root_fd, "workspace-uploads", self._upload_root_identity
            )
            self._workspace_root_fd = os.open(
                "workspace-snapshots", directory_flags, dir_fd=self._root_fd
            )
            self._workspace_root_identity = os.fstat(self._workspace_root_fd)
            _require_private_directory_metadata(self._workspace_root_identity)
            _require_entry_binding(
                self._root_fd, "workspace-snapshots", self._workspace_root_identity
            )
            self._verify_lifecycle_storage()
        except Exception:
            for attribute in ("_workspace_root_fd", "_upload_root_fd"):
                descriptor = getattr(self, attribute, None)
                if descriptor is not None:
                    os.close(descriptor)
                    delattr(self, attribute)
            raise

    def _initialize_or_verify_store_identity(self) -> None:
        fingerprint = _schema_fingerprint(self._connection)
        empty_fingerprint = _expected_schema_fingerprint(())
        legacy_fingerprint = _expected_schema_fingerprint(_LEGACY_SCHEMA)
        current_fingerprint = _expected_schema_fingerprint(_SCHEMA)
        if fingerprint == empty_fingerprint:
            if hasattr(self, "_marker_fd"):
                raise StoreCorruptionError(
                    "Core Control store identity marker has no database identity"
                )
            self._require_unbound_managed_roots_empty()
            self._create_pending_store_identity(include_base_schema=True)
        elif fingerprint == legacy_fingerprint:
            if hasattr(self, "_marker_fd"):
                raise StoreCorruptionError(
                    "Core Control legacy store identity conflicts with a root marker"
                )
            self._verify_database_integrity()
            self._require_legacy_migration_is_unconflicted()
            self._require_unbound_managed_roots_empty()
            self._create_pending_store_identity(include_base_schema=False)
        elif fingerprint != current_fingerprint:
            raise StoreCorruptionError(
                "Core Control schema fingerprint is incompatible; provider state is fresh-only"
            )

        row = self._read_store_identity_row()
        self._require_store_identity_root(row)
        self._store_id = row["store_id"]
        if row["binding_state"] == "pending":
            self._require_pending_database_is_unconflicted()
            self._require_unbound_managed_roots_empty()
            marker_identity = self._ensure_store_identity_marker(row)
            with self._transaction():
                current = self._read_store_identity_row()
                if current["binding_state"] != "pending" or current["store_id"] != self._store_id:
                    raise StoreCorruptionError(
                        "Core Control pending store identity changed during binding"
                    )
                self._connection.execute(
                    "UPDATE store_identity SET binding_state = 'bound', marker_dev = ?, "
                    "marker_ino = ? WHERE singleton = 1 AND binding_state = 'pending'",
                    (marker_identity.st_dev, marker_identity.st_ino),
                )
            row = self._read_store_identity_row()
        self._verify_bound_store_identity(row)
        self._expected_marker_bytes = self._store_identity_marker_bytes(row)

    def _create_pending_store_identity(self, *, include_base_schema: bool) -> None:
        store_id = secrets.token_hex(_STORE_ID_BYTES)
        with self._transaction():
            statements = _SCHEMA if include_base_schema else (_STORE_IDENTITY_SCHEMA,)
            for statement in statements:
                self._connection.execute(statement)
            self._connection.execute(
                "INSERT INTO store_identity(singleton, store_id, binding_state, root_dev, "
                "root_ino, marker_dev, marker_ino) VALUES (1, ?, 'pending', ?, ?, NULL, NULL)",
                (store_id, self._root_identity.st_dev, self._root_identity.st_ino),
            )
            self._verify_schema_fingerprint()

    def _read_store_identity_row(self) -> sqlite3.Row:
        aggregate = self._connection.execute(
            "SELECT COUNT(*), COALESCE(SUM(length(CAST(store_id AS BLOB))), 0), "
            "COALESCE(SUM(length(CAST(binding_state AS BLOB))), 0) FROM store_identity"
        ).fetchone()
        if aggregate is None or int(aggregate[0]) != 1:
            raise StoreCorruptionError("Core Control store identity row is invalid")
        if int(aggregate[1]) != _STORE_ID_HEX_LENGTH or int(aggregate[2]) > 8:
            raise StoreCorruptionError("Core Control store identity row is invalid")
        row = self._connection.execute(
            "SELECT singleton, "
            f"CASE WHEN length(CAST(store_id AS BLOB)) = {_STORE_ID_HEX_LENGTH} "
            "THEN store_id END AS store_id, "
            "CASE WHEN length(CAST(binding_state AS BLOB)) <= 8 "
            "THEN binding_state END AS binding_state, "
            "root_dev, root_ino, marker_dev, marker_ino FROM store_identity LIMIT 2"
        ).fetchone()
        if (
            row is None
            or row["singleton"] != 1
            or not _is_store_id(row["store_id"])
            or row["binding_state"] not in {"pending", "bound"}
            or not isinstance(row["root_dev"], int)
            or row["root_dev"] < 0
            or not isinstance(row["root_ino"], int)
            or row["root_ino"] <= 0
        ):
            raise StoreCorruptionError("Core Control store identity row is invalid")
        marker_values = (row["marker_dev"], row["marker_ino"])
        if row["binding_state"] == "pending":
            if marker_values != (None, None):
                raise StoreCorruptionError("Core Control pending store identity is invalid")
        elif (
            not isinstance(marker_values[0], int)
            or marker_values[0] < 0
            or not isinstance(marker_values[1], int)
            or marker_values[1] <= 0
        ):
            raise StoreCorruptionError("Core Control bound store identity is invalid")
        return row

    def _require_store_identity_root(self, row: sqlite3.Row) -> None:
        if (row["root_dev"], row["root_ino"]) != (
            self._root_identity.st_dev,
            self._root_identity.st_ino,
        ):
            raise StoreCorruptionError(
                "Core Control store identity is bound to a different provider root"
            )

    def _store_identity_marker_bytes(self, row: sqlite3.Row) -> bytes:
        return _canonical_bytes(
            {
                "root_dev": row["root_dev"],
                "root_ino": row["root_ino"],
                "schema_version": "1",
                "store_id": row["store_id"],
            }
        )

    def _ensure_store_identity_marker(self, row: sqlite3.Row) -> os.stat_result:
        expected = self._store_identity_marker_bytes(row)
        if not hasattr(self, "_marker_fd"):
            temporary_name = f".{_STORE_IDENTITY_MARKER}.{secrets.token_hex(16)}.tmp"
            marker_fd = os.open(
                temporary_name,
                os.O_RDWR
                | os.O_CREAT
                | os.O_EXCL
                | getattr(os, "O_NOFOLLOW", 0),
                0o600,
                dir_fd=self._root_fd,
            )
            try:
                os.fchmod(marker_fd, 0o600)
                _write_all(marker_fd, expected)
                os.fsync(marker_fd)
                marker_identity = os.fstat(marker_fd)
                _require_private_regular_metadata(marker_identity)
                _require_entry_binding(
                    self._root_fd, temporary_name, marker_identity
                )
                _rename_noreplace(
                    temporary_name,
                    _STORE_IDENTITY_MARKER,
                    directory_fd=self._root_fd,
                )
                os.fsync(self._root_fd)
                _require_entry_binding(
                    self._root_fd, _STORE_IDENTITY_MARKER, marker_identity
                )
            except Exception:
                os.close(marker_fd)
                raise
            self._marker_fd = marker_fd
            self._marker_identity = marker_identity
        self._verify_store_identity_marker(expected)
        return self._marker_identity

    def _verify_bound_store_identity(self, row: sqlite3.Row) -> None:
        if row["binding_state"] != "bound" or not hasattr(self, "_marker_fd"):
            raise StoreCorruptionError(
                "Core Control bound store identity marker is missing"
            )
        if (row["marker_dev"], row["marker_ino"]) != (
            self._marker_identity.st_dev,
            self._marker_identity.st_ino,
        ):
            raise StoreCorruptionError(
                "Core Control store identity marker inode does not match the database"
            )
        self._verify_store_identity_marker(self._store_identity_marker_bytes(row))

    def _verify_store_identity_marker(self, expected: bytes) -> None:
        marker_fd = getattr(self, "_marker_fd", None)
        if marker_fd is None:
            raise StoreCorruptionError("Core Control store identity marker is missing")
        before = os.fstat(marker_fd)
        _require_private_regular_metadata(before)
        _require_same_identity(before, self._marker_identity, "store identity marker")
        _require_entry_binding(self._root_fd, _STORE_IDENTITY_MARKER, self._marker_identity)
        if before.st_size != len(expected) or os.pread(marker_fd, len(expected) + 1, 0) != expected:
            raise StoreCorruptionError("Core Control store identity marker content is invalid")
        after = os.fstat(marker_fd)
        _require_same_file_state(after, before, "store identity marker")
        _require_entry_binding(self._root_fd, _STORE_IDENTITY_MARKER, self._marker_identity)

    def _require_unbound_managed_roots_empty(self) -> None:
        directory_flags = (
            os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
        )
        for name in ("workspace-uploads", "workspace-snapshots"):
            try:
                directory_fd = os.open(name, directory_flags, dir_fd=self._root_fd)
            except FileNotFoundError:
                continue
            try:
                identity = os.fstat(directory_fd)
                _require_private_directory_metadata(identity)
                _require_entry_binding(self._root_fd, name, identity)
                with os.scandir(directory_fd) as entries:
                    if next(entries, None) is not None:
                        raise StoreCorruptionError(
                            "Core Control unbound managed state cannot be claimed"
                        )
                _require_entry_binding(self._root_fd, name, identity)
            finally:
                os.close(directory_fd)

    def _require_legacy_migration_is_unconflicted(self) -> None:
        self._require_database_has_no_business_rows("legacy")

    def _require_pending_database_is_unconflicted(self) -> None:
        self._require_database_has_no_business_rows("pending identity")

    def _require_database_has_no_business_rows(self, label: str) -> None:
        for table in (
            "projects",
            "workspace_uploads",
            "workspace_publication_owners",
            "idempotency_records",
            "managed_cleanup_intents",
            "failed_idempotency_records",
            "events",
        ):
            row = self._connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()
            if row is None or int(row[0]) != 0:
                raise StoreCorruptionError(
                    f"Core Control {label} has state that cannot be identity-bound"
                )

    def _load_or_create_signing_key(self) -> bytes:
        aggregate = self._connection.execute(
            "SELECT COUNT(*), "
            "COALESCE(SUM(length(CAST(key AS BLOB))), 0), "
            "COALESCE(SUM(length(CAST(value AS BLOB))), 0) FROM metadata"
        ).fetchone()
        if aggregate is None:
            raise StoreCorruptionError("Core Control metadata is unavailable")
        row_count = int(aggregate[0])
        total_bytes = int(aggregate[1]) + int(aggregate[2])
        if row_count > 1 or total_bytes > _MAX_METADATA_BYTES:
            raise StoreCorruptionError("Core Control metadata quota is exceeded")
        if row_count:
            row = self._connection.execute(
                "SELECT CASE WHEN length(CAST(key AS BLOB)) <= 64 THEN key END AS key, "
                "CASE WHEN length(CAST(value AS BLOB)) <= 32 THEN value END AS value "
                "FROM metadata LIMIT 2"
            ).fetchone()
            if (
                row is None
                or row["key"] != "signing_key"
                or row["value"] is None
                or len(bytes(row["value"])) != 32
            ):
                raise StoreCorruptionError("Core Control metadata is invalid")
            return bytes(row["value"])
        value = secrets.token_bytes(32)
        self._connection.execute(
            "INSERT INTO metadata(key, value) VALUES ('signing_key', ?)", (value,)
        )
        return value

    def _prepare_database_authority(self) -> None:
        name = "provider.sqlite3"
        flags = os.O_RDWR | getattr(os, "O_NOFOLLOW", 0)
        try:
            fd = os.open(name, flags | os.O_CREAT | os.O_EXCL, 0o600, dir_fd=self._root_fd)
        except FileExistsError:
            fd = os.open(name, flags, dir_fd=self._root_fd)
        try:
            metadata = os.fstat(fd)
            _require_private_regular_metadata(metadata)
            _require_entry_binding(self._root_fd, name, metadata)
        except Exception:
            os.close(fd)
            raise
        self._database_fds[name] = fd
        self._database_identities[name] = metadata
        for suffix in ("-wal", "-shm"):
            sidecar = name + suffix
            try:
                sidecar_fd = os.open(sidecar, flags, dir_fd=self._root_fd)
            except FileNotFoundError:
                continue
            try:
                sidecar_metadata = os.fstat(sidecar_fd)
                _require_private_regular_metadata(sidecar_metadata)
                _require_entry_binding(self._root_fd, sidecar, sidecar_metadata)
            except Exception:
                os.close(sidecar_fd)
                raise
            self._database_fds[sidecar] = sidecar_fd
            self._database_identities[sidecar] = sidecar_metadata

    def _bind_database_sidecars(self) -> None:
        for suffix in ("-wal", "-shm"):
            name = "provider.sqlite3" + suffix
            if name in self._database_fds:
                continue
            try:
                fd = os.open(
                    name,
                    os.O_RDWR | getattr(os, "O_NOFOLLOW", 0),
                    dir_fd=self._root_fd,
                )
            except OSError as exc:
                raise StoreCorruptionError(
                    "Core Control SQLite sidecar is missing or unsafe"
                ) from exc
            try:
                metadata = os.fstat(fd)
                _require_private_regular_metadata(metadata)
                _require_entry_binding(self._root_fd, name, metadata)
            except Exception:
                os.close(fd)
                raise
            self._database_fds[name] = fd
            self._database_identities[name] = metadata
        self._verify_database_authority()

    def _verify_database_authority(self) -> None:
        if not self._database_fds:
            return
        for name, fd in self._database_fds.items():
            current = os.fstat(fd)
            try:
                _require_private_regular_metadata(current)
            except StoreCorruptionError as exc:
                raise StoreCorruptionError(f"Core Control database file {name} is unsafe") from exc
            _require_same_identity(current, self._database_identities[name], f"database {name}")
            _require_entry_binding(self._root_fd, name, self._database_identities[name])
            if name.endswith("-wal") and current.st_size > _MAX_WAL_BYTES:
                raise StoreCorruptionError("Core Control database WAL quota is exceeded")
            if name == "provider.sqlite3" and current.st_size > _MAX_DATABASE_BYTES:
                raise StoreCorruptionError("Core Control database size quota is exceeded")

    def _verify_schema_fingerprint(self) -> None:
        if _schema_fingerprint(self._connection) != _expected_schema_fingerprint():
            raise StoreCorruptionError(
                "Core Control schema fingerprint is incompatible; provider state is fresh-only"
            )

    def _verify_database_integrity(self) -> None:
        self._verify_database_authority()
        integrity = self._connection.execute("PRAGMA integrity_check(1)")
        row = integrity.fetchone()
        if row is None or row[0] != "ok" or integrity.fetchone() is not None:
            raise StoreCorruptionError("Core Control database integrity check failed")
        if self._connection.execute("PRAGMA foreign_key_check").fetchone() is not None:
            raise StoreCorruptionError("Core Control database foreign key check failed")
        page_size = int(self._connection.execute("PRAGMA page_size").fetchone()[0])
        page_count = int(self._connection.execute("PRAGMA page_count").fetchone()[0])
        if page_size <= 0 or page_count * page_size > _MAX_DATABASE_BYTES:
            raise StoreCorruptionError("Core Control database page quota is exceeded")

    def _resource_identity_hmac(self, kind: str, resource_id: str) -> str:
        return hmac.new(
            self._signing_key,
            f"resource.v1:{kind}:{resource_id}".encode("ascii"),
            hashlib.sha256,
        ).hexdigest()

    def _open_database_connection(self) -> sqlite3.Connection:
        authority_fd = self._database_fds.get("provider.sqlite3")
        if authority_fd is None:
            raise StoreCorruptionError("Core Control database authority is unavailable")
        descriptor_path = f"/proc/self/fd/{authority_fd}"
        try:
            descriptor_identity = os.stat(descriptor_path, follow_symlinks=True)
        except OSError as exc:
            raise CoreControlStoreError(
                "Core Control cannot attach SQLite to its held database authority"
            ) from exc
        _require_same_identity(
            descriptor_identity,
            self._database_identities["provider.sqlite3"],
            "database descriptor",
        )

        last_error: Exception | None = None
        for _attempt in range(2):
            self._verify_database_authority()
            connection: sqlite3.Connection | None = None
            try:
                connection = sqlite3.connect(
                    f"file:{descriptor_path}?mode=rw",
                    uri=True,
                    isolation_level=None,
                    check_same_thread=False,
                    timeout=30,
                )
                connection.row_factory = sqlite3.Row
                database_rows = connection.execute("PRAGMA database_list").fetchmany(2)
                if len(database_rows) != 1 or database_rows[0][1] != "main":
                    raise _DatabaseAttachRaceError(
                        "Core Control SQLite connection identity is invalid"
                    )
                resolved_database = Path(database_rows[0][2])
                if resolved_database != self.root / "provider.sqlite3":
                    raise _DatabaseAttachRaceError(
                        "Core Control SQLite connection path changed during attach"
                    )
                self._verify_database_authority()
                schema_row = connection.execute("SELECT 1 FROM sqlite_schema LIMIT 1").fetchone()
                if (
                    schema_row is not None
                    and _schema_fingerprint(connection)
                    not in {
                        _expected_schema_fingerprint(_LEGACY_SCHEMA),
                        _expected_schema_fingerprint(_SCHEMA),
                    }
                ):
                    raise StoreCorruptionError(
                        "Core Control schema fingerprint is incompatible; "
                        "provider state is fresh-only"
                    )
                connection.execute("PRAGMA foreign_keys = ON")
                self._verify_database_authority()
                return connection
            except (_DatabaseAttachRaceError, sqlite3.Error) as exc:
                last_error = exc
                if connection is not None:
                    connection.close()
                self._verify_database_authority()
            except Exception:
                if connection is not None:
                    connection.close()
                raise
        raise StoreCorruptionError(
            "Core Control SQLite connection could not bind its database authority"
        ) from last_error

    def _configure_database_connection(self) -> None:
        self._verify_lifecycle_storage()
        self._connection.execute("PRAGMA journal_mode = WAL")
        self._connection.execute("PRAGMA synchronous = FULL")
        page_size = int(self._connection.execute("PRAGMA page_size").fetchone()[0])
        if page_size <= 0:
            raise StoreCorruptionError("Core Control database page size is invalid")
        self._connection.execute(f"PRAGMA max_page_count = {_MAX_DATABASE_BYTES // page_size}")
        self._verify_lifecycle_storage()

    def _require_etag(self, current: str, supplied: str, resource_type: str) -> None:
        if not hmac.compare_digest(current, supplied):
            raise ETagPreconditionError(resource_type)

    def _timestamp(self) -> str:
        return (
            self._clock()
            .astimezone(timezone.utc)
            .isoformat(timespec="microseconds")
            .replace("+00:00", "Z")
        )

    def _transaction(self):
        return _Transaction(self._connection, self._verify_lifecycle_storage)


class _Transaction:
    def __init__(
        self,
        connection: sqlite3.Connection,
        verify_storage: Callable[[], None],
    ) -> None:
        self.connection = connection
        self.verify_storage = verify_storage
        self.outcome: Literal["pending", "rolled_back", "committed", "unknown"] = "pending"

    def __enter__(self):
        self.verify_storage()
        self.connection.execute("BEGIN IMMEDIATE")
        return self

    def __exit__(self, exc_type, exc, traceback) -> bool:
        if exc_type is not None:
            try:
                self.connection.execute("ROLLBACK")
            except sqlite3.Error as rollback_exc:
                self.outcome = "unknown"
                raise CommitOutcomeUnknownError(
                    "Core Control transaction rollback outcome is unknown"
                ) from rollback_exc
            else:
                self.outcome = "rolled_back"
            return False
        try:
            self.verify_storage()
        except Exception:
            self.connection.execute("ROLLBACK")
            self.outcome = "rolled_back"
            raise
        try:
            self.connection.execute("COMMIT")
        except Exception as exc:
            self.outcome = "unknown"
            raise CommitOutcomeUnknownError(
                "Core Control transaction commit outcome is unknown"
            ) from exc
        except BaseException:
            self.outcome = "unknown"
            raise
        self.outcome = "committed"
        try:
            self._verify_after_commit()
        except Exception as exc:
            raise PostCommitStoreError(
                "committed Core Control state failed lifecycle verification"
            ) from exc
        return False

    def _verify_after_commit(self) -> None:
        self.verify_storage()


def _new_id(prefix: str) -> str:
    return f"{prefix}-{secrets.token_hex(16)}"


def _schema_fingerprint(connection: sqlite3.Connection) -> bytes:
    aggregate = connection.execute(
        "SELECT COUNT(*), "
        "COALESCE(SUM(length(CAST(type AS BLOB))), 0), "
        "COALESCE(SUM(length(CAST(name AS BLOB))), 0), "
        "COALESCE(SUM(length(CAST(tbl_name AS BLOB))), 0), "
        "COALESCE(SUM(length(CAST(sql AS BLOB))), 0) FROM sqlite_schema"
    ).fetchone()
    if aggregate is None:
        raise StoreCorruptionError("Core Control schema fingerprint is unavailable")
    row_count = int(aggregate[0])
    byte_count = sum(int(aggregate[index]) for index in range(1, 5))
    if row_count > 64 or byte_count > _MAX_SCHEMA_BYTES:
        raise StoreCorruptionError("Core Control schema fingerprint is oversized")
    rows = connection.execute(
        "SELECT "
        "CASE WHEN length(CAST(type AS BLOB)) <= 32 THEN type END, "
        "CASE WHEN length(CAST(name AS BLOB)) <= 256 THEN name END, "
        "CASE WHEN length(CAST(tbl_name AS BLOB)) <= 256 THEN tbl_name END, "
        f"CASE WHEN length(CAST(sql AS BLOB)) <= {_MAX_SCHEMA_BYTES} THEN sql END "
        "FROM sqlite_schema ORDER BY type, name"
    )
    schema: list[list[object]] = []
    while True:
        page = rows.fetchmany(_RECOVERY_PAGE_SIZE)
        if not page:
            break
        schema.extend([[row[0], row[1], row[2], row[3]] for row in page])
        if len(schema) > 64 or any(value is None for row in schema for value in row[:3]):
            raise StoreCorruptionError("Core Control schema fingerprint is oversized")
    if len(schema) != row_count:
        raise StoreCorruptionError("Core Control schema fingerprint changed during read")
    return _canonical_bytes(schema)


def _expected_schema_fingerprint(schema: tuple[str, ...] = _SCHEMA) -> bytes:
    connection = sqlite3.connect(":memory:")
    try:
        for statement in schema:
            connection.execute(statement)
        return _schema_fingerprint(connection)
    finally:
        connection.close()


def _snapshot(
    signing_key: bytes,
    kind: m.SnapshotKind,
    payload: object,
    now: str,
) -> m.ImmutableSnapshotRefV1:
    return _snapshot_from_digest(
        signing_key,
        kind,
        hashlib.sha256(_canonical_bytes(payload)).hexdigest(),
        now,
    )


def _snapshot_from_digest(
    signing_key: bytes,
    kind: m.SnapshotKind,
    content_sha256: str,
    now: str,
) -> m.ImmutableSnapshotRefV1:
    return m.ImmutableSnapshotRefV1(
        id=_core_owned_id(
            signing_key,
            f"{kind.value}-snapshot",
            {"content_sha256": content_sha256, "created_at": now},
        ),
        kind=kind,
        content_sha256=content_sha256,
        created_at=now,
    )


def _workspace_publication_snapshot(
    signing_key: bytes,
    *,
    project_id: str,
    upload_id: str,
    archive_sha256: str,
    now: str,
) -> m.ImmutableSnapshotRefV1:
    return _snapshot(
        signing_key,
        m.SnapshotKind.WORKSPACE,
        {
            "project_id": project_id,
            "upload_id": upload_id,
            "archive_sha256": archive_sha256,
        },
        now,
    )


def _core_owned_id(signing_key: bytes, prefix: str, payload: object) -> str:
    digest = hmac.new(
        signing_key,
        _canonical_bytes({"domain": f"{prefix}.v1", "payload": payload}),
        hashlib.sha256,
    ).hexdigest()[:32]
    return f"{prefix}-{digest}"


def _project_snapshot_payload(data: Mapping[str, Any]) -> dict[str, Any]:
    return {
        key: _json_value(value)
        for key, value in data.items()
        if key
        not in {
            "current_project_snapshot",
            "etag",
            "created_at",
            "updated_at",
            "status",
            "active_revision",
            "registry_digest",
            "model_preparation",
        }
    }


def _etag(payload: object, *, version: int) -> str:
    digest = hashlib.sha256(
        _canonical_bytes({"resource_version": version, "resource": _json_value(payload)})
    ).hexdigest()
    return f'"{digest}"'


def _model_with_etag(
    model_type: type[_ModelT],
    data: Mapping[str, Any],
    *,
    version: int,
) -> _ModelT:
    placeholder = '"' + ("0" * 64) + '"'
    provisional = model_type.model_validate({**data, "etag": placeholder})
    canonical_data = provisional.model_dump(mode="python", exclude={"etag"})
    return model_type.model_validate(
        {**canonical_data, "etag": _etag(canonical_data, version=version)}
    )


def _idempotency_envelope(
    operation_id: str,
    scope: str,
    request: BaseModel | None,
    semantic_headers: Mapping[str, str],
) -> _IdempotencyRequestEnvelope:
    request_json = _canonical_bytes(
        request.model_dump(mode="json", exclude_unset=True) if request is not None else None
    )
    headers_json = _canonical_bytes(dict(sorted(semantic_headers.items())))
    return _IdempotencyRequestEnvelope(
        digest=_idempotency_request_digest(operation_id, scope, request_json, headers_json),
        request_json=request_json,
        semantic_headers_json=headers_json,
    )


def _idempotency_request_digest(
    operation_id: str,
    scope: str,
    request_json: bytes,
    semantic_headers_json: bytes,
) -> str:
    return hashlib.sha256(
        _canonical_bytes(
            {
                "principal": "core-control-v1",
                "operation_id": operation_id,
                "scope": scope,
                "request": json.loads(request_json),
                "semantic_headers": json.loads(semantic_headers_json),
            }
        )
    ).hexdigest()


def _failed_idempotency_identity(
    operation_id: str, arguments: Mapping[str, object]
) -> tuple[str, str, str] | None:
    key = arguments.get("idempotency_key")
    if type(key) is not str:
        return None
    resource_keys = (
        "project_id",
        "upload_id",
        "run_id",
        "service_id",
        "operation_id",
        "diagnostic_id",
        "artifact_id",
        "logs_ref",
        "revision_id",
    )
    resource_scope = {name: arguments[name] for name in resource_keys if name in arguments}
    scope = _canonical_bytes(resource_scope).decode("utf-8")
    request_arguments = {
        name: value for name, value in arguments.items() if name != "idempotency_key"
    }
    digest = hashlib.sha256(
        _canonical_bytes(
            {
                "principal": "core-control-v1",
                "operation_id": operation_id,
                "scope": resource_scope,
                "arguments": request_arguments,
            }
        )
    ).hexdigest()
    return scope, key, digest


def _model_bytes(model: BaseModel) -> bytes:
    return _canonical_bytes(model.model_dump(mode="json"))


def _canonical_bytes(value: object) -> bytes:
    return json.dumps(
        _json_value(value), ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


def _json_value(value: object) -> object:
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json")
    if isinstance(value, dict):
        return {str(key): _json_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_value(item) for item in value]
    if hasattr(value, "value") and isinstance(getattr(value, "value"), str):
        return getattr(value, "value")
    return value


def _validate_bytes(model: type[_ModelT], value: bytes) -> _ModelT:
    try:
        return model.model_validate_json(bytes(value))
    except ValidationError as exc:
        raise StoreCorruptionError(f"persisted {model.__name__} is invalid") from exc


def _validate_idempotency_row(
    row: sqlite3.Row | Mapping[str, Any],
    *,
    signing_key: bytes | None = None,
    publication_owner_lookup: Callable[[str], tuple[str, str] | None] | None = None,
) -> BaseModel | None:
    operation_id = row["operation_id"]
    operation_spec = _IDEMPOTENCY_OPERATION_SPECS.get(operation_id)
    if (
        operation_spec is None
        or (int(row["status_code"]), row["response_type"]) != operation_spec[:2]
    ):
        raise StoreCorruptionError("idempotency operation response is invalid")
    request, semantic_headers = _validate_idempotency_request_envelope(row)
    if signing_key is not None:
        _validate_request_core_owned_ids(request, signing_key)
    resource_scope = _parse_idempotency_success_scope(
        operation_id,
        row["resource_scope"],
        operation_spec[2],
    )

    response_type = row["response_type"]
    response_json = row["response_json"]
    if response_type == "NoContent":
        if response_json is not None or row["etag"] is not None:
            raise StoreCorruptionError("no-content idempotency row is invalid")
        _validate_idempotency_response_semantics(
            operation_id, resource_scope, request, semantic_headers, None
        )
        return None

    response_model = _IDEMPOTENCY_RESPONSE_MODELS.get(response_type)
    if response_model is None or response_json is None:
        raise StoreCorruptionError("idempotency response type is invalid")
    model = _validate_bytes(response_model, response_json)
    if _model_bytes(model) != bytes(response_json):
        raise StoreCorruptionError("idempotency response is not canonical")
    if isinstance(model, (m.ProjectV1, m.WorkspaceUploadSessionV1)):
        expected_etag = model.etag
    elif isinstance(model, m.WorkspaceUploadFinalizeResponseV1):
        expected_etag = model.upload.etag
    else:
        expected_etag = None
    if row["etag"] != expected_etag:
        raise StoreCorruptionError("idempotency response ETag is invalid")
    if signing_key is not None:
        _validate_response_core_owned_ids(
            model,
            signing_key,
            publication_owner_lookup=publication_owner_lookup,
        )
    _validate_idempotency_response_semantics(
        operation_id, resource_scope, request, semantic_headers, model
    )
    return model


def _validate_response_core_owned_ids(
    model: BaseModel,
    signing_key: bytes,
    *,
    publication_owner_lookup: Callable[[str], tuple[str, str] | None] | None,
) -> None:
    snapshots: list[m.ImmutableSnapshotRefV1] = []
    publications: list[tuple[m.WorkspacePublicationV1, str, str]] = []
    if isinstance(model, m.ProjectV1):
        expected_task = _snapshot(
            signing_key,
            m.SnapshotKind.TASK,
            model.task.model_dump(mode="json"),
            model.current_task_snapshot.created_at,
        )
        project_payload = model.model_dump(
            mode="python", exclude={"etag", "current_project_snapshot"}
        )
        expected_project = _snapshot(
            signing_key,
            m.SnapshotKind.PROJECT,
            _project_snapshot_payload(project_payload),
            model.current_project_snapshot.created_at,
        )
        if model.current_task_snapshot != expected_task or model.current_project_snapshot != (
            expected_project
        ):
            raise StoreCorruptionError("idempotency project snapshot closure is invalid")
        if isinstance(model.workspace, m.ScratchWorkspaceSpecV1):
            workspace = model.current_workspace_snapshot
            if workspace is None or workspace != _snapshot_from_digest(
                signing_key,
                m.SnapshotKind.WORKSPACE,
                _EMPTY_WORKSPACE_DIGEST,
                workspace.created_at,
            ):
                raise StoreCorruptionError("idempotency scratch snapshot closure is invalid")
        snapshots.extend((model.current_project_snapshot, model.current_task_snapshot))
        if model.current_workspace_snapshot is not None:
            snapshots.append(model.current_workspace_snapshot)
        if model.workspace_publication is not None:
            if publication_owner_lookup is None:
                raise StoreCorruptionError("idempotency publication owner lookup is unavailable")
            owner = publication_owner_lookup(model.workspace_publication.workspace_snapshot.id)
            if owner is None or owner[0] != model.id:
                raise StoreCorruptionError("idempotency publication owner is invalid")
            publications.append((model.workspace_publication, owner[0], owner[1]))
    elif isinstance(model, m.WorkspaceUploadSessionV1):
        snapshots.append(model.project_snapshot)
        if model.base_workspace_snapshot is not None:
            snapshots.append(model.base_workspace_snapshot)
        if model.publication is not None:
            if publication_owner_lookup is not None:
                owner = publication_owner_lookup(model.publication.workspace_snapshot.id)
                if owner != (model.project_id, model.id):
                    raise StoreCorruptionError("idempotency publication owner is invalid")
            publications.append((model.publication, model.project_id, model.id))
    elif isinstance(model, m.WorkspaceUploadFinalizeResponseV1):
        _validate_response_core_owned_ids(
            model.project,
            signing_key,
            publication_owner_lookup=publication_owner_lookup,
        )
        _validate_response_core_owned_ids(
            model.upload,
            signing_key,
            publication_owner_lookup=publication_owner_lookup,
        )
        publications.append((model.publication, model.project_id, model.upload.id))
    elif isinstance(model, m.ProjectValidationResponseV1):
        return
    for snapshot in snapshots:
        if snapshot != _snapshot_from_digest(
            signing_key,
            snapshot.kind,
            snapshot.content_sha256,
            snapshot.created_at,
        ):
            raise StoreCorruptionError("idempotency snapshot Core identity is invalid")
    for publication, project_id, upload_id in publications:
        expected = _workspace_publication_snapshot(
            signing_key,
            project_id=project_id,
            upload_id=upload_id,
            archive_sha256=publication.archive.content_sha256,
            now=publication.workspace_snapshot.created_at,
        )
        content_id = _core_owned_id(
            signing_key,
            "workspace-content",
            {
                "project_id": project_id,
                "upload_id": upload_id,
                "sha256": publication.archive.content_sha256,
                "byte_size": publication.archive.byte_size,
                "published_at": publication.published_at,
            },
        )
        if (
            publication.workspace_snapshot != expected
            or publication.content_ref.content_id != content_id
        ):
            raise StoreCorruptionError("idempotency publication Core identity is invalid")


def _publication_snapshot_ids(model: BaseModel | None) -> set[str]:
    snapshot_ids: set[str] = set()
    if isinstance(model, m.ProjectV1) and model.workspace_publication is not None:
        snapshot_ids.add(model.workspace_publication.workspace_snapshot.id)
    elif isinstance(model, m.WorkspaceUploadSessionV1) and model.publication is not None:
        snapshot_ids.add(model.publication.workspace_snapshot.id)
    elif isinstance(model, m.WorkspaceUploadFinalizeResponseV1):
        snapshot_ids.add(model.publication.workspace_snapshot.id)
        snapshot_ids.update(_publication_snapshot_ids(model.project))
        snapshot_ids.update(_publication_snapshot_ids(model.upload))
    return snapshot_ids


def _validate_request_core_owned_ids(request: BaseModel | None, signing_key: bytes) -> None:
    snapshots: list[m.ImmutableSnapshotRefV1] = []
    if isinstance(request, m.WorkspaceUploadCreateV1):
        snapshots.append(request.project_snapshot)
        if request.base_workspace_snapshot is not None:
            snapshots.append(request.base_workspace_snapshot)
    elif isinstance(request, m.ProjectValidationRequestV1):
        snapshots.extend((request.project_snapshot, request.workspace_snapshot))
    for snapshot in snapshots:
        if snapshot != _snapshot_from_digest(
            signing_key,
            snapshot.kind,
            snapshot.content_sha256,
            snapshot.created_at,
        ):
            raise StoreCorruptionError("idempotency request snapshot Core identity is invalid")


def _validate_idempotency_request_envelope(
    row: sqlite3.Row | Mapping[str, Any],
) -> tuple[BaseModel | None, dict[str, str]]:
    operation_id = row["operation_id"]
    if operation_id not in _IDEMPOTENCY_REQUEST_MODELS:
        raise StoreCorruptionError("idempotency request operation is invalid")
    request_model = _IDEMPOTENCY_REQUEST_MODELS[operation_id]
    try:
        request_json = bytes(row["request_json"])
        headers_json = bytes(row["semantic_headers_json"])
        request_value = json.loads(request_json)
        header_value = json.loads(headers_json)
    except (KeyError, TypeError, ValueError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise StoreCorruptionError("idempotency request envelope is invalid") from exc
    if (
        _canonical_bytes(request_value) != request_json
        or _canonical_bytes(header_value) != headers_json
        or type(header_value) is not dict
        or any(
            type(key) is not str or type(value) is not str for key, value in header_value.items()
        )
    ):
        raise StoreCorruptionError("idempotency request envelope is not canonical")
    if request_model is None:
        if request_value is not None:
            raise StoreCorruptionError("idempotency request envelope is invalid")
        request = None
    else:
        try:
            request = request_model.model_validate(request_value)
        except ValidationError as exc:
            raise StoreCorruptionError("idempotency request envelope is invalid") from exc
        if _canonical_bytes(request.model_dump(mode="json", exclude_unset=True)) != request_json:
            raise StoreCorruptionError("idempotency request envelope is not canonical")
    expected_headers = {
        "createCoreProjectV1": set(),
        "validateCoreProjectV1": set(),
        "patchCoreProjectV1": {"if-match"},
        "deleteCoreProjectV1": {"if-match"},
        "createCoreWorkspaceUploadV1": {"if-match"},
        "putCoreWorkspaceUploadChunkV1": {"if-match"},
        "finalizeCoreWorkspaceUploadV1": {"if-match", "if-project-match"},
        "abortCoreWorkspaceUploadV1": {"if-match"},
    }[operation_id]
    if set(header_value) != expected_headers or any(
        not _is_etag(value) for value in header_value.values()
    ):
        raise StoreCorruptionError("idempotency semantic headers are invalid")
    expected_digest = _idempotency_request_digest(
        operation_id, row["resource_scope"], request_json, headers_json
    )
    if not _is_sha256(row["request_digest"]) or not hmac.compare_digest(
        row["request_digest"], expected_digest
    ):
        raise StoreCorruptionError("idempotency request digest is invalid")
    return request, header_value


def _parse_idempotency_success_scope(
    operation_id: str,
    value: object,
    scope_kind: Literal["global", "project", "upload"],
) -> _IdempotencyResourceScope:
    if type(value) is not str:
        raise StoreCorruptionError("idempotency response semantic binding is invalid")
    if scope_kind == "global":
        if operation_id != "createCoreProjectV1" or value != "projects":
            raise StoreCorruptionError("idempotency response semantic binding is invalid")
        return _IdempotencyResourceScope(project_id=None, upload_id=None)
    if scope_kind == "project":
        if not _is_managed_resource_id(value, "project"):
            raise StoreCorruptionError("idempotency response semantic binding is invalid")
        return _IdempotencyResourceScope(project_id=value, upload_id=None)

    parts = value.split(":")
    if (
        len(parts) != 2
        or not _is_managed_resource_id(parts[0], "project")
        or not _is_managed_resource_id(parts[1], "upload")
    ):
        raise StoreCorruptionError("idempotency response semantic binding is invalid")
    return _IdempotencyResourceScope(project_id=parts[0], upload_id=parts[1])


def _validate_idempotency_response_semantics(
    operation_id: str,
    scope: _IdempotencyResourceScope,
    request: BaseModel | None,
    semantic_headers: Mapping[str, str],
    model: BaseModel | None,
) -> None:
    valid = False
    if operation_id == "createCoreProjectV1" and isinstance(model, m.ProjectV1):
        assert isinstance(request, m.ProjectCreateV1)
        valid = (
            scope == _IdempotencyResourceScope(project_id=None, upload_id=None)
            and _project_response_has_core_identity(model)
            and model.created_at == model.updated_at
            and model.current_project_snapshot.created_at == model.created_at
            and model.current_task_snapshot.created_at == model.created_at
            and (
                model.current_workspace_snapshot is None
                or model.current_workspace_snapshot.created_at == model.created_at
            )
            and model.model_preparation.updated_at == model.created_at
            and model.workspace_publication is None
            and model.name == request.name
            and model.description == request.description
            and model.spec == request.spec
            and model.task == request.task
            and model.workspace == request.workspace
        )
    elif operation_id == "patchCoreProjectV1" and isinstance(model, m.ProjectV1):
        assert isinstance(request, m.ProjectPatchV1)
        response_values = model.model_dump(mode="python")
        request_values = request.model_dump(mode="python", exclude_unset=True)
        request_values.pop("schema_version", None)
        valid = (
            model.id == scope.project_id
            and _project_response_has_core_identity(model)
            and all(response_values[field] == value for field, value in request_values.items())
            and _is_etag(semantic_headers.get("if-match"))
        )
    elif operation_id == "deleteCoreProjectV1":
        valid = (
            request is None
            and model is None
            and scope.project_id is not None
            and scope.upload_id is None
            and _is_etag(semantic_headers.get("if-match"))
        )
    elif operation_id == "createCoreWorkspaceUploadV1" and isinstance(
        model, m.WorkspaceUploadSessionV1
    ):
        assert isinstance(request, m.WorkspaceUploadCreateV1)
        valid = (
            _upload_response_matches_scope(model, scope, require_upload_id=False)
            and model.status is m.WorkspaceUploadStatus.OPEN
            and model.accepted_offset == 0
            and model.created_at == model.updated_at
            and model.project_snapshot == request.project_snapshot
            and model.archive == request.archive
            and model.base_workspace_snapshot == request.base_workspace_snapshot
            and model.project_etag == semantic_headers.get("if-match")
        )
    elif operation_id == "putCoreWorkspaceUploadChunkV1" and isinstance(
        model, m.WorkspaceUploadSessionV1
    ):
        assert isinstance(request, m.WorkspaceUploadChunkV1)
        valid = (
            _upload_response_matches_scope(model, scope, require_upload_id=True)
            and model.status is m.WorkspaceUploadStatus.OPEN
            and model.accepted_offset == request.offset + request.byte_length
            and _is_etag(semantic_headers.get("if-match"))
        )
    elif operation_id == "finalizeCoreWorkspaceUploadV1" and isinstance(
        model, m.WorkspaceUploadFinalizeResponseV1
    ):
        assert isinstance(request, m.WorkspaceUploadFinalizeV1)
        valid = (
            model.project_id == scope.project_id
            and model.project.id == scope.project_id
            and _project_response_has_core_identity(model.project)
            and _upload_response_matches_scope(model.upload, scope, require_upload_id=True)
            and model.upload.status is m.WorkspaceUploadStatus.FINALIZED
            and model.project.workspace_publication == model.publication
            and model.upload.publication == model.publication
            and model.project.current_workspace_snapshot == model.publication.workspace_snapshot
            and model.publication.published_at == model.upload.updated_at
            and model.project.updated_at == model.upload.updated_at
            and model.publication.workspace_snapshot.created_at == model.upload.updated_at
            and model.publication.archive.content_sha256 == request.content_sha256
            and model.upload.project_etag == semantic_headers.get("if-project-match")
            and _is_etag(semantic_headers.get("if-match"))
        )
    elif operation_id == "abortCoreWorkspaceUploadV1" and isinstance(
        model, m.WorkspaceUploadSessionV1
    ):
        assert isinstance(request, m.WorkspaceUploadAbortV1)
        valid = (
            _upload_response_matches_scope(model, scope, require_upload_id=True)
            and model.status is m.WorkspaceUploadStatus.ABORTED
            and _is_etag(semantic_headers.get("if-match"))
        )
    elif operation_id == "validateCoreProjectV1" and isinstance(
        model, m.ProjectValidationResponseV1
    ):
        assert isinstance(request, m.ProjectValidationRequestV1)
        valid = (
            scope.project_id is not None
            and scope.upload_id is None
            and model.valid is True
            and len(model.checks) == 1
            and model.checks[0].id == "verified-registry"
            and model.checks[0].status is m.CheckStatus.OK
            and model.checks[0].message
            == "The project is valid against the verified executable registry."
            and model.checks[0].target_id is None
            and model.checks[0].method_id is None
            and model.registry_digest == request.expected_registry_digest
            and _is_managed_resource_id(request.project_snapshot.id, "project-snapshot")
            and _is_managed_resource_id(request.workspace_snapshot.id, "workspace-snapshot")
        )
    if not valid:
        raise StoreCorruptionError(
            "idempotency response semantic binding is invalid for request/response"
        )


def _project_response_has_core_identity(project: m.ProjectV1) -> bool:
    return (
        _is_managed_resource_id(project.id, "project")
        and project.status is m.ProjectStatus.DRAFT
        and project.active_revision is None
        and _is_managed_resource_id(project.current_project_snapshot.id, "project-snapshot")
        and project.current_project_snapshot.created_at == project.updated_at
        and _is_managed_resource_id(project.current_task_snapshot.id, "task-snapshot")
        and (
            project.current_workspace_snapshot is None
            or _is_managed_resource_id(project.current_workspace_snapshot.id, "workspace-snapshot")
        )
        and (
            project.workspace_publication is None
            or _publication_has_core_identity(project.workspace_publication)
        )
    )


def _upload_response_matches_scope(
    upload: m.WorkspaceUploadSessionV1,
    scope: _IdempotencyResourceScope,
    *,
    require_upload_id: bool,
) -> bool:
    return (
        scope.project_id is not None
        and upload.project_id == scope.project_id
        and _is_managed_resource_id(upload.id, "upload")
        and (not require_upload_id or upload.id == scope.upload_id)
        and _is_managed_resource_id(upload.project_snapshot.id, "project-snapshot")
        and (
            upload.base_workspace_snapshot is None
            or _is_managed_resource_id(upload.base_workspace_snapshot.id, "workspace-snapshot")
        )
        and (upload.publication is None or _publication_has_core_identity(upload.publication))
    )


def _publication_has_core_identity(publication: m.WorkspacePublicationV1) -> bool:
    return _is_managed_resource_id(
        publication.content_ref.content_id, "workspace-content"
    ) and _is_managed_resource_id(publication.workspace_snapshot.id, "workspace-snapshot")


def _is_managed_resource_id(value: object, prefix: str) -> bool:
    expected_prefix = f"{prefix}-"
    if type(value) is not str or not value.startswith(expected_prefix):
        return False
    suffix = value[len(expected_prefix) :]
    return len(suffix) == 32 and all(character in "0123456789abcdef" for character in suffix)


def _success_scope_for_failed_idempotency(row: sqlite3.Row) -> str | None:
    operation_id = row["operation_id"]
    operation_spec = _IDEMPOTENCY_OPERATION_SPECS.get(operation_id)
    if operation_spec is None:
        return None
    try:
        resource_scope = json.loads(row["resource_scope"])
    except (TypeError, json.JSONDecodeError) as exc:
        raise StoreCorruptionError("failed idempotency resource scope is invalid") from exc
    if (
        type(resource_scope) is not dict
        or _canonical_bytes(resource_scope).decode("utf-8") != row["resource_scope"]
    ):
        raise StoreCorruptionError("failed idempotency resource scope is invalid")

    scope_kind = operation_spec[2]
    if scope_kind == "global":
        if resource_scope:
            raise StoreCorruptionError("failed idempotency resource scope is invalid")
        return "projects"
    if scope_kind == "project":
        if set(resource_scope) != {"project_id"} or type(resource_scope["project_id"]) is not str:
            raise StoreCorruptionError("failed idempotency resource scope is invalid")
        return resource_scope["project_id"]
    if scope_kind == "upload":
        if set(resource_scope) != {"project_id", "upload_id"} or any(
            type(resource_scope[name]) is not str for name in ("project_id", "upload_id")
        ):
            raise StoreCorruptionError("failed idempotency resource scope is invalid")
        return f"{resource_scope['project_id']}:{resource_scope['upload_id']}"
    raise StoreCorruptionError("idempotency operation scope is unsupported")


def _is_sha256(value: object) -> bool:
    return (
        type(value) is str
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )


def _is_store_id(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) == _STORE_ID_HEX_LENGTH
        and all(character in "0123456789abcdef" for character in value)
    )


def _is_timestamp(value: object) -> bool:
    if type(value) is not str or len(value.encode("utf-8")) > 64 or not value.endswith("Z"):
        return False
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError:
        return False
    return parsed.tzinfo is not None and parsed.utcoffset() == timezone.utc.utcoffset(parsed)


def _is_safe_managed_entry_name(value: object) -> bool:
    if type(value) is not str or value in {"", ".", ".."}:
        return False
    try:
        encoded = os.fsencode(value)
    except UnicodeEncodeError:
        return False
    return (
        len(encoded) <= 255
        and b"/" not in encoded
        and b"\0" not in encoded
        and os.path.basename(value) == value
    )


def _is_managed_upload_file_name(value: object) -> bool:
    return (
        type(value) is str
        and value.endswith(".part")
        and _is_managed_resource_id(value[: -len(".part")], "upload")
    )


def _is_etag(value: object) -> bool:
    return (
        type(value) is str
        and len(value) == 66
        and value[0] == value[-1] == '"'
        and _is_sha256(value[1:-1])
    )


def _write_all(fd: int, content: bytes) -> None:
    view = memoryview(content)
    while view:
        written = os.write(fd, view)
        if written <= 0:
            raise OSError("failed to write workspace upload")
        view = view[written:]


def _pread_exact(fd: int, length: int, offset: int) -> bytes:
    content = bytearray()
    while len(content) < length:
        chunk = os.pread(fd, length - len(content), offset + len(content))
        if not chunk:
            raise StoreCorruptionError("workspace upload file ended during verification")
        content.extend(chunk)
    return bytes(content)


def _require_bound_regular_entry(
    parent_fd: int,
    name: str,
    fd: int,
    *,
    expected_size: int,
) -> None:
    metadata = os.fstat(fd)
    _require_private_regular_metadata(metadata)
    if metadata.st_size != expected_size:
        raise StoreCorruptionError("workspace upload file size is invalid")
    _require_entry_binding(parent_fd, name, metadata)


def _require_private_regular_metadata(metadata: os.stat_result) -> None:
    if (
        not stat.S_ISREG(metadata.st_mode)
        or metadata.st_uid != os.geteuid()
        or metadata.st_nlink != 1
        or stat.S_IMODE(metadata.st_mode) != 0o600
    ):
        raise StoreCorruptionError("Core Control private file is unsafe")


def _require_private_directory_metadata(metadata: os.stat_result) -> None:
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or metadata.st_uid != os.geteuid()
        or stat.S_IMODE(metadata.st_mode) != 0o700
    ):
        raise StoreCorruptionError("Core Control managed root is not privately owned")


def _require_owner_directory_metadata(metadata: os.stat_result, *, label: str) -> None:
    if not stat.S_ISDIR(metadata.st_mode) or metadata.st_uid != os.geteuid():
        raise StoreCorruptionError(f"Core Control {label} is not owner-bound")


def _require_path_binding(path: Path, expected: os.stat_result) -> None:
    try:
        current = path.stat(follow_symlinks=False)
    except OSError as exc:
        raise StoreCorruptionError("Core Control managed path binding is invalid") from exc
    if not _same_identity(current, expected):
        raise StoreCorruptionError("Core Control managed path binding changed")


def _require_entry_binding(parent_fd: int, name: str, expected: os.stat_result) -> None:
    try:
        current = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    except OSError as exc:
        raise StoreCorruptionError("Core Control managed entry binding is invalid") from exc
    if not _same_identity(current, expected):
        raise StoreCorruptionError("Core Control managed entry binding changed")


def _same_identity(left: os.stat_result, right: os.stat_result) -> bool:
    return left.st_dev == right.st_dev and left.st_ino == right.st_ino


def _require_same_identity(
    current: os.stat_result,
    expected: os.stat_result,
    label: str,
) -> None:
    if not _same_identity(current, expected):
        raise StoreCorruptionError(f"Core Control {label} identity changed")


def _require_same_file_state(
    current: os.stat_result,
    expected: os.stat_result,
    label: str,
) -> None:
    if (
        not _same_identity(current, expected)
        or current.st_mode != expected.st_mode
        or current.st_uid != expected.st_uid
        or current.st_nlink != expected.st_nlink
        or current.st_size != expected.st_size
        or current.st_mtime_ns != expected.st_mtime_ns
        or current.st_ctime_ns != expected.st_ctime_ns
    ):
        raise StoreCorruptionError(f"Core Control {label} changed during verification")


def _remove_bound_entry_at(
    parent_fd: int,
    name: str,
    entry_fd: int,
    expected_identity: os.stat_result,
    *,
    root_kind: Literal["upload", "workspace"],
) -> None:
    current = os.fstat(entry_fd)
    _require_same_identity(current, expected_identity, "managed cleanup entry")
    _require_entry_binding(parent_fd, name, expected_identity)
    _quarantine_and_remove_entry_at(
        root_kind,
        parent_fd,
        name,
        expected_identity=expected_identity,
        expected_fd=entry_fd,
        budget=None,
    )


def _managed_entry_identity(
    parent_fd: int,
    name: str,
    *,
    expected_type: Literal["file", "directory"],
    required: bool,
) -> os.stat_result | None:
    try:
        metadata = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    except FileNotFoundError:
        if required:
            raise StoreCorruptionError("Core Control managed cleanup entry is missing")
        return None
    except OSError as exc:
        raise StoreCorruptionError("Core Control managed cleanup entry is unreadable") from exc
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    if expected_type == "directory":
        flags |= getattr(os, "O_DIRECTORY", 0)
    elif expected_type != "file":
        raise AssertionError("unsupported managed entry type")
    try:
        entry_fd = os.open(name, flags, dir_fd=parent_fd)
    except OSError as exc:
        raise StoreCorruptionError("Core Control managed cleanup entry is unsafe") from exc
    try:
        current = os.fstat(entry_fd)
        if expected_type == "file":
            _require_private_regular_metadata(current)
        else:
            _require_private_directory_metadata(current)
        _require_same_identity(current, metadata, "managed cleanup entry")
        _require_entry_binding(parent_fd, name, metadata)
    finally:
        os.close(entry_fd)
    return metadata


def _reconcile_orphan_entries_at(
    root_kind: Literal["upload", "workspace"],
    root_fd: int,
    live_names: set[str],
    *,
    budget: _ManagedCleanupBudget,
) -> bool:
    while True:
        orphan_name: str | None = None
        try:
            with os.scandir(root_fd) as entries:
                for entry in entries:
                    if not budget.consume(entry.name):
                        return False
                    if entry.name not in live_names:
                        orphan_name = entry.name
                        break
        except OSError as exc:
            raise StoreCorruptionError("Core Control managed orphan scan failed") from exc
        if orphan_name is None:
            os.fsync(root_fd)
            return True
        if not _quarantine_and_remove_entry_at(
            root_kind,
            root_fd,
            orphan_name,
            expected_identity=None,
            expected_fd=None,
            budget=budget,
        ):
            return False


def _quarantine_and_remove_entry_at(
    root_kind: Literal["upload", "workspace"],
    parent_fd: int,
    name: str,
    *,
    expected_identity: os.stat_result | None,
    expected_fd: int | None,
    budget: _ManagedCleanupBudget | None,
) -> bool:
    if not _is_safe_managed_entry_name(name):
        raise StoreCorruptionError("Core Control managed orphan name is invalid")
    try:
        first_identity = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    except FileNotFoundError:
        return True
    except OSError as exc:
        raise StoreCorruptionError("Core Control managed orphan is unreadable") from exc
    if first_identity.st_uid != os.geteuid():
        raise StoreCorruptionError("Core Control managed orphan has the wrong owner")
    if expected_identity is not None and not _same_identity(first_identity, expected_identity):
        raise StoreCorruptionError("Core Control managed cleanup entry identity changed")

    entry_fd: int | None = None
    entry_type: Literal["file", "directory", "symlink"]
    if stat.S_ISREG(first_identity.st_mode):
        entry_type = "file"
        try:
            entry_fd = os.open(
                name,
                os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0),
                dir_fd=parent_fd,
            )
        except OSError as exc:
            raise StoreCorruptionError("Core Control managed orphan file is unsafe") from exc
        current = os.fstat(entry_fd)
        if (
            not stat.S_ISREG(current.st_mode)
            or current.st_uid != os.geteuid()
            or current.st_nlink != 1
        ):
            os.close(entry_fd)
            raise StoreCorruptionError("Core Control managed orphan file is unsafe")
        _require_same_identity(current, first_identity, "managed orphan file")
    elif stat.S_ISDIR(first_identity.st_mode):
        entry_type = "directory"
        try:
            entry_fd = os.open(
                name,
                os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0),
                dir_fd=parent_fd,
            )
        except OSError as exc:
            raise StoreCorruptionError("Core Control managed orphan directory is unsafe") from exc
        current = os.fstat(entry_fd)
        if not stat.S_ISDIR(current.st_mode) or current.st_uid != os.geteuid():
            os.close(entry_fd)
            raise StoreCorruptionError("Core Control managed orphan directory is unsafe")
        _require_same_identity(current, first_identity, "managed orphan directory")
    elif stat.S_ISLNK(first_identity.st_mode):
        entry_type = "symlink"
    else:
        raise StoreCorruptionError("Core Control managed orphan type is invalid")

    try:
        if expected_fd is not None:
            _require_same_identity(os.fstat(expected_fd), first_identity, "managed cleanup entry")
        _after_cleanup_entry_observed(root_kind, parent_fd, name, first_identity)
        _require_entry_binding(parent_fd, name, first_identity)
        quarantine_name = _quarantine_entry_noreplace(parent_fd, name)
        os.fsync(parent_fd)
        _after_managed_quarantine(
            root_kind,
            parent_fd,
            name,
            quarantine_name,
        )
        _require_entry_binding(parent_fd, quarantine_name, first_identity)
        if entry_fd is not None:
            _require_same_identity(os.fstat(entry_fd), first_identity, "managed quarantined entry")

        if entry_type == "directory":
            assert entry_fd is not None
            while True:
                with os.scandir(entry_fd) as children:
                    child = next(children, None)
                if child is None:
                    break
                if budget is not None and not budget.consume(child.name):
                    return False
                if not _quarantine_and_remove_entry_at(
                    root_kind,
                    entry_fd,
                    child.name,
                    expected_identity=None,
                    expected_fd=None,
                    budget=budget,
                ):
                    return False
            os.fsync(entry_fd)
            _require_same_identity(
                os.fstat(entry_fd), first_identity, "managed quarantined directory"
            )
            _require_entry_binding(parent_fd, quarantine_name, first_identity)
            try:
                os.rmdir(quarantine_name, dir_fd=parent_fd)
            except OSError as exc:
                raise StoreCorruptionError(
                    "Core Control managed orphan directory could not be removed"
                ) from exc
        else:
            _require_entry_binding(parent_fd, quarantine_name, first_identity)
            try:
                os.unlink(quarantine_name, dir_fd=parent_fd)
            except OSError as exc:
                raise StoreCorruptionError(
                    "Core Control managed orphan could not be removed"
                ) from exc
        os.fsync(parent_fd)
        return True
    finally:
        if entry_fd is not None:
            os.close(entry_fd)


def _quarantine_entry_noreplace(parent_fd: int, name: str) -> str:
    for _attempt in range(128):
        quarantine_name = f".quarantine-{secrets.token_hex(24)}"
        try:
            _rename_noreplace(name, quarantine_name, directory_fd=parent_fd)
        except FileExistsError:
            continue
        return quarantine_name
    raise StoreCorruptionError("Core Control could not allocate a quarantine entry")


def _rename_noreplace(source: str, destination: str, *, directory_fd: int) -> None:
    if sys.platform != "linux":
        raise OSError(errno.ENOSYS, "safe no-replace rename requires Linux renameat2")
    libc = ctypes.CDLL(None, use_errno=True)
    renameat2 = getattr(libc, "renameat2", None)
    if renameat2 is None:
        raise OSError(errno.ENOSYS, "safe no-replace rename requires renameat2")
    renameat2.argtypes = (
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_uint,
    )
    renameat2.restype = ctypes.c_int
    result = renameat2(
        directory_fd,
        os.fsencode(source),
        directory_fd,
        os.fsencode(destination),
        _RENAME_NOREPLACE,
    )
    if result != 0:
        error = ctypes.get_errno()
        raise OSError(error, os.strerror(error), destination)


def _after_cleanup_entry_observed(
    root_kind: Literal["upload", "workspace"],
    parent_fd: int,
    name: str,
    identity: os.stat_result,
) -> None:
    del root_kind, parent_fd, name, identity


def _after_managed_quarantine(
    root_kind: Literal["upload", "workspace"],
    parent_fd: int,
    original_name: str,
    quarantine_name: str,
) -> None:
    del root_kind, parent_fd, original_name, quarantine_name


def _verify_managed_disk_quota(
    root_fd: int,
    live_names: set[str],
    *,
    max_entries: int,
    max_bytes: int,
) -> None:
    entries_seen = 0
    bytes_seen = 0

    def visit_entry(directory_fd: int, name: str) -> None:
        nonlocal entries_seen, bytes_seen
        entries_seen += 1
        if entries_seen > max_entries:
            raise StoreCorruptionError("Core Control managed disk entry quota is exceeded")
        metadata = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
        if metadata.st_uid != os.geteuid():
            raise StoreCorruptionError("Core Control managed disk owner is invalid")
        bytes_seen += max(metadata.st_size, metadata.st_blocks * 512)
        if bytes_seen > max_bytes:
            raise StoreCorruptionError("Core Control managed disk byte quota is exceeded")
        if stat.S_ISREG(metadata.st_mode):
            if metadata.st_nlink != 1:
                raise StoreCorruptionError("Core Control managed disk file has multiple links")
            entry_fd = os.open(
                name,
                os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0),
                dir_fd=directory_fd,
            )
            try:
                _require_same_identity(os.fstat(entry_fd), metadata, "managed disk file")
                _require_entry_binding(directory_fd, name, metadata)
            finally:
                os.close(entry_fd)
            return
        if not stat.S_ISDIR(metadata.st_mode):
            raise StoreCorruptionError("Core Control managed disk entry type is invalid")
        child_fd = os.open(
            name,
            os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0),
            dir_fd=directory_fd,
        )
        try:
            if not _same_identity(os.fstat(child_fd), metadata):
                raise StoreCorruptionError("Core Control managed disk directory binding changed")
            with os.scandir(child_fd) as children:
                for child in children:
                    visit_entry(child_fd, child.name)
            _require_entry_binding(directory_fd, name, metadata)
        finally:
            os.close(child_fd)

    if len(live_names) > max_entries:
        raise StoreCorruptionError("Core Control managed disk entry quota is exceeded")
    for live_name in sorted(live_names):
        if not _is_safe_managed_entry_name(live_name):
            raise StoreCorruptionError("Core Control managed live entry name is invalid")
        try:
            visit_entry(root_fd, live_name)
        except OSError as exc:
            raise StoreCorruptionError(
                "Core Control managed live entry is missing or unsafe"
            ) from exc


__all__ = [
    "CoreControlStoreError",
    "CoreControlStoreV1",
    "CursorExpiredError",
    "CursorInvalidError",
    "ETagPreconditionError",
    "EventCursorExpiredError",
    "EventCursorInvalidError",
    "IdempotencyCapacityError",
    "IdempotencyConflictError",
    "PostCommitStoreError",
    "ResourceConflictError",
    "ResourceNotFoundError",
    "StoreCorruptionError",
    "StoredResult",
]
