"""Durable task logs and timeline for the incremental OpenEvo daemon."""

from __future__ import annotations

import hashlib
import json
import secrets
import sqlite3
import threading
from collections.abc import Callable
from contextlib import AbstractContextManager, contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


class TaskJournalCursorError(RuntimeError):
    """A task journal cursor is beyond its authoritative sequence."""


class TaskJournalNotFoundError(KeyError):
    """The requested task is absent from session authority."""


ConnectionFactory = Callable[[], AbstractContextManager[sqlite3.Connection]]


class SqliteTaskJournal:
    """Own per-task append-only logs and typed lifecycle timeline entries."""

    def __init__(
        self,
        path: Path,
        *,
        lock: Any | None = None,
        connection_factory: ConnectionFactory | None = None,
        max_log_text: int = 16_384,
        clock: Callable[[], str] | None = None,
        event_id_factory: Callable[[], str] | None = None,
    ) -> None:
        self.path = path
        self._lock = lock or threading.RLock()
        self._connection_factory = connection_factory or self._open_connection
        self._max_log_text = max_log_text
        self._clock = clock or _utc_now
        self._event_id_factory = event_id_factory or (
            lambda: f"development-task-event-{secrets.token_hex(16)}"
        )

    def initialize(self) -> None:
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        with self._lock, self._connection_factory() as connection:
            self.initialize_schema(connection)

    @staticmethod
    def initialize_schema(connection: sqlite3.Connection) -> None:
        connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS development_task_logs_v2 (
                task_id TEXT NOT NULL REFERENCES development_sessions(session_id),
                sequence INTEGER NOT NULL CHECK (sequence > 0),
                occurred_at TEXT NOT NULL,
                stream TEXT NOT NULL CHECK (
                    stream IN ('system', 'stdout', 'stderr', 'transcript')
                ),
                message TEXT NOT NULL,
                PRIMARY KEY(task_id, sequence)
            );
            CREATE TABLE IF NOT EXISTS development_task_timeline_v2 (
                task_id TEXT NOT NULL REFERENCES development_sessions(session_id),
                sequence INTEGER NOT NULL CHECK (sequence > 0),
                event_id TEXT NOT NULL UNIQUE,
                project_id TEXT NOT NULL REFERENCES development_projects(project_id),
                event_type TEXT NOT NULL CHECK (
                    event_type IN ('task_admitted', 'attempt_appended', 'dataset_sealed')
                ),
                dataset_id TEXT,
                dataset_sha256 TEXT,
                occurred_at TEXT NOT NULL,
                PRIMARY KEY(task_id, sequence),
                CHECK (
                    (event_type = 'dataset_sealed' AND dataset_id IS NOT NULL
                     AND dataset_sha256 IS NOT NULL)
                    OR
                    (event_type != 'dataset_sealed' AND dataset_id IS NULL
                     AND dataset_sha256 IS NULL)
                )
            );
            """
        )

    def append_log(
        self,
        connection: sqlite3.Connection,
        *,
        task_id: str,
        stream: str,
        message: object,
        occurred_at: str | None = None,
    ) -> None:
        if not isinstance(message, str) or not message:
            return
        timestamp = occurred_at or self._clock()
        for offset in range(0, len(message), self._max_log_text):
            sequence = connection.execute(
                "SELECT COALESCE(MAX(sequence), 0) + 1 AS next_sequence "
                "FROM development_task_logs_v2 WHERE task_id = ?",
                (task_id,),
            ).fetchone()["next_sequence"]
            connection.execute(
                "INSERT INTO development_task_logs_v2("
                "task_id, sequence, occurred_at, stream, message"
                ") VALUES (?, ?, ?, ?, ?)",
                (
                    task_id,
                    sequence,
                    timestamp,
                    stream,
                    message[offset : offset + self._max_log_text],
                ),
            )

    def append_timeline(
        self,
        connection: sqlite3.Connection,
        *,
        task_id: str,
        project_id: str,
        event_type: str,
        occurred_at: str,
        dataset_id: str | None = None,
        dataset_sha256: str | None = None,
    ) -> None:
        existing = connection.execute(
            "SELECT 1 FROM development_task_timeline_v2 "
            "WHERE task_id = ? AND event_type = ?",
            (task_id, event_type),
        ).fetchone()
        if existing is not None:
            return
        sequence = connection.execute(
            "SELECT COALESCE(MAX(sequence), 0) + 1 AS next_sequence "
            "FROM development_task_timeline_v2 WHERE task_id = ?",
            (task_id,),
        ).fetchone()["next_sequence"]
        connection.execute(
            "INSERT INTO development_task_timeline_v2("
            "task_id, sequence, event_id, project_id, event_type, dataset_id, "
            "dataset_sha256, occurred_at"
            ") VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                task_id,
                sequence,
                self._event_id_factory(),
                project_id,
                event_type,
                dataset_id,
                dataset_sha256,
                occurred_at,
            ),
        )

    def backfill(self, connection: sqlite3.Connection) -> None:
        for row in connection.execute(
            "SELECT * FROM development_sessions ORDER BY created_at, session_id"
        ).fetchall():
            task_id = row["session_id"]
            if connection.execute(
                "SELECT 1 FROM development_task_timeline_v2 WHERE task_id = ? LIMIT 1",
                (task_id,),
            ).fetchone() is None:
                self.append_timeline(
                    connection,
                    task_id=task_id,
                    project_id=row["project_id"],
                    event_type="task_admitted",
                    occurred_at=row["created_at"],
                )
                self.append_timeline(
                    connection,
                    task_id=task_id,
                    project_id=row["project_id"],
                    event_type="attempt_appended",
                    occurred_at=row["created_at"],
                )
                dataset = connection.execute(
                    "SELECT artifact_id, uri, name, created_at "
                    "FROM development_dataset_artifacts WHERE session_id = ?",
                    (task_id,),
                ).fetchone()
                if dataset is not None:
                    dataset_sha256 = hashlib.sha256(
                        _canonical_json(
                            {
                                "artifact_id": dataset["artifact_id"],
                                "name": dataset["name"],
                                "uri": dataset["uri"],
                            }
                        ).encode("utf-8")
                    ).hexdigest()
                    self.append_timeline(
                        connection,
                        task_id=task_id,
                        project_id=row["project_id"],
                        event_type="dataset_sealed",
                        occurred_at=dataset["created_at"],
                        dataset_id=dataset["artifact_id"],
                        dataset_sha256=dataset_sha256,
                    )
            if connection.execute(
                "SELECT 1 FROM development_task_logs_v2 WHERE task_id = ? LIMIT 1",
                (task_id,),
            ).fetchone() is None:
                for message in json.loads(row["logs_json"]):
                    self.append_log(
                        connection,
                        task_id=task_id,
                        stream="system",
                        message=message,
                        occurred_at=row["updated_at"],
                    )
                self.append_log(
                    connection,
                    task_id=task_id,
                    stream="transcript",
                    message=row["response"],
                    occurred_at=row["updated_at"],
                )
                self.append_log(
                    connection,
                    task_id=task_id,
                    stream="system",
                    message=row["error"],
                    occurred_at=row["updated_at"],
                )

    def read_logs(
        self,
        task_id: str,
        *,
        after_sequence: int,
        limit: int,
    ) -> tuple[list[dict[str, Any]], bool]:
        with self._lock, self._connection_factory() as connection:
            self._require_task(connection, task_id)
            latest_sequence = connection.execute(
                "SELECT COALESCE(MAX(sequence), 0) AS latest_sequence "
                "FROM development_task_logs_v2 WHERE task_id = ?",
                (task_id,),
            ).fetchone()["latest_sequence"]
            if after_sequence > latest_sequence:
                raise TaskJournalCursorError("log cursor is beyond the authoritative journal")
            rows = connection.execute(
                "SELECT sequence, occurred_at, stream, message "
                "FROM development_task_logs_v2 "
                "WHERE task_id = ? AND sequence > ? ORDER BY sequence LIMIT ?",
                (task_id, after_sequence, limit + 1),
            ).fetchall()
        return [dict(row) for row in rows[:limit]], len(rows) > limit

    def read_timeline(
        self,
        task_id: str,
        *,
        after_sequence: int,
        limit: int,
    ) -> tuple[list[dict[str, Any]], bool]:
        with self._lock, self._connection_factory() as connection:
            self._require_task(connection, task_id)
            latest_sequence = connection.execute(
                "SELECT COALESCE(MAX(sequence), 0) AS latest_sequence "
                "FROM development_task_timeline_v2 WHERE task_id = ?",
                (task_id,),
            ).fetchone()["latest_sequence"]
            if after_sequence > latest_sequence:
                raise TaskJournalCursorError(
                    "timeline cursor is beyond the authoritative journal"
                )
            rows = connection.execute(
                "SELECT sequence, event_id, occurred_at, project_id, task_id, event_type, "
                "dataset_id, dataset_sha256 FROM development_task_timeline_v2 "
                "WHERE task_id = ? AND sequence > ? ORDER BY sequence LIMIT ?",
                (task_id, after_sequence, limit + 1),
            ).fetchall()
        items: list[dict[str, Any]] = []
        for row in rows[:limit]:
            item = dict(row)
            if item["event_type"] != "dataset_sealed":
                item.pop("dataset_id")
                item.pop("dataset_sha256")
            items.append(item)
        return items, len(rows) > limit

    @staticmethod
    def _require_task(connection: sqlite3.Connection, task_id: str) -> None:
        if connection.execute(
            "SELECT 1 FROM development_sessions WHERE session_id = ?",
            (task_id,),
        ).fetchone() is None:
            raise TaskJournalNotFoundError(task_id)

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
