"""Durable Session lifecycle authority for the self-hosted OpenEvo daemon.

The store preserves the proven ``development_sessions`` SQLite layout and can
share the compatibility daemon's lock, connection factory, and task journal.
That keeps every state transition and its corresponding journal append in one
transaction while the daemon is migrated incrementally.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from collections.abc import Callable
from contextlib import AbstractContextManager, contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from openevo.daemon.task_journal import SqliteTaskJournal


class SessionConflictError(RuntimeError):
    """A requested Session transition conflicts with durable state."""


class SessionCancellationRequested(RuntimeError):
    """Completion lost a race with a durable cancellation request."""


ConnectionFactory = Callable[[], AbstractContextManager[sqlite3.Connection]]
SelectionNormalizer = Callable[[object], list[dict[str, Any]]]


class SqliteSessionStore:
    """Own Session schema, migrations, records, and lifecycle transitions."""

    def __init__(
        self,
        path: Path,
        *,
        task_journal: SqliteTaskJournal,
        lock: Any | None = None,
        connection_factory: ConnectionFactory | None = None,
        clock: Callable[[], str] | None = None,
        selection_normalizer: SelectionNormalizer | None = None,
    ) -> None:
        self.path = path
        self._task_journal = task_journal
        self._lock = lock or threading.RLock()
        self._connection_factory = connection_factory or self._open_connection
        self._clock = clock or _utc_now
        self._selection_normalizer = selection_normalizer or _default_selection_normalizer

    def initialize(self) -> None:
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        with self._lock, self._connection_factory() as connection:
            self.initialize_schema(connection)
            self.migrate_schema(connection)

    @staticmethod
    def initialize_schema(connection: sqlite3.Connection) -> None:
        connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS development_sessions (
                session_id TEXT PRIMARY KEY,
                project_id TEXT NOT NULL REFERENCES development_projects(project_id),
                task_title TEXT NOT NULL,
                instruction TEXT NOT NULL,
                response TEXT,
                model TEXT,
                state TEXT NOT NULL CHECK (state IN ('running', 'completed', 'failed')),
                duration_ms INTEGER,
                logs_json TEXT NOT NULL,
                selected_evolution_json TEXT NOT NULL DEFAULT '[]',
                evolution_errors_json TEXT NOT NULL DEFAULT '[]',
                workspace_changes_json TEXT NOT NULL DEFAULT '[]',
                context_artifact_ids_json TEXT NOT NULL DEFAULT '[]',
                runtime_activation_json TEXT NOT NULL DEFAULT 'null',
                cancellation_requested INTEGER NOT NULL DEFAULT 0
                    CHECK (cancellation_requested IN (0, 1)),
                terminal_kind TEXT CHECK (terminal_kind IN ('failed', 'cancelled')),
                error TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS development_sessions_project_created
                ON development_sessions(project_id, created_at, session_id);
            """
        )

    def migrate_schema(self, connection: sqlite3.Connection) -> None:
        columns = {
            row["name"]
            for row in connection.execute("PRAGMA table_info(development_sessions)")
        }
        additions = (
            ("selected_evolution_json", "TEXT NOT NULL DEFAULT '[]'"),
            ("evolution_errors_json", "TEXT NOT NULL DEFAULT '[]'"),
            ("workspace_changes_json", "TEXT NOT NULL DEFAULT '[]'"),
            ("context_artifact_ids_json", "TEXT NOT NULL DEFAULT '[]'"),
            ("runtime_activation_json", "TEXT NOT NULL DEFAULT 'null'"),
            (
                "cancellation_requested",
                "INTEGER NOT NULL DEFAULT 0 CHECK (cancellation_requested IN (0, 1))",
            ),
            (
                "terminal_kind",
                "TEXT CHECK (terminal_kind IN ('failed', 'cancelled'))",
            ),
        )
        for name, declaration in additions:
            if name not in columns:
                connection.execute(
                    f"ALTER TABLE development_sessions ADD COLUMN {name} {declaration}"
                )
        connection.execute(
            "UPDATE development_sessions SET runtime_activation_json = 'null' "
            "WHERE runtime_activation_json = '{}'"
        )
        for row in connection.execute(
            "SELECT session_id, selected_evolution_json FROM development_sessions"
        ).fetchall():
            stored = json.loads(row["selected_evolution_json"])
            normalized_json = _canonical_json(self._selection_normalizer(stored))
            if normalized_json != row["selected_evolution_json"]:
                connection.execute(
                    "UPDATE development_sessions SET selected_evolution_json = ? "
                    "WHERE session_id = ?",
                    (normalized_json, row["session_id"]),
                )

    def recover_interrupted(
        self,
        connection: sqlite3.Connection,
        *,
        occurred_at: str,
    ) -> tuple[str, ...]:
        session_ids = tuple(
            row["session_id"]
            for row in connection.execute(
                "SELECT session_id FROM development_sessions WHERE state = 'running'"
            ).fetchall()
        )
        connection.execute(
            """
            UPDATE development_sessions
            SET state = 'failed', terminal_kind = 'failed', error = ?, updated_at = ?
            WHERE state = 'running'
            """,
            ("Development daemon restarted before this session completed.", occurred_at),
        )
        return session_ids

    def append_recovery_logs(
        self,
        connection: sqlite3.Connection,
        session_ids: tuple[str, ...],
        *,
        occurred_at: str,
    ) -> None:
        for session_id in session_ids:
            self._task_journal.append_log(
                connection,
                task_id=session_id,
                stream="system",
                message=(
                    "Session failed: Development daemon restarted before this "
                    "session completed."
                ),
                occurred_at=occurred_at,
            )

    def start(self, session_id: str, request: dict[str, str]) -> None:
        now = self._clock()
        with self._lock, self._connection_factory() as connection:
            project = connection.execute(
                "SELECT display_name FROM development_projects WHERE project_id = ?",
                (request["project_id"],),
            ).fetchone()
            if project is None:
                raise KeyError(request["project_id"])
            if project["display_name"] != request["project_name"]:
                raise SessionConflictError(
                    "project_name does not match the persisted project"
                )
            context_rows = connection.execute(
                """
                SELECT artifact.artifact_id
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
                (request["project_id"], request["project_id"]),
            ).fetchall()
            context_artifact_ids = [row["artifact_id"] for row in context_rows]
            connection.execute(
                """
                INSERT INTO development_sessions(
                    session_id, project_id, task_title, instruction, response, model,
                    state, duration_ms, logs_json, selected_evolution_json,
                    evolution_errors_json, workspace_changes_json,
                    context_artifact_ids_json, error, created_at, updated_at
                ) VALUES (?, ?, ?, ?, NULL, NULL, 'running', NULL, ?, ?, '[]', '[]', ?, NULL, ?, ?)
                """,
                (
                    session_id,
                    request["project_id"],
                    request["task_title"],
                    request["instruction"],
                    _canonical_json(["Remote development daemon admitted the session."]),
                    "[]",
                    _canonical_json(context_artifact_ids),
                    now,
                    now,
                ),
            )
            self._task_journal.append_log(
                connection,
                task_id=session_id,
                stream="system",
                message="Remote development daemon admitted the session.",
                occurred_at=now,
            )
            self._task_journal.append_timeline(
                connection,
                task_id=session_id,
                project_id=request["project_id"],
                event_type="task_admitted",
                occurred_at=now,
            )
            self._task_journal.append_timeline(
                connection,
                task_id=session_id,
                project_id=request["project_id"],
                event_type="attempt_appended",
                occurred_at=now,
            )

    def complete(self, session_id: str, result: dict[str, Any]) -> None:
        now = self._clock()
        with self._lock, self._connection_factory() as connection:
            row = connection.execute(
                "SELECT logs_json, cancellation_requested FROM development_sessions "
                "WHERE session_id = ?",
                (session_id,),
            ).fetchone()
            if row is None:
                raise KeyError(session_id)
            if row["cancellation_requested"]:
                raise SessionCancellationRequested("Session cancelled by user")
            merged_logs = json.loads(row["logs_json"])
            appended_logs: list[str] = []
            for message in result["logs"]:
                if message not in merged_logs:
                    merged_logs.append(message)
                    appended_logs.append(message)
            connection.execute(
                """
                UPDATE development_sessions
                SET response = ?, model = ?, state = 'completed', duration_ms = ?,
                    logs_json = ?, workspace_changes_json = ?, runtime_activation_json = ?,
                    cancellation_requested = 0, terminal_kind = NULL,
                    error = NULL, updated_at = ?
                WHERE session_id = ?
                """,
                (
                    result["response"],
                    result["model"],
                    result["duration_ms"],
                    _canonical_json(merged_logs),
                    _canonical_json(result.get("workspace_changes", [])),
                    _canonical_json(result.get("runtime_activation")),
                    now,
                    session_id,
                ),
            )
            for message in appended_logs:
                self._task_journal.append_log(
                    connection,
                    task_id=session_id,
                    stream="system",
                    message=message,
                    occurred_at=now,
                )
            self._task_journal.append_log(
                connection,
                task_id=session_id,
                stream="transcript",
                message=result["response"],
                occurred_at=now,
            )

    def append_log(self, session_id: str, message: str) -> list[str]:
        now = self._clock()
        with self._lock, self._connection_factory() as connection:
            row = connection.execute(
                "SELECT logs_json FROM development_sessions WHERE session_id = ?",
                (session_id,),
            ).fetchone()
            if row is None:
                raise KeyError(session_id)
            logs = json.loads(row["logs_json"])
            logs.append(message)
            connection.execute(
                "UPDATE development_sessions SET logs_json = ?, updated_at = ? "
                "WHERE session_id = ?",
                (_canonical_json(logs), now, session_id),
            )
            self._task_journal.append_log(
                connection,
                task_id=session_id,
                stream="system",
                message=message,
                occurred_at=now,
            )
        return logs

    def get(self, session_id: str) -> dict[str, Any]:
        with self._lock, self._connection_factory() as connection:
            row = connection.execute(
                "SELECT * FROM development_sessions WHERE session_id = ?",
                (session_id,),
            ).fetchone()
            evidence_ready = connection.execute(
                "SELECT 1 FROM development_dataset_artifacts WHERE session_id = ?",
                (session_id,),
            ).fetchone() is not None
        if row is None:
            raise KeyError(session_id)
        return self.record(row, evolution_evidence_ready=evidence_ready)

    def cancellation_requested(self, session_id: str) -> bool:
        with self._lock, self._connection_factory() as connection:
            row = connection.execute(
                "SELECT cancellation_requested FROM development_sessions "
                "WHERE session_id = ?",
                (session_id,),
            ).fetchone()
        if row is None:
            raise KeyError(session_id)
        return bool(row["cancellation_requested"])

    def request_cancellation(self, session_id: str) -> dict[str, Any]:
        now = self._clock()
        with self._lock, self._connection_factory() as connection:
            row = connection.execute(
                "SELECT state, logs_json FROM development_sessions WHERE session_id = ?",
                (session_id,),
            ).fetchone()
            if row is None:
                raise KeyError(session_id)
            if row["state"] != "running":
                raise SessionConflictError("session is already terminal")
            logs = json.loads(row["logs_json"])
            message = "Cancellation requested; stopping the active harness process."
            if message not in logs:
                logs.append(message)
                self._task_journal.append_log(
                    connection,
                    task_id=session_id,
                    stream="system",
                    message=message,
                    occurred_at=now,
                )
            connection.execute(
                "UPDATE development_sessions "
                "SET cancellation_requested = 1, logs_json = ?, updated_at = ? "
                "WHERE session_id = ? AND state = 'running'",
                (_canonical_json(logs), now, session_id),
            )
            updated = connection.execute(
                "SELECT * FROM development_sessions WHERE session_id = ?",
                (session_id,),
            ).fetchone()
        return self.record(updated)

    def cancel(
        self,
        session_id: str,
        workspace_changes: list[dict[str, Any]] | None = None,
    ) -> None:
        now = self._clock()
        with self._lock, self._connection_factory() as connection:
            row = connection.execute(
                "SELECT logs_json FROM development_sessions WHERE session_id = ?",
                (session_id,),
            ).fetchone()
            if row is None:
                raise KeyError(session_id)
            logs = json.loads(row["logs_json"])
            message = "Session cancelled by user."
            if message not in logs:
                logs.append(message)
                self._task_journal.append_log(
                    connection,
                    task_id=session_id,
                    stream="system",
                    message=message,
                    occurred_at=now,
                )
            connection.execute(
                """
                UPDATE development_sessions
                SET state = 'failed', cancellation_requested = 1,
                    terminal_kind = 'cancelled', logs_json = ?, workspace_changes_json = ?,
                    error = NULL, updated_at = ?
                WHERE session_id = ? AND state = 'running'
                """,
                (
                    _canonical_json(logs),
                    _canonical_json(workspace_changes or []),
                    now,
                    session_id,
                ),
            )

    def fail(
        self,
        session_id: str,
        error: str,
        workspace_changes: list[dict[str, Any]] | None = None,
    ) -> None:
        now = self._clock()
        with self._lock, self._connection_factory() as connection:
            row = connection.execute(
                "SELECT logs_json FROM development_sessions WHERE session_id = ?",
                (session_id,),
            ).fetchone()
            if row is None:
                raise KeyError(session_id)
            logs = json.loads(row["logs_json"])
            message = f"Session failed: {error}"
            logs.append(message)
            connection.execute(
                """
                UPDATE development_sessions
                SET state = 'failed', logs_json = ?, workspace_changes_json = ?,
                    terminal_kind = 'failed', error = ?, updated_at = ?
                WHERE session_id = ?
                """,
                (
                    _canonical_json(logs),
                    _canonical_json(workspace_changes or []),
                    error,
                    now,
                    session_id,
                ),
            )
            self._task_journal.append_log(
                connection,
                task_id=session_id,
                stream="system",
                message=message,
                occurred_at=now,
            )

    def record(
        self,
        row: sqlite3.Row,
        *,
        evolution_evidence_ready: bool = False,
    ) -> dict[str, Any]:
        state = row["state"]
        if row["terminal_kind"] == "cancelled":
            state = "cancelled"
        elif state == "running" and row["cancellation_requested"]:
            state = "cancelling"
        return {
            "session_id": row["session_id"],
            "project_id": row["project_id"],
            "task_title": row["task_title"],
            "instruction": row["instruction"],
            "response": row["response"],
            "model": row["model"],
            "state": state,
            "duration_ms": row["duration_ms"],
            "logs": json.loads(row["logs_json"]),
            "selected_evolution": self._selection_normalizer(
                json.loads(row["selected_evolution_json"])
            ),
            "evolution_errors": json.loads(row["evolution_errors_json"]),
            "workspace_changes": json.loads(row["workspace_changes_json"]),
            "context_artifact_ids": json.loads(row["context_artifact_ids_json"]),
            "runtime_activation": json.loads(row["runtime_activation_json"]),
            "evolution_evidence_ready": evolution_evidence_ready,
            "error": row["error"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
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


def _default_selection_normalizer(value: object) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        raise RuntimeError("persisted Session evolution selection is not a list")
    return value


def _canonical_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _utc_now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")
