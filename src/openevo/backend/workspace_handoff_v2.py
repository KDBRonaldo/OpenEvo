"""Opaque one-Attempt workspace handoff between Core and the managed Gateway."""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
from functools import wraps
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import sqlite3
import stat
import threading
from typing import (
    BinaryIO,
    Callable,
    Concatenate,
    Iterator,
    Literal,
    ParamSpec,
    TypeVar,
)

from pydantic import BaseModel, Field, model_validator

from openevo.backend.contracts.v1.models import WorkspaceArchiveDeclarationV1
from openevo.backend.contracts.v1.workspace import (
    WorkspaceArchiveError,
    verify_and_materialize_workspace,
)
from openevo.backend.contracts.v2 import models as m2
from openevo.backend.contracts.v2.snapshots import canonical_contract_bytes
from openevo.evolution.materialization_root_lock import MaterializationRootLock
from openevo.workspace_archive import (
    WorkspaceArchiveBuildError,
    write_workspace_archive,
)


_SCHEMA = """
CREATE TABLE IF NOT EXISTS metadata (
    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
    schema_version INTEGER NOT NULL CHECK (schema_version = 1),
    store_id TEXT NOT NULL CHECK (length(store_id) = 64)
) STRICT;
CREATE TABLE IF NOT EXISTS handoffs (
    handoff_id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL,
    attempt_id TEXT NOT NULL UNIQUE,
    request_sha256 TEXT NOT NULL CHECK (length(request_sha256) = 64),
    request_json BLOB NOT NULL,
    binding_json BLOB NOT NULL,
    state TEXT NOT NULL CHECK (
        state IN ('input_ready', 'claimed', 'publishing', 'result_ready', 'consumed')
    ),
    session_id TEXT,
    receipt_json BLOB,
    resource_version INTEGER NOT NULL CHECK (resource_version >= 1)
) STRICT;
"""
_MARKER_NAME = ".workspace-handoff-v2.identity.json"
_DATABASE_NAME = "workspace-handoff-v2.sqlite3"
_INPUT_DIRECTORY = "inputs"
_RESULT_DIRECTORY = "results"
_MAX_HANDOFFS = 10_000
_ID_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$"
_SHA256_PATTERN = r"^[0-9a-f]{64}$"
_ID_RE = re.compile(_ID_PATTERN, re.ASCII)
_TEMP_RE = re.compile(r"^\.workspace-handoff-[0-9a-f]{32}\.tmp$", re.ASCII)
_DIRECTORY_FLAGS = (
    os.O_RDONLY
    | getattr(os, "O_CLOEXEC", 0)
    | getattr(os, "O_DIRECTORY", 0)
    | getattr(os, "O_NOFOLLOW", 0)
)
_FILE_READ_FLAGS = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
WORKSPACE_HANDOFF_ROOT_ENV = "OPENEVO_WORKSPACE_HANDOFF_ROOT"
_P = ParamSpec("_P")
_R = TypeVar("_R")


class WorkspaceHandoffErrorV2(RuntimeError):
    pass


class WorkspaceHandoffConflictV2(WorkspaceHandoffErrorV2):
    pass


class WorkspaceHandoffIntegrityErrorV2(WorkspaceHandoffErrorV2):
    pass


def _serialized(
    method: Callable[Concatenate["WorkspaceHandoffStoreV2", _P], _R],
) -> Callable[Concatenate["WorkspaceHandoffStoreV2", _P], _R]:
    """Serialize one file/SQLite transition across threads and processes."""

    @wraps(method)
    def wrapped(
        self: WorkspaceHandoffStoreV2,
        *args: _P.args,
        **kwargs: _P.kwargs,
    ) -> _R:
        with self._lock, self._process_lock.locked() as locked_root:
            self._verify_locked_root(locked_root)
            self._verify_store_binding()
            try:
                return method(self, *args, **kwargs)
            finally:
                self._verify_store_binding()
                self._verify_locked_root(locked_root)

    return wrapped


class _HandoffModel(m2.ContractModel):
    pass


class WorkspaceHandoffRequestV2(_HandoffModel):
    workspace_handoff_request_contract_version: Literal["2"] = "2"
    task_id: str = Field(pattern=_ID_PATTERN)
    attempt_id: str = Field(pattern=_ID_PATTERN)
    task_admission_id: str = Field(pattern=_ID_PATTERN)
    admission_sha256: str = Field(pattern=_SHA256_PATTERN)
    project_id: str = Field(pattern=_ID_PATTERN)
    input_workspace_snapshot: m2.WorkspaceSnapshotRefV2
    input_archive: m2.WorkspaceArchiveDeclarationV2
    service_generation_sha256: str = Field(pattern=_SHA256_PATTERN)
    registry_sha256: str = Field(pattern=_SHA256_PATTERN)
    framework_lock_sha256: str = Field(pattern=_SHA256_PATTERN)

    @model_validator(mode="after")
    def _input_project_and_shape(self) -> WorkspaceHandoffRequestV2:
        if self.input_workspace_snapshot.project_id != self.project_id:
            raise ValueError("workspace handoff input belongs to another project")
        if (
            self.input_workspace_snapshot.entry_count != self.input_archive.entry_count
            or self.input_workspace_snapshot.byte_size != self.input_archive.extracted_byte_size
        ):
            raise ValueError("workspace handoff input archive shape differs from snapshot")
        return self


