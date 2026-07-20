"""Private durable persistence for the Desktop/Core bridge v1 protocol."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, replace
from datetime import datetime
import fcntl
from hashlib import sha256
import json
import os
from pathlib import Path
import re
import secrets
import sqlite3
import stat
import sys
import threading
from typing import Any, TypeVar, cast
import unicodedata

from pydantic import ValidationError

from desktop.sidecar.core_bridge_v1 import (
    CoreProjectCreateOperationV1,
    CoreProjectCreateStateV1,
    CoreProjectHeadSuccessorProofV1,
    CoreProjectMappingV1,
    CoreProjectPatchImmutableAuthorityV1,
    CoreProjectPatchMutableAuthorityV1,
    CoreProjectPatchOperationV1,
    CoreProjectPatchStateV1,
    CoreWorkspaceUploadAbortOperationV1,
    CoreWorkspaceUploadAbortStateV1,
    CoreWorkspaceUploadFinalizeAuthorityV1,
    CoreWorkspaceUploadFinalizeStateV1,
    _completed_patch_project_authority,
    revision_manifest_sha256_v1,
)
from openevo.backend.contracts.v1 import models as core_v1


SCHEMA_VERSION = 3
DATABASE_FILENAME = "core-bridge-v1.sqlite3"
JOURNAL_FILENAME = f"{DATABASE_FILENAME}-journal"
WAL_FILENAME = f"{DATABASE_FILENAME}-wal"
SHM_FILENAME = f"{DATABASE_FILENAME}-shm"
OWNER_LOCK_FILENAME = "core-bridge-v1.lock"
IDENTITY_MARKER_FILENAME = "core-bridge-v1.identity"
ROOT_ANCHOR_PREFIX = ".openevo-core-bridge-v1-root-"

MAX_DOCUMENT_BYTES = 4 * 1024 * 1024
MAX_DATABASE_BYTES = 1024 * 1024 * 1024
MAX_JOURNAL_BYTES = 2 * 1024 * 1024 * 1024
MAX_RECOVERY_ROWS = 120_000
MAX_RECOVERY_BYTES = 512 * 1024 * 1024
MAX_SCHEMA_OBJECTS = 16
MAX_SCHEMA_BYTES = 64 * 1024
DEFAULT_MAX_MAPPING_HISTORY_ROWS = 100_000
MAX_IDENTITY_BYTES = 512
MARKER_SLOT_BYTES = 4096
MARKER_FILE_BYTES = MARKER_SLOT_BYTES * 2
_SQLITE_SYNCHRONOUS_FULL = 2

_DIGEST_RE = re.compile(r"^[0-9a-f]{64}$")
_MANAGED_FILES = (DATABASE_FILENAME, OWNER_LOCK_FILENAME, IDENTITY_MARKER_FILENAME)
_SQLITE_SIDE_FILES = (JOURNAL_FILENAME, WAL_FILENAME, SHM_FILENAME)
_MAPPING_TRANSITION_RECORD = "CoreProjectMappingTransitionV1"
_PROJECT_HEAD_TRANSITION_RECORD = "CoreProjectHeadMappingTransitionV1"
_PROJECT_HEAD_AND_PATCH_TRANSITION_RECORD = (
    "CoreProjectHeadAndPatchMappingTransitionV1"
)
_ModelT = TypeVar("_ModelT", bound=core_v1.ContractModel)


class CoreBridgeStoreError(RuntimeError):
    """Base class for durable Desktop/Core bridge persistence failures."""


class CoreBridgeStoreStateRootError(CoreBridgeStoreError):
    """The private state root or a managed file is unsafe."""


class CoreBridgeStoreSchemaError(CoreBridgeStoreError):
    """The SQLite schema does not match the frozen private schema fingerprint."""


class CoreBridgeStoreDataCorruptionError(CoreBridgeStoreError):
    """Persisted bridge authority does not satisfy its closed contract."""


class CoreBridgeStoreContractError(CoreBridgeStoreError, ValueError):
    """A caller supplied an invalid bridge persistence operation."""


class CoreBridgeStoreConflictError(CoreBridgeStoreError):
    """An exact full-row compare-and-swap lost authority."""


class CoreBridgeStoreCapacityError(CoreBridgeStoreError):
    """A durable recovery or history bound would be exceeded."""


_SCHEMA = (
    """
    CREATE TABLE schema_metadata (
        singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
        schema_version INTEGER NOT NULL CHECK (schema_version = 3),
        schema_fingerprint TEXT NOT NULL CHECK (length(schema_fingerprint) = 64)
    ) STRICT
    """,
    """
    CREATE TABLE store_identity (
        singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
        store_id TEXT NOT NULL CHECK (length(store_id) = 64),
        root_device INTEGER NOT NULL CHECK (root_device >= 0),
        root_inode INTEGER NOT NULL CHECK (root_inode > 0),
        database_device INTEGER NOT NULL CHECK (database_device >= 0),
        database_inode INTEGER NOT NULL CHECK (database_inode > 0),
        marker_device INTEGER NOT NULL CHECK (marker_device >= 0),
        marker_inode INTEGER NOT NULL CHECK (marker_inode > 0),
        anchor_device INTEGER NOT NULL CHECK (anchor_device >= 0),
        anchor_inode INTEGER NOT NULL CHECK (anchor_inode > 0),
        lock_device INTEGER NOT NULL CHECK (lock_device >= 0),
        lock_inode INTEGER NOT NULL CHECK (lock_inode > 0),
        marker_generation INTEGER NOT NULL CHECK (marker_generation >= 0),
        authority_digest TEXT NOT NULL CHECK (length(authority_digest) = 64),
        previous_marker_generation INTEGER NOT NULL CHECK (previous_marker_generation >= 0),
        previous_authority_digest TEXT NOT NULL CHECK (length(previous_authority_digest) = 64),
        binding_state TEXT NOT NULL CHECK (binding_state IN ('pending', 'bound'))
    ) STRICT
    """,
    f"""
    CREATE TABLE create_operations (
        local_project_id TEXT PRIMARY KEY,
        state TEXT NOT NULL CHECK (state IN ('pre_create', 'unknown', 'bound')),
        document_json BLOB NOT NULL,
        document_sha256 TEXT NOT NULL CHECK (length(document_sha256) = 64),
        CHECK (length(CAST(local_project_id AS BLOB)) BETWEEN 1 AND {MAX_IDENTITY_BYTES}),
        CHECK (length(document_json) BETWEEN 1 AND {MAX_DOCUMENT_BYTES})
    ) STRICT
    """,
    f"""
    CREATE TABLE patch_operations (
        local_project_id TEXT PRIMARY KEY,
        state TEXT NOT NULL CHECK (state IN ('pre_patch', 'unknown', 'applied')),
        document_json BLOB NOT NULL,
        document_sha256 TEXT NOT NULL CHECK (length(document_sha256) = 64),
        CHECK (length(CAST(local_project_id AS BLOB)) BETWEEN 1 AND {MAX_IDENTITY_BYTES}),
        CHECK (length(document_json) BETWEEN 1 AND {MAX_DOCUMENT_BYTES}),
        FOREIGN KEY (local_project_id) REFERENCES create_operations(local_project_id)
            ON DELETE RESTRICT
    ) STRICT
    """,
    f"""
    CREATE TABLE mappings (
        local_project_id TEXT PRIMARY KEY,
        core_project_id TEXT NOT NULL,
        mapping_generation INTEGER NOT NULL CHECK (mapping_generation >= 1),
        request_sha256 TEXT NOT NULL CHECK (length(request_sha256) = 64),
        document_json BLOB NOT NULL,
        document_sha256 TEXT NOT NULL CHECK (length(document_sha256) = 64),
        CHECK (length(CAST(local_project_id AS BLOB)) BETWEEN 1 AND {MAX_IDENTITY_BYTES}),
        CHECK (length(CAST(core_project_id AS BLOB)) BETWEEN 1 AND {MAX_IDENTITY_BYTES}),
        CHECK (length(document_json) BETWEEN 1 AND {MAX_DOCUMENT_BYTES}),
        FOREIGN KEY (local_project_id) REFERENCES create_operations(local_project_id)
            ON DELETE RESTRICT
    ) STRICT
    """,
    f"""
    CREATE TABLE mapping_history (
        local_project_id TEXT NOT NULL,
        mapping_generation INTEGER NOT NULL CHECK (mapping_generation >= 1),
        core_project_id TEXT NOT NULL,
        request_sha256 TEXT NOT NULL CHECK (length(request_sha256) = 64),
        document_json BLOB NOT NULL,
        document_sha256 TEXT NOT NULL CHECK (length(document_sha256) = 64),
        PRIMARY KEY (local_project_id, mapping_generation),
        CHECK (length(CAST(local_project_id AS BLOB)) BETWEEN 1 AND {MAX_IDENTITY_BYTES}),
        CHECK (length(CAST(core_project_id AS BLOB)) BETWEEN 1 AND {MAX_IDENTITY_BYTES}),
        CHECK (length(document_json) BETWEEN 1 AND {MAX_DOCUMENT_BYTES}),
        FOREIGN KEY (local_project_id) REFERENCES create_operations(local_project_id)
            ON DELETE RESTRICT
    ) STRICT
    """,
    "CREATE INDEX mapping_history_project_idx ON mapping_history(local_project_id, mapping_generation)",
)


def _canonical_json_bytes(value: Any) -> bytes:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeError) as exc:
        raise CoreBridgeStoreContractError("bridge state is not canonical JSON data") from exc


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
        raise CoreBridgeStoreSchemaError("bridge schema exceeds its fingerprint bounds")
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


def _expected_schema() -> tuple[tuple[tuple[object, ...], ...], str]:
    connection = sqlite3.connect(":memory:")
    try:
        connection.execute("PRAGMA foreign_keys = ON")
        for statement in _SCHEMA:
            connection.execute(statement)
        rows = _schema_rows(connection)
    finally:
        connection.close()
    return rows, sha256(_canonical_json_bytes(rows)).hexdigest()


_EXPECTED_SCHEMA_ROWS, _EXPECTED_SCHEMA_DIGEST = _expected_schema()


def _before_mapping_commit() -> None:
    """Test seam after all mapping transaction writes and before commit."""


def _exact_object(value: object, keys: frozenset[str], *, label: str) -> dict[str, Any]:
    if type(value) is not dict or frozenset(value) != keys:
        raise CoreBridgeStoreDataCorruptionError(f"stored {label} is not a closed object")
    return cast(dict[str, Any], value)


def _bounded_text(value: object, *, label: str, minimum: int = 1) -> str:
    if type(value) is not str:
        raise CoreBridgeStoreContractError(f"bridge {label} must be text")
    try:
        encoded = value.encode("utf-8", errors="strict")
    except UnicodeError as exc:
        raise CoreBridgeStoreContractError(f"bridge {label} is not valid UTF-8") from exc
    if (
        not minimum <= len(encoded) <= MAX_IDENTITY_BYTES
        or value != value.strip()
        or unicodedata.normalize("NFC", value) != value
        or any(unicodedata.category(character) == "Cc" for character in value)
    ):
        raise CoreBridgeStoreContractError(f"bridge {label} is invalid")
    return value


def _digest(value: object, *, label: str) -> str:
    if type(value) is not str or _DIGEST_RE.fullmatch(value) is None:
        raise CoreBridgeStoreContractError(f"bridge {label} is not a SHA-256 digest")
    return value


def _optional(value: object, decoder: Callable[[object], Any]) -> Any:
    return None if value is None else decoder(value)


def _model_value(model: core_v1.ContractModel) -> dict[str, Any]:
    return cast(dict[str, Any], model.model_dump(mode="json"))


def _model(model_type: type[_ModelT], value: object, *, label: str) -> _ModelT:
    if type(value) is not dict:
        raise CoreBridgeStoreDataCorruptionError(f"stored {label} is not an object")
    try:
        parsed = model_type.model_validate_json(
            _canonical_json_bytes(value),
            strict=True,
            context={"_openevo_historical_codex_model_recovery": True},
        )
    except (ValidationError, ValueError, TypeError) as exc:
        raise CoreBridgeStoreDataCorruptionError(
            f"stored {label} violates its Core model"
        ) from exc
    if _canonical_json_bytes(parsed.model_dump(mode="json")) != _canonical_json_bytes(value):
        raise CoreBridgeStoreDataCorruptionError(f"stored {label} is not an exact Core model")
    return parsed


def _abort_value(value: CoreWorkspaceUploadAbortOperationV1) -> dict[str, Any]:
    value.__post_init__()
    return {
        "idempotency_key": _bounded_text(value.idempotency_key, label="abort key"),
        "request": _model_value(value.request),
        "request_sha256": _digest(value.request_sha256, label="abort request digest"),
        "state": value.state.value,
        "upload": _model_value(value.upload),
    }


def _abort_from_value(value: object) -> CoreWorkspaceUploadAbortOperationV1:
    data = _exact_object(
        value,
        frozenset({"upload", "request_sha256", "request", "idempotency_key", "state"}),
        label="workspace abort",
    )
    try:
        state = CoreWorkspaceUploadAbortStateV1(data["state"])
    except (TypeError, ValueError) as exc:
        raise CoreBridgeStoreDataCorruptionError(
            "stored workspace abort state is invalid"
        ) from exc
    try:
        return CoreWorkspaceUploadAbortOperationV1(
            upload=_model(
                core_v1.WorkspaceUploadSessionV1, data["upload"], label="workspace abort upload"
            ),
            request_sha256=_digest(data["request_sha256"], label="abort request digest"),
            request=_model(
                core_v1.WorkspaceUploadAbortV1, data["request"], label="workspace abort request"
            ),
            idempotency_key=_bounded_text(data["idempotency_key"], label="abort key"),
            state=state,
        )
    except (ValueError, CoreBridgeStoreContractError) as exc:
        raise CoreBridgeStoreDataCorruptionError("stored workspace abort is invalid") from exc


def _finalize_value(value: CoreWorkspaceUploadFinalizeAuthorityV1) -> dict[str, Any]:
    value.verify()
    return {
        "idempotency_key": _bounded_text(value.idempotency_key, label="finalize key"),
        "outcome": None if value.outcome is None else _model_value(value.outcome),
        "outcome_sha256": _optional(
            value.outcome_sha256,
            lambda item: _digest(item, label="finalize outcome digest"),
        ),
        "project_etag": _bounded_text(value.project_etag, label="finalize project ETag"),
        "request": _model_value(value.request),
        "request_sha256": _digest(value.request_sha256, label="finalize request digest"),
        "state": value.state.value,
        "upload": _model_value(value.upload),
        "upload_etag": _bounded_text(value.upload_etag, label="finalize upload ETag"),
    }


def _finalize_from_value(value: object) -> CoreWorkspaceUploadFinalizeAuthorityV1:
    data = _exact_object(
        value,
        frozenset(
            {
                "upload",
                "request_sha256",
                "request",
                "idempotency_key",
                "upload_etag",
                "project_etag",
                "state",
                "outcome",
                "outcome_sha256",
            }
        ),
        label="workspace finalize",
    )
    try:
        state = CoreWorkspaceUploadFinalizeStateV1(data["state"])
    except (TypeError, ValueError) as exc:
        raise CoreBridgeStoreDataCorruptionError(
            "stored workspace finalize state is invalid"
        ) from exc
    try:
        return CoreWorkspaceUploadFinalizeAuthorityV1(
            upload=_model(
                core_v1.WorkspaceUploadSessionV1,
                data["upload"],
                label="workspace finalize upload",
            ),
            request_sha256=_digest(data["request_sha256"], label="finalize request digest"),
            request=_model(
                core_v1.WorkspaceUploadFinalizeV1,
                data["request"],
                label="workspace finalize request",
            ),
            idempotency_key=_bounded_text(data["idempotency_key"], label="finalize key"),
            upload_etag=_bounded_text(data["upload_etag"], label="finalize upload ETag"),
            project_etag=_bounded_text(data["project_etag"], label="finalize project ETag"),
            state=state,
            outcome=_optional(
                data["outcome"],
                lambda item: _model(
                    core_v1.WorkspaceUploadFinalizeResponseV1,
                    item,
                    label="workspace finalize outcome",
                ),
            ),
            outcome_sha256=_optional(
                data["outcome_sha256"],
                lambda item: _digest(item, label="finalize outcome digest"),
            ),
        )
    except (ValueError, CoreBridgeStoreContractError) as exc:
        raise CoreBridgeStoreDataCorruptionError("stored workspace finalize is invalid") from exc


def _create_value(value: CoreProjectCreateOperationV1) -> dict[str, Any]:
    if type(value) is not CoreProjectCreateOperationV1:
        raise CoreBridgeStoreContractError("create operation has the wrong type")
    value.__post_init__()
    if (
        value.workspace_upload_project_snapshot is not None
        and value.workspace_upload_project_snapshot.kind is not core_v1.SnapshotKind.PROJECT
    ):
        raise CoreBridgeStoreContractError(
            "workspace upload authority must use a project snapshot"
        )
    return {
        "core_host_identity": _bounded_text(value.core_host_identity, label="Core host identity"),
        "core_project_id": _optional(
            value.core_project_id,
            lambda item: _bounded_text(item, label="Core project ID"),
        ),
        "idempotency_key": _bounded_text(value.idempotency_key, label="create key"),
        "local_project_id": _bounded_text(value.local_project_id, label="Local project ID"),
        "profile_id": _bounded_text(value.profile_id, label="profile ID"),
        "project_create": _model_value(value.project_create),
        "project_immutable_authority": (
            None
            if value.project_immutable_authority is None
            else _immutable_value(value.project_immutable_authority)
        ),
        "record_type": "CoreProjectCreateOperationV1",
        "request_sha256": _digest(value.request_sha256, label="create request digest"),
        "schema_version": "1",
        "state": value.state.value,
        "workspace_upload_abort": (
            None
            if value.workspace_upload_abort is None
            else _abort_value(value.workspace_upload_abort)
        ),
        "workspace_upload_finalize": (
            None
            if value.workspace_upload_finalize is None
            else _finalize_value(value.workspace_upload_finalize)
        ),
        "workspace_upload_id": _optional(
            value.workspace_upload_id,
            lambda item: _bounded_text(item, label="workspace upload ID"),
        ),
        "workspace_upload_project_snapshot": (
            None
            if value.workspace_upload_project_snapshot is None
            else _model_value(value.workspace_upload_project_snapshot)
        ),
    }


_CREATE_KEYS = frozenset(
    {
        "schema_version",
        "record_type",
        "local_project_id",
        "profile_id",
        "core_host_identity",
        "request_sha256",
        "project_create",
        "project_immutable_authority",
        "idempotency_key",
        "state",
        "core_project_id",
        "workspace_upload_id",
        "workspace_upload_project_snapshot",
        "workspace_upload_abort",
        "workspace_upload_finalize",
    }
)


def _create_from_value(value: object) -> CoreProjectCreateOperationV1:
    data = _exact_object(value, _CREATE_KEYS, label="create operation")
    if data["schema_version"] != "1" or data["record_type"] != "CoreProjectCreateOperationV1":
        raise CoreBridgeStoreDataCorruptionError("stored create operation type is invalid")
    try:
        state = CoreProjectCreateStateV1(data["state"])
    except (TypeError, ValueError) as exc:
        raise CoreBridgeStoreDataCorruptionError("stored create state is invalid") from exc
    try:
        return CoreProjectCreateOperationV1(
            local_project_id=_bounded_text(data["local_project_id"], label="Local project ID"),
            profile_id=_bounded_text(data["profile_id"], label="profile ID"),
            core_host_identity=_bounded_text(
                data["core_host_identity"], label="Core host identity"
            ),
            request_sha256=_digest(data["request_sha256"], label="create request digest"),
            project_create=_model(
                core_v1.ProjectCreateV1, data["project_create"], label="project create"
            ),
            idempotency_key=_bounded_text(data["idempotency_key"], label="create key"),
            state=state,
            core_project_id=_optional(
                data["core_project_id"],
                lambda item: _bounded_text(item, label="Core project ID"),
            ),
            project_immutable_authority=_optional(
                data["project_immutable_authority"], _immutable_from_value
            ),
            workspace_upload_id=_optional(
                data["workspace_upload_id"],
                lambda item: _bounded_text(item, label="workspace upload ID"),
            ),
            workspace_upload_project_snapshot=_optional(
                data["workspace_upload_project_snapshot"],
                lambda item: _model(
                    core_v1.ImmutableSnapshotRefV1,
                    item,
                    label="workspace upload project snapshot",
                ),
            ),
            workspace_upload_abort=_optional(data["workspace_upload_abort"], _abort_from_value),
            workspace_upload_finalize=_optional(
                data["workspace_upload_finalize"], _finalize_from_value
            ),
        )
    except (ValueError, CoreBridgeStoreContractError) as exc:
        raise CoreBridgeStoreDataCorruptionError("stored create operation is invalid") from exc


def _immutable_value(value: CoreProjectPatchImmutableAuthorityV1) -> dict[str, Any]:
    return {
        "created_at": _bounded_text(value.created_at, label="project created timestamp"),
        "project_create": _model_value(value.project_create),
        "project_id": _bounded_text(value.project_id, label="authority project ID"),
        "task_snapshot": _model_value(value.task_snapshot),
    }


def _immutable_from_value(value: object) -> CoreProjectPatchImmutableAuthorityV1:
    data = _exact_object(
        value,
        frozenset({"project_id", "project_create", "task_snapshot", "created_at"}),
        label="patch immutable authority",
    )
    return CoreProjectPatchImmutableAuthorityV1(
        project_id=_bounded_text(data["project_id"], label="authority project ID"),
        project_create=_model(
            core_v1.ProjectCreateV1,
            data["project_create"],
            label="authority project create",
        ),
        task_snapshot=_model(
            core_v1.ImmutableSnapshotRefV1,
            data["task_snapshot"],
            label="authority task snapshot",
        ),
        created_at=_bounded_text(data["created_at"], label="project created timestamp"),
    )


def _mutable_value(value: CoreProjectPatchMutableAuthorityV1) -> dict[str, Any]:
    return {
        "active_revision": (
            None if value.active_revision is None else _model_value(value.active_revision)
        ),
        "etag": _bounded_text(value.etag, label="project ETag"),
        "model_preparation": _model_value(value.model_preparation),
        "project_snapshot": _model_value(value.project_snapshot),
        "registry_digest": _optional(
            value.registry_digest,
            lambda item: _digest(item, label="registry digest"),
        ),
        "status": value.status.value,
        "updated_at": _bounded_text(value.updated_at, label="project updated timestamp"),
        "workspace_publication": (
            None
            if value.workspace_publication is None
            else _model_value(value.workspace_publication)
        ),
        "workspace_snapshot": (
            None if value.workspace_snapshot is None else _model_value(value.workspace_snapshot)
        ),
    }


def _mutable_from_value(value: object) -> CoreProjectPatchMutableAuthorityV1:
    data = _exact_object(
        value,
        frozenset(
            {
                "status",
                "project_snapshot",
                "workspace_snapshot",
                "workspace_publication",
                "active_revision",
                "registry_digest",
                "model_preparation",
                "updated_at",
                "etag",
            }
        ),
        label="patch mutable authority",
    )
    try:
        status = core_v1.ProjectStatus(data["status"])
    except (TypeError, ValueError) as exc:
        raise CoreBridgeStoreDataCorruptionError("stored project status is invalid") from exc
    return CoreProjectPatchMutableAuthorityV1(
        status=status,
        project_snapshot=_model(
            core_v1.ImmutableSnapshotRefV1,
            data["project_snapshot"],
            label="authority project snapshot",
        ),
        workspace_snapshot=_optional(
            data["workspace_snapshot"],
            lambda item: _model(
                core_v1.ImmutableSnapshotRefV1, item, label="authority workspace snapshot"
            ),
        ),
        workspace_publication=_optional(
            data["workspace_publication"],
            lambda item: _model(
                core_v1.WorkspacePublicationV1, item, label="authority workspace publication"
            ),
        ),
        active_revision=_optional(
            data["active_revision"],
            lambda item: _model(core_v1.RevisionRefV1, item, label="authority active revision"),
        ),
        registry_digest=_optional(
            data["registry_digest"], lambda item: _digest(item, label="registry digest")
        ),
        model_preparation=_model(
            core_v1.ModelPreparationV1,
            data["model_preparation"],
            label="authority model preparation",
        ),
        updated_at=_bounded_text(data["updated_at"], label="project updated timestamp"),
        etag=_bounded_text(data["etag"], label="project ETag"),
    )


def _patch_value(value: CoreProjectPatchOperationV1) -> dict[str, Any]:
    if type(value) is not CoreProjectPatchOperationV1:
        raise CoreBridgeStoreContractError("patch operation has the wrong type")
    value.__post_init__()
    return {
        "base_project": _model_value(value.base_project),
        "core_host_identity": _bounded_text(value.core_host_identity, label="Core host identity"),
        "core_project_id": _bounded_text(value.core_project_id, label="Core project ID"),
        "idempotency_key": _bounded_text(value.idempotency_key, label="patch key"),
        "local_project_id": _bounded_text(value.local_project_id, label="Local project ID"),
        "new_project_create": _model_value(value.new_project_create),
        "new_request_sha256": _digest(value.new_request_sha256, label="new request digest"),
        "old_project_create": _model_value(value.old_project_create),
        "old_request_sha256": _digest(value.old_request_sha256, label="old request digest"),
        "outcome": None if value.outcome is None else _model_value(value.outcome),
        "outcome_immutable": (
            None if value.outcome_immutable is None else _immutable_value(value.outcome_immutable)
        ),
        "outcome_mutable": (
            None if value.outcome_mutable is None else _mutable_value(value.outcome_mutable)
        ),
        "patch": _model_value(value.patch),
        "patch_request_sha256": _digest(value.patch_request_sha256, label="patch digest"),
        "profile_id": _bounded_text(value.profile_id, label="profile ID"),
        "record_type": "CoreProjectPatchOperationV1",
        "schema_version": "1",
        "state": value.state.value,
    }


_PATCH_KEYS = frozenset(
    {
        "schema_version",
        "record_type",
        "local_project_id",
        "profile_id",
        "core_host_identity",
        "core_project_id",
        "old_request_sha256",
        "old_project_create",
        "new_request_sha256",
        "new_project_create",
        "patch_request_sha256",
        "patch",
        "idempotency_key",
        "base_project",
        "state",
        "outcome",
        "outcome_immutable",
        "outcome_mutable",
    }
)


def _patch_from_value(value: object) -> CoreProjectPatchOperationV1:
    data = _exact_object(value, _PATCH_KEYS, label="patch operation")
    if data["schema_version"] != "1" or data["record_type"] != "CoreProjectPatchOperationV1":
        raise CoreBridgeStoreDataCorruptionError("stored patch operation type is invalid")
    try:
        state = CoreProjectPatchStateV1(data["state"])
    except (TypeError, ValueError) as exc:
        raise CoreBridgeStoreDataCorruptionError("stored patch state is invalid") from exc
    try:
        return CoreProjectPatchOperationV1(
            local_project_id=_bounded_text(data["local_project_id"], label="Local project ID"),
            profile_id=_bounded_text(data["profile_id"], label="profile ID"),
            core_host_identity=_bounded_text(
                data["core_host_identity"], label="Core host identity"
            ),
            core_project_id=_bounded_text(data["core_project_id"], label="Core project ID"),
            old_request_sha256=_digest(data["old_request_sha256"], label="old request digest"),
            old_project_create=_model(
                core_v1.ProjectCreateV1,
                data["old_project_create"],
                label="old project create",
            ),
            new_request_sha256=_digest(data["new_request_sha256"], label="new request digest"),
            new_project_create=_model(
                core_v1.ProjectCreateV1,
                data["new_project_create"],
                label="new project create",
            ),
            patch_request_sha256=_digest(data["patch_request_sha256"], label="patch digest"),
            patch=_model(core_v1.ProjectPatchV1, data["patch"], label="project patch"),
            idempotency_key=_bounded_text(data["idempotency_key"], label="patch key"),
            base_project=_model(
                core_v1.ProjectV1, data["base_project"], label="patch base project"
            ),
            state=state,
            outcome=_optional(
                data["outcome"],
                lambda item: _model(core_v1.ProjectV1, item, label="patch outcome"),
            ),
            outcome_immutable=_optional(data["outcome_immutable"], _immutable_from_value),
            outcome_mutable=_optional(data["outcome_mutable"], _mutable_from_value),
        )
    except (ValueError, CoreBridgeStoreContractError) as exc:
        raise CoreBridgeStoreDataCorruptionError("stored patch operation is invalid") from exc


def _mapping_value(value: CoreProjectMappingV1) -> dict[str, Any]:
    if type(value) is not CoreProjectMappingV1:
        raise CoreBridgeStoreContractError("mapping has the wrong type")
    value.__post_init__()
    if sha256(_canonical_json_bytes(value.project_create.model_dump(mode="json"))).hexdigest() != (
        value.request_sha256
    ):
        raise CoreBridgeStoreContractError("mapping request digest does not match project intent")
    if (
        value.project_snapshot.kind is not core_v1.SnapshotKind.PROJECT
        or value.task_snapshot.kind is not core_v1.SnapshotKind.TASK
        or value.workspace_snapshot.kind is not core_v1.SnapshotKind.WORKSPACE
        or value.active_revision.project_id != value.core_project_id
    ):
        raise CoreBridgeStoreContractError("mapping typed authority is inconsistent")
    if (
        value.immutable_authority.project_id != value.core_project_id
        or value.immutable_authority.project_create != value.project_create
        or value.immutable_authority.task_snapshot != value.task_snapshot
        or value.mutable_authority.project_snapshot != value.project_snapshot
        or value.mutable_authority.workspace_snapshot != value.workspace_snapshot
        or value.mutable_authority.registry_digest != value.registry_digest
        or value.mutable_authority.etag != value.project_etag
        or value.mutable_authority.active_revision != value.active_revision
        or value.mutable_authority.updated_at != value.project_updated_at
    ):
        raise CoreBridgeStoreContractError(
            "mapping flattened fields do not match complete project authority"
        )
    return {
        "active_revision": _model_value(value.active_revision),
        "core_host_identity": _bounded_text(value.core_host_identity, label="Core host identity"),
        "core_project_id": _bounded_text(value.core_project_id, label="Core project ID"),
        "local_project_id": _bounded_text(value.local_project_id, label="Local project ID"),
        "immutable_authority": _immutable_value(value.immutable_authority),
        "mapping_generation": value.mapping_generation,
        "mutable_authority": _mutable_value(value.mutable_authority),
        "predecessor_request_sha256": _optional(
            value.predecessor_request_sha256,
            lambda item: _digest(item, label="predecessor request digest"),
        ),
        "profile_id": _bounded_text(value.profile_id, label="profile ID"),
        "project_create": _model_value(value.project_create),
        "project_etag": _bounded_text(value.project_etag, label="project ETag"),
        "project_snapshot": _model_value(value.project_snapshot),
        "project_updated_at": _bounded_text(
            value.project_updated_at, label="project updated timestamp"
        ),
        "record_type": "CoreProjectMappingV1",
        "registry_digest": _digest(value.registry_digest, label="registry digest"),
        "request_sha256": _digest(value.request_sha256, label="mapping request digest"),
        "schema_version": "1",
        "task_snapshot": _model_value(value.task_snapshot),
        "workspace_snapshot": _model_value(value.workspace_snapshot),
    }


_MAPPING_KEYS = frozenset(
    {
        "schema_version",
        "record_type",
        "local_project_id",
        "profile_id",
        "core_host_identity",
        "core_project_id",
        "request_sha256",
        "project_create",
        "project_snapshot",
        "task_snapshot",
        "workspace_snapshot",
        "registry_digest",
        "project_etag",
        "active_revision",
        "project_updated_at",
        "mapping_generation",
        "immutable_authority",
        "mutable_authority",
        "predecessor_request_sha256",
    }
)


def _mapping_from_value(value: object) -> CoreProjectMappingV1:
    data = _exact_object(value, _MAPPING_KEYS, label="project mapping")
    if data["schema_version"] != "1" or data["record_type"] != "CoreProjectMappingV1":
        raise CoreBridgeStoreDataCorruptionError("stored mapping type is invalid")
    if type(data["mapping_generation"]) is not int:
        raise CoreBridgeStoreDataCorruptionError("stored mapping generation is invalid")
    try:
        mapping = CoreProjectMappingV1(
            local_project_id=_bounded_text(data["local_project_id"], label="Local project ID"),
            profile_id=_bounded_text(data["profile_id"], label="profile ID"),
            core_host_identity=_bounded_text(
                data["core_host_identity"], label="Core host identity"
            ),
            core_project_id=_bounded_text(data["core_project_id"], label="Core project ID"),
            request_sha256=_digest(data["request_sha256"], label="mapping request digest"),
            project_create=_model(
                core_v1.ProjectCreateV1, data["project_create"], label="mapped project create"
            ),
            project_snapshot=_model(
                core_v1.ImmutableSnapshotRefV1,
                data["project_snapshot"],
                label="mapped project snapshot",
            ),
            task_snapshot=_model(
                core_v1.ImmutableSnapshotRefV1,
                data["task_snapshot"],
                label="mapped task snapshot",
            ),
            workspace_snapshot=_model(
                core_v1.ImmutableSnapshotRefV1,
                data["workspace_snapshot"],
                label="mapped workspace snapshot",
            ),
            registry_digest=_digest(data["registry_digest"], label="registry digest"),
            project_etag=_bounded_text(data["project_etag"], label="project ETag"),
            active_revision=_model(
                core_v1.RevisionRefV1,
                data["active_revision"],
                label="mapped active revision",
            ),
            project_updated_at=_bounded_text(
                data["project_updated_at"], label="project updated timestamp"
            ),
            immutable_authority=_immutable_from_value(data["immutable_authority"]),
            mutable_authority=_mutable_from_value(data["mutable_authority"]),
            mapping_generation=data["mapping_generation"],
            predecessor_request_sha256=_optional(
                data["predecessor_request_sha256"],
                lambda item: _digest(item, label="predecessor request digest"),
            ),
        )
        _mapping_value(mapping)
        return mapping
    except (ValueError, CoreBridgeStoreContractError) as exc:
        raise CoreBridgeStoreDataCorruptionError("stored project mapping is invalid") from exc


@dataclass(frozen=True, slots=True)
class _ProjectHeadSuccessorHistoryAuthority:
    proof: CoreProjectHeadSuccessorProofV1
    predecessor_mapping_sha256: str | None = None
    predecessor_project_sha256: str | None = None


def _project_head_successor_value(
    value: _ProjectHeadSuccessorHistoryAuthority,
) -> dict[str, Any]:
    if type(value) is not _ProjectHeadSuccessorHistoryAuthority:
        raise CoreBridgeStoreContractError("project-head successor authority has the wrong type")
    if type(value.proof) is not CoreProjectHeadSuccessorProofV1:
        raise CoreBridgeStoreContractError("project-head successor proof has the wrong type")
    if (value.predecessor_mapping_sha256 is None) == (
        value.predecessor_project_sha256 is None
    ):
        raise CoreBridgeStoreContractError(
            "project-head successor authority must bind exactly one predecessor"
        )
    proof_value = {
        "head": _model_value(value.proof.head),
        "project": _model_value(value.proof.project),
        "revision": _model_value(value.proof.revision),
    }
    if value.predecessor_mapping_sha256 is not None:
        return {
            **proof_value,
            "predecessor_mapping_sha256": _digest(
                value.predecessor_mapping_sha256,
                label="predecessor mapping digest",
            ),
        }
    return {
        **proof_value,
        "predecessor_mapping_sha256": None,
        "predecessor_project_sha256": _digest(
            value.predecessor_project_sha256,
            label="predecessor project digest",
        ),
    }


def _project_head_successor_from_value(
    value: object,
) -> _ProjectHeadSuccessorHistoryAuthority:
    if not isinstance(value, dict):
        raise CoreBridgeStoreDataCorruptionError(
            "stored project-head successor authority is not an object"
        )
    legacy = "predecessor_project_sha256" not in value
    data = _exact_object(
        value,
        frozenset(
            {
                "predecessor_mapping_sha256",
                *(() if legacy else ("predecessor_project_sha256",)),
                "project",
                "head",
                "revision",
            }
        ),
        label="project-head successor authority",
    )
    try:
        predecessor_mapping_sha256 = _optional(
            data["predecessor_mapping_sha256"],
            lambda item: _digest(item, label="predecessor mapping digest"),
        )
        predecessor_project_sha256 = (
            None
            if legacy
            else _optional(
                data["predecessor_project_sha256"],
                lambda item: _digest(item, label="predecessor project digest"),
            )
        )
        if (predecessor_mapping_sha256 is None) == (
            predecessor_project_sha256 is None
        ):
            raise CoreBridgeStoreContractError(
                "stored successor authority must bind exactly one predecessor"
            )
        return _ProjectHeadSuccessorHistoryAuthority(
            predecessor_mapping_sha256=predecessor_mapping_sha256,
            predecessor_project_sha256=predecessor_project_sha256,
            proof=CoreProjectHeadSuccessorProofV1(
                project=_model(
                    core_v1.ProjectV1,
                    data["project"],
                    label="successor project",
                ),
                head=_model(
                    core_v1.RevisionHeadV1,
                    data["head"],
                    label="successor revision head",
                ),
                revision=_model(
                    core_v1.RevisionV1,
                    data["revision"],
                    label="successor revision",
                ),
                predecessor_project=None,
            ),
        )
    except (ValueError, CoreBridgeStoreContractError) as exc:
        raise CoreBridgeStoreDataCorruptionError(
            "stored project-head successor authority is invalid"
        ) from exc


@dataclass(frozen=True, slots=True)
class _MappingHistoryEntry:
    mapping: CoreProjectMappingV1
    create_operation: CoreProjectCreateOperationV1
    completed_patch: CoreProjectPatchOperationV1 | None
    project_head_successor: _ProjectHeadSuccessorHistoryAuthority | None = None


def _history_value(value: _MappingHistoryEntry) -> dict[str, Any]:
    if value.project_head_successor is not None:
        if value.completed_patch is not None:
            # The 0.1.1 reader rejects this closed record type during startup.
            # The history append and current mapping update share one transaction,
            # so the row itself is the durable rollback barrier.
            return {
                "completed_patch": _patch_value(value.completed_patch),
                "create_operation": _create_value(value.create_operation),
                "mapping": _mapping_value(value.mapping),
                "project_head_successor": _project_head_successor_value(
                    value.project_head_successor
                ),
                "record_type": _PROJECT_HEAD_AND_PATCH_TRANSITION_RECORD,
                "schema_version": "1",
            }
        return {
            "create_operation": _create_value(value.create_operation),
            "mapping": _mapping_value(value.mapping),
            "project_head_successor": _project_head_successor_value(
                value.project_head_successor
            ),
            "record_type": _PROJECT_HEAD_TRANSITION_RECORD,
            "schema_version": "1",
        }
    return {
        "completed_patch": (
            None if value.completed_patch is None else _patch_value(value.completed_patch)
        ),
        "create_operation": _create_value(value.create_operation),
        "mapping": _mapping_value(value.mapping),
        "record_type": _MAPPING_TRANSITION_RECORD,
        "schema_version": "1",
    }


def _history_from_value(value: object) -> _MappingHistoryEntry:
    if type(value) is not dict:
        raise CoreBridgeStoreDataCorruptionError(
            "stored mapping transition history is not an object"
        )
    if value.get("record_type") == _PROJECT_HEAD_TRANSITION_RECORD:
        data = _exact_object(
            value,
            frozenset(
                {
                    "schema_version",
                    "record_type",
                    "mapping",
                    "create_operation",
                    "project_head_successor",
                }
            ),
            label="project-head mapping transition history",
        )
        if data["schema_version"] != "1":
            raise CoreBridgeStoreDataCorruptionError(
                "stored project-head mapping transition type is invalid"
            )
        return _MappingHistoryEntry(
            mapping=_mapping_from_value(data["mapping"]),
            create_operation=_create_from_value(data["create_operation"]),
            completed_patch=None,
            project_head_successor=_project_head_successor_from_value(
                data["project_head_successor"]
            ),
        )
    if value.get("record_type") == _PROJECT_HEAD_AND_PATCH_TRANSITION_RECORD:
        data = _exact_object(
            value,
            frozenset(
                {
                    "schema_version",
                    "record_type",
                    "mapping",
                    "create_operation",
                    "completed_patch",
                    "project_head_successor",
                }
            ),
            label="project-head and patch mapping transition history",
        )
        if data["schema_version"] != "1":
            raise CoreBridgeStoreDataCorruptionError(
                "stored project-head and patch mapping transition type is invalid"
            )
        return _MappingHistoryEntry(
            mapping=_mapping_from_value(data["mapping"]),
            create_operation=_create_from_value(data["create_operation"]),
            completed_patch=_patch_from_value(data["completed_patch"]),
            project_head_successor=_project_head_successor_from_value(
                data["project_head_successor"]
            ),
        )
    data = _exact_object(
        value,
        frozenset(
            {
                "schema_version",
                "record_type",
                "mapping",
                "create_operation",
                "completed_patch",
            }
        ),
        label="mapping transition history",
    )
    if data["schema_version"] != "1" or data["record_type"] != _MAPPING_TRANSITION_RECORD:
        raise CoreBridgeStoreDataCorruptionError("stored mapping transition type is invalid")
    return _MappingHistoryEntry(
        mapping=_mapping_from_value(data["mapping"]),
        create_operation=_create_from_value(data["create_operation"]),
        completed_patch=_optional(data["completed_patch"], _patch_from_value),
        project_head_successor=None,
    )


def _encoded(value: dict[str, Any]) -> tuple[bytes, str]:
    raw = _canonical_json_bytes(value)
    if not 1 <= len(raw) <= MAX_DOCUMENT_BYTES:
        raise CoreBridgeStoreCapacityError("bridge document exceeds its byte bound")
    return raw, sha256(raw).hexdigest()


def _decode_document(
    raw: object,
    claimed_digest: object,
    *,
    label: str,
    decoder: Callable[[object], Any],
    encoder: Callable[[Any], dict[str, Any]],
) -> Any:
    if type(raw) is not bytes or not 1 <= len(raw) <= MAX_DOCUMENT_BYTES:
        raise CoreBridgeStoreDataCorruptionError(f"stored {label} has an invalid byte size")
    if type(claimed_digest) is not str or _DIGEST_RE.fullmatch(claimed_digest) is None:
        raise CoreBridgeStoreDataCorruptionError(f"stored {label} digest is invalid")
    if sha256(raw).hexdigest() != claimed_digest:
        raise CoreBridgeStoreDataCorruptionError(f"stored {label} digest does not match")
    try:
        value = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CoreBridgeStoreDataCorruptionError(f"stored {label} is not valid JSON") from exc
    try:
        canonical = _canonical_json_bytes(value)
    except CoreBridgeStoreContractError as exc:
        raise CoreBridgeStoreDataCorruptionError(f"stored {label} is not canonical JSON") from exc
    if canonical != raw:
        raise CoreBridgeStoreDataCorruptionError(f"stored {label} is not canonical JSON")
    try:
        decoded = decoder(value)
        reencoded = _canonical_json_bytes(encoder(decoded))
    except CoreBridgeStoreDataCorruptionError:
        raise
    except (CoreBridgeStoreContractError, TypeError, ValueError) as exc:
        raise CoreBridgeStoreDataCorruptionError(
            f"stored {label} violates its closed record invariant"
        ) from exc
    if reencoded != raw:
        raise CoreBridgeStoreDataCorruptionError(f"stored {label} is not an exact closed record")
    return decoded


class DesktopCoreBridgeStoreV1:
    """Owner-locked, fail-closed SQLite implementation of bridge persistence v1."""

    def __init__(
        self,
        state_root: Path | str,
        *,
        max_mapping_history_rows: int = DEFAULT_MAX_MAPPING_HISTORY_ROWS,
    ) -> None:
        self._require_secure_platform()
        if type(max_mapping_history_rows) is not int or not 1 <= max_mapping_history_rows <= (
            MAX_RECOVERY_ROWS
        ):
            raise ValueError("max_mapping_history_rows is outside the recovery row bound")
        self._max_mapping_history_rows = max_mapping_history_rows
        self._transaction_lock = threading.RLock()
        self._closed = False
        self._owner_pid = os.getpid()
        self._identity_ready = False
        root = Path(os.path.abspath(os.fspath(Path(state_root).expanduser())))
        self._create_or_validate_root(root)
        self._state_root = root
        self._anchor_name = (
            ROOT_ANCHOR_PREFIX + sha256(os.fsencode(os.fspath(root))).hexdigest() + ".identity"
        )
        flags = os.O_RDONLY | os.O_CLOEXEC | os.O_DIRECTORY | os.O_NOFOLLOW
        try:
            self._root_fd = os.open(root, flags)
        except OSError as exc:
            raise CoreBridgeStoreStateRootError(
                "bridge state root could not be securely opened"
            ) from exc
        root_stat = os.fstat(self._root_fd)
        self._root_identity = (root_stat.st_dev, root_stat.st_ino)
        try:
            self._reject_unknown_state_entries()
            self._open_anchor_parent(root.parent)
            self._ensure_empty_private_file(OWNER_LOCK_FILENAME)
            self._acquire_owner_lock()
            self._ensure_empty_private_file(DATABASE_FILENAME)
            database_stat = self._verify_private_file(DATABASE_FILENAME)
            marker_stat = self._optional_private_file(IDENTITY_MARKER_FILENAME)
            database_was_empty = database_stat.st_size == 0
            if database_was_empty and marker_stat is None:
                self._ensure_empty_private_file(IDENTITY_MARKER_FILENAME)
                marker_stat = self._verify_private_file(IDENTITY_MARKER_FILENAME)
            if marker_stat is None:
                raise CoreBridgeStoreStateRootError(
                    "nonempty or legacy bridge store has no durable identity marker"
                )
            anchor_stat = self._optional_anchor_file()
            if database_was_empty and anchor_stat is None:
                self._ensure_empty_anchor_file()
                anchor_stat = self._verify_anchor_file()
            if anchor_stat is None:
                raise CoreBridgeStoreStateRootError(
                    "nonempty or legacy bridge store has no durable root identity anchor"
                )
            if database_was_empty and anchor_stat.st_size not in (0, MARKER_FILE_BYTES):
                raise CoreBridgeStoreStateRootError("fresh bridge root identity anchor is invalid")
            self._managed_identities = {
                OWNER_LOCK_FILENAME: self._owner_lock_identity,
                DATABASE_FILENAME: self._file_identity(database_stat),
                IDENTITY_MARKER_FILENAME: self._file_identity(marker_stat),
            }
            self._anchor_identity = self._file_identity(anchor_stat)
            self._open_database_file()
            self._verify_storage_files()
            self._verify_sqlite_default_synchronous_full()
            self._connection = self._open_database_connection()
            fresh_database = self._is_fresh_database_after_recovery()
            self._initialize_schema(fresh_database=fresh_database)
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

    def __enter__(self) -> DesktopCoreBridgeStoreV1:
        self._verify_root()
        return self

    def __exit__(self, *args: object) -> None:
        del args
        self.close()

    def __del__(self) -> None:
        if getattr(self, "_closed", True) is False:
            try:
                self.close()
            except (CoreBridgeStoreError, OSError):
                pass

    def close(self) -> None:
        self._check_owner_pid()
        with self._transaction_lock:
            if not self._closed:
                self._close_resources()

    def _close_resources(self) -> None:
        owned_process = os.getpid() == getattr(self, "_owner_pid", None)
        connection = getattr(self, "_connection", None)
        if connection is not None:
            try:
                connection.close()
            finally:
                del self._connection
        database_fd = getattr(self, "_database_fd", None)
        if database_fd is not None:
            os.close(database_fd)
            del self._database_fd
        owner_lock_fd = getattr(self, "_owner_lock_fd", None)
        if owner_lock_fd is not None:
            try:
                if owned_process:
                    fcntl.flock(owner_lock_fd, fcntl.LOCK_UN)
            finally:
                os.close(owner_lock_fd)
                del self._owner_lock_fd
        root_fd = getattr(self, "_root_fd", None)
        if root_fd is not None:
            os.close(root_fd)
            del self._root_fd
        parent_fd = getattr(self, "_anchor_parent_fd", None)
        if parent_fd is not None:
            os.close(parent_fd)
            del self._anchor_parent_fd
        self._closed = True

    def _check_owner_pid(self) -> None:
        if os.getpid() != getattr(self, "_owner_pid", None):
            raise CoreBridgeStoreStateRootError("bridge store cannot be inherited across fork")

    @staticmethod
    def _require_secure_platform() -> None:
        if (
            not hasattr(os, "O_NOFOLLOW")
            or not hasattr(os, "O_DIRECTORY")
            or not hasattr(os, "pwrite")
            or os.open not in os.supports_dir_fd
            or os.stat not in os.supports_dir_fd
            or os.stat not in os.supports_follow_symlinks
        ):
            raise CoreBridgeStoreStateRootError(
                "platform lacks no-follow descriptor-relative bridge storage"
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
            raise CoreBridgeStoreStateRootError("bridge state root must be a real directory")
        if hasattr(os, "getuid") and root_stat.st_uid != os.getuid():
            raise CoreBridgeStoreStateRootError("bridge state root must be owned by this user")
        if stat.S_IMODE(root_stat.st_mode) != 0o700:
            raise CoreBridgeStoreStateRootError("bridge state root mode must be 0700")

    def _verify_root(self) -> None:
        if self._closed:
            raise CoreBridgeStoreStateRootError("bridge store is closed")
        self._check_owner_pid()
        try:
            path_stat = os.lstat(self._state_root)
            fd_stat = os.fstat(self._root_fd)
        except OSError as exc:
            raise CoreBridgeStoreStateRootError("bridge state root is unavailable") from exc
        if (
            not stat.S_ISDIR(path_stat.st_mode)
            or stat.S_ISLNK(path_stat.st_mode)
            or (path_stat.st_dev, path_stat.st_ino) != self._root_identity
            or (fd_stat.st_dev, fd_stat.st_ino) != self._root_identity
        ):
            raise CoreBridgeStoreStateRootError("bridge state root identity changed")
        if hasattr(os, "getuid") and path_stat.st_uid != os.getuid():
            raise CoreBridgeStoreStateRootError("bridge state root ownership changed")
        if stat.S_IMODE(path_stat.st_mode) != 0o700:
            raise CoreBridgeStoreStateRootError("bridge state root mode changed")

    def _reject_unknown_state_entries(self) -> None:
        allowed = frozenset((*_MANAGED_FILES, *_SQLITE_SIDE_FILES))
        try:
            with os.scandir(self._root_fd) as entries:
                for entry in entries:
                    if entry.name not in allowed:
                        raise CoreBridgeStoreStateRootError(
                            "bridge state root contains unknown managed state"
                        )
        except CoreBridgeStoreError:
            raise
        except OSError as exc:
            raise CoreBridgeStoreStateRootError(
                "bridge state root could not be enumerated"
            ) from exc

    @staticmethod
    def _file_identity(value: os.stat_result) -> tuple[int, int]:
        return value.st_dev, value.st_ino

    def _open_anchor_parent(self, parent: Path) -> None:
        try:
            path_stat = os.lstat(parent)
            descriptor = os.open(
                parent,
                os.O_RDONLY | os.O_CLOEXEC | os.O_DIRECTORY | os.O_NOFOLLOW,
            )
            fd_stat = os.fstat(descriptor)
        except OSError as exc:
            raise CoreBridgeStoreStateRootError(
                "bridge root anchor parent could not be securely opened"
            ) from exc
        if (
            not stat.S_ISDIR(path_stat.st_mode)
            or stat.S_ISLNK(path_stat.st_mode)
            or self._file_identity(path_stat) != self._file_identity(fd_stat)
            or (hasattr(os, "getuid") and path_stat.st_uid != os.getuid())
            or stat.S_IMODE(path_stat.st_mode) & 0o022
        ):
            os.close(descriptor)
            raise CoreBridgeStoreStateRootError(
                "bridge root anchor parent must be owner-controlled"
            )
        self._anchor_parent = parent
        self._anchor_parent_fd = descriptor
        self._anchor_parent_identity = self._file_identity(fd_stat)

    def _verify_anchor_parent(self) -> None:
        self._check_owner_pid()
        try:
            path_stat = os.lstat(self._anchor_parent)
            fd_stat = os.fstat(self._anchor_parent_fd)
        except OSError as exc:
            raise CoreBridgeStoreStateRootError(
                "bridge root anchor parent is unavailable"
            ) from exc
        if (
            self._file_identity(path_stat) != self._anchor_parent_identity
            or self._file_identity(fd_stat) != self._anchor_parent_identity
            or stat.S_IMODE(path_stat.st_mode) & 0o022
        ):
            raise CoreBridgeStoreStateRootError("bridge root anchor parent identity changed")

    @staticmethod
    def _validate_private_file_stat(name: str, value: os.stat_result) -> os.stat_result:
        if not stat.S_ISREG(value.st_mode) or stat.S_ISLNK(value.st_mode):
            raise CoreBridgeStoreStateRootError(f"private bridge file {name} must be regular")
        if value.st_nlink != 1:
            raise CoreBridgeStoreStateRootError(f"private bridge file {name} must have one link")
        if hasattr(os, "getuid") and value.st_uid != os.getuid():
            raise CoreBridgeStoreStateRootError(f"private bridge file {name} has the wrong owner")
        if stat.S_IMODE(value.st_mode) != 0o600:
            raise CoreBridgeStoreStateRootError(f"private bridge file {name} mode must be 0600")
        return value

    def _verify_private_file(self, name: str) -> os.stat_result:
        self._verify_root()
        try:
            value = os.stat(name, dir_fd=self._root_fd, follow_symlinks=False)
        except OSError as exc:
            raise CoreBridgeStoreStateRootError(
                f"private bridge file {name} is unavailable"
            ) from exc
        return self._validate_private_file_stat(name, value)

    def _optional_private_file(self, name: str) -> os.stat_result | None:
        self._verify_root()
        try:
            value = os.stat(name, dir_fd=self._root_fd, follow_symlinks=False)
        except FileNotFoundError:
            return None
        except OSError as exc:
            raise CoreBridgeStoreStateRootError(f"SQLite side file {name} is unavailable") from exc
        return self._validate_private_file_stat(name, value)

    def _optional_anchor_file(self) -> os.stat_result | None:
        self._verify_anchor_parent()
        try:
            value = os.stat(
                self._anchor_name,
                dir_fd=self._anchor_parent_fd,
                follow_symlinks=False,
            )
        except FileNotFoundError:
            return None
        except OSError as exc:
            raise CoreBridgeStoreStateRootError(
                "bridge durable root identity anchor is unavailable"
            ) from exc
        return self._validate_private_file_stat(self._anchor_name, value)

    def _verify_anchor_file(self) -> os.stat_result:
        value = self._optional_anchor_file()
        if value is None:
            raise CoreBridgeStoreStateRootError(
                "bridge durable root identity anchor is unavailable"
            )
        return value

    def _ensure_empty_private_file(self, name: str) -> None:
        self._verify_root()
        flags = os.O_RDWR | os.O_CLOEXEC | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW
        try:
            descriptor = os.open(name, flags, 0o600, dir_fd=self._root_fd)
        except FileExistsError:
            self._verify_private_file(name)
            return
        except OSError as exc:
            raise CoreBridgeStoreStateRootError(
                f"could not create private bridge file {name}"
            ) from exc
        try:
            os.fchmod(descriptor, 0o600)
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        os.fsync(self._root_fd)

    def _ensure_empty_anchor_file(self) -> None:
        self._verify_anchor_parent()
        flags = os.O_RDWR | os.O_CLOEXEC | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW
        try:
            descriptor = os.open(
                self._anchor_name,
                flags,
                0o600,
                dir_fd=self._anchor_parent_fd,
            )
        except FileExistsError:
            self._verify_anchor_file()
            return
        except OSError as exc:
            raise CoreBridgeStoreStateRootError(
                "could not create bridge durable root identity anchor"
            ) from exc
        try:
            os.fchmod(descriptor, 0o600)
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        os.fsync(self._anchor_parent_fd)

    def _acquire_owner_lock(self) -> None:
        expected = self._verify_private_file(OWNER_LOCK_FILENAME)
        flags = os.O_RDWR | os.O_CLOEXEC | os.O_NOFOLLOW
        try:
            descriptor = os.open(OWNER_LOCK_FILENAME, flags, dir_fd=self._root_fd)
        except OSError as exc:
            raise CoreBridgeStoreStateRootError("bridge owner lock could not be opened") from exc
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            os.close(descriptor)
            raise CoreBridgeStoreStateRootError(
                "bridge state root is already owned by another process"
            ) from exc
        except OSError:
            os.close(descriptor)
            raise
        if self._file_identity(os.fstat(descriptor)) != self._file_identity(expected):
            fcntl.flock(descriptor, fcntl.LOCK_UN)
            os.close(descriptor)
            raise CoreBridgeStoreStateRootError("bridge owner lock identity changed")
        self._owner_lock_fd = descriptor
        self._owner_lock_identity = self._file_identity(os.fstat(descriptor))

    def _open_database_file(self) -> None:
        expected = self._verify_private_file(DATABASE_FILENAME)
        flags = os.O_RDWR | os.O_CLOEXEC | os.O_NOFOLLOW
        try:
            descriptor = os.open(DATABASE_FILENAME, flags, dir_fd=self._root_fd)
        except OSError as exc:
            raise CoreBridgeStoreStateRootError(
                "bridge database could not be securely opened"
            ) from exc
        if self._file_identity(os.fstat(descriptor)) != self._file_identity(expected):
            os.close(descriptor)
            raise CoreBridgeStoreStateRootError("bridge database identity changed while opening")
        self._database_fd = descriptor

    def _verify_storage_files(self) -> None:
        self._verify_root()
        self._verify_anchor_parent()
        identities = getattr(self, "_managed_identities", None)
        for name in _MANAGED_FILES:
            value = self._verify_private_file(name)
            if identities is not None and self._file_identity(value) != identities[name]:
                raise CoreBridgeStoreStateRootError(f"private bridge file {name} identity changed")
        database_fd = getattr(self, "_database_fd", None)
        if (
            database_fd is not None
            and self._file_identity(os.fstat(database_fd))
            != (self._managed_identities[DATABASE_FILENAME])
        ):
            raise CoreBridgeStoreStateRootError("held bridge database identity changed")
        marker_size = self._verify_private_file(IDENTITY_MARKER_FILENAME).st_size
        if marker_size != MARKER_FILE_BYTES and not (
            not self._identity_ready and marker_size == 0
        ):
            raise CoreBridgeStoreStateRootError(
                "bridge durable identity marker has an invalid byte size"
            )
        anchor = self._verify_anchor_file()
        anchor_identity = getattr(self, "_anchor_identity", None)
        if anchor_identity is not None and self._file_identity(anchor) != anchor_identity:
            raise CoreBridgeStoreStateRootError(
                "bridge durable root identity anchor identity changed"
            )
        if anchor.st_size != MARKER_FILE_BYTES and not (
            not self._identity_ready and anchor.st_size == 0
        ):
            raise CoreBridgeStoreStateRootError(
                "bridge durable root identity anchor has an invalid byte size"
            )
        journal = self._optional_private_file(JOURNAL_FILENAME)
        if journal is not None and journal.st_size > MAX_JOURNAL_BYTES:
            raise CoreBridgeStoreStateRootError("bridge SQLite journal exceeds its byte bound")
        for name in (WAL_FILENAME, SHM_FILENAME):
            if self._optional_private_file(name) is not None:
                raise CoreBridgeStoreStateRootError(f"SQLite side file {name} is forbidden")
        if self._verify_private_file(DATABASE_FILENAME).st_size > MAX_DATABASE_BYTES:
            raise CoreBridgeStoreStateRootError("bridge database exceeds its byte bound")

    def _open_database_connection(self) -> sqlite3.Connection:
        self._verify_storage_files()
        try:
            if sys.platform == "darwin":
                database_target: str | Path = self.database_path
                uri = False
            elif sys.platform.startswith("linux"):
                database_target = f"file:/dev/fd/{self._database_fd}?mode=rw"
                uri = True
            else:
                raise CoreBridgeStoreStateRootError(
                    "bridge SQLite platform is unsupported"
                )
            connection = sqlite3.connect(
                database_target,
                timeout=30,
                isolation_level=None,
                check_same_thread=False,
                uri=uri,
            )
            if connection.execute("PRAGMA synchronous").fetchone() != (_SQLITE_SYNCHRONOUS_FULL,):
                raise CoreBridgeStoreStateRootError(
                    "SQLite target connection default synchronous is not FULL"
                )
            connection.row_factory = sqlite3.Row
            database_rows = connection.execute("PRAGMA database_list").fetchall()
            if len(database_rows) != 1:
                raise CoreBridgeStoreStateRootError("SQLite opened an unexpected database set")
            opened_path = cast(str, database_rows[0][2])
            if type(opened_path) is not str or not os.path.isabs(opened_path):
                raise CoreBridgeStoreStateRootError(
                    "SQLite returned an invalid bridge database path"
                )
            try:
                opened_stat = os.stat(opened_path)
            except OSError as exc:
                raise CoreBridgeStoreStateRootError(
                    "SQLite bridge database identity could not be verified"
                ) from exc
            if self._file_identity(opened_stat) != self._managed_identities[DATABASE_FILENAME]:
                raise CoreBridgeStoreStateRootError(
                    "SQLite opened an unexpected bridge database inode"
                )
            self._verify_storage_files()
            connection.execute("PRAGMA foreign_keys = ON")
            connection.execute("PRAGMA busy_timeout = 30000")
            if connection.execute("PRAGMA journal_mode = DELETE").fetchone()[0] != "delete":
                raise CoreBridgeStoreStateRootError("SQLite rollback journal mode is unavailable")
            connection.execute("PRAGMA synchronous = FULL")
            if connection.execute("PRAGMA synchronous").fetchone()[0] != (
                _SQLITE_SYNCHRONOUS_FULL
            ):
                raise CoreBridgeStoreStateRootError(
                    "SQLite full synchronous mode could not be enforced"
                )
            connection.execute("PRAGMA temp_store = MEMORY")
            connection.execute("PRAGMA trusted_schema = OFF")
            page_size = cast(int, connection.execute("PRAGMA page_size").fetchone()[0])
            if not 512 <= page_size <= 65_536:
                raise CoreBridgeStoreStateRootError("SQLite page size is outside bridge bounds")
            self._max_page_count = MAX_DATABASE_BYTES // page_size
            configured_pages = connection.execute(
                f"PRAGMA max_page_count = {self._max_page_count}"
            ).fetchone()[0]
            if configured_pages != self._max_page_count:
                raise CoreBridgeStoreStateRootError("SQLite max_page_count could not be enforced")
            configured_journal = connection.execute(
                f"PRAGMA journal_size_limit = {MAX_JOURNAL_BYTES}"
            ).fetchone()[0]
            if configured_journal != MAX_JOURNAL_BYTES:
                raise CoreBridgeStoreStateRootError(
                    "SQLite journal_size_limit could not be enforced"
                )
            self._verify_storage_files()
            return connection
        except sqlite3.DatabaseError as exc:
            if "connection" in locals():
                connection.close()
            raise CoreBridgeStoreDataCorruptionError(
                "bridge SQLite database could not be safely opened"
            ) from exc
        except BaseException:
            if "connection" in locals():
                connection.close()
            raise

    @staticmethod
    def _verify_sqlite_default_synchronous_full() -> None:
        probe: sqlite3.Connection | None = None
        try:
            probe = sqlite3.connect(":memory:", isolation_level=None)
            synchronous = probe.execute("PRAGMA synchronous").fetchone()
        except sqlite3.DatabaseError as exc:
            raise CoreBridgeStoreStateRootError(
                "SQLite library default synchronous could not be verified"
            ) from exc
        finally:
            if probe is not None:
                probe.close()
        if synchronous != (_SQLITE_SYNCHRONOUS_FULL,):
            raise CoreBridgeStoreStateRootError("SQLite library default synchronous is not FULL")

    def _is_fresh_database_after_recovery(self) -> bool:
        connection = self._connection
        try:
            page_count = cast(int, connection.execute("PRAGMA page_count").fetchone()[0])
            user_version = cast(int, connection.execute("PRAGMA user_version").fetchone()[0])
            schema_rows = cast(
                int,
                connection.execute("SELECT count(*) FROM sqlite_schema").fetchone()[0],
            )
        except sqlite3.DatabaseError as exc:
            raise CoreBridgeStoreDataCorruptionError(
                "bridge SQLite recovery state could not be read"
            ) from exc
        self._verify_storage_files()
        database_stat = os.fstat(self._database_fd)
        if self._file_identity(database_stat) != self._managed_identities[DATABASE_FILENAME]:
            raise CoreBridgeStoreStateRootError(
                "held bridge database identity changed after SQLite recovery"
            )
        database_empty = (
            database_stat.st_size == 0
            and page_count == 0
            and user_version == 0
            and schema_rows == 0
        )
        if not database_empty:
            return False
        return self._read_identity_marker() is None and self._read_root_anchor() is None

    @staticmethod
    def _validate_schema(connection: sqlite3.Connection) -> None:
        try:
            rows = _schema_rows(connection)
            digest = sha256(_canonical_json_bytes(rows)).hexdigest()
        except sqlite3.DatabaseError as exc:
            raise CoreBridgeStoreSchemaError("bridge schema could not be read") from exc
        if rows != _EXPECTED_SCHEMA_ROWS or digest != _EXPECTED_SCHEMA_DIGEST:
            raise CoreBridgeStoreSchemaError(
                "bridge schema fingerprint does not match canonical private v3"
            )
        metadata = connection.execute(
            "SELECT singleton, schema_version, schema_fingerprint FROM schema_metadata"
        ).fetchall()
        if [tuple(row) for row in metadata] != [(1, SCHEMA_VERSION, _EXPECTED_SCHEMA_DIGEST)]:
            raise CoreBridgeStoreSchemaError("bridge schema metadata does not match private v3")
        if connection.execute("PRAGMA user_version").fetchone()[0] != SCHEMA_VERSION:
            raise CoreBridgeStoreSchemaError(
                "bridge SQLite user_version does not match private v3"
            )

    @staticmethod
    def _authority_digest(connection: sqlite3.Connection) -> str:
        authority: list[tuple[object, ...]] = []
        for table, order in (
            ("create_operations", "local_project_id"),
            ("patch_operations", "local_project_id"),
            ("mappings", "local_project_id"),
            ("mapping_history", "local_project_id, mapping_generation"),
        ):
            authority.extend(
                (table, *tuple(row))
                for row in connection.execute(
                    f"SELECT local_project_id, document_sha256 FROM {table} ORDER BY {order}"
                )
            )
        return sha256(_canonical_json_bytes(authority)).hexdigest()

    @staticmethod
    def _identity_row(connection: sqlite3.Connection) -> dict[str, object]:
        probe = connection.execute(
            """
            SELECT count(*) AS row_count,
                   coalesce(sum(
                       (typeof(singleton) != 'integer') +
                       (typeof(root_device) != 'integer') +
                       (typeof(root_inode) != 'integer') +
                       (typeof(database_device) != 'integer') +
                       (typeof(database_inode) != 'integer') +
                       (typeof(marker_device) != 'integer') +
                       (typeof(marker_inode) != 'integer') +
                       (typeof(anchor_device) != 'integer') +
                       (typeof(anchor_inode) != 'integer') +
                       (typeof(lock_device) != 'integer') +
                       (typeof(lock_inode) != 'integer') +
                       (typeof(marker_generation) != 'integer') +
                       (typeof(previous_marker_generation) != 'integer')
                   ), 0) AS integer_type_errors,
                   coalesce(sum(typeof(store_id) != 'text'), 0)
                       AS store_id_type_errors,
                   coalesce(sum(length(CAST(store_id AS BLOB))), 0)
                       AS store_id_bytes,
                   coalesce(sum(typeof(authority_digest) != 'text'), 0)
                       AS authority_digest_type_errors,
                   coalesce(sum(length(CAST(authority_digest AS BLOB))), 0)
                       AS authority_digest_bytes,
                   coalesce(sum(typeof(previous_authority_digest) != 'text'), 0)
                       AS previous_authority_digest_type_errors,
                   coalesce(sum(length(CAST(previous_authority_digest AS BLOB))), 0)
                       AS previous_authority_digest_bytes,
                   coalesce(sum(typeof(binding_state) != 'text'), 0)
                       AS binding_state_type_errors,
                   coalesce(sum(length(CAST(binding_state AS BLOB))), 0)
                       AS binding_state_bytes
            FROM store_identity
            """
        ).fetchone()
        if probe is None or probe["row_count"] != 1:
            raise CoreBridgeStoreStateRootError(
                "bridge database store identity row is missing or duplicated"
            )
        if (
            probe["integer_type_errors"] != 0
            or probe["store_id_type_errors"] != 0
            or probe["store_id_bytes"] != 64
            or probe["authority_digest_type_errors"] != 0
            or probe["authority_digest_bytes"] != 64
            or probe["previous_authority_digest_type_errors"] != 0
            or probe["previous_authority_digest_bytes"] != 64
            or probe["binding_state_type_errors"] != 0
            or not 1 <= probe["binding_state_bytes"] <= 7
        ):
            raise CoreBridgeStoreStateRootError("bridge database store identity is invalid")
        selected = connection.execute(
            """
            SELECT CASE WHEN typeof(singleton) = 'integer' THEN singleton END
                       AS singleton,
                   CASE WHEN typeof(store_id) = 'text'
                                  AND length(CAST(store_id AS BLOB)) = 64
                        THEN store_id END AS store_id,
                   CASE WHEN typeof(root_device) = 'integer' THEN root_device END
                       AS root_device,
                   CASE WHEN typeof(root_inode) = 'integer' THEN root_inode END
                       AS root_inode,
                   CASE WHEN typeof(database_device) = 'integer' THEN database_device END
                       AS database_device,
                   CASE WHEN typeof(database_inode) = 'integer' THEN database_inode END
                       AS database_inode,
                   CASE WHEN typeof(marker_device) = 'integer' THEN marker_device END
                       AS marker_device,
                   CASE WHEN typeof(marker_inode) = 'integer' THEN marker_inode END
                       AS marker_inode,
                   CASE WHEN typeof(anchor_device) = 'integer' THEN anchor_device END
                       AS anchor_device,
                   CASE WHEN typeof(anchor_inode) = 'integer' THEN anchor_inode END
                       AS anchor_inode,
                   CASE WHEN typeof(lock_device) = 'integer' THEN lock_device END
                       AS lock_device,
                   CASE WHEN typeof(lock_inode) = 'integer' THEN lock_inode END
                       AS lock_inode,
                   CASE WHEN typeof(marker_generation) = 'integer'
                        THEN marker_generation END AS marker_generation,
                   CASE WHEN typeof(authority_digest) = 'text'
                                  AND length(CAST(authority_digest AS BLOB)) = 64
                        THEN authority_digest END AS authority_digest,
                   CASE WHEN typeof(previous_marker_generation) = 'integer'
                        THEN previous_marker_generation END AS previous_marker_generation,
                   CASE WHEN typeof(previous_authority_digest) = 'text'
                                  AND length(CAST(previous_authority_digest AS BLOB)) = 64
                        THEN previous_authority_digest END AS previous_authority_digest,
                   CASE WHEN typeof(binding_state) = 'text'
                                  AND length(CAST(binding_state AS BLOB)) BETWEEN 1 AND 7
                        THEN binding_state END AS binding_state
            FROM store_identity
            """
        ).fetchone()
        if selected is None:
            raise CoreBridgeStoreStateRootError(
                "bridge database store identity row is missing or duplicated"
            )
        row = dict(selected)
        text_fields = (
            "store_id",
            "authority_digest",
            "previous_authority_digest",
            "binding_state",
        )
        integer_fields = (
            "root_device",
            "root_inode",
            "database_device",
            "database_inode",
            "marker_device",
            "marker_inode",
            "anchor_device",
            "anchor_inode",
            "lock_device",
            "lock_inode",
            "marker_generation",
            "previous_marker_generation",
        )
        if (
            row.get("singleton") != 1
            or any(type(row.get(field)) is not str for field in text_fields)
            or any(type(row.get(field)) is not int for field in integer_fields)
        ):
            raise CoreBridgeStoreStateRootError("bridge database store identity is invalid")
        for field in ("store_id", "authority_digest", "previous_authority_digest"):
            _digest(row[field], label=f"store identity {field}")
        if row["binding_state"] not in ("pending", "bound"):
            raise CoreBridgeStoreStateRootError(
                "bridge database store identity binding state is invalid"
            )
        return row

    @staticmethod
    def _require_empty_pending_authority(connection: sqlite3.Connection) -> None:
        rows, byte_count = DesktopCoreBridgeStoreV1._recovery_usage(connection)
        if rows > MAX_RECOVERY_ROWS or byte_count > MAX_RECOVERY_BYTES:
            raise CoreBridgeStoreCapacityError(
                "pending bridge authority exceeds its recovery capacity"
            )
        if rows != 0 or byte_count != 0:
            raise CoreBridgeStoreStateRootError(
                "pending bridge store identity does not describe a fresh empty store"
            )

    @staticmethod
    def _marker_payload(identity: dict[str, object]) -> dict[str, object]:
        return {
            key: identity[key]
            for key in (
                "store_id",
                "root_device",
                "root_inode",
                "database_device",
                "database_inode",
                "marker_device",
                "marker_inode",
                "anchor_device",
                "anchor_inode",
                "lock_device",
                "lock_inode",
                "marker_generation",
                "authority_digest",
            )
        }

    def _read_marker_file(
        self,
        *,
        name: str,
        dir_fd: int,
        expected: os.stat_result,
        label: str,
        initial_publication: dict[str, object] | None = None,
    ) -> dict[str, object] | None:
        flags = os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW
        try:
            descriptor = os.open(name, flags, dir_fd=dir_fd)
        except OSError as exc:
            raise CoreBridgeStoreStateRootError(
                f"bridge durable {label} could not be opened"
            ) from exc
        try:
            if self._file_identity(os.fstat(descriptor)) != self._file_identity(expected):
                raise CoreBridgeStoreStateRootError(f"bridge durable {label} identity changed")
            raw = os.read(descriptor, MARKER_FILE_BYTES + 1)
        finally:
            os.close(descriptor)
        if raw == b"":
            return None
        if len(raw) != MARKER_FILE_BYTES:
            raise CoreBridgeStoreStateRootError(f"bridge durable {label} has an invalid byte size")
        records: list[dict[str, object]] = []
        payload_keys = frozenset(
            {
                "store_id",
                "root_device",
                "root_inode",
                "database_device",
                "database_inode",
                "marker_device",
                "marker_inode",
                "anchor_device",
                "anchor_inode",
                "lock_device",
                "lock_inode",
                "marker_generation",
                "authority_digest",
            }
        )
        for offset in (0, MARKER_SLOT_BYTES):
            slot = raw[offset : offset + MARKER_SLOT_BYTES].rstrip(b" \0")
            if not slot:
                continue
            try:
                envelope = json.loads(slot)
                data = _exact_object(
                    envelope,
                    frozenset({"payload", "payload_sha256"}),
                    label="identity marker envelope",
                )
                payload = _exact_object(
                    data["payload"], payload_keys, label="identity marker payload"
                )
                if _canonical_json_bytes(envelope) != slot:
                    raise ValueError("marker is not canonical")
                if sha256(_canonical_json_bytes(payload)).hexdigest() != _digest(
                    data["payload_sha256"], label="identity marker digest"
                ):
                    raise ValueError("marker digest differs")
                marker_integer_fields = payload_keys - {
                    "store_id",
                    "authority_digest",
                }
                if any(
                    type(payload[field]) is not int or cast(int, payload[field]) < 0
                    for field in marker_integer_fields
                ):
                    raise ValueError("marker integer identity is invalid")
                _digest(payload["store_id"], label="identity marker store ID")
                _digest(payload["authority_digest"], label="identity marker authority")
                records.append(payload)
            except (CoreBridgeStoreError, TypeError, ValueError, json.JSONDecodeError):
                continue
        if not records:
            if initial_publication is not None:
                if cast(int, initial_publication["marker_generation"]) != 0:
                    raise CoreBridgeStoreStateRootError(
                        f"bridge durable {label} initial publication is invalid"
                    )
                inactive_slot = raw[MARKER_SLOT_BYTES:]
                if inactive_slot == b"\0" * MARKER_SLOT_BYTES:
                    return None
            raise CoreBridgeStoreStateRootError(f"bridge durable {label} has no valid slot")
        records.sort(key=lambda item: cast(int, item["marker_generation"]))
        if len(records) == 2 and (
            records[0]["marker_generation"] == records[1]["marker_generation"]
            and records[0] != records[1]
        ):
            raise CoreBridgeStoreStateRootError(f"bridge durable {label} slots conflict")
        return records[-1]

    def _read_identity_marker(
        self, *, initial_publication: dict[str, object] | None = None
    ) -> dict[str, object] | None:
        return self._read_marker_file(
            name=IDENTITY_MARKER_FILENAME,
            dir_fd=self._root_fd,
            expected=self._verify_private_file(IDENTITY_MARKER_FILENAME),
            label="identity marker",
            initial_publication=initial_publication,
        )

    def _read_root_anchor(
        self, *, initial_publication: dict[str, object] | None = None
    ) -> dict[str, object] | None:
        return self._read_marker_file(
            name=self._anchor_name,
            dir_fd=self._anchor_parent_fd,
            expected=self._verify_anchor_file(),
            label="root identity anchor",
            initial_publication=initial_publication,
        )

    def _write_marker_file(
        self,
        identity: dict[str, object],
        *,
        name: str,
        dir_fd: int,
        expected: os.stat_result,
        sync_dir_fd: int,
        label: str,
    ) -> None:
        payload = self._marker_payload(identity)
        envelope = {
            "payload": payload,
            "payload_sha256": sha256(_canonical_json_bytes(payload)).hexdigest(),
        }
        encoded = _canonical_json_bytes(envelope)
        if len(encoded) > MARKER_SLOT_BYTES:
            raise CoreBridgeStoreCapacityError("bridge identity marker exceeds its slot bound")
        slot = encoded + b" " * (MARKER_SLOT_BYTES - len(encoded))
        flags = os.O_RDWR | os.O_CLOEXEC | os.O_NOFOLLOW
        descriptor = os.open(name, flags, dir_fd=dir_fd)
        try:
            if self._file_identity(os.fstat(descriptor)) != self._file_identity(expected):
                raise CoreBridgeStoreStateRootError(f"bridge durable {label} identity changed")
            if os.fstat(descriptor).st_size == 0:
                os.ftruncate(descriptor, MARKER_FILE_BYTES)
            offset = cast(int, identity["marker_generation"]) % 2 * MARKER_SLOT_BYTES
            if os.pwrite(descriptor, slot, offset) != len(slot):
                raise CoreBridgeStoreStateRootError(f"bridge durable {label} write was incomplete")
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        os.fsync(sync_dir_fd)

    def _write_identity_marker(self, identity: dict[str, object]) -> None:
        self._write_marker_file(
            identity,
            name=IDENTITY_MARKER_FILENAME,
            dir_fd=self._root_fd,
            expected=self._verify_private_file(IDENTITY_MARKER_FILENAME),
            sync_dir_fd=self._root_fd,
            label="identity marker",
        )

    def _write_root_anchor(self, identity: dict[str, object]) -> None:
        self._write_marker_file(
            identity,
            name=self._anchor_name,
            dir_fd=self._anchor_parent_fd,
            expected=self._verify_anchor_file(),
            sync_dir_fd=self._anchor_parent_fd,
            label="root identity anchor",
        )

    def _validate_store_binding(
        self,
        connection: sqlite3.Connection,
        *,
        recover_forward: bool,
    ) -> None:
        identity = self._identity_row(connection)
        if identity["binding_state"] != "bound":
            raise CoreBridgeStoreStateRootError(
                "bridge database store identity binding is not complete"
            )
        physical = {
            "root_device": self._root_identity[0],
            "root_inode": self._root_identity[1],
            "database_device": self._managed_identities[DATABASE_FILENAME][0],
            "database_inode": self._managed_identities[DATABASE_FILENAME][1],
            "marker_device": self._managed_identities[IDENTITY_MARKER_FILENAME][0],
            "marker_inode": self._managed_identities[IDENTITY_MARKER_FILENAME][1],
            "anchor_device": self._anchor_identity[0],
            "anchor_inode": self._anchor_identity[1],
            "lock_device": self._managed_identities[OWNER_LOCK_FILENAME][0],
            "lock_inode": self._managed_identities[OWNER_LOCK_FILENAME][1],
        }
        if any(identity[key] != value for key, value in physical.items()):
            raise CoreBridgeStoreStateRootError(
                "bridge database and durable marker physical identity binding differs"
            )
        database_generation = cast(int, identity["marker_generation"])
        records = (
            ("identity marker", self._read_identity_marker(), self._write_identity_marker),
            ("root identity anchor", self._read_root_anchor(), self._write_root_anchor),
        )
        for label, marker, writer in records:
            if marker is None:
                raise CoreBridgeStoreStateRootError(f"bridge durable {label} is empty")
            for key, value in physical.items():
                if marker[key] != value:
                    raise CoreBridgeStoreStateRootError(
                        f"bridge durable {label} physical root identity differs"
                    )
            if marker["store_id"] != identity["store_id"]:
                raise CoreBridgeStoreStateRootError(
                    f"bridge durable {label} belongs to a different store identity"
                )
            marker_generation = cast(int, marker["marker_generation"])
            if database_generation == marker_generation and (
                marker["authority_digest"] == identity["authority_digest"]
            ):
                continue
            if database_generation < marker_generation:
                raise CoreBridgeStoreStateRootError("bridge database durable rollback detected")
            if (
                recover_forward
                and database_generation == marker_generation + 1
                and identity["previous_marker_generation"] == marker_generation
                and identity["previous_authority_digest"] == marker["authority_digest"]
            ):
                writer(identity)
                continue
            raise CoreBridgeStoreStateRootError(
                f"bridge database and durable {label} authority differ"
            )

    def _complete_pending_store_binding(self, connection: sqlite3.Connection) -> None:
        identity = self._identity_row(connection)
        if identity["binding_state"] != "pending":
            return
        self._require_empty_pending_authority(connection)
        empty_authority = self._authority_digest(connection)
        if (
            identity["marker_generation"] != 0
            or identity["previous_marker_generation"] != 0
            or identity["authority_digest"] != empty_authority
            or identity["previous_authority_digest"] != empty_authority
        ):
            raise CoreBridgeStoreStateRootError(
                "pending bridge store identity does not describe a fresh empty store"
            )
        physical = {
            "root_device": self._root_identity[0],
            "root_inode": self._root_identity[1],
            "database_device": self._managed_identities[DATABASE_FILENAME][0],
            "database_inode": self._managed_identities[DATABASE_FILENAME][1],
            "marker_device": self._managed_identities[IDENTITY_MARKER_FILENAME][0],
            "marker_inode": self._managed_identities[IDENTITY_MARKER_FILENAME][1],
            "anchor_device": self._anchor_identity[0],
            "anchor_inode": self._anchor_identity[1],
            "lock_device": self._managed_identities[OWNER_LOCK_FILENAME][0],
            "lock_inode": self._managed_identities[OWNER_LOCK_FILENAME][1],
        }
        if any(identity[key] != value for key, value in physical.items()):
            raise CoreBridgeStoreStateRootError(
                "pending bridge store identity has a different physical binding"
            )
        expected_marker = self._marker_payload(identity)
        marker_operations = (
            (
                "identity marker",
                lambda: self._read_identity_marker(initial_publication=identity),
                self._write_identity_marker,
            ),
            (
                "root identity anchor",
                lambda: self._read_root_anchor(initial_publication=identity),
                self._write_root_anchor,
            ),
        )
        for label, reader, writer in marker_operations:
            marker = reader()
            if marker is None:
                writer(identity)
                marker = reader()
            if marker != expected_marker:
                raise CoreBridgeStoreStateRootError(
                    f"pending bridge durable {label} differs from store identity"
                )

        try:
            connection.execute("BEGIN EXCLUSIVE")
            if self._identity_row(connection) != identity:
                raise CoreBridgeStoreStateRootError(
                    "pending bridge store identity changed during binding"
                )
            self._require_empty_pending_authority(connection)
            if self._authority_digest(connection) != empty_authority:
                raise CoreBridgeStoreStateRootError(
                    "pending bridge authority changed during binding"
                )
            updated = connection.execute(
                """
                UPDATE store_identity
                SET binding_state = 'bound'
                WHERE singleton = 1 AND binding_state = 'pending'
                """
            )
            if updated.rowcount != 1:
                raise CoreBridgeStoreStateRootError(
                    "pending bridge store identity could not be bound"
                )
            connection.commit()
        except CoreBridgeStoreError:
            connection.rollback()
            raise
        except sqlite3.DatabaseError as exc:
            connection.rollback()
            raise CoreBridgeStoreSchemaError(
                "pending bridge store identity binding failed"
            ) from exc
        except BaseException:
            connection.rollback()
            raise

    def _initialize_schema(self, *, fresh_database: bool) -> None:
        connection = self._connection
        fresh_identity: dict[str, object] | None = None
        try:
            connection.execute("BEGIN EXCLUSIVE")
            version = connection.execute("PRAGMA user_version").fetchone()[0]
            if version == 0:
                if (
                    not fresh_database
                    or self._read_identity_marker() is not None
                    or self._read_root_anchor() is not None
                ):
                    raise CoreBridgeStoreSchemaError(
                        "unversioned or partial bridge state is not eligible for fresh creation"
                    )
                existing = {
                    row[0]
                    for row in connection.execute(
                        "SELECT name FROM sqlite_schema WHERE type = 'table'"
                    )
                    if row[0] != "sqlite_sequence"
                }
                if existing:
                    raise CoreBridgeStoreSchemaError("unversioned bridge database is not empty")
                for statement in _SCHEMA:
                    connection.execute(statement)
                authority_digest = self._authority_digest(connection)
                marker_identity = self._managed_identities[IDENTITY_MARKER_FILENAME]
                database_identity = self._managed_identities[DATABASE_FILENAME]
                fresh_identity = {
                    "singleton": 1,
                    "store_id": secrets.token_hex(32),
                    "root_device": self._root_identity[0],
                    "root_inode": self._root_identity[1],
                    "database_device": database_identity[0],
                    "database_inode": database_identity[1],
                    "marker_device": marker_identity[0],
                    "marker_inode": marker_identity[1],
                    "anchor_device": self._anchor_identity[0],
                    "anchor_inode": self._anchor_identity[1],
                    "lock_device": self._owner_lock_identity[0],
                    "lock_inode": self._owner_lock_identity[1],
                    "marker_generation": 0,
                    "authority_digest": authority_digest,
                    "previous_marker_generation": 0,
                    "previous_authority_digest": authority_digest,
                    "binding_state": "pending",
                }
                connection.execute(
                    """
                    INSERT INTO store_identity (
                        singleton, store_id, root_device, root_inode,
                        database_device, database_inode, marker_device, marker_inode,
                        anchor_device, anchor_inode, lock_device, lock_inode,
                        marker_generation, authority_digest,
                        previous_marker_generation, previous_authority_digest,
                        binding_state
                    ) VALUES (
                        :singleton, :store_id, :root_device, :root_inode,
                        :database_device, :database_inode, :marker_device, :marker_inode,
                        :anchor_device, :anchor_inode, :lock_device, :lock_inode,
                        :marker_generation, :authority_digest,
                        :previous_marker_generation, :previous_authority_digest,
                        :binding_state
                    )
                    """,
                    fresh_identity,
                )
                connection.execute(
                    """
                    INSERT INTO schema_metadata(singleton, schema_version, schema_fingerprint)
                    VALUES (1, ?, ?)
                    """,
                    (SCHEMA_VERSION, _EXPECTED_SCHEMA_DIGEST),
                )
                connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
            elif version != SCHEMA_VERSION:
                raise CoreBridgeStoreSchemaError(f"unsupported bridge schema version {version}")
            self._validate_schema(connection)
            self._verify_storage_files()
            connection.commit()
        except CoreBridgeStoreError:
            connection.rollback()
            raise
        except sqlite3.DatabaseError as exc:
            connection.rollback()
            raise CoreBridgeStoreSchemaError(
                "bridge schema initialization or validation failed"
            ) from exc
        except BaseException:
            connection.rollback()
            raise
        self._complete_pending_store_binding(connection)
        self._identity_ready = True
        self._verify_storage_files()
        self._validate_store_binding(connection, recover_forward=True)
        os.fsync(self._root_fd)

    @staticmethod
    def _recovery_usage(connection: sqlite3.Connection) -> tuple[int, int]:
        total_rows = 0
        total_bytes = 0
        for table, columns in (
            (
                "create_operations",
                ("local_project_id", "state", "document_json", "document_sha256"),
            ),
            (
                "patch_operations",
                ("local_project_id", "state", "document_json", "document_sha256"),
            ),
            (
                "mappings",
                (
                    "local_project_id",
                    "core_project_id",
                    "request_sha256",
                    "document_json",
                    "document_sha256",
                ),
            ),
            (
                "mapping_history",
                (
                    "local_project_id",
                    "core_project_id",
                    "request_sha256",
                    "document_json",
                    "document_sha256",
                ),
            ),
        ):
            expression = " + ".join(
                f"coalesce(length(CAST({name} AS BLOB)), 0)" for name in columns
            )
            count, byte_count = connection.execute(
                f"SELECT count(*), coalesce(sum({expression}), 0) FROM {table}"
            ).fetchone()
            total_rows += cast(int, count)
            total_bytes += cast(int, byte_count)
        return total_rows, total_bytes

    def _validate_capacity(self, connection: sqlite3.Connection) -> None:
        rows, byte_count = self._recovery_usage(connection)
        history_rows = connection.execute("SELECT count(*) FROM mapping_history").fetchone()[0]
        if (
            rows > MAX_RECOVERY_ROWS
            or byte_count > MAX_RECOVERY_BYTES
            or history_rows > self._max_mapping_history_rows
        ):
            raise CoreBridgeStoreCapacityError("bridge recovery capacity is exceeded")

    @staticmethod
    def _bounded_table_rows(
        connection: sqlite3.Connection,
        table: str,
        *,
        scalar_columns: tuple[str, ...],
    ) -> list[sqlite3.Row]:
        length_columns = ", ".join(
            f"length(CAST({column} AS BLOB)) AS {column}_bytes" for column in scalar_columns
        )
        probes = connection.execute(
            f"""
            SELECT rowid, {length_columns}, length(document_json) AS document_bytes,
                   length(CAST(document_sha256 AS BLOB)) AS digest_bytes
            FROM {table}
            ORDER BY rowid
            """
        ).fetchall()
        rows: list[sqlite3.Row] = []
        for probe in probes:
            if (
                type(probe["document_bytes"]) is not int
                or not 1 <= probe["document_bytes"] <= MAX_DOCUMENT_BYTES
                or probe["digest_bytes"] != 64
                or any(
                    type(probe[f"{column}_bytes"]) is not int
                    or not 1 <= probe[f"{column}_bytes"] <= MAX_IDENTITY_BYTES
                    for column in scalar_columns
                )
            ):
                raise CoreBridgeStoreDataCorruptionError(
                    f"bridge {table} row exceeds its recovery bound"
                )
            selected = connection.execute(
                f"""
                SELECT *,
                       CASE WHEN length(document_json) = ? THEN document_json END AS guarded_document,
                       CASE WHEN length(CAST(document_sha256 AS BLOB)) = 64
                            THEN document_sha256 END AS guarded_digest
                FROM {table}
                WHERE rowid = ? AND length(document_json) = ?
                """,
                (probe["document_bytes"], probe["rowid"], probe["document_bytes"]),
            ).fetchone()
            if (
                selected is None
                or selected["guarded_document"] is None
                or selected["guarded_digest"] is None
            ):
                raise CoreBridgeStoreDataCorruptionError(
                    f"bridge {table} row changed during recovery"
                )
            rows.append(selected)
        return rows

    def _recover_and_validate(self) -> None:
        connection = self._connection
        try:
            connection.execute("BEGIN EXCLUSIVE")
            self._validate_schema(connection)
            self._validate_store_binding(connection, recover_forward=True)
            integrity = connection.execute("PRAGMA integrity_check(1)").fetchall()
            if [tuple(row) for row in integrity] != [("ok",)]:
                raise CoreBridgeStoreDataCorruptionError("bridge SQLite integrity check failed")
            if connection.execute("PRAGMA foreign_key_check").fetchone() is not None:
                raise CoreBridgeStoreDataCorruptionError("bridge foreign key check failed")
            self._validate_capacity(connection)
            creates = {
                operation.local_project_id: operation
                for operation in (
                    self._create_from_row(row)
                    for row in self._bounded_table_rows(
                        connection,
                        "create_operations",
                        scalar_columns=("local_project_id", "state"),
                    )
                )
            }
            patches = {
                operation.local_project_id: operation
                for operation in (
                    self._patch_from_row(row)
                    for row in self._bounded_table_rows(
                        connection,
                        "patch_operations",
                        scalar_columns=("local_project_id", "state"),
                    )
                )
            }
            mappings = {
                mapping.local_project_id: mapping
                for mapping in (
                    self._mapping_from_row(row)
                    for row in self._bounded_table_rows(
                        connection,
                        "mappings",
                        scalar_columns=("local_project_id", "core_project_id", "request_sha256"),
                    )
                )
            }
            history: defaultdict[str, list[_MappingHistoryEntry]] = defaultdict(list)
            for row in self._bounded_table_rows(
                connection,
                "mapping_history",
                scalar_columns=("local_project_id", "core_project_id", "request_sha256"),
            ):
                entry = self._history_from_row(row)
                history[entry.mapping.local_project_id].append(entry)
            self._validate_authority_graph(creates, patches, mappings, history)
            identity = self._identity_row(connection)
            if self._authority_digest(connection) != identity["authority_digest"]:
                raise CoreBridgeStoreDataCorruptionError(
                    "bridge durable authority digest does not match canonical rows"
                )
            self._verify_storage_files()
            connection.commit()
        except CoreBridgeStoreError:
            connection.rollback()
            raise
        except sqlite3.DatabaseError as exc:
            connection.rollback()
            raise CoreBridgeStoreDataCorruptionError("bridge SQLite recovery failed") from exc
        except BaseException:
            connection.rollback()
            raise

    @staticmethod
    def _same_owner(left: Any, right: Any) -> bool:
        return (
            left.local_project_id,
            left.profile_id,
            left.core_host_identity,
            left.core_project_id,
        ) == (
            right.local_project_id,
            right.profile_id,
            right.core_host_identity,
            right.core_project_id,
        )

    @classmethod
    def _validate_mapping_owner(
        cls,
        operation: CoreProjectCreateOperationV1,
        mapping: CoreProjectMappingV1,
    ) -> None:
        create_authority = operation.project_immutable_authority
        mapping_authority = mapping.immutable_authority
        if (
            operation.state is not CoreProjectCreateStateV1.BOUND
            or operation.local_project_id != mapping.local_project_id
            or operation.profile_id != mapping.profile_id
            or operation.core_host_identity != mapping.core_host_identity
            or operation.core_project_id != mapping.core_project_id
            or create_authority is None
            or create_authority.project_id != mapping.core_project_id
            or mapping_authority.project_id != mapping.core_project_id
            or create_authority.created_at != mapping_authority.created_at
        ):
            raise CoreBridgeStoreContractError(
                "mapping authority does not match the bound create operation"
            )
        expected_mapping_authority = replace(
            create_authority,
            project_create=mapping.project_create,
            task_snapshot=mapping.task_snapshot,
        )
        if mapping_authority != expected_mapping_authority:
            raise CoreBridgeStoreContractError(
                "mapping immutable authority does not descend from project creation"
            )

    @classmethod
    def _validate_authority_graph(
        cls,
        creates: dict[str, CoreProjectCreateOperationV1],
        patches: dict[str, CoreProjectPatchOperationV1],
        mappings: dict[str, CoreProjectMappingV1],
        history: dict[str, list[_MappingHistoryEntry]],
    ) -> None:
        if set(history) != set(mappings):
            raise CoreBridgeStoreDataCorruptionError(
                "mapping history projects differ from current mappings"
            )
        try:
            for project_id, mapping in mappings.items():
                operation = creates[project_id]
                cls._validate_mapping_owner(operation, mapping)
                ordered = sorted(
                    history[project_id], key=lambda item: item.mapping.mapping_generation
                )
                if not ordered or ordered[-1].mapping != mapping:
                    raise CoreBridgeStoreDataCorruptionError(
                        "current mapping is not the latest exact history row"
                    )
                cls._validate_history_authority_reuse(tuple(entry.mapping for entry in ordered))
                previous: CoreProjectMappingV1 | None = None
                for index, entry in enumerate(ordered, start=1):
                    item = entry.mapping
                    if item.mapping_generation != index or not cls._same_owner(item, mapping):
                        raise CoreBridgeStoreDataCorruptionError(
                            "mapping history is not a contiguous owner-bound sequence"
                        )
                    if index == 1:
                        if item.predecessor_request_sha256 is not None:
                            raise CoreBridgeStoreDataCorruptionError(
                                "first mapping history row has a predecessor"
                            )
                    elif (
                        item.predecessor_request_sha256
                        != ordered[index - 2].mapping.request_sha256
                    ):
                        raise CoreBridgeStoreDataCorruptionError(
                            "mapping history predecessor digest is not exact"
                        )
                    cls._validate_mapping_transition(
                        entry.create_operation,
                        item,
                        previous,
                        entry.completed_patch,
                        entry.project_head_successor,
                    )
                    previous = item
            for project_id, patch in patches.items():
                operation = creates[project_id]
                if (
                    operation.state is not CoreProjectCreateStateV1.BOUND
                    or patch.profile_id != operation.profile_id
                    or patch.core_host_identity != operation.core_host_identity
                    or patch.core_project_id != operation.core_project_id
                ):
                    raise CoreBridgeStoreDataCorruptionError(
                        "pending patch does not match bound create authority"
                    )
                mapping = mappings.get(project_id)
                expected_old = (
                    operation.request_sha256 if mapping is None else mapping.request_sha256
                )
                if patch.old_request_sha256 != expected_old:
                    raise CoreBridgeStoreDataCorruptionError(
                        "pending patch old intent does not match durable authority"
                    )
                if patch.state is CoreProjectPatchStateV1.APPLIED:
                    cls._validate_applied_patch_snapshots(patch)
        except (KeyError, CoreBridgeStoreContractError) as exc:
            raise CoreBridgeStoreDataCorruptionError(
                "bridge authority graph references missing or inconsistent state"
            ) from exc

    @contextmanager
    def _transaction(self, *, write: bool) -> Iterator[sqlite3.Connection]:
        self._check_owner_pid()
        with self._transaction_lock:
            self._verify_storage_files()
            connection = self._connection
            committed = False
            changed = False
            next_identity: dict[str, object] | None = None
            try:
                connection.execute("BEGIN IMMEDIATE" if write else "BEGIN")
                self._validate_schema(connection)
                self._validate_store_binding(connection, recover_forward=False)
                changes_before = connection.total_changes
                yield connection
                if write:
                    self._validate_capacity(connection)
                    changed = connection.total_changes != changes_before
                    if changed:
                        current_identity = self._identity_row(connection)
                        next_generation = cast(int, current_identity["marker_generation"]) + 1
                        next_digest = self._authority_digest(connection)
                        connection.execute(
                            """
                            UPDATE store_identity
                            SET previous_marker_generation = marker_generation,
                                previous_authority_digest = authority_digest,
                                marker_generation = ?, authority_digest = ?
                            WHERE singleton = 1 AND marker_generation = ?
                              AND authority_digest = ?
                            """,
                            (
                                next_generation,
                                next_digest,
                                current_identity["marker_generation"],
                                current_identity["authority_digest"],
                            ),
                        )
                        next_identity = self._identity_row(connection)
                self._verify_storage_files()
                connection.commit()
                committed = True
            except CoreBridgeStoreError:
                if not committed:
                    connection.rollback()
                raise
            except sqlite3.DatabaseError as exc:
                if not committed:
                    connection.rollback()
                raise CoreBridgeStoreDataCorruptionError(
                    "bridge SQLite transaction failed"
                ) from exc
            except BaseException:
                if not committed:
                    connection.rollback()
                raise
            if changed:
                assert next_identity is not None
                self._write_identity_marker(next_identity)
                self._write_root_anchor(next_identity)
            self._verify_storage_files()
            self._validate_store_binding(connection, recover_forward=False)

    @staticmethod
    def _validate_project_id(value: str) -> str:
        return _bounded_text(value, label="Local project ID")

    @staticmethod
    def _create_from_row(row: sqlite3.Row) -> CoreProjectCreateOperationV1:
        operation = _decode_document(
            row["guarded_document"],
            row["guarded_digest"],
            label="create operation",
            decoder=_create_from_value,
            encoder=_create_value,
        )
        if (
            operation.local_project_id != row["local_project_id"]
            or operation.state.value != row["state"]
        ):
            raise CoreBridgeStoreDataCorruptionError(
                "create operation indexed fields differ from its document"
            )
        return cast(CoreProjectCreateOperationV1, operation)

    @staticmethod
    def _patch_from_row(row: sqlite3.Row) -> CoreProjectPatchOperationV1:
        operation = _decode_document(
            row["guarded_document"],
            row["guarded_digest"],
            label="patch operation",
            decoder=_patch_from_value,
            encoder=_patch_value,
        )
        if (
            operation.local_project_id != row["local_project_id"]
            or operation.state.value != row["state"]
        ):
            raise CoreBridgeStoreDataCorruptionError(
                "patch operation indexed fields differ from its document"
            )
        return cast(CoreProjectPatchOperationV1, operation)

    @staticmethod
    def _mapping_from_row(row: sqlite3.Row) -> CoreProjectMappingV1:
        mapping = _decode_document(
            row["guarded_document"],
            row["guarded_digest"],
            label="project mapping",
            decoder=_mapping_from_value,
            encoder=_mapping_value,
        )
        if (
            mapping.local_project_id != row["local_project_id"]
            or mapping.core_project_id != row["core_project_id"]
            or mapping.request_sha256 != row["request_sha256"]
            or mapping.mapping_generation != row["mapping_generation"]
        ):
            raise CoreBridgeStoreDataCorruptionError(
                "mapping indexed fields differ from its document"
            )
        return cast(CoreProjectMappingV1, mapping)

    @staticmethod
    def _history_from_row(row: sqlite3.Row) -> _MappingHistoryEntry:
        entry = _decode_document(
            row["guarded_document"],
            row["guarded_digest"],
            label="mapping transition history",
            decoder=_history_from_value,
            encoder=_history_value,
        )
        mapping = entry.mapping
        if (
            mapping.local_project_id != row["local_project_id"]
            or mapping.core_project_id != row["core_project_id"]
            or mapping.request_sha256 != row["request_sha256"]
            or mapping.mapping_generation != row["mapping_generation"]
        ):
            raise CoreBridgeStoreDataCorruptionError(
                "mapping history indexed fields differ from its transition proof"
            )
        return cast(_MappingHistoryEntry, entry)

    @staticmethod
    def _single_document_row(
        connection: sqlite3.Connection,
        table: str,
        local_project_id: str,
    ) -> sqlite3.Row | None:
        probe = connection.execute(
            f"""
            SELECT length(document_json) AS document_bytes,
                   length(CAST(document_sha256 AS BLOB)) AS digest_bytes
            FROM {table}
            WHERE local_project_id = ?
            """,
            (local_project_id,),
        ).fetchone()
        if probe is None:
            return None
        if (
            type(probe["document_bytes"]) is not int
            or not 1 <= probe["document_bytes"] <= MAX_DOCUMENT_BYTES
            or probe["digest_bytes"] != 64
        ):
            raise CoreBridgeStoreDataCorruptionError(
                f"bridge {table} row exceeds its document bound"
            )
        row = connection.execute(
            f"""
            SELECT *,
                   CASE WHEN length(document_json) = ? THEN document_json END
                       AS guarded_document,
                   CASE WHEN length(CAST(document_sha256 AS BLOB)) = 64
                        THEN document_sha256 END AS guarded_digest
            FROM {table}
            WHERE local_project_id = ? AND length(document_json) = ?
            """,
            (probe["document_bytes"], local_project_id, probe["document_bytes"]),
        ).fetchone()
        if row is None or row["guarded_document"] is None or row["guarded_digest"] is None:
            raise CoreBridgeStoreDataCorruptionError(
                f"bridge {table} row changed during guarded read"
            )
        return row

    def _load_create_conn(
        self, connection: sqlite3.Connection, local_project_id: str
    ) -> CoreProjectCreateOperationV1 | None:
        row = self._single_document_row(connection, "create_operations", local_project_id)
        return None if row is None else self._create_from_row(row)

    def _load_patch_conn(
        self, connection: sqlite3.Connection, local_project_id: str
    ) -> CoreProjectPatchOperationV1 | None:
        row = self._single_document_row(connection, "patch_operations", local_project_id)
        return None if row is None else self._patch_from_row(row)

    def _load_mapping_conn(
        self, connection: sqlite3.Connection, local_project_id: str
    ) -> CoreProjectMappingV1 | None:
        row = self._single_document_row(connection, "mappings", local_project_id)
        return None if row is None else self._mapping_from_row(row)

    def load_create(self, local_project_id: str) -> CoreProjectCreateOperationV1 | None:
        local_project_id = self._validate_project_id(local_project_id)
        with self._transaction(write=False) as connection:
            return self._load_create_conn(connection, local_project_id)

    def load_patch(self, local_project_id: str) -> CoreProjectPatchOperationV1 | None:
        local_project_id = self._validate_project_id(local_project_id)
        with self._transaction(write=False) as connection:
            return self._load_patch_conn(connection, local_project_id)

    def load_mapping(self, local_project_id: str) -> CoreProjectMappingV1 | None:
        local_project_id = self._validate_project_id(local_project_id)
        with self._transaction(write=False) as connection:
            return self._load_mapping_conn(connection, local_project_id)

    def load_mapping_history(self, local_project_id: str) -> tuple[CoreProjectMappingV1, ...]:
        local_project_id = self._validate_project_id(local_project_id)
        with self._transaction(write=False) as connection:
            rows = self._bounded_table_rows(
                connection,
                "mapping_history",
                scalar_columns=(
                    "local_project_id",
                    "core_project_id",
                    "request_sha256",
                ),
            )
            selected = tuple(
                self._history_from_row(row).mapping
                for row in rows
                if row["local_project_id"] == local_project_id
            )
            if len(selected) > self._max_mapping_history_rows:
                raise CoreBridgeStoreCapacityError("mapping history exceeds its row bound")
            return tuple(sorted(selected, key=lambda item: item.mapping_generation))

    @staticmethod
    def _write_create(
        connection: sqlite3.Connection,
        operation: CoreProjectCreateOperationV1,
        *,
        expected: CoreProjectCreateOperationV1,
    ) -> None:
        raw, digest = _encoded(_create_value(operation))
        expected_raw, _ = _encoded(_create_value(expected))
        cursor = connection.execute(
            """
            UPDATE create_operations
            SET state = ?, document_json = ?, document_sha256 = ?
            WHERE local_project_id = ? AND document_json = ?
            """,
            (operation.state.value, raw, digest, operation.local_project_id, expected_raw),
        )
        if cursor.rowcount != 1:
            raise CoreBridgeStoreConflictError("create operation compare-and-swap failed")

    def reserve_create(
        self, operation: CoreProjectCreateOperationV1
    ) -> CoreProjectCreateOperationV1:
        raw, digest = _encoded(_create_value(operation))
        if operation.state is not CoreProjectCreateStateV1.PRE_CREATE:
            raise CoreBridgeStoreContractError("new create reservation must be pre_create")
        with self._transaction(write=True) as connection:
            current = self._load_create_conn(connection, operation.local_project_id)
            if current is None:
                connection.execute(
                    """
                    INSERT INTO create_operations(
                        local_project_id, state, document_json, document_sha256
                    ) VALUES (?, ?, ?, ?)
                    """,
                    (operation.local_project_id, operation.state.value, raw, digest),
                )
                return operation
            if (
                current.local_project_id != operation.local_project_id
                or current.profile_id != operation.profile_id
                or current.core_host_identity != operation.core_host_identity
            ):
                raise CoreBridgeStoreConflictError(
                    "create reservation conflicts with durable project authority"
                )
            if current.state is CoreProjectCreateStateV1.BOUND:
                return current
            if (
                current.request_sha256 != operation.request_sha256
                or current.project_create != operation.project_create
            ):
                raise CoreBridgeStoreConflictError(
                    "create reservation conflicts with durable project intent"
                )
            if current.state is CoreProjectCreateStateV1.PRE_CREATE:
                self._write_create(connection, operation, expected=current)
                return operation
            return current

    def mark_create_unknown(
        self, operation: CoreProjectCreateOperationV1
    ) -> CoreProjectCreateOperationV1:
        _create_value(operation)
        if operation.state is not CoreProjectCreateStateV1.PRE_CREATE:
            raise CoreBridgeStoreContractError("only pre_create may transition to unknown")
        updated = replace(operation, state=CoreProjectCreateStateV1.UNKNOWN)
        with self._transaction(write=True) as connection:
            self._write_create(connection, updated, expected=operation)
        return updated

    def bind_created_project(
        self,
        operation: CoreProjectCreateOperationV1,
        core_project_id: str,
        *,
        immutable_authority: CoreProjectPatchImmutableAuthorityV1,
    ) -> CoreProjectCreateOperationV1:
        _create_value(operation)
        core_project_id = _bounded_text(core_project_id, label="Core project ID")
        _immutable_value(immutable_authority)
        if operation.state is not CoreProjectCreateStateV1.UNKNOWN:
            raise CoreBridgeStoreContractError("only unknown create may become bound")
        if (
            immutable_authority.project_id != core_project_id
            or immutable_authority.project_create != operation.project_create
        ):
            raise CoreBridgeStoreContractError(
                "create immutable authority does not match the created project"
            )
        updated = replace(
            operation,
            state=CoreProjectCreateStateV1.BOUND,
            core_project_id=core_project_id,
            project_immutable_authority=immutable_authority,
        )
        with self._transaction(write=True) as connection:
            self._write_create(connection, updated, expected=operation)
        return updated

    @staticmethod
    def _create_immutable_identity(
        value: CoreProjectCreateOperationV1,
    ) -> tuple[object, ...]:
        return (
            value.local_project_id,
            value.profile_id,
            value.core_host_identity,
            value.request_sha256,
            value.project_create,
            value.idempotency_key,
            value.state,
            value.core_project_id,
            value.project_immutable_authority,
        )

    def update_create(
        self,
        operation: CoreProjectCreateOperationV1,
        *,
        expected_previous: CoreProjectCreateOperationV1,
    ) -> CoreProjectCreateOperationV1:
        _create_value(operation)
        _create_value(expected_previous)
        if (
            operation.state is not CoreProjectCreateStateV1.BOUND
            or self._create_immutable_identity(operation)
            != self._create_immutable_identity(expected_previous)
        ):
            raise CoreBridgeStoreContractError(
                "create update may only change bound workspace authority"
            )
        with self._transaction(write=True) as connection:
            self._write_create(connection, operation, expected=expected_previous)
        return operation

    @staticmethod
    def _write_patch(
        connection: sqlite3.Connection,
        operation: CoreProjectPatchOperationV1,
        *,
        expected: CoreProjectPatchOperationV1,
    ) -> None:
        raw, digest = _encoded(_patch_value(operation))
        expected_raw, _ = _encoded(_patch_value(expected))
        cursor = connection.execute(
            """
            UPDATE patch_operations
            SET state = ?, document_json = ?, document_sha256 = ?
            WHERE local_project_id = ? AND document_json = ?
            """,
            (operation.state.value, raw, digest, operation.local_project_id, expected_raw),
        )
        if cursor.rowcount != 1:
            raise CoreBridgeStoreConflictError("patch operation compare-and-swap failed")

    def reserve_patch(self, operation: CoreProjectPatchOperationV1) -> CoreProjectPatchOperationV1:
        raw, digest = _encoded(_patch_value(operation))
        if operation.state is not CoreProjectPatchStateV1.PRE_PATCH:
            raise CoreBridgeStoreContractError("new patch reservation must be pre_patch")
        with self._transaction(write=True) as connection:
            create = self._load_create_conn(connection, operation.local_project_id)
            if create is None or create.state is not CoreProjectCreateStateV1.BOUND:
                raise CoreBridgeStoreConflictError("patch requires a bound create operation")
            current = self._load_patch_conn(connection, operation.local_project_id)
            if current is not None:
                return current
            mapping = self._load_mapping_conn(connection, operation.local_project_id)
            expected_old = create.request_sha256 if mapping is None else mapping.request_sha256
            if (
                operation.profile_id != create.profile_id
                or operation.core_host_identity != create.core_host_identity
                or operation.core_project_id != create.core_project_id
                or operation.old_request_sha256 != expected_old
            ):
                raise CoreBridgeStoreConflictError(
                    "patch reservation conflicts with durable project authority"
                )
            connection.execute(
                """
                INSERT INTO patch_operations(
                    local_project_id, state, document_json, document_sha256
                ) VALUES (?, ?, ?, ?)
                """,
                (operation.local_project_id, operation.state.value, raw, digest),
            )
            return operation

    def mark_patch_unknown(
        self, operation: CoreProjectPatchOperationV1
    ) -> CoreProjectPatchOperationV1:
        _patch_value(operation)
        if operation.state is not CoreProjectPatchStateV1.PRE_PATCH:
            raise CoreBridgeStoreContractError("only pre_patch may transition to unknown")
        updated = replace(operation, state=CoreProjectPatchStateV1.UNKNOWN)
        with self._transaction(write=True) as connection:
            self._write_patch(connection, updated, expected=operation)
        return updated

    def record_patch_applied(
        self,
        operation: CoreProjectPatchOperationV1,
        outcome: core_v1.ProjectV1,
        *,
        outcome_immutable: CoreProjectPatchImmutableAuthorityV1,
        outcome_mutable: CoreProjectPatchMutableAuthorityV1,
    ) -> CoreProjectPatchOperationV1:
        _patch_value(operation)
        if operation.state is not CoreProjectPatchStateV1.UNKNOWN:
            raise CoreBridgeStoreContractError("only unknown patch may become applied")
        if type(outcome) is not core_v1.ProjectV1:
            raise CoreBridgeStoreContractError("patch outcome has the wrong type")
        updated = replace(
            operation,
            state=CoreProjectPatchStateV1.APPLIED,
            outcome=outcome,
            outcome_immutable=outcome_immutable,
            outcome_mutable=outcome_mutable,
        )
        _patch_value(updated)
        self._validate_applied_patch_snapshots(updated)
        with self._transaction(write=True) as connection:
            self._write_patch(connection, updated, expected=operation)
        return updated

    @staticmethod
    def _project_create_from_project(project: core_v1.ProjectV1) -> core_v1.ProjectCreateV1:
        return core_v1.ProjectCreateV1(
            name=project.name,
            description=project.description,
            spec=project.spec,
            task=project.task,
            workspace=project.workspace,
        )

    @classmethod
    def _first_mapping_predecessor_project(
        cls,
        operation: CoreProjectCreateOperationV1,
        completed_patch: CoreProjectPatchOperationV1 | None,
    ) -> core_v1.ProjectV1 | None:
        if completed_patch is not None:
            return _completed_patch_project_authority(
                completed_patch,
                operation.workspace_upload_finalize,
            )
        finalize = operation.workspace_upload_finalize
        if (
            finalize is not None
            and finalize.state is CoreWorkspaceUploadFinalizeStateV1.APPLIED
            and finalize.outcome is not None
            and cls._project_create_from_project(finalize.outcome.project)
            == operation.project_create
        ):
            return finalize.outcome.project
        return None

    @classmethod
    def _completed_patch_successor_predecessor(
        cls,
        operation: CoreProjectCreateOperationV1,
        completed_patch: CoreProjectPatchOperationV1,
        current: CoreProjectMappingV1,
    ) -> tuple[core_v1.ProjectV1, bool] | None:
        outcome = completed_patch.outcome
        if outcome is None:
            raise CoreBridgeStoreContractError(
                "completed patch successor authority has no outcome"
            )
        latest = _completed_patch_project_authority(
            completed_patch,
            operation.workspace_upload_finalize,
        )
        assert latest is not None
        transitions: list[tuple[core_v1.ProjectV1, bool]] = []
        base_revision = completed_patch.base_project.active_revision
        outcome_revision = (
            outcome.active_revision
            if outcome.active_revision is not None
            else base_revision
        )
        latest_revision = latest.active_revision or outcome_revision
        if base_revision != outcome_revision:
            valid_initial_revision = base_revision is None and (
                outcome_revision is None
                or (
                    outcome_revision.project_id == completed_patch.core_project_id
                    and outcome_revision.generation == 0
                )
            )
            if not valid_initial_revision and (
                base_revision is None
                or outcome_revision is None
                or not cls._revision_is_same_or_successor(
                    base_revision,
                    outcome_revision,
                )
            ):
                raise CoreBridgeStoreContractError(
                    "completed patch revision authority is not adjacent"
                )
            if base_revision is not None:
                transitions.append((completed_patch.base_project, True))
        if outcome_revision != latest_revision:
            valid_initial_revision = outcome_revision is None and (
                latest_revision is None
                or (
                    latest_revision.project_id == completed_patch.core_project_id
                    and latest_revision.generation == 0
                )
            )
            if not valid_initial_revision and (
                outcome_revision is None
                or latest_revision is None
                or not cls._revision_is_same_or_successor(
                    outcome_revision,
                    latest_revision,
                )
            ):
                raise CoreBridgeStoreContractError(
                    "workspace finalize revision authority is not adjacent"
                )
            if outcome_revision is not None:
                transitions.append(
                    (
                        outcome
                        if outcome.active_revision is not None
                        else completed_patch.base_project,
                        True,
                    )
                )
        if latest_revision != current.active_revision:
            if (
                latest_revision is None
                or not cls._revision_is_same_or_successor(
                    latest_revision,
                    current.active_revision,
                )
                or latest.active_revision is None
            ):
                raise CoreBridgeStoreContractError(
                    "completed patch mapping revision authority is not adjacent"
                )
            transitions.append((latest, False))
        if len(transitions) > 1:
            raise CoreBridgeStoreContractError(
                "completed patch mapping requires multiple successor proofs"
            )
        return None if not transitions else transitions[0]

    @classmethod
    def _first_unpatched_successor_predecessor(
        cls,
        operation: CoreProjectCreateOperationV1,
        current: CoreProjectMappingV1,
    ) -> core_v1.ProjectV1 | None:
        predecessor = cls._first_mapping_predecessor_project(operation, None)
        if predecessor is None:
            if current.active_revision.generation != 0:
                raise CoreBridgeStoreContractError(
                    "first mapping is not bound to a genesis revision"
                )
            return None
        predecessor_revision = predecessor.active_revision
        if predecessor_revision is None or predecessor_revision.generation != 0:
            raise CoreBridgeStoreContractError(
                "initial workspace publication is not a genesis revision"
            )
        if current.active_revision == predecessor_revision:
            return None
        if not cls._revision_is_same_or_successor(
            predecessor_revision,
            current.active_revision,
        ):
            raise CoreBridgeStoreContractError(
                "initial workspace mapping revision authority is not adjacent"
            )
        return predecessor

    @classmethod
    def _first_mapping_successor_predecessor(
        cls,
        operation: CoreProjectCreateOperationV1,
        completed_patch: CoreProjectPatchOperationV1 | None,
        current: CoreProjectMappingV1,
    ) -> tuple[core_v1.ProjectV1, bool] | None:
        completed_successor = (
            None
            if completed_patch is None
            else cls._completed_patch_successor_predecessor(
                operation,
                completed_patch,
                current,
            )
        )
        initial_finalize_predecessor = cls._first_mapping_predecessor_project(
            operation,
            None,
        )
        initial_predecessor = (
            cls._first_unpatched_successor_predecessor(operation, current)
            if initial_finalize_predecessor is not None or completed_successor is None
            else None
        )
        if initial_predecessor is None:
            return completed_successor
        if completed_successor is None:
            return initial_predecessor, False
        completed_predecessor, completed_action_mutation = completed_successor
        if initial_predecessor.active_revision != completed_predecessor.active_revision:
            raise CoreBridgeStoreContractError(
                "first mapping requires multiple independent successor proofs"
            )
        return (
            initial_predecessor,
            completed_action_mutation
            or completed_patch is not None
            and completed_patch.base_project == initial_predecessor,
        )

    @classmethod
    def _validate_mapping_transition(
        cls,
        operation: CoreProjectCreateOperationV1,
        mapping: CoreProjectMappingV1,
        expected_previous: CoreProjectMappingV1 | None,
        completed_patch: CoreProjectPatchOperationV1 | None,
        project_head_successor: _ProjectHeadSuccessorHistoryAuthority | None,
    ) -> None:
        cls._validate_mapping_owner(operation, mapping)
        expected_generation = (
            1 if expected_previous is None else expected_previous.mapping_generation + 1
        )
        expected_predecessor = (
            None if expected_previous is None else expected_previous.request_sha256
        )
        if (
            mapping.mapping_generation != expected_generation
            or mapping.predecessor_request_sha256 != expected_predecessor
        ):
            raise CoreBridgeStoreContractError(
                "mapping generation or predecessor is not the exact successor"
            )
        completed_successor = (
            cls._completed_patch_successor_predecessor(
                operation,
                completed_patch,
                mapping,
            )
            if completed_patch is not None
            else None
        )
        completed_predecessor_project = (
            None if completed_successor is None else completed_successor[0]
        )
        completed_action_mutation = bool(
            completed_successor is not None and completed_successor[1]
        )
        first_mapping_successor = (
            cls._first_mapping_successor_predecessor(
                operation,
                completed_patch,
                mapping,
            )
            if expected_previous is None
            else None
        )
        first_mapping_predecessor = (
            None if first_mapping_successor is None else first_mapping_successor[0]
        )
        if expected_previous is None and first_mapping_successor is not None:
            completed_action_mutation = first_mapping_successor[1]
        required_project_predecessor = (
            first_mapping_predecessor
            if expected_previous is None
            else completed_predecessor_project
        )
        if (
            (completed_patch is not None or expected_previous is None)
            and (
            (required_project_predecessor is None)
            != (project_head_successor is None)
            )
        ):
            raise CoreBridgeStoreContractError(
                "completed patch verified project-head successor proof presence is invalid"
            )
        if expected_previous is None:
            if mapping.request_sha256 != operation.request_sha256:
                if completed_patch is None:
                    raise CoreBridgeStoreContractError(
                        "first changed mapping requires an applied patch"
                    )
        elif not cls._same_owner(expected_previous, mapping):
            raise CoreBridgeStoreContractError("mapping successor rewrites project ownership")
        if mapping.request_sha256 != (
            operation.request_sha256
            if completed_patch is None
            else completed_patch.new_request_sha256
        ):
            if (
                expected_previous is None
                or mapping.request_sha256 != expected_previous.request_sha256
            ):
                raise CoreBridgeStoreContractError(
                    "mapping request does not match create or completed patch authority"
                )
        if completed_patch is not None and (
            completed_patch.state is not CoreProjectPatchStateV1.APPLIED
            or completed_patch.local_project_id != mapping.local_project_id
            or completed_patch.profile_id != mapping.profile_id
            or completed_patch.core_host_identity != mapping.core_host_identity
            or completed_patch.core_project_id != mapping.core_project_id
            or completed_patch.new_request_sha256 != mapping.request_sha256
            or completed_patch.new_project_create != mapping.project_create
        ):
            raise CoreBridgeStoreContractError(
                "completed patch does not authorize the mapping successor"
            )
        if completed_patch is not None:
            cls._validate_applied_patch_snapshots(completed_patch)
        finalize = operation.workspace_upload_finalize
        if (
            expected_previous is None
            and completed_patch is None
            and finalize is not None
            and (
                finalize.state is not CoreWorkspaceUploadFinalizeStateV1.APPLIED
                or finalize.outcome is None
                or (
                    not cls._project_authorizes_mapping(finalize.outcome.project, mapping)
                    and project_head_successor is None
                )
            )
        ):
            raise CoreBridgeStoreContractError(
                "first imported mapping is not bound to the applied finalize outcome"
            )
        if expected_previous is not None:
            completed_patch_project = completed_predecessor_project
            completed_patch_latest_project = (
                None
                if completed_patch is None
                else _completed_patch_project_authority(completed_patch, finalize)
            )
            cls._validate_mapping_monotonicity(
                expected_previous,
                mapping,
                completed_patch=completed_patch,
                completed_patch_project=completed_patch_project,
                completed_patch_latest_project=completed_patch_latest_project,
                completed_action_mutation=completed_action_mutation,
                project_head_successor=project_head_successor,
            )
        elif project_head_successor is not None:
            assert required_project_predecessor is not None
            cls._validate_project_head_successor_authority(
                None,
                mapping,
                project_head_successor,
                completed_patch=completed_patch,
                completed_patch_project=required_project_predecessor,
                completed_patch_latest_project=(
                    cls._first_mapping_predecessor_project(operation, None)
                    if completed_patch is None
                    else _completed_patch_project_authority(
                        completed_patch,
                        finalize,
                    )
                ),
                completed_action_mutation=completed_action_mutation,
            )
        if completed_patch is not None:
            assert completed_patch.outcome is not None
            authorities = [completed_patch.outcome]
            finalized_project = _completed_patch_project_authority(
                completed_patch,
                finalize,
            )
            if finalized_project is not None and finalized_project != completed_patch.outcome:
                authorities.append(finalized_project)
            patch_authorizes_mapping = any(
                cls._project_authorizes_mapping(authority, mapping) for authority in authorities
            )
            if not patch_authorizes_mapping and project_head_successor is None:
                raise CoreBridgeStoreContractError(
                    "mapping successor is not bound to the applied patch outcome"
                )

    @staticmethod
    def _revision_is_same_or_successor(
        previous: core_v1.RevisionRefV1,
        current: core_v1.RevisionRefV1,
    ) -> bool:
        return current == previous or (
            current.project_id == previous.project_id
            and current.generation == previous.generation + 1
            and current.id != previous.id
        )

    @classmethod
    def _has_project_head_successor_shape(
        cls,
        previous: CoreProjectMappingV1,
        current: CoreProjectMappingV1,
    ) -> bool:
        previous_mutable = previous.mutable_authority
        current_mutable = current.mutable_authority
        if (
            previous.request_sha256 != current.request_sha256
            or previous.project_create != current.project_create
            or previous.immutable_authority != current.immutable_authority
            or current.active_revision == previous.active_revision
            or not cls._revision_is_same_or_successor(
                previous.active_revision,
                current.active_revision,
            )
        ):
            return False
        expected_mutable = replace(
            previous_mutable,
            project_snapshot=current_mutable.project_snapshot,
            workspace_snapshot=current_mutable.workspace_snapshot,
            active_revision=current_mutable.active_revision,
            registry_digest=current_mutable.registry_digest,
            updated_at=current_mutable.updated_at,
            etag=current_mutable.etag,
        )
        return bool(
            current_mutable == expected_mutable
            and current.project_etag != previous.project_etag
            and cls._timestamp(current.project_updated_at)
            > cls._timestamp(previous.project_updated_at)
        )

    @classmethod
    def _validate_project_head_successor_authority(
        cls,
        previous: CoreProjectMappingV1 | None,
        current: CoreProjectMappingV1,
        authority: _ProjectHeadSuccessorHistoryAuthority,
        *,
        completed_patch: CoreProjectPatchOperationV1 | None,
        completed_patch_project: core_v1.ProjectV1 | None,
        completed_patch_latest_project: core_v1.ProjectV1 | None,
        completed_action_mutation: bool,
    ) -> None:
        predecessor_project = completed_patch_project
        if previous is not None:
            if (
                authority.predecessor_mapping_sha256 != cls._mapping_digest(previous)
                or authority.predecessor_project_sha256 is not None
            ):
                raise CoreBridgeStoreContractError(
                    "project-head successor predecessor mapping digest is invalid"
                )
            predecessor_revision = previous.active_revision
        else:
            if predecessor_project is None:
                raise CoreBridgeStoreContractError(
                    "first mapping successor lacks predecessor project authority"
                )
            predecessor_project_sha256 = sha256(
                _canonical_json_bytes(_model_value(predecessor_project))
            ).hexdigest()
            if (
                authority.predecessor_mapping_sha256 is not None
                or authority.predecessor_project_sha256
                != predecessor_project_sha256
            ):
                raise CoreBridgeStoreContractError(
                    "project-head successor predecessor project digest is invalid"
                )
            predecessor_revision = predecessor_project.active_revision
        if predecessor_revision is None:
            raise CoreBridgeStoreContractError(
                "project-head successor predecessor has no active revision"
            )
        if completed_patch is None and previous is not None:
            valid_successor_shape = cls._has_project_head_successor_shape(previous, current)
        else:
            current_mutable = current.mutable_authority
            assert predecessor_project is not None
            predecessor_mutable = CoreProjectPatchMutableAuthorityV1(
                status=predecessor_project.status,
                project_snapshot=predecessor_project.current_project_snapshot,
                workspace_snapshot=predecessor_project.current_workspace_snapshot,
                workspace_publication=predecessor_project.workspace_publication,
                active_revision=predecessor_project.active_revision,
                registry_digest=predecessor_project.registry_digest,
                model_preparation=predecessor_project.model_preparation,
                updated_at=predecessor_project.updated_at,
                etag=predecessor_project.etag,
            )
            predecessor_immutable = CoreProjectPatchImmutableAuthorityV1(
                project_id=predecessor_project.id,
                project_create=cls._project_create_from_project(predecessor_project),
                task_snapshot=predecessor_project.current_task_snapshot,
                created_at=predecessor_project.created_at,
            )
            if completed_patch is not None:
                latest_project = completed_patch_latest_project
                latest_immutable = (
                    None
                    if latest_project is None
                    else CoreProjectPatchImmutableAuthorityV1(
                        project_id=latest_project.id,
                        project_create=cls._project_create_from_project(latest_project),
                        task_snapshot=latest_project.current_task_snapshot,
                        created_at=latest_project.created_at,
                    )
                )
                valid_patch_binding = bool(
                    latest_project is not None
                    and completed_patch.new_request_sha256 == current.request_sha256
                    and completed_patch.new_project_create == current.project_create
                    and latest_immutable == current.immutable_authority
                    and (
                        previous is None
                        or completed_patch.base_project.active_revision
                        == previous.active_revision
                    )
                )
            else:
                latest_project = predecessor_project
                valid_patch_binding = bool(
                    predecessor_immutable.project_create == current.project_create
                    and predecessor_immutable == current.immutable_authority
                )
            if completed_action_mutation:
                valid_mutable_shape = bool(
                    latest_project is not None
                    and cls._project_authorizes_mapping(latest_project, current)
                    and current.project_etag != predecessor_project.etag
                    and cls._timestamp(current.project_updated_at)
                    > cls._timestamp(predecessor_project.updated_at)
                )
            else:
                expected_mutable = replace(
                    predecessor_mutable,
                    project_snapshot=current_mutable.project_snapshot,
                    workspace_snapshot=current_mutable.workspace_snapshot,
                    active_revision=current_mutable.active_revision,
                    registry_digest=current_mutable.registry_digest,
                    updated_at=current_mutable.updated_at,
                    etag=current_mutable.etag,
                )
                valid_mutable_shape = bool(
                    current_mutable == expected_mutable
                    and current.project_etag != predecessor_project.etag
                    and cls._timestamp(current.project_updated_at)
                    > cls._timestamp(predecessor_project.updated_at)
                )
            valid_successor_shape = bool(
                predecessor_revision == predecessor_project.active_revision
                and valid_patch_binding
                and current.active_revision != predecessor_revision
                and cls._revision_is_same_or_successor(
                    predecessor_revision,
                    current.active_revision,
                )
                and valid_mutable_shape
            )
        if not valid_successor_shape:
            raise CoreBridgeStoreContractError(
                "project-head successor mapping shape is invalid"
            )
        proof = authority.proof
        if (
            previous is not None
            and completed_patch is None
            and proof.predecessor_project is not None
        ) or (
            completed_patch_project is not None
            and proof.predecessor_project is not None
            and proof.predecessor_project != completed_patch_project
        ):
            raise CoreBridgeStoreContractError(
                "project-head successor proof predecessor authority is invalid"
            )
        project = proof.project
        head = proof.head
        revision = proof.revision
        transition = revision.transition
        head_transition = head.transition
        if head.successor_revision is None:
            head_binds_active_revision = bool(
                head_transition is None and head.updated_at == revision.updated_at
            )
        else:
            head_binds_active_revision = bool(
                head_transition is not None
                and head_transition.state is not core_v1.RevisionTransitionState.ACTIVE
                and head_transition.predecessor_revision == revision.revision
                and head_transition.successor_revision == head.successor_revision
                and head_transition.updated_at == head.updated_at
                and cls._timestamp(head.updated_at) >= cls._timestamp(revision.updated_at)
            )
        if (
            not cls._project_authorizes_mapping(project, current)
            or project.id != current.core_project_id
            or project.active_revision != current.active_revision
            or project.current_project_snapshot != current.project_snapshot
            or project.current_task_snapshot != current.task_snapshot
            or project.current_workspace_snapshot != current.workspace_snapshot
            or project.registry_digest != current.registry_digest
            or head.project_id != current.core_project_id
            or head.active_revision != current.active_revision
            or revision.revision != current.active_revision
            or revision.status is not core_v1.RevisionStatus.ACTIVE
            or revision.predecessor_revision != predecessor_revision
            or revision.revision.manifest_sha256
            != revision_manifest_sha256_v1(
                project_id=revision.revision.project_id,
                generation=revision.revision.generation,
                predecessor_revision=revision.predecessor_revision,
                project_snapshot=revision.project_snapshot,
                task_snapshot=revision.task_snapshot,
                workspace_snapshot=revision.workspace_snapshot,
                registry_digest=revision.registry_digest,
            )
            or transition is None
            or transition.state is not core_v1.RevisionTransitionState.ACTIVE
            or transition.predecessor_revision != predecessor_revision
            or transition.successor_revision != current.active_revision
            or transition.progress_completed != 1
            or transition.progress_total != 1
            or transition.message != "Project revision activated."
            or transition.error is not None
            or revision.error is not None
            or revision.created_at != revision.updated_at
            or revision.activated_at != revision.updated_at
            or transition.updated_at != revision.updated_at
            or project.updated_at != revision.updated_at
            or not head_binds_active_revision
            or revision.project_snapshot != current.project_snapshot
            or revision.task_snapshot != current.task_snapshot
            or revision.workspace_snapshot != current.workspace_snapshot
            or revision.registry_digest != current.registry_digest
        ):
            raise CoreBridgeStoreContractError(
                "project-head successor proof does not bind one active revision closure"
            )

    @staticmethod
    def _snapshot_digest(value: core_v1.ImmutableSnapshotRefV1) -> str:
        return sha256(_canonical_json_bytes(_model_value(value))).hexdigest()

    @staticmethod
    def _mapping_digest(value: CoreProjectMappingV1) -> str:
        return sha256(_canonical_json_bytes(_mapping_value(value))).hexdigest()

    @classmethod
    def _validate_history_authority_reuse(
        cls,
        history: tuple[CoreProjectMappingV1, ...],
    ) -> None:
        seen_etags: set[str] = set()
        seen_snapshots: dict[str, set[str]] = {
            "project_snapshot": set(),
            "task_snapshot": set(),
            "workspace_snapshot": set(),
        }
        previous: CoreProjectMappingV1 | None = None
        for mapping in history:
            if previous is not None:
                if (
                    mapping.project_etag != previous.project_etag
                    and mapping.project_etag in seen_etags
                ):
                    raise CoreBridgeStoreContractError(
                        "mapping project ETag reuses older authority"
                    )
                for field, seen in seen_snapshots.items():
                    current_snapshot = cast(
                        core_v1.ImmutableSnapshotRefV1, getattr(mapping, field)
                    )
                    previous_snapshot = cast(
                        core_v1.ImmutableSnapshotRefV1, getattr(previous, field)
                    )
                    digest = cls._snapshot_digest(current_snapshot)
                    if current_snapshot != previous_snapshot and digest in seen:
                        raise CoreBridgeStoreContractError(
                            f"mapping {field} reuses older authority"
                        )
            seen_etags.add(mapping.project_etag)
            for field, seen in seen_snapshots.items():
                snapshot = cast(core_v1.ImmutableSnapshotRefV1, getattr(mapping, field))
                seen.add(cls._snapshot_digest(snapshot))
            previous = mapping

    @staticmethod
    def _validate_applied_patch_snapshots(
        operation: CoreProjectPatchOperationV1,
    ) -> None:
        outcome = operation.outcome
        if outcome is None or outcome.current_project_snapshot == (
            operation.base_project.current_project_snapshot
        ):
            raise CoreBridgeStoreContractError("applied patch did not sign a new project snapshot")
        task_changed = operation.base_project.task != operation.new_project_create.task
        if task_changed == (
            outcome.current_task_snapshot == operation.base_project.current_task_snapshot
        ):
            raise CoreBridgeStoreContractError(
                "applied patch task snapshot transition is inconsistent"
            )
        workspace_changed = (
            operation.base_project.workspace != operation.new_project_create.workspace
        )
        workspace_same = outcome.current_workspace_snapshot == (
            operation.base_project.current_workspace_snapshot
        )
        imported_pending = (
            workspace_changed
            and outcome.current_workspace_snapshot is None
            and isinstance(operation.base_project.workspace, core_v1.ImportedWorkspaceSpecV1)
            and isinstance(
                operation.new_project_create.workspace,
                core_v1.ImportedWorkspaceSpecV1,
            )
        )
        if (workspace_changed and workspace_same and not imported_pending) or (
            not workspace_changed and not workspace_same
        ):
            raise CoreBridgeStoreContractError(
                "applied patch workspace snapshot transition is inconsistent"
            )

    @classmethod
    def _project_authorizes_mapping(
        cls,
        project: core_v1.ProjectV1,
        mapping: CoreProjectMappingV1,
    ) -> bool:
        project_immutable = CoreProjectPatchImmutableAuthorityV1(
            project_id=project.id,
            project_create=core_v1.ProjectCreateV1(
                name=project.name,
                description=project.description,
                spec=project.spec,
                task=project.task,
                workspace=project.workspace,
            ),
            task_snapshot=project.current_task_snapshot,
            created_at=project.created_at,
        )
        project_mutable = CoreProjectPatchMutableAuthorityV1(
            status=project.status,
            project_snapshot=project.current_project_snapshot,
            workspace_snapshot=project.current_workspace_snapshot,
            workspace_publication=project.workspace_publication,
            active_revision=project.active_revision,
            registry_digest=project.registry_digest,
            model_preparation=project.model_preparation,
            updated_at=project.updated_at,
            etag=project.etag,
        )
        mapping_mutable = mapping.mutable_authority
        return bool(
            project_immutable == mapping.immutable_authority
            and mapping_mutable == project_mutable
        )

    @staticmethod
    def _timestamp(value: str) -> datetime:
        try:
            return datetime.fromisoformat(value.removesuffix("Z") + "+00:00")
        except ValueError as exc:
            raise CoreBridgeStoreContractError("mapping authority timestamp is invalid") from exc

    @classmethod
    def _validate_mapping_monotonicity(
        cls,
        previous: CoreProjectMappingV1,
        current: CoreProjectMappingV1,
        *,
        completed_patch: CoreProjectPatchOperationV1 | None,
        completed_patch_project: core_v1.ProjectV1 | None,
        completed_patch_latest_project: core_v1.ProjectV1 | None,
        completed_action_mutation: bool,
        project_head_successor: _ProjectHeadSuccessorHistoryAuthority | None,
    ) -> None:
        if not cls._revision_is_same_or_successor(
            previous.active_revision, current.active_revision
        ):
            raise CoreBridgeStoreContractError("mapping active revision is not monotonic")
        if cls._timestamp(current.project_updated_at) < cls._timestamp(
            previous.project_updated_at
        ):
            raise CoreBridgeStoreContractError("mapping project timestamp rolled back")
        project_authority_changed = any(
            (
                previous.project_snapshot != current.project_snapshot,
                previous.task_snapshot != current.task_snapshot,
                previous.workspace_snapshot != current.workspace_snapshot,
                previous.active_revision != current.active_revision,
                previous.project_updated_at != current.project_updated_at,
            )
        )
        if project_authority_changed and previous.project_etag == current.project_etag:
            raise CoreBridgeStoreContractError(
                "mapping project authority changed without a new ETag"
            )
        if project_head_successor is not None:
            cls._validate_project_head_successor_authority(
                previous,
                current,
                project_head_successor,
                completed_patch=completed_patch,
                completed_patch_project=completed_patch_project,
                completed_patch_latest_project=completed_patch_latest_project,
                completed_action_mutation=completed_action_mutation,
            )
        elif current.active_revision != previous.active_revision:
            raise CoreBridgeStoreContractError(
                "mapping revision changed without a verified project-head successor"
            )
        elif completed_patch is None:
            raise CoreBridgeStoreContractError(
                "mapping changed without an applied outcome or verified project-head successor"
            )

    def commit_mapping(
        self,
        operation: CoreProjectCreateOperationV1,
        mapping: CoreProjectMappingV1,
        *,
        expected_previous: CoreProjectMappingV1 | None,
        completed_patch: CoreProjectPatchOperationV1 | None,
        project_head_successor: CoreProjectHeadSuccessorProofV1 | None = None,
    ) -> None:
        _create_value(operation)
        mapping_raw, mapping_digest = _encoded(_mapping_value(mapping))
        if expected_previous is not None:
            _mapping_value(expected_previous)
        if completed_patch is not None:
            _patch_value(completed_patch)
        successor_authority: _ProjectHeadSuccessorHistoryAuthority | None = None
        if project_head_successor is not None:
            if type(project_head_successor) is not CoreProjectHeadSuccessorProofV1:
                raise CoreBridgeStoreContractError(
                    "project-head successor proof has the wrong type"
                )
            if expected_previous is None:
                successor_predecessor = self._first_mapping_successor_predecessor(
                    operation,
                    completed_patch,
                    mapping,
                )
                predecessor_project = (
                    None
                    if successor_predecessor is None
                    else successor_predecessor[0]
                )
            elif completed_patch is not None:
                successor_predecessor = self._completed_patch_successor_predecessor(
                    operation,
                    completed_patch,
                    mapping,
                )
                predecessor_project = (
                    None
                    if successor_predecessor is None
                    else successor_predecessor[0]
                )
            else:
                predecessor_project = None
            if predecessor_project is not None and (
                project_head_successor.predecessor_project != predecessor_project
            ):
                raise CoreBridgeStoreContractError(
                    "project successor proof lacks exact predecessor project authority"
                )
            if predecessor_project is None and (
                project_head_successor.predecessor_project is not None
            ):
                raise CoreBridgeStoreContractError(
                    "project successor proof rewrites predecessor authority"
                )
            successor_authority = _ProjectHeadSuccessorHistoryAuthority(
                predecessor_mapping_sha256=(
                    None
                    if expected_previous is None
                    else self._mapping_digest(expected_previous)
                ),
                predecessor_project_sha256=(
                    None
                    if expected_previous is not None or predecessor_project is None
                    else sha256(
                        _canonical_json_bytes(_model_value(predecessor_project))
                    ).hexdigest()
                ),
                proof=project_head_successor,
            )
            _project_head_successor_value(successor_authority)
        self._validate_mapping_transition(
            operation,
            mapping,
            expected_previous,
            completed_patch,
            successor_authority,
        )
        history_entry = _MappingHistoryEntry(
            mapping=mapping,
            create_operation=operation,
            completed_patch=completed_patch,
            project_head_successor=successor_authority,
        )
        history_raw, history_digest = _encoded(_history_value(history_entry))
        with self._transaction(write=True) as connection:
            current_create = self._load_create_conn(connection, operation.local_project_id)
            current_mapping = self._load_mapping_conn(connection, operation.local_project_id)
            current_patch = self._load_patch_conn(connection, operation.local_project_id)
            if current_create != operation:
                raise CoreBridgeStoreConflictError(
                    "mapping commit create authority compare-and-swap failed"
                )
            if current_mapping == mapping:
                history = connection.execute(
                    """
                    SELECT document_json FROM mapping_history
                    WHERE local_project_id = ? AND mapping_generation = ?
                    """,
                    (mapping.local_project_id, mapping.mapping_generation),
                ).fetchone()
                if (
                    history is not None
                    and history["document_json"] == history_raw
                    and current_patch is None
                ):
                    return
                raise CoreBridgeStoreConflictError(
                    "mapping commit retry does not match complete durable state"
                )
            if current_mapping != expected_previous:
                raise CoreBridgeStoreConflictError("mapping compare-and-swap failed")
            if completed_patch is None:
                if current_patch is not None:
                    raise CoreBridgeStoreConflictError(
                        "mapping commit would leave an unrelated pending patch"
                    )
            elif current_patch != completed_patch:
                raise CoreBridgeStoreConflictError("completed patch compare-and-swap failed")
            existing_history = tuple(
                self._history_from_row(row).mapping
                for row in self._bounded_table_rows(
                    connection,
                    "mapping_history",
                    scalar_columns=(
                        "local_project_id",
                        "core_project_id",
                        "request_sha256",
                    ),
                )
                if row["local_project_id"] == mapping.local_project_id
            )
            self._validate_history_authority_reuse((*existing_history, mapping))
            history_count = connection.execute("SELECT count(*) FROM mapping_history").fetchone()[
                0
            ]
            if history_count >= self._max_mapping_history_rows:
                raise CoreBridgeStoreCapacityError("mapping history row capacity is exhausted")
            connection.execute(
                """
                INSERT INTO mapping_history(
                    local_project_id, mapping_generation, core_project_id,
                    request_sha256, document_json, document_sha256
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    mapping.local_project_id,
                    mapping.mapping_generation,
                    mapping.core_project_id,
                    mapping.request_sha256,
                    history_raw,
                    history_digest,
                ),
            )
            if expected_previous is None:
                connection.execute(
                    """
                    INSERT INTO mappings(
                        local_project_id, core_project_id, mapping_generation,
                        request_sha256, document_json, document_sha256
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        mapping.local_project_id,
                        mapping.core_project_id,
                        mapping.mapping_generation,
                        mapping.request_sha256,
                        mapping_raw,
                        mapping_digest,
                    ),
                )
            else:
                expected_raw, _ = _encoded(_mapping_value(expected_previous))
                cursor = connection.execute(
                    """
                    UPDATE mappings
                    SET core_project_id = ?, mapping_generation = ?, request_sha256 = ?,
                        document_json = ?, document_sha256 = ?
                    WHERE local_project_id = ? AND document_json = ?
                    """,
                    (
                        mapping.core_project_id,
                        mapping.mapping_generation,
                        mapping.request_sha256,
                        mapping_raw,
                        mapping_digest,
                        mapping.local_project_id,
                        expected_raw,
                    ),
                )
                if cursor.rowcount != 1:
                    raise CoreBridgeStoreConflictError("mapping full-row update failed")
            if completed_patch is not None:
                completed_raw, _ = _encoded(_patch_value(completed_patch))
                cursor = connection.execute(
                    """
                    DELETE FROM patch_operations
                    WHERE local_project_id = ? AND state = 'applied' AND document_json = ?
                    """,
                    (completed_patch.local_project_id, completed_raw),
                )
                if cursor.rowcount != 1:
                    raise CoreBridgeStoreConflictError("applied patch atomic cleanup failed")
            _before_mapping_commit()


__all__ = [
    "CoreBridgeStoreCapacityError",
    "CoreBridgeStoreConflictError",
    "CoreBridgeStoreContractError",
    "CoreBridgeStoreDataCorruptionError",
    "CoreBridgeStoreError",
    "CoreBridgeStoreSchemaError",
    "CoreBridgeStoreStateRootError",
    "DATABASE_FILENAME",
    "DEFAULT_MAX_MAPPING_HISTORY_ROWS",
    "DesktopCoreBridgeStoreV1",
    "IDENTITY_MARKER_FILENAME",
]
