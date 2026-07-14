from __future__ import annotations

import base64
from collections.abc import Callable, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
import fcntl
import hashlib
import hmac
import json
import os
from pathlib import Path
import secrets
import sqlite3
import stat
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
_IDEMPOTENCY_RESPONSE_MODELS: dict[str, type[BaseModel]] = {
    model.__name__: model
    for model in (
        m.ProjectV1,
        m.WorkspaceUploadSessionV1,
        m.WorkspaceUploadFinalizeResponseV1,
        m.ProjectValidationResponseV1,
    )
}


class CoreControlStoreError(Exception):
    pass


class StoreCorruptionError(CoreControlStoreError):
    pass


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


_SCHEMA = (
    """
    CREATE TABLE IF NOT EXISTS metadata (
        key TEXT PRIMARY KEY,
        value BLOB NOT NULL
    ) STRICT
    """,
    """
    CREATE TABLE IF NOT EXISTS projects (
        project_id TEXT PRIMARY KEY,
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
        document_json BLOB NOT NULL,
        resource_version INTEGER NOT NULL CHECK (resource_version >= 1),
        file_name TEXT NOT NULL,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        FOREIGN KEY (project_id) REFERENCES projects(project_id) ON DELETE CASCADE
    ) STRICT
    """,
    """
    CREATE TABLE IF NOT EXISTS idempotency_records (
        operation_id TEXT NOT NULL,
        resource_scope TEXT NOT NULL,
        idempotency_key TEXT NOT NULL,
        request_digest TEXT NOT NULL,
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
        self.root = Path(state_root).expanduser().resolve() / "core-control-v1"
        self.upload_root = self.root / "workspace-uploads"
        self.workspace_root = self.root / "workspace-snapshots"
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._event_replay_limit = event_replay_limit
        self._mutex = threading.RLock()
        self._closed = False
        self._prepare_root()
        self._lock_file = (self.root / "provider.lock").open("a+b")
        try:
            fcntl.flock(self._lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            self._lock_file.close()
            raise CoreControlStoreError("Core Control provider state is already owned") from exc
        self._connection = sqlite3.connect(
            self.root / "provider.sqlite3",
            isolation_level=None,
            check_same_thread=False,
            timeout=30,
        )
        self._connection.row_factory = sqlite3.Row
        self._connection.execute("PRAGMA foreign_keys = ON")
        self._connection.execute("PRAGMA journal_mode = WAL")
        self._connection.execute("PRAGMA synchronous = FULL")
        with self._transaction():
            for statement in _SCHEMA:
                self._connection.execute(statement)
            self._signing_key = self._load_or_create_signing_key()
        try:
            self._harden_database_files()
            self._recover_and_validate()
        except Exception:
            self._connection.close()
            fcntl.flock(self._lock_file.fileno(), fcntl.LOCK_UN)
            self._lock_file.close()
            self._closed = True
            raise

    def close(self) -> None:
        with self._mutex:
            if self._closed:
                return
            self._connection.close()
            fcntl.flock(self._lock_file.fileno(), fcntl.LOCK_UN)
            self._lock_file.close()
            self._closed = True

    def create_project(
        self,
        request: m.ProjectCreateV1,
        *,
        idempotency_key: str,
        registry_digest: str | None,
    ) -> StoredResult:
        digest = _request_digest("createCoreProjectV1", "projects", request, {})
        with self._mutex, self._transaction():
            replay = self._idempotency_replay(
                "createCoreProjectV1",
                "projects",
                idempotency_key,
                digest,
                m.ProjectV1,
            )
            if replay is not None:
                return replay
            now = self._timestamp()
            project = self._new_project(request, now=now, registry_digest=registry_digest)
            self._connection.execute(
                "INSERT INTO projects VALUES (?, ?, 1, ?, ?)",
                (project.id, _model_bytes(project), now, now),
            )
            self._append_project_event(project, now=now)
            result = StoredResult(201, project, project.etag)
            self._store_idempotency(
                "createCoreProjectV1", "projects", idempotency_key, digest, result
            )
            return result

    def get_project(self, project_id: str) -> m.ProjectV1:
        with self._mutex:
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
        query_binding = f"projects:{sort}:{direction}"
        boundary: tuple[str, str] | None = None
        if after is not None:
            boundary = self._decode_cursor(after, query_binding)
        with self._mutex:
            rows = self._connection.execute("SELECT document_json FROM projects").fetchall()
        projects = [_validate_bytes(m.ProjectV1, row["document_json"]) for row in rows]
        projects.sort(
            key=lambda item: (str(getattr(item, sort)), item.id), reverse=direction == "desc"
        )
        if boundary is not None:
            if direction == "asc":
                projects = [
                    item for item in projects if (str(getattr(item, sort)), item.id) > boundary
                ]
            else:
                projects = [
                    item for item in projects if (str(getattr(item, sort)), item.id) < boundary
                ]
        selected = projects[: limit + 1]
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
        digest = _request_digest("patchCoreProjectV1", project_id, request, headers)
        with self._mutex, self._transaction():
            replay = self._idempotency_replay(
                "patchCoreProjectV1", project_id, idempotency_key, digest, m.ProjectV1
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
                "patchCoreProjectV1", project_id, idempotency_key, digest, result
            )
            return result

    def delete_project(
        self,
        project_id: str,
        *,
        if_match: str,
        idempotency_key: str,
    ) -> StoredResult:
        digest = _request_digest("deleteCoreProjectV1", project_id, None, {"if-match": if_match})
        upload_files: list[Path] = []
        with self._mutex, self._transaction():
            replay = self._idempotency_replay(
                "deleteCoreProjectV1", project_id, idempotency_key, digest, None
            )
            if replay is not None:
                return replay
            _, current = self._project_row(project_id)
            self._require_etag(current.etag, if_match, "project")
            upload_files = [
                self.upload_root / row["file_name"]
                for row in self._connection.execute(
                    "SELECT file_name FROM workspace_uploads WHERE project_id = ?", (project_id,)
                ).fetchall()
            ]
            self._connection.execute("DELETE FROM projects WHERE project_id = ?", (project_id,))
            result = StoredResult(204, None)
            self._store_idempotency(
                "deleteCoreProjectV1", project_id, idempotency_key, digest, result
            )
        for path in upload_files:
            path.unlink(missing_ok=True)
        return result

    def create_upload(
        self,
        project_id: str,
        request: m.WorkspaceUploadCreateV1,
        *,
        if_match: str,
        idempotency_key: str,
    ) -> StoredResult:
        digest = _request_digest(
            "createCoreWorkspaceUploadV1",
            project_id,
            request,
            {"if-match": if_match},
        )
        with self._mutex, self._transaction():
            replay = self._idempotency_replay(
                "createCoreWorkspaceUploadV1",
                project_id,
                idempotency_key,
                digest,
                m.WorkspaceUploadSessionV1,
            )
            if replay is not None:
                return replay
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
            file_path = self.upload_root / file_name
            fd = os.open(
                file_path,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
                0o600,
            )
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
            _fsync_directory(self.upload_root)
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
            session = m.WorkspaceUploadSessionV1(
                **session_data, etag=_etag(session_data, version=1)
            )
            self._connection.execute(
                "INSERT INTO workspace_uploads VALUES (?, ?, ?, 1, ?, ?, ?)",
                (upload_id, project_id, _model_bytes(session), file_name, now, now),
            )
            result = StoredResult(201, session, session.etag)
            self._store_idempotency(
                "createCoreWorkspaceUploadV1",
                project_id,
                idempotency_key,
                digest,
                result,
            )
            return result

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
        digest = _request_digest(
            "putCoreWorkspaceUploadChunkV1", scope, request, {"if-match": if_match}
        )
        content = base64.b64decode(request.content_base64, validate=True)
        with self._mutex:
            old_offset = 0
            file_path: Path | None = None
            file_fd: int | None = None
            try:
                with self._transaction():
                    replay = self._idempotency_replay(
                        "putCoreWorkspaceUploadChunkV1",
                        scope,
                        idempotency_key,
                        digest,
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
                    file_path = self.upload_root / row["file_name"]
                    file_fd = os.open(
                        file_path,
                        os.O_RDWR | getattr(os, "O_NOFOLLOW", 0),
                    )
                    _require_bound_regular_file(file_path, file_fd, expected_size=old_offset)
                    os.lseek(file_fd, old_offset, os.SEEK_SET)
                    _write_all(file_fd, content)
                    os.fsync(file_fd)
                    _require_bound_regular_file(
                        file_path,
                        file_fd,
                        expected_size=old_offset + len(content),
                    )
                    now = self._timestamp()
                    version = int(row["resource_version"]) + 1
                    updated_data = upload.model_dump(mode="python", exclude={"etag"})
                    updated_data.update(accepted_offset=old_offset + len(content), updated_at=now)
                    updated = m.WorkspaceUploadSessionV1(
                        **updated_data, etag=_etag(updated_data, version=version)
                    )
                    self._connection.execute(
                        "UPDATE workspace_uploads SET document_json = ?, resource_version = ?, "
                        "updated_at = ? WHERE upload_id = ?",
                        (_model_bytes(updated), version, now, upload_id),
                    )
                    result = StoredResult(200, updated, updated.etag)
                    self._store_idempotency(
                        "putCoreWorkspaceUploadChunkV1",
                        scope,
                        idempotency_key,
                        digest,
                        result,
                    )
                    return result
            except Exception:
                if file_fd is not None:
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
        digest = _request_digest(
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
                    digest,
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
                archive_path = self.upload_root / upload_row["file_name"]

            now = self._timestamp()
            workspace_snapshot = _snapshot(
                m.SnapshotKind.WORKSPACE,
                {"archive_sha256": upload.archive.content_sha256},
                now,
            )
            destination = self.workspace_root / workspace_snapshot.id
            try:
                verify_and_materialize_workspace(archive_path, upload.archive, destination)
            except WorkspaceArchiveError as exc:
                raise ResourceConflictError(
                    "workspace_archive_invalid", "The workspace archive is not canonical."
                ) from exc

            with self._transaction():
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
                    content_id=_new_id("workspace-content"),
                    sha256=upload.archive.content_sha256,
                    byte_size=upload.archive.byte_size,
                )
                publication = m.WorkspacePublicationV1(
                    archive=upload.archive,
                    content_ref=content_ref,
                    workspace_snapshot=workspace_snapshot,
                    published_at=now,
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
                    m.SnapshotKind.PROJECT, _project_snapshot_payload(project_data), now
                )
                project_data["current_project_snapshot"] = project_snapshot
                updated_project = m.ProjectV1(
                    **project_data, etag=_etag(project_data, version=project_version)
                )
                upload_version = int(upload_row["resource_version"]) + 1
                upload_data = upload.model_dump(mode="python", exclude={"etag"})
                upload_data.update(
                    status=m.WorkspaceUploadStatus.FINALIZED,
                    publication=publication,
                    updated_at=now,
                )
                updated_upload = m.WorkspaceUploadSessionV1(
                    **upload_data, etag=_etag(upload_data, version=upload_version)
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
                    digest,
                    result,
                )
                return result

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
        digest = _request_digest(
            "abortCoreWorkspaceUploadV1", scope, request, {"if-match": if_match}
        )
        with self._mutex, self._transaction():
            replay = self._idempotency_replay(
                "abortCoreWorkspaceUploadV1",
                scope,
                idempotency_key,
                digest,
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
            now = self._timestamp()
            version = int(row["resource_version"]) + 1
            data = upload.model_dump(mode="python", exclude={"etag"})
            data.update(status=m.WorkspaceUploadStatus.ABORTED, updated_at=now)
            updated = m.WorkspaceUploadSessionV1(**data, etag=_etag(data, version=version))
            self._connection.execute(
                "UPDATE workspace_uploads SET document_json = ?, resource_version = ?, "
                "updated_at = ? WHERE upload_id = ?",
                (_model_bytes(updated), version, now, upload_id),
            )
            result = StoredResult(200, updated, updated.etag)
            self._store_idempotency(
                "abortCoreWorkspaceUploadV1", scope, idempotency_key, digest, result
            )
        (self.upload_root / row["file_name"]).unlink(missing_ok=True)
        return result

    def store_validation_result(
        self,
        project_id: str,
        request: m.ProjectValidationRequestV1,
        *,
        idempotency_key: str,
        response_factory: Callable[[m.ProjectV1], m.ProjectValidationResponseV1],
    ) -> StoredResult:
        digest = _request_digest("validateCoreProjectV1", project_id, request, {})
        with self._mutex, self._transaction():
            replay = self._idempotency_replay(
                "validateCoreProjectV1",
                project_id,
                idempotency_key,
                digest,
                m.ProjectValidationResponseV1,
            )
            if replay is not None:
                return replay
            _, project = self._project_row(project_id)
            response = response_factory(project)
            result = StoredResult(200, response)
            self._store_idempotency(
                "validateCoreProjectV1", project_id, idempotency_key, digest, result
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
                "SELECT frame_json FROM events WHERE sequence > ? ORDER BY sequence ASC",
                (after_sequence,),
            ).fetchall()
            frames: list[dict[str, object]] = []
            for row in rows:
                frame = _validate_bytes(m.SseFrameV1, row["frame_json"])
                frames.append(frame.model_dump(mode="json"))
            return frames

    def event_cursor(self, sequence: int) -> str:
        body = f"evt.v1.{sequence}"
        signature = hmac.new(self._signing_key, body.encode("ascii"), hashlib.sha256).hexdigest()[
            :24
        ]
        return f"{body}.{signature}"

    def _new_project(
        self,
        request: m.ProjectCreateV1,
        *,
        now: str,
        registry_digest: str | None,
    ) -> m.ProjectV1:
        project_id = _new_id("project")
        task_snapshot = _snapshot(m.SnapshotKind.TASK, request.task.model_dump(mode="json"), now)
        workspace_snapshot = None
        if isinstance(request.workspace, m.ScratchWorkspaceSpecV1):
            workspace_snapshot = m.ImmutableSnapshotRefV1(
                id=_new_id("workspace-snapshot"),
                kind=m.SnapshotKind.WORKSPACE,
                content_sha256=_EMPTY_WORKSPACE_DIGEST,
                created_at=now,
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
        project_snapshot = _snapshot(m.SnapshotKind.PROJECT, _project_snapshot_payload(data), now)
        data["current_project_snapshot"] = project_snapshot
        return m.ProjectV1(**data, etag=_etag(data, version=1))

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
            task_snapshot = _snapshot(m.SnapshotKind.TASK, task.model_dump(mode="json"), now)
        workspace_snapshot = current.current_workspace_snapshot
        publication = current.workspace_publication
        if "workspace" in fields and workspace != current.workspace:
            publication = None
            if isinstance(workspace, m.ScratchWorkspaceSpecV1):
                workspace_snapshot = m.ImmutableSnapshotRefV1(
                    id=_new_id("workspace-snapshot"),
                    kind=m.SnapshotKind.WORKSPACE,
                    content_sha256=_EMPTY_WORKSPACE_DIGEST,
                    created_at=now,
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
            m.SnapshotKind.PROJECT, _project_snapshot_payload(data), now
        )
        return m.ProjectV1(**data, etag=_etag(data, version=version))

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
        row = self._connection.execute(
            "SELECT * FROM projects WHERE project_id = ?", (project_id,)
        ).fetchone()
        if row is None:
            raise ResourceNotFoundError("project", project_id)
        return row, _validate_bytes(m.ProjectV1, row["document_json"])

    def _upload_row(
        self, project_id: str, upload_id: str
    ) -> tuple[sqlite3.Row, m.WorkspaceUploadSessionV1]:
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
        request_digest: str,
        response_model: type[_ModelT] | None,
    ) -> StoredResult | None:
        row = self._connection.execute(
            "SELECT * FROM idempotency_records WHERE operation_id = ? "
            "AND resource_scope = ? AND idempotency_key = ?",
            (operation_id, scope, key),
        ).fetchone()
        if row is None:
            return None
        if not hmac.compare_digest(row["request_digest"], request_digest):
            raise IdempotencyConflictError("idempotency key was reused")
        model = None
        if response_model is not None:
            if row["response_json"] is None or row["response_type"] != response_model.__name__:
                raise StoreCorruptionError("idempotency response type is invalid")
            model = _validate_bytes(response_model, row["response_json"])
        elif row["response_json"] is not None or row["response_type"] != "NoContent":
            raise StoreCorruptionError("no-content idempotency response is invalid")
        return StoredResult(int(row["status_code"]), model, row["etag"], replayed=True)

    def _store_idempotency(
        self,
        operation_id: str,
        scope: str,
        key: str,
        request_digest: str,
        result: StoredResult,
    ) -> None:
        now = int(time.time())
        self._connection.execute(
            "DELETE FROM idempotency_records WHERE expires_at_epoch <= ?", (now,)
        )
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
            "INSERT INTO idempotency_records VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                operation_id,
                scope,
                key,
                request_digest,
                result.status_code,
                response_type,
                response_bytes,
                result.etag,
                now,
                now + _IDEMPOTENCY_RETENTION_SECONDS,
            ),
        )

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

    def _recover_and_validate(self) -> None:
        with self._mutex, self._transaction():
            project_publications: dict[str, m.WorkspacePublicationV1] = {}
            for row in self._connection.execute("SELECT * FROM projects"):
                project = _validate_bytes(m.ProjectV1, row["document_json"])
                version = int(row["resource_version"])
                project_data = project.model_dump(mode="python", exclude={"etag"})
                if (
                    project.id != row["project_id"]
                    or project.created_at != row["created_at"]
                    or project.updated_at != row["updated_at"]
                    or project.etag != _etag(project_data, version=version)
                ):
                    raise StoreCorruptionError("project row identity is invalid")
                if project.workspace_publication is not None:
                    publication = project.workspace_publication
                    expected_digest = hashlib.sha256(
                        _canonical_bytes({"archive_sha256": publication.archive.content_sha256})
                    ).hexdigest()
                    if publication.workspace_snapshot.content_sha256 != expected_digest:
                        raise StoreCorruptionError("workspace snapshot digest binding is invalid")
                    existing = project_publications.setdefault(
                        publication.workspace_snapshot.id, publication
                    )
                    if existing != publication:
                        raise StoreCorruptionError("workspace snapshot publication is ambiguous")
            referenced_files: set[str] = set()
            snapshot_sources: dict[str, tuple[m.WorkspacePublicationV1, str]] = {}
            for row in self._connection.execute("SELECT * FROM workspace_uploads"):
                upload = _validate_bytes(m.WorkspaceUploadSessionV1, row["document_json"])
                version = int(row["resource_version"])
                upload_data = upload.model_dump(mode="python", exclude={"etag"})
                if (
                    upload.id != row["upload_id"]
                    or upload.project_id != row["project_id"]
                    or upload.created_at != row["created_at"]
                    or upload.updated_at != row["updated_at"]
                    or upload.etag != _etag(upload_data, version=version)
                ):
                    raise StoreCorruptionError("workspace upload row identity is invalid")
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
                    existing = snapshot_sources.setdefault(
                        snapshot_id, (publication, file_name)
                    )
                    if existing[0] != publication:
                        raise StoreCorruptionError("workspace snapshot source is ambiguous")
            for snapshot_id, publication in project_publications.items():
                source = snapshot_sources.get(snapshot_id)
                if source is None or source[0] != publication:
                    raise StoreCorruptionError(
                        "published project workspace has no authoritative upload"
                    )
            for row in self._connection.execute(
                "SELECT sequence, event_id, frame_json FROM events ORDER BY sequence"
            ):
                frame = _validate_bytes(m.SseFrameV1, row["frame_json"])
                if frame.id != row["event_id"] or frame.data.root.sequence != row["sequence"]:
                    raise StoreCorruptionError("event row identity is invalid")
            for row in self._connection.execute("SELECT * FROM idempotency_records"):
                response_type = row["response_type"]
                response_json = row["response_json"]
                if response_type == "NoContent":
                    if response_json is not None or int(row["status_code"]) != 204:
                        raise StoreCorruptionError("no-content idempotency row is invalid")
                else:
                    response_model = _IDEMPOTENCY_RESPONSE_MODELS.get(response_type)
                    if response_model is None or response_json is None:
                        raise StoreCorruptionError("idempotency response type is invalid")
                    _validate_bytes(response_model, response_json)
                if len(row["request_digest"]) != 64:
                    raise StoreCorruptionError("idempotency request digest is invalid")
            for row in self._connection.execute("SELECT * FROM failed_idempotency_records"):
                _validate_bytes(m.ApiErrorV1, row["error_json"])
                if len(row["request_digest"]) != 64:
                    raise StoreCorruptionError("failed idempotency request digest is invalid")

            with self._managed_root_fd(self.upload_root) as upload_root_fd:
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
                for name in os.listdir(upload_root_fd):
                    if name not in referenced_files:
                        _remove_entry_at(upload_root_fd, name)
                with self._managed_root_fd(self.workspace_root) as workspace_root_fd:
                    for snapshot_id, (publication, archive_name) in snapshot_sources.items():
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
                    for name in os.listdir(workspace_root_fd):
                        if name not in snapshot_sources:
                            _remove_entry_at(workspace_root_fd, name)

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

    @contextmanager
    def _managed_root_fd(self, path: Path):
        try:
            fd = os.open(
                path,
                os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0),
            )
        except OSError as exc:
            raise StoreCorruptionError("Core Control managed root is unavailable") from exc
        try:
            identity = os.fstat(fd)
            _require_private_directory_metadata(identity)
            _require_path_binding(path, identity)
            yield fd
            _require_path_binding(path, identity)
        finally:
            os.close(fd)

    def _prepare_root(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self.root, 0o700)
        metadata = self.root.stat(follow_symlinks=False)
        if not stat.S_ISDIR(metadata.st_mode) or metadata.st_uid != os.geteuid():
            raise CoreControlStoreError("Core Control provider root is not privately owned")
        self.upload_root.mkdir(mode=0o700, exist_ok=True)
        self.workspace_root.mkdir(mode=0o700, exist_ok=True)
        for managed_root in (self.upload_root, self.workspace_root):
            metadata = managed_root.stat(follow_symlinks=False)
            if not stat.S_ISDIR(metadata.st_mode) or metadata.st_uid != os.geteuid():
                raise CoreControlStoreError("Core Control managed root is not privately owned")
            os.chmod(managed_root, 0o700)

    def _load_or_create_signing_key(self) -> bytes:
        row = self._connection.execute(
            "SELECT value FROM metadata WHERE key = 'signing_key'"
        ).fetchone()
        if row is not None:
            value = bytes(row["value"])
            if len(value) != 32:
                raise StoreCorruptionError("provider signing key is invalid")
            return value
        value = secrets.token_bytes(32)
        self._connection.execute(
            "INSERT INTO metadata(key, value) VALUES ('signing_key', ?)", (value,)
        )
        return value

    def _harden_database_files(self) -> None:
        for suffix in ("", "-wal", "-shm"):
            path = self.root / f"provider.sqlite3{suffix}"
            if path.exists():
                os.chmod(path, 0o600)

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
        return _Transaction(self._connection)


class _Transaction:
    def __init__(self, connection: sqlite3.Connection) -> None:
        self.connection = connection

    def __enter__(self):
        self.connection.execute("BEGIN IMMEDIATE")
        return self

    def __exit__(self, exc_type, exc, traceback) -> bool:
        self.connection.execute("COMMIT" if exc_type is None else "ROLLBACK")
        return False


def _new_id(prefix: str) -> str:
    return f"{prefix}-{secrets.token_hex(16)}"


def _snapshot(kind: m.SnapshotKind, payload: object, now: str) -> m.ImmutableSnapshotRefV1:
    return m.ImmutableSnapshotRefV1(
        id=_new_id(f"{kind.value}-snapshot"),
        kind=kind,
        content_sha256=hashlib.sha256(_canonical_bytes(payload)).hexdigest(),
        created_at=now,
    )


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


def _request_digest(
    operation_id: str,
    scope: str,
    request: BaseModel | None,
    semantic_headers: Mapping[str, str],
) -> str:
    return hashlib.sha256(
        _canonical_bytes(
            {
                "principal": "core-control-v1",
                "operation_id": operation_id,
                "scope": scope,
                "request": _json_value(request),
                "semantic_headers": dict(sorted(semantic_headers.items())),
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


def _write_all(fd: int, content: bytes) -> None:
    view = memoryview(content)
    while view:
        written = os.write(fd, view)
        if written <= 0:
            raise OSError("failed to write workspace upload")
        view = view[written:]


def _require_bound_regular_file(path: Path, fd: int, *, expected_size: int) -> None:
    metadata = os.fstat(fd)
    _require_private_regular_metadata(metadata)
    if metadata.st_size != expected_size:
        raise StoreCorruptionError("workspace upload file size is invalid")
    _require_path_binding(path, metadata)


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
        raise StoreCorruptionError("workspace upload file is unsafe")


def _require_private_directory_metadata(metadata: os.stat_result) -> None:
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or metadata.st_uid != os.geteuid()
        or stat.S_IMODE(metadata.st_mode) != 0o700
    ):
        raise StoreCorruptionError("Core Control managed root is not privately owned")


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


def _remove_entry_at(parent_fd: int, name: str) -> None:
    try:
        metadata = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    except FileNotFoundError:
        return
    except OSError as exc:
        raise StoreCorruptionError("Core Control managed orphan is unreadable") from exc
    if metadata.st_uid != os.geteuid():
        raise StoreCorruptionError("Core Control managed orphan has the wrong owner")
    if not stat.S_ISDIR(metadata.st_mode):
        try:
            os.unlink(name, dir_fd=parent_fd)
        except OSError as exc:
            raise StoreCorruptionError("Core Control managed orphan could not be removed") from exc
        return

    try:
        child_fd = os.open(
            name,
            os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0),
            dir_fd=parent_fd,
        )
    except OSError as exc:
        raise StoreCorruptionError("Core Control managed orphan directory is unsafe") from exc
    try:
        if not _same_identity(os.fstat(child_fd), metadata):
            raise StoreCorruptionError("Core Control managed orphan binding changed")
        for child_name in os.listdir(child_fd):
            _remove_entry_at(child_fd, child_name)
        _require_entry_binding(parent_fd, name, metadata)
    finally:
        os.close(child_fd)
    try:
        os.rmdir(name, dir_fd=parent_fd)
    except OSError as exc:
        raise StoreCorruptionError("Core Control managed orphan directory could not be removed") from exc


def _fsync_directory(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


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
    "ResourceConflictError",
    "ResourceNotFoundError",
    "StoreCorruptionError",
    "StoredResult",
]