class WorkspaceHandoffBindingV2(_HandoffModel):
    workspace_handoff_contract_version: Literal["2"] = "2"
    handoff_id: str = Field(pattern=_ID_PATTERN)
    task_id: str = Field(pattern=_ID_PATTERN)
    attempt_id: str = Field(pattern=_ID_PATTERN)
    task_admission_id: str = Field(pattern=_ID_PATTERN)
    admission_sha256: str = Field(pattern=_SHA256_PATTERN)
    project_id: str = Field(pattern=_ID_PATTERN)
    input_workspace_snapshot: m2.WorkspaceSnapshotRefV2
    input_archive: m2.WorkspaceArchiveDeclarationV2
    service_generation_sha256: str = Field(pattern=_SHA256_PATTERN)
    registry_sha256: str = Field(pattern=_SHA256_PATTERN)
    framework_lock_sha256: str = Field(pattern=_SHA256_PATTERN)
    created_at: m2.UtcTimestamp


class WorkspaceResultReceiptV2(_HandoffModel):
    workspace_result_contract_version: Literal["2"] = "2"
    handoff_id: str = Field(pattern=_ID_PATTERN)
    task_id: str = Field(pattern=_ID_PATTERN)
    attempt_id: str = Field(pattern=_ID_PATTERN)
    task_admission_id: str = Field(pattern=_ID_PATTERN)
    admission_sha256: str = Field(pattern=_SHA256_PATTERN)
    project_id: str = Field(pattern=_ID_PATTERN)
    session_id: str = Field(pattern=_ID_PATTERN)
    input_workspace_snapshot_id: str = Field(pattern=_ID_PATTERN)
    input_workspace_manifest_sha256: str = Field(pattern=_SHA256_PATTERN)
    service_generation_sha256: str = Field(pattern=_SHA256_PATTERN)
    registry_sha256: str = Field(pattern=_SHA256_PATTERN)
    framework_lock_sha256: str = Field(pattern=_SHA256_PATTERN)
    output_archive: m2.WorkspaceArchiveDeclarationV2
    published_at: m2.UtcTimestamp
    result_manifest_sha256: str = Field(pattern=_SHA256_PATTERN)

    def canonical_manifest_bytes(self) -> bytes:
        return _canonical_json_bytes(
            self.model_dump(mode="json", exclude={"result_manifest_sha256"})
        )

    @model_validator(mode="after")
    def _content_addressed(self) -> WorkspaceResultReceiptV2:
        expected = hashlib.sha256(self.canonical_manifest_bytes()).hexdigest()
        if self.result_manifest_sha256 != expected:
            raise ValueError("workspace result receipt digest is invalid")
        return self


