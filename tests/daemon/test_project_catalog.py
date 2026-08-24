from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from openevo.daemon.project_catalog import (
    ProjectCatalogConflictError,
    ProjectCatalogNotFoundError,
    SqliteProjectCatalog,
)


def _catalog(path: Path, timestamps: list[str]) -> SqliteProjectCatalog:
    catalog = SqliteProjectCatalog(path, clock=lambda: timestamps.pop(0))
    catalog.initialize()
    return catalog


def test_catalog_preserves_all_projects_and_active_identity_across_restart(
    tmp_path: Path,
) -> None:
    database = tmp_path / "state.sqlite3"
    catalog = _catalog(
        database,
        ["2026-08-24T01:00:00Z", "2026-08-24T02:00:00Z"],
    )

    first, first_created = catalog.create(
        {"project_id": "project-a", "display_name": "Project A", "config": {"a": 1}}
    )
    second, second_created = catalog.create(
        {"project_id": "project-b", "display_name": "Project B", "config": {"b": 2}}
    )

    assert first_created is True
    assert second_created is True
    assert first.project_id == "project-a"
    assert second.project_id == "project-b"
    restored = SqliteProjectCatalog(database)
    restored.initialize()
    state = restored.state()
    assert [project.project_id for project in state.projects] == ["project-a", "project-b"]
    assert state.active_project_id == "project-b"

    restored.activate("project-a")
    activated = restored.state()
    assert [project.project_id for project in activated.projects] == ["project-a", "project-b"]
    assert activated.active_project_id == "project-a"


def test_identical_create_is_idempotent_and_conflicting_reuse_is_rejected(
    tmp_path: Path,
) -> None:
    database = tmp_path / "state.sqlite3"
    request = {"project_id": "project-a", "display_name": "Project A", "config": {"a": 1}}
    catalog = _catalog(
        database,
        ["2026-08-24T01:00:00Z", "unused-idempotent", "unused-conflict"],
    )
    created, was_created = catalog.create(request)

    repeated, repeated_created = catalog.create(request)

    assert was_created is True
    assert repeated_created is False
    assert repeated == created
    with pytest.raises(ProjectCatalogConflictError, match="already exists"):
        catalog.create({**request, "display_name": "Different"})
    assert catalog.state().projects == (created,)


def test_update_keeps_other_projects_visible_and_missing_project_fails(
    tmp_path: Path,
) -> None:
    database = tmp_path / "state.sqlite3"
    catalog = _catalog(
        database,
        [
            "2026-08-24T01:00:00Z",
            "2026-08-24T02:00:00Z",
            "2026-08-24T03:00:00Z",
            "2026-08-24T04:00:00Z",
        ],
    )
    catalog.create({"project_id": "project-a", "display_name": "A", "config": {}})
    catalog.create({"project_id": "project-b", "display_name": "B", "config": {}})

    updated = catalog.update("project-a", {"display_name": "A2", "config": {"v": 2}})

    assert updated.display_name == "A2"
    assert updated.config == {"v": 2}
    assert [project.project_id for project in catalog.state().projects] == [
        "project-a",
        "project-b",
    ]
    with pytest.raises(ProjectCatalogNotFoundError):
        catalog.update("missing", {"display_name": "Missing", "config": {}})
    with pytest.raises(ProjectCatalogNotFoundError):
        catalog.activate("missing")


def test_catalog_reads_the_existing_development_schema_without_migration(
    tmp_path: Path,
) -> None:
    database = tmp_path / "state.sqlite3"
    with sqlite3.connect(database) as connection:
        connection.executescript(
            """
            CREATE TABLE development_metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE development_projects (
                project_id TEXT PRIMARY KEY,
                display_name TEXT NOT NULL,
                config_json TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            INSERT INTO development_projects VALUES (
                'legacy-project', 'Legacy', '{"schema_version":"2"}',
                '2026-08-23T01:00:00Z', '2026-08-23T01:00:00Z'
            );
            INSERT INTO development_metadata VALUES ('active_project_id', 'legacy-project');
            """
        )

    catalog = SqliteProjectCatalog(database)
    catalog.initialize()

    state = catalog.state()
    assert state.active_project_id == "legacy-project"
    assert state.projects[0].as_dict() == {
        "project_id": "legacy-project",
        "display_name": "Legacy",
        "config": {"schema_version": "2"},
        "created_at": "2026-08-23T01:00:00Z",
        "updated_at": "2026-08-23T01:00:00Z",
    }
