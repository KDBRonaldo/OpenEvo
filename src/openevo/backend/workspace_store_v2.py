"""Private durable workspace-upload and immutable-snapshot authority for Core v2."""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import sqlite3
import stat
import threading
from typing import Iterator

# v2 reuses only the immutable deterministic-tar format verifier. Upload state,
# authority, recovery, and publication remain exclusively owned by this v2 store.
from openevo.backend.contracts.v1.models import (
    WorkspaceArchiveDeclarationV1,
)
from openevo.backend.contracts.v1.workspace import (
    WorkspaceArchiveError,
    verify_and_materialize_workspace,
    verify_materialized_workspace,
)
from openevo.backend.contracts.v2 import models as m
from openevo.backend.contracts.v2.snapshots import (
    canonical_contract_bytes,
    parse_contract_json_bytes,
)


_SCHEMA_VERSION = 1
_SCHEMA = """
CREATE TABLE metadata (
    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
    schema_version INTEGER NOT NULL CHECK (schema_version = 1),
    store_id TEXT NOT NULL CHECK (length(store_id) = 64),
    binding_state TEXT NOT NULL CHECK (binding_state IN ('pending', 'bound')),
    consumed_archive_bytes INTEGER NOT NULL CHECK (consumed_archive_bytes >= 0),
    consumed_extracted_bytes INTEGER NOT NULL CHECK (consumed_extracted_bytes >= 0),
    consumed_entries INTEGER NOT NULL CHECK (consumed_entries >= 0)
) STRICT;
CREATE TABLE uploads (
    upload_id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL,
    expected_project_head_id TEXT,
    expected_project_head_manifest_sha256 TEXT,
    expected_project_config_sha256 TEXT NOT NULL
        CHECK (length(expected_project_config_sha256) = 64),
    archive_json BLOB NOT NULL,
    chunk_byte_size INTEGER NOT NULL CHECK (chunk_byte_size BETWEEN 1024 AND 8388608),
    chunk_count INTEGER NOT NULL CHECK (chunk_count BETWEEN 1 AND 65536),
    next_chunk_index INTEGER NOT NULL CHECK (next_chunk_index BETWEEN 0 AND 65536),
    accepted_byte_size INTEGER NOT NULL CHECK (accepted_byte_size >= 0),
    state TEXT NOT NULL CHECK (state IN ('open', 'finalized', 'aborted')),
    workspace_snapshot_json BLOB,
    abort_reason TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    resource_version INTEGER NOT NULL CHECK (resource_version >= 1)
) STRICT;
CREATE TABLE upload_create_requests (
    project_id TEXT NOT NULL,
    idempotency_key TEXT NOT NULL,
    request_sha256 TEXT NOT NULL CHECK (length(request_sha256) = 64),
    request_json BLOB NOT NULL,
    response_sha256 TEXT NOT NULL CHECK (length(response_sha256) = 64),
    response_json BLOB NOT NULL,
    upload_id TEXT NOT NULL UNIQUE,
    PRIMARY KEY(project_id, idempotency_key),
    FOREIGN KEY(upload_id) REFERENCES uploads(upload_id) ON DELETE RESTRICT
) STRICT;
CREATE TABLE upload_actions (
    upload_id TEXT NOT NULL,
    idempotency_key TEXT NOT NULL,
    action_kind TEXT NOT NULL CHECK (action_kind IN ('chunk', 'finalize', 'abort')),
    request_sha256 TEXT NOT NULL CHECK (length(request_sha256) = 64),
    request_json BLOB NOT NULL,
    response_sha256 TEXT NOT NULL CHECK (length(response_sha256) = 64),
    response_json BLOB NOT NULL,
    PRIMARY KEY(upload_id, idempotency_key),
    FOREIGN KEY(upload_id) REFERENCES uploads(upload_id) ON DELETE RESTRICT
) STRICT;
CREATE TABLE upload_chunks (
    upload_id TEXT NOT NULL,
    chunk_index INTEGER NOT NULL CHECK (chunk_index BETWEEN 0 AND 65535),
    content_sha256 TEXT NOT NULL CHECK (length(content_sha256) = 64),
    byte_size INTEGER NOT NULL CHECK (byte_size BETWEEN 1 AND 8388608),
    idempotency_key TEXT NOT NULL,
    PRIMARY KEY(upload_id, chunk_index),
    UNIQUE(upload_id, idempotency_key),
    FOREIGN KEY(upload_id) REFERENCES uploads(upload_id) ON DELETE RESTRICT
) STRICT;
CREATE TABLE empty_snapshot_publications (
    workspace_snapshot_id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL,
    snapshot_json BLOB NOT NULL,
    UNIQUE(project_id)
) STRICT;
CREATE TABLE snapshots (
    workspace_snapshot_id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL,
    manifest_sha256 TEXT NOT NULL CHECK (length(manifest_sha256) = 64),
    snapshot_json BLOB NOT NULL,
    source_upload_id TEXT UNIQUE,
    UNIQUE(project_id, manifest_sha256),
    FOREIGN KEY(source_upload_id) REFERENCES uploads(upload_id) ON DELETE RESTRICT
) STRICT;
"""
_MARKER_NAME = ".workspace-store-v2.identity.json"
_DATABASE_NAME = "workspace-store-v2.sqlite3"
_UPLOADS_NAME = "uploads"
_SNAPSHOTS_NAME = "snapshots"
_MAX_UPLOADS = 10_000
_MAX_SNAPSHOTS = 20_000
_MAX_CUMULATIVE_ARCHIVE_BYTES = 64 * 1024 * 1024 * 1024
_MAX_CUMULATIVE_EXTRACTED_BYTES = 64 * 1024 * 1024 * 1024
_MAX_CUMULATIVE_ENTRIES = 400_000
_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z", re.ASCII)
_SHA256_RE = re.compile(r"[0-9a-f]{64}\Z", re.ASCII)
_ETAG_RE = re.compile(r'"[0-9a-f]{64}"\Z', re.ASCII)
_MARKER_TEMP_RE = re.compile(r"\.workspace-marker-[0-9a-f]{32}\.tmp\Z", re.ASCII)


class WorkspaceStoreV2Error(RuntimeError):
    """Workspace authority is unavailable or cannot be trusted."""


class WorkspaceIntegrityErrorV2(WorkspaceStoreV2Error):
    pass


class WorkspaceNotFoundV2(WorkspaceStoreV2Error):
    pass


class WorkspaceConflictV2(WorkspaceStoreV2Error):
    pass


class WorkspacePreconditionFailedV2(WorkspaceConflictV2):
    pass


class WorkspaceIdempotencyConflictV2(WorkspaceConflictV2):
    pass


def _after_chunk_write_before_commit(*_args: object) -> None:
    """Test-only fault boundary after durable bytes and before SQLite authority."""


def _after_snapshot_publish_before_commit(*_args: object) -> None:
    """Test-only fault boundary after no-replace publication and before SQLite."""


def _after_empty_snapshot_publish_before_commit(*_args: object) -> None:
    """Test-only fault boundary after scratch directory publication."""