class WorkspaceHandoffStoreV2:
    """Durable private store whose records expose opaque identities only."""

    def __init__(self, root: Path | str) -> None:
        self.root = Path(root).expanduser().absolute()
        if self.root == Path("/"):
            raise WorkspaceHandoffIntegrityErrorV2("workspace handoff root is too broad")
        self.database = self.root / _DATABASE_NAME
        self._lock = threading.RLock()
        self._closed = False
        self._root_fd = -1
        self._inputs_fd = -1
        self._results_fd = -1
        self._root_identity: os.stat_result | None = None
        self._database_identity: os.stat_result | None = None
        self._store_id: str | None = None
        self._process_lock = MaterializationRootLock(self.root)
        try:
            self._prepare_root()
            with self._process_lock.locked() as locked_root:
                self._open_roots()
                self._verify_locked_root(locked_root)
                self._initialize_or_open_database()
                self._recover()
                self._verify_all()
                self._verify_locked_root(locked_root)
        except BaseException:
            self.close()
            raise

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            for name in ("_results_fd", "_inputs_fd", "_root_fd"):
                descriptor = getattr(self, name)
                if descriptor >= 0:
                    os.close(descriptor)
                    setattr(self, name, -1)

    @_serialized
    def reserve(
        self,
        request: WorkspaceHandoffRequestV2,
        source_workspace: Path | str,
        *,
        now: datetime,
    ) -> WorkspaceHandoffBindingV2:
        request = _exact_model(WorkspaceHandoffRequestV2, request)
        request_json = canonical_contract_bytes(request)
        request_sha256 = hashlib.sha256(request_json).hexdigest()
        handoff_id = f"workspace-handoff-{request_sha256}"
        with self._lock, self._reader() as connection:
            replay = connection.execute(
                "SELECT request_sha256, request_json, binding_json FROM handoffs "
                "WHERE attempt_id = ?",
                (request.attempt_id,),
            ).fetchone()
            if replay is not None:
                if (
                    replay["request_sha256"] != request_sha256
                    or bytes(replay["request_json"]) != request_json
                ):
                    raise WorkspaceHandoffConflictV2(
                        "workspace handoff Attempt identity was reused"
                    )
                binding = _model_from_bytes(
                    WorkspaceHandoffBindingV2,
                    bytes(replay["binding_json"]),
                )
                self._verify_archive(
                    self._inputs_fd,
                    _archive_name(handoff_id),
                    binding.input_archive,
                )
                return binding

        temporary = f".workspace-handoff-{secrets.token_hex(16)}.tmp"
        descriptor = -1
        try:
            descriptor = os.open(
                temporary,
                os.O_RDWR
                | os.O_CREAT
                | os.O_EXCL
                | getattr(os, "O_CLOEXEC", 0)
                | getattr(os, "O_NOFOLLOW", 0),
                0o600,
                dir_fd=self._inputs_fd,
            )
            try:
                archive = write_workspace_archive(source_workspace, descriptor)
            except WorkspaceArchiveBuildError as exc:
                raise WorkspaceHandoffConflictV2(
                    "workspace handoff input could not be archived safely"
                ) from exc
            if archive != request.input_archive:
                raise WorkspaceHandoffConflictV2(
                    "workspace handoff input differs from its immutable snapshot"
                )
            os.close(descriptor)
            descriptor = -1
            self._publish_no_replace(
                self._inputs_fd,
                temporary,
                _archive_name(handoff_id),
                archive,
            )
            binding = WorkspaceHandoffBindingV2(
                handoff_id=handoff_id,
                task_id=request.task_id,
                attempt_id=request.attempt_id,
                task_admission_id=request.task_admission_id,
                admission_sha256=request.admission_sha256,
                project_id=request.project_id,
                input_workspace_snapshot=request.input_workspace_snapshot,
                input_archive=archive,
                service_generation_sha256=request.service_generation_sha256,
                registry_sha256=request.registry_sha256,
                framework_lock_sha256=request.framework_lock_sha256,
                created_at=_timestamp(now),
            )
            with self._transaction() as connection:
                replay = connection.execute(
                    "SELECT request_sha256, request_json, binding_json FROM handoffs "
                    "WHERE attempt_id = ?",
                    (request.attempt_id,),
                ).fetchone()
                if replay is not None:
                    if (
                        replay["request_sha256"] != request_sha256
                        or bytes(replay["request_json"]) != request_json
                        or _model_from_bytes(
                            WorkspaceHandoffBindingV2,
                            bytes(replay["binding_json"]),
                        )
                        != binding
                    ):
                        raise WorkspaceHandoffConflictV2(
                            "workspace handoff Attempt identity was reused"
                        )
                    return binding
                if int(connection.execute("SELECT COUNT(*) FROM handoffs").fetchone()[0]) >= (
                    _MAX_HANDOFFS
                ):
                    raise WorkspaceHandoffConflictV2("workspace handoff capacity is exhausted")
                connection.execute(
                    "INSERT INTO handoffs(handoff_id, task_id, attempt_id, request_sha256, "
                    "request_json, binding_json, state, session_id, receipt_json, "
                    "resource_version) VALUES (?, ?, ?, ?, ?, ?, 'input_ready', NULL, "
                    "NULL, 1)",
                    (
                        handoff_id,
                        request.task_id,
                        request.attempt_id,
                        request_sha256,
                        request_json,
                        canonical_contract_bytes(binding),
                    ),
                )
                return binding
        finally:
            if descriptor >= 0:
                os.close(descriptor)
            try:
                os.unlink(temporary, dir_fd=self._inputs_fd)
            except FileNotFoundError:
                pass

    @_serialized
    def claim(
        self,
        binding: WorkspaceHandoffBindingV2,
        *,
        session_id: str,
        generation_sha256: str,
        registry_sha256: str,
        framework_lock_sha256: str,
    ) -> WorkspaceHandoffBindingV2:
        binding = _exact_model(WorkspaceHandoffBindingV2, binding)
        session_id = _resource_id(session_id, label="session")
        with self._lock, self._transaction() as connection:
            row, recovered = self._load_row(connection, binding.handoff_id)
            if recovered != binding:
                raise WorkspaceHandoffConflictV2("workspace handoff binding changed")
            if (
                generation_sha256 != binding.service_generation_sha256
                or registry_sha256 != binding.registry_sha256
                or framework_lock_sha256 != binding.framework_lock_sha256
            ):
                raise WorkspaceHandoffConflictV2("workspace handoff service generation changed")
            if row["state"] != "input_ready":
                if row["session_id"] == session_id:
                    return binding
                raise WorkspaceHandoffConflictV2("workspace handoff is owned by another session")
            connection.execute(
                "UPDATE handoffs SET state = 'claimed', session_id = ?, "
                "resource_version = resource_version + 1 WHERE handoff_id = ?",
                (session_id, binding.handoff_id),
            )
            return binding

    @_serialized
    def materialize_input(
        self,
        binding: WorkspaceHandoffBindingV2,
        *,
        session_id: str,
        destination_parent: Path | str,
    ) -> None:
        binding = _exact_model(WorkspaceHandoffBindingV2, binding)
        session_id = _resource_id(session_id, label="session")
        with self._lock, self._reader() as connection:
            row, recovered = self._load_row(connection, binding.handoff_id)
            if (
                recovered != binding
                or row["session_id"] != session_id
                or row["state"] not in {"claimed", "publishing", "result_ready"}
            ):
                raise WorkspaceHandoffConflictV2(
                    "workspace handoff input is not owned by this session"
                )
            self._verify_archive(
                self._inputs_fd,
                _archive_name(binding.handoff_id),
                binding.input_archive,
            )
            parent_fd = _open_private_directory(Path(destination_parent))
            try:
                verify_and_materialize_workspace(
                    self.root / _INPUT_DIRECTORY / _archive_name(binding.handoff_id),
                    _v1_declaration(binding.input_archive),
                    archive_root_fd=self._inputs_fd,
                    archive_name=_archive_name(binding.handoff_id),
                    workspace_root_fd=parent_fd,
                    snapshot_name="workspace",
                )
            except (WorkspaceArchiveError, OSError, FileExistsError) as exc:
                raise WorkspaceHandoffConflictV2(
                    "workspace handoff input could not be materialized"
                ) from exc
            finally:
                os.close(parent_fd)

    @_serialized
    def publish_result(
        self,
        binding: WorkspaceHandoffBindingV2,
        *,
        session_id: str,
        workspace_root: Path | str,
        now: datetime,
    ) -> WorkspaceResultReceiptV2:
        binding = _exact_model(WorkspaceHandoffBindingV2, binding)
        session_id = _resource_id(session_id, label="session")
        with self._lock, self._reader() as connection:
            row, recovered = self._load_row(connection, binding.handoff_id)
            if recovered != binding or row["session_id"] != session_id:
                raise WorkspaceHandoffConflictV2("workspace result is not owned by this session")
            if row["state"] in {"result_ready", "consumed"}:
                receipt = _receipt_from_row(row)
                self._verify_archive(
                    self._results_fd,
                    _archive_name(binding.handoff_id),
                    receipt.output_archive,
                )
                return receipt
            if row["state"] != "claimed":
                raise WorkspaceHandoffConflictV2(
                    "workspace result publication is already in progress"
                )

        temporary = f".workspace-handoff-{secrets.token_hex(16)}.tmp"
        descriptor = -1
        try:
            descriptor = os.open(
                temporary,
                os.O_RDWR
                | os.O_CREAT
                | os.O_EXCL
                | getattr(os, "O_CLOEXEC", 0)
                | getattr(os, "O_NOFOLLOW", 0),
                0o600,
                dir_fd=self._results_fd,
            )
            try:
                archive = write_workspace_archive(workspace_root, descriptor)
            except WorkspaceArchiveBuildError as exc:
                raise WorkspaceHandoffConflictV2(
                    "workspace result failed closed archive validation"
                ) from exc
            os.close(descriptor)
            descriptor = -1
            provisional = WorkspaceResultReceiptV2.model_construct(
                workspace_result_contract_version="2",
                handoff_id=binding.handoff_id,
                task_id=binding.task_id,
                attempt_id=binding.attempt_id,
                task_admission_id=binding.task_admission_id,
                admission_sha256=binding.admission_sha256,
                project_id=binding.project_id,
                session_id=session_id,
                input_workspace_snapshot_id=(
                    binding.input_workspace_snapshot.workspace_snapshot_id
                ),
                input_workspace_manifest_sha256=(binding.input_workspace_snapshot.manifest_sha256),
                service_generation_sha256=binding.service_generation_sha256,
                registry_sha256=binding.registry_sha256,
                framework_lock_sha256=binding.framework_lock_sha256,
                output_archive=archive,
                published_at=_timestamp(now),
                result_manifest_sha256="0" * 64,
            )
            receipt = WorkspaceResultReceiptV2.model_validate(
                {
                    **provisional.model_dump(mode="python"),
                    "result_manifest_sha256": hashlib.sha256(
                        provisional.canonical_manifest_bytes()
                    ).hexdigest(),
                }
            )
            with self._transaction() as connection:
                row, recovered = self._load_row(connection, binding.handoff_id)
                if (
                    recovered != binding
                    or row["session_id"] != session_id
                    or row["state"] != "claimed"
                ):
                    raise WorkspaceHandoffConflictV2(
                        "workspace result ownership changed before publication"
                    )
                connection.execute(
                    "UPDATE handoffs SET state = 'publishing', receipt_json = ?, "
                    "resource_version = resource_version + 1 WHERE handoff_id = ?",
                    (canonical_contract_bytes(receipt), binding.handoff_id),
                )
            self._publish_no_replace(
                self._results_fd,
                temporary,
                _archive_name(binding.handoff_id),
                archive,
            )
            with self._transaction() as connection:
                row, recovered = self._load_row(connection, binding.handoff_id)
                if (
                    recovered != binding
                    or row["state"] != "publishing"
                    or _receipt_from_row(row) != receipt
                ):
                    raise WorkspaceHandoffIntegrityErrorV2(
                        "workspace result publication authority changed"
                    )
                connection.execute(
                    "UPDATE handoffs SET state = 'result_ready', "
                    "resource_version = resource_version + 1 WHERE handoff_id = ?",
                    (binding.handoff_id,),
                )
            return receipt
        finally:
            if descriptor >= 0:
                os.close(descriptor)
            try:
                os.unlink(temporary, dir_fd=self._results_fd)
            except FileNotFoundError:
                pass

    @_serialized
    def get_binding(self, handoff_id: str) -> WorkspaceHandoffBindingV2:
        handoff_id = _resource_id(handoff_id, label="workspace handoff")
        with self._lock, self._reader() as connection:
            _, binding = self._load_row(connection, handoff_id)
            return binding

    @_serialized
    def get_result(self, handoff_id: str) -> WorkspaceResultReceiptV2 | None:
        handoff_id = _resource_id(handoff_id, label="workspace handoff")
        with self._lock, self._reader() as connection:
            row, _ = self._load_row(connection, handoff_id)
            if row["state"] not in {"result_ready", "consumed"}:
                return None
            receipt = _receipt_from_row(row)
            self._verify_archive(
                self._results_fd,
                _archive_name(handoff_id),
                receipt.output_archive,
            )
            return receipt

    @contextmanager
    def open_result(self, receipt: WorkspaceResultReceiptV2) -> Iterator[BinaryIO]:
        receipt = _exact_model(WorkspaceResultReceiptV2, receipt)
        with self._lock, self._process_lock.locked() as locked_root, self._reader() as connection:
            self._verify_locked_root(locked_root)
            self._verify_store_binding()
            row, _ = self._load_row(connection, receipt.handoff_id)
            if (
                row["state"] not in {"result_ready", "consumed"}
                or _receipt_from_row(row) != receipt
            ):
                raise WorkspaceHandoffConflictV2("workspace result receipt is not authoritative")
            descriptor = self._open_archive(
                self._results_fd,
                _archive_name(receipt.handoff_id),
                receipt.output_archive,
            )
            self._verify_store_binding()
            self._verify_locked_root(locked_root)
        stream = os.fdopen(descriptor, "rb", closefd=True)
        try:
            yield stream
        finally:
            stream.close()

    @_serialized
    def mark_consumed(self, receipt: WorkspaceResultReceiptV2) -> None:
        receipt = _exact_model(WorkspaceResultReceiptV2, receipt)
        with self._lock, self._transaction() as connection:
            row, _ = self._load_row(connection, receipt.handoff_id)
            if _receipt_from_row(row) != receipt or row["state"] not in {
                "result_ready",
                "consumed",
            }:
                raise WorkspaceHandoffConflictV2("workspace result cannot be marked consumed")
            if row["state"] == "result_ready":
                connection.execute(
                    "UPDATE handoffs SET state = 'consumed', "
                    "resource_version = resource_version + 1 WHERE handoff_id = ?",
                    (receipt.handoff_id,),
                )

    def _prepare_root(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        metadata = self.root.stat(follow_symlinks=False)
        if (
            not stat.S_ISDIR(metadata.st_mode)
            or metadata.st_uid != os.geteuid()
            or stat.S_IMODE(metadata.st_mode) != 0o700
        ):
            raise WorkspaceHandoffIntegrityErrorV2(
                "workspace handoff root must be a private owned directory"
            )

    def _verify_locked_root(self, descriptor: int) -> None:
        if self._root_fd < 0 or self._root_identity is None:
            raise WorkspaceHandoffIntegrityErrorV2("workspace handoff root is not held open")
        _require_private_directory(descriptor, "workspace handoff locked root")
        locked = os.fstat(descriptor)
        held = os.fstat(self._root_fd)
        if (locked.st_dev, locked.st_ino) != (held.st_dev, held.st_ino) or (
            held.st_dev,
            held.st_ino,
        ) != (self._root_identity.st_dev, self._root_identity.st_ino):
            raise WorkspaceHandoffIntegrityErrorV2("workspace handoff root path changed")

    def _open_roots(self) -> None:
        self._root_fd = os.open(self.root, _DIRECTORY_FLAGS)
        self._root_identity = os.fstat(self._root_fd)
        for name in (_INPUT_DIRECTORY, _RESULT_DIRECTORY):
            try:
                os.mkdir(name, 0o700, dir_fd=self._root_fd)
            except FileExistsError:
                pass
        self._inputs_fd = os.open(_INPUT_DIRECTORY, _DIRECTORY_FLAGS, dir_fd=self._root_fd)
        self._results_fd = os.open(_RESULT_DIRECTORY, _DIRECTORY_FLAGS, dir_fd=self._root_fd)
        _require_private_directory(self._root_fd, "workspace handoff root")
        _require_private_directory(self._inputs_fd, "workspace handoff inputs")
        _require_private_directory(self._results_fd, "workspace handoff results")

    def _initialize_or_open_database(self) -> None:
        fresh = not _entry_exists(self._root_fd, _DATABASE_NAME)
        if fresh:
            if _entry_exists(self._root_fd, _MARKER_NAME):
                raise WorkspaceHandoffIntegrityErrorV2(
                    "fresh workspace handoff store cannot claim an identity marker"
                )
            if set(os.listdir(self._root_fd)) != {
                _INPUT_DIRECTORY,
                _RESULT_DIRECTORY,
            }:
                raise WorkspaceHandoffIntegrityErrorV2(
                    "fresh workspace handoff root contains unmanaged state"
                )
            if os.listdir(self._inputs_fd) or os.listdir(self._results_fd):
                raise WorkspaceHandoffIntegrityErrorV2(
                    "fresh workspace handoff store cannot claim managed archives"
                )
            store_id = secrets.token_hex(32)
            with sqlite3.connect(self.database) as connection:
                connection.executescript(_SCHEMA)
                connection.execute(
                    "INSERT INTO metadata(singleton, schema_version, store_id) VALUES (1, 1, ?)",
                    (store_id,),
                )
                connection.commit()
            os.chmod(self.database, 0o600)
            self._database_identity = self.database.stat(follow_symlinks=False)
            self._write_marker(store_id)
        else:
            self._verify_database_file()
            with sqlite3.connect(self.database) as connection:
                connection.row_factory = sqlite3.Row
                if _schema_rows(connection) != _expected_schema_rows():
                    raise WorkspaceHandoffIntegrityErrorV2("workspace handoff schema is not exact")
                row = connection.execute(
                    "SELECT schema_version, store_id FROM metadata WHERE singleton = 1"
                ).fetchone()
                if (
                    row is None
                    or int(row["schema_version"]) != 1
                    or re.fullmatch(r"[0-9a-f]{64}", str(row["store_id"])) is None
                ):
                    raise WorkspaceHandoffIntegrityErrorV2("workspace handoff metadata is invalid")
                store_id = str(row["store_id"])
            self._database_identity = self.database.stat(follow_symlinks=False)
            self._verify_marker(store_id)
        self._store_id = store_id
        self._verify_database_file()

    def _write_marker(self, store_id: str) -> None:
        payload = self._expected_marker_bytes(store_id)
        descriptor = os.open(
            _MARKER_NAME,
            os.O_WRONLY
            | os.O_CREAT
            | os.O_EXCL
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0),
            0o600,
            dir_fd=self._root_fd,
        )
        try:
            _write_all(descriptor, payload)
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        os.fsync(self._root_fd)

    def _expected_marker_bytes(self, store_id: str) -> bytes:
        if self._database_identity is None:
            raise WorkspaceHandoffIntegrityErrorV2(
                "workspace handoff database identity is unavailable"
            )
        return _marker_bytes(
            store_id,
            root=os.fstat(self._root_fd),
            database=self._database_identity,
            inputs=os.fstat(self._inputs_fd),
            results=os.fstat(self._results_fd),
        )

    def _verify_marker(self, store_id: str) -> None:
        expected = self._expected_marker_bytes(store_id)
        try:
            descriptor = os.open(
                _MARKER_NAME,
                _FILE_READ_FLAGS,
                dir_fd=self._root_fd,
            )
        except OSError as exc:
            raise WorkspaceHandoffIntegrityErrorV2(
                "workspace handoff identity marker is unavailable"
            ) from exc
        try:
            metadata = os.fstat(descriptor)
            if (
                not stat.S_ISREG(metadata.st_mode)
                or metadata.st_uid != os.geteuid()
                or metadata.st_nlink != 1
                or stat.S_IMODE(metadata.st_mode) != 0o600
                or metadata.st_size != len(expected)
                or _read_exact(descriptor, metadata.st_size) != expected
            ):
                raise WorkspaceHandoffIntegrityErrorV2(
                    "workspace handoff identity marker is invalid"
                )
            path_metadata = os.stat(
                _MARKER_NAME,
                dir_fd=self._root_fd,
                follow_symlinks=False,
            )
            if (metadata.st_dev, metadata.st_ino) != (
                path_metadata.st_dev,
                path_metadata.st_ino,
            ):
                raise WorkspaceHandoffIntegrityErrorV2("workspace handoff marker path changed")
        finally:
            os.close(descriptor)

    def _recover(self) -> None:
        with self._lock, self._transaction() as connection:
            rows = connection.execute(
                "SELECT handoff_id, state, binding_json, receipt_json FROM handoffs"
            ).fetchall()
            expected_inputs: set[str] = set()
            expected_results: set[str] = set()
            for row in rows:
                handoff_id = _resource_id(str(row["handoff_id"]), label="workspace handoff")
                binding = _model_from_bytes(
                    WorkspaceHandoffBindingV2,
                    bytes(row["binding_json"]),
                )
                input_name = _archive_name(handoff_id)
                expected_inputs.add(input_name)
                self._verify_archive(self._inputs_fd, input_name, binding.input_archive)
                if row["state"] == "publishing":
                    receipt = _receipt_from_row(row)
                    result_name = _archive_name(handoff_id)
                    if _entry_exists(self._results_fd, result_name):
                        self._verify_archive(
                            self._results_fd,
                            result_name,
                            receipt.output_archive,
                        )
                        connection.execute(
                            "UPDATE handoffs SET state = 'result_ready', "
                            "resource_version = resource_version + 1 WHERE handoff_id = ?",
                            (handoff_id,),
                        )
                        expected_results.add(result_name)
                    else:
                        connection.execute(
                            "UPDATE handoffs SET state = 'claimed', receipt_json = NULL, "
                            "resource_version = resource_version + 1 WHERE handoff_id = ?",
                            (handoff_id,),
                        )
                elif row["state"] in {"result_ready", "consumed"}:
                    receipt = _receipt_from_row(row)
                    result_name = _archive_name(handoff_id)
                    expected_results.add(result_name)
                    self._verify_archive(
                        self._results_fd,
                        result_name,
                        receipt.output_archive,
                    )
            _clean_archive_directory(self._inputs_fd, expected_inputs)
            _clean_archive_directory(self._results_fd, expected_results)

    def _verify_all(self) -> None:
        self._verify_store_binding()
        with self._reader() as connection:
            if _schema_rows(connection) != _expected_schema_rows():
                raise WorkspaceHandoffIntegrityErrorV2("workspace handoff schema is not exact")
            quick_check = connection.execute("PRAGMA quick_check").fetchall()
            if len(quick_check) != 1 or tuple(quick_check[0]) != ("ok",):
                raise WorkspaceHandoffIntegrityErrorV2(
                    "workspace handoff database integrity failed"
                )
            if connection.execute("PRAGMA foreign_key_check").fetchone() is not None:
                raise WorkspaceHandoffIntegrityErrorV2(
                    "workspace handoff foreign-key integrity failed"
                )
            rows = connection.execute(
                "SELECT handoff_id, task_id, attempt_id, request_sha256, request_json, "
                "binding_json, state, session_id, receipt_json FROM handoffs "
                "ORDER BY handoff_id LIMIT ?",
                (_MAX_HANDOFFS + 1,),
            ).fetchall()
            if len(rows) > _MAX_HANDOFFS:
                raise WorkspaceHandoffIntegrityErrorV2(
                    "workspace handoff inventory exceeds its bound"
                )
            for row in rows:
                binding = _model_from_bytes(
                    WorkspaceHandoffBindingV2,
                    bytes(row["binding_json"]),
                )
                request = _model_from_bytes(
                    WorkspaceHandoffRequestV2,
                    bytes(row["request_json"]),
                )
                if (
                    binding.handoff_id != row["handoff_id"]
                    or binding.task_id != row["task_id"]
                    or binding.attempt_id != row["attempt_id"]
                    or request.task_id != binding.task_id
                    or request.attempt_id != binding.attempt_id
                    or hashlib.sha256(bytes(row["request_json"])).hexdigest()
                    != row["request_sha256"]
                ):
                    raise WorkspaceHandoffIntegrityErrorV2(
                        "persisted workspace handoff row is inconsistent"
                    )
                state = str(row["state"])
                if (state == "input_ready") != (row["session_id"] is None):
                    raise WorkspaceHandoffIntegrityErrorV2(
                        "persisted workspace handoff session ownership is inconsistent"
                    )
                if (state in {"result_ready", "consumed"}) != (row["receipt_json"] is not None):
                    raise WorkspaceHandoffIntegrityErrorV2(
                        "persisted workspace result authority is inconsistent"
                    )
        if set(os.listdir(self._root_fd)) != {
            _DATABASE_NAME,
            _INPUT_DIRECTORY,
            _MARKER_NAME,
            _RESULT_DIRECTORY,
        }:
            raise WorkspaceHandoffIntegrityErrorV2(
                "workspace handoff root contains unmanaged state"
            )

    def _load_row(
        self,
        connection: sqlite3.Connection,
        handoff_id: str,
    ) -> tuple[sqlite3.Row, WorkspaceHandoffBindingV2]:
        row = connection.execute(
            "SELECT * FROM handoffs WHERE handoff_id = ?",
            (handoff_id,),
        ).fetchone()
        if row is None:
            raise WorkspaceHandoffConflictV2("workspace handoff was not found")
        binding = _model_from_bytes(
            WorkspaceHandoffBindingV2,
            bytes(row["binding_json"]),
        )
        if binding.handoff_id != handoff_id:
            raise WorkspaceHandoffIntegrityErrorV2(
                "workspace handoff row identity is inconsistent"
            )
        return row, binding

    def _publish_no_replace(
        self,
        directory_fd: int,
        temporary: str,
        final: str,
        archive: m2.WorkspaceArchiveDeclarationV2,
    ) -> None:
        try:
            os.link(
                temporary,
                final,
                src_dir_fd=directory_fd,
                dst_dir_fd=directory_fd,
                follow_symlinks=False,
            )
            os.unlink(temporary, dir_fd=directory_fd)
            os.fsync(directory_fd)
        except FileExistsError:
            self._verify_archive(directory_fd, final, archive)
        self._verify_archive(directory_fd, final, archive)

    def _open_archive(
        self,
        directory_fd: int,
        name: str,
        archive: m2.WorkspaceArchiveDeclarationV2,
    ) -> int:
        try:
            descriptor = os.open(name, _FILE_READ_FLAGS, dir_fd=directory_fd)
        except OSError as exc:
            raise WorkspaceHandoffIntegrityErrorV2(
                "workspace handoff archive is unavailable"
            ) from exc
        try:
            metadata = os.fstat(descriptor)
            if (
                not stat.S_ISREG(metadata.st_mode)
                or metadata.st_uid != os.geteuid()
                or metadata.st_nlink != 1
                or stat.S_IMODE(metadata.st_mode) != 0o600
                or metadata.st_size != archive.byte_size
                or _sha256_fd(descriptor, metadata.st_size) != archive.content_sha256
            ):
                raise WorkspaceHandoffIntegrityErrorV2("workspace handoff archive is inconsistent")
            path_metadata = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
            if (metadata.st_dev, metadata.st_ino) != (
                path_metadata.st_dev,
                path_metadata.st_ino,
            ):
                raise WorkspaceHandoffIntegrityErrorV2("workspace handoff archive path changed")
            return descriptor
        except BaseException:
            os.close(descriptor)
            raise

    def _verify_archive(
        self,
        directory_fd: int,
        name: str,
        archive: m2.WorkspaceArchiveDeclarationV2,
    ) -> None:
        descriptor = self._open_archive(directory_fd, name, archive)
        os.close(descriptor)

    def _verify_database_file(self) -> None:
        try:
            metadata = self.database.stat(follow_symlinks=False)
        except OSError as exc:
            raise WorkspaceHandoffIntegrityErrorV2(
                "workspace handoff database is unavailable"
            ) from exc
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid != os.geteuid()
            or metadata.st_nlink != 1
            or stat.S_IMODE(metadata.st_mode) != 0o600
        ):
            raise WorkspaceHandoffIntegrityErrorV2(
                "workspace handoff database is not a private regular file"
            )
        if self._database_identity is not None and (
            metadata.st_dev,
            metadata.st_ino,
        ) != (
            self._database_identity.st_dev,
            self._database_identity.st_ino,
        ):
            raise WorkspaceHandoffIntegrityErrorV2("workspace handoff database binding changed")

    def _verify_store_binding(self) -> None:
        self._verify_database_file()
        if self._store_id is not None:
            self._verify_marker(self._store_id)

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
            raise WorkspaceHandoffErrorV2("workspace handoff store is closed")
        self._verify_database_file()
        connection = sqlite3.connect(self.database, timeout=10.0)
        try:
            self._verify_database_file()
        except BaseException:
            connection.close()
            raise
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 10000")
        return connection


