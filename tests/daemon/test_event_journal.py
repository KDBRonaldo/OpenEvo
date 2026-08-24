from __future__ import annotations

import hashlib
import json
import threading
import time
from pathlib import Path

import pytest

from openevo.daemon.event_journal import (
    EventCursorExpiredError,
    SqliteStateEventJournal,
)


def _journal(
    path: Path,
    *,
    retention_limit: int = 4_096,
) -> SqliteStateEventJournal:
    event_ids = iter(f"development-event-{index}" for index in range(1, 100))
    timestamps = iter(f"2026-08-24T00:00:{index:02d}Z" for index in range(1, 100))
    journal = SqliteStateEventJournal(
        path,
        retention_limit=retention_limit,
        event_id_factory=lambda: next(event_ids),
        clock=lambda: next(timestamps),
    )
    journal.initialize()
    return journal


def test_journal_persists_ordered_digest_bound_events_across_restart(
    tmp_path: Path,
) -> None:
    database = tmp_path / "state.sqlite3"
    journal = _journal(database)
    journal.emit("project-a")
    journal.emit("project-b")

    page = journal.read(after_sequence=0, limit=100, wait_seconds=0)

    assert [event["sequence"] for event in page["events"]] == [1, 2]
    assert [event["project_id"] for event in page["events"]] == ["project-a", "project-b"]
    first = page["events"][0]
    expected_payload = json.dumps(
        {
            "event_id": first["event_id"],
            "event_type": "state_changed",
            "occurred_at": first["occurred_at"],
            "project_id": first["project_id"],
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    assert first["payload_sha256"] == hashlib.sha256(expected_payload).hexdigest()

    restored = SqliteStateEventJournal(database)
    restored.initialize()
    assert restored.read(after_sequence=0, limit=100, wait_seconds=0) == page


def test_journal_long_poll_wakes_only_after_committed_emit(tmp_path: Path) -> None:
    journal = _journal(tmp_path / "state.sqlite3")
    result: dict[str, object] = {}

    def wait_for_event() -> None:
        result.update(journal.read(after_sequence=0, limit=100, wait_seconds=2))

    thread = threading.Thread(target=wait_for_event)
    thread.start()
    time.sleep(0.05)
    journal.emit("project-a")
    thread.join(timeout=2)

    assert not thread.is_alive()
    assert [event["sequence"] for event in result["events"]] == [1]


def test_journal_bounds_replay_and_rejects_expired_or_ahead_cursors(
    tmp_path: Path,
) -> None:
    journal = _journal(tmp_path / "state.sqlite3", retention_limit=3)
    for _ in range(5):
        journal.emit("project-a")

    with pytest.raises(EventCursorExpiredError, match="outside the replay window"):
        journal.read(after_sequence=1, limit=100, wait_seconds=0)
    with pytest.raises(EventCursorExpiredError, match="ahead of daemon authority"):
        journal.read(after_sequence=6, limit=100, wait_seconds=0)
    page = journal.read(after_sequence=2, limit=2, wait_seconds=0)
    assert [event["sequence"] for event in page["events"]] == [3, 4]
    assert page["latest_sequence"] == 5
    assert page["has_more"] is True