class WorkspaceStoreV2:
    """Own bounded uploads and immutable extracted workspace snapshots."""

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root).expanduser().absolute()
        if self.root == Path("/"):
            raise WorkspaceIntegrityErrorV2("workspace store root is too broad")
        self.database = self.root / _DATABASE_NAME
        self._lock = threading.RLock()
        self._closed = False
        self._root_fd = -1
        self._uploads_fd = -1
        self._snapshots_fd = -1
        self._root_identity: os.stat_result | None = None
        self._database_identity: os.stat_result | None = None
        try:
            self._prepare_root()
            self._open_roots()
            fresh_database = not _entry_exists(self._root_fd, _DATABASE_NAME)
            if fresh_database:
                self._initialize_fresh_store()
            else:
                self._open_existing_store()
            self._database_identity = self.database.stat(follow_symlinks=False)
            self._recover_filesystem()
            self._verify_database_and_state()
            self._verify_root_inventory()
        except BaseException:
            self.close()
            raise

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            for attribute in ("_snapshots_fd", "_uploads_fd", "_root_fd"):
                descriptor = getattr(self, attribute, -1)
                if descriptor >= 0:
                    os.close(descriptor)
                    setattr(self, attribute, -1)

    def ensure_empty_snapshot(self, project_id: str) -> m.WorkspaceSnapshotRefV2:
        project_id = _resource_id(project_id, label="project")
        declaration = _empty_archive_declaration()
        snapshot = _snapshot_for(project_id, declaration)
        snapshot_json = canonical_contract_bytes(snapshot)
        with self._lock:
            with self._transaction() as connection:
                existing = connection.execute(
                    "SELECT snapshot_json, source_upload_id FROM snapshots "
                    "WHERE workspace_snapshot_id = ?",
                    (snapshot.workspace_snapshot_id,),
                ).fetchone()
                if existing is not None:
                    loaded = _snapshot_from_bytes(bytes(existing["snapshot_json"]))
                    if loaded != snapshot or existing["source_upload_id"] is not None:
                        raise WorkspaceIntegrityErrorV2(
                            "empty workspace snapshot identity was reused"
                        )
                    self._verify_empty_snapshot_directory(
                        snapshot.workspace_snapshot_id
                    )
                    return loaded
                pending = connection.execute(
                    "SELECT project_id, snapshot_json FROM empty_snapshot_publications "
                    "WHERE workspace_snapshot_id = ?",
                    (snapshot.workspace_snapshot_id,),
                ).fetchone()
                if pending is None:
                    if int(
                        connection.execute(
                            "SELECT (SELECT COUNT(*) FROM snapshots) + "
                            "(SELECT COUNT(*) FROM empty_snapshot_publications)"
                        ).fetchone()[0]
                    ) >= _MAX_SNAPSHOTS:
                        raise WorkspaceConflictV2(
                            "workspace snapshot capacity is exhausted"
                        )
                    connection.execute(
                        "INSERT INTO empty_snapshot_publications("
                        "workspace_snapshot_id, project_id, snapshot_json) "
                        "VALUES (?, ?, ?)",
                        (
                            snapshot.workspace_snapshot_id,
                            project_id,
                            snapshot_json,
                        ),
                    )
                elif (
                    pending["project_id"] != project_id
                    or bytes(pending["snapshot_json"]) != snapshot_json
                ):
                    raise WorkspaceIntegrityErrorV2(
                        "pending empty workspace snapshot is inconsistent"
                    )
            self._create_empty_snapshot_directory(snapshot.workspace_snapshot_id)
            _after_empty_snapshot_publish_before_commit(
                project_id,
                snapshot.workspace_snapshot_id,
            )
            with self._transaction() as connection:
                pending = connection.execute(
                    "SELECT project_id, snapshot_json FROM empty_snapshot_publications "
                    "WHERE workspace_snapshot_id = ?",
                    (snapshot.workspace_snapshot_id,),
                ).fetchone()
                if (
                    pending is None
                    or pending["project_id"] != project_id
                    or bytes(pending["snapshot_json"]) != snapshot_json
                ):
                    raise WorkspaceIntegrityErrorV2(
                        "pending empty workspace snapshot authority changed"
                    )
                connection.execute(
                    "INSERT INTO snapshots(workspace_snapshot_id, project_id, "
                    "manifest_sha256, snapshot_json, source_upload_id) "
                    "VALUES (?, ?, ?, ?, NULL)",
                    (
                        snapshot.workspace_snapshot_id,
                        snapshot.project_id,
                        snapshot.manifest_sha256,
                        snapshot_json,
                    ),
                )
                connection.execute(
                    "DELETE FROM empty_snapshot_publications "
                    "WHERE workspace_snapshot_id = ?",
                    (snapshot.workspace_snapshot_id,),
                )
                return snapshot

    def create_upload(
        self,
        project_id: str,
        request: m.WorkspaceUploadCreateV2,
        *,
        idempotency_key: str,
        now: datetime,
    ) -> tuple[m.WorkspaceUploadSessionV2, bool]:
        project_id = _resource_id(project_id, label="project")
        request = _exact_model(m.WorkspaceUploadCreateV2, request)
        idempotency_key = _idempotency_key(idempotency_key)
        timestamp = _timestamp(now)
        request_json = canonical_contract_bytes(request)
        request_sha256 = hashlib.sha256(request_json).hexdigest()
        with self._lock, self._transaction() as connection:
            prior = connection.execute(
                "SELECT request_sha256, request_json, response_sha256, "
                "response_json, upload_id "
                "FROM upload_create_requests WHERE project_id = ? "
                "AND idempotency_key = ?",
                (project_id, idempotency_key),
            ).fetchone()
            if prior is not None:
                if (
                    prior["request_sha256"] != request_sha256
                    or bytes(prior["request_json"]) != request_json
                ):
                    raise WorkspaceIdempotencyConflictV2(
                        "workspace create idempotency key was reused"
                    )
                response_json = bytes(prior["response_json"])
                response = _session_from_bytes(response_json)
                if (
                    hashlib.sha256(response_json).hexdigest()
                    != prior["response_sha256"]
                    or response.upload_id != prior["upload_id"]
                    or response.project_id != project_id
                ):
                    raise WorkspaceIntegrityErrorV2(
                        "workspace create replay response is inconsistent"
                    )
                return response, True
            if int(connection.execute("SELECT COUNT(*) FROM uploads").fetchone()[0]) >= (
                _MAX_UPLOADS
            ):
                raise WorkspaceConflictV2("workspace upload capacity is exhausted")
            metadata = connection.execute(
                "SELECT consumed_archive_bytes, consumed_extracted_bytes, "
                "consumed_entries FROM metadata WHERE singleton = 1"
            ).fetchone()
            if metadata is None:
                raise WorkspaceIntegrityErrorV2("workspace store metadata is missing")
            if (
                int(metadata["consumed_archive_bytes"]) + request.archive.byte_size
                > _MAX_CUMULATIVE_ARCHIVE_BYTES
                or int(metadata["consumed_extracted_bytes"])
                + request.archive.extracted_byte_size
                > _MAX_CUMULATIVE_EXTRACTED_BYTES
                or int(metadata["consumed_entries"]) + request.archive.entry_count
                > _MAX_CUMULATIVE_ENTRIES
            ):
                raise WorkspaceConflictV2("workspace cumulative budget is exhausted")
            upload_id = f"workspace-upload-{secrets.token_hex(16)}"
            archive_name = _archive_name(upload_id)
            archive_fd = -1
            try:
                archive_fd = os.open(
                    archive_name,
                    os.O_WRONLY
                    | os.O_CREAT
                    | os.O_EXCL
                    | getattr(os, "O_CLOEXEC", 0)
                    | getattr(os, "O_NOFOLLOW", 0),
                    0o600,
                    dir_fd=self._uploads_fd,
                )
                os.fsync(archive_fd)
                _require_regular_file(
                    archive_fd,
                    expected_size=0,
                    label="workspace upload archive",
                )
                os.fsync(self._uploads_fd)
                connection.execute(
                    "INSERT INTO uploads(upload_id, project_id, "
                    "expected_project_head_id, expected_project_head_manifest_sha256, "
                    "expected_project_config_sha256, archive_json, chunk_byte_size, "
                    "chunk_count, next_chunk_index, accepted_byte_size, state, "
                    "workspace_snapshot_json, abort_reason, created_at, updated_at, "
                    "resource_version) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0, 0, 'open', "
                    "NULL, NULL, ?, ?, 1)",
                    (
                        upload_id,
                        project_id,
                        request.expected_project_head_id,
                        request.expected_project_head_manifest_sha256,
                        request.expected_project_config_sha256,
                        canonical_contract_bytes(request.archive),
                        request.chunk_byte_size,
                        request.chunk_count,
                        timestamp,
                        timestamp,
                    ),
                )
                connection.execute(
                    "UPDATE metadata SET consumed_archive_bytes = "
                    "consumed_archive_bytes + ?, consumed_extracted_bytes = "
                    "consumed_extracted_bytes + ?, consumed_entries = consumed_entries + ? "
                    "WHERE singleton = 1",
                    (
                        request.archive.byte_size,
                        request.archive.extracted_byte_size,
                        request.archive.entry_count,
                    ),
                )
                response = _load_session(connection, upload_id)
                response_json = canonical_contract_bytes(response)
                connection.execute(
                    "INSERT INTO upload_create_requests(project_id, idempotency_key, "
                    "request_sha256, request_json, response_sha256, response_json, "
                    "upload_id) VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (
                        project_id,
                        idempotency_key,
                        request_sha256,
                        request_json,
                        hashlib.sha256(response_json).hexdigest(),
                        response_json,
                        upload_id,
                    ),
                )
                return response, False
            except BaseException:
                if archive_fd >= 0:
                    os.close(archive_fd)
                    archive_fd = -1
                _discard_regular_file_if_owned(
                    self._uploads_fd,
                    archive_name,
                    expected_size=0,
                )
                raise
            finally:
                if archive_fd >= 0:
                    os.close(archive_fd)

    def get_upload(
        self,
        project_id: str,
        upload_id: str,
    ) -> m.WorkspaceUploadSessionV2:
        project_id = _resource_id(project_id, label="project")
        upload_id = _resource_id(upload_id, label="upload")
        with self._lock, self._reader() as connection:
            session = _load_session(connection, upload_id)
            if session.project_id != project_id:
                raise WorkspaceNotFoundV2("workspace upload was not found")
            return session

    def put_chunk(
        self,
        project_id: str,
        upload_id: str,
        *,
        chunk_index: int,
        chunk: bytes,
        chunk_sha256: str,
        chunk_byte_size: int,
        if_match: str,
        idempotency_key: str,
        now: datetime,
    ) -> tuple[m.WorkspaceUploadSessionV2, bool]:
        project_id = _resource_id(project_id, label="project")
        upload_id = _resource_id(upload_id, label="upload")
        idempotency_key = _idempotency_key(idempotency_key)
        if type(chunk_index) is not int or not 0 <= chunk_index < m.MAX_WORKSPACE_CHUNKS:
            raise ValueError("workspace chunk index is invalid")
        if type(chunk) is not bytes:
            raise TypeError("workspace chunk must be exact bytes")
        if type(chunk_byte_size) is not int or chunk_byte_size != len(chunk):
            raise WorkspaceIntegrityErrorV2("workspace chunk byte size differs")
        if not 1 <= chunk_byte_size <= m.MAX_WORKSPACE_CHUNK_BYTES:
            raise WorkspaceIntegrityErrorV2("workspace chunk byte size is invalid")
        chunk_sha256 = _sha256(chunk_sha256, label="chunk")
        if hashlib.sha256(chunk).hexdigest() != chunk_sha256:
            raise WorkspaceIntegrityErrorV2("workspace chunk digest differs")
        if_match = _etag(if_match)
        timestamp = _timestamp(now)
        request_json = _canonical_action_bytes(
            {
                "action": "chunk",
                "chunk_byte_size": chunk_byte_size,
                "chunk_index": chunk_index,
                "chunk_sha256": chunk_sha256,
                "if_match": if_match,
            }
        )
        with self._lock, self._transaction() as connection:
            replay = _action_replay(
                connection,
                upload_id=upload_id,
                idempotency_key=idempotency_key,
                request_json=request_json,
            )
            if replay is not None:
                if replay.project_id != project_id:
                    raise WorkspaceNotFoundV2("workspace upload was not found")
                return replay, True
            session = _load_session(connection, upload_id)
            if session.project_id != project_id:
                raise WorkspaceNotFoundV2("workspace upload was not found")
            if session.state != "open":
                raise WorkspacePreconditionFailedV2("workspace upload is not open")
            if session.etag != if_match:
                raise WorkspacePreconditionFailedV2("workspace upload ETag changed")
            if session.next_chunk_index != chunk_index:
                raise WorkspacePreconditionFailedV2("workspace chunk is out of order")
            remaining = session.archive.byte_size - session.accepted_byte_size
            expected_size = min(session.chunk_byte_size, remaining)
            if chunk_byte_size != expected_size:
                raise WorkspaceIntegrityErrorV2("workspace chunk has the wrong length")
            archive_fd = self._open_upload_archive(
                upload_id,
                expected_size=session.accepted_byte_size,
                writable=True,
            )
            try:
                os.lseek(archive_fd, session.accepted_byte_size, os.SEEK_SET)
                _write_all(archive_fd, chunk)
                os.fsync(archive_fd)
                _require_regular_file(
                    archive_fd,
                    expected_size=session.accepted_byte_size + chunk_byte_size,
                    label="workspace upload archive",
                )
                _after_chunk_write_before_commit(upload_id, chunk_index)
            finally:
                os.close(archive_fd)
            connection.execute(
                "INSERT INTO upload_chunks(upload_id, chunk_index, content_sha256, "
                "byte_size, idempotency_key) VALUES (?, ?, ?, ?, ?)",
                (
                    upload_id,
                    chunk_index,
                    chunk_sha256,
                    chunk_byte_size,
                    idempotency_key,
                ),
            )
            connection.execute(
                "UPDATE uploads SET next_chunk_index = next_chunk_index + 1, "
                "accepted_byte_size = accepted_byte_size + ?, updated_at = ?, "
                "resource_version = resource_version + 1 WHERE upload_id = ?",
                (chunk_byte_size, timestamp, upload_id),
            )
            response = _load_session(connection, upload_id)
            response_json = canonical_contract_bytes(response)
            connection.execute(
                "INSERT INTO upload_actions(upload_id, idempotency_key, action_kind, "
                "request_sha256, request_json, response_sha256, response_json) "
                "VALUES (?, ?, 'chunk', ?, ?, ?, ?)",
                (
                    upload_id,
                    idempotency_key,
                    hashlib.sha256(request_json).hexdigest(),
                    request_json,
                    hashlib.sha256(response_json).hexdigest(),
                    response_json,
                ),
            )
            return response, False

    def finalize_upload(
        self,
        project_id: str,
        upload_id: str,
        request: m.WorkspaceUploadFinalizeV2,
        *,
        if_match: str,
        idempotency_key: str,
        now: datetime,
    ) -> tuple[m.WorkspaceUploadSessionV2, bool]:
        project_id = _resource_id(project_id, label="project")
        upload_id = _resource_id(upload_id, label="upload")
        request = _exact_model(m.WorkspaceUploadFinalizeV2, request)
        idempotency_key = _idempotency_key(idempotency_key)
        if_match = _etag(if_match)
        timestamp = _timestamp(now)
        request_json = _canonical_action_bytes(
            {
                "action": "finalize",
                "if_match": if_match,
                "request": request.model_dump(mode="json"),
            }
        )
        with self._lock, self._transaction() as connection:
            replay = _action_replay(
                connection,
                upload_id=upload_id,
                idempotency_key=idempotency_key,
                request_json=request_json,
            )
            if replay is not None:
                if replay.project_id != project_id:
                    raise WorkspaceNotFoundV2("workspace upload was not found")
                return replay, True
            session = _load_session(connection, upload_id)
            if session.project_id != project_id:
                raise WorkspaceNotFoundV2("workspace upload was not found")
            if session.state != "open":
                raise WorkspacePreconditionFailedV2("workspace upload is not open")
            if session.etag != if_match:
                raise WorkspacePreconditionFailedV2("workspace upload ETag changed")
            if (
                session.next_chunk_index != session.chunk_count
                or session.accepted_byte_size != session.archive.byte_size
            ):
                raise WorkspacePreconditionFailedV2("workspace upload is incomplete")
            if request.expected_content_sha256 != session.archive.content_sha256:
                raise WorkspacePreconditionFailedV2("workspace archive identity changed")
            snapshot = _snapshot_for(project_id, session.archive)
            archive_path = self.root / _UPLOADS_NAME / _archive_name(upload_id)
            declaration_v1 = _v1_archive_declaration(session.archive)
            try:
                verify_and_materialize_workspace(
                    archive_path,
                    declaration_v1,
                    archive_root_fd=self._uploads_fd,
                    archive_name=_archive_name(upload_id),
                    workspace_root_fd=self._snapshots_fd,
                    snapshot_name=snapshot.workspace_snapshot_id,
                )
            except FileExistsError:
                try:
                    verify_materialized_workspace(
                        archive_path,
                        declaration_v1,
                        archive_root_fd=self._uploads_fd,
                        archive_name=_archive_name(upload_id),
                        workspace_root_fd=self._snapshots_fd,
                        snapshot_name=snapshot.workspace_snapshot_id,
                    )
                except WorkspaceArchiveError as exc:
                    raise WorkspaceIntegrityErrorV2(
                        "workspace snapshot no-replace destination is invalid"
                    ) from exc
            except (WorkspaceArchiveError, OSError) as exc:
                raise WorkspaceIntegrityErrorV2(
                    "workspace archive failed closed validation"
                ) from exc
            _after_snapshot_publish_before_commit(upload_id, snapshot.workspace_snapshot_id)
            existing = connection.execute(
                "SELECT project_id, manifest_sha256, snapshot_json, source_upload_id "
                "FROM snapshots WHERE workspace_snapshot_id = ?",
                (snapshot.workspace_snapshot_id,),
            ).fetchone()
            snapshot_json = canonical_contract_bytes(snapshot)
            if existing is None:
                if int(
                    connection.execute("SELECT COUNT(*) FROM snapshots").fetchone()[0]
                ) >= _MAX_SNAPSHOTS:
                    raise WorkspaceConflictV2("workspace snapshot capacity is exhausted")
                connection.execute(
                    "INSERT INTO snapshots(workspace_snapshot_id, project_id, "
                    "manifest_sha256, snapshot_json, source_upload_id) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (
                        snapshot.workspace_snapshot_id,
                        project_id,
                        snapshot.manifest_sha256,
                        snapshot_json,
                        upload_id,
                    ),
                )
            elif (
                existing["project_id"] != project_id
                or existing["manifest_sha256"] != snapshot.manifest_sha256
                or bytes(existing["snapshot_json"]) != snapshot_json
            ):
                raise WorkspaceIntegrityErrorV2("workspace snapshot identity was reused")
            connection.execute(
                "UPDATE uploads SET state = 'finalized', workspace_snapshot_json = ?, "
                "updated_at = ?, resource_version = resource_version + 1 "
                "WHERE upload_id = ?",
                (snapshot_json, timestamp, upload_id),
            )
            response = _load_session(connection, upload_id)
            response_json = canonical_contract_bytes(response)
            connection.execute(
                "INSERT INTO upload_actions(upload_id, idempotency_key, action_kind, "
                "request_sha256, request_json, response_sha256, response_json) "
                "VALUES (?, ?, 'finalize', ?, ?, ?, ?)",
                (
                    upload_id,
                    idempotency_key,
                    hashlib.sha256(request_json).hexdigest(),
                    request_json,
                    hashlib.sha256(response_json).hexdigest(),
                    response_json,
                ),
            )
            return response, False

    def abort_upload(
        self,
        project_id: str,
        upload_id: str,
        request: m.WorkspaceUploadAbortV2,
        *,
        if_match: str,
        idempotency_key: str,
        now: datetime,
    ) -> tuple[m.WorkspaceUploadSessionV2, bool]:
        project_id = _resource_id(project_id, label="project")
        upload_id = _resource_id(upload_id, label="upload")
        request = _exact_model(m.WorkspaceUploadAbortV2, request)
        idempotency_key = _idempotency_key(idempotency_key)
        if_match = _etag(if_match)
        timestamp = _timestamp(now)
        request_json = _canonical_action_bytes(
            {
                "action": "abort",
                "if_match": if_match,
                "request": request.model_dump(mode="json"),
            }
        )
        with self._lock, self._transaction() as connection:
            replay = _action_replay(
                connection,
                upload_id=upload_id,
                idempotency_key=idempotency_key,
                request_json=request_json,
            )
            if replay is not None:
                if replay.project_id != project_id:
                    raise WorkspaceNotFoundV2("workspace upload was not found")
                return replay, True
            session = _load_session(connection, upload_id)
            if session.project_id != project_id:
                raise WorkspaceNotFoundV2("workspace upload was not found")
            if session.state != "open":
                raise WorkspacePreconditionFailedV2("workspace upload is not open")
            if session.etag != if_match:
                raise WorkspacePreconditionFailedV2("workspace upload ETag changed")
            connection.execute(
                "UPDATE uploads SET state = 'aborted', abort_reason = ?, updated_at = ?, "
                "resource_version = resource_version + 1 WHERE upload_id = ?",
                (request.reason, timestamp, upload_id),
            )
            response = _load_session(connection, upload_id)
            response_json = canonical_contract_bytes(response)
            connection.execute(
                "INSERT INTO upload_actions(upload_id, idempotency_key, action_kind, "
                "request_sha256, request_json, response_sha256, response_json) "
                "VALUES (?, ?, 'abort', ?, ?, ?, ?)",
                (
                    upload_id,
                    idempotency_key,
                    hashlib.sha256(request_json).hexdigest(),
                    request_json,
                    hashlib.sha256(response_json).hexdigest(),
                    response_json,
                ),
            )
            return response, False

    def get_snapshot(self, workspace_snapshot_id: str) -> m.WorkspaceSnapshotRefV2:
        workspace_snapshot_id = _resource_id(
            workspace_snapshot_id,
            label="workspace snapshot",
        )
        with self._lock, self._reader() as connection:
            row = connection.execute(
                "SELECT snapshot_json, source_upload_id FROM snapshots "
                "WHERE workspace_snapshot_id = ?",
                (workspace_snapshot_id,),
            ).fetchone()
            if row is None:
                raise WorkspaceNotFoundV2("workspace snapshot was not found")
            snapshot = _snapshot_from_bytes(bytes(row["snapshot_json"]))
            self._verify_snapshot_filesystem(
                connection,
                snapshot,
                source_upload_id=(
                    None
                    if row["source_upload_id"] is None
                    else str(row["source_upload_id"])
                ),
            )
            return snapshot

    def snapshot_path(self, snapshot: m.WorkspaceSnapshotRefV2) -> Path:
        snapshot = _exact_model(m.WorkspaceSnapshotRefV2, snapshot)
        with self._lock, self._reader() as connection:
            row = connection.execute(
                "SELECT snapshot_json, source_upload_id FROM snapshots "
                "WHERE workspace_snapshot_id = ?",
                (snapshot.workspace_snapshot_id,),
            ).fetchone()
            if row is None or _snapshot_from_bytes(bytes(row["snapshot_json"])) != snapshot:
                raise WorkspaceNotFoundV2("workspace snapshot was not found")
            self._verify_snapshot_filesystem(
                connection,
                snapshot,
                source_upload_id=(
                    None
                    if row["source_upload_id"] is None
                    else str(row["source_upload_id"])
                ),
            )
            return self.root / _SNAPSHOTS_NAME / snapshot.workspace_snapshot_id

    def _prepare_root(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        metadata = self.root.stat(follow_symlinks=False)
        if (
            not stat.S_ISDIR(metadata.st_mode)
            or metadata.st_uid != os.geteuid()
            or stat.S_IMODE(metadata.st_mode) != 0o700
        ):
            raise WorkspaceIntegrityErrorV2(
                "workspace store root must be a private owned directory"
            )

    def _open_roots(self) -> None:
        self._root_fd = os.open(
            self.root,
            os.O_RDONLY
            | getattr(os, "O_DIRECTORY", 0)
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0),
        )
        self._root_identity = os.fstat(self._root_fd)
        _require_directory(self._root_fd, mode=0o700, label="workspace store root")
        for name in (_UPLOADS_NAME, _SNAPSHOTS_NAME):
            try:
                os.mkdir(name, 0o700, dir_fd=self._root_fd)
                os.fsync(self._root_fd)
            except FileExistsError:
                pass
        self._uploads_fd = _open_directory_at(self._root_fd, _UPLOADS_NAME, mode=0o700)
        self._snapshots_fd = _open_directory_at(
            self._root_fd,
            _SNAPSHOTS_NAME,
            mode=0o700,
        )

    def _initialize_fresh_store(self) -> None:
        if _entry_exists(self._root_fd, _MARKER_NAME):
            raise WorkspaceIntegrityErrorV2(
                "fresh workspace store cannot claim an existing marker"
            )
        if set(os.listdir(self._root_fd)) != {_UPLOADS_NAME, _SNAPSHOTS_NAME}:
            raise WorkspaceIntegrityErrorV2(
                "fresh workspace store root contains unmanaged state"
            )
        if os.listdir(self._uploads_fd) or os.listdir(self._snapshots_fd):
            raise WorkspaceIntegrityErrorV2(
                "fresh workspace store cannot claim managed state"
            )
        store_id = secrets.token_hex(32)
        connection = sqlite3.connect(self.database)
        try:
            connection.executescript(_SCHEMA)
            connection.execute(
                "INSERT INTO metadata(singleton, schema_version, store_id, "
                "binding_state, consumed_archive_bytes, consumed_extracted_bytes, "
                "consumed_entries) VALUES (1, 1, ?, 'pending', 0, 0, 0)",
                (store_id,),
            )
            connection.commit()
        finally:
            connection.close()
        os.chmod(self.database, 0o600)
        self._write_marker(store_id)
        with sqlite3.connect(self.database) as connection:
            connection.execute(
                "UPDATE metadata SET binding_state = 'bound' WHERE singleton = 1"
            )
            connection.commit()

    def _open_existing_store(self) -> None:
        self._require_database_file()
        with sqlite3.connect(self.database) as connection:
            connection.row_factory = sqlite3.Row
            if _schema_rows(connection) != _expected_schema_rows():
                raise WorkspaceIntegrityErrorV2("workspace store schema is not exact")
            row = connection.execute(
                "SELECT schema_version, store_id, binding_state FROM metadata "
                "WHERE singleton = 1"
            ).fetchone()
            if row is None or int(row["schema_version"]) != _SCHEMA_VERSION:
                raise WorkspaceIntegrityErrorV2("workspace store identity is invalid")
            store_id = _sha256(str(row["store_id"]), label="store")
            marker_exists = _entry_exists(self._root_fd, _MARKER_NAME)
            if row["binding_state"] == "pending":
                managed_rows = sum(
                    int(connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
                    for table in (
                        "uploads",
                        "upload_create_requests",
                        "upload_actions",
                        "upload_chunks",
                        "empty_snapshot_publications",
                        "snapshots",
                    )
                )
                if managed_rows or os.listdir(self._uploads_fd) or os.listdir(
                    self._snapshots_fd
                ):
                    raise WorkspaceIntegrityErrorV2(
                        "pending workspace identity owns unexpected state"
                    )
                self._recover_pending_marker_publication(
                    store_id,
                    marker_exists=marker_exists,
                )
                self._verify_marker(store_id)
                connection.execute(
                    "UPDATE metadata SET binding_state = 'bound' WHERE singleton = 1"
                )
                connection.commit()
            elif row["binding_state"] == "bound":
                if not marker_exists:
                    raise WorkspaceIntegrityErrorV2(
                        "bound workspace store marker is missing"
                    )
                self._verify_marker(store_id)
            else:
                raise WorkspaceIntegrityErrorV2("workspace binding state is invalid")

    def _recover_pending_marker_publication(
        self,
        store_id: str,
        *,
        marker_exists: bool,
    ) -> None:
        temporaries = sorted(
            name
            for name in os.listdir(self._root_fd)
            if _MARKER_TEMP_RE.fullmatch(name) is not None
        )
        if len(temporaries) > 1:
            raise WorkspaceIntegrityErrorV2(
                "pending workspace marker has ambiguous temporary state"
            )
        expected = _marker_bytes(
            store_id,
            root=os.fstat(self._root_fd),
            database=self.database.stat(follow_symlinks=False),
            uploads=os.fstat(self._uploads_fd),
            snapshots=os.fstat(self._snapshots_fd),
        )
        if marker_exists:
            marker = self._verify_marker_candidate(_MARKER_NAME, expected)
            if temporaries:
                temporary = self._verify_marker_candidate(temporaries[0], expected)
                if (
                    not _same_identity(marker, temporary)
                    or marker.st_nlink != 2
                    or temporary.st_nlink != 2
                ):
                    raise WorkspaceIntegrityErrorV2(
                        "pending workspace marker link binding is invalid"
                    )
                os.unlink(temporaries[0], dir_fd=self._root_fd)
                os.fsync(self._root_fd)
            elif marker.st_nlink != 1:
                raise WorkspaceIntegrityErrorV2(
                    "pending workspace marker link count is invalid"
                )
            return
        if not temporaries:
            self._write_marker(store_id)
            return
        temporary_name = temporaries[0]
        temporary = self._verify_marker_candidate(temporary_name, expected)
        if temporary.st_nlink != 1:
            raise WorkspaceIntegrityErrorV2(
                "pending workspace marker temporary link count is invalid"
            )
        try:
            os.link(
                temporary_name,
                _MARKER_NAME,
                src_dir_fd=self._root_fd,
                dst_dir_fd=self._root_fd,
                follow_symlinks=False,
            )
        except OSError as exc:
            raise WorkspaceIntegrityErrorV2(
                "pending workspace marker could not be published"
            ) from exc
        os.unlink(temporary_name, dir_fd=self._root_fd)
        os.fsync(self._root_fd)

    def _verify_marker_candidate(
        self,
        name: str,
        expected: bytes,
    ) -> os.stat_result:
        try:
            descriptor = os.open(
                name,
                os.O_RDONLY
                | getattr(os, "O_CLOEXEC", 0)
                | getattr(os, "O_NOFOLLOW", 0),
                dir_fd=self._root_fd,
            )
        except OSError as exc:
            raise WorkspaceIntegrityErrorV2(
                "pending workspace marker candidate is unavailable"
            ) from exc
        try:
            metadata = os.fstat(descriptor)
            if (
                not stat.S_ISREG(metadata.st_mode)
                or metadata.st_uid != os.geteuid()
                or stat.S_IMODE(metadata.st_mode) != 0o600
                or metadata.st_size != len(expected)
                or _read_exact(descriptor, metadata.st_size) != expected
            ):
                raise WorkspaceIntegrityErrorV2(
                    "pending workspace marker candidate is invalid"
                )
            _require_entry_binding(self._root_fd, name, metadata)
            return metadata
        finally:
            os.close(descriptor)

    def _write_marker(self, store_id: str) -> None:
        payload = _marker_bytes(
            store_id,
            root=os.fstat(self._root_fd),
            database=self.database.stat(follow_symlinks=False),
            uploads=os.fstat(self._uploads_fd),
            snapshots=os.fstat(self._snapshots_fd),
        )
        temporary = f".workspace-marker-{secrets.token_hex(16)}.tmp"
        descriptor = -1
        try:
            descriptor = os.open(
                temporary,
                os.O_WRONLY
                | os.O_CREAT
                | os.O_EXCL
                | getattr(os, "O_CLOEXEC", 0)
                | getattr(os, "O_NOFOLLOW", 0),
                0o600,
                dir_fd=self._root_fd,
            )
            _write_all(descriptor, payload)
            os.fsync(descriptor)
            os.link(
                temporary,
                _MARKER_NAME,
                src_dir_fd=self._root_fd,
                dst_dir_fd=self._root_fd,
                follow_symlinks=False,
            )
            os.unlink(temporary, dir_fd=self._root_fd)
            os.fsync(self._root_fd)
        except FileExistsError as exc:
            raise WorkspaceIntegrityErrorV2(
                "workspace identity marker already exists"
            ) from exc
        finally:
            if descriptor >= 0:
                os.close(descriptor)
            try:
                os.unlink(temporary, dir_fd=self._root_fd)
            except FileNotFoundError:
                pass

    def _verify_marker(self, store_id: str) -> None:
        descriptor = os.open(
            _MARKER_NAME,
            os.O_RDONLY
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0),
            dir_fd=self._root_fd,
        )
        try:
            metadata = _require_regular_file(
                descriptor,
                expected_size=None,
                label="workspace identity marker",
            )
            if metadata.st_size > 4096:
                raise WorkspaceIntegrityErrorV2(
                    "workspace identity marker exceeds its bound"
                )
            payload = _read_exact(descriptor, metadata.st_size)
            expected = _marker_bytes(
                store_id,
                root=os.fstat(self._root_fd),
                database=self.database.stat(follow_symlinks=False),
                uploads=os.fstat(self._uploads_fd),
                snapshots=os.fstat(self._snapshots_fd),
            )
            if payload != expected:
                raise WorkspaceIntegrityErrorV2(
                    "workspace identity marker is inconsistent"
                )
            _require_entry_binding(self._root_fd, _MARKER_NAME, metadata)
        finally:
            os.close(descriptor)

    def _recover_filesystem(self) -> None:
        with self._lock, self._transaction() as connection:
            uploads = {
                str(row["upload_id"]): (int(row["accepted_byte_size"]), str(row["state"]))
                for row in connection.execute(
                    "SELECT upload_id, accepted_byte_size, state FROM uploads"
                ).fetchall()
            }
            expected_upload_names = {_archive_name(upload_id) for upload_id in uploads}
            for name in os.listdir(self._uploads_fd):
                if name not in expected_upload_names:
                    _discard_regular_file_if_owned(
                        self._uploads_fd,
                        name,
                        expected_size=None,
                    )
            for upload_id, (accepted, state) in uploads.items():
                name = _archive_name(upload_id)
                try:
                    descriptor = self._open_upload_archive(
                        upload_id,
                        expected_size=None,
                        writable=True,
                    )
                except WorkspaceIntegrityErrorV2:
                    if state == "aborted" and not _entry_exists(self._uploads_fd, name):
                        continue
                    raise
                try:
                    metadata = os.fstat(descriptor)
                    if metadata.st_size < accepted:
                        raise WorkspaceIntegrityErrorV2(
                            "workspace upload archive lost committed bytes"
                        )
                    if metadata.st_size > accepted:
                        os.ftruncate(descriptor, accepted)
                        os.fsync(descriptor)
                    _require_regular_file(
                        descriptor,
                        expected_size=accepted,
                        label="workspace upload archive",
                    )
                    _verify_committed_chunks(
                        connection,
                        _load_session(connection, upload_id),
                        descriptor=descriptor,
                    )
                finally:
                    os.close(descriptor)

            pending_empty = connection.execute(
                "SELECT workspace_snapshot_id, project_id, snapshot_json "
                "FROM empty_snapshot_publications ORDER BY workspace_snapshot_id "
                "LIMIT ?",
                (_MAX_SNAPSHOTS + 1,),
            ).fetchall()
            snapshot_count = int(
                connection.execute("SELECT COUNT(*) FROM snapshots").fetchone()[0]
            )
            if (
                len(pending_empty) > _MAX_SNAPSHOTS
                or snapshot_count + len(pending_empty) > _MAX_SNAPSHOTS
            ):
                raise WorkspaceIntegrityErrorV2(
                    "pending empty workspace snapshot inventory is too large"
                )
            for row in pending_empty:
                snapshot = _snapshot_from_bytes(bytes(row["snapshot_json"]))
                if (
                    snapshot.workspace_snapshot_id != row["workspace_snapshot_id"]
                    or snapshot.project_id != row["project_id"]
                    or snapshot
                    != _snapshot_for(
                        snapshot.project_id,
                        _empty_archive_declaration(),
                    )
                ):
                    raise WorkspaceIntegrityErrorV2(
                        "pending empty workspace snapshot is invalid"
                    )
                self._create_empty_snapshot_directory(
                    snapshot.workspace_snapshot_id
                )
                existing = connection.execute(
                    "SELECT snapshot_json, source_upload_id FROM snapshots "
                    "WHERE workspace_snapshot_id = ?",
                    (snapshot.workspace_snapshot_id,),
                ).fetchone()
                snapshot_json = canonical_contract_bytes(snapshot)
                if existing is None:
                    connection.execute(
                        "INSERT INTO snapshots(workspace_snapshot_id, project_id, "
                        "manifest_sha256, snapshot_json, source_upload_id) "
                        "VALUES (?, ?, ?, ?, NULL)",
                        (
                            snapshot.workspace_snapshot_id,
                            snapshot.project_id,
                            snapshot.manifest_sha256,
                            snapshot_json,
                        ),
                    )
                elif (
                    bytes(existing["snapshot_json"]) != snapshot_json
                    or existing["source_upload_id"] is not None
                ):
                    raise WorkspaceIntegrityErrorV2(
                        "recovered empty workspace snapshot differs"
                    )
                connection.execute(
                    "DELETE FROM empty_snapshot_publications "
                    "WHERE workspace_snapshot_id = ?",
                    (snapshot.workspace_snapshot_id,),
                )

            snapshot_rows = connection.execute(
                "SELECT workspace_snapshot_id, snapshot_json, source_upload_id "
                "FROM snapshots"
            ).fetchall()
            snapshots = {
                str(row["workspace_snapshot_id"]): (
                    _snapshot_from_bytes(bytes(row["snapshot_json"])),
                    None
                    if row["source_upload_id"] is None
                    else str(row["source_upload_id"]),
                )
                for row in snapshot_rows
            }
            allowed_uncommitted: dict[str, m.WorkspaceUploadSessionV2] = {}
            for row in connection.execute(
                "SELECT upload_id "
                "FROM uploads WHERE state = 'open'"
            ).fetchall():
                upload = _load_session(connection, str(row["upload_id"]))
                if upload.accepted_byte_size == upload.archive.byte_size:
                    name = _snapshot_for(
                        upload.project_id,
                        upload.archive,
                    ).workspace_snapshot_id
                    allowed_uncommitted.setdefault(name, upload)
            for name in os.listdir(self._snapshots_fd):
                if name not in snapshots and name not in allowed_uncommitted:
                    raise WorkspaceIntegrityErrorV2(
                        "workspace snapshot root contains unmanaged state"
                    )
                if name in allowed_uncommitted and name not in snapshots:
                    upload = allowed_uncommitted[name]
                    try:
                        verify_materialized_workspace(
                            self.root
                            / _UPLOADS_NAME
                            / _archive_name(upload.upload_id),
                            _v1_archive_declaration(upload.archive),
                            archive_root_fd=self._uploads_fd,
                            archive_name=_archive_name(upload.upload_id),
                            workspace_root_fd=self._snapshots_fd,
                            snapshot_name=name,
                        )
                    except (WorkspaceArchiveError, OSError) as exc:
                        raise WorkspaceIntegrityErrorV2(
                            "uncommitted workspace snapshot failed verification"
                        ) from exc
            for snapshot, source_upload_id in snapshots.values():
                self._verify_snapshot_filesystem(
                    connection,
                    snapshot,
                    source_upload_id=source_upload_id,
                )

    def _verify_database_and_state(self) -> None:
        self._verify_root_binding()
        self._require_database_file()
        with self._reader() as connection:
            if _schema_rows(connection) != _expected_schema_rows():
                raise WorkspaceIntegrityErrorV2("workspace store schema is not exact")
            quick_check = connection.execute("PRAGMA quick_check").fetchall()
            if len(quick_check) != 1 or tuple(quick_check[0]) != ("ok",):
                raise WorkspaceIntegrityErrorV2("workspace database integrity failed")
            if connection.execute("PRAGMA foreign_key_check").fetchone() is not None:
                raise WorkspaceIntegrityErrorV2(
                    "workspace database foreign-key integrity failed"
                )
            if int(
                connection.execute(
                    "SELECT COUNT(*) FROM empty_snapshot_publications"
                ).fetchone()[0]
            ) != 0:
                raise WorkspaceIntegrityErrorV2(
                    "pending empty workspace recovery did not converge"
                )
            metadata = connection.execute(
                "SELECT schema_version, store_id, binding_state, "
                "consumed_archive_bytes, consumed_extracted_bytes, consumed_entries "
                "FROM metadata WHERE singleton = 1"
            ).fetchone()
            if (
                metadata is None
                or int(metadata["schema_version"]) != _SCHEMA_VERSION
                or metadata["binding_state"] != "bound"
                or int(metadata["consumed_archive_bytes"])
                > _MAX_CUMULATIVE_ARCHIVE_BYTES
                or int(metadata["consumed_extracted_bytes"])
                > _MAX_CUMULATIVE_EXTRACTED_BYTES
                or int(metadata["consumed_entries"]) > _MAX_CUMULATIVE_ENTRIES
            ):
                raise WorkspaceIntegrityErrorV2("workspace metadata is invalid")
            self._verify_marker(_sha256(str(metadata["store_id"]), label="store"))
            upload_rows = connection.execute(
                "SELECT upload_id FROM uploads LIMIT ?",
                (_MAX_UPLOADS + 1,),
            ).fetchall()
            if len(upload_rows) > _MAX_UPLOADS:
                raise WorkspaceIntegrityErrorV2("workspace upload inventory is too large")
            declared_archive_bytes = 0
            declared_extracted_bytes = 0
            declared_entries = 0
            for row in upload_rows:
                session = _load_session(connection, str(row["upload_id"]))
                _verify_upload_database_closure(connection, session)
                declared_archive_bytes += session.archive.byte_size
                declared_extracted_bytes += session.archive.extracted_byte_size
                declared_entries += session.archive.entry_count
            if (
                declared_archive_bytes != int(metadata["consumed_archive_bytes"])
                or declared_extracted_bytes
                != int(metadata["consumed_extracted_bytes"])
                or declared_entries != int(metadata["consumed_entries"])
            ):
                raise WorkspaceIntegrityErrorV2(
                    "workspace cumulative budget authority is inconsistent"
                )
            snapshot_rows = connection.execute(
                "SELECT workspace_snapshot_id, project_id, manifest_sha256, "
                "snapshot_json FROM snapshots LIMIT ?",
                (_MAX_SNAPSHOTS + 1,),
            ).fetchall()
            if len(snapshot_rows) > _MAX_SNAPSHOTS:
                raise WorkspaceIntegrityErrorV2(
                    "workspace snapshot inventory is too large"
                )
            for row in snapshot_rows:
                snapshot = _snapshot_from_bytes(bytes(row["snapshot_json"]))
                if (
                    snapshot.workspace_snapshot_id != row["workspace_snapshot_id"]
                    or snapshot.project_id != row["project_id"]
                    or snapshot.manifest_sha256 != row["manifest_sha256"]
                ):
                    raise WorkspaceIntegrityErrorV2(
                        "workspace snapshot row is inconsistent"
                    )
            for row in connection.execute(
                "SELECT request_sha256, request_json, response_sha256, response_json "
                "FROM upload_create_requests UNION ALL SELECT request_sha256, "
                "request_json, response_sha256, response_json FROM upload_actions"
            ).fetchall():
                request_json = bytes(row["request_json"])
                response_json = bytes(row["response_json"])
                if (
                    hashlib.sha256(request_json).hexdigest()
                    != row["request_sha256"]
                    or hashlib.sha256(response_json).hexdigest()
                    != row["response_sha256"]
                ):
                    raise WorkspaceIntegrityErrorV2(
                        "workspace idempotency replay digest is inconsistent"
                    )

    def _verify_snapshot_filesystem(
        self,
        connection: sqlite3.Connection,
        snapshot: m.WorkspaceSnapshotRefV2,
        *,
        source_upload_id: str | None,
    ) -> None:
        if source_upload_id is None:
            if snapshot != _snapshot_for(
                snapshot.project_id,
                _empty_archive_declaration(),
            ):
                raise WorkspaceIntegrityErrorV2(
                    "empty workspace snapshot identity is inconsistent"
                )
            self._verify_empty_snapshot_directory(snapshot.workspace_snapshot_id)
            if snapshot.entry_count or snapshot.byte_size:
                raise WorkspaceIntegrityErrorV2("empty workspace snapshot is inconsistent")
            return
        upload = _load_session(connection, source_upload_id)
        if (
            upload.workspace_snapshot != snapshot
            or upload.state != "finalized"
            or _snapshot_for(upload.project_id, upload.archive) != snapshot
        ):
            raise WorkspaceIntegrityErrorV2(
                "workspace snapshot source upload is inconsistent"
            )
        try:
            verify_materialized_workspace(
                self.root / _UPLOADS_NAME / _archive_name(source_upload_id),
                _v1_archive_declaration(upload.archive),
                archive_root_fd=self._uploads_fd,
                archive_name=_archive_name(source_upload_id),
                workspace_root_fd=self._snapshots_fd,
                snapshot_name=snapshot.workspace_snapshot_id,
            )
        except (WorkspaceArchiveError, OSError) as exc:
            raise WorkspaceIntegrityErrorV2(
                "published workspace snapshot failed verification"
            ) from exc

    def _create_empty_snapshot_directory(self, snapshot_name: str) -> None:
        try:
            os.mkdir(snapshot_name, 0o700, dir_fd=self._snapshots_fd)
            os.fsync(self._snapshots_fd)
        except FileExistsError:
            self._verify_empty_snapshot_directory(snapshot_name)

    def _verify_empty_snapshot_directory(self, snapshot_name: str) -> None:
        descriptor = _open_directory_at(self._snapshots_fd, snapshot_name, mode=0o700)
        try:
            if os.listdir(descriptor):
                raise WorkspaceIntegrityErrorV2(
                    "empty workspace snapshot contains unexpected entries"
                )
        finally:
            os.close(descriptor)

    def _open_upload_archive(
        self,
        upload_id: str,
        *,
        expected_size: int | None,
        writable: bool,
    ) -> int:
        flags = os.O_RDWR if writable else os.O_RDONLY
        try:
            descriptor = os.open(
                _archive_name(upload_id),
                flags
                | getattr(os, "O_CLOEXEC", 0)
                | getattr(os, "O_NOFOLLOW", 0),
                dir_fd=self._uploads_fd,
            )
        except OSError as exc:
            raise WorkspaceIntegrityErrorV2(
                "workspace upload archive is unavailable"
            ) from exc
        try:
            metadata = _require_regular_file(
                descriptor,
                expected_size=expected_size,
                label="workspace upload archive",
            )
            _require_entry_binding(
                self._uploads_fd,
                _archive_name(upload_id),
                metadata,
            )
            return descriptor
        except BaseException:
            os.close(descriptor)
            raise

    def _require_database_file(self) -> None:
        try:
            metadata = self.database.stat(follow_symlinks=False)
        except OSError as exc:
            raise WorkspaceIntegrityErrorV2("workspace database is missing") from exc
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid != os.geteuid()
            or metadata.st_nlink != 1
            or stat.S_IMODE(metadata.st_mode) != 0o600
        ):
            raise WorkspaceIntegrityErrorV2(
                "workspace database must be a private regular file"
            )
        if self._database_identity is not None and not _same_identity(
            metadata,
            self._database_identity,
        ):
            raise WorkspaceIntegrityErrorV2("workspace database binding changed")

    def _verify_root_binding(self) -> None:
        if self._closed or self._root_fd < 0 or self._root_identity is None:
            raise WorkspaceIntegrityErrorV2("workspace store is closed")
        try:
            current = self.root.stat(follow_symlinks=False)
        except OSError as exc:
            raise WorkspaceIntegrityErrorV2("workspace root binding is missing") from exc
        held = os.fstat(self._root_fd)
        if not _same_identity(current, held) or not _same_identity(
            held,
            self._root_identity,
        ):
            raise WorkspaceIntegrityErrorV2("workspace root binding changed")

    def _verify_root_inventory(self) -> None:
        expected = {
            _DATABASE_NAME,
            _MARKER_NAME,
            _SNAPSHOTS_NAME,
            _UPLOADS_NAME,
        }
        if set(os.listdir(self._root_fd)) != expected:
            raise WorkspaceIntegrityErrorV2(
                "workspace store root contains unmanaged state"
            )

    @contextmanager
    def _transaction(self) -> Iterator[sqlite3.Connection]:
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            yield connection
            self._verify_root_binding()
            self._require_database_file()
            connection.execute("COMMIT")
            self._verify_root_binding()
            self._require_database_file()
        except BaseException:
            try:
                connection.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            raise
        finally:
            connection.close()

    @contextmanager
    def _reader(self) -> Iterator[sqlite3.Connection]:
        connection = self._connect()
        try:
            yield connection
            self._verify_root_binding()
            self._require_database_file()
        finally:
            connection.close()

    def _connect(self) -> sqlite3.Connection:
        self._verify_root_binding()
        self._require_database_file()
        connection = sqlite3.connect(self.database, timeout=10.0)
        try:
            self._verify_root_binding()
            self._require_database_file()
        except BaseException:
            connection.close()
            raise
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 10000")
        return connection