def _receipt_from_row(row: sqlite3.Row) -> WorkspaceResultReceiptV2:
    if row["receipt_json"] is None:
        raise WorkspaceHandoffIntegrityErrorV2("workspace result receipt is missing")
    return _model_from_bytes(
        WorkspaceResultReceiptV2,
        bytes(row["receipt_json"]),
    )


def _exact_model(model_type: type[BaseModel], value: BaseModel):
    if type(value) is not model_type:
        raise TypeError(f"{model_type.__name__} has the wrong type")
    return model_type.model_validate(value.model_dump(mode="python"))


def _model_from_bytes(model_type: type[BaseModel], payload: bytes):
    if len(payload) > 1024 * 1024:
        raise WorkspaceHandoffIntegrityErrorV2(
            "persisted workspace handoff document exceeds its bound"
        )
    try:
        model = model_type.model_validate_json(payload)
    except Exception as exc:
        raise WorkspaceHandoffIntegrityErrorV2(
            "persisted workspace handoff document is invalid"
        ) from exc
    if canonical_contract_bytes(model) != payload:
        raise WorkspaceHandoffIntegrityErrorV2(
            "persisted workspace handoff document is not canonical"
        )
    return model


def _resource_id(value: str, *, label: str) -> str:
    if not isinstance(value, str) or _ID_RE.fullmatch(value) is None:
        raise ValueError(f"workspace handoff {label} ID is invalid")
    return value


