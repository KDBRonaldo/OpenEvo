from __future__ import annotations

from pathlib import Path

import pytest

from openevo.daemon.artifact_store import SqliteArtifactStore
from openevo.daemon.errors import RequestError
from openevo.daemon.project_catalog import SqliteProjectCatalog
from openevo.daemon.session_store import SqliteSessionStore
from openevo.daemon.task_journal import SqliteTaskJournal


def _stores(
    database: Path,
) -> tuple[SqliteArtifactStore, SqliteProjectCatalog, SqliteSessionStore]:
    def clock() -> str:
        return "2026-08-24T00:00:00Z"

    catalog = SqliteProjectCatalog(database, clock=clock)
    catalog.initialize()
    journal = SqliteTaskJournal(database, clock=clock)
    journal.initialize()
    sessions = SqliteSessionStore(
        database,
        task_journal=journal,
        clock=clock,
    )
    sessions.initialize()
    artifacts = SqliteArtifactStore(
        database,
        task_journal=journal,
        clock=clock,
    )
    artifacts.initialize()
    return artifacts, catalog, sessions


def test_artifacts_are_durable_paginated_and_context_scoped(tmp_path: Path) -> None:
    database = tmp_path / "state.sqlite3"
    artifacts, catalog, sessions = _stores(database)
    catalog.create({"project_id": "project-1", "display_name": "Project", "config": {}})
    sessions.start(
        "session-1",
        {
            "project_id": "project-1",
            "project_name": "Project",
            "task_title": "Task",
            "instruction": "Produce context",
        },
    )

    artifacts.record_dataset(
        artifact_id="dataset-1",
        project_id="project-1",
        session_id="session-1",
        uri="file:///managed/dataset-1",
        name="Dataset 1",
    )
    for artifact_id, target_id, promoted in (
        ("artifact-1", "text_memory", True),
        ("artifact-2", "skill_bundle", True),
        ("artifact-3", "agent_system", False),
    ):
        artifacts.create(
            artifact_id=artifact_id,
            project_id="project-1",
            session_id="session-1",
            target_id=target_id,
            artifact_type=target_id,
            method_id=f"{target_id}_reflector",
            renderer_kind=("file_bundle" if target_id == "skill_bundle" else "markdown"),
            documents=[
                {
                    "path": "SKILL.md" if target_id == "skill_bundle" else "memory.md",
                    "media_type": "text/markdown",
                    "content": artifact_id,
                }
            ],
            manifest={"source": "dataset-1"},
            previous_artifact_id=None,
            promoted=promoted,
        )

    first = artifacts.page(
        project_id="project-1",
        task_id=None,
        after_artifact_id=None,
        limit=1,
    )
    second = artifacts.page(
        project_id="project-1",
        task_id=None,
        after_artifact_id=first.next_cursor,
        limit=10,
    )

    assert [item["artifact_id"] for item in first.items] == ["artifact-1"]
    assert [item["artifact_id"] for item in second.items] == [
        "artifact-2",
        "artifact-3",
    ]
    assert [item["artifact_id"] for item in artifacts.latest_context("project-1")] == [
        "artifact-2",
        "artifact-1",
    ]
    assert (
        SqliteArtifactStore(
            database,
            task_journal=SqliteTaskJournal(database),
        ).dataset("dataset-1")["session_id"]
        == "session-1"
    )

    with pytest.raises(RequestError, match="cursor"):
        artifacts.page(
            project_id="project-1",
            task_id=None,
            after_artifact_id="missing",
            limit=10,
        )
