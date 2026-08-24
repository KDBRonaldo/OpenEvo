from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from openevo.daemon.session_store import (
    SessionCancellationRequested,
    SessionConflictError,
    SqliteSessionStore,
)
from openevo.daemon.task_journal import SqliteTaskJournal


def _open(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(path)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    return connection


def _session_authority(
    path: Path,
    *,
    timestamps: list[str] | None = None,
) -> tuple[SqliteSessionStore, SqliteTaskJournal]:
    event_ids = iter(f"task-event-{index}" for index in range(1, 100))
    journal = SqliteTaskJournal(
        path,
        event_id_factory=lambda: next(event_ids),
    )
    clock_values = iter(timestamps or ["2026-08-25T00:00:00Z"] * 20)
    store = SqliteSessionStore(
        path,
        task_journal=journal,
        clock=lambda: next(clock_values),
    )
    with _open(path) as connection:
        connection.executescript(
            """
            CREATE TABLE development_projects (
                project_id TEXT PRIMARY KEY,
                display_name TEXT NOT NULL
            );
            INSERT INTO development_projects VALUES ('project-a', 'Project A');
            INSERT INTO development_projects VALUES ('project-b', 'Project B');
            CREATE TABLE development_evolution_artifacts_v2 (
                artifact_id TEXT PRIMARY KEY,
                project_id TEXT NOT NULL,
                target_id TEXT NOT NULL,
                artifact_type TEXT NOT NULL,
                promoted INTEGER NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE TABLE development_dataset_artifacts (
                artifact_id TEXT PRIMARY KEY,
                session_id TEXT NOT NULL UNIQUE
            );
            """
        )
        store.initialize_schema(connection)
        store.migrate_schema(connection)
        journal.initialize_schema(connection)
    return store, journal


def _request(project_id: str = "project-a") -> dict[str, str]:
    suffix = project_id.removeprefix("project-").upper()
    return {
        "project_id": project_id,
        "project_name": f"Project {suffix}",
        "task_title": f"Task {suffix}",
        "instruction": f"Run project {suffix}.",
    }


def test_completed_session_and_selected_context_survive_restart(tmp_path: Path) -> None:
    database = tmp_path / "state.sqlite3"
    store, journal = _session_authority(
        database,
        timestamps=[
            "2026-08-25T01:00:00Z",
            "2026-08-25T02:00:00Z",
            "2026-08-25T03:00:00Z",
        ],
    )
    with _open(database) as connection:
        connection.executemany(
            "INSERT INTO development_evolution_artifacts_v2 VALUES (?, ?, ?, ?, ?, ?)",
            [
                ("memory-old", "project-a", "text_memory", "text_memory", 1, "01"),
                ("memory-new", "project-a", "text_memory", "text_memory", 1, "02"),
                ("report-a", "project-a", "report", "report", 1, "03"),
                ("memory-b", "project-b", "text_memory", "text_memory", 1, "04"),
            ],
        )

    store.start("session-a", _request())
    assert store.get("session-a")["context_artifact_ids"] == ["memory-new"]
    store.complete(
        "session-a",
        {
            "response": "Completed response",
            "model": "gpt-5.5",
            "duration_ms": 42,
            "logs": ["Harness completed."],
            "workspace_changes": [{"path": "result.txt", "kind": "added"}],
            "runtime_activation": {"context_id": "context-a"},
        },
    )

    restored = SqliteSessionStore(database, task_journal=journal)
    record = restored.get("session-a")
    assert record["state"] == "completed"
    assert record["response"] == "Completed response"
    assert record["workspace_changes"] == [{"kind": "added", "path": "result.txt"}]
    assert record["runtime_activation"] == {"context_id": "context-a"}
    logs, _ = journal.read_logs("session-a", after_sequence=0, limit=20)
    assert logs[-1]["stream"] == "transcript"
    assert logs[-1]["message"] == "Completed response"

    store.start("session-b", _request("project-b"))
    assert store.get("session-b")["context_artifact_ids"] == ["memory-b"]
    with _open(database) as connection:
        rows = connection.execute(
            "SELECT session_id, project_id FROM development_sessions "
            "ORDER BY session_id"
        ).fetchall()
    assert [tuple(row) for row in rows] == [
        ("session-a", "project-a"),
        ("session-b", "project-b"),
    ]


def test_cancellation_is_durable_and_wins_completion_race(tmp_path: Path) -> None:
    database = tmp_path / "state.sqlite3"
    store, journal = _session_authority(database)
    store.start("session-cancel", _request())

    assert store.request_cancellation("session-cancel")["state"] == "cancelling"
    with pytest.raises(SessionCancellationRequested):
        store.complete(
            "session-cancel",
            {
                "response": "too late",
                "model": "gpt-5.5",
                "duration_ms": 1,
                "logs": [],
            },
        )
    store.cancel("session-cancel", [{"path": "partial.txt", "kind": "added"}])

    restored = SqliteSessionStore(database, task_journal=journal)
    record = restored.get("session-cancel")
    assert record["state"] == "cancelled"
    assert record["error"] is None
    assert record["workspace_changes"] == [
        {"kind": "added", "path": "partial.txt"}
    ]
    with pytest.raises(SessionConflictError, match="terminal"):
        restored.request_cancellation("session-cancel")


def test_running_session_recovers_as_failed_with_journal_entry(tmp_path: Path) -> None:
    database = tmp_path / "state.sqlite3"
    store, journal = _session_authority(database)
    store.start("session-interrupted", _request())

    with _open(database) as connection:
        interrupted = store.recover_interrupted(
            connection,
            occurred_at="2026-08-25T03:00:00Z",
        )
        store.append_recovery_logs(
            connection,
            interrupted,
            occurred_at="2026-08-25T03:00:00Z",
        )

    assert interrupted == ("session-interrupted",)
    record = SqliteSessionStore(database, task_journal=journal).get(
        "session-interrupted"
    )
    assert record["state"] == "failed"
    assert "restarted" in record["error"]
    logs, _ = journal.read_logs("session-interrupted", after_sequence=0, limit=20)
    assert "restarted" in logs[-1]["message"]


def test_legacy_session_table_is_upgraded_without_losing_rows(tmp_path: Path) -> None:
    database = tmp_path / "state.sqlite3"
    with _open(database) as connection:
        connection.executescript(
            """
            CREATE TABLE development_projects (project_id TEXT PRIMARY KEY);
            INSERT INTO development_projects VALUES ('project-a');
            CREATE TABLE development_sessions (
                session_id TEXT PRIMARY KEY,
                project_id TEXT NOT NULL,
                task_title TEXT NOT NULL,
                instruction TEXT NOT NULL,
                response TEXT,
                model TEXT,
                state TEXT NOT NULL,
                duration_ms INTEGER,
                logs_json TEXT NOT NULL,
                error TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            INSERT INTO development_sessions VALUES (
                'legacy-session', 'project-a', 'Legacy', 'Preserve me',
                'done', 'gpt-5.5', 'completed', 5, '[]', NULL, '01', '02'
            );
            """
        )
    journal = SqliteTaskJournal(database)
    store = SqliteSessionStore(database, task_journal=journal)
    store.initialize()

    with _open(database) as connection:
        columns = {
            row["name"]
            for row in connection.execute("PRAGMA table_info(development_sessions)")
        }
        row = connection.execute(
            "SELECT * FROM development_sessions WHERE session_id = 'legacy-session'"
        ).fetchone()
    assert {
        "selected_evolution_json",
        "evolution_errors_json",
        "workspace_changes_json",
        "context_artifact_ids_json",
        "runtime_activation_json",
        "cancellation_requested",
        "terminal_kind",
    } <= columns
    assert row["instruction"] == "Preserve me"
    assert row["runtime_activation_json"] == "null"
