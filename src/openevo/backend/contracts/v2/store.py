"""Small durable catalog for Core Control API v2 project metadata.

Task, Attempt, transition, and project-head authority deliberately remain owned by
``ScienceTaskStoreV2``.  This store persists the user-facing project catalog,
project mutation/validation replay records, and opaque genesis-publication journal;
the provider joins it with the science authority at read time so a catalog row can
never manufacture an active project head.
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
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

from . import models as m
from .snapshots import canonical_contract_bytes, parse_contract_json_bytes


_SCHEMA = """
CREATE TABLE IF NOT EXISTS metadata (
    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
    schema_version INTEGER NOT NULL CHECK (schema_version = 2)
) STRICT;
CREATE TABLE IF NOT EXISTS projects (
    project_id TEXT PRIMARY KEY,
    display_name TEXT NOT NULL,
    project_config_sha256 TEXT NOT NULL CHECK (length(project_config_sha256) = 64),
    project_config_json BLOB NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    resource_version INTEGER NOT NULL CHECK (resource_version >= 1)
) STRICT;
CREATE TABLE IF NOT EXISTS project_create_requests (
    idempotency_key TEXT PRIMARY KEY,
    request_sha256 TEXT NOT NULL CHECK (length(request_sha256) = 64),
    request_json BLOB NOT NULL,
    project_id TEXT NOT NULL UNIQUE,
    FOREIGN KEY(project_id) REFERENCES projects(project_id) ON DELETE RESTRICT
) STRICT;
CREATE TABLE IF NOT EXISTS project_update_requests (
    project_id TEXT NOT NULL,
    idempotency_key TEXT NOT NULL,
    request_sha256 TEXT NOT NULL CHECK (length(request_sha256) = 64),
    request_json BLOB NOT NULL,
    PRIMARY KEY(project_id, idempotency_key),
    FOREIGN KEY(project_id) REFERENCES projects(project_id) ON DELETE RESTRICT
) STRICT;
CREATE TABLE IF NOT EXISTS project_validation_requests (
    project_id TEXT NOT NULL,
    idempotency_key TEXT NOT NULL,
    request_sha256 TEXT NOT NULL CHECK (length(request_sha256) = 64),
    request_json BLOB NOT NULL,
    response_json BLOB,
    PRIMARY KEY(project_id, idempotency_key),
    FOREIGN KEY(project_id) REFERENCES projects(project_id) ON DELETE RESTRICT
) STRICT;
CREATE TABLE IF NOT EXISTS project_authority_records (
    project_id TEXT PRIMARY KEY,
    record_sha256 TEXT NOT NULL CHECK (length(record_sha256) = 64),
    record_json BLOB NOT NULL,
    resource_version INTEGER NOT NULL CHECK (resource_version >= 1),
    FOREIGN KEY(project_id) REFERENCES projects(project_id) ON DELETE RESTRICT
) STRICT;
CREATE TABLE IF NOT EXISTS operations (
    operation_id TEXT PRIMARY KEY,
    operation_json BLOB NOT NULL
) STRICT;
CREATE TABLE IF NOT EXISTS action_requests (
    action_scope TEXT NOT NULL,
    idempotency_key TEXT NOT NULL,
    request_sha256 TEXT NOT NULL CHECK (length(request_sha256) = 64),
    request_json BLOB NOT NULL,
    operation_id TEXT UNIQUE,
    PRIMARY KEY(action_scope, idempotency_key),
    FOREIGN KEY(operation_id) REFERENCES operations(operation_id) ON DELETE RESTRICT
) STRICT;
"""
_MAX_PROJECTS = 10_000
_MAX_OPERATIONS = 100_000
_MAX_PROJECT_AUTHORITY_BYTES = 256 * 1024
_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z", re.ASCII)
_SHA256_RE = re.compile(r"[0-9a-f]{64}\Z", re.ASCII)
_ETAG_RE = re.compile(r'"[0-9a-f]{64}"\Z', re.ASCII)


class CoreControlStoreV2Error(RuntimeError):
    """Durable v2 catalog could not be trusted or updated."""


class ProjectNotFoundV2(CoreControlStoreV2Error):
    pass


class ProjectConflictV2(CoreControlStoreV2Error):
    pass


class ProjectIdempotencyConflictV2(ProjectConflictV2):
    pass


class ProjectPreconditionFailedV2(ProjectConflictV2):
    pass


class OperationNotFoundV2(CoreControlStoreV2Error):
    pass


@dataclass(frozen=True, slots=True)
class ProjectRecordV2:
    project_id: str
    display_name: str
    config: m.ScienceProjectConfigV2
    project_config_sha256: str
    created_at: str
    updated_at: str
    resource_version: int


@dataclass(frozen=True, slots=True)
class ActionReservationV2:
    operation: m.OperationV2 | None
    resumed: bool


@dataclass(frozen=True, slots=True)
class ProjectAuthorityDocumentV2:
    project_id: str
    record_sha256: str
    record_json: bytes
    resource_version: int


class CoreControlStoreV2:
    """Private, single-process SQLite owner for the v2 project catalog."""

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root).expanduser().absolute()
        self.database = self.root / "core-control-v2.sqlite3"
        self._lock = threading.RLock()
        self._closed = False
        self._prepare_root()
        if self.database.exists() and self.database.is_symlink():
            raise CoreControlStoreV2Error("v2 catalog database must not be a symlink")
        with self._reader() as connection:
            connection.executescript(_SCHEMA)
            connection.execute(
                "INSERT OR IGNORE INTO metadata(singleton, schema_version) VALUES (1, 2)"
            )
            connection.commit()
        os.chmod(self.database, 0o600)
        self._verify_database()

    def close(self) -> None:
        with self._lock:
            self._closed = True

    def create_project(
        self,
        request: m.ProjectCreateV2,
        *,
        idempotency_key: str,
        now: datetime,
    ) -> tuple[ProjectRecordV2, bool]:
        request = _exact_model(m.ProjectCreateV2, request)
        idempotency_key = _idempotency_key(idempotency_key)
        request_json = canonical_contract_bytes(request)
        request_sha256 = hashlib.sha256(request_json).hexdigest()
        timestamp = _timestamp(now)
        with self._lock, self._transaction() as connection:
            prior = connection.execute(
                "SELECT request_sha256, request_json, project_id "
                "FROM project_create_requests WHERE idempotency_key = ?",
                (idempotency_key,),
            ).fetchone()
            if prior is not None:
                if (
                    prior["request_sha256"] != request_sha256
                    or bytes(prior["request_json"]) != request_json
                ):
                    raise ProjectIdempotencyConflictV2(
                        "v2 project idempotency key was reused"
                    )
                return _load_project(connection, str(prior["project_id"])), True
            if int(connection.execute("SELECT COUNT(*) FROM projects").fetchone()[0]) >= (
                _MAX_PROJECTS
            ):
                raise ProjectConflictV2("v2 project catalog capacity is exhausted")
            project_id = f"project-{secrets.token_hex(16)}"
            config_json = canonical_contract_bytes(request.config)
            project_config_sha256 = m.project_config_sha256_for(request.config)
            connection.execute(
                "INSERT INTO projects(project_id, display_name, project_config_sha256, "
                "project_config_json, created_at, updated_at, resource_version) "
                "VALUES (?, ?, ?, ?, ?, ?, 1)",
                (
                    project_id,
                    request.display_name,
                    project_config_sha256,
                    config_json,
                    timestamp,
                    timestamp,
                ),
            )
            connection.execute(
                "INSERT INTO project_create_requests(idempotency_key, request_sha256, "
                "request_json, project_id) VALUES (?, ?, ?, ?)",
                (idempotency_key, request_sha256, request_json, project_id),
            )
            return _load_project(connection, project_id), False

    def upsert_authoritative_project(
        self,
        *,
        project_id: str,
        display_name: str,
        config: m.ScienceProjectConfigV2,
        now: datetime,
    ) -> ProjectRecordV2:
        project_id = _resource_id(project_id, label="project")
        if not isinstance(display_name, str) or not 1 <= len(display_name) <= 128:
            raise ValueError("v2 project display name is invalid")
        config = _exact_model(m.ScienceProjectConfigV2, config)
        config_json = canonical_contract_bytes(config)
        project_config_sha256 = m.project_config_sha256_for(config)
        timestamp = _timestamp(now)
        with self._lock, self._transaction() as connection:
            row = connection.execute(
                "SELECT display_name, project_config_sha256, project_config_json "
                "FROM projects "
                "WHERE project_id = ?",
                (project_id,),
            ).fetchone()
            if row is None:
                if int(
                    connection.execute("SELECT COUNT(*) FROM projects").fetchone()[0]
                ) >= _MAX_PROJECTS:
                    raise ProjectConflictV2("v2 project catalog capacity is exhausted")
                connection.execute(
                    "INSERT INTO projects(project_id, display_name, "
                    "project_config_sha256, project_config_json, created_at, updated_at, "
                    "resource_version) VALUES (?, ?, ?, ?, ?, ?, 1)",
                    (
                        project_id,
                        display_name,
                        project_config_sha256,
                        config_json,
                        timestamp,
                        timestamp,
                    ),
                )
            else:
                if (
                    row["project_config_sha256"] != project_config_sha256
                    or bytes(row["project_config_json"]) != config_json
                ):
                    raise ProjectConflictV2(
                        "v2 project catalog disagrees with admission authority"
                    )
                if row["display_name"] != display_name:
                    connection.execute(
                        "UPDATE projects SET display_name = ?, updated_at = ?, "
                        "resource_version = resource_version + 1 WHERE project_id = ?",
                        (display_name, timestamp, project_id),
                    )
            return _load_project(connection, project_id)

    def update_project(
        self,
        project_id: str,
        request: m.ProjectUpdateV2,
        *,
        if_match: str,
        current_etag: str,
        idempotency_key: str,
        now: datetime,
    ) -> tuple[ProjectRecordV2, bool]:
        project_id = _resource_id(project_id, label="project")
        request = _exact_model(m.ProjectUpdateV2, request)
        if_match = _etag(if_match)
        current_etag = _etag(current_etag)
        idempotency_key = _idempotency_key(idempotency_key)
        request_json = json.dumps(
            {
                "if_match": if_match,
                "request": request.model_dump(mode="json"),
            },
            ensure_ascii=True,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        request_sha256 = hashlib.sha256(request_json).hexdigest()
        timestamp = _timestamp(now)
        with self._lock, self._transaction() as connection:
            prior = connection.execute(
                "SELECT request_sha256, request_json FROM project_update_requests "
                "WHERE project_id = ? AND idempotency_key = ?",
                (project_id, idempotency_key),
            ).fetchone()
            if prior is not None:
                if (
                    prior["request_sha256"] != request_sha256
                    or bytes(prior["request_json"]) != request_json
                ):
                    raise ProjectIdempotencyConflictV2(
                        "v2 project update idempotency key was reused"
                    )
                return _load_project(connection, project_id), True
            current = _load_project(connection, project_id)
            if if_match != current_etag:
                raise ProjectPreconditionFailedV2(
                    "v2 project resource ETag changed"
                )
            if (
                request.expected_project_config_sha256
                != current.project_config_sha256
            ):
                raise ProjectPreconditionFailedV2(
                    "v2 project config changed"
                )
            if int(
                connection.execute(
                    "SELECT COUNT(*) FROM project_update_requests"
                ).fetchone()[0]
            ) >= _MAX_OPERATIONS:
                raise ProjectConflictV2(
                    "v2 project update request capacity is exhausted"
                )
            config_json = canonical_contract_bytes(request.config)
            config_sha256 = m.project_config_sha256_for(request.config)
            connection.execute(
                "UPDATE projects SET display_name = ?, project_config_sha256 = ?, "
                "project_config_json = ?, updated_at = ?, "
                "resource_version = resource_version + 1 WHERE project_id = ?",
                (
                    request.display_name,
                    config_sha256,
                    config_json,
                    timestamp,
                    project_id,
                ),
            )
            connection.execute(
                "INSERT INTO project_update_requests(project_id, idempotency_key, "
                "request_sha256, request_json) VALUES (?, ?, ?, ?)",
                (project_id, idempotency_key, request_sha256, request_json),
            )
            return _load_project(connection, project_id), False

    def begin_project_validation(
        self,
        project_id: str,
        request: m.ProjectValidationRequestV2,
        *,
        idempotency_key: str,
    ) -> m.ProjectValidationResponseV2 | None:
        project_id = _resource_id(project_id, label="project")
        request = _exact_model(m.ProjectValidationRequestV2, request)
        idempotency_key = _idempotency_key(idempotency_key)
        request_json = canonical_contract_bytes(request)
        request_sha256 = hashlib.sha256(request_json).hexdigest()
        with self._lock, self._transaction() as connection:
            _load_project(connection, project_id)
            row = connection.execute(
                "SELECT request_sha256, request_json, response_json "
                "FROM project_validation_requests WHERE project_id = ? "
                "AND idempotency_key = ?",
                (project_id, idempotency_key),
            ).fetchone()
            if row is None:
                if int(
                    connection.execute(
                        "SELECT COUNT(*) FROM project_validation_requests"
                    ).fetchone()[0]
                ) >= _MAX_OPERATIONS:
                    raise ProjectConflictV2(
                        "v2 project validation capacity is exhausted"
                    )
                connection.execute(
                    "INSERT INTO project_validation_requests(project_id, "
                    "idempotency_key, request_sha256, request_json, response_json) "
                    "VALUES (?, ?, ?, ?, NULL)",
                    (project_id, idempotency_key, request_sha256, request_json),
                )
                return None
            _require_same_validation_request(
                row,
                request_sha256=request_sha256,
                request_json=request_json,
            )
            if row["response_json"] is None:
                return None
            return _load_project_validation_response(
                bytes(row["response_json"]),
                project_id=project_id,
            )

    def commit_project_validation(
        self,
        project_id: str,
        request: m.ProjectValidationRequestV2,
        response: m.ProjectValidationResponseV2,
        *,
        idempotency_key: str,
    ) -> m.ProjectValidationResponseV2:
        project_id = _resource_id(project_id, label="project")
        request = _exact_model(m.ProjectValidationRequestV2, request)
        response = _exact_model(m.ProjectValidationResponseV2, response)
        idempotency_key = _idempotency_key(idempotency_key)
        if response.project_id != project_id:
            raise ValueError("v2 project validation response crosses projects")
        request_json = canonical_contract_bytes(request)
        request_sha256 = hashlib.sha256(request_json).hexdigest()
        response_json = canonical_contract_bytes(response)
        with self._lock, self._transaction() as connection:
            row = connection.execute(
                "SELECT request_sha256, request_json, response_json "
                "FROM project_validation_requests WHERE project_id = ? "
                "AND idempotency_key = ?",
                (project_id, idempotency_key),
            ).fetchone()
            if row is None:
                raise CoreControlStoreV2Error(
                    "v2 project validation reservation is missing"
                )
            _require_same_validation_request(
                row,
                request_sha256=request_sha256,
                request_json=request_json,
            )
            if row["response_json"] is not None:
                existing = _load_project_validation_response(
                    bytes(row["response_json"]),
                    project_id=project_id,
                )
                if existing != response:
                    return existing
                return existing
            connection.execute(
                "UPDATE project_validation_requests SET response_json = ? "
                "WHERE project_id = ? AND idempotency_key = ?",
                (response_json, project_id, idempotency_key),
            )
            return _load_project_validation_response(
                response_json,
                project_id=project_id,
            )

    def get_project(self, project_id: str) -> ProjectRecordV2:
        project_id = _resource_id(project_id, label="project")
        with self._lock, self._reader() as connection:
            return _load_project(connection, project_id)

    def list_projects(self) -> list[ProjectRecordV2]:
        with self._lock, self._reader() as connection:
            rows = connection.execute(
                "SELECT project_id FROM projects ORDER BY project_id LIMIT ?",
                (_MAX_PROJECTS + 1,),
            ).fetchall()
            if len(rows) > _MAX_PROJECTS:
                raise CoreControlStoreV2Error("v2 project catalog exceeds its bound")
            return [_load_project(connection, str(row["project_id"])) for row in rows]

    def get_project_authority_document(
        self,
        project_id: str,
    ) -> ProjectAuthorityDocumentV2 | None:
        project_id = _resource_id(project_id, label="project")
        with self._lock, self._reader() as connection:
            return _load_project_authority_document(
                connection,
                project_id,
                required=False,
            )

    def list_project_authority_documents(self) -> list[ProjectAuthorityDocumentV2]:
        with self._lock, self._reader() as connection:
            rows = connection.execute(
                "SELECT project_id FROM project_authority_records "
                "ORDER BY project_id LIMIT ?",
                (_MAX_PROJECTS + 1,),
            ).fetchall()
            if len(rows) > _MAX_PROJECTS:
                raise CoreControlStoreV2Error(
                    "v2 project authority inventory exceeds its bound"
                )
            return [
                _load_project_authority_document(
                    connection,
                    str(row["project_id"]),
                    required=True,
                )
                for row in rows
            ]

    def put_project_authority_document(
        self,
        *,
        project_id: str,
        record_json: bytes,
        expected_record_sha256: str | None,
    ) -> ProjectAuthorityDocumentV2:
        project_id = _resource_id(project_id, label="project")
        record_json = _canonical_project_authority_document(record_json)
        record_sha256 = hashlib.sha256(record_json).hexdigest()
        if expected_record_sha256 is not None:
            expected_record_sha256 = _sha256(
                expected_record_sha256,
                label="project authority record",
            )
        with self._lock, self._transaction() as connection:
            _load_project(connection, project_id)
            current = _load_project_authority_document(
                connection,
                project_id,
                required=False,
            )
            if current is None:
                if expected_record_sha256 is not None:
                    raise ProjectConflictV2(
                        "v2 project authority record does not exist"
                    )
                connection.execute(
                    "INSERT INTO project_authority_records(project_id, "
                    "record_sha256, record_json, resource_version) "
                    "VALUES (?, ?, ?, 1)",
                    (project_id, record_sha256, record_json),
                )
            elif current.record_json == record_json:
                return current
            else:
                if (
                    expected_record_sha256 is None
                    or current.record_sha256 != expected_record_sha256
                ):
                    raise ProjectConflictV2(
                        "v2 project authority record changed"
                    )
                connection.execute(
                    "UPDATE project_authority_records SET record_sha256 = ?, "
                    "record_json = ?, resource_version = resource_version + 1 "
                    "WHERE project_id = ?",
                    (record_sha256, record_json, project_id),
                )
            loaded = _load_project_authority_document(
                connection,
                project_id,
                required=True,
            )
            if loaded is None:  # pragma: no cover - required=True is authoritative
                raise CoreControlStoreV2Error(
                    "v2 project authority record publication failed"
                )
            return loaded

    def begin_action(
        self,
        *,
        action_scope: str,
        idempotency_key: str,
        request_json: bytes,
    ) -> ActionReservationV2:
        action_scope = _action_scope(action_scope)
        idempotency_key = _idempotency_key(idempotency_key)
        request_json = _canonical_json_document(request_json)
        request_sha256 = hashlib.sha256(request_json).hexdigest()
        with self._lock, self._transaction() as connection:
            prior = connection.execute(
                "SELECT request_sha256, request_json, operation_id "
                "FROM action_requests WHERE action_scope = ? AND idempotency_key = ?",
                (action_scope, idempotency_key),
            ).fetchone()
            if prior is not None:
                if (
                    prior["request_sha256"] != request_sha256
                    or bytes(prior["request_json"]) != request_json
                ):
                    raise ProjectIdempotencyConflictV2(
                        "v2 action idempotency key was reused"
                    )
                operation_id = prior["operation_id"]
                return ActionReservationV2(
                    operation=(
                        None
                        if operation_id is None
                        else _load_operation(connection, str(operation_id))
                    ),
                    resumed=True,
                )
            if int(
                connection.execute("SELECT COUNT(*) FROM action_requests").fetchone()[0]
            ) >= _MAX_OPERATIONS:
                raise ProjectConflictV2("v2 action request capacity is exhausted")
            connection.execute(
                "INSERT INTO action_requests(action_scope, idempotency_key, "
                "request_sha256, request_json, operation_id) VALUES (?, ?, ?, ?, NULL)",
                (action_scope, idempotency_key, request_sha256, request_json),
            )
            return ActionReservationV2(operation=None, resumed=False)

    def commit_action(
        self,
        *,
        action_scope: str,
        idempotency_key: str,
        request_json: bytes,
        operation: m.OperationV2,
    ) -> m.OperationV2:
        action_scope = _action_scope(action_scope)
        idempotency_key = _idempotency_key(idempotency_key)
        request_json = _canonical_json_document(request_json)
        request_sha256 = hashlib.sha256(request_json).hexdigest()
        operation = _exact_model(m.OperationV2, operation)
        with self._lock, self._transaction() as connection:
            row = connection.execute(
                "SELECT request_sha256, request_json, operation_id "
                "FROM action_requests WHERE action_scope = ? AND idempotency_key = ?",
                (action_scope, idempotency_key),
            ).fetchone()
            if row is None:
                raise CoreControlStoreV2Error("v2 action reservation is missing")
            if (
                row["request_sha256"] != request_sha256
                or bytes(row["request_json"]) != request_json
            ):
                raise ProjectIdempotencyConflictV2(
                    "v2 action idempotency key was reused"
                )
            if row["operation_id"] is not None:
                existing = _load_operation(connection, str(row["operation_id"]))
                if existing != operation:
                    raise CoreControlStoreV2Error(
                        "v2 action replay produced another operation"
                    )
                return existing
            if int(connection.execute("SELECT COUNT(*) FROM operations").fetchone()[0]) >= (
                _MAX_OPERATIONS
            ):
                raise ProjectConflictV2("v2 operation capacity is exhausted")
            connection.execute(
                "INSERT INTO operations(operation_id, operation_json) VALUES (?, ?)",
                (operation.operation_id, canonical_contract_bytes(operation)),
            )
            connection.execute(
                "UPDATE action_requests SET operation_id = ? "
                "WHERE action_scope = ? AND idempotency_key = ?",
                (operation.operation_id, action_scope, idempotency_key),
            )
            return _load_operation(connection, operation.operation_id)

    def get_operation(self, operation_id: str) -> m.OperationV2:
        operation_id = _resource_id(operation_id, label="operation")
        with self._lock, self._reader() as connection:
            return _load_operation(connection, operation_id)

    def _prepare_root(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        metadata = self.root.stat(follow_symlinks=False)
        if (
            not stat.S_ISDIR(metadata.st_mode)
            or metadata.st_uid != os.geteuid()
            or stat.S_IMODE(metadata.st_mode) != 0o700
        ):
            raise CoreControlStoreV2Error(
                "v2 catalog root must be a private owned directory"
            )

    def _verify_database(self) -> None:
        metadata = self.database.stat(follow_symlinks=False)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid != os.geteuid()
            or metadata.st_nlink != 1
            or stat.S_IMODE(metadata.st_mode) != 0o600
        ):
            raise CoreControlStoreV2Error(
                "v2 catalog database is not a private regular file"
            )
        with self._reader() as connection:
            if _schema_rows(connection) != _expected_schema_rows():
                raise CoreControlStoreV2Error("v2 catalog schema is not exact")
            quick_check = connection.execute("PRAGMA quick_check").fetchall()
            if len(quick_check) != 1 or tuple(quick_check[0]) != ("ok",):
                raise CoreControlStoreV2Error("v2 catalog database integrity failed")
            if connection.execute("PRAGMA foreign_key_check").fetchone() is not None:
                raise CoreControlStoreV2Error(
                    "v2 catalog foreign-key integrity failed"
                )
            identity = connection.execute(
                "SELECT schema_version FROM metadata WHERE singleton = 1"
            ).fetchone()
            if identity is None or int(identity["schema_version"]) != 2:
                raise CoreControlStoreV2Error("v2 catalog schema identity is invalid")
            rows = connection.execute(
                "SELECT project_id FROM projects ORDER BY project_id LIMIT ?",
                (_MAX_PROJECTS + 1,),
            ).fetchall()
            if len(rows) > _MAX_PROJECTS:
                raise CoreControlStoreV2Error("v2 project catalog exceeds its bound")
            for row in rows:
                _load_project(connection, str(row["project_id"]))
            requests = connection.execute(
                "SELECT idempotency_key, request_sha256, request_json, project_id "
                "FROM project_create_requests"
            ).fetchall()
            if len(requests) > _MAX_PROJECTS:
                raise CoreControlStoreV2Error(
                    "v2 project idempotency inventory exceeds its bound"
                )
            for row in requests:
                request_json = bytes(row["request_json"])
                try:
                    parse_contract_json_bytes(
                        m.ProjectCreateV2,
                        request_json,
                        max_depth=m.MAX_PROJECT_CONFIG_JSON_DEPTH,
                        max_nodes=m.MAX_PROJECT_CONFIG_BYTES,
                        max_collection_items=m.MAX_PROJECT_CONFIG_BYTES,
                    )
                except (TypeError, ValueError) as exc:
                    raise CoreControlStoreV2Error(
                        "persisted v2 project request is invalid"
                    ) from exc
                if (
                    _idempotency_key(str(row["idempotency_key"]))
                    != row["idempotency_key"]
                    or hashlib.sha256(request_json).hexdigest()
                    != row["request_sha256"]
                ):
                    raise CoreControlStoreV2Error(
                        "persisted v2 project request digest is inconsistent"
                    )
                _load_project(connection, str(row["project_id"]))
            update_requests = connection.execute(
                "SELECT project_id, idempotency_key, request_sha256, request_json "
                "FROM project_update_requests LIMIT ?",
                (_MAX_OPERATIONS + 1,),
            ).fetchall()
            if len(update_requests) > _MAX_OPERATIONS:
                raise CoreControlStoreV2Error(
                    "v2 project update request inventory exceeds its bound"
                )
            for row in update_requests:
                payload = bytes(row["request_json"])
                try:
                    _parse_project_update_request(payload)
                    inconsistent = (
                        _resource_id(str(row["project_id"]), label="project")
                        != row["project_id"]
                        or _idempotency_key(str(row["idempotency_key"]))
                        != row["idempotency_key"]
                        or hashlib.sha256(payload).hexdigest()
                        != row["request_sha256"]
                    )
                except (TypeError, ValueError) as exc:
                    raise CoreControlStoreV2Error(
                        "persisted v2 project update request is invalid"
                    ) from exc
                if inconsistent:
                    raise CoreControlStoreV2Error(
                        "persisted v2 project update request is inconsistent"
                    )
                _load_project(connection, str(row["project_id"]))
            validation_requests = connection.execute(
                "SELECT project_id, idempotency_key, request_sha256, request_json, "
                "response_json FROM project_validation_requests LIMIT ?",
                (_MAX_OPERATIONS + 1,),
            ).fetchall()
            if len(validation_requests) > _MAX_OPERATIONS:
                raise CoreControlStoreV2Error(
                    "v2 project validation inventory exceeds its bound"
                )
            for row in validation_requests:
                request_json = bytes(row["request_json"])
                try:
                    request = parse_contract_json_bytes(
                        m.ProjectValidationRequestV2,
                        request_json,
                    )
                except (TypeError, ValueError) as exc:
                    raise CoreControlStoreV2Error(
                        "persisted v2 project validation request is invalid"
                    ) from exc
                if (
                    _idempotency_key(str(row["idempotency_key"]))
                    != row["idempotency_key"]
                    or canonical_contract_bytes(request) != request_json
                    or hashlib.sha256(request_json).hexdigest()
                    != row["request_sha256"]
                ):
                    raise CoreControlStoreV2Error(
                        "persisted v2 project validation request is inconsistent"
                    )
                project_id = str(row["project_id"])
                _load_project(connection, project_id)
                if row["response_json"] is not None:
                    _load_project_validation_response(
                        bytes(row["response_json"]),
                        project_id=project_id,
                    )
            authority_rows = connection.execute(
                "SELECT project_id FROM project_authority_records LIMIT ?",
                (_MAX_PROJECTS + 1,),
            ).fetchall()
            if len(authority_rows) > _MAX_PROJECTS:
                raise CoreControlStoreV2Error(
                    "v2 project authority inventory exceeds its bound"
                )
            for row in authority_rows:
                _load_project_authority_document(
                    connection,
                    str(row["project_id"]),
                    required=True,
                )
            operation_rows = connection.execute(
                "SELECT operation_id FROM operations ORDER BY operation_id LIMIT ?",
                (_MAX_OPERATIONS + 1,),
            ).fetchall()
            if len(operation_rows) > _MAX_OPERATIONS:
                raise CoreControlStoreV2Error("v2 operation inventory exceeds its bound")
            for row in operation_rows:
                _load_operation(connection, str(row["operation_id"]))
            orphan_operation = connection.execute(
                "SELECT operations.operation_id FROM operations "
                "LEFT JOIN action_requests "
                "ON action_requests.operation_id = operations.operation_id "
                "WHERE action_requests.operation_id IS NULL LIMIT 1"
            ).fetchone()
            if orphan_operation is not None:
                raise CoreControlStoreV2Error(
                    "persisted v2 operation has no action authority"
                )
            action_rows = connection.execute(
                "SELECT action_scope, idempotency_key, request_sha256, request_json, "
                "operation_id FROM action_requests LIMIT ?",
                (_MAX_OPERATIONS + 1,),
            ).fetchall()
            if len(action_rows) > _MAX_OPERATIONS:
                raise CoreControlStoreV2Error(
                    "v2 action request inventory exceeds its bound"
                )
            for row in action_rows:
                try:
                    request_json = _canonical_json_document(
                        bytes(row["request_json"])
                    )
                    inconsistent = (
                        _action_scope(str(row["action_scope"]))
                        != row["action_scope"]
                        or _idempotency_key(str(row["idempotency_key"]))
                        != row["idempotency_key"]
                        or hashlib.sha256(request_json).hexdigest()
                        != row["request_sha256"]
                    )
                except (TypeError, ValueError) as exc:
                    raise CoreControlStoreV2Error(
                        "persisted v2 action request is inconsistent"
                    ) from exc
                if inconsistent:
                    raise CoreControlStoreV2Error(
                        "persisted v2 action request is inconsistent"
                    )
                if row["operation_id"] is not None:
                    _load_operation(connection, str(row["operation_id"]))

    @contextmanager
    def _transaction(self) -> Iterator[sqlite3.Connection]:
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            yield connection
            connection.execute("COMMIT")
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
        finally:
            connection.close()

    def _connect(self) -> sqlite3.Connection:
        if self._closed:
            raise CoreControlStoreV2Error("v2 catalog store is closed")
        connection = sqlite3.connect(self.database, timeout=10.0)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 10000")
        return connection


def _load_project(
    connection: sqlite3.Connection, project_id: str
) -> ProjectRecordV2:
    row = connection.execute(
        "SELECT project_id, display_name, project_config_sha256, project_config_json, "
        "created_at, updated_at, resource_version FROM projects WHERE project_id = ?",
        (project_id,),
    ).fetchone()
    if row is None:
        raise ProjectNotFoundV2("v2 project was not found")
    try:
        config_json = bytes(row["project_config_json"])
        config = parse_contract_json_bytes(
            m.ScienceProjectConfigV2,
            config_json,
            max_depth=m.MAX_PROJECT_CONFIG_JSON_DEPTH,
            max_nodes=m.MAX_PROJECT_CONFIG_BYTES,
            max_collection_items=m.MAX_PROJECT_CONFIG_BYTES,
        )
        loaded = ProjectRecordV2(
            project_id=_resource_id(str(row["project_id"]), label="project"),
            display_name=str(row["display_name"]),
            config=config,
            project_config_sha256=_sha256(
                str(row["project_config_sha256"]), label="project config"
            ),
            created_at=_timestamp_text(str(row["created_at"])),
            updated_at=_timestamp_text(str(row["updated_at"])),
            resource_version=int(row["resource_version"]),
        )
    except (TypeError, ValueError) as exc:
        raise CoreControlStoreV2Error("persisted v2 project is invalid") from exc
    if (
        not 1 <= len(loaded.display_name) <= 128
        or loaded.resource_version < 1
        or canonical_contract_bytes(loaded.config) != config_json
        or m.project_config_sha256_for(loaded.config)
        != loaded.project_config_sha256
    ):
        raise CoreControlStoreV2Error("persisted v2 project is invalid")
    return loaded


def _load_project_authority_document(
    connection: sqlite3.Connection,
    project_id: str,
    *,
    required: bool,
) -> ProjectAuthorityDocumentV2 | None:
    row = connection.execute(
        "SELECT project_id, record_sha256, record_json, resource_version "
        "FROM project_authority_records WHERE project_id = ?",
        (project_id,),
    ).fetchone()
    if row is None:
        if required:
            raise CoreControlStoreV2Error(
                "persisted v2 project authority record is missing"
            )
        return None
    try:
        record_json = _canonical_project_authority_document(bytes(row["record_json"]))
        document = ProjectAuthorityDocumentV2(
            project_id=_resource_id(str(row["project_id"]), label="project"),
            record_sha256=_sha256(
                str(row["record_sha256"]),
                label="project authority record",
            ),
            record_json=record_json,
            resource_version=int(row["resource_version"]),
        )
    except (TypeError, ValueError) as exc:
        raise CoreControlStoreV2Error(
            "persisted v2 project authority record is invalid"
        ) from exc
    if (
        document.project_id != project_id
        or document.resource_version < 1
        or hashlib.sha256(document.record_json).hexdigest()
        != document.record_sha256
    ):
        raise CoreControlStoreV2Error(
            "persisted v2 project authority record is inconsistent"
        )
    _load_project(connection, document.project_id)
    return document


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


def _load_operation(
    connection: sqlite3.Connection,
    operation_id: str,
) -> m.OperationV2:
    row = connection.execute(
        "SELECT operation_id, operation_json FROM operations WHERE operation_id = ?",
        (operation_id,),
    ).fetchone()
    if row is None:
        raise OperationNotFoundV2("v2 operation was not found")
    payload = bytes(row["operation_json"])
    try:
        operation = parse_contract_json_bytes(m.OperationV2, payload)
    except (TypeError, ValueError) as exc:
        raise CoreControlStoreV2Error("persisted v2 operation is invalid") from exc
    if (
        operation.operation_id != row["operation_id"]
        or canonical_contract_bytes(operation) != payload
        or operation.etag != operation_etag_for(operation)
    ):
        raise CoreControlStoreV2Error("persisted v2 operation is inconsistent")
    return operation


def _require_same_validation_request(
    row: sqlite3.Row,
    *,
    request_sha256: str,
    request_json: bytes,
) -> None:
    if (
        row["request_sha256"] != request_sha256
        or bytes(row["request_json"]) != request_json
    ):
        raise ProjectIdempotencyConflictV2(
            "v2 project validation idempotency key was reused"
        )


def _load_project_validation_response(
    payload: bytes,
    *,
    project_id: str,
) -> m.ProjectValidationResponseV2:
    try:
        response = parse_contract_json_bytes(m.ProjectValidationResponseV2, payload)
    except (TypeError, ValueError) as exc:
        raise CoreControlStoreV2Error(
            "persisted v2 project validation response is invalid"
        ) from exc
    if (
        response.project_id != project_id
        or canonical_contract_bytes(response) != payload
    ):
        raise CoreControlStoreV2Error(
            "persisted v2 project validation response is inconsistent"
        )
    return response


def _exact_model(model_type: type[m.ContractModel], value: m.ContractModel):
    if type(value) is not model_type:
        raise TypeError(f"v2 value must be exact {model_type.__name__}")
    return model_type.model_validate(value.model_dump(mode="python"))


def _resource_id(value: str, *, label: str) -> str:
    if not isinstance(value, str) or _ID_RE.fullmatch(value) is None:
        raise ValueError(f"v2 {label} ID is invalid")
    return value


def _sha256(value: str, *, label: str) -> str:
    if not isinstance(value, str) or _SHA256_RE.fullmatch(value) is None:
        raise ValueError(f"v2 {label} digest is invalid")
    return value


def _etag(value: str) -> str:
    if not isinstance(value, str) or _ETAG_RE.fullmatch(value) is None:
        raise ValueError("v2 project ETag is invalid")
    return value


def _idempotency_key(value: str) -> str:
    if (
        not isinstance(value, str)
        or not 1 <= len(value) <= 256
        or any(ord(character) < 32 or ord(character) == 127 for character in value)
    ):
        raise ValueError("v2 idempotency key is invalid")
    return value


def _action_scope(value: str) -> str:
    if (
        not isinstance(value, str)
        or not 1 <= len(value) <= 256
        or any(ord(character) < 32 or ord(character) == 127 for character in value)
    ):
        raise ValueError("v2 action scope is invalid")
    return value


def _canonical_json_document(payload: bytes) -> bytes:
    if type(payload) is not bytes or not 1 <= len(payload) <= 1024 * 1024:
        raise ValueError("v2 action request document is invalid")
    try:
        value = json.loads(payload.decode("utf-8", errors="strict"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("v2 action request document is invalid") from exc
    if not isinstance(value, dict):
        raise ValueError("v2 action request document must be an object")
    canonical = json.dumps(
        value,
        ensure_ascii=True,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    if canonical != payload:
        raise ValueError("v2 action request document is not canonical")
    return canonical


def _canonical_project_authority_document(payload: bytes) -> bytes:
    if (
        type(payload) is not bytes
        or not 1 <= len(payload) <= _MAX_PROJECT_AUTHORITY_BYTES
    ):
        raise ValueError("v2 project authority document is invalid")
    try:
        value = json.loads(payload.decode("utf-8", errors="strict"))
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError) as exc:
        raise ValueError("v2 project authority document is invalid") from exc
    if not isinstance(value, dict):
        raise ValueError("v2 project authority document must be an object")
    try:
        canonical = json.dumps(
            value,
            ensure_ascii=True,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    except (TypeError, ValueError, RecursionError) as exc:
        raise ValueError("v2 project authority document is invalid") from exc
    if canonical != payload:
        raise ValueError("v2 project authority document is not canonical")
    return canonical


def _parse_project_update_request(
    payload: bytes,
) -> tuple[str, m.ProjectUpdateV2]:
    payload = _canonical_json_document(payload)
    value = json.loads(payload)
    if type(value) is not dict or set(value) != {"if_match", "request"}:
        raise ValueError("v2 project update request is not closed")
    if_match = _etag(value["if_match"])
    request = m.ProjectUpdateV2.model_validate(value["request"])
    expected = json.dumps(
        {
            "if_match": if_match,
            "request": request.model_dump(mode="json"),
        },
        ensure_ascii=True,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    if expected != payload:
        raise ValueError("v2 project update request is not canonical")
    return if_match, request


def _timestamp(value: datetime) -> str:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise TypeError("v2 timestamp requires an aware datetime")
    normalized = value.astimezone(timezone.utc)
    return normalized.isoformat(timespec="microseconds").replace("+00:00", "Z")


def _timestamp_text(value: str) -> str:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("v2 timestamp is invalid") from exc
    if parsed.tzinfo is None or _timestamp(parsed) != value:
        raise ValueError("v2 timestamp is not canonical")
    return value


def project_etag_payload(
    record: ProjectRecordV2,
    *,
    active_project_head: m.ProjectHeadRefV2 | None,
    admission_etag: str | None,
    state: str,
) -> str:
    """Return a stable strong ETag for the public joined project representation."""

    payload = {
        "project_id": record.project_id,
        "display_name": record.display_name,
        "project_config_sha256": record.project_config_sha256,
        "active_project_head": (
            None
            if active_project_head is None
            else active_project_head.model_dump(mode="json")
        ),
        "admission_etag": admission_etag,
        "state": state,
        "created_at": record.created_at,
        "updated_at": record.updated_at,
        "resource_version": record.resource_version,
    }
    encoded = json.dumps(
        payload,
        ensure_ascii=True,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return f'"{hashlib.sha256(encoded).hexdigest()}"'


def operation_etag_for(operation: m.OperationV2) -> str:
    if type(operation) is not m.OperationV2:
        raise TypeError("v2 operation ETag requires an exact OperationV2")
    payload = operation.model_dump(mode="json", exclude={"etag"})
    encoded = json.dumps(
        payload,
        ensure_ascii=True,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return f'"{hashlib.sha256(encoded).hexdigest()}"'


__all__ = [
    "ActionReservationV2",
    "CoreControlStoreV2",
    "CoreControlStoreV2Error",
    "ProjectConflictV2",
    "ProjectIdempotencyConflictV2",
    "ProjectNotFoundV2",
    "ProjectPreconditionFailedV2",
    "ProjectAuthorityDocumentV2",
    "ProjectRecordV2",
    "OperationNotFoundV2",
    "operation_etag_for",
    "project_etag_payload",
]
