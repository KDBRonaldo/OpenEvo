"""Durable private ledger for Core-owned science run execution."""

from __future__ import annotations

import base64
from collections.abc import Callable, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import stat
import threading
from typing import Iterator, TypeVar

from pydantic import BaseModel

from openevo.backend.contracts.v1 import models as m


_T = TypeVar("_T", bound=BaseModel)
_SCHEMA = """
CREATE TABLE IF NOT EXISTS metadata (
    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
    schema_version INTEGER NOT NULL CHECK (schema_version = 1)
) STRICT;
CREATE TABLE IF NOT EXISTS runs (
    run_id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL,
    idempotency_key TEXT NOT NULL,
    request_digest TEXT NOT NULL CHECK (length(request_digest) = 64),
    request_json BLOB NOT NULL,
    run_json BLOB NOT NULL,
    input_context_json BLOB NOT NULL,
    result_json BLOB,
    resource_version INTEGER NOT NULL CHECK (resource_version >= 1),
    deleted INTEGER NOT NULL DEFAULT 0 CHECK (deleted IN (0, 1)),
    UNIQUE(project_id, idempotency_key)
) STRICT;
CREATE TABLE IF NOT EXISTS mutations (
    operation_id TEXT NOT NULL,
    run_id TEXT NOT NULL,
    idempotency_key TEXT NOT NULL,
    request_digest TEXT NOT NULL CHECK (length(request_digest) = 64),
    response_json BLOB,
    status_code INTEGER NOT NULL,
    PRIMARY KEY(operation_id, run_id, idempotency_key),
    FOREIGN KEY(run_id) REFERENCES runs(run_id) ON DELETE CASCADE
) STRICT;
CREATE TABLE IF NOT EXISTS timeline (
    run_id TEXT NOT NULL,
    sequence INTEGER NOT NULL CHECK (sequence >= 0),
    entry_json BLOB NOT NULL,
    PRIMARY KEY(run_id, sequence),
    FOREIGN KEY(run_id) REFERENCES runs(run_id) ON DELETE CASCADE
) STRICT;
CREATE TABLE IF NOT EXISTS logs (
    run_id TEXT NOT NULL,
    sequence INTEGER NOT NULL CHECK (sequence >= 0),
    entry_json BLOB NOT NULL,
    PRIMARY KEY(run_id, sequence),
    FOREIGN KEY(run_id) REFERENCES runs(run_id) ON DELETE CASCADE
) STRICT;
CREATE TABLE IF NOT EXISTS artifacts (
    artifact_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL,
    revision_id TEXT NOT NULL,
    artifact_json BLOB NOT NULL,
    FOREIGN KEY(run_id) REFERENCES runs(run_id) ON DELETE CASCADE
) STRICT;
CREATE TABLE IF NOT EXISTS revision_contexts (
    revision_id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL,
    context_json BLOB NOT NULL
) STRICT;
CREATE TABLE IF NOT EXISTS admissions (
    run_id TEXT NOT NULL,
    operation TEXT NOT NULL,
    task_id TEXT NOT NULL,
    session_id TEXT NOT NULL,
    generation_digest TEXT NOT NULL CHECK (length(generation_digest) = 64),
    registry_digest TEXT NOT NULL CHECK (length(registry_digest) = 64),
    framework_lock_digest TEXT NOT NULL CHECK (length(framework_lock_digest) = 64),
    payload_sha256 TEXT NOT NULL CHECK (length(payload_sha256) = 64),
    PRIMARY KEY(run_id, operation, task_id, session_id),
    FOREIGN KEY(run_id) REFERENCES runs(run_id) ON DELETE CASCADE
) STRICT;
CREATE INDEX IF NOT EXISTS admissions_task_idx ON admissions(task_id, operation);
CREATE UNIQUE INDEX IF NOT EXISTS admissions_rollout_task_idx
ON admissions(task_id)
WHERE operation = 'rollout_task_submit';
"""
_MAX_RUNS = 10_000
_MAX_DOCUMENT_BYTES = 4 * 1024 * 1024
_MAX_TIMELINE_ROWS = 100_000
_MAX_TIMELINE_PER_RUN = 10_000
_MAX_LOG_ROWS = 1_000_000
_MAX_LOGS_PER_RUN = 50_000
_MAX_ARTIFACT_ROWS = 100_000
_MAX_ARTIFACTS_PER_RUN = 1_024
_MAX_ADMISSION_ROWS = 100_000
_MAX_ADMISSIONS_PER_RUN = 4_096
_ACTIVE_STATUSES = frozenset(
    {m.RunStatus.PREPARING, m.RunStatus.RUNNING, m.RunStatus.CANCELLING}
)


class ScienceRunStoreError(RuntimeError):
    pass