def _load_session(
    connection: sqlite3.Connection,
    upload_id: str,
) -> m.WorkspaceUploadSessionV2:
    row = connection.execute(
        "SELECT upload_id, project_id, expected_project_head_id, "
        "expected_project_head_manifest_sha256, expected_project_config_sha256, "
        "archive_json, chunk_byte_size, chunk_count, next_chunk_index, "
        "accepted_byte_size, state, workspace_snapshot_json, abort_reason, "
        "created_at, updated_at, resource_version FROM uploads WHERE upload_id = ?",
        (upload_id,),
    ).fetchone()
    if row is None:
        raise WorkspaceNotFoundV2("workspace upload was not found")
    try:
        archive = _archive_from_bytes(bytes(row["archive_json"]))
        snapshot = (
            None
            if row["workspace_snapshot_json"] is None
            else _snapshot_from_bytes(bytes(row["workspace_snapshot_json"]))
        )
        provisional = m.WorkspaceUploadSessionV2(
            upload_id=_resource_id(str(row["upload_id"]), label="upload"),
            project_id=_resource_id(str(row["project_id"]), label="project"),
            state=str(row["state"]),
            expected_project_head_id=(
                None
                if row["expected_project_head_id"] is None
                else _resource_id(
                    str(row["expected_project_head_id"]),
                    label="project head",
                )
            ),
            expected_project_head_manifest_sha256=(
                None
                if row["expected_project_head_manifest_sha256"] is None
                else _sha256(
                    str(row["expected_project_head_manifest_sha256"]),
                    label="project head manifest",
                )
            ),
            expected_project_config_sha256=_sha256(
                str(row["expected_project_config_sha256"]),
                label="project config",
            ),
            archive=archive,
            chunk_byte_size=int(row["chunk_byte_size"]),
            chunk_count=int(row["chunk_count"]),
            next_chunk_index=int(row["next_chunk_index"]),
            accepted_byte_size=int(row["accepted_byte_size"]),
            workspace_snapshot=snapshot,
            created_at=_timestamp_text(str(row["created_at"])),
            updated_at=_timestamp_text(str(row["updated_at"])),
            etag='"' + ("0" * 64) + '"',
        )
        session = m.WorkspaceUploadSessionV2.model_validate(
            {
                **provisional.model_dump(mode="python"),
                "etag": _session_etag(provisional),
            }
        )
    except (TypeError, ValueError) as exc:
        raise WorkspaceIntegrityErrorV2(
            "persisted workspace upload is invalid"
        ) from exc
    if (
        int(row["resource_version"]) < 1
        or (session.state == "aborted") != (row["abort_reason"] is not None)
        or (session.state == "finalized") != (snapshot is not None)
    ):
        raise WorkspaceIntegrityErrorV2("persisted workspace upload is inconsistent")
    return session


