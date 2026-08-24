"""Durable dataset and evolution artifact authority for the self-hosted daemon.

This store preserves the compatibility daemon's SQLite tables while moving
artifact identity, persistence, promotion lookup, and stable pagination into
the product daemon package. HTTP model projection remains an adapter concern.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
from collections.abc import Callable
from contextlib import AbstractContextManager, contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from openevo.daemon.errors import RequestError
from openevo.daemon.task_journal import SqliteTaskJournal


ConnectionFactory = Callable[[], AbstractContextManager[sqlite3.Connection]]


@dataclass(frozen=True)
class ArtifactPage:
    """One stable cursor page of canonical artifact records."""

    items: tuple[dict[str, Any], ...]
    next_cursor: str | None
    has_more: bool


class SqliteArtifactStore:
    """Own artifact schema and durable artifact collection behavior."""

    def __init__(
        self,
        path: Path,
        *,
        task_journal: SqliteTaskJournal,
        lock: Any | None = None,
        connection_factory: ConnectionFactory | None = None,
        clock: Callable[[], str] | None = None,
    ) -> None:
        self.path = path
        self._task_journal = task_journal
        self._lock = lock or threading.RLock()
        self._connection_factory = connection_factory or self._open_connection
        self._clock = clock or _utc_now

    def initialize(self) -> None:
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        with self._lock, self._connection_factory() as connection:
            self.initialize_schema(connection)
            self.migrate_schema(connection)

    @staticmethod
    def initialize_schema(connection: sqlite3.Connection) -> None:
        connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS development_evolution_artifacts_v2 (
                artifact_id TEXT PRIMARY KEY,
                project_id TEXT NOT NULL REFERENCES development_projects(project_id),
                session_id TEXT NOT NULL REFERENCES development_sessions(session_id),
                run_id TEXT,
                target_id TEXT NOT NULL,
                artifact_type TEXT NOT NULL,
                method_id TEXT NOT NULL,
                renderer_kind TEXT NOT NULL CHECK (
                    renderer_kind IN ('markdown', 'file_bundle', 'structured_summary', 'adapter')
                ),
                documents_json TEXT NOT NULL,
                manifest_json TEXT NOT NULL,
                content_sha256 TEXT NOT NULL,
                byte_size INTEGER NOT NULL,
                previous_artifact_id TEXT,
                promoted INTEGER NOT NULL DEFAULT 1 CHECK (promoted IN (0, 1)),
                created_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS development_evolution_artifacts_v2_project_created
                ON development_evolution_artifacts_v2(
                    project_id, target_id, created_at, artifact_id
                );
            CREATE TABLE IF NOT EXISTS development_dataset_artifacts (
                artifact_id TEXT PRIMARY KEY,
                project_id TEXT NOT NULL REFERENCES development_projects(project_id),
                session_id TEXT NOT NULL UNIQUE REFERENCES development_sessions(session_id),
                uri TEXT NOT NULL,
                name TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS development_dataset_artifacts_project_created
                ON development_dataset_artifacts(project_id, created_at, artifact_id);
            """
        )

    @staticmethod
    def migrate_schema(connection: sqlite3.Connection) -> None:
        columns = {
            row["name"]
            for row in connection.execute("PRAGMA table_info(development_evolution_artifacts_v2)")
        }
        if "promoted" not in columns:
            connection.execute(
                "ALTER TABLE development_evolution_artifacts_v2 "
                "ADD COLUMN promoted INTEGER NOT NULL DEFAULT 1 "
                "CHECK (promoted IN (0, 1))"
            )
        if "run_id" not in columns:
            connection.execute(
                "ALTER TABLE development_evolution_artifacts_v2 ADD COLUMN run_id TEXT"
            )

    def latest(self, project_id: str, target_id: str) -> dict[str, Any] | None:
        with self._lock, self._connection_factory() as connection:
            row = connection.execute(
                """
                SELECT * FROM development_evolution_artifacts_v2
                WHERE project_id = ? AND target_id = ?
                  AND artifact_type != 'report' AND promoted = 1
                ORDER BY created_at DESC, artifact_id DESC
                LIMIT 1
                """,
                (project_id, target_id),
            ).fetchone()
        return None if row is None else self.record(row)

    def latest_context(self, project_id: str) -> list[dict[str, Any]]:
        with self._lock, self._connection_factory() as connection:
            rows = connection.execute(
                """
                SELECT artifact.*
                FROM development_evolution_artifacts_v2 AS artifact
                JOIN (
                    SELECT target_id, MAX(created_at || artifact_id) AS latest
                    FROM development_evolution_artifacts_v2
                    WHERE project_id = ? AND artifact_type != 'report' AND promoted = 1
                    GROUP BY target_id
                ) AS selected
                  ON selected.target_id = artifact.target_id
                 AND selected.latest = artifact.created_at || artifact.artifact_id
                WHERE artifact.project_id = ?
                ORDER BY artifact.target_id
                """,
                (project_id, project_id),
            ).fetchall()
        return [self.record(row) for row in rows]

    def record_dataset(
        self,
        *,
        artifact_id: str,
        project_id: str,
        session_id: str,
        uri: str,
        name: str,
        manifest_sha256: str | None = None,
    ) -> None:
        now = self._clock()
        effective_manifest_sha256 = (
            manifest_sha256
            or hashlib.sha256(
                _canonical_json({"artifact_id": artifact_id, "name": name, "uri": uri}).encode(
                    "utf-8"
                )
            ).hexdigest()
        )
        with self._lock, self._connection_factory() as connection:
            connection.execute(
                """
                INSERT INTO development_dataset_artifacts(
                    artifact_id, project_id, session_id, uri, name, created_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(session_id) DO UPDATE SET
                    artifact_id = excluded.artifact_id,
                    project_id = excluded.project_id,
                    uri = excluded.uri,
                    name = excluded.name
                """,
                (artifact_id, project_id, session_id, uri, name, now),
            )
            self._task_journal.append_timeline(
                connection,
                task_id=session_id,
                project_id=project_id,
                event_type="dataset_sealed",
                occurred_at=now,
                dataset_id=artifact_id,
                dataset_sha256=effective_manifest_sha256,
            )

    def datasets(self, project_id: str) -> list[dict[str, str]]:
        with self._lock, self._connection_factory() as connection:
            rows = connection.execute(
                """
                SELECT artifact_id, project_id, session_id, uri, name, created_at
                FROM development_dataset_artifacts
                WHERE project_id = ?
                ORDER BY created_at, artifact_id
                """,
                (project_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def dataset(self, artifact_id: str) -> dict[str, str]:
        with self._lock, self._connection_factory() as connection:
            row = connection.execute(
                "SELECT artifact_id, project_id, session_id, uri, name, created_at "
                "FROM development_dataset_artifacts WHERE artifact_id = ?",
                (artifact_id,),
            ).fetchone()
        if row is None:
            raise KeyError(artifact_id)
        return dict(row)

    def get(self, artifact_id: str) -> dict[str, Any]:
        with self._lock, self._connection_factory() as connection:
            row = connection.execute(
                "SELECT * FROM development_evolution_artifacts_v2 WHERE artifact_id = ?",
                (artifact_id,),
            ).fetchone()
        if row is None:
            raise KeyError(artifact_id)
        return self.record(row)

    def page(
        self,
        *,
        project_id: str | None,
        task_id: str | None,
        after_artifact_id: str | None,
        limit: int,
    ) -> ArtifactPage:
        clauses: list[str] = []
        parameters: list[object] = []
        if project_id is not None:
            clauses.append("project_id = ?")
            parameters.append(project_id)
        if task_id is not None:
            clauses.append("session_id = ?")
            parameters.append(task_id)
        where = " AND ".join(clauses) if clauses else "1 = 1"
        with self._lock, self._connection_factory() as connection:
            if (
                project_id is not None
                and connection.execute(
                    "SELECT 1 FROM development_projects WHERE project_id = ?",
                    (project_id,),
                ).fetchone()
                is None
            ):
                raise KeyError(project_id)
            if (
                task_id is not None
                and connection.execute(
                    "SELECT 1 FROM development_sessions WHERE session_id = ?",
                    (task_id,),
                ).fetchone()
                is None
            ):
                raise KeyError(task_id)
            if after_artifact_id is not None:
                cursor = connection.execute(
                    "SELECT created_at, artifact_id "
                    "FROM development_evolution_artifacts_v2 "
                    f"WHERE {where} AND artifact_id = ?",
                    (*parameters, after_artifact_id),
                ).fetchone()
                if cursor is None:
                    raise RequestError("artifact cursor is not part of this collection")
                clauses.append("(created_at > ? OR (created_at = ? AND artifact_id > ?))")
                parameters.extend(
                    [
                        cursor["created_at"],
                        cursor["created_at"],
                        cursor["artifact_id"],
                    ]
                )
                where = " AND ".join(clauses)
            rows = connection.execute(
                "SELECT * FROM development_evolution_artifacts_v2 "
                f"WHERE {where} ORDER BY created_at, artifact_id LIMIT ?",
                (*parameters, limit + 1),
            ).fetchall()
        has_more = len(rows) > limit
        records = tuple(self.record(row) for row in rows[:limit])
        return ArtifactPage(
            items=records,
            next_cursor=(records[-1]["artifact_id"] if has_more and records else None),
            has_more=has_more,
        )

    def create(
        self,
        *,
        artifact_id: str,
        project_id: str,
        session_id: str,
        run_id: str | None = None,
        target_id: str,
        artifact_type: str,
        method_id: str,
        renderer_kind: str,
        documents: list[dict[str, str]],
        manifest: dict[str, Any],
        previous_artifact_id: str | None,
        promoted: bool,
    ) -> dict[str, Any]:
        encoded = _canonical_json(documents).encode("utf-8")
        created_at = self._clock()
        with self._lock, self._connection_factory() as connection:
            connection.execute(
                """
                INSERT INTO development_evolution_artifacts_v2(
                    artifact_id, project_id, session_id, run_id, target_id, artifact_type,
                    method_id, renderer_kind, documents_json, manifest_json,
                    content_sha256, byte_size, previous_artifact_id, promoted, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    artifact_id,
                    project_id,
                    session_id,
                    run_id,
                    target_id,
                    artifact_type,
                    method_id,
                    renderer_kind,
                    _canonical_json(documents),
                    _canonical_json(manifest),
                    hashlib.sha256(encoded).hexdigest(),
                    sum(len(document["content"].encode("utf-8")) for document in documents),
                    previous_artifact_id,
                    int(promoted),
                    created_at,
                ),
            )
            row = connection.execute(
                "SELECT * FROM development_evolution_artifacts_v2 WHERE artifact_id = ?",
                (artifact_id,),
            ).fetchone()
        if row is None:
            raise RuntimeError("document evolution artifact was not persisted")
        return self.record(row)

    @staticmethod
    def record(row: sqlite3.Row) -> dict[str, Any]:
        documents = json.loads(row["documents_json"])
        primary = documents[0] if documents else None
        return {
            "artifact_id": row["artifact_id"],
            "project_id": row["project_id"],
            "session_id": row["session_id"],
            "run_id": row["run_id"],
            "target_id": row["target_id"],
            "artifact_type": row["artifact_type"],
            "method": row["method_id"],
            "renderer_kind": row["renderer_kind"],
            "documents": documents,
            "manifest": json.loads(row["manifest_json"]),
            "content_path": primary["path"] if primary else None,
            "content": primary["content"] if primary else None,
            "content_sha256": row["content_sha256"],
            "byte_size": row["byte_size"],
            "previous_artifact_id": row["previous_artifact_id"],
            "promoted": bool(row["promoted"]),
            "created_at": row["created_at"],
        }

    @contextmanager
    def _open_connection(self):
        connection = sqlite3.connect(self.path, timeout=10)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        try:
            with connection:
                yield connection
        finally:
            connection.close()


def _canonical_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _utc_now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")