def _timestamp(value: datetime) -> str:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise ValueError("workspace handoff clock must be timezone-aware")
    return value.astimezone(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _archive_name(handoff_id: str) -> str:
    return f"{_resource_id(handoff_id, label='workspace handoff')}.tar"


def _marker_bytes(
    store_id: str,
    *,
    root: os.stat_result,
    database: os.stat_result,
    inputs: os.stat_result,
    results: os.stat_result,
) -> bytes:
    return _canonical_json_bytes(
        {
            "binding_version": "1",
            "database": [database.st_dev, database.st_ino],
            "inputs": [inputs.st_dev, inputs.st_ino],
            "results": [results.st_dev, results.st_ino],
            "root": [root.st_dev, root.st_ino],
            "schema_version": 1,
            "store_id": store_id,
        }
    )


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


def _canonical_json_bytes(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=True,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _require_private_directory(descriptor: int, label: str) -> None:
    metadata = os.fstat(descriptor)
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or metadata.st_uid != os.geteuid()
        or stat.S_IMODE(metadata.st_mode) != 0o700
    ):
        raise WorkspaceHandoffIntegrityErrorV2(f"{label} must be a private owned directory")


def _open_private_directory(path: Path) -> int:
    absolute = path.expanduser().absolute()
    if absolute == Path("/"):
        raise WorkspaceHandoffConflictV2("workspace destination is too broad")
    descriptor = os.open(absolute, _DIRECTORY_FLAGS)
    try:
        _require_private_directory(descriptor, "workspace destination")
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def _entry_exists(directory_fd: int, name: str) -> bool:
    try:
        os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
    except FileNotFoundError:
        return False
    return True


def _clean_archive_directory(directory_fd: int, expected: set[str]) -> None:
    for name in os.listdir(directory_fd):
        if name in expected:
            continue
        if _TEMP_RE.fullmatch(name) is None and not name.endswith(".tar"):
            raise WorkspaceHandoffIntegrityErrorV2(
                "workspace handoff directory contains unmanaged state"
            )
        metadata = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid != os.geteuid()
            or metadata.st_nlink != 1
        ):
            raise WorkspaceHandoffIntegrityErrorV2("workspace handoff orphan is unsafe")
        os.unlink(name, dir_fd=directory_fd)
    os.fsync(directory_fd)