def _verify_committed_chunks(
    connection: sqlite3.Connection,
    session: m.WorkspaceUploadSessionV2,
    *,
    descriptor: int | None,
) -> None:
    rows = connection.execute(
        "SELECT chunk_index, content_sha256, byte_size, idempotency_key "
        "FROM upload_chunks WHERE upload_id = ? ORDER BY chunk_index",
        (session.upload_id,),
    ).fetchall()
    if len(rows) != session.next_chunk_index:
        raise WorkspaceIntegrityErrorV2(
            "workspace committed chunk inventory is inconsistent"
        )
    offset = 0
    for expected_index, row in enumerate(rows):
        try:
            index = int(row["chunk_index"])
            byte_size = int(row["byte_size"])
            content_sha256 = _sha256(str(row["content_sha256"]), label="chunk")
            idempotency_key = _idempotency_key(str(row["idempotency_key"]))
        except (TypeError, ValueError) as exc:
            raise WorkspaceIntegrityErrorV2(
                "workspace committed chunk metadata is invalid"
            ) from exc
        expected_size = min(
            session.chunk_byte_size,
            session.archive.byte_size - offset,
        )
        if index != expected_index or byte_size != expected_size:
            raise WorkspaceIntegrityErrorV2(
                "workspace committed chunk sequence is inconsistent"
            )
        action = connection.execute(
            "SELECT action_kind, request_sha256, request_json, response_sha256, "
            "response_json FROM upload_actions "
            "WHERE upload_id = ? AND idempotency_key = ?",
            (session.upload_id, idempotency_key),
        ).fetchone()
        if action is None or action["action_kind"] != "chunk":
            raise WorkspaceIntegrityErrorV2(
                "workspace committed chunk action is missing"
            )
        request_json = bytes(action["request_json"])
        response_json = bytes(action["response_json"])
        response = _session_from_bytes(response_json)
        if (
            hashlib.sha256(request_json).hexdigest() != action["request_sha256"]
            or hashlib.sha256(response_json).hexdigest()
            != action["response_sha256"]
            or not _chunk_action_matches(
                request_json,
                chunk_index=index,
                chunk_sha256=content_sha256,
                chunk_byte_size=byte_size,
            )
            or not _same_upload_authority(response, session)
            or response.state != "open"
            or response.next_chunk_index != index + 1
            or response.accepted_byte_size != offset + byte_size
            or response.workspace_snapshot is not None
        ):
            raise WorkspaceIntegrityErrorV2(
                "workspace committed chunk action is inconsistent"
            )
        if descriptor is not None:
            content = _pread_exact(descriptor, byte_size, offset=offset)
            if hashlib.sha256(content).hexdigest() != content_sha256:
                raise WorkspaceIntegrityErrorV2(
                    "workspace committed chunk digest changed"
                )
        offset += byte_size
    if offset != session.accepted_byte_size:
        raise WorkspaceIntegrityErrorV2(
            "workspace committed chunk bytes are inconsistent"
        )