class ScienceRunNotFound(ScienceRunStoreError):
    pass


class ScienceRunConflict(ScienceRunStoreError):
    pass


class ScienceRunPreconditionFailed(ScienceRunStoreError):
    pass


@dataclass(frozen=True, slots=True)
class ScienceRunCreateAdmission:
    project_id: str
    idempotency_key: str
    request_digest: str
    request_json: bytes = field(repr=False)
    _owner: object = field(repr=False, compare=False)


class ScienceRunStore:
    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)
        self._lock = threading.RLock()
        self._create_condition = threading.Condition(self._lock)
        self._create_admissions: dict[tuple[str, str], ScienceRunCreateAdmission] = {}
        self._closed = False
        self._prepare_root()
        self.database = self.root / "science-runs.sqlite3"
        if self.database.exists() and self.database.is_symlink():
            raise ScienceRunStoreError("science run database must not be a symlink")
        with self._reader() as connection:
            connection.executescript(_SCHEMA)
            connection.execute(
                "INSERT OR IGNORE INTO metadata(singleton, schema_version) VALUES (1, 1)"
            )
            connection.commit()
        os.chmod(self.database, 0o600)
        self._verify_database()

    def close(self) -> None:
        with self._create_condition:
            self._closed = True
            self._create_condition.notify_all()

    def begin_create_run(
        self,
        *,
        request: m.RunCreateV1,
        idempotency_key: str,
    ) -> tuple[m.RunV1 | None, ScienceRunCreateAdmission | None]:
        request_json = _model_bytes(request)
        request_digest = hashlib.sha256(request_json).hexdigest()
        identity = (request.project_id, idempotency_key)
        with self._create_condition:
            while True:
                with self._reader() as connection:
                    existing = _existing_create_run(
                        connection,
                        project_id=request.project_id,
                        idempotency_key=idempotency_key,
                        request_digest=request_digest,
                        request_json=request_json,
                    )
                if existing is not None:
                    return existing, None
                pending = self._create_admissions.get(identity)
                if pending is None:
                    admission = ScienceRunCreateAdmission(
                        project_id=request.project_id,
                        idempotency_key=idempotency_key,
                        request_digest=request_digest,
                        request_json=request_json,
                        _owner=object(),
                    )
                    self._create_admissions[identity] = admission
                    return None, admission
                if (
                    pending.request_digest != request_digest
                    or pending.request_json != request_json
                ):
                    raise ScienceRunConflict("run idempotency key was reused")
                self._create_condition.wait()
                if self._closed:
                    raise ScienceRunStoreError("science run store is closed")

    def commit_create_run(
        self,
        admission: ScienceRunCreateAdmission,
        *,
        run: m.RunV1,
        input_context: Mapping[str, Sequence[str]],
    ) -> tuple[m.RunV1, bool]:
        context_json = _context_bytes(input_context)
        identity = (admission.project_id, admission.idempotency_key)
        with self._create_condition:
            if self._create_admissions.get(identity) is not admission:
                raise ScienceRunStoreError("science run create admission ownership is invalid")
            try:
                with self._transaction() as connection:
                    existing = _existing_create_run(
                        connection,
                        project_id=admission.project_id,
                        idempotency_key=admission.idempotency_key,
                        request_digest=admission.request_digest,
                        request_json=admission.request_json,
                    )
                    if existing is not None:
                        return existing, True
                    count = int(
                        connection.execute("SELECT COUNT(*) FROM runs").fetchone()[0]
                    )
                    if count >= _MAX_RUNS:
                        raise ScienceRunConflict("science run capacity is exhausted")
                    connection.execute(
                        "INSERT INTO runs(run_id, project_id, idempotency_key, "
                        "request_digest, request_json, run_json, input_context_json, "
                        "resource_version) VALUES (?, ?, ?, ?, ?, ?, ?, 1)",
                        (
                            run.id,
                            admission.project_id,
                            admission.idempotency_key,
                            admission.request_digest,
                            admission.request_json,
                            _model_bytes(run),
                            context_json,
                        ),
                    )
                    return run, False
            finally:
                self._release_create_admission(admission)

    def abort_create_run(self, admission: ScienceRunCreateAdmission) -> None:
        with self._create_condition:
            self._release_create_admission(admission)

    def _release_create_admission(self, admission: ScienceRunCreateAdmission) -> None:
        identity = (admission.project_id, admission.idempotency_key)
        if self._create_admissions.get(identity) is admission:
            del self._create_admissions[identity]
            self._create_condition.notify_all()

    def create_run(
        self,
        *,
        request: m.RunCreateV1,
        idempotency_key: str,
        run: m.RunV1,
        input_context: Mapping[str, Sequence[str]],
    ) -> tuple[m.RunV1, bool]:
        existing, admission = self.begin_create_run(
            request=request,
            idempotency_key=idempotency_key,
        )
        if existing is not None:
            return existing, True
        assert admission is not None
        try:
            return self.commit_create_run(
                admission,
                run=run,
                input_context=input_context,
            )
        finally:
            self.abort_create_run(admission)

    def get_run(self, run_id: str, *, include_deleted: bool = False) -> m.RunV1:
        with self._lock, self._reader() as connection:
            row = connection.execute(
                "SELECT run_json, deleted FROM runs WHERE run_id = ?", (run_id,)
            ).fetchone()
        if row is None or (bool(row["deleted"]) and not include_deleted):
            raise ScienceRunNotFound("science run was not found")
        return _model(m.RunV1, row["run_json"])

    def request_for_run(self, run_id: str) -> m.RunCreateV1:
        with self._lock, self._reader() as connection:
            row = connection.execute(
                "SELECT request_json FROM runs WHERE run_id = ? AND deleted = 0",
                (run_id,),
            ).fetchone()
        if row is None:
            raise ScienceRunNotFound("science run was not found")
        return _model(m.RunCreateV1, row["request_json"])

    def input_context(self, run_id: str) -> dict[str, list[str]]:
        with self._lock, self._reader() as connection:
            row = connection.execute(
                "SELECT input_context_json FROM runs WHERE run_id = ? AND deleted = 0",
                (run_id,),
            ).fetchone()
        if row is None:
            raise ScienceRunNotFound("science run was not found")
        return _context_value(row["input_context_json"])

    def result_for_run(self, run_id: str) -> dict[str, object] | None:
        with self._lock, self._reader() as connection:
            row = connection.execute(
                "SELECT result_json FROM runs WHERE run_id = ? AND deleted = 0",
                (run_id,),
            ).fetchone()
        if row is None:
            raise ScienceRunNotFound("science run was not found")
        if row["result_json"] is None:
            return None
        return _object_value(row["result_json"], label="science run result")

    def store_result(self, run_id: str, result: Mapping[str, object]) -> None:
        payload = _json_bytes(result)
        with self._lock, self._transaction() as connection:
            row = connection.execute(
                "SELECT result_json, deleted FROM runs WHERE run_id = ?",
                (run_id,),
            ).fetchone()
            if row is None or bool(row["deleted"]):
                raise ScienceRunNotFound("science run was not found")
            existing = row["result_json"]
            if existing is not None and bytes(existing) != payload:
                raise ScienceRunConflict("science run result changed after persistence")
            if existing is None:
                connection.execute(
                    "UPDATE runs SET result_json = ? WHERE run_id = ?",
                    (payload, run_id),
                )

    def list_runs(self) -> list[m.RunV1]:
        with self._lock, self._reader() as connection:
            rows = connection.execute(
                "SELECT run_json FROM runs WHERE deleted = 0 ORDER BY run_id LIMIT ?",
                (_MAX_RUNS + 1,),
            ).fetchall()
        if len(rows) > _MAX_RUNS:
            raise ScienceRunStoreError("science run inventory exceeds its bound")
        return [_model(m.RunV1, row["run_json"]) for row in rows]

    def mutate_run(
        self,
        run_id: str,
        transform: Callable[[m.RunV1, int], m.RunV1],
        *,
        expected_etag: str | None = None,
        result: Mapping[str, object] | None = None,
    ) -> m.RunV1:
        with self._lock, self._transaction() as connection:
            row = connection.execute(
                "SELECT run_json, resource_version, deleted FROM runs WHERE run_id = ?",
                (run_id,),
            ).fetchone()
            if row is None or bool(row["deleted"]):
                raise ScienceRunNotFound("science run was not found")
            current = _model(m.RunV1, row["run_json"])
            if expected_etag is not None and current.etag != expected_etag:
                raise ScienceRunPreconditionFailed("science run ETag changed")
            version = int(row["resource_version"]) + 1
            updated = transform(current, version)
            result_json = None if result is None else _json_bytes(result)
            connection.execute(
                "UPDATE runs SET run_json = ?, resource_version = ?, "
                "result_json = COALESCE(?, result_json) WHERE run_id = ?",
                (_model_bytes(updated), version, result_json, run_id),
            )
            return updated

    def mutate_run_with_evidence(
        self,
        run_id: str,
        transform: Callable[[m.RunV1, int], m.RunV1],
        *,
        timeline_builders: Sequence[
            Callable[[m.RunV1, int], m.TimelineEntryV1]
        ] = (),
        log_builders: Sequence[Callable[[m.RunV1, int], m.LogEntryV1]] = (),
    ) -> m.RunV1:
        with self._lock, self._transaction() as connection:
            row = connection.execute(
                "SELECT run_json, resource_version, deleted FROM runs WHERE run_id = ?",
                (run_id,),
            ).fetchone()
            if row is None or bool(row["deleted"]):
                raise ScienceRunNotFound("science run was not found")
            current = _model(m.RunV1, row["run_json"])
            version = int(row["resource_version"]) + 1
            updated = transform(current, version)
            timeline_row = connection.execute(
                "SELECT COUNT(*), COALESCE(MAX(sequence), -1) FROM timeline WHERE run_id = ?",
                (run_id,),
            ).fetchone()
            log_row = connection.execute(
                "SELECT COUNT(*), COALESCE(MAX(sequence), -1) FROM logs WHERE run_id = ?",
                (run_id,),
            ).fetchone()
            timeline_count = int(timeline_row[0])
            log_count = int(log_row[0])
            total_timeline = int(
                connection.execute("SELECT COUNT(*) FROM timeline").fetchone()[0]
            )
            total_logs = int(connection.execute("SELECT COUNT(*) FROM logs").fetchone()[0])
            if (
                timeline_count + len(timeline_builders) > _MAX_TIMELINE_PER_RUN
                or total_timeline + len(timeline_builders) > _MAX_TIMELINE_ROWS
            ):
                raise ScienceRunConflict("science run timeline capacity is exhausted")
            if (
                log_count + len(log_builders) > _MAX_LOGS_PER_RUN
                or total_logs + len(log_builders) > _MAX_LOG_ROWS
            ):
                raise ScienceRunConflict("science run log capacity is exhausted")
            next_timeline = int(timeline_row[1]) + 1
            for offset, build in enumerate(timeline_builders):
                sequence = next_timeline + offset
                entry = build(updated, sequence)
                if entry.run_id != run_id or entry.sequence != sequence:
                    raise ScienceRunStoreError(
                        "science run timeline builder changed its identity"
                    )
                connection.execute(
                    "INSERT INTO timeline(run_id, sequence, entry_json) VALUES (?, ?, ?)",
                    (run_id, sequence, _model_bytes(entry)),
                )
            next_log = int(log_row[1]) + 1
            for offset, build in enumerate(log_builders):
                sequence = next_log + offset
                entry = build(updated, sequence)
                if entry.run_id != run_id or entry.sequence != sequence:
                    raise ScienceRunStoreError("science run log builder changed its identity")
                connection.execute(
                    "INSERT INTO logs(run_id, sequence, entry_json) VALUES (?, ?, ?)",
                    (run_id, sequence, _model_bytes(entry)),
                )
            connection.execute(
                "UPDATE runs SET run_json = ?, resource_version = ? WHERE run_id = ?",
                (_model_bytes(updated), version, run_id),
            )
            return updated

    def apply_mutation(
        self,
        operation_id: str,
        run_id: str,
        idempotency_key: str,
        request_digest: str,
        *,
        expected_etag: str,
        status_code: int,
        transform: Callable[[m.RunV1, int], m.RunV1 | None],
        deleted: bool = False,
    ) -> tuple[m.RunV1 | None, bool]:
        with self._lock, self._transaction() as connection:
            row = connection.execute(
                "SELECT request_digest, response_json, status_code FROM mutations "
                "WHERE operation_id = ? AND run_id = ? AND idempotency_key = ?",
                (operation_id, run_id, idempotency_key),
            ).fetchone()
            if row is not None:
                if row["request_digest"] != request_digest:
                    raise ScienceRunConflict("run mutation idempotency key was reused")
                if int(row["status_code"]) != status_code:
                    raise ScienceRunStoreError("persisted mutation status changed")
                response = (
                    None
                    if row["response_json"] is None
                    else _model(m.RunV1, row["response_json"])
                )
                return response, True
            run_row = connection.execute(
                "SELECT run_json, resource_version, deleted FROM runs WHERE run_id = ?",
                (run_id,),
            ).fetchone()
            if run_row is None or bool(run_row["deleted"]):
                raise ScienceRunNotFound("science run was not found")
            current = _model(m.RunV1, run_row["run_json"])
            if current.etag != expected_etag:
                raise ScienceRunPreconditionFailed("science run ETag changed")
            version = int(run_row["resource_version"]) + 1
            response = transform(current, version)
            if response is not None:
                connection.execute(
                    "UPDATE runs SET run_json = ?, resource_version = ? WHERE run_id = ?",
                    (_model_bytes(response), version, run_id),
                )
            connection.execute(
                "INSERT INTO mutations(operation_id, run_id, idempotency_key, "
                "request_digest, response_json, status_code) VALUES (?, ?, ?, ?, ?, ?)",
                (
                    operation_id,
                    run_id,
                    idempotency_key,
                    request_digest,
                    None if response is None else _model_bytes(response),
                    status_code,
                ),
            )
            if deleted:
                connection.execute("UPDATE runs SET deleted = 1 WHERE run_id = ?", (run_id,))
            return response, False

    def append_timeline(
        self,
        run_id: str,
        build: Callable[[int], m.TimelineEntryV1],
    ) -> m.TimelineEntryV1:
        with self._lock, self._transaction() as connection:
            _require_live_run(connection, run_id)
            total = int(connection.execute("SELECT COUNT(*) FROM timeline").fetchone()[0])
            if total >= _MAX_TIMELINE_ROWS:
                raise ScienceRunConflict("science run timeline capacity is exhausted")
            row = connection.execute(
                "SELECT COUNT(*), COALESCE(MAX(sequence), -1) FROM timeline WHERE run_id = ?",
                (run_id,),
            ).fetchone()
            if int(row[0]) >= _MAX_TIMELINE_PER_RUN:
                raise ScienceRunConflict("science run timeline capacity is exhausted")
            sequence = int(row[1]) + 1
            entry = build(sequence)
            if entry.run_id != run_id or entry.sequence != sequence:
                raise ScienceRunStoreError("science run timeline builder changed its identity")
            connection.execute(
                "INSERT INTO timeline(run_id, sequence, entry_json) VALUES (?, ?, ?)",
                (entry.run_id, entry.sequence, _model_bytes(entry)),
            )
            return entry

    def timeline(self, run_id: str) -> list[m.TimelineEntryV1]:
        self.get_run(run_id)
        with self._lock, self._reader() as connection:
            rows = connection.execute(
                "SELECT entry_json FROM timeline WHERE run_id = ? ORDER BY sequence",
                (run_id,),
            ).fetchall()
        return [_model(m.TimelineEntryV1, row["entry_json"]) for row in rows]

    def append_log(
        self,
        run_id: str,
        build: Callable[[int], m.LogEntryV1],
    ) -> m.LogEntryV1:
        with self._lock, self._transaction() as connection:
            _require_live_run(connection, run_id)
            total = int(connection.execute("SELECT COUNT(*) FROM logs").fetchone()[0])
            if total >= _MAX_LOG_ROWS:
                raise ScienceRunConflict("science run log capacity is exhausted")
            row = connection.execute(
                "SELECT COUNT(*), COALESCE(MAX(sequence), -1) FROM logs WHERE run_id = ?",
                (run_id,),
            ).fetchone()
            if int(row[0]) >= _MAX_LOGS_PER_RUN:
                raise ScienceRunConflict("science run log capacity is exhausted")
            sequence = int(row[1]) + 1
            entry = build(sequence)
            if entry.run_id != run_id or entry.sequence != sequence:
                raise ScienceRunStoreError("science run log builder changed its identity")
            connection.execute(
                "INSERT INTO logs(run_id, sequence, entry_json) VALUES (?, ?, ?)",
                (entry.run_id, entry.sequence, _model_bytes(entry)),
            )
            return entry

    def logs(self, run_id: str) -> list[m.LogEntryV1]:
        self.get_run(run_id)
        with self._lock, self._reader() as connection:
            rows = connection.execute(
                "SELECT entry_json FROM logs WHERE run_id = ? ORDER BY sequence",
                (run_id,),
            ).fetchall()
        return [_model(m.LogEntryV1, row["entry_json"]) for row in rows]

    def store_artifacts(
        self,
        run_id: str,
        revision: m.RevisionRefV1,
        artifacts: Sequence[m.ArtifactSummaryV1],
    ) -> None:
        if len(artifacts) > _MAX_ARTIFACTS_PER_RUN:
            raise ScienceRunConflict("science run artifact capacity is exhausted")
        if len({artifact.id for artifact in artifacts}) != len(artifacts):
            raise ValueError("science run artifact IDs must be unique")
        with self._lock, self._transaction() as connection:
            run = _require_live_run(connection, run_id)
            if revision.project_id != run.project_id:
                raise ValueError("science run artifact revision belongs to another project")
            total = int(connection.execute("SELECT COUNT(*) FROM artifacts").fetchone()[0])
            existing_count = int(
                connection.execute(
                    "SELECT COUNT(*) FROM artifacts WHERE run_id = ?", (run_id,)
                ).fetchone()[0]
            )
            new_count = 0
            for artifact in artifacts:
                if artifact.run_id != run_id or artifact.project_id != run.project_id:
                    raise ValueError("science run artifact identity is invalid")
                payload = _model_bytes(artifact)
                existing = connection.execute(
                    "SELECT run_id, revision_id, artifact_json FROM artifacts "
                    "WHERE artifact_id = ?",
                    (artifact.id,),
                ).fetchone()
                if existing is not None:
                    if (
                        existing["run_id"] != run_id
                        or existing["revision_id"] != revision.id
                        or bytes(existing["artifact_json"]) != payload
                    ):
                        raise ScienceRunConflict("science run artifact identity was reused")
                    continue
                new_count += 1
                if existing_count + new_count > _MAX_ARTIFACTS_PER_RUN:
                    raise ScienceRunConflict("science run artifact capacity is exhausted")
                if total + new_count > _MAX_ARTIFACT_ROWS:
                    raise ScienceRunConflict("science run artifact capacity is exhausted")
                connection.execute(
                    "INSERT INTO artifacts(artifact_id, run_id, revision_id, artifact_json) "
                    "VALUES (?, ?, ?, ?)",
                    (artifact.id, run_id, revision.id, payload),
                )

    def artifacts_for_run(self, run_id: str) -> list[m.ArtifactSummaryV1]:
        self.get_run(run_id)
        with self._lock, self._reader() as connection:
            rows = connection.execute(
                "SELECT artifact_json FROM artifacts WHERE run_id = ? ORDER BY artifact_id",
                (run_id,),
            ).fetchall()
        return [_artifact_model(row["artifact_json"]) for row in rows]

    def artifacts_by_ids(self, artifact_ids: Sequence[str]) -> list[m.ArtifactSummaryV1]:
        if not artifact_ids:
            return []
        if len(artifact_ids) > 1024:
            raise ScienceRunStoreError("context artifact inventory exceeds its bound")
        unique_ids = list(dict.fromkeys(artifact_ids))
        placeholders = ",".join("?" for _ in unique_ids)
        with self._lock, self._reader() as connection:
            rows = connection.execute(
                f"SELECT artifact_id, artifact_json FROM artifacts "
                f"WHERE artifact_id IN ({placeholders})",
                unique_ids,
            ).fetchall()
        values = {row["artifact_id"]: _artifact_model(row["artifact_json"]) for row in rows}
        if set(values) != set(unique_ids):
            raise ScienceRunStoreError("revision context references an unknown artifact")
        return [values[item] for item in artifact_ids]

    def set_revision_context(
        self,
        project_id: str,
        revision_id: str,
        context: Mapping[str, Sequence[str]],
    ) -> None:
        payload = _context_bytes(context)
        with self._lock, self._transaction() as connection:
            existing = connection.execute(
                "SELECT project_id, context_json FROM revision_contexts WHERE revision_id = ?",
                (revision_id,),
            ).fetchone()
            if existing is not None:
                if existing["project_id"] != project_id or bytes(existing["context_json"]) != payload:
                    raise ScienceRunConflict("revision context identity was reused")
                return
            connection.execute(
                "INSERT INTO revision_contexts(revision_id, project_id, context_json) "
                "VALUES (?, ?, ?)",
                (revision_id, project_id, payload),
            )

    def revision_context(self, project_id: str, revision_id: str) -> dict[str, list[str]]:
        with self._lock, self._reader() as connection:
            row = connection.execute(
                "SELECT context_json FROM revision_contexts WHERE revision_id = ? "
                "AND project_id = ?",
                (revision_id, project_id),
            ).fetchone()
        return {} if row is None else _context_value(row["context_json"])

    def register_admission(
        self,
        *,
        run_id: str,
        operation: str,
        task_id: str,
        session_id: str | None,
        generation_digest: str,
        registry_digest: str,
        framework_lock_digest: str,
        payload_sha256: str,
        allow_create: bool,
    ) -> bool:
        for label, value in (
            ("run operation", operation),
            ("task ID", task_id),
            ("session ID", session_id or ""),
        ):
            if not isinstance(value, str) or len(value.encode("utf-8")) > 256:
                raise ValueError(f"science run admission {label} is invalid")
        for digest in (
            generation_digest,
            registry_digest,
            framework_lock_digest,
            payload_sha256,
        ):
            if len(digest) != 64 or any(character not in "0123456789abcdef" for character in digest):
                raise ValueError("science run admission digest is invalid")
        normalized_session = session_id or ""
        with self._lock, self._transaction() as connection:
            run = _require_live_run(connection, run_id)
            if run.status not in _ACTIVE_STATUSES:
                return False
            row = connection.execute(
                "SELECT generation_digest, registry_digest, framework_lock_digest, "
                "payload_sha256 FROM admissions WHERE run_id = ? AND operation = ? "
                "AND task_id = ? AND session_id = ?",
                (run_id, operation, task_id, normalized_session),
            ).fetchone()
            if row is not None:
                return all(
                    row[key] == value
                    for key, value in (
                        ("generation_digest", generation_digest),
                        ("registry_digest", registry_digest),
                        ("framework_lock_digest", framework_lock_digest),
                        ("payload_sha256", payload_sha256),
                    )
                )
            if not allow_create:
                return False
            per_run = int(
                connection.execute(
                    "SELECT COUNT(*) FROM admissions WHERE run_id = ?", (run_id,)
                ).fetchone()[0]
            )
            total = int(connection.execute("SELECT COUNT(*) FROM admissions").fetchone()[0])
            if per_run >= _MAX_ADMISSIONS_PER_RUN or total >= _MAX_ADMISSION_ROWS:
                raise ScienceRunConflict("science run admission capacity is exhausted")
            connection.execute(
                "INSERT INTO admissions(run_id, operation, task_id, session_id, "
                "generation_digest, registry_digest, framework_lock_digest, payload_sha256) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    run_id,
                    operation,
                    task_id,
                    normalized_session,
                    generation_digest,
                    registry_digest,
                    framework_lock_digest,
                    payload_sha256,
                ),
            )
            return True

    def run_for_admitted_task(self, task_id: str) -> str | None:
        with self._lock, self._reader() as connection:
            rows = connection.execute(
                "SELECT run_id FROM admissions WHERE task_id = ? AND operation = ? LIMIT 2",
                (task_id, "rollout_task_submit"),
            ).fetchall()
        if len(rows) > 1:
            raise ScienceRunStoreError("rollout task admission identity is ambiguous")
        return None if not rows else str(rows[0]["run_id"])

    def active_run_ids(self) -> list[str]:
        return [run.id for run in self.list_runs() if run.status in _ACTIVE_STATUSES]

    def queued_run_ids(self) -> list[str]:
        return [run.id for run in self.list_runs() if run.status is m.RunStatus.QUEUED]

    def _prepare_root(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        metadata = self.root.stat(follow_symlinks=False)
        if (
            not stat.S_ISDIR(metadata.st_mode)
            or metadata.st_uid != os.geteuid()
            or stat.S_IMODE(metadata.st_mode) != 0o700
        ):
            raise ScienceRunStoreError("science run root must be a private owned directory")

    def _verify_database(self) -> None:
        metadata = self.database.stat(follow_symlinks=False)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid != os.geteuid()
            or metadata.st_nlink != 1
            or stat.S_IMODE(metadata.st_mode) != 0o600
        ):
            raise ScienceRunStoreError("science run database is not a private regular file")
        with self._reader() as connection:
            row = connection.execute(
                "SELECT schema_version FROM metadata WHERE singleton = 1"
            ).fetchone()
            if row is None or int(row["schema_version"]) != 1:
                raise ScienceRunStoreError("science run schema identity is invalid")

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
            raise ScienceRunStoreError("science run store is closed")
        connection = sqlite3.connect(self.database, timeout=10.0)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 10000")
        return connection