def _sha256_fd(descriptor: int, size: int) -> str:
    digest = hashlib.sha256()
    offset = 0
    while offset < size:
        chunk = os.pread(descriptor, min(1024 * 1024, size - offset), offset)
        if not chunk:
            raise WorkspaceHandoffIntegrityErrorV2(
                "workspace handoff archive changed while hashing"
            )
        digest.update(chunk)
        offset += len(chunk)
    if os.pread(descriptor, 1, size):
        raise WorkspaceHandoffIntegrityErrorV2("workspace handoff archive changed while hashing")
    return digest.hexdigest()


def _write_all(descriptor: int, payload: bytes) -> None:
    offset = 0
    while offset < len(payload):
        written = os.write(descriptor, payload[offset:])
        if written <= 0:
            raise WorkspaceHandoffIntegrityErrorV2("workspace handoff identity write failed")
        offset += written


def _read_exact(descriptor: int, size: int) -> bytes:
    payload = bytearray()
    offset = 0
    while offset < size:
        chunk = os.pread(descriptor, min(4096, size - offset), offset)
        if not chunk:
            raise WorkspaceHandoffIntegrityErrorV2("workspace handoff identity marker ended early")
        payload.extend(chunk)
        offset += len(chunk)
    if os.pread(descriptor, 1, size):
        raise WorkspaceHandoffIntegrityErrorV2(
            "workspace handoff identity marker exceeds its declared size"
        )
    return bytes(payload)


def _v1_declaration(
    archive: m2.WorkspaceArchiveDeclarationV2,
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
                "max_entries": m2.MAX_SNAPSHOT_ENTRIES,
                "max_path_depth": 32,
                "max_path_bytes": 256,
                "max_file_bytes": 0o77777777777,
                "max_extracted_bytes": m2.MAX_SNAPSHOT_BYTES,
            },
        }
    )


__all__ = [
    "WorkspaceHandoffBindingV2",
    "WorkspaceHandoffConflictV2",
    "WorkspaceHandoffErrorV2",
    "WorkspaceHandoffIntegrityErrorV2",
    "WorkspaceHandoffRequestV2",
    "WorkspaceHandoffStoreV2",
    "WorkspaceResultReceiptV2",
    "WORKSPACE_HANDOFF_ROOT_ENV",
]