def _verify_upload_database_closure(
    connection: sqlite3.Connection,
    session: m.WorkspaceUploadSessionV2,
) -> None:
    create = connection.execute(
        "SELECT project_id, idempotency_key, request_sha256, request_json, "
        "response_sha256, response_json "
        "FROM upload_create_requests WHERE upload_id = ?",
        (session.upload_id,),
    ).fetchone()
    if create is None or create["project_id"] != session.project_id:
        raise WorkspaceIntegrityErrorV2(
            "workspace upload create authority is missing"
        )
    request_json = bytes(create["request_json"])
    response_json = bytes(create["response_json"])
    try:
        request = parse_contract_json_bytes(m.WorkspaceUploadCreateV2, request_json)
        response = _session_from_bytes(response_json)
        create_key = _idempotency_key(str(create["idempotency_key"]))
    except (TypeError, ValueError) as exc:
        raise WorkspaceIntegrityErrorV2(
            "workspace upload create authority is invalid"
        ) from exc
    if (
        canonical_contract_bytes(request) != request_json
        or hashlib.sha256(request_json).hexdigest() != create["request_sha256"]
        or hashlib.sha256(response_json).hexdigest() != create["response_sha256"]
        or create_key != create["idempotency_key"]
        or request.expected_project_head_id != session.expected_project_head_id
        or request.expected_project_head_manifest_sha256
        != session.expected_project_head_manifest_sha256
        or request.expected_project_config_sha256
        != session.expected_project_config_sha256
        or request.archive != session.archive
        or request.chunk_byte_size != session.chunk_byte_size
        or request.chunk_count != session.chunk_count
        or not _same_upload_authority(response, session)
        or response.state != "open"
        or response.next_chunk_index != 0
        or response.accepted_byte_size != 0
        or response.workspace_snapshot is not None
        or response.updated_at != response.created_at
    ):
        raise WorkspaceIntegrityErrorV2(
            "workspace upload create authority is inconsistent"
        )
    _verify_committed_chunks(connection, session, descriptor=None)
    actions = connection.execute(
        "SELECT action_kind, request_sha256, request_json, response_sha256, "
        "response_json FROM upload_actions "
        "WHERE upload_id = ?",
        (session.upload_id,),
    ).fetchall()
    terminal = [row for row in actions if row["action_kind"] != "chunk"]
    expected_terminal_count = 0 if session.state == "open" else 1
    if (
        len(actions) != session.next_chunk_index + expected_terminal_count
        or len(terminal) != expected_terminal_count
    ):
        raise WorkspaceIntegrityErrorV2(
            "workspace upload action inventory is inconsistent"
        )
    row = connection.execute(
        "SELECT resource_version, abort_reason FROM uploads WHERE upload_id = ?",
        (session.upload_id,),
    ).fetchone()
    if row is None or int(row["resource_version"]) != 1 + len(actions):
        raise WorkspaceIntegrityErrorV2(
            "workspace upload resource version is inconsistent"
        )
    if not terminal:
        return
    action = terminal[0]
    action_json = bytes(action["request_json"])
    response_json = bytes(action["response_json"])
    try:
        value = json.loads(action_json)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise WorkspaceIntegrityErrorV2(
            "workspace terminal action is invalid"
        ) from exc
    if (
        type(value) is not dict
        or set(value) != {"action", "if_match", "request"}
        or _canonical_action_bytes(value) != action_json
        or hashlib.sha256(action_json).hexdigest() != action["request_sha256"]
        or hashlib.sha256(response_json).hexdigest()
        != action["response_sha256"]
        or _session_from_bytes(response_json) != session
        or not isinstance(value["if_match"], str)
        or _ETAG_RE.fullmatch(value["if_match"]) is None
        or value["action"] != action["action_kind"]
    ):
        raise WorkspaceIntegrityErrorV2(
            "workspace terminal action is inconsistent"
        )
    try:
        if session.state == "finalized":
            terminal_request = m.WorkspaceUploadFinalizeV2.model_validate(
                value["request"]
            )
            valid = (
                action["action_kind"] == "finalize"
                and terminal_request.expected_content_sha256
                == session.archive.content_sha256
                and row["abort_reason"] is None
            )
        else:
            terminal_request = m.WorkspaceUploadAbortV2.model_validate(
                value["request"]
            )
            valid = (
                action["action_kind"] == "abort"
                and terminal_request.reason == row["abort_reason"]
            )
    except (TypeError, ValueError) as exc:
        raise WorkspaceIntegrityErrorV2(
            "workspace terminal action request is invalid"
        ) from exc
    if not valid:
        raise WorkspaceIntegrityErrorV2(
            "workspace terminal action request is inconsistent"
        )


