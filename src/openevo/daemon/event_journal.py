"""Recoverable state-change journal for the incremental OpenEvo daemon.

The journal preserves the existing ``development_state_events`` schema and can
share the compatibility daemon's SQLite connection factory.  Events are
written in the same transaction as the state mutation they announce; waiters
are notified only after that transaction has committed.
"""

from __future__ import annotations

import hashlib
import json
import secrets
import sqlite3
import threading
import time
from collections.abc import Callable
from contextlib import AbstractContextManager, contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


class EventCursorExpiredError(RuntimeError):
    """The requested cursor cannot be replayed by this journal."""


ConnectionFactory = Callable[[], AbstractContextManager[sqlite3.Connection]]


class SqliteStateEventJournal:
    """Persist and replay bounded project state-change notifications."""

    def __init__(
        self,
        path: Path,
        *,
        condition: threading.Condition | None = None,
        connection_factory: ConnectionFactory | None = None,
        retention_limit: int | Callable[[], int] = 4_096,
        clock: Callable[[], str] | None = None,
        event_id_factory: Callable[[], str] | None = None,
    ) -> None:
        self.path = path
        self._condition = condition or threading.Condition(threading.RLock())
        self._connection_factory = connection_factory or self._open_connection
        self._retention_limit = retention_limit
        self._clock = clock or _utc_now
        self._event_id_factory = event_id_factory or (
            lambda: f"development-event-{secrets.token_hex(16)}"
        )

    def initialize(self) -> None:
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        with self._connection_factory() as connection:
            self.initialize_schema(connection)

    @staticmethod
    def initialize_schema(connection: sqlite3.Connection) -> None:
        connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS development_state_events (
                sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                event_id TEXT NOT NULL UNIQUE,
                project_id TEXT NOT NULL,
                event_type TEXT NOT NULL CHECK (event_type = 'state_changed'),
                payload_sha256 TEXT NOT NULL,
                occurred_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS development_state_events_project_sequence
                ON development_state_events(project_id, sequence);
            """
        )

    def append(self, connection: sqlite3.Connection, *, project_id: str | None = None) -> bool:
        table = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' "
            "AND name = 'development_state_events'"
        ).fetchone()
        if table is None:
            return False
        project_id = project_id or self._resolve_project_id(connection)
        if not project_id:
            return False
        occurred_at = self._clock()
        event_id = self._event_id_factory()
        payload_sha256 = hashlib.sha256(
            _canonical_json(
                {
                    "event_id": event_id,
                    "event_type": "state_changed",
                    "occurred_at": occurred_at,
                    "project_id": project_id,
                }
            ).encode("utf-8")
        ).hexdigest()
        connection.execute(
            "INSERT INTO development_state_events("
            "event_id, project_id, event_type, payload_sha256, occurred_at"
            ") VALUES (?, ?, 'state_changed', ?, ?)",
            (event_id, project_id, payload_sha256, occurred_at),
        )
        retention_limit = self._current_retention_limit()
        connection.execute(
            "DELETE FROM development_state_events WHERE sequence < ("
            "SELECT sequence FROM development_state_events "
            "ORDER BY sequence DESC LIMIT 1 OFFSET ?)",
            (retention_limit - 1,),
        )
        return True

    def emit(self, project_id: str) -> None:
        with self._condition:
            with self._connection_factory() as connection:
                emitted = self.append(connection, project_id=project_id)
            if emitted:
                self._condition.notify_all()

    def notify_committed_change(self) -> None:
        with self._condition:
            self._condition.notify_all()

    def read(
        self,
        *,
        after_sequence: int | None,
        limit: int,
        wait_seconds: float,
    ) -> dict[str, Any]:
        deadline = time.monotonic() + wait_seconds
        with self._condition:
            while True:
                with self._connection_factory() as connection:
                    bounds = connection.execute(
                        "SELECT MIN(sequence) AS earliest, MAX(sequence) AS latest "
                        "FROM development_state_events"
                    ).fetchone()
                    earliest = bounds["earliest"] if bounds is not None else None
                    latest = bounds["latest"] if bounds is not None else None
                    latest_sequence = int(latest or 0)
                    if after_sequence is None:
                        return self._page([], latest_sequence=latest_sequence, has_more=False)
                    if after_sequence > latest_sequence:
                        raise EventCursorExpiredError(
                            "event cursor is ahead of daemon authority"
                        )
                    if earliest is not None and after_sequence < int(earliest) - 1:
                        raise EventCursorExpiredError(
                            "event cursor is outside the replay window"
                        )
                    rows = connection.execute(
                        "SELECT * FROM development_state_events WHERE sequence > ? "
                        "ORDER BY sequence LIMIT ?",
                        (after_sequence, limit + 1),
                    ).fetchall()
                has_more = len(rows) > limit
                page = rows[:limit]
                if page:
                    return self._page(
                        [self._event_record(row) for row in page],
                        latest_sequence=latest_sequence,
                        has_more=has_more,
                    )
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return self._page([], latest_sequence=latest_sequence, has_more=False)
                self._condition.wait(remaining)

    @staticmethod
    def _page(
        events: list[dict[str, Any]],
        *,
        latest_sequence: int,
        has_more: bool,
    ) -> dict[str, Any]:
        return {
            "schema_version": "1",
            "events": events,
            "latest_sequence": latest_sequence,
            "has_more": has_more,
        }

    @staticmethod
    def _event_record(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "sequence": row["sequence"],
            "event_id": row["event_id"],
            "project_id": row["project_id"],
            "event_type": row["event_type"],
            "payload_sha256": row["payload_sha256"],
            "occurred_at": row["occurred_at"],
        }

    @staticmethod
    def _resolve_project_id(connection: sqlite3.Connection) -> str | None:
        metadata_table = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' "
            "AND name = 'development_metadata'"
        ).fetchone()
        if metadata_table is not None:
            active = connection.execute(
                "SELECT value FROM development_metadata WHERE key = 'active_project_id'"
            ).fetchone()
            if active is not None and active["value"]:
                return active["value"]
        project_table = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' "
            "AND name = 'development_projects'"
        ).fetchone()
        if project_table is None:
            return None
        latest = connection.execute(
            "SELECT project_id FROM development_projects "
            "ORDER BY updated_at DESC, project_id DESC LIMIT 1"
        ).fetchone()
        return latest["project_id"] if latest is not None else None

    def _current_retention_limit(self) -> int:
        value = self._retention_limit() if callable(self._retention_limit) else self._retention_limit
        if isinstance(value, bool) or not isinstance(value, int) or value < 1:
            raise ValueError("event retention limit must be a positive integer")
        return value

    @contextmanager
    def _open_connection(self):
        connection = sqlite3.connect(self.path, timeout=10)
        connection.row_factory = sqlite3.Row
        try:
            with connection:
                yield connection
        finally:
            connection.close()


def _canonical_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _utc_now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")