def page_items(
    items: Sequence[_T],
    *,
    limit: int,
    after: str | None,
    query: str,
) -> tuple[list[_T], str | None, bool]:
    offset = _decode_cursor(after, query) if after is not None else 0
    selected = list(items[offset : offset + limit + 1])
    has_more = len(selected) > limit
    selected = selected[:limit]
    next_cursor = _encode_cursor(query, offset + limit) if has_more else None
    return selected, next_cursor, has_more


def _encode_cursor(query: str, offset: int) -> str:
    payload = _json_bytes({"offset": offset, "query": hashlib.sha256(query.encode()).hexdigest()})
    return base64.urlsafe_b64encode(payload).decode("ascii").rstrip("=")


def _decode_cursor(value: str, query: str) -> int:
    if not isinstance(value, str) or not 1 <= len(value) <= 512:
        raise ValueError("science run cursor is invalid")
    try:
        padded = value + "=" * (-len(value) % 4)
        payload = json.loads(base64.urlsafe_b64decode(padded.encode("ascii")))
    except (ValueError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError("science run cursor is invalid") from exc
    expected = hashlib.sha256(query.encode()).hexdigest()
    if (
        not isinstance(payload, dict)
        or set(payload) != {"offset", "query"}
        or not isinstance(payload["offset"], int)
        or isinstance(payload["offset"], bool)
        or not 0 <= payload["offset"] <= _MAX_RUNS
        or payload["query"] != expected
    ):
        raise ValueError("science run cursor is invalid")
    return payload["offset"]


def _artifact_model(payload: bytes | str) -> m.ArtifactSummaryV1:
    try:
        value = json.loads(payload)
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise ScienceRunStoreError("persisted artifact summary is invalid") from exc
    artifact_type = value.get("artifact_type") if isinstance(value, dict) else None
    model_type = {
        str(m.ArtifactType.TEXT_MEMORY): m.TextMemoryArtifactSummaryV1,
        str(m.ArtifactType.SKILL_BUNDLE): m.SkillBundleArtifactSummaryV1,
        str(m.ArtifactType.AGENT_SYSTEM): m.AgentSystemArtifactSummaryV1,
        str(m.ArtifactType.PARAMETRIC_MEMORY): m.ParametricMemoryArtifactSummaryV1,
    }.get(str(artifact_type))
    if model_type is None:
        raise ScienceRunStoreError("persisted artifact type is invalid")
    return _model(model_type, payload)


def _existing_create_run(
    connection: sqlite3.Connection,
    *,
    project_id: str,
    idempotency_key: str,
    request_digest: str,
    request_json: bytes,
) -> m.RunV1 | None:
    existing = connection.execute(
        "SELECT request_digest, request_json, run_json FROM runs "
        "WHERE project_id = ? AND idempotency_key = ?",
        (project_id, idempotency_key),
    ).fetchone()
    if existing is None:
        return None
    if (
        existing["request_digest"] != request_digest
        or bytes(existing["request_json"]) != request_json
    ):
        raise ScienceRunConflict("run idempotency key was reused")
    return _model(m.RunV1, existing["run_json"])


def _require_live_run(connection: sqlite3.Connection, run_id: str) -> m.RunV1:
    row = connection.execute(
        "SELECT run_json, deleted FROM runs WHERE run_id = ?", (run_id,)
    ).fetchone()
    if row is None or bool(row["deleted"]):
        raise ScienceRunNotFound("science run was not found")
    return _model(m.RunV1, row["run_json"])


def _model(model_type: type[_T], payload: bytes | str) -> _T:
    try:
        value = model_type.model_validate_json(payload)
    except Exception as exc:
        raise ScienceRunStoreError("persisted science run document is invalid") from exc
    raw = payload.encode("utf-8") if isinstance(payload, str) else bytes(payload)
    if _model_bytes(value) != raw:
        raise ScienceRunStoreError("persisted science run document is not canonical")
    return value


def _model_bytes(model: BaseModel) -> bytes:
    return _json_bytes(model.model_dump(mode="json"))


def _json_bytes(value: object) -> bytes:
    payload = json.dumps(
        value,
        ensure_ascii=True,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    if len(payload) > _MAX_DOCUMENT_BYTES:
        raise ScienceRunStoreError("science run document exceeds its byte bound")
    return payload


def _context_bytes(value: Mapping[str, Sequence[str]]) -> bytes:
    normalized: dict[str, list[str]] = {}
    total = 0
    if len(value) > 128:
        raise ValueError("revision context has too many artifact types")
    for artifact_type, artifact_ids in sorted(value.items()):
        if not isinstance(artifact_type, str) or not 1 <= len(artifact_type) <= 128:
            raise ValueError("revision context artifact type is invalid")
        normalized_ids = list(artifact_ids)
        if (
            len(normalized_ids) > 256
            or len(set(normalized_ids)) != len(normalized_ids)
            or any(not isinstance(item, str) or not 1 <= len(item) <= 256 for item in normalized_ids)
        ):
            raise ValueError("revision context artifact IDs are invalid")
        total += len(normalized_ids)
        if total > 1024:
            raise ValueError("revision context has too many artifacts")
        normalized[artifact_type] = normalized_ids
    return _json_bytes(normalized)


def _context_value(payload: bytes | str) -> dict[str, list[str]]:
    try:
        value = json.loads(payload)
        canonical = _context_bytes(value)
    except Exception as exc:
        raise ScienceRunStoreError("persisted revision context is invalid") from exc
    raw = payload.encode("utf-8") if isinstance(payload, str) else bytes(payload)
    if canonical != raw:
        raise ScienceRunStoreError("persisted revision context is not canonical")
    return {key: list(items) for key, items in value.items()}


def _object_value(payload: bytes | str, *, label: str) -> dict[str, object]:
    try:
        value = json.loads(payload)
        canonical = _json_bytes(value)
    except Exception as exc:
        raise ScienceRunStoreError(f"persisted {label} is invalid") from exc
    raw = payload.encode("utf-8") if isinstance(payload, str) else bytes(payload)
    if not isinstance(value, dict) or canonical != raw:
        raise ScienceRunStoreError(f"persisted {label} is not a canonical object")
    return value


__all__ = [
    "ScienceRunConflict",
    "ScienceRunCreateAdmission",
    "ScienceRunNotFound",
    "ScienceRunPreconditionFailed",
    "ScienceRunStore",
    "ScienceRunStoreError",
    "page_items",
]