def _chunk_action_matches(
    payload: bytes,
    *,
    chunk_index: int,
    chunk_sha256: str,
    chunk_byte_size: int,
) -> bool:
    try:
        value = json.loads(payload)
    except (UnicodeDecodeError, json.JSONDecodeError):
        return False
    return (
        type(value) is dict
        and set(value) == {
            "action",
            "chunk_byte_size",
            "chunk_index",
            "chunk_sha256",
            "if_match",
        }
        and value["action"] == "chunk"
        and type(value["chunk_index"]) is int
        and value["chunk_index"] == chunk_index
        and type(value["chunk_byte_size"]) is int
        and value["chunk_byte_size"] == chunk_byte_size
        and value["chunk_sha256"] == chunk_sha256
        and isinstance(value["if_match"], str)
        and _ETAG_RE.fullmatch(value["if_match"]) is not None
        and _canonical_action_bytes(value) == payload
    )


def _session_etag(session: m.WorkspaceUploadSessionV2) -> str:
    payload = session.model_dump(mode="json", exclude={"etag"})
    encoded = json.dumps(
        payload,
        ensure_ascii=True,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return f'"{hashlib.sha256(encoded).hexdigest()}"'


def _same_upload_authority(
    left: m.WorkspaceUploadSessionV2,
    right: m.WorkspaceUploadSessionV2,
) -> bool:
    return (
        left.upload_id == right.upload_id
        and left.project_id == right.project_id
        and left.expected_project_head_id == right.expected_project_head_id
        and left.expected_project_head_manifest_sha256
        == right.expected_project_head_manifest_sha256
        and left.expected_project_config_sha256
        == right.expected_project_config_sha256
        and left.archive == right.archive
        and left.chunk_byte_size == right.chunk_byte_size
        and left.chunk_count == right.chunk_count
        and left.created_at == right.created_at
    )


def _action_replay(
    connection: sqlite3.Connection,
    *,
    upload_id: str,
    idempotency_key: str,
    request_json: bytes,
) -> m.WorkspaceUploadSessionV2 | None:
    row = connection.execute(
        "SELECT request_sha256, request_json, response_sha256, response_json "
        "FROM upload_actions "
        "WHERE upload_id = ? AND idempotency_key = ?",
        (upload_id, idempotency_key),
    ).fetchone()
    if row is None:
        return None
    if (
        row["request_sha256"] != hashlib.sha256(request_json).hexdigest()
        or bytes(row["request_json"]) != request_json
    ):
        raise WorkspaceIdempotencyConflictV2(
            "workspace action idempotency key was reused"
        )
    response_json = bytes(row["response_json"])
    response = _session_from_bytes(response_json)
    if (
        hashlib.sha256(response_json).hexdigest() != row["response_sha256"]
        or response.upload_id != upload_id
    ):
        raise WorkspaceIntegrityErrorV2(
            "workspace action replay response is inconsistent"
        )
    return response


def _empty_archive_declaration() -> m.WorkspaceArchiveDeclarationV2:
    return m.WorkspaceArchiveDeclarationV2(
        format="openevo_deterministic_tar_v1",
        media_type="application/vnd.openevo.workspace-tar",
        content_sha256=hashlib.sha256(b"\0" * 1024).hexdigest(),
        byte_size=1024,
        entry_count=0,
        extracted_byte_size=0,
    )


def _snapshot_for(
    project_id: str,
    archive: m.WorkspaceArchiveDeclarationV2,
) -> m.WorkspaceSnapshotRefV2:
    manifest_payload = {
        "archive": archive.model_dump(mode="json"),
        "manifest_contract_version": "2",
        "project_id": project_id,
    }
    manifest_bytes = json.dumps(
        manifest_payload,
        ensure_ascii=True,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    manifest_sha256 = hashlib.sha256(manifest_bytes).hexdigest()
    return m.WorkspaceSnapshotRefV2(
        workspace_snapshot_id=f"workspace-{manifest_sha256}",
        project_id=project_id,
        manifest_sha256=manifest_sha256,
        entry_count=archive.entry_count,
        byte_size=archive.extracted_byte_size,
    )


def _archive_from_bytes(payload: bytes) -> m.WorkspaceArchiveDeclarationV2:
    try:
        archive = parse_contract_json_bytes(m.WorkspaceArchiveDeclarationV2, payload)
    except (TypeError, ValueError) as exc:
        raise WorkspaceIntegrityErrorV2(
            "persisted workspace archive declaration is invalid"
        ) from exc
    if canonical_contract_bytes(archive) != payload:
        raise WorkspaceIntegrityErrorV2(
            "persisted workspace archive declaration is not canonical"
        )
    return archive


def _snapshot_from_bytes(payload: bytes) -> m.WorkspaceSnapshotRefV2:
    try:
        snapshot = parse_contract_json_bytes(m.WorkspaceSnapshotRefV2, payload)
    except (TypeError, ValueError) as exc:
        raise WorkspaceIntegrityErrorV2(
            "persisted workspace snapshot is invalid"
        ) from exc
    if canonical_contract_bytes(snapshot) != payload:
        raise WorkspaceIntegrityErrorV2(
            "persisted workspace snapshot is not canonical"
        )
    return snapshot


def _session_from_bytes(payload: bytes) -> m.WorkspaceUploadSessionV2:
    try:
        session = parse_contract_json_bytes(m.WorkspaceUploadSessionV2, payload)
    except (TypeError, ValueError) as exc:
        raise WorkspaceIntegrityErrorV2(
            "persisted workspace upload response is invalid"
        ) from exc
    if (
        canonical_contract_bytes(session) != payload
        or _session_etag(session) != session.etag
    ):
        raise WorkspaceIntegrityErrorV2(
            "persisted workspace upload response is not canonical"
        )
    return session


def _v1_archive_declaration(
    archive: m.WorkspaceArchiveDeclarationV2,
) -> WorkspaceArchiveDeclarationV1:
    return WorkspaceArchiveDeclarationV1.model_validate(
        {
            "content_sha256": archive.content_sha256,
            "byte_size": archive.byte_size,
            "format": archive.format,
            "entry_count": archive.entry_count,
            "extracted_byte_size": archive.extracted_byte_size,
            "policy": {
                "media_type": archive.media_type,
                "tar_format": "posix_ustar",
                "entry_types": "regular_files_and_directories",
                "path_policy": "utf8_nfc_posix_relative_ustar_split_v1",
                "entry_order": "header_path_byte_lexicographic_parents_first",
                "metadata_policy": "uid_gid_zero_names_empty_mtime_zero",
                "header_policy": "posix_ustar_canonical_header_v1",
                "body_policy": "zero_pad_to_512_bytes",
                "terminator_policy": "two_zero_blocks_no_trailing_bytes",
                "file_mode_policy": "0644_or_0755",
                "directory_mode": "0755",
                "allow_symlinks": False,
                "allow_hardlinks": False,
                "allow_devices": False,
                "allow_fifos": False,
                "allow_sparse_files": False,
                "allow_tar_extensions": False,
                "max_entries": 100_000,
                "max_path_depth": 32,
                "max_path_bytes": 256,
                "max_file_bytes": 0o77777777777,
                "max_extracted_bytes": m.MAX_SNAPSHOT_BYTES,
            },
        }
    )


def _marker_bytes(
    store_id: str,
    *,
    root: os.stat_result,
    database: os.stat_result,
    uploads: os.stat_result,
    snapshots: os.stat_result,
) -> bytes:
    return json.dumps(
        {
            "binding_version": "1",
            "database": [database.st_dev, database.st_ino],
            "root": [root.st_dev, root.st_ino],
            "schema_version": _SCHEMA_VERSION,
            "snapshots": [snapshots.st_dev, snapshots.st_ino],
            "store_id": store_id,
            "uploads": [uploads.st_dev, uploads.st_ino],
        },
        ensure_ascii=True,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _schema_rows(connection: sqlite3.Connection) -> tuple[tuple[object, ...], ...]:
    rows = connection.execute(
        "SELECT type, name, tbl_name, sql FROM sqlite_schema "
        "WHERE name NOT LIKE 'sqlite_%' ORDER BY type, name"
    ).fetchall()
    return tuple(tuple(row) for row in rows)


def _expected_schema_rows() -> tuple[tuple[object, ...], ...]:
    connection = sqlite3.connect(":memory:")
    try:
        connection.executescript(_SCHEMA)
        return _schema_rows(connection)
    finally:
        connection.close()


def _canonical_action_bytes(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=True,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _exact_model(model_type, value):
    if type(value) is not model_type:
        raise TypeError(f"workspace value must be exact {model_type.__name__}")
    return model_type.model_validate(value.model_dump(mode="python"))


def _resource_id(value: str, *, label: str) -> str:
    if not isinstance(value, str) or _ID_RE.fullmatch(value) is None:
        raise ValueError(f"workspace {label} ID is invalid")
    return value


def _sha256(value: str, *, label: str) -> str:
    if not isinstance(value, str) or _SHA256_RE.fullmatch(value) is None:
        raise ValueError(f"workspace {label} digest is invalid")
    return value


def _etag(value: str) -> str:
    if not isinstance(value, str) or _ETAG_RE.fullmatch(value) is None:
        raise ValueError("workspace ETag is invalid")
    return value


def _idempotency_key(value: str) -> str:
    if (
        not isinstance(value, str)
        or not 1 <= len(value) <= 256
        or any(ord(character) < 32 or ord(character) == 127 for character in value)
    ):
        raise ValueError("workspace idempotency key is invalid")
    return value


def _timestamp(value: datetime) -> str:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise TypeError("workspace timestamp requires an aware datetime")
    return value.astimezone(timezone.utc).isoformat(timespec="microseconds").replace(
        "+00:00",
        "Z",
    )


def _timestamp_text(value: str) -> str:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("workspace timestamp is invalid") from exc
    if parsed.tzinfo is None or _timestamp(parsed) != value:
        raise ValueError("workspace timestamp is not canonical")
    return value


def _archive_name(upload_id: str) -> str:
    return f"{_resource_id(upload_id, label='upload')}.part"


def _open_directory_at(parent_fd: int, name: str, *, mode: int) -> int:
    try:
        descriptor = os.open(
            name,
            os.O_RDONLY
            | getattr(os, "O_DIRECTORY", 0)
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0),
            dir_fd=parent_fd,
        )
    except OSError as exc:
        raise WorkspaceIntegrityErrorV2(f"{name} directory is unavailable") from exc
    try:
        metadata = _require_directory(descriptor, mode=mode, label=name)
        _require_entry_binding(parent_fd, name, metadata)
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def _require_directory(
    descriptor: int,
    *,
    mode: int,
    label: str,
) -> os.stat_result:
    metadata = os.fstat(descriptor)
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or metadata.st_uid != os.geteuid()
        or stat.S_IMODE(metadata.st_mode) != mode
    ):
        raise WorkspaceIntegrityErrorV2(f"{label} directory metadata is invalid")
    return metadata


def _require_regular_file(
    descriptor: int,
    *,
    expected_size: int | None,
    label: str,
) -> os.stat_result:
    metadata = os.fstat(descriptor)
    if (
        not stat.S_ISREG(metadata.st_mode)
        or metadata.st_uid != os.geteuid()
        or metadata.st_nlink != 1
        or stat.S_IMODE(metadata.st_mode) != 0o600
        or (expected_size is not None and metadata.st_size != expected_size)
    ):
        raise WorkspaceIntegrityErrorV2(f"{label} metadata is invalid")
    return metadata


def _require_entry_binding(
    parent_fd: int,
    name: str,
    expected: os.stat_result,
) -> None:
    try:
        current = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    except OSError as exc:
        raise WorkspaceIntegrityErrorV2("workspace entry binding is missing") from exc
    if not _same_identity(current, expected):
        raise WorkspaceIntegrityErrorV2("workspace entry binding changed")


def _entry_exists(parent_fd: int, name: str) -> bool:
    try:
        os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    except FileNotFoundError:
        return False
    return True


def _same_identity(left: os.stat_result, right: os.stat_result) -> bool:
    return left.st_dev == right.st_dev and left.st_ino == right.st_ino


def _write_all(descriptor: int, content: bytes) -> None:
    view = memoryview(content)
    while view:
        written = os.write(descriptor, view)
        if written <= 0:
            raise WorkspaceIntegrityErrorV2("workspace write made no progress")
        view = view[written:]


def _read_exact(descriptor: int, size: int) -> bytes:
    content = bytearray()
    while len(content) < size:
        chunk = os.read(descriptor, size - len(content))
        if not chunk:
            break
        content.extend(chunk)
    if len(content) != size:
        raise WorkspaceIntegrityErrorV2("workspace file ended before its bound")
    return bytes(content)


def _pread_exact(descriptor: int, size: int, *, offset: int) -> bytes:
    content = bytearray()
    while len(content) < size:
        chunk = os.pread(descriptor, size - len(content), offset + len(content))
        if not chunk:
            break
        content.extend(chunk)
    if len(content) != size:
        raise WorkspaceIntegrityErrorV2("workspace chunk ended before its bound")
    return bytes(content)


def _discard_regular_file_if_owned(
    parent_fd: int,
    name: str,
    *,
    expected_size: int | None,
) -> None:
    try:
        descriptor = os.open(
            name,
            os.O_RDONLY
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0),
            dir_fd=parent_fd,
        )
    except FileNotFoundError:
        return
    except OSError as exc:
        raise WorkspaceIntegrityErrorV2(
            "workspace orphan file is unsafe"
        ) from exc
    try:
        metadata = _require_regular_file(
            descriptor,
            expected_size=expected_size,
            label="workspace orphan file",
        )
        _require_entry_binding(parent_fd, name, metadata)
        os.unlink(name, dir_fd=parent_fd)
        os.fsync(parent_fd)
    finally:
        os.close(descriptor)


__all__ = [
    "WorkspaceConflictV2",
    "WorkspaceIdempotencyConflictV2",
    "WorkspaceIntegrityErrorV2",
    "WorkspaceNotFoundV2",
    "WorkspacePreconditionFailedV2",
    "WorkspaceStoreV2",
    "WorkspaceStoreV2Error",
]
