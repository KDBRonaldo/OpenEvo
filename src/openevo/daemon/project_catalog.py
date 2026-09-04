"""Durable project catalog extracted from the proven remote daemon path.

The catalog deliberately preserves the existing ``development_projects`` and
``development_metadata`` SQLite layout.  During the incremental migration it
can share the legacy daemon's connection factory, so project mutations remain
in the same transaction and continue to produce the existing state events.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from collections.abc import Callable
from contextlib import AbstractContextManager, contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


class ProjectCatalogConflictError(RuntimeError):
    """A project identity was reused for different immutable input."""


class ProjectCatalogNotFoundError(KeyError):
    """The requested project is absent from the durable catalog."""


@dataclass(frozen=True)
class PersistedProject:
    project_id: str
    display_name: str
    config: dict[str, Any]
    created_at: str
    updated_at: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "project_id": self.project_id,
            "display_name": self.display_name,
            "config": self.config,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }


@dataclass(frozen=True)
class ProjectCatalogState:
    projects: tuple[PersistedProject, ...]
    active_project_id: str | None


ConnectionFactory = Callable[[], AbstractContextManager[sqlite3.Connection]]


class SqliteProjectCatalog:
    """Own project persistence while allowing a shared daemon transaction boundary."""

    def __init__(
        self,
        path: Path,
        *,
        lock: Any | None = None,
        connection_factory: ConnectionFactory | None = None,
        clock: Callable[[], str] | None = None,
    ) -> None:
        self.path = path
        self._lock = lock or threading.RLock()
        self._connection_factory = connection_factory or self._open_connection
        self._clock = clock or _utc_now

    def initialize(self) -> None:
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        with self._lock, self._connection_factory() as connection:
            self.initialize_schema(connection)

    @staticmethod
    def initialize_schema(connection: sqlite3.Connection) -> None:
        connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS development_metadata (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS development_projects (
                project_id TEXT PRIMARY KEY,
                display_name TEXT NOT NULL,
                config_json TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS development_deleted_projects (
                project_id TEXT PRIMARY KEY REFERENCES development_projects(project_id),
                action_id TEXT NOT NULL UNIQUE,
                deleted_at TEXT NOT NULL
            );
            """
        )

    def state(self) -> ProjectCatalogState:
        with self._lock, self._connection_factory() as connection:
            return self.read_state(connection)

    @classmethod
    def read_state(cls, connection: sqlite3.Connection) -> ProjectCatalogState:
        projects = tuple(
            cls._project_from_row(row)
            for row in connection.execute(
                "SELECT project.* FROM development_projects AS project "
                "LEFT JOIN development_deleted_projects AS deleted "
                "ON deleted.project_id = project.project_id "
                "WHERE deleted.project_id IS NULL "
                "ORDER BY project.created_at, project.project_id"
            )
        )
        active_row = connection.execute(
            "SELECT value FROM development_metadata WHERE key = 'active_project_id'"
        ).fetchone()
        active_project_id = active_row["value"] if active_row is not None else None
        project_ids = {project.project_id for project in projects}
        if active_project_id not in project_ids:
            active_project_id = projects[-1].project_id if projects else None
        return ProjectCatalogState(
            projects=projects,
            active_project_id=active_project_id,
        )

    def create(self, request: dict[str, Any]) -> tuple[PersistedProject, bool]:
        now = self._clock()
        project_id = request["project_id"]
        display_name = request["display_name"]
        config = request["config"]
        with self._lock, self._connection_factory() as connection:
            try:
                connection.execute(
                    """
                    INSERT INTO development_projects(
                        project_id, display_name, config_json, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?)
                    """,
                    (project_id, display_name, _canonical_json(config), now, now),
                )
            except sqlite3.IntegrityError as exc:
                existing_row = connection.execute(
                    "SELECT * FROM development_projects WHERE project_id = ?",
                    (project_id,),
                ).fetchone()
                if existing_row is None:
                    raise
                existing = self._project_from_row(existing_row)
                deleted = connection.execute(
                    "SELECT 1 FROM development_deleted_projects WHERE project_id = ?",
                    (project_id,),
                ).fetchone()
                if deleted is not None:
                    raise ProjectCatalogConflictError(
                        "deleted project_id cannot be reused"
                    ) from exc
                if existing.display_name != display_name or existing.config != config:
                    raise ProjectCatalogConflictError("project_id already exists") from exc
                self._set_active(connection, project_id)
                return existing, False
            self._set_active(connection, project_id)
        return PersistedProject(
            project_id=project_id,
            display_name=display_name,
            config=config,
            created_at=now,
            updated_at=now,
        ), True

    def update(self, project_id: str, request: dict[str, Any]) -> PersistedProject:
        now = self._clock()
        with self._lock, self._connection_factory() as connection:
            existing_row = connection.execute(
                "SELECT * FROM development_projects WHERE project_id = ? "
                "AND NOT EXISTS ("
                "SELECT 1 FROM development_deleted_projects "
                "WHERE development_deleted_projects.project_id = development_projects.project_id"
                ")",
                (project_id,),
            ).fetchone()
            if existing_row is None:
                raise ProjectCatalogNotFoundError(project_id)
            existing = self._project_from_row(existing_row)
            existing_execution = existing.config.get("execution")
            requested_execution = request["config"].get("execution")
            existing_mode = (
                existing_execution.get("mode") if isinstance(existing_execution, dict) else None
            )
            requested_mode = (
                requested_execution.get("mode") if isinstance(requested_execution, dict) else None
            )
            if (
                isinstance(existing_mode, str)
                and isinstance(requested_mode, str)
                and requested_mode != existing_mode
            ):
                raise ProjectCatalogConflictError(
                    "project execution mode is immutable; create a new project to use another mode"
                )
            cursor = connection.execute(
                """
                UPDATE development_projects
                SET display_name = ?, config_json = ?, updated_at = ?
                WHERE project_id = ?
                  AND NOT EXISTS (
                      SELECT 1 FROM development_deleted_projects
                      WHERE development_deleted_projects.project_id = development_projects.project_id
                  )
                """,
                (
                    request["display_name"],
                    _canonical_json(request["config"]),
                    now,
                    project_id,
                ),
            )
            if cursor.rowcount != 1:
                raise ProjectCatalogNotFoundError(project_id)
            row = connection.execute(
                "SELECT * FROM development_projects WHERE project_id = ?",
                (project_id,),
            ).fetchone()
            if row is None:  # pragma: no cover - guarded by the transaction above
                raise ProjectCatalogNotFoundError(project_id)
            return self._project_from_row(row)

    def activate(self, project_id: str) -> None:
        with self._lock, self._connection_factory() as connection:
            if not self.exists(connection, project_id):
                raise ProjectCatalogNotFoundError(project_id)
            self._set_active(connection, project_id)

    def delete(self, project_id: str, action_id: str) -> str | None:
        """Persistently hide one Project while retaining its historical identity."""

        now = self._clock()
        with self._lock, self._connection_factory() as connection:
            project = connection.execute(
                "SELECT 1 FROM development_projects WHERE project_id = ?",
                (project_id,),
            ).fetchone()
            if project is None:
                raise ProjectCatalogNotFoundError(project_id)
            deleted = connection.execute(
                "SELECT action_id FROM development_deleted_projects WHERE project_id = ?",
                (project_id,),
            ).fetchone()
            if deleted is not None:
                if deleted["action_id"] != action_id:
                    raise ProjectCatalogNotFoundError(project_id)
            else:
                try:
                    connection.execute(
                        "INSERT INTO development_deleted_projects(project_id, action_id, deleted_at) "
                        "VALUES (?, ?, ?)",
                        (project_id, action_id, now),
                    )
                except sqlite3.IntegrityError as exc:
                    raise ProjectCatalogConflictError(
                        "action_id is already bound to another Project deletion"
                    ) from exc

            state = self.read_state(connection)
            active_row = connection.execute(
                "SELECT value FROM development_metadata WHERE key = 'active_project_id'"
            ).fetchone()
            persisted_active = active_row["value"] if active_row is not None else None
            visible_ids = {candidate.project_id for candidate in state.projects}
            if persisted_active == project_id or persisted_active not in visible_ids:
                replacement = state.projects[-1].project_id if state.projects else None
                if replacement is None:
                    connection.execute(
                        "DELETE FROM development_metadata WHERE key = 'active_project_id'"
                    )
                else:
                    self._set_active(connection, replacement)
                return replacement
            return state.active_project_id

    def require(self, project_id: str) -> None:
        with self._lock, self._connection_factory() as connection:
            if not self.exists(connection, project_id):
                raise ProjectCatalogNotFoundError(project_id)

    @staticmethod
    def exists(connection: sqlite3.Connection, project_id: str) -> bool:
        return (
            connection.execute(
                "SELECT 1 FROM development_projects AS project "
                "LEFT JOIN development_deleted_projects AS deleted "
                "ON deleted.project_id = project.project_id "
                "WHERE project.project_id = ? AND deleted.project_id IS NULL",
                (project_id,),
            ).fetchone()
            is not None
        )

    @staticmethod
    def _set_active(connection: sqlite3.Connection, project_id: str) -> None:
        connection.execute(
            """
            INSERT INTO development_metadata(key, value) VALUES ('active_project_id', ?)
            ON CONFLICT(key) DO UPDATE SET value = excluded.value
            """,
            (project_id,),
        )

    @staticmethod
    def _project_from_row(row: sqlite3.Row) -> PersistedProject:
        config = json.loads(row["config_json"])
        if not isinstance(config, dict):
            raise RuntimeError("persisted project config is not an object")
        return PersistedProject(
            project_id=row["project_id"],
            display_name=row["display_name"],
            config=config,
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

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
