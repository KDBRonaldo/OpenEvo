from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from openevo.daemon.task_journal import SqliteTaskJournal, TaskJournalCursorError


def _journal(path: Path, *, max_log_text: int = 16_384) -> SqliteTaskJournal:
    with sqlite3.connect(path) as connection:
        connection.executescript(
            """
            CREATE TABLE development_projects (project_id TEXT PRIMARY KEY);
            CREATE TABLE development_sessions (session_id TEXT PRIMARY KEY);
            INSERT INTO development_projects VALUES ('project-a');
            INSERT INTO development_sessions VALUES ('task-a');
            """
        )
    event_ids = iter(f"task-event-{index}" for index in range(1, 10))
    journal = SqliteTaskJournal(
        path,
        max_log_text=max_log_text,
        event_id_factory=lambda: next(event_ids),
    )
    journal.initialize()
    return journal


def test_task_logs_are_chunked_paginated_and_restart_safe(tmp_path: Path) -> None:
    database = tmp_path / "state.sqlite3"
    journal = _journal(database, max_log_text=4)
    with sqlite3.connect(database) as connection:
        connection.row_factory = sqlite3.Row
        journal.append_log(
            connection,
            task_id="task-a",
            stream="stdout",
            message="abcdefghij",
            occurred_at="2026-08-24T01:00:00Z",
        )

    first, has_more = journal.read_logs("task-a", after_sequence=0, limit=2)
    assert [item["message"] for item in first] == ["abcd", "efgh"]
    assert has_more is True
    restored = SqliteTaskJournal(database)
    restored.initialize()
    later, later_has_more = restored.read_logs("task-a", after_sequence=2, limit=10)
    assert [item["message"] for item in later] == ["ij"]
    assert later_has_more is False
    with pytest.raises(TaskJournalCursorError, match="beyond"):
        restored.read_logs("task-a", after_sequence=99, limit=10)


def test_task_timeline_is_typed_idempotent_and_restart_safe(tmp_path: Path) -> None:
    database = tmp_path / "state.sqlite3"
    journal = _journal(database)
    with sqlite3.connect(database) as connection:
        connection.row_factory = sqlite3.Row
        journal.append_timeline(
            connection,
            task_id="task-a",
            project_id="project-a",
            event_type="task_admitted",
            occurred_at="2026-08-24T01:00:00Z",
        )
        journal.append_timeline(
            connection,
            task_id="task-a",
            project_id="project-a",
            event_type="task_admitted",
            occurred_at="2026-08-24T02:00:00Z",
        )
        journal.append_timeline(
            connection,
            task_id="task-a",
            project_id="project-a",
            event_type="dataset_sealed",
            occurred_at="2026-08-24T03:00:00Z",
            dataset_id="dataset-a",
            dataset_sha256="a" * 64,
        )

    items, has_more = SqliteTaskJournal(database).read_timeline(
        "task-a", after_sequence=0, limit=10
    )
    assert [item["event_type"] for item in items] == ["task_admitted", "dataset_sealed"]
    assert "dataset_id" not in items[0]
    assert items[1]["dataset_id"] == "dataset-a"
    assert has_more is False
