"""Authoritative loopback OpenEvo product daemon.

This is the formal composition root for Project, Session, workspace, artifact,
Agent Runner, and Evolution services.  It must never be exposed directly to a
network: bind it to loopback and reach it only through an SSH tunnel.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import hashlib
import hmac
import json
import os
from pathlib import Path, PurePosixPath
import re
import secrets
import shutil
import sqlite3
import threading
from urllib.parse import parse_qs, quote, urlsplit
from contextlib import contextmanager
from datetime import datetime, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Iterator, Sequence

from pydantic import ValidationError


from openevo.backend.evolution_runtime import codex_development_runtime_adapter
from openevo.backend.harness_adapter import (
    HarnessCancellation,
    HarnessRunCancelled,
)
from openevo.backend.contracts.v2 import models as core_v2
from openevo.daemon.agent_runner import (
    AgentSessionExecutor,
    CodexAgentRunner,
)
from openevo.daemon.artifact_store import SqliteArtifactStore
from openevo.daemon.event_journal import (
    EventCursorExpiredError,
    SqliteStateEventJournal,
)
from openevo.daemon.errors import (
    AgentRunError,
    EvolutionRunError,
    RequestError,
    StateConflictError,
)
from openevo.daemon.evolution_orchestrator import (
    EvolutionOrchestrator,
    development_registry_snapshot,
)
from openevo.daemon.project_catalog import (
    ProjectCatalogConflictError,
    SqliteProjectCatalog,
)
from openevo.daemon.session_store import (
    SessionCancellationRequested,
    SessionConflictError,
    SqliteSessionStore,
)
from openevo.daemon.session_runtime import (
    SessionExecutionConflictError,
    SessionExecutionManager,
)
from openevo.daemon.task_journal import (
    SqliteTaskJournal,
    TaskJournalCursorError,
)
from openevo.daemon.workspace_store import (
    MAX_WORKSPACE_ENTRIES,
    MAX_WORKSPACE_UPLOAD_FILE_BYTES,
    ProjectWorkspaceStore,
)
from openevo.daemon.contracts import (
    DevelopmentArtifactPageV2,
    DevelopmentArtifactV2,
    DevelopmentCapabilitiesV2,
    DevelopmentEvolutionJobPageV2,
    DevelopmentEvolutionJobRetryV2,
    DevelopmentEvolutionJobV2,
    DevelopmentEvolutionRunApplyV2,
    DevelopmentEvolutionRunCreateV2,
    DevelopmentEvolutionRunPageV2,
    DevelopmentEvolutionRunV2,
    DevelopmentProjectActivateV2,
    DevelopmentProjectAuthorityV2,
    DevelopmentProjectCreateV2,
    DevelopmentProjectUpdateV2,
    DevelopmentStateV2,
    DevelopmentTaskCancelV2,
    DevelopmentTaskCreateV2,
    DevelopmentTaskObservationPageV2,
    DevelopmentTaskObservationV2,
    DevelopmentTaskPresentationPageV2,
    DevelopmentTaskPresentationV2,
    DevelopmentTaskTimelinePageV2,
    DevelopmentWorkspaceDeleteV2,
    DevelopmentWorkspaceMutationV2,
    DevelopmentWorkspacePageV2,
)


MAX_REQUEST_BYTES = 256 * 1024
MAX_CAPTURE_BYTES = 2 * 1024 * 1024
MAX_AGENT_WORKSPACE_CONTEXT_BYTES = 512 * 1024
MAX_DEVELOPMENT_STATE_EVENTS = 4_096
MAX_DEVELOPMENT_EVENT_PAGE = 100
MAX_DEVELOPMENT_EVENT_WAIT_SECONDS = 10.0
DEFAULT_TIMEOUT_SECONDS = 300
ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
ALLOWED_REQUEST_FIELDS = {
    "schema_version",
    "project_id",
    "project_head_id",
    "project_name",
    "task_title",
    "instruction",
}
ALLOWED_PROJECT_FIELDS = {"schema_version", "project_id", "display_name", "config"}
ALLOWED_PROJECT_UPDATE_FIELDS = {"schema_version", "display_name", "config"}
PROJECT_PATH_PATTERN = re.compile(r"^/openevo-dev-agent/v1/projects/([^/]+)$")
ACTIVATE_PATH_PATTERN = re.compile(r"^/openevo-dev-agent/v1/projects/([^/]+)/activate$")
SESSION_PATH_PATTERN = re.compile(r"^/openevo-dev-agent/v1/sessions/([^/]+)$")
SESSION_CANCEL_PATH_PATTERN = re.compile(r"^/openevo-dev-agent/v1/sessions/([^/]+)/cancel$")
EVOLUTION_JOB_RETRY_PATH_PATTERN = re.compile(
    r"^/openevo-dev-agent/v1/evolution-jobs/([^/]+)/retry$"
)
EVOLUTION_RUN_APPLY_PATH_PATTERN = re.compile(
    r"^/openevo-dev-agent/v1/evolution-runs/([^/]+)/apply$"
)
WORKSPACE_FILES_PATH_PATTERN = re.compile(
    r"^/openevo-dev-agent/v1/projects/([^/]+)/workspace/files$"
)
DEVELOPMENT_EVENTS_PATH = "/openevo-dev-agent/v1/events"
DAEMON_V2_DEVELOPMENT_EVENTS_PATH = "/v2/development/events"
DAEMON_V2_TASKS_PATH = "/v2/tasks"
DAEMON_V2_TASK_PATH_PATTERN = re.compile(r"^/v2/tasks/([^/]+)$")
DAEMON_V2_TASK_LOGS_PATH_PATTERN = re.compile(r"^/v2/tasks/([^/]+)/logs$")
DAEMON_V2_TASK_TIMELINE_PATH_PATTERN = re.compile(r"^/v2/tasks/([^/]+)/timeline$")
DAEMON_V2_TASK_ARTIFACTS_PATH_PATTERN = re.compile(r"^/v2/tasks/([^/]+)/artifacts$")
DAEMON_V2_DEVELOPMENT_TASKS_PATH = "/v2/development/tasks"
DAEMON_V2_DEVELOPMENT_TASK_PATH_PATTERN = re.compile(r"^/v2/development/tasks/([^/]+)$")
DAEMON_V2_DEVELOPMENT_TASK_CANCEL_PATH_PATTERN = re.compile(
    r"^/v2/development/tasks/([^/]+)/cancel$"
)
DAEMON_V2_DEVELOPMENT_STATE_PATH = "/v2/development/state"
DAEMON_V2_DEVELOPMENT_CAPABILITIES_PATH = "/v2/development/capabilities"
DAEMON_V2_DEVELOPMENT_PROJECTS_PATH = "/v2/development/projects"
DAEMON_V2_DEVELOPMENT_PROJECT_PATH_PATTERN = re.compile(r"^/v2/development/projects/([^/]+)$")
DAEMON_V2_DEVELOPMENT_PROJECT_ACTIVATE_PATH_PATTERN = re.compile(
    r"^/v2/development/projects/([^/]+)/activate$"
)
DAEMON_V2_ARTIFACT_CONTENT_PATH_PATTERN = re.compile(r"^/v2/artifacts/([^/]+)/content$")
DAEMON_V2_ARTIFACT_PATH_PATTERN = re.compile(r"^/v2/artifacts/([^/]+)$")
DAEMON_V2_DEVELOPMENT_ARTIFACTS_PATH = "/v2/development/artifacts"
DAEMON_V2_DEVELOPMENT_ARTIFACT_PATH_PATTERN = re.compile(r"^/v2/development/artifacts/([^/]+)$")
DAEMON_V2_DEVELOPMENT_EVOLUTION_RUNS_PATH = "/v2/development/evolution-runs"
DAEMON_V2_DEVELOPMENT_EVOLUTION_JOBS_PATH = "/v2/development/evolution-jobs"
DAEMON_V2_DEVELOPMENT_EVOLUTION_JOB_RETRY_PATH_PATTERN = re.compile(
    r"^/v2/development/evolution-jobs/([^/]+)/retry$"
)
DAEMON_V2_DEVELOPMENT_EVOLUTION_JOB_PATH_PATTERN = re.compile(
    r"^/v2/development/evolution-jobs/([^/]+)$"
)
DAEMON_V2_DEVELOPMENT_EVOLUTION_RUN_APPLY_PATH_PATTERN = re.compile(
    r"^/v2/development/evolution-runs/([^/]+)/apply$"
)
DAEMON_V2_DEVELOPMENT_EVOLUTION_RUN_PATH_PATTERN = re.compile(
    r"^/v2/development/evolution-runs/([^/]+)$"
)
DAEMON_V2_WORKSPACE_PATH_PATTERN = re.compile(r"^/v2/projects/([^/]+)/workspace$")
DAEMON_V2_WORKSPACE_FILES_PATH_PATTERN = re.compile(r"^/v2/projects/([^/]+)/workspace/files$")
MAX_DAEMON_V2_LOG_PAGE = 100
MAX_DAEMON_V2_TASK_PAGE = 100
MAX_DAEMON_V2_WORKSPACE_PAGE = 100
MAX_DAEMON_V2_ARTIFACT_PAGE = 100
MAX_DAEMON_V2_DEVELOPMENT_ARTIFACT_PAGE = 5
MAX_DAEMON_V2_EVOLUTION_RUN_PAGE = 25
MAX_DAEMON_V2_EVOLUTION_JOB_PAGE = 25
MAX_DAEMON_V2_TASK_PRESENTATION_PAGE = 25
MAX_DAEMON_V2_LOG_TEXT = 16_384


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def canonical_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def normalize_selected_evolution(value: object) -> list[dict[str, Any]]:
    """Upgrade pre-capability session selections to the generic selection shape."""

    if not isinstance(value, list):
        return []
    normalized: list[dict[str, Any]] = []
    for selection in value:
        if not isinstance(selection, dict):
            continue
        target_id = selection.get("target_id")
        method = selection.get("method")
        config = selection.get("config", {})
        if not isinstance(target_id, str) or not ID_PATTERN.fullmatch(target_id):
            continue
        if not isinstance(method, str) or not ID_PATTERN.fullmatch(method):
            continue
        if not isinstance(config, dict):
            config = {}
        normalized.append(
            {
                "target_id": target_id,
                "method": method,
                "config": config,
            }
        )
    return normalized


def validate_evolution_run_request(payload: object) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise RequestError("request body must be a JSON object")
    unknown = set(payload) - {"schema_version", "project_id", "session_ids", "selections"}
    if unknown:
        raise RequestError(f"unknown request fields: {', '.join(sorted(unknown))}")
    if payload.get("schema_version") != "1":
        raise RequestError("schema_version must be '1'")
    project_id = payload.get("project_id")
    if not isinstance(project_id, str) or not ID_PATTERN.fullmatch(project_id):
        raise RequestError("project_id is invalid")
    session_ids = payload.get("session_ids")
    if not isinstance(session_ids, list) or not session_ids or len(session_ids) > 128:
        raise RequestError("session_ids must contain between 1 and 128 sessions")
    if any(not isinstance(value, str) or not ID_PATTERN.fullmatch(value) for value in session_ids):
        raise RequestError("session_ids contains an invalid session id")
    if len(set(session_ids)) != len(session_ids):
        raise RequestError("session_ids must not contain duplicates")
    selections = normalize_selected_evolution(payload.get("selections"))
    if not selections:
        raise RequestError("selections must contain at least one enabled Evolution method")
    if len(selections) != len(payload.get("selections", [])):
        raise RequestError("selections contains an invalid Evolution method")
    if len({selection["target_id"] for selection in selections}) != len(selections):
        raise RequestError("selections must contain at most one method per target")
    return {
        "project_id": project_id,
        "session_ids": session_ids,
        "selections": selections,
    }


def validate_project_request(payload: object, *, updating: bool = False) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise RequestError("request body must be a JSON object")
    allowed = ALLOWED_PROJECT_UPDATE_FIELDS if updating else ALLOWED_PROJECT_FIELDS
    unknown = set(payload) - allowed
    if unknown:
        raise RequestError(f"unknown request fields: {', '.join(sorted(unknown))}")
    if payload.get("schema_version") != "1":
        raise RequestError("schema_version must be '1'")
    result: dict[str, Any] = {}
    if not updating:
        project_id = payload.get("project_id")
        if not isinstance(project_id, str) or not ID_PATTERN.fullmatch(project_id):
            raise RequestError("project_id is invalid")
        result["project_id"] = project_id
    display_name = payload.get("display_name")
    if not isinstance(display_name, str) or not display_name.strip() or len(display_name) > 200:
        raise RequestError("display_name must be a non-empty string of at most 200 characters")
    config = payload.get("config")
    if not isinstance(config, dict):
        raise RequestError("config must be a JSON object")
    if len(canonical_json(config).encode("utf-8")) > 192 * 1024:
        raise RequestError("config is too large")
    result["display_name"] = display_name.strip()
    result["config"] = config
    return result


class DevelopmentStateStore:
    """Small SQLite authority for the self-hosted Project/Session loop."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = threading.RLock()
        self._event_condition = threading.Condition(self._lock)
        self.workspaces = ProjectWorkspaceStore(path.parent / "workspaces")
        self.event_journal = SqliteStateEventJournal(
            path,
            condition=self._event_condition,
            connection_factory=lambda: self._connection(emit_event=False),
            retention_limit=lambda: MAX_DEVELOPMENT_STATE_EVENTS,
            clock=utc_now,
        )
        self.task_journal = SqliteTaskJournal(
            path,
            lock=self._lock,
            connection_factory=self._connection,
            max_log_text=MAX_DAEMON_V2_LOG_TEXT,
            clock=utc_now,
        )
        self.session_store = SqliteSessionStore(
            path,
            task_journal=self.task_journal,
            lock=self._lock,
            connection_factory=self._connection,
            clock=utc_now,
            selection_normalizer=normalize_selected_evolution,
        )
        self.project_catalog = SqliteProjectCatalog(
            path,
            lock=self._lock,
            connection_factory=self._connection,
            clock=utc_now,
        )
        self.artifact_store = SqliteArtifactStore(
            path,
            task_journal=self.task_journal,
            lock=self._lock,
            connection_factory=self._connection,
            clock=utc_now,
        )
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        try:
            path.parent.chmod(0o700)
        except OSError:
            pass
        with self._connection() as connection:
            self.project_catalog.initialize_schema(connection)
            self.event_journal.initialize_schema(connection)
            self.session_store.initialize_schema(connection)
            self.artifact_store.initialize_schema(connection)
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS development_artifacts (
                    artifact_id TEXT PRIMARY KEY,
                    project_id TEXT NOT NULL REFERENCES development_projects(project_id),
                    session_id TEXT NOT NULL UNIQUE REFERENCES development_sessions(session_id),
                    artifact_type TEXT NOT NULL CHECK (artifact_type = 'text_memory'),
                    method TEXT NOT NULL CHECK (method = 'text_memory_reflector'),
                    content TEXT NOT NULL,
                    content_sha256 TEXT NOT NULL,
                    byte_size INTEGER NOT NULL,
                    previous_artifact_id TEXT REFERENCES development_artifacts(artifact_id),
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS development_artifacts_project_created
                    ON development_artifacts(project_id, created_at, artifact_id);
                CREATE TABLE IF NOT EXISTS development_document_artifacts (
                    artifact_id TEXT PRIMARY KEY,
                    project_id TEXT NOT NULL REFERENCES development_projects(project_id),
                    session_id TEXT NOT NULL REFERENCES development_sessions(session_id),
                    artifact_type TEXT NOT NULL CHECK (
                        artifact_type IN ('text_memory', 'skill_bundle', 'agent_system')
                    ),
                    method TEXT NOT NULL CHECK (
                        method IN (
                            'text_memory_reflector',
                            'skill_bundle_reflector',
                            'agent_system_reflector'
                        )
                    ),
                    content_path TEXT NOT NULL,
                    content TEXT NOT NULL,
                    content_sha256 TEXT NOT NULL,
                    byte_size INTEGER NOT NULL,
                    previous_artifact_id TEXT,
                    created_at TEXT NOT NULL,
                    UNIQUE(session_id, artifact_type)
                );
                CREATE INDEX IF NOT EXISTS development_document_artifacts_project_created
                    ON development_document_artifacts(project_id, artifact_type, created_at, artifact_id);
                CREATE TABLE IF NOT EXISTS development_evolution_runs (
                    run_id TEXT PRIMARY KEY,
                    action_id TEXT NOT NULL UNIQUE,
                    project_id TEXT NOT NULL REFERENCES development_projects(project_id),
                    source_session_ids_json TEXT NOT NULL,
                    selections_json TEXT NOT NULL,
                    state TEXT NOT NULL CHECK (
                        state IN ('running', 'candidate_ready', 'applied', 'failed')
                    ),
                    artifact_ids_json TEXT NOT NULL DEFAULT '[]',
                    error TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS development_evolution_runs_project_created
                    ON development_evolution_runs(project_id, created_at, run_id);
                CREATE TABLE IF NOT EXISTS development_evolution_jobs (
                    job_id TEXT PRIMARY KEY,
                    session_id TEXT NOT NULL REFERENCES development_sessions(session_id),
                    run_id TEXT,
                    target_id TEXT NOT NULL,
                    method_id TEXT NOT NULL,
                    requested_method_id TEXT NOT NULL,
                    resolver_input_artifact_ids_json TEXT NOT NULL DEFAULT '[]',
                    previous_artifact_id TEXT,
                    config_json TEXT NOT NULL,
                    state TEXT NOT NULL CHECK (state IN ('queued', 'running', 'completed', 'failed')),
                    artifact_ids_json TEXT NOT NULL DEFAULT '[]',
                    error TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(run_id, target_id)
                );
                CREATE INDEX IF NOT EXISTS development_evolution_jobs_session
                    ON development_evolution_jobs(session_id, created_at, job_id);
                CREATE TABLE IF NOT EXISTS development_evolution_job_attempts (
                    attempt_id TEXT PRIMARY KEY,
                    action_id TEXT,
                    job_id TEXT NOT NULL REFERENCES development_evolution_jobs(job_id),
                    ordinal INTEGER NOT NULL CHECK (ordinal > 0),
                    state TEXT NOT NULL CHECK (
                        state IN ('queued', 'running', 'completed', 'failed', 'cancelled')
                    ),
                    stage TEXT NOT NULL,
                    artifact_ids_json TEXT NOT NULL DEFAULT '[]',
                    error_code TEXT,
                    error_message TEXT,
                    logs_json TEXT NOT NULL DEFAULT '[]',
                    created_at TEXT NOT NULL,
                    started_at TEXT,
                    completed_at TEXT,
                    updated_at TEXT NOT NULL,
                    UNIQUE(job_id, ordinal)
                );
                CREATE INDEX IF NOT EXISTS development_evolution_attempts_job
                    ON development_evolution_job_attempts(job_id, ordinal);
                """
            )
            self.task_journal.initialize_schema(connection)
            self.session_store.migrate_schema(connection)
            self.artifact_store.migrate_schema(connection)
            evolution_run_columns = {
                row["name"]
                for row in connection.execute("PRAGMA table_info(development_evolution_runs)")
            }
            if "action_id" not in evolution_run_columns:
                connection.execute(
                    "ALTER TABLE development_evolution_runs ADD COLUMN action_id TEXT"
                )
                connection.execute(
                    "UPDATE development_evolution_runs "
                    "SET action_id = 'legacy-' || run_id WHERE action_id IS NULL"
                )
            connection.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS "
                "development_evolution_runs_action_id "
                "ON development_evolution_runs(action_id)"
            )
            job_columns = {
                row["name"]
                for row in connection.execute("PRAGMA table_info(development_evolution_jobs)")
            }
            if "previous_artifact_id" not in job_columns:
                connection.execute(
                    "ALTER TABLE development_evolution_jobs ADD COLUMN previous_artifact_id TEXT"
                )
            if "requested_method_id" not in job_columns:
                connection.execute(
                    "ALTER TABLE development_evolution_jobs ADD COLUMN requested_method_id TEXT"
                )
                connection.execute(
                    "UPDATE development_evolution_jobs "
                    "SET requested_method_id = method_id "
                    "WHERE requested_method_id IS NULL"
                )
            if "resolver_input_artifact_ids_json" not in job_columns:
                connection.execute(
                    "ALTER TABLE development_evolution_jobs "
                    "ADD COLUMN resolver_input_artifact_ids_json TEXT NOT NULL DEFAULT '[]'"
                )
            if "run_id" not in job_columns:
                connection.execute("ALTER TABLE development_evolution_jobs ADD COLUMN run_id TEXT")
            job_table_sql = connection.execute(
                "SELECT sql FROM sqlite_master WHERE type = 'table' "
                "AND name = 'development_evolution_jobs'"
            ).fetchone()["sql"]
            if "UNIQUE(session_id, target_id)" in job_table_sql:
                connection.executescript(
                    """
                    CREATE TABLE development_evolution_jobs_rebuilt (
                        job_id TEXT PRIMARY KEY,
                        session_id TEXT NOT NULL REFERENCES development_sessions(session_id),
                        run_id TEXT,
                        target_id TEXT NOT NULL,
                        method_id TEXT NOT NULL,
                        requested_method_id TEXT NOT NULL,
                        resolver_input_artifact_ids_json TEXT NOT NULL DEFAULT '[]',
                        previous_artifact_id TEXT,
                        config_json TEXT NOT NULL,
                        state TEXT NOT NULL CHECK (state IN ('queued', 'running', 'completed', 'failed')),
                        artifact_ids_json TEXT NOT NULL DEFAULT '[]',
                        error TEXT,
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL,
                        UNIQUE(run_id, target_id)
                    );
                    INSERT INTO development_evolution_jobs_rebuilt
                    SELECT job_id, session_id, run_id, target_id, method_id,
                           requested_method_id, resolver_input_artifact_ids_json,
                           previous_artifact_id, config_json, state,
                           artifact_ids_json, error, created_at, updated_at
                    FROM development_evolution_jobs;
                    CREATE TABLE development_evolution_job_attempts_rebuilt (
                        attempt_id TEXT PRIMARY KEY,
                        action_id TEXT,
                        job_id TEXT NOT NULL REFERENCES development_evolution_jobs_rebuilt(job_id),
                        ordinal INTEGER NOT NULL CHECK (ordinal > 0),
                        state TEXT NOT NULL CHECK (
                            state IN ('queued', 'running', 'completed', 'failed', 'cancelled')
                        ),
                        stage TEXT NOT NULL,
                        artifact_ids_json TEXT NOT NULL DEFAULT '[]',
                        error_code TEXT,
                        error_message TEXT,
                        logs_json TEXT NOT NULL DEFAULT '[]',
                        created_at TEXT NOT NULL,
                        started_at TEXT,
                        completed_at TEXT,
                        updated_at TEXT NOT NULL,
                        UNIQUE(job_id, ordinal)
                    );
                    INSERT INTO development_evolution_job_attempts_rebuilt
                    SELECT attempt_id, NULL, job_id, ordinal, state, stage,
                           artifact_ids_json, error_code, error_message, logs_json,
                           created_at, started_at, completed_at, updated_at
                    FROM development_evolution_job_attempts;
                    DROP TABLE development_evolution_job_attempts;
                    DROP TABLE development_evolution_jobs;
                    ALTER TABLE development_evolution_jobs_rebuilt
                        RENAME TO development_evolution_jobs;
                    ALTER TABLE development_evolution_job_attempts_rebuilt
                        RENAME TO development_evolution_job_attempts;
                    CREATE INDEX development_evolution_jobs_session
                        ON development_evolution_jobs(session_id, created_at, job_id);
                    CREATE INDEX development_evolution_attempts_job
                        ON development_evolution_job_attempts(job_id, ordinal);
                    """
                )
            attempt_columns = {
                row["name"]
                for row in connection.execute(
                    "PRAGMA table_info(development_evolution_job_attempts)"
                )
            }
            if "action_id" not in attempt_columns:
                connection.execute(
                    "ALTER TABLE development_evolution_job_attempts ADD COLUMN action_id TEXT"
                )
            connection.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS "
                "development_evolution_attempts_action_id "
                "ON development_evolution_job_attempts(action_id) "
                "WHERE action_id IS NOT NULL"
            )
            connection.execute(
                """
                INSERT OR IGNORE INTO development_document_artifacts(
                    artifact_id, project_id, session_id, artifact_type, method, content_path,
                    content, content_sha256, byte_size, previous_artifact_id, created_at
                )
                SELECT artifact_id, project_id, session_id, artifact_type, method, 'memory.md',
                       content, content_sha256, byte_size, previous_artifact_id, created_at
                FROM development_artifacts
                """
            )
            connection.execute(
                """
                INSERT OR IGNORE INTO development_evolution_artifacts_v2(
                    artifact_id, project_id, session_id, target_id, artifact_type,
                    method_id, renderer_kind, documents_json, manifest_json,
                    content_sha256, byte_size, previous_artifact_id, created_at
                )
                SELECT artifact_id, project_id, session_id, artifact_type, artifact_type,
                       method,
                       CASE artifact_type WHEN 'skill_bundle' THEN 'file_bundle' ELSE 'markdown' END,
                       json_array(json_object('path', content_path, 'media_type', 'text/markdown',
                                              'content', content)),
                       json_object('content_path', content_path),
                       content_sha256, byte_size, previous_artifact_id, created_at
                FROM development_document_artifacts
                """
            )
            restarted_at = utc_now()
            connection.execute(
                """
                UPDATE development_evolution_jobs
                SET state = 'failed', error = ?, updated_at = ?
                WHERE state IN ('queued', 'running')
                """,
                (
                    "Development daemon restarted before this evolution job completed.",
                    restarted_at,
                ),
            )
            connection.execute(
                """
                UPDATE development_evolution_job_attempts
                SET state = 'failed', error_code = 'daemon_restarted',
                    error_message = ?, completed_at = ?, updated_at = ?
                WHERE state IN ('queued', 'running')
                """,
                (
                    "Development daemon restarted before this evolution attempt completed.",
                    restarted_at,
                    restarted_at,
                ),
            )
            connection.execute(
                """
                INSERT OR IGNORE INTO development_evolution_job_attempts(
                    attempt_id, job_id, ordinal, state, stage, artifact_ids_json,
                    error_code, error_message, logs_json, created_at, started_at,
                    completed_at, updated_at
                )
                SELECT job_id || '-attempt-1', job_id, 1,
                       CASE state WHEN 'completed' THEN 'completed' ELSE 'failed' END,
                       CASE state WHEN 'completed' THEN 'completed' ELSE 'unknown' END,
                       artifact_ids_json,
                       CASE WHEN state = 'completed' THEN NULL ELSE 'legacy_failure' END,
                       error,
                       '[]', created_at, created_at,
                       CASE WHEN state IN ('completed', 'failed') THEN updated_at ELSE NULL END,
                       updated_at
                FROM development_evolution_jobs
                """
            )
            interrupted_sessions = self.session_store.recover_interrupted(
                connection,
                occurred_at=restarted_at,
            )
            self._backfill_task_journals(connection)
            self.session_store.append_recovery_logs(
                connection,
                interrupted_sessions,
                occurred_at=restarted_at,
            )
            project_ids = [
                project.project_id
                for project in self.project_catalog.read_state(connection).projects
            ]
        for project_id in project_ids:
            self.workspaces.ensure_project(project_id)
        try:
            path.chmod(0o600)
        except OSError:
            pass

    @contextmanager
    def _connection(self, *, emit_event: bool = True) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.path, timeout=10)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        emitted = False
        try:
            with connection:
                initial_changes = connection.total_changes
                yield connection
                if emit_event and connection.total_changes > initial_changes:
                    emitted = self.event_journal.append(connection)
        finally:
            connection.close()
        if emitted:
            self.event_journal.notify_committed_change()

    def _emit_project_event(self, project_id: str) -> None:
        self.event_journal.emit(project_id)

    def read_events(
        self,
        *,
        after_sequence: int | None,
        limit: int,
        wait_seconds: float,
    ) -> dict[str, Any]:
        return self.event_journal.read(
            after_sequence=after_sequence,
            limit=limit,
            wait_seconds=wait_seconds,
        )

    def snapshot(self) -> dict[str, Any]:
        with self._lock, self._connection() as connection:
            catalog_state = self.project_catalog.read_state(connection)
            projects = [project.as_dict() for project in catalog_state.projects]
            evidence_session_ids = {
                row["session_id"]
                for row in connection.execute(
                    "SELECT session_id FROM development_dataset_artifacts"
                )
            }
            sessions = [
                self._session_record(
                    row,
                    evolution_evidence_ready=row["session_id"] in evidence_session_ids,
                )
                for row in connection.execute(
                    "SELECT * FROM development_sessions ORDER BY created_at, session_id"
                )
            ]
            artifacts = [
                self._artifact_record(row)
                for row in connection.execute(
                    "SELECT * FROM development_evolution_artifacts_v2 ORDER BY created_at, artifact_id"
                )
            ]
            attempt_rows = connection.execute(
                "SELECT * FROM development_evolution_job_attempts ORDER BY job_id, ordinal"
            ).fetchall()
            attempts_by_job: dict[str, list[dict[str, Any]]] = {}
            for attempt_row in attempt_rows:
                attempts_by_job.setdefault(attempt_row["job_id"], []).append(
                    self._attempt_record(attempt_row)
                )
            jobs = [
                self._job_record(row, attempts_by_job.get(row["job_id"], []))
                for row in connection.execute(
                    "SELECT * FROM development_evolution_jobs ORDER BY created_at, job_id"
                )
            ]
            evolution_runs = [
                self._evolution_run_record(row)
                for row in connection.execute(
                    "SELECT * FROM development_evolution_runs ORDER BY created_at, run_id"
                )
            ]
        active_project_id = catalog_state.active_project_id
        return {
            "schema_version": "1",
            "active_project_id": active_project_id,
            "projects": projects,
            "sessions": sessions,
            "artifacts": artifacts,
            "evolution_jobs": jobs,
            "evolution_runs": evolution_runs,
            "workspaces": [
                self.workspaces.snapshot(project["project_id"]) for project in projects
            ],
        }

    def state_v2(self) -> DevelopmentStateV2:
        with self._lock, self._connection() as connection:
            catalog_state = self.project_catalog.read_state(connection)
            projects = [
                DevelopmentProjectAuthorityV2(
                    project_id=project.project_id,
                    display_name=project.display_name,
                    config=project.config,
                    created_at=project.created_at,
                    updated_at=project.updated_at,
                )
                for project in catalog_state.projects
            ]
        return DevelopmentStateV2(
            active_project_id=catalog_state.active_project_id,
            projects=projects,
        )

    def create_project(self, request: dict[str, Any]) -> dict[str, Any]:
        try:
            project, created = self.project_catalog.create(request)
        except ProjectCatalogConflictError as exc:
            raise StateConflictError(str(exc)) from exc
        if created:
            self.workspaces.ensure_project(request["project_id"])
        return project.as_dict()

    def workspace_path(self, project_id: str) -> Path:
        self.project_catalog.require(project_id)
        return self.workspaces.project_path(project_id)

    def workspace_snapshot(self, project_id: str) -> dict[str, Any]:
        self.workspace_path(project_id)
        return self.workspaces.snapshot(project_id)

    def workspace_page_v2(
        self,
        project_id: str,
        *,
        after_path: str | None,
        expected_manifest_sha256: str | None,
        limit: int,
    ) -> DevelopmentWorkspacePageV2:
        self.workspace_path(project_id)
        authority = self.workspaces.authoritative_snapshot_v2(project_id)
        manifest_sha256 = authority["manifest_sha256"]
        if expected_manifest_sha256 is not None and expected_manifest_sha256 != manifest_sha256:
            raise StateConflictError("workspace changed while its inventory was paged")
        entries = authority["entries"]
        start = 0
        if after_path is not None:
            positions = [
                index for index, entry in enumerate(entries) if entry["path"] == after_path
            ]
            if len(positions) != 1:
                raise RequestError("workspace cursor is not part of this inventory")
            start = positions[0] + 1
        selected = entries[start : start + limit]
        has_more = start + len(selected) < len(entries)
        return DevelopmentWorkspacePageV2.model_validate(
            {
                "schema_version": "2",
                "project_id": project_id,
                "manifest_sha256": manifest_sha256,
                "items": [{"schema_version": "2", **entry} for entry in selected],
                "next_cursor": selected[-1]["path"] if selected and has_more else None,
                "has_more": has_more,
                "truncated": authority["truncated"],
            }
        )

    def workspace_mutation_v2(
        self,
        project_id: str,
        relative_path: str,
    ) -> DevelopmentWorkspaceMutationV2:
        authority = self.workspaces.authoritative_snapshot_v2(project_id)
        entry = next(
            (
                candidate
                for candidate in authority["entries"]
                if candidate["kind"] == "file" and candidate["path"] == relative_path
            ),
            None,
        )
        if entry is None:
            raise KeyError(relative_path)
        return DevelopmentWorkspaceMutationV2.model_validate(
            {
                "schema_version": "2",
                "project_id": project_id,
                "manifest_sha256": authority["manifest_sha256"],
                "entry": {"schema_version": "2", **entry},
            }
        )

    def apply_workspace_mutations(self, project_id: str, mutations: object) -> None:
        self.workspace_path(project_id)
        self.workspaces.apply_mutations(project_id, mutations)
        self._emit_project_event(project_id)

    def upload_workspace_file(
        self,
        project_id: str,
        relative_path: object,
        payload: bytes,
        *,
        overwrite: bool,
    ) -> dict[str, Any]:
        self.workspace_path(project_id)
        result = self.workspaces.upload_file(
            project_id,
            relative_path,
            payload,
            overwrite=overwrite,
        )
        self._emit_project_event(project_id)
        return result

    def download_workspace_file(
        self,
        project_id: str,
        relative_path: object,
    ) -> tuple[bytes, str, str]:
        self.workspace_path(project_id)
        return self.workspaces.read_file(project_id, relative_path)

    def delete_workspace_file(self, project_id: str, relative_path: object) -> str:
        self.workspace_path(project_id)
        deleted_path = self.workspaces.delete_file(project_id, relative_path)
        self._emit_project_event(project_id)
        return deleted_path

    def workspace_delete_v2(
        self,
        project_id: str,
        deleted_path: str,
    ) -> DevelopmentWorkspaceDeleteV2:
        authority = self.workspaces.authoritative_snapshot_v2(project_id)
        return DevelopmentWorkspaceDeleteV2.model_validate(
            {
                "schema_version": "2",
                "project_id": project_id,
                "manifest_sha256": authority["manifest_sha256"],
                "deleted_path": deleted_path,
            }
        )

    def update_project(self, project_id: str, request: dict[str, Any]) -> dict[str, Any]:
        return self.project_catalog.update(project_id, request).as_dict()

    def activate_project(self, project_id: str) -> None:
        self.project_catalog.activate(project_id)

    def _append_task_log_v2(
        self,
        connection: sqlite3.Connection,
        *,
        task_id: str,
        stream: str,
        message: object,
        occurred_at: str | None = None,
    ) -> None:
        self.task_journal.append_log(
            connection,
            task_id=task_id,
            stream=stream,
            message=message,
            occurred_at=occurred_at,
        )

    def _append_task_timeline_v2(
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
        self.task_journal.append_timeline(
            connection,
            task_id=task_id,
            project_id=project_id,
            event_type=event_type,
            occurred_at=occurred_at,
            dataset_id=dataset_id,
            dataset_sha256=dataset_sha256,
        )

    def _backfill_task_journals(self, connection: sqlite3.Connection) -> None:
        self.task_journal.backfill(connection)

    def start_session(self, session_id: str, request: dict[str, str]) -> None:
        try:
            self.session_store.start(session_id, request)
        except SessionConflictError as exc:
            raise StateConflictError(str(exc)) from exc

    def complete_session(self, session_id: str, result: dict[str, Any]) -> None:
        try:
            self.session_store.complete(session_id, result)
        except SessionCancellationRequested as exc:
            raise HarnessRunCancelled(str(exc)) from exc

    def append_session_log(self, session_id: str, message: str) -> list[str]:
        return self.session_store.append_log(session_id, message)

    def get_session(self, session_id: str) -> dict[str, Any]:
        return self.session_store.get(session_id)

    def task_observations_v2(
        self,
        *,
        project_id: str | None = None,
        after_task_id: str | None = None,
        limit: int = MAX_DAEMON_V2_TASK_PAGE,
    ) -> DevelopmentTaskObservationPageV2:
        with self._lock, self._connection() as connection:
            if project_id is None:
                rows = connection.execute(
                    "SELECT * FROM development_sessions ORDER BY created_at, session_id"
                ).fetchall()
            else:
                rows = connection.execute(
                    "SELECT * FROM development_sessions WHERE project_id = ? "
                    "ORDER BY created_at, session_id",
                    (project_id,),
                ).fetchall()
        observations = [self._task_observation_v2(row) for row in rows]
        start = 0
        if after_task_id is not None:
            try:
                start = next(
                    index + 1
                    for index, observation in enumerate(observations)
                    if observation.task_id == after_task_id
                )
            except StopIteration as exc:
                raise RequestError("task cursor is not part of this collection") from exc
        page = observations[start : start + limit]
        has_more = start + len(page) < len(observations)
        return DevelopmentTaskObservationPageV2(
            items=page,
            next_cursor=page[-1].task_id if has_more and page else None,
            has_more=has_more,
        )

    def task_observation_v2(self, task_id: str) -> DevelopmentTaskObservationV2:
        with self._lock, self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM development_sessions WHERE session_id = ?",
                (task_id,),
            ).fetchone()
        if row is None:
            raise KeyError(task_id)
        return self._task_observation_v2(row)

    def task_presentations_v2(
        self,
        *,
        project_id: str,
        after_task_id: str | None = None,
        limit: int = MAX_DAEMON_V2_TASK_PRESENTATION_PAGE,
    ) -> DevelopmentTaskPresentationPageV2:
        with self._lock, self._connection() as connection:
            if (
                connection.execute(
                    "SELECT 1 FROM development_projects WHERE project_id = ?", (project_id,)
                ).fetchone()
                is None
            ):
                raise KeyError(project_id)
            rows = connection.execute(
                "SELECT * FROM development_sessions WHERE project_id = ? "
                "ORDER BY created_at, session_id",
                (project_id,),
            ).fetchall()
            evidence_ids = {
                row["session_id"]
                for row in connection.execute(
                    "SELECT session_id FROM development_dataset_artifacts WHERE project_id = ?",
                    (project_id,),
                ).fetchall()
            }
        presentations = [
            self._task_presentation_v2(
                row,
                evolution_evidence_ready=row["session_id"] in evidence_ids,
            )
            for row in rows
        ]
        start = 0
        if after_task_id is not None:
            try:
                start = next(
                    index + 1
                    for index, item in enumerate(presentations)
                    if item.task_id == after_task_id
                )
            except StopIteration as exc:
                raise RequestError("Task presentation cursor is not part of this project") from exc
        page = presentations[start : start + limit]
        has_more = start + len(page) < len(presentations)
        return DevelopmentTaskPresentationPageV2(
            items=page,
            next_cursor=page[-1].task_id if has_more and page else None,
            has_more=has_more,
        )

    def task_presentation_v2(self, task_id: str) -> DevelopmentTaskPresentationV2:
        with self._lock, self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM development_sessions WHERE session_id = ?", (task_id,)
            ).fetchone()
            evidence_ready = (
                connection.execute(
                    "SELECT 1 FROM development_dataset_artifacts WHERE session_id = ?",
                    (task_id,),
                ).fetchone()
                is not None
            )
        if row is None:
            raise KeyError(task_id)
        return self._task_presentation_v2(row, evolution_evidence_ready=evidence_ready)

    def task_logs_v2(
        self,
        task_id: str,
        *,
        after_sequence: int,
        limit: int,
    ) -> core_v2.LogPageV2:
        try:
            rows, has_more = self.task_journal.read_logs(
                task_id,
                after_sequence=after_sequence,
                limit=limit,
            )
        except TaskJournalCursorError as exc:
            raise RequestError(str(exc)) from exc
        page = [core_v2.LogEntryV2.model_validate(row) for row in rows]
        return core_v2.LogPageV2(
            items=page,
            next_cursor=str(page[-1].sequence) if has_more and page else None,
            has_more=has_more,
        )

    def task_timeline_v2(
        self,
        task_id: str,
        *,
        after_sequence: int,
        limit: int,
    ) -> DevelopmentTaskTimelinePageV2:
        try:
            rows, has_more = self.task_journal.read_timeline(
                task_id,
                after_sequence=after_sequence,
                limit=limit,
            )
        except TaskJournalCursorError as exc:
            raise RequestError(str(exc)) from exc
        items = [{"schema_version": "2", **row} for row in rows]
        next_cursor = str(items[-1]["sequence"]) if has_more and items else None
        return DevelopmentTaskTimelinePageV2.model_validate(
            {
                "schema_version": "2",
                "items": items,
                "next_cursor": next_cursor,
                "has_more": has_more,
            }
        )

    def cancellation_requested(self, session_id: str) -> bool:
        return self.session_store.cancellation_requested(session_id)

    def request_session_cancellation(self, session_id: str) -> dict[str, Any]:
        try:
            return self.session_store.request_cancellation(session_id)
        except SessionConflictError as exc:
            raise StateConflictError(str(exc)) from exc

    def cancel_session(
        self,
        session_id: str,
        workspace_changes: list[dict[str, Any]] | None = None,
    ) -> None:
        self.session_store.cancel(session_id, workspace_changes)

    def record_evolution_errors(
        self,
        session_id: str,
        errors: list[dict[str, str]],
    ) -> None:
        with self._lock, self._connection() as connection:
            connection.execute(
                "UPDATE development_sessions SET evolution_errors_json = ?, updated_at = ? "
                "WHERE session_id = ?",
                (canonical_json(errors), utc_now(), session_id),
            )

    def set_evolution_error(
        self,
        session_id: str,
        *,
        target_id: str,
        method: str,
        message: str | None,
    ) -> None:
        with self._lock, self._connection() as connection:
            row = connection.execute(
                "SELECT evolution_errors_json FROM development_sessions WHERE session_id = ?",
                (session_id,),
            ).fetchone()
            if row is None:
                raise KeyError(session_id)
            errors = [
                item
                for item in json.loads(row["evolution_errors_json"])
                if item.get("target_id") != target_id
            ]
            if message is not None:
                errors.append(
                    {
                        "target_id": target_id,
                        "method": method,
                        "message": message,
                    }
                )
            connection.execute(
                "UPDATE development_sessions SET evolution_errors_json = ?, updated_at = ? "
                "WHERE session_id = ?",
                (canonical_json(errors), utc_now(), session_id),
            )

    def latest_artifact(self, project_id: str, target_id: str) -> dict[str, Any] | None:
        return self.artifact_store.latest(project_id, target_id)

    def project_config(self, project_id: str) -> dict[str, Any]:
        with self._lock, self._connection() as connection:
            row = connection.execute(
                "SELECT config_json FROM development_projects WHERE project_id = ?",
                (project_id,),
            ).fetchone()
        if row is None:
            raise KeyError(project_id)
        return json.loads(row["config_json"])

    def project(self, project_id: str) -> dict[str, Any]:
        with self._lock, self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM development_projects WHERE project_id = ?",
                (project_id,),
            ).fetchone()
        if row is None:
            raise KeyError(project_id)
        return {
            "project_id": row["project_id"],
            "display_name": row["display_name"],
            "config": json.loads(row["config_json"]),
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }

    def latest_memory(self, project_id: str) -> dict[str, Any] | None:
        return self.latest_artifact(project_id, "text_memory")

    def latest_context_artifacts(self, project_id: str) -> list[dict[str, Any]]:
        return self.artifact_store.latest_context(project_id)

    def record_dataset_artifact(
        self,
        *,
        artifact_id: str,
        project_id: str,
        session_id: str,
        uri: str,
        name: str,
        manifest_sha256: str | None = None,
    ) -> None:
        self.artifact_store.record_dataset(
            artifact_id=artifact_id,
            project_id=project_id,
            session_id=session_id,
            uri=uri,
            name=name,
            manifest_sha256=manifest_sha256,
        )

    def dataset_artifacts(self, project_id: str) -> list[dict[str, str]]:
        return self.artifact_store.datasets(project_id)

    def completed_sessions(self) -> list[dict[str, Any]]:
        """Return successful Sessions that can be sealed as transcript evidence."""

        with self._lock, self._connection() as connection:
            rows = connection.execute(
                "SELECT * FROM development_sessions "
                "WHERE state = 'completed' AND response IS NOT NULL "
                "ORDER BY created_at, session_id"
            ).fetchall()
        return [self._session_record(row) for row in rows]

    def start_evolution_run(
        self,
        run_id: str,
        request: dict[str, Any],
    ) -> dict[str, Any]:
        now = utc_now()
        session_ids = request["session_ids"]
        action_id = request.get("action_id", f"legacy-{run_id}")
        with self._lock, self._connection() as connection:
            existing = connection.execute(
                "SELECT * FROM development_evolution_runs WHERE action_id = ?",
                (action_id,),
            ).fetchone()
            if existing is not None:
                record = self._evolution_run_record(existing)
                if (
                    record["project_id"] != request["project_id"]
                    or record["source_session_ids"] != session_ids
                    or record["selections"] != request["selections"]
                ):
                    raise StateConflictError(
                        "Evolution action_id is already bound to another request"
                    )
                return record
            if (
                connection.execute(
                    "SELECT 1 FROM development_projects WHERE project_id = ?",
                    (request["project_id"],),
                ).fetchone()
                is None
            ):
                raise KeyError(request["project_id"])
            rows = connection.execute(
                f"SELECT session_id, project_id, state FROM development_sessions "
                f"WHERE session_id IN ({','.join('?' for _ in session_ids)})",
                tuple(session_ids),
            ).fetchall()
            by_id = {row["session_id"]: row for row in rows}
            if set(by_id) != set(session_ids):
                raise RequestError("one or more selected Sessions do not exist")
            if any(row["project_id"] != request["project_id"] for row in rows):
                raise RequestError("all selected Sessions must belong to the active Project")
            if any(row["state"] != "completed" for row in rows):
                raise StateConflictError(
                    "only completed Sessions can be used as Evolution evidence"
                )
            missing_dataset = connection.execute(
                f"SELECT session_id FROM development_dataset_artifacts "
                f"WHERE session_id IN ({','.join('?' for _ in session_ids)})",
                tuple(session_ids),
            ).fetchall()
            available_session_ids = {row["session_id"] for row in missing_dataset}
            if available_session_ids != set(session_ids):
                unavailable = [
                    session_id
                    for session_id in session_ids
                    if session_id not in available_session_ids
                ]
                raise StateConflictError(
                    "Sessions unavailable as Evolution evidence: " + ", ".join(unavailable)
                )
            running = connection.execute(
                "SELECT 1 FROM development_evolution_runs "
                "WHERE project_id = ? AND state = 'running' LIMIT 1",
                (request["project_id"],),
            ).fetchone()
            if running is not None:
                raise StateConflictError(
                    "another Evolution Run is already running for this Project"
                )
            connection.execute(
                """
                INSERT INTO development_evolution_runs(
                    run_id, action_id, project_id, source_session_ids_json, selections_json,
                    state, artifact_ids_json, error, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, 'running', '[]', NULL, ?, ?)
                """,
                (
                    run_id,
                    action_id,
                    request["project_id"],
                    canonical_json(session_ids),
                    canonical_json(request["selections"]),
                    now,
                    now,
                ),
            )
            row = connection.execute(
                "SELECT * FROM development_evolution_runs WHERE run_id = ?",
                (run_id,),
            ).fetchone()
        return self._evolution_run_record(row)

    def evolution_run_for_action(
        self,
        request: dict[str, Any],
    ) -> dict[str, Any] | None:
        action_id = request.get("action_id")
        if action_id is None:
            return None
        with self._lock, self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM development_evolution_runs WHERE action_id = ?",
                (action_id,),
            ).fetchone()
        if row is None:
            return None
        record = self._evolution_run_record(row)
        if (
            record["project_id"] != request["project_id"]
            or record["source_session_ids"] != request["session_ids"]
            or record["selections"] != request["selections"]
        ):
            raise StateConflictError("Evolution action_id is already bound to another request")
        return record

    def evolution_run_v2(self, run_id: str) -> DevelopmentEvolutionRunV2:
        with self._lock, self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM development_evolution_runs WHERE run_id = ?",
                (run_id,),
            ).fetchone()
        if row is None:
            raise KeyError(run_id)
        return self._development_evolution_run_v2(
            self._evolution_run_record(row, include_action_id=True)
        )

    def evolution_run_page_v2(
        self,
        *,
        project_id: str,
        after_run_id: str | None,
        limit: int,
    ) -> DevelopmentEvolutionRunPageV2:
        with self._lock, self._connection() as connection:
            if (
                connection.execute(
                    "SELECT 1 FROM development_projects WHERE project_id = ?",
                    (project_id,),
                ).fetchone()
                is None
            ):
                raise KeyError(project_id)
            parameters: list[object] = [project_id]
            cursor_clause = ""
            if after_run_id is not None:
                cursor = connection.execute(
                    "SELECT created_at, run_id FROM development_evolution_runs "
                    "WHERE project_id = ? AND run_id = ?",
                    (project_id, after_run_id),
                ).fetchone()
                if cursor is None:
                    raise RequestError("Evolution Run cursor is not part of this Project")
                cursor_clause = "AND (created_at > ? OR (created_at = ? AND run_id > ?)) "
                parameters.extend([cursor["created_at"], cursor["created_at"], cursor["run_id"]])
            rows = connection.execute(
                "SELECT * FROM development_evolution_runs WHERE project_id = ? "
                + cursor_clause
                + "ORDER BY created_at, run_id LIMIT ?",
                (*parameters, limit + 1),
            ).fetchall()
        has_more = len(rows) > limit
        selected = rows[:limit]
        items = [
            self._development_evolution_run_v2(
                self._evolution_run_record(row, include_action_id=True)
            )
            for row in selected
        ]
        return DevelopmentEvolutionRunPageV2(
            items=items,
            next_cursor=items[-1].run_id if has_more and items else None,
            has_more=has_more,
        )

    def finish_evolution_run(
        self,
        run_id: str,
        *,
        artifact_ids: list[str],
        error: str | None,
    ) -> dict[str, Any]:
        now = utc_now()
        effective_error = error
        if effective_error is None and not artifact_ids:
            effective_error = "Evolution Run produced no candidate artifacts"
        with self._lock, self._connection() as connection:
            cursor = connection.execute(
                "UPDATE development_evolution_runs SET state = ?, artifact_ids_json = ?, "
                "error = ?, updated_at = ? WHERE run_id = ? AND state = 'running'",
                (
                    "failed" if effective_error is not None else "candidate_ready",
                    canonical_json(artifact_ids),
                    effective_error,
                    now,
                    run_id,
                ),
            )
            if cursor.rowcount != 1:
                raise StateConflictError("Evolution Run is no longer running")
            row = connection.execute(
                "SELECT * FROM development_evolution_runs WHERE run_id = ?",
                (run_id,),
            ).fetchone()
        return self._evolution_run_record(row)

    def apply_evolution_run(self, run_id: str) -> dict[str, Any]:
        now = utc_now()
        with self._lock, self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM development_evolution_runs WHERE run_id = ?",
                (run_id,),
            ).fetchone()
            if row is None:
                raise KeyError(run_id)
            if row["state"] == "applied":
                return self._evolution_run_record(row)
            if row["state"] != "candidate_ready":
                raise StateConflictError("only a candidate-ready Evolution Run can be applied")
            artifact_ids = json.loads(row["artifact_ids_json"])
            if not artifact_ids:
                raise StateConflictError("Evolution Run produced no candidate artifacts")
            artifacts = connection.execute(
                f"SELECT artifact_id, project_id, target_id, artifact_type "
                f"FROM development_evolution_artifacts_v2 "
                f"WHERE artifact_id IN ({','.join('?' for _ in artifact_ids)})",
                tuple(artifact_ids),
            ).fetchall()
            if {item["artifact_id"] for item in artifacts} != set(artifact_ids):
                raise StateConflictError("Evolution Run candidate artifacts are incomplete")
            if any(item["project_id"] != row["project_id"] for item in artifacts):
                raise StateConflictError("Evolution Run candidate belongs to another Project")
            runtime_artifacts = [item for item in artifacts if item["artifact_type"] != "report"]
            if runtime_artifacts:
                target_ids = sorted({item["target_id"] for item in runtime_artifacts})
                runtime_artifact_ids = [item["artifact_id"] for item in runtime_artifacts]
                connection.execute(
                    f"UPDATE development_evolution_artifacts_v2 SET promoted = 0 "
                    f"WHERE project_id = ? AND target_id IN "
                    f"({','.join('?' for _ in target_ids)})",
                    (row["project_id"], *target_ids),
                )
                connection.execute(
                    f"UPDATE development_evolution_artifacts_v2 SET promoted = 1 "
                    f"WHERE artifact_id IN "
                    f"({','.join('?' for _ in runtime_artifact_ids)})",
                    tuple(runtime_artifact_ids),
                )
            connection.execute(
                "UPDATE development_evolution_runs SET state = 'applied', updated_at = ? "
                "WHERE run_id = ?",
                (now, run_id),
            )
            updated = connection.execute(
                "SELECT * FROM development_evolution_runs WHERE run_id = ?",
                (run_id,),
            ).fetchone()
        return self._evolution_run_record(updated)

    def start_evolution_job(
        self,
        *,
        job_id: str,
        session_id: str,
        run_id: str | None = None,
        target_id: str,
        method_id: str,
        requested_method_id: str,
        resolver_input_artifact_ids: list[str],
        previous_artifact_id: str | None,
        config: dict[str, Any],
    ) -> dict[str, Any]:
        now = utc_now()
        attempt_id = f"{job_id}-attempt-1"
        with self._lock, self._connection() as connection:
            connection.execute(
                """
                INSERT INTO development_evolution_jobs(
                    job_id, session_id, run_id, target_id, method_id, requested_method_id,
                    resolver_input_artifact_ids_json, previous_artifact_id, config_json, state,
                    artifact_ids_json, error, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'running', '[]', NULL, ?, ?)
                """,
                (
                    job_id,
                    session_id,
                    run_id,
                    target_id,
                    method_id,
                    requested_method_id,
                    canonical_json(resolver_input_artifact_ids),
                    previous_artifact_id,
                    canonical_json(config),
                    now,
                    now,
                ),
            )
            connection.execute(
                """
                INSERT INTO development_evolution_job_attempts(
                    attempt_id, job_id, ordinal, state, stage, artifact_ids_json,
                    error_code, error_message, logs_json, created_at, started_at,
                    completed_at, updated_at
                ) VALUES (?, ?, 1, 'running', 'input_resolution', '[]', NULL, NULL,
                          ?, ?, ?, NULL, ?)
                """,
                (
                    attempt_id,
                    job_id,
                    canonical_json(["Resolving the fixed Evolution Job inputs."]),
                    now,
                    now,
                    now,
                ),
            )
            row = connection.execute(
                "SELECT * FROM development_evolution_job_attempts WHERE attempt_id = ?",
                (attempt_id,),
            ).fetchone()
        return self._attempt_record(row)

    def evolution_retry_for_action(
        self,
        job_id: str,
        action_id: str,
    ) -> dict[str, Any] | None:
        with self._lock, self._connection() as connection:
            bound = connection.execute(
                "SELECT job_id FROM development_evolution_job_attempts WHERE action_id = ?",
                (action_id,),
            ).fetchone()
        if bound is None:
            return None
        if bound["job_id"] != job_id:
            raise StateConflictError("Evolution retry action_id is already bound to another Job")
        return self.get_evolution_job(job_id)

    def start_evolution_retry(
        self,
        job_id: str,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        job, attempt, _created = self.start_evolution_retry_v2(
            job_id,
            f"legacy-retry-{secrets.token_hex(16)}",
        )
        return job, attempt

    def start_evolution_retry_v2(
        self,
        job_id: str,
        action_id: str,
    ) -> tuple[dict[str, Any], dict[str, Any], bool]:
        now = utc_now()
        with self._lock, self._connection() as connection:
            bound = connection.execute(
                "SELECT * FROM development_evolution_job_attempts WHERE action_id = ?",
                (action_id,),
            ).fetchone()
            if bound is not None:
                if bound["job_id"] != job_id:
                    raise StateConflictError(
                        "Evolution retry action_id is already bound to another Job"
                    )
                job = connection.execute(
                    "SELECT * FROM development_evolution_jobs WHERE job_id = ?",
                    (job_id,),
                ).fetchone()
                attempts = [
                    self._attempt_record(attempt)
                    for attempt in connection.execute(
                        "SELECT * FROM development_evolution_job_attempts "
                        "WHERE job_id = ? ORDER BY ordinal",
                        (job_id,),
                    )
                ]
                return self._job_record(job, attempts), self._attempt_record(bound), False
            job_row = connection.execute(
                """
                SELECT job.*, session.state AS session_state, session.project_id
                FROM development_evolution_jobs AS job
                JOIN development_sessions AS session ON session.session_id = job.session_id
                WHERE job.job_id = ?
                """,
                (job_id,),
            ).fetchone()
            if job_row is None:
                raise KeyError(job_id)
            if job_row["state"] != "failed":
                raise StateConflictError("only a failed Evolution Job can be retried")
            if job_row["session_state"] != "completed":
                raise StateConflictError("the parent Session must be completed before retry")
            running = connection.execute(
                """
                SELECT 1
                FROM development_evolution_jobs AS candidate
                JOIN development_sessions AS session
                  ON session.session_id = candidate.session_id
                WHERE session.project_id = ? AND candidate.state IN ('queued', 'running')
                LIMIT 1
                """,
                (job_row["project_id"],),
            ).fetchone()
            if running is not None:
                raise StateConflictError(
                    "another Evolution Job is already running for this project"
                )
            ordinal = connection.execute(
                "SELECT COALESCE(MAX(ordinal), 0) + 1 AS ordinal "
                "FROM development_evolution_job_attempts WHERE job_id = ?",
                (job_id,),
            ).fetchone()["ordinal"]
            attempt_id = f"{job_id}-attempt-{ordinal}"
            connection.execute(
                """
                INSERT INTO development_evolution_job_attempts(
                    attempt_id, action_id, job_id, ordinal, state, stage, artifact_ids_json,
                    error_code, error_message, logs_json, created_at, started_at,
                    completed_at, updated_at
                ) VALUES (?, ?, ?, ?, 'running', 'input_resolution', '[]', NULL, NULL,
                          ?, ?, ?, NULL, ?)
                """,
                (
                    attempt_id,
                    action_id,
                    job_id,
                    ordinal,
                    canonical_json(["Retry admitted with the original fixed inputs."]),
                    now,
                    now,
                    now,
                ),
            )
            connection.execute(
                "UPDATE development_evolution_jobs "
                "SET state = 'running', artifact_ids_json = '[]', error = NULL, updated_at = ? "
                "WHERE job_id = ?",
                (now, job_id),
            )
            if job_row["run_id"] is not None:
                connection.execute(
                    "UPDATE development_evolution_runs SET state = 'running', error = NULL, "
                    "updated_at = ? WHERE run_id = ? AND state = 'failed'",
                    (now, job_row["run_id"]),
                )
            job = connection.execute(
                "SELECT * FROM development_evolution_jobs WHERE job_id = ?",
                (job_id,),
            ).fetchone()
            attempt = connection.execute(
                "SELECT * FROM development_evolution_job_attempts WHERE attempt_id = ?",
                (attempt_id,),
            ).fetchone()
        return (
            self._job_record(job, [self._attempt_record(attempt)]),
            self._attempt_record(attempt),
            True,
        )

    def reconcile_evolution_run(self, run_id: str) -> dict[str, Any]:
        now = utc_now()
        with self._lock, self._connection() as connection:
            run = connection.execute(
                "SELECT * FROM development_evolution_runs WHERE run_id = ?",
                (run_id,),
            ).fetchone()
            if run is None:
                raise KeyError(run_id)
            jobs = connection.execute(
                "SELECT state, artifact_ids_json, error FROM development_evolution_jobs "
                "WHERE run_id = ? ORDER BY created_at, job_id",
                (run_id,),
            ).fetchall()
            artifact_ids = [
                artifact_id for job in jobs for artifact_id in json.loads(job["artifact_ids_json"])
            ]
            errors = [job["error"] for job in jobs if job["error"]]
            if jobs and all(job["state"] == "completed" for job in jobs):
                state = "candidate_ready"
                error = None
            elif any(job["state"] == "failed" for job in jobs):
                state = "failed"
                error = "; ".join(errors) or "one or more Evolution methods failed"
            else:
                state = "running"
                error = None
            connection.execute(
                "UPDATE development_evolution_runs SET state = ?, artifact_ids_json = ?, "
                "error = ?, updated_at = ? WHERE run_id = ? AND state != 'applied'",
                (state, canonical_json(artifact_ids), error, now, run_id),
            )
            updated = connection.execute(
                "SELECT * FROM development_evolution_runs WHERE run_id = ?",
                (run_id,),
            ).fetchone()
        return self._evolution_run_record(updated)

    def update_evolution_attempt(self, attempt_id: str, *, stage: str, message: str) -> None:
        now = utc_now()
        with self._lock, self._connection() as connection:
            row = connection.execute(
                "SELECT logs_json, state FROM development_evolution_job_attempts "
                "WHERE attempt_id = ?",
                (attempt_id,),
            ).fetchone()
            if row is None:
                raise KeyError(attempt_id)
            if row["state"] != "running":
                raise StateConflictError("Evolution attempt is already terminal")
            logs = json.loads(row["logs_json"])
            logs.append(message)
            connection.execute(
                "UPDATE development_evolution_job_attempts "
                "SET stage = ?, logs_json = ?, updated_at = ? WHERE attempt_id = ?",
                (stage, canonical_json(logs), now, attempt_id),
            )

    def finish_evolution_job(
        self,
        job_id: str,
        *,
        attempt_id: str | None = None,
        artifact_ids: list[str] | None = None,
        error: str | None = None,
        error_stage: str | None = None,
        error_code: str | None = None,
    ) -> None:
        now = utc_now()
        with self._lock, self._connection() as connection:
            if attempt_id is None:
                attempt_row = connection.execute(
                    "SELECT attempt_id FROM development_evolution_job_attempts "
                    "WHERE job_id = ? AND state = 'running' ORDER BY ordinal DESC LIMIT 1",
                    (job_id,),
                ).fetchone()
                attempt_id = None if attempt_row is None else attempt_row["attempt_id"]
            connection.execute(
                """
                UPDATE development_evolution_jobs
                SET state = ?, artifact_ids_json = ?, error = ?, updated_at = ?
                WHERE job_id = ?
                """,
                (
                    "failed" if error is not None else "completed",
                    canonical_json(artifact_ids or []),
                    error,
                    now,
                    job_id,
                ),
            )
            if attempt_id is not None:
                attempt_row = connection.execute(
                    "SELECT logs_json FROM development_evolution_job_attempts WHERE attempt_id = ?",
                    (attempt_id,),
                ).fetchone()
                if attempt_row is None:
                    raise KeyError(attempt_id)
                logs = json.loads(attempt_row["logs_json"])
                logs.append(
                    "Evolution attempt failed."
                    if error is not None
                    else "Evolution attempt completed and published its outputs."
                )
                connection.execute(
                    """
                    UPDATE development_evolution_job_attempts
                    SET state = ?, stage = ?, artifact_ids_json = ?, error_code = ?,
                        error_message = ?, logs_json = ?, completed_at = ?, updated_at = ?
                    WHERE attempt_id = ?
                    """,
                    (
                        "failed" if error is not None else "completed",
                        error_stage or ("failed" if error is not None else "completed"),
                        canonical_json(artifact_ids or []),
                        error_code,
                        error,
                        canonical_json(logs),
                        now,
                        now,
                        attempt_id,
                    ),
                )

    def get_evolution_job(self, job_id: str) -> dict[str, Any]:
        with self._lock, self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM development_evolution_jobs WHERE job_id = ?",
                (job_id,),
            ).fetchone()
            if row is None:
                raise KeyError(job_id)
            attempts = [
                self._attempt_record(attempt)
                for attempt in connection.execute(
                    "SELECT * FROM development_evolution_job_attempts "
                    "WHERE job_id = ? ORDER BY ordinal",
                    (job_id,),
                )
            ]
        return self._job_record(row, attempts)

    def evolution_job_v2(self, job_id: str) -> DevelopmentEvolutionJobV2:
        with self._lock, self._connection() as connection:
            row = connection.execute(
                "SELECT job.*, session.project_id "
                "FROM development_evolution_jobs AS job "
                "JOIN development_sessions AS session "
                "ON session.session_id = job.session_id "
                "WHERE job.job_id = ?",
                (job_id,),
            ).fetchone()
            if row is None:
                raise KeyError(job_id)
            attempts = [
                self._attempt_record(attempt, include_action_id=True)
                for attempt in connection.execute(
                    "SELECT * FROM development_evolution_job_attempts "
                    "WHERE job_id = ? ORDER BY ordinal",
                    (job_id,),
                )
            ]
        return self._development_evolution_job_v2(
            self._job_record(row, attempts),
            project_id=row["project_id"],
        )

    def evolution_job_page_v2(
        self,
        *,
        project_id: str,
        after_job_id: str | None,
        limit: int,
    ) -> DevelopmentEvolutionJobPageV2:
        with self._lock, self._connection() as connection:
            if (
                connection.execute(
                    "SELECT 1 FROM development_projects WHERE project_id = ?",
                    (project_id,),
                ).fetchone()
                is None
            ):
                raise KeyError(project_id)
            parameters: list[object] = [project_id]
            cursor_clause = ""
            if after_job_id is not None:
                cursor = connection.execute(
                    "SELECT job.created_at, job.job_id "
                    "FROM development_evolution_jobs AS job "
                    "JOIN development_sessions AS session "
                    "ON session.session_id = job.session_id "
                    "WHERE session.project_id = ? AND job.job_id = ?",
                    (project_id, after_job_id),
                ).fetchone()
                if cursor is None:
                    raise RequestError("Evolution Job cursor is not part of this Project")
                cursor_clause = (
                    "AND (job.created_at > ? OR (job.created_at = ? AND job.job_id > ?)) "
                )
                parameters.extend([cursor["created_at"], cursor["created_at"], cursor["job_id"]])
            rows = connection.execute(
                "SELECT job.*, session.project_id "
                "FROM development_evolution_jobs AS job "
                "JOIN development_sessions AS session "
                "ON session.session_id = job.session_id "
                "WHERE session.project_id = ? "
                + cursor_clause
                + "ORDER BY job.created_at, job.job_id LIMIT ?",
                (*parameters, limit + 1),
            ).fetchall()
            selected = rows[:limit]
            attempts_by_job: dict[str, list[dict[str, Any]]] = {}
            if selected:
                job_ids = [row["job_id"] for row in selected]
                for attempt in connection.execute(
                    "SELECT * FROM development_evolution_job_attempts "
                    f"WHERE job_id IN ({','.join('?' for _ in job_ids)}) "
                    "ORDER BY job_id, ordinal",
                    tuple(job_ids),
                ):
                    attempts_by_job.setdefault(attempt["job_id"], []).append(
                        self._attempt_record(attempt, include_action_id=True)
                    )
        has_more = len(rows) > limit
        items = [
            self._development_evolution_job_v2(
                self._job_record(row, attempts_by_job.get(row["job_id"], [])),
                project_id=row["project_id"],
            )
            for row in selected
        ]
        return DevelopmentEvolutionJobPageV2(
            items=items,
            next_cursor=items[-1].job_id if has_more and items else None,
            has_more=has_more,
        )

    def dataset_artifact(self, artifact_id: str) -> dict[str, str]:
        return self.artifact_store.dataset(artifact_id)

    def artifact(self, artifact_id: str) -> dict[str, Any]:
        return self.artifact_store.get(artifact_id)

    def artifact_observation_v2(self, artifact_id: str) -> core_v2.ArtifactV2:
        return self._core_artifact_v2(self.artifact(artifact_id))

    def artifact_content_observation_v2(self, artifact_id: str) -> core_v2.ArtifactContentV2:
        record = self.artifact(artifact_id)
        documents = record["documents"]
        media_type = documents[0]["media_type"] if documents else "application/octet-stream"
        artifact = self._core_artifact_v2(record)
        return core_v2.ArtifactContentV2(
            artifact=artifact,
            media_type=media_type,
            content_sha256=record["content_sha256"],
            byte_size=record["byte_size"],
        )

    def artifact_page_v2(
        self,
        *,
        project_id: str | None,
        task_id: str | None,
        after_artifact_id: str | None,
        limit: int,
        development_detail: bool,
    ) -> core_v2.ArtifactPageV2 | DevelopmentArtifactPageV2:
        page = self.artifact_store.page(
            project_id=project_id,
            task_id=task_id,
            after_artifact_id=after_artifact_id,
            limit=limit,
        )
        if development_detail:
            return DevelopmentArtifactPageV2.model_validate(
                {
                    "schema_version": "2",
                    "items": [self._development_artifact_v2(record) for record in page.items],
                    "next_cursor": page.next_cursor,
                    "has_more": page.has_more,
                }
            )
        return core_v2.ArtifactPageV2(
            items=[self._core_artifact_v2(record) for record in page.items],
            next_cursor=page.next_cursor,
            has_more=page.has_more,
        )

    def development_artifact_v2(self, artifact_id: str) -> DevelopmentArtifactV2:
        return DevelopmentArtifactV2.model_validate(
            self._development_artifact_v2(self.artifact(artifact_id))
        )

    def record_evolution_artifact(
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
        return self.artifact_store.create(
            artifact_id=artifact_id,
            project_id=project_id,
            session_id=session_id,
            run_id=run_id,
            target_id=target_id,
            artifact_type=artifact_type,
            method_id=method_id,
            renderer_kind=renderer_kind,
            documents=documents,
            manifest=manifest,
            previous_artifact_id=previous_artifact_id,
            promoted=promoted,
        )

    def fail_session(
        self,
        session_id: str,
        error: str,
        workspace_changes: list[dict[str, Any]] | None = None,
    ) -> None:
        self.session_store.fail(session_id, error, workspace_changes)

    def _session_record(
        self,
        row: sqlite3.Row,
        *,
        evolution_evidence_ready: bool = False,
    ) -> dict[str, Any]:
        return self.session_store.record(
            row,
            evolution_evidence_ready=evolution_evidence_ready,
        )

    @staticmethod
    def _task_observation_v2(row: sqlite3.Row) -> DevelopmentTaskObservationV2:
        state = row["state"]
        if row["terminal_kind"] == "cancelled":
            state = "cancelled"
        elif state == "running" and row["cancellation_requested"]:
            state = "cancelling"
        elif state == "completed":
            state = "closed"
        return DevelopmentTaskObservationV2(
            task_id=row["session_id"],
            project_id=row["project_id"],
            project_head_id=row["project_head_id"],
            state=state,
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    def _task_presentation_v2(
        self,
        row: sqlite3.Row,
        *,
        evolution_evidence_ready: bool,
    ) -> DevelopmentTaskPresentationV2:
        record = self._session_record(row, evolution_evidence_ready=evolution_evidence_ready)
        return DevelopmentTaskPresentationV2.model_validate(
            {
                "schema_version": "2",
                "task_id": record["session_id"],
                "project_id": record["project_id"],
                "project_head_id": record["project_head_id"],
                "task_title": record["task_title"],
                "instruction": record["instruction"],
                "response": record["response"],
                "model": record["model"],
                "state": record["state"],
                "duration_ms": record["duration_ms"],
                "selected_evolution": [
                    {"schema_version": "2", **selection}
                    for selection in record["selected_evolution"]
                ],
                "evolution_errors": [
                    {"schema_version": "2", **error} for error in record["evolution_errors"]
                ],
                "workspace_changes": [
                    {
                        "schema_version": "2",
                        **change,
                        "diff_lines": [
                            {"schema_version": "2", **line}
                            for line in change.get("diff_lines", [])
                        ],
                    }
                    for change in record["workspace_changes"]
                ],
                "context_artifact_ids": record["context_artifact_ids"],
                "evolution_evidence_ready": record["evolution_evidence_ready"],
                "error": record["error"],
                "created_at": record["created_at"],
                "updated_at": record["updated_at"],
            }
        )

    @staticmethod
    def _artifact_record(row: sqlite3.Row) -> dict[str, Any]:
        return SqliteArtifactStore.record(row)

    @staticmethod
    def _core_artifact_v2(record: dict[str, Any]) -> core_v2.ArtifactV2:
        artifact_type = (
            "diagnostic" if record["artifact_type"] == "report" else record["artifact_type"]
        )
        return core_v2.ArtifactV2(
            artifact_id=record["artifact_id"],
            project_id=record["project_id"],
            artifact_type=artifact_type,
            manifest_sha256=record["content_sha256"],
            byte_size=record["byte_size"],
            created_at=record["created_at"],
        )

    @staticmethod
    def _development_artifact_v2(record: dict[str, Any]) -> dict[str, Any]:
        return {
            "schema_version": "2",
            **record,
            "documents": [{"schema_version": "2", **document} for document in record["documents"]],
        }

    @staticmethod
    def _job_record(
        row: sqlite3.Row,
        attempts: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        return {
            "job_id": row["job_id"],
            "session_id": row["session_id"],
            "run_id": row["run_id"],
            "target_id": row["target_id"],
            "method_id": row["method_id"],
            "requested_method_id": row["requested_method_id"],
            "resolver_input_artifact_ids": json.loads(row["resolver_input_artifact_ids_json"]),
            "previous_artifact_id": row["previous_artifact_id"],
            "config": json.loads(row["config_json"]),
            "state": row["state"],
            "artifact_ids": json.loads(row["artifact_ids_json"]),
            "error": row["error"],
            "attempts": attempts or [],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }

    @staticmethod
    def _evolution_run_record(
        row: sqlite3.Row,
        *,
        include_action_id: bool = False,
    ) -> dict[str, Any]:
        record = {
            "run_id": row["run_id"],
            "project_id": row["project_id"],
            "source_session_ids": json.loads(row["source_session_ids_json"]),
            "selections": normalize_selected_evolution(json.loads(row["selections_json"])),
            "state": row["state"],
            "artifact_ids": json.loads(row["artifact_ids_json"]),
            "error": row["error"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }
        if include_action_id:
            record["action_id"] = row["action_id"]
        return record

    @staticmethod
    def _development_evolution_run_v2(
        record: dict[str, Any],
    ) -> DevelopmentEvolutionRunV2:
        return DevelopmentEvolutionRunV2.model_validate(
            {
                "schema_version": "2",
                "run_id": record["run_id"],
                "action_id": record["action_id"],
                "project_id": record["project_id"],
                "source_task_ids": record["source_session_ids"],
                "selections": [
                    {"schema_version": "2", **selection} for selection in record["selections"]
                ],
                "state": record["state"],
                "artifact_ids": record["artifact_ids"],
                "error": record["error"],
                "created_at": record["created_at"],
                "updated_at": record["updated_at"],
            }
        )

    @staticmethod
    def _development_evolution_job_v2(
        record: dict[str, Any],
        *,
        project_id: str,
    ) -> DevelopmentEvolutionJobV2:
        return DevelopmentEvolutionJobV2.model_validate(
            {
                "schema_version": "2",
                "job_id": record["job_id"],
                "project_id": project_id,
                "task_id": record["session_id"],
                "run_id": record["run_id"],
                "target_id": record["target_id"],
                "method_id": record["method_id"],
                "requested_method_id": record["requested_method_id"],
                "resolver_input_artifact_ids": record["resolver_input_artifact_ids"],
                "previous_artifact_id": record["previous_artifact_id"],
                "config": record["config"],
                "state": record["state"],
                "artifact_ids": record["artifact_ids"],
                "error": record["error"],
                "attempts": [{"schema_version": "2", **attempt} for attempt in record["attempts"]],
                "created_at": record["created_at"],
                "updated_at": record["updated_at"],
            }
        )

    @staticmethod
    def _attempt_record(
        row: sqlite3.Row,
        *,
        include_action_id: bool = False,
    ) -> dict[str, Any]:
        record = {
            "attempt_id": row["attempt_id"],
            "job_id": row["job_id"],
            "ordinal": row["ordinal"],
            "state": row["state"],
            "stage": row["stage"],
            "artifact_ids": json.loads(row["artifact_ids_json"]),
            "error_code": row["error_code"],
            "error_message": row["error_message"],
            "logs": json.loads(row["logs_json"]),
            "created_at": row["created_at"],
            "started_at": row["started_at"],
            "completed_at": row["completed_at"],
            "updated_at": row["updated_at"],
        }
        if include_action_id:
            record["action_id"] = row["action_id"]
        return record


def validate_request(payload: object) -> dict[str, str]:
    if not isinstance(payload, dict):
        raise RequestError("request body must be a JSON object")
    unknown = set(payload) - ALLOWED_REQUEST_FIELDS
    if unknown:
        raise RequestError(f"unknown request fields: {', '.join(sorted(unknown))}")
    if payload.get("schema_version") != "1":
        raise RequestError("schema_version must be '1'")

    project_id = payload.get("project_id")
    if not isinstance(project_id, str) or not ID_PATTERN.fullmatch(project_id):
        raise RequestError("project_id is invalid")

    result = {"project_id": project_id}
    project_head_id = payload.get("project_head_id")
    if project_head_id is not None:
        if not isinstance(project_head_id, str) or not ID_PATTERN.fullmatch(project_head_id):
            raise RequestError("project_head_id is invalid")
        result["project_head_id"] = project_head_id
    for field, maximum in (("project_name", 200), ("task_title", 200), ("instruction", 32_000)):
        value = payload.get(field)
        if not isinstance(value, str) or not value.strip():
            raise RequestError(f"{field} must be a non-empty string")
        if len(value) > maximum:
            raise RequestError(f"{field} is too long")
        result[field] = value.strip()
    return result


def extract_event_logs(stdout: str) -> list[str]:
    messages: list[str] = []
    for raw_line in stdout.splitlines():
        try:
            event = json.loads(raw_line)
        except json.JSONDecodeError:
            continue
        if not isinstance(event, dict):
            continue
        event_type = event.get("type")
        if isinstance(event_type, str) and event_type not in {"item.completed"}:
            messages.append(f"Codex event: {event_type}")
    return messages[-20:]


class _DevelopmentArtifactPayloads:
    """Bounded read service for DB-owned development artifact documents."""

    def __init__(self, documents: dict[str, dict[str, str]]) -> None:
        self._documents = documents

    def read_utf8_prefix(
        self,
        payload_handle: str,
        relative_path: str,
        *,
        max_chars: int,
        max_bytes: int,
    ) -> str:
        try:
            content = self._documents[payload_handle][relative_path]
        except KeyError as exc:
            raise ValueError("development artifact payload is unavailable") from exc
        clipped = content[:max_chars]
        encoded = clipped.encode("utf-8")
        if len(encoded) > max_bytes:
            clipped = encoded[:max_bytes].decode("utf-8", errors="ignore")
        return clipped


class DevelopmentRuntimeContextMaterializer:
    """Project Core handler contributions into one isolated Codex runtime workspace.

    This is a development adapter, not the release artifact store/materializer. It deliberately
    consumes the same closed handler input/output contracts so target behavior is not inferred
    from a UI card or renderer kind.
    """

    def __init__(self, registry: Any | None = None) -> None:
        self._registry = registry or development_registry_snapshot()

    @staticmethod
    def _copy_workspace(source: Path, destination: Path) -> None:
        destination.mkdir(mode=0o700, parents=True, exist_ok=False)
        entries = 0
        for candidate in sorted(source.rglob("*")):
            entries += 1
            if entries > MAX_WORKSPACE_ENTRIES:
                raise AgentRunError("persistent workspace exceeds the runtime entry limit")
            if candidate.is_symlink():
                raise AgentRunError("persistent workspace contains an unsupported symbolic link")
            relative = candidate.relative_to(source)
            target = destination / relative
            if candidate.is_dir():
                target.mkdir(mode=0o700, parents=True, exist_ok=True)
            elif candidate.is_file():
                target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                shutil.copyfile(candidate, target)
            else:
                raise AgentRunError("persistent workspace contains an unsupported entry")

    @staticmethod
    def _scope_roots(runtime_workspace: Path) -> dict[str, Path]:
        return {
            "target_data": runtime_workspace / ".openevo" / "evolution",
            "harness_skills": runtime_workspace / ".agents" / "skills",
            "harness_instruction": runtime_workspace,
        }

    @staticmethod
    def _write_text(root: Path, relative_path: str, content: str) -> Path:
        from openevo.evolution.framework.contracts import validate_relative_path

        normalized = validate_relative_path(relative_path)
        destination = root.joinpath(*PurePosixPath(normalized).parts)
        root_resolved = root.resolve()
        destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        if destination.is_symlink() or root_resolved not in destination.resolve().parents:
            raise AgentRunError("runtime contribution escaped its destination scope")
        destination.write_text(content, encoding="utf-8")
        return destination

    @staticmethod
    def _codex_skill_entrypoint(skill_directory: str, content: str) -> str:
        """Add required Codex skill metadata without changing the stored artifact."""

        if content.startswith("---\n"):
            closing = content.find("\n---\n", 4)
            if closing != -1:
                header = content[4:closing]
                if re.search(r"(?m)^name:\s*\S+", header) and re.search(
                    r"(?m)^description:\s*\S+", header
                ):
                    return content
        normalized = re.sub(r"[^a-z0-9-]+", "-", skill_directory.lower()).strip("-")
        if not normalized or not normalized[0].isalpha():
            normalized = f"openevo-{normalized or 'evolved-skill'}"
        normalized = normalized[:64].rstrip("-")
        return (
            "---\n"
            f"name: {normalized}\n"
            "description: Apply this evolved OpenEvo workflow when the current task "
            "matches its instructions.\n"
            "---\n\n"
            f"{content.lstrip()}"
        )

    def _project(
        self, contexts: object
    ) -> tuple[list[tuple[Any, Any]], dict[str, dict[str, str]]]:
        from openevo.evolution.framework.builtin_handlers import BUILTIN_HANDLER_REGISTRY
        from openevo.evolution.framework.contracts import EvolutionExecutionProfile
        from openevo.evolution.framework.handlers import (
            PayloadManifestEntry,
            RuntimeDestinationRoots,
            TargetHandlerInput,
            TargetHandlerServices,
            TrustedArtifactSnapshot,
            payload_tree_digest,
        )

        pairs: list[tuple[Any, Any]] = []
        payload_documents: dict[str, dict[str, str]] = {}
        if not isinstance(contexts, list):
            return pairs, payload_documents
        for rank, context in enumerate(contexts):
            if not isinstance(context, dict):
                raise AgentRunError("evolved context record is invalid")
            target_id = context.get("target_id")
            try:
                target = self._registry.targets[target_id]
                handler_descriptor = self._registry.target_handlers[target.handler_id]
                handler = BUILTIN_HANDLER_REGISTRY[target.handler_id]
            except (KeyError, TypeError) as exc:
                raise AgentRunError(
                    f"evolved target {target_id!r} is not in the Core catalog"
                ) from exc
            if context.get("artifact_type") != target.artifact_type:
                raise AgentRunError(f"evolved target {target_id!r} has the wrong artifact type")
            raw_documents = context.get("documents")
            if not isinstance(raw_documents, list) or not raw_documents:
                raise AgentRunError(f"evolved target {target_id!r} has no readable payload")
            handle = f"development_payload_{rank}"
            document_map: dict[str, str] = {}
            entries: list[Any] = []
            for raw_document in raw_documents:
                if not isinstance(raw_document, dict):
                    raise AgentRunError("evolved artifact document is invalid")
                path = raw_document.get("path")
                content = raw_document.get("content")
                media_type = raw_document.get("media_type", "text/plain")
                if (
                    not isinstance(path, str)
                    or not isinstance(content, str)
                    or not isinstance(media_type, str)
                ):
                    raise AgentRunError("evolved artifact document is invalid")
                encoded = content.encode("utf-8")
                entry = PayloadManifestEntry(
                    relative_path=path,
                    media_type=media_type,
                    size_bytes=len(encoded),
                    sha256=hashlib.sha256(encoded).hexdigest(),
                )
                entries.append(entry)
                document_map[entry.relative_path] = content
            payload_documents[handle] = document_map
            manifest = context.get("manifest")
            if not isinstance(manifest, dict):
                raise AgentRunError("evolved artifact manifest is invalid")
            artifact_id = context.get("artifact_id")
            if not isinstance(artifact_id, str) or not artifact_id:
                raise AgentRunError("evolved artifact identity is invalid")
            payload_entries = tuple(sorted(entries, key=lambda entry: entry.relative_path))
            snapshot = TrustedArtifactSnapshot(
                artifact_id=artifact_id,
                artifact_type=target.artifact_type,
                name=f"evolved {target.display_name}",
                uri_scheme="file",
                payload_handle=handle,
                payload_entries=payload_entries,
                payload_manifest_digest=payload_tree_digest(payload_entries),
                manifest_json=canonical_json(manifest),
                scores_json="{}",
                rank_index=0,
            )
            handler_input = TargetHandlerInput(
                target_id=target.id,
                handler_id=target.handler_id,
                execution_profile=EvolutionExecutionProfile(
                    execution_mode="subscription",
                    capture_mode="transcript",
                    harness_id="codex",
                ),
                # Handler contracts require canonical Linux runtime roots. The development
                # materializer maps these scopes into its private temporary workspace below.
                destination_roots=RuntimeDestinationRoots(
                    target_data="/openevo/session/evolution",
                    harness_skills="/openevo/session/evolution/skills",
                    harness_instruction="/workspace",
                ),
                ranked_artifacts=(snapshot,),
            )
            output = self._registry.validate_handler_output(
                handler(
                    handler_input,
                    TargetHandlerServices(
                        payloads=_DevelopmentArtifactPayloads(payload_documents)
                    ),
                ),
                handler_input=handler_input,
            )
            if output.handler_id != handler_descriptor.id:
                raise AgentRunError("Core target handler identity changed during projection")
            pairs.append((handler_input, output))
        return pairs, payload_documents

    def materialize(
        self,
        *,
        persistent_workspace: Path,
        runtime_workspace: Path,
        contexts: object,
    ) -> dict[str, Any]:
        from openevo.evolution.framework.contracts import (
            DestinationScope,
            EnvironmentValueKind,
        )
        from openevo.evolution.framework.contributions import (
            InlineTextPayloadContribution,
            StagedPayloadContribution,
        )
        from openevo.evolution.framework.runtime_controls import (
            AgentSystemRuntimeControlV1,
            validate_runtime_control,
        )

        self._copy_workspace(persistent_workspace, runtime_workspace)
        pairs, payload_documents = self._project(contexts)
        outputs = self._registry.validate_handler_outputs(pairs)
        scope_roots = self._scope_roots(runtime_workspace)
        artifact_handles = {
            handler_input.ranked_artifacts[0].artifact_id: handler_input.ranked_artifacts[
                0
            ].payload_handle
            for handler_input, _output in pairs
        }
        contribution_paths: dict[str, Path] = {}
        instructions: list[str] = []
        activations: list[str] = []
        environment: dict[str, str] = {}
        runtime_controls: list[dict[str, Any]] = []

        for output in outputs:
            handler_descriptor = self._registry.target_handlers[output.handler_id]
            for instruction in output.instructions:
                section = instruction.text.strip()
                if handler_descriptor.instruction_preamble:
                    section = f"{handler_descriptor.instruction_preamble}\n{section}"
                instructions.append(section)
            for payload in output.staged_payloads:
                scope = payload.destination_scope.value
                root = scope_roots[scope]
                if isinstance(payload, InlineTextPayloadContribution):
                    contribution_paths[payload.contribution_id] = self._write_text(
                        root, payload.destination_relative_path, payload.text
                    )
                    if payload.destination_relative_path.startswith("runtime-controls/"):
                        try:
                            control = validate_runtime_control(json.loads(payload.text))
                        except (json.JSONDecodeError, ValueError) as exc:
                            raise AgentRunError(
                                "Core returned an invalid runtime-control contribution"
                            ) from exc
                        runtime_controls.append(control.model_dump(mode="json"))
                        activations.append(
                            f"{output.target_id}: {control.kind} runtime control v"
                            f"{control.contract_version} loaded"
                        )
                        if (
                            isinstance(control, AgentSystemRuntimeControlV1)
                            and control.spawn_plan is not None
                        ):
                            activations.append(
                                f"{output.target_id}: structured spawn plan staged for "
                                "the harness adapter"
                            )
                    continue
                if not isinstance(payload, StagedPayloadContribution):
                    raise AgentRunError("Core returned an unsupported payload contribution")
                source_handle = artifact_handles.get(payload.source_artifact_id)
                source = payload_documents.get(source_handle or "")
                if source is None:
                    raise AgentRunError("Core contribution source is unavailable")
                destination_root = root.joinpath(
                    *PurePosixPath(payload.destination_relative_path).parts
                )
                written: list[Path] = []
                if payload.source_relative_path == ".":
                    for source_path, content in source.items():
                        if (
                            payload.destination_scope is DestinationScope.HARNESS_SKILLS
                            and source_path == "SKILL.md"
                        ):
                            content = self._codex_skill_entrypoint(
                                payload.destination_relative_path,
                                content,
                            )
                        written.append(self._write_text(destination_root, source_path, content))
                    contribution_paths[payload.contribution_id] = destination_root
                else:
                    try:
                        content = source[payload.source_relative_path]
                    except KeyError as exc:
                        raise AgentRunError(
                            "Core contribution source file is unavailable"
                        ) from exc
                    written.append(
                        self._write_text(root, payload.destination_relative_path, content)
                    )
                    contribution_paths[payload.contribution_id] = written[0]
            for binding in output.environment:
                if binding.value_kind is EnvironmentValueKind.SCOPE_ROOT:
                    if binding.destination_scope is None:
                        raise AgentRunError("Core returned an invalid scope-root binding")
                    environment[binding.name] = os.fspath(
                        scope_roots[binding.destination_scope.value]
                    )
                    continue
                paths = [
                    os.fspath(contribution_paths[contribution_id])
                    for contribution_id in binding.value_contribution_ids
                ]
                if binding.value_kind is EnvironmentValueKind.JSON_PATHS:
                    environment[binding.name] = canonical_json(paths)
                elif len(paths) == 1:
                    environment[binding.name] = paths[0]
                else:
                    raise AgentRunError("Core returned an invalid runtime path binding")
            if output.instructions:
                activations.append(f"{output.target_id}: instruction contribution loaded")
            if any(
                payload.destination_scope is DestinationScope.HARNESS_SKILLS
                for payload in output.staged_payloads
            ):
                activations.append(f"{output.target_id}: Codex skill bundle staged")
            if any(
                payload.destination_scope is DestinationScope.HARNESS_INSTRUCTION
                for payload in output.staged_payloads
            ):
                activations.append(f"{output.target_id}: native harness instruction staged")

        return {
            "workspace_path": runtime_workspace,
            "instruction_sections": instructions,
            "environment": environment,
            "activations": activations,
            "runtime_controls": runtime_controls,
        }


class CodexRunner(CodexAgentRunner):
    """Compatibility constructor for the extracted daemon Codex runner."""

    def __init__(self, codex_binary: str, timeout_seconds: int, model: str | None) -> None:
        super().__init__(
            codex_binary=codex_binary,
            timeout_seconds=timeout_seconds,
            model=model,
            context_materializer_factory=DevelopmentRuntimeContextMaterializer,
            runtime_control_adapter=codex_development_runtime_adapter(),
            extract_event_logs=extract_event_logs,
            max_capture_bytes=MAX_CAPTURE_BYTES,
            max_response_bytes=MAX_REQUEST_BYTES,
            max_workspace_context_bytes=MAX_AGENT_WORKSPACE_CONTEXT_BYTES,
        )


# Kept as a source-compatible name for development tests and scripts written before document
# evolution was expanded beyond text memory.
DocumentEvolutionRunner = EvolutionOrchestrator
TextMemoryEvolutionRunner = DocumentEvolutionRunner


class DevelopmentSessionCoordinator:
    """Own asynchronous development Session execution independently of HTTP request threads."""

    def __init__(
        self,
        *,
        runner: CodexRunner,
        store: DevelopmentStateStore,
        evolution_runner: DocumentEvolutionRunner | None,
    ) -> None:
        self._store = store
        self._evolution_runner = evolution_runner
        self._turn_lock = threading.Lock()
        self._agent_executor = AgentSessionExecutor(
            store=store,
            runner=runner,
            evidence_sealer=(
                self._seal_session_evidence if evolution_runner is not None else None
            ),
        )
        self._session_runtime = SessionExecutionManager(
            store=store,
            executor=self._agent_executor.execute,
            cancellation_factory=HarnessCancellation,
            execution_failed=self._record_unhandled_execution_failure,
            operation_lock=self._turn_lock,
        )

    def submit(self, request: dict[str, str], *, session_id: str | None = None) -> str:
        try:
            return self._session_runtime.submit(request, session_id=session_id)
        except SessionExecutionConflictError as exc:
            raise StateConflictError(str(exc)) from exc

    def cancel(self, session_id: str) -> dict[str, Any]:
        return self._session_runtime.cancel(session_id)

    def _record_unhandled_execution_failure(
        self,
        session_id: str,
        error: BaseException,
    ) -> None:
        try:
            if self._store.cancellation_requested(session_id):
                self._store.cancel_session(session_id, [])
            else:
                self._store.fail_session(
                    session_id,
                    f"unexpected development session failure: {error}",
                    [],
                )
        except Exception:
            # Startup recovery remains the final authority when persistence is
            # unavailable while a worker is already failing.
            pass

    def _seal_session_evidence(
        self,
        session_id: str,
        request: dict[str, str],
        result: dict[str, Any],
    ) -> None:
        if self._evolution_runner is None:
            return
        self._evolution_runner.capture_session_dataset(
            session_id=session_id,
            request=request,
            result=result,
            store=self._store,
        )

    def retry_evolution(self, job_id: str, *, action_id: str | None = None) -> dict[str, Any]:
        if self._evolution_runner is None:
            raise StateConflictError("the Evolution runner is unavailable")
        if action_id is not None:
            existing = self._store.evolution_retry_for_action(job_id, action_id)
            if existing is not None:
                return existing
        if not self._turn_lock.acquire(blocking=False):
            if action_id is not None:
                existing = self._store.evolution_retry_for_action(job_id, action_id)
                if existing is not None:
                    return existing
            raise StateConflictError("another development session or Evolution retry is running")
        try:
            effective_action_id = action_id or f"legacy-retry-{secrets.token_hex(16)}"
            job, attempt, created = self._store.start_evolution_retry_v2(
                job_id,
                effective_action_id,
            )
            if not created:
                self._turn_lock.release()
                return job
            self._store.set_evolution_error(
                job["session_id"],
                target_id=job["target_id"],
                method=job["requested_method_id"],
                message=None,
            )
        except Exception:
            self._turn_lock.release()
            raise
        thread = threading.Thread(
            target=self._execute_evolution_retry,
            name=f"openevo-retry-{attempt['attempt_id']}",
            args=(job, attempt),
            daemon=True,
        )
        thread.start()
        return self._store.get_evolution_job(job_id)

    def submit_evolution(self, request: dict[str, Any]) -> dict[str, Any]:
        if self._evolution_runner is None:
            raise StateConflictError("the Evolution runner is unavailable")
        if not self._turn_lock.acquire(blocking=False):
            existing = self._store.evolution_run_for_action(request)
            if existing is not None:
                return existing
            raise StateConflictError("another development Session or Evolution Run is active")
        run_id = f"evolution-run-{secrets.token_hex(8)}"
        try:
            run = self._store.start_evolution_run(run_id, request)
        except Exception:
            self._turn_lock.release()
            raise
        if run["run_id"] != run_id:
            self._turn_lock.release()
            return run
        thread = threading.Thread(
            target=self._execute_evolution_run,
            name=f"openevo-{run_id}",
            args=(run,),
            daemon=True,
        )
        thread.start()
        return run

    def apply_evolution(self, run_id: str) -> dict[str, Any]:
        if not self._turn_lock.acquire(blocking=False):
            raise StateConflictError("another development Session or Evolution Run is active")
        try:
            return self._store.apply_evolution_run(run_id)
        finally:
            self._turn_lock.release()

    def upload_workspace_file_v2(
        self,
        project_id: str,
        relative_path: object,
        payload: bytes,
        *,
        overwrite: bool,
    ) -> DevelopmentWorkspaceMutationV2:
        if not self._turn_lock.acquire(blocking=False):
            raise StateConflictError(
                "workspace uploads are unavailable while a Session or Evolution retry is running"
            )
        try:
            entry = self._store.upload_workspace_file(
                project_id, relative_path, payload, overwrite=overwrite
            )
            return self._store.workspace_mutation_v2(project_id, entry["path"])
        finally:
            self._turn_lock.release()

    def delete_workspace_file(self, project_id: str, relative_path: object) -> str:
        if not self._turn_lock.acquire(blocking=False):
            raise StateConflictError(
                "workspace deletes are unavailable while a Session or Evolution retry is running"
            )
        try:
            return self._store.delete_workspace_file(project_id, relative_path)
        finally:
            self._turn_lock.release()

    def delete_workspace_file_v2(
        self,
        project_id: str,
        relative_path: object,
    ) -> DevelopmentWorkspaceDeleteV2:
        if not self._turn_lock.acquire(blocking=False):
            raise StateConflictError(
                "workspace deletes are unavailable while a Session or Evolution retry is running"
            )
        try:
            deleted_path = self._store.delete_workspace_file(project_id, relative_path)
            return self._store.workspace_delete_v2(project_id, deleted_path)
        finally:
            self._turn_lock.release()

    def upload_workspace_file(
        self,
        project_id: str,
        relative_path: object,
        payload: bytes,
        *,
        overwrite: bool,
    ) -> dict[str, Any]:
        if not self._turn_lock.acquire(blocking=False):
            raise StateConflictError(
                "workspace uploads are unavailable while a Session or Evolution retry is running"
            )
        try:
            return self._store.upload_workspace_file(
                project_id,
                relative_path,
                payload,
                overwrite=overwrite,
            )
        finally:
            self._turn_lock.release()

    def _execute_evolution_retry(
        self,
        job: dict[str, Any],
        attempt: dict[str, Any],
    ) -> None:
        try:
            artifacts = self._evolution_runner.retry(
                job=job,
                attempt=attempt,
                store=self._store,
            )
            if job.get("run_id") is not None:
                self._store.reconcile_evolution_run(job["run_id"])
            self._store.set_evolution_error(
                job["session_id"],
                target_id=job["target_id"],
                method=job["requested_method_id"],
                message=None,
            )
            self._store.append_session_log(
                job["session_id"],
                f"Evolution retry attempt {attempt['ordinal']} completed and published "
                f"{len(artifacts)} artifact(s).",
            )
        except Exception as exc:
            self._store.set_evolution_error(
                job["session_id"],
                target_id=job["target_id"],
                method=job["requested_method_id"],
                message=str(exc),
            )
            self._store.append_session_log(
                job["session_id"],
                f"Evolution retry attempt {attempt['ordinal']} failed: {exc}",
            )
            if job.get("run_id") is not None:
                self._store.reconcile_evolution_run(job["run_id"])
        finally:
            self._turn_lock.release()

    def _execute_evolution_run(self, run: dict[str, Any]) -> None:
        try:
            result = self._evolution_runner.evolve_run(run=run, store=self._store)
            errors = result["errors"]
            self._store.finish_evolution_run(
                run["run_id"],
                artifact_ids=[artifact["artifact_id"] for artifact in result["artifacts"]],
                error=(
                    None
                    if not errors
                    else "; ".join(f"{error['target_id']}: {error['message']}" for error in errors)
                ),
            )
        except Exception as exc:
            try:
                self._store.finish_evolution_run(
                    run["run_id"],
                    artifact_ids=[],
                    error=str(exc),
                )
            except Exception:
                pass
        finally:
            self._turn_lock.release()


class DevelopmentAgentServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(
        self,
        address: tuple[str, int],
        token: str,
        runner: CodexRunner,
        store: DevelopmentStateStore,
        evolution_runner: DocumentEvolutionRunner | None = None,
    ) -> None:
        super().__init__(address, DevelopmentAgentHandler)
        self.token = token
        self.runner = runner
        self.store = store
        self.evolution_runner = evolution_runner
        self.sessions = DevelopmentSessionCoordinator(
            runner=runner,
            store=store,
            evolution_runner=evolution_runner,
        )


class DevelopmentAgentHandler(BaseHTTPRequestHandler):
    server: DevelopmentAgentServer
    server_version = "OpenEvoDevelopmentAgent/1"

    def do_GET(self) -> None:  # noqa: N802
        parsed_path = urlsplit(self.path)
        if parsed_path.path == "/health":
            self._json(
                HTTPStatus.OK,
                {"schema_version": "1", "service": "openevo-daemon", "status": "ok"},
            )
            return
        if not self._authorized():
            return
        if parsed_path.path == DAEMON_V2_TASKS_PATH:
            try:
                parameters = parse_qs(
                    parsed_path.query, keep_blank_values=True, strict_parsing=True
                )
                if set(parameters) - {"project_id", "after", "limit"} or any(
                    len(values) != 1 for values in parameters.values()
                ):
                    raise RequestError("task query contains unsupported parameters")
                project_id = parameters.get("project_id", [None])[0]
                if project_id is not None and not ID_PATTERN.fullmatch(project_id):
                    raise RequestError("project_id is invalid")
                after_task_id = parameters.get("after", [None])[0]
                if after_task_id is not None and not ID_PATTERN.fullmatch(after_task_id):
                    raise RequestError("task cursor is invalid")
                limit_raw = parameters.get("limit", [str(MAX_DAEMON_V2_TASK_PAGE)])[0]
                if not limit_raw.isascii() or not limit_raw.isdigit():
                    raise RequestError("task limit must be an integer")
                limit = int(limit_raw)
                if not 1 <= limit <= MAX_DAEMON_V2_TASK_PAGE:
                    raise RequestError("task limit is outside the supported bound")
                page = self.server.store.task_observations_v2(
                    project_id=project_id,
                    after_task_id=after_task_id,
                    limit=limit,
                )
            except RequestError as exc:
                self._json_error_v2(HTTPStatus.BAD_REQUEST, "invalid_request", str(exc))
            else:
                self._json(HTTPStatus.OK, page.model_dump(mode="json"))
            return
        task_logs_match = DAEMON_V2_TASK_LOGS_PATH_PATTERN.fullmatch(parsed_path.path)
        if task_logs_match:
            task_id = task_logs_match.group(1)
            try:
                if not ID_PATTERN.fullmatch(task_id):
                    raise RequestError("task_id is invalid")
                parameters = parse_qs(
                    parsed_path.query, keep_blank_values=True, strict_parsing=True
                )
                if set(parameters) - {"after", "limit"} or any(
                    len(values) != 1 for values in parameters.values()
                ):
                    raise RequestError("log query contains unsupported parameters")
                after_raw = parameters.get("after", ["0"])[0]
                limit_raw = parameters.get("limit", [str(MAX_DAEMON_V2_LOG_PAGE)])[0]
                if not after_raw.isascii() or not after_raw.isdigit():
                    raise RequestError("log cursor must be a non-negative integer")
                if not limit_raw.isascii() or not limit_raw.isdigit():
                    raise RequestError("log limit must be an integer")
                after_sequence = int(after_raw)
                limit = int(limit_raw)
                if not 1 <= limit <= MAX_DAEMON_V2_LOG_PAGE:
                    raise RequestError("log limit is outside the supported bound")
                page = self.server.store.task_logs_v2(
                    task_id, after_sequence=after_sequence, limit=limit
                )
            except RequestError as exc:
                self._json_error_v2(HTTPStatus.BAD_REQUEST, "invalid_request", str(exc))
            except KeyError:
                self._json_error_v2(HTTPStatus.NOT_FOUND, "not_found", "task not found")
            else:
                self._json(HTTPStatus.OK, page.model_dump(mode="json"))
            return
        task_timeline_match = DAEMON_V2_TASK_TIMELINE_PATH_PATTERN.fullmatch(parsed_path.path)
        if task_timeline_match:
            task_id = task_timeline_match.group(1)
            try:
                if not ID_PATTERN.fullmatch(task_id):
                    raise RequestError("task_id is invalid")
                parameters = parse_qs(
                    parsed_path.query, keep_blank_values=True, strict_parsing=True
                )
                if set(parameters) - {"after", "limit"} or any(
                    len(values) != 1 for values in parameters.values()
                ):
                    raise RequestError("timeline query contains unsupported parameters")
                after_raw = parameters.get("after", ["0"])[0]
                limit_raw = parameters.get("limit", [str(MAX_DAEMON_V2_LOG_PAGE)])[0]
                if not after_raw.isascii() or not after_raw.isdigit():
                    raise RequestError("timeline cursor must be a non-negative integer")
                if not limit_raw.isascii() or not limit_raw.isdigit():
                    raise RequestError("timeline limit must be an integer")
                after_sequence = int(after_raw)
                limit = int(limit_raw)
                if not 1 <= limit <= MAX_DAEMON_V2_LOG_PAGE:
                    raise RequestError("timeline limit is outside the supported bound")
                page = self.server.store.task_timeline_v2(
                    task_id, after_sequence=after_sequence, limit=limit
                )
            except RequestError as exc:
                self._json_error_v2(HTTPStatus.BAD_REQUEST, "invalid_request", str(exc))
            except KeyError:
                self._json_error_v2(HTTPStatus.NOT_FOUND, "not_found", "task not found")
            else:
                self._json(HTTPStatus.OK, page.model_dump(mode="json"))
            return
        task_artifacts_match = DAEMON_V2_TASK_ARTIFACTS_PATH_PATTERN.fullmatch(parsed_path.path)
        if task_artifacts_match:
            task_id = task_artifacts_match.group(1)
            try:
                if not ID_PATTERN.fullmatch(task_id):
                    raise RequestError("task_id is invalid")
                _, _, after_artifact_id, limit = self._artifact_page_query(
                    parsed_path.query,
                    allow_filters=False,
                    maximum_limit=MAX_DAEMON_V2_ARTIFACT_PAGE,
                )
                page = self.server.store.artifact_page_v2(
                    project_id=None,
                    task_id=task_id,
                    after_artifact_id=after_artifact_id,
                    limit=limit,
                    development_detail=False,
                )
            except RequestError as exc:
                self._json_error_v2(HTTPStatus.BAD_REQUEST, "invalid_request", str(exc))
            except KeyError:
                self._json_error_v2(HTTPStatus.NOT_FOUND, "not_found", "task not found")
            else:
                self._json(HTTPStatus.OK, page.model_dump(mode="json"))
            return
        artifact_content_match = DAEMON_V2_ARTIFACT_CONTENT_PATH_PATTERN.fullmatch(
            parsed_path.path
        )
        if artifact_content_match:
            artifact_id = artifact_content_match.group(1)
            try:
                if not ID_PATTERN.fullmatch(artifact_id):
                    raise RequestError("artifact_id is invalid")
                artifact = self.server.store.artifact_content_observation_v2(artifact_id)
            except RequestError as exc:
                self._json_error_v2(HTTPStatus.BAD_REQUEST, "invalid_request", str(exc))
            except KeyError:
                self._json_error_v2(HTTPStatus.NOT_FOUND, "not_found", "artifact not found")
            else:
                self._json(HTTPStatus.OK, artifact.model_dump(mode="json"))
            return
        artifact_match = DAEMON_V2_ARTIFACT_PATH_PATTERN.fullmatch(parsed_path.path)
        if artifact_match:
            artifact_id = artifact_match.group(1)
            try:
                if not ID_PATTERN.fullmatch(artifact_id):
                    raise RequestError("artifact_id is invalid")
                artifact = self.server.store.artifact_observation_v2(artifact_id)
            except RequestError as exc:
                self._json_error_v2(HTTPStatus.BAD_REQUEST, "invalid_request", str(exc))
            except KeyError:
                self._json_error_v2(HTTPStatus.NOT_FOUND, "not_found", "artifact not found")
            else:
                self._json(HTTPStatus.OK, artifact.model_dump(mode="json"))
            return
        if parsed_path.path == DAEMON_V2_DEVELOPMENT_ARTIFACTS_PATH:
            try:
                project_id, task_id, after_artifact_id, limit = self._artifact_page_query(
                    parsed_path.query,
                    allow_filters=True,
                    maximum_limit=MAX_DAEMON_V2_DEVELOPMENT_ARTIFACT_PAGE,
                )
                if project_id is None:
                    raise RequestError("development artifact query requires project_id")
                page = self.server.store.artifact_page_v2(
                    project_id=project_id,
                    task_id=task_id,
                    after_artifact_id=after_artifact_id,
                    limit=limit,
                    development_detail=True,
                )
            except RequestError as exc:
                self._json_error_v2(HTTPStatus.BAD_REQUEST, "invalid_request", str(exc))
            except KeyError:
                self._json_error_v2(HTTPStatus.NOT_FOUND, "not_found", "project or task not found")
            else:
                self._json(HTTPStatus.OK, page.model_dump(mode="json"))
            return
        development_artifact_match = DAEMON_V2_DEVELOPMENT_ARTIFACT_PATH_PATTERN.fullmatch(
            parsed_path.path
        )
        if development_artifact_match:
            artifact_id = development_artifact_match.group(1)
            try:
                if not ID_PATTERN.fullmatch(artifact_id):
                    raise RequestError("artifact_id is invalid")
                artifact = self.server.store.development_artifact_v2(artifact_id)
            except RequestError as exc:
                self._json_error_v2(HTTPStatus.BAD_REQUEST, "invalid_request", str(exc))
            except KeyError:
                self._json_error_v2(HTTPStatus.NOT_FOUND, "not_found", "artifact not found")
            else:
                self._json(HTTPStatus.OK, artifact.model_dump(mode="json"))
            return
        if parsed_path.path == DAEMON_V2_DEVELOPMENT_EVOLUTION_RUNS_PATH:
            try:
                parameters = parse_qs(
                    parsed_path.query, keep_blank_values=True, strict_parsing=True
                )
                if set(parameters) - {"project_id", "after", "limit"} or any(
                    len(values) != 1 for values in parameters.values()
                ):
                    raise RequestError("Evolution Run query contains unsupported parameters")
                project_id = parameters.get("project_id", [None])[0]
                if project_id is None or not ID_PATTERN.fullmatch(project_id):
                    raise RequestError("project_id is invalid")
                after_run_id = parameters.get("after", [None])[0]
                if after_run_id is not None and not ID_PATTERN.fullmatch(after_run_id):
                    raise RequestError("Evolution Run cursor is invalid")
                limit_raw = parameters.get("limit", [str(MAX_DAEMON_V2_EVOLUTION_RUN_PAGE)])[0]
                if not limit_raw.isascii() or not limit_raw.isdigit():
                    raise RequestError("Evolution Run limit must be an integer")
                limit = int(limit_raw)
                if not 1 <= limit <= MAX_DAEMON_V2_EVOLUTION_RUN_PAGE:
                    raise RequestError("Evolution Run limit is outside the supported bound")
                page = self.server.store.evolution_run_page_v2(
                    project_id=project_id,
                    after_run_id=after_run_id,
                    limit=limit,
                )
            except RequestError as exc:
                self._json_error_v2(HTTPStatus.BAD_REQUEST, "invalid_request", str(exc))
            except KeyError:
                self._json_error_v2(HTTPStatus.NOT_FOUND, "not_found", "project not found")
            else:
                self._json(HTTPStatus.OK, page.model_dump(mode="json"))
            return
        if parsed_path.path == DAEMON_V2_DEVELOPMENT_EVOLUTION_JOBS_PATH:
            try:
                parameters = parse_qs(
                    parsed_path.query, keep_blank_values=True, strict_parsing=True
                )
                if set(parameters) - {"project_id", "after", "limit"} or any(
                    len(values) != 1 for values in parameters.values()
                ):
                    raise RequestError("Evolution Job query contains unsupported parameters")
                project_id = parameters.get("project_id", [None])[0]
                if project_id is None or not ID_PATTERN.fullmatch(project_id):
                    raise RequestError("project_id is invalid")
                after_job_id = parameters.get("after", [None])[0]
                if after_job_id is not None and not ID_PATTERN.fullmatch(after_job_id):
                    raise RequestError("Evolution Job cursor is invalid")
                limit_raw = parameters.get("limit", [str(MAX_DAEMON_V2_EVOLUTION_JOB_PAGE)])[0]
                if not limit_raw.isascii() or not limit_raw.isdigit():
                    raise RequestError("Evolution Job limit must be an integer")
                limit = int(limit_raw)
                if not 1 <= limit <= MAX_DAEMON_V2_EVOLUTION_JOB_PAGE:
                    raise RequestError("Evolution Job limit is outside the supported bound")
                page = self.server.store.evolution_job_page_v2(
                    project_id=project_id,
                    after_job_id=after_job_id,
                    limit=limit,
                )
            except RequestError as exc:
                self._json_error_v2(HTTPStatus.BAD_REQUEST, "invalid_request", str(exc))
            except KeyError:
                self._json_error_v2(HTTPStatus.NOT_FOUND, "not_found", "project not found")
            else:
                self._json(HTTPStatus.OK, page.model_dump(mode="json"))
            return
        evolution_job_match = DAEMON_V2_DEVELOPMENT_EVOLUTION_JOB_PATH_PATTERN.fullmatch(
            parsed_path.path
        )
        if evolution_job_match:
            job_id = evolution_job_match.group(1)
            try:
                if not ID_PATTERN.fullmatch(job_id):
                    raise RequestError("job_id is invalid")
                job = self.server.store.evolution_job_v2(job_id)
            except RequestError as exc:
                self._json_error_v2(HTTPStatus.BAD_REQUEST, "invalid_request", str(exc))
            except KeyError:
                self._json_error_v2(HTTPStatus.NOT_FOUND, "not_found", "Evolution Job not found")
            else:
                self._json(HTTPStatus.OK, job.model_dump(mode="json"))
            return
        evolution_run_match = DAEMON_V2_DEVELOPMENT_EVOLUTION_RUN_PATH_PATTERN.fullmatch(
            parsed_path.path
        )
        if evolution_run_match:
            run_id = evolution_run_match.group(1)
            try:
                if not ID_PATTERN.fullmatch(run_id):
                    raise RequestError("run_id is invalid")
                run = self.server.store.evolution_run_v2(run_id)
            except RequestError as exc:
                self._json_error_v2(HTTPStatus.BAD_REQUEST, "invalid_request", str(exc))
            except KeyError:
                self._json_error_v2(HTTPStatus.NOT_FOUND, "not_found", "Evolution Run not found")
            else:
                self._json(HTTPStatus.OK, run.model_dump(mode="json"))
            return
        if parsed_path.path == DAEMON_V2_DEVELOPMENT_TASKS_PATH:
            try:
                parameters = parse_qs(
                    parsed_path.query, keep_blank_values=True, strict_parsing=True
                )
                if set(parameters) - {"project_id", "after", "limit"} or any(
                    len(values) != 1 for values in parameters.values()
                ):
                    raise RequestError("Task presentation query contains unsupported parameters")
                project_id = parameters.get("project_id", [None])[0]
                if project_id is None or not ID_PATTERN.fullmatch(project_id):
                    raise RequestError("project_id is required and must be valid")
                after = parameters.get("after", [None])[0]
                if after == "":
                    raise RequestError("Task presentation cursor cannot be empty")
                limit_raw = parameters.get("limit", [str(MAX_DAEMON_V2_TASK_PRESENTATION_PAGE)])[0]
                if not limit_raw.isascii() or not limit_raw.isdigit():
                    raise RequestError("Task presentation limit must be an integer")
                limit = int(limit_raw)
                if not 1 <= limit <= MAX_DAEMON_V2_TASK_PRESENTATION_PAGE:
                    raise RequestError("Task presentation limit is outside the supported bound")
                page = self.server.store.task_presentations_v2(
                    project_id=project_id,
                    after_task_id=after,
                    limit=limit,
                )
            except RequestError as exc:
                self._json_error_v2(HTTPStatus.BAD_REQUEST, "invalid_request", str(exc))
            except KeyError:
                self._json_error_v2(HTTPStatus.NOT_FOUND, "not_found", "project not found")
            else:
                self._json(HTTPStatus.OK, page.model_dump(mode="json"))
            return
        presentation_match = DAEMON_V2_DEVELOPMENT_TASK_PATH_PATTERN.fullmatch(parsed_path.path)
        if presentation_match:
            task_id = presentation_match.group(1)
            try:
                if not ID_PATTERN.fullmatch(task_id):
                    raise RequestError("task_id is invalid")
                presentation = self.server.store.task_presentation_v2(task_id)
            except RequestError as exc:
                self._json_error_v2(HTTPStatus.BAD_REQUEST, "invalid_request", str(exc))
            except KeyError:
                self._json_error_v2(HTTPStatus.NOT_FOUND, "not_found", "task not found")
            else:
                self._json(HTTPStatus.OK, presentation.model_dump(mode="json"))
            return
        task_match = DAEMON_V2_TASK_PATH_PATTERN.fullmatch(parsed_path.path)
        if task_match:
            task_id = task_match.group(1)
            if not ID_PATTERN.fullmatch(task_id):
                self._json_error_v2(
                    HTTPStatus.BAD_REQUEST, "invalid_request", "task_id is invalid"
                )
                return
            try:
                task = self.server.store.task_observation_v2(task_id)
            except KeyError:
                self._json_error_v2(HTTPStatus.NOT_FOUND, "not_found", "task not found")
            else:
                self._json(HTTPStatus.OK, task.model_dump(mode="json"))
            return
        workspace_page_match = DAEMON_V2_WORKSPACE_PATH_PATTERN.fullmatch(parsed_path.path)
        if workspace_page_match:
            project_id = workspace_page_match.group(1)
            try:
                if not ID_PATTERN.fullmatch(project_id):
                    raise RequestError("project_id is invalid")
                parameters = parse_qs(
                    parsed_path.query, keep_blank_values=True, strict_parsing=True
                )
                if set(parameters) - {"after", "limit", "manifest_sha256"} or any(
                    len(values) != 1 for values in parameters.values()
                ):
                    raise RequestError("workspace query contains unsupported parameters")
                after_path = parameters.get("after", [None])[0]
                if after_path == "":
                    raise RequestError("workspace cursor cannot be empty")
                manifest_sha256 = parameters.get("manifest_sha256", [None])[0]
                if manifest_sha256 is not None and not re.fullmatch(
                    r"[0-9a-f]{64}", manifest_sha256
                ):
                    raise RequestError("workspace manifest digest is invalid")
                limit_raw = parameters.get("limit", [str(MAX_DAEMON_V2_WORKSPACE_PAGE)])[0]
                if not limit_raw.isascii() or not limit_raw.isdigit():
                    raise RequestError("workspace limit must be an integer")
                limit = int(limit_raw)
                if not 1 <= limit <= MAX_DAEMON_V2_WORKSPACE_PAGE:
                    raise RequestError("workspace limit is outside the supported bound")
                page = self.server.store.workspace_page_v2(
                    project_id,
                    after_path=after_path,
                    expected_manifest_sha256=manifest_sha256,
                    limit=limit,
                )
            except RequestError as exc:
                self._json_error_v2(HTTPStatus.BAD_REQUEST, "invalid_request", str(exc))
            except KeyError:
                self._json_error_v2(HTTPStatus.NOT_FOUND, "not_found", "project not found")
            except StateConflictError as exc:
                self._json_error_v2(HTTPStatus.CONFLICT, "state_conflict", str(exc))
            else:
                self._json(HTTPStatus.OK, page.model_dump(mode="json"))
            return
        workspace_file_v2_match = DAEMON_V2_WORKSPACE_FILES_PATH_PATTERN.fullmatch(
            parsed_path.path
        )
        if workspace_file_v2_match:
            project_id = workspace_file_v2_match.group(1)
            try:
                if not ID_PATTERN.fullmatch(project_id):
                    raise RequestError("project_id is invalid")
                relative_path, _ = self._workspace_query(parsed_path.query, allow_overwrite=False)
                payload, media_type, file_name = self.server.store.download_workspace_file(
                    project_id, relative_path
                )
            except RequestError as exc:
                self._json_error_v2(HTTPStatus.BAD_REQUEST, "invalid_request", str(exc))
            except KeyError:
                self._json_error_v2(HTTPStatus.NOT_FOUND, "not_found", "workspace file not found")
            else:
                self._binary(
                    payload,
                    media_type,
                    file_name,
                    content_sha256=hashlib.sha256(payload).hexdigest(),
                )
            return
        workspace_match = WORKSPACE_FILES_PATH_PATTERN.fullmatch(parsed_path.path)
        if workspace_match:
            project_id = workspace_match.group(1)
            if not ID_PATTERN.fullmatch(project_id):
                self._json_error(
                    HTTPStatus.BAD_REQUEST, "invalid_request", "project_id is invalid"
                )
                return
            try:
                relative_path, _ = self._workspace_query(parsed_path.query, allow_overwrite=False)
                payload, media_type, file_name = self.server.store.download_workspace_file(
                    project_id,
                    relative_path,
                )
            except RequestError as exc:
                self._json_error(HTTPStatus.BAD_REQUEST, "invalid_request", str(exc))
            except KeyError:
                self._json_error(HTTPStatus.NOT_FOUND, "not_found", "workspace file not found")
            else:
                self._binary(payload, media_type, file_name)
            return
        if self.path == "/openevo-dev-agent/health":
            self._json(HTTPStatus.OK, {"schema_version": "1", "status": "ready"})
            return
        if parsed_path.path == DAEMON_V2_DEVELOPMENT_STATE_PATH:
            self._json(
                HTTPStatus.OK,
                self.server.store.state_v2().model_dump(mode="json"),
            )
            return
        if parsed_path.path == DAEMON_V2_DEVELOPMENT_CAPABILITIES_PATH:
            if self.server.evolution_runner is None:
                self._json_error_v2(
                    HTTPStatus.SERVICE_UNAVAILABLE,
                    "capabilities_unavailable",
                    "development evolution runner is unavailable",
                )
            else:
                legacy = self.server.evolution_runner.capabilities()
                response = DevelopmentCapabilitiesV2(
                    authority="development_catalog_unverified",
                    capabilities=legacy["capabilities"],
                )
                self._json(HTTPStatus.OK, response.model_dump(mode="json"))
            return
        if parsed_path.path == DAEMON_V2_DEVELOPMENT_PROJECTS_PATH:
            state = self.server.store.state_v2()
            self._json(
                HTTPStatus.OK,
                {
                    "schema_version": "2",
                    "items": [item.model_dump(mode="json") for item in state.projects],
                    "next_cursor": None,
                    "has_more": False,
                },
            )
            return
        project_v2_match = DAEMON_V2_DEVELOPMENT_PROJECT_PATH_PATTERN.fullmatch(parsed_path.path)
        if project_v2_match:
            project_id = project_v2_match.group(1)
            try:
                if not ID_PATTERN.fullmatch(project_id):
                    raise RequestError("project_id is invalid")
                project = self.server.store.project(project_id)
                response = DevelopmentProjectAuthorityV2.model_validate(
                    {
                        "schema_version": "2",
                        **project,
                    }
                )
            except RequestError as exc:
                self._json_error_v2(HTTPStatus.BAD_REQUEST, "invalid_request", str(exc))
            except KeyError:
                self._json_error_v2(HTTPStatus.NOT_FOUND, "not_found", "project not found")
            else:
                self._json(HTTPStatus.OK, response.model_dump(mode="json"))
            return
        if self.path == "/openevo-dev-agent/v1/state":
            self._json(HTTPStatus.OK, self.server.store.snapshot())
            return
        if parsed_path.path in {
            DEVELOPMENT_EVENTS_PATH,
            DAEMON_V2_DEVELOPMENT_EVENTS_PATH,
        }:
            try:
                parameters = parse_qs(
                    parsed_path.query,
                    keep_blank_values=True,
                    strict_parsing=True,
                )
                if set(parameters) - {"after", "limit", "wait_ms"}:
                    raise RequestError("event query contains unknown parameters")
                if any(len(values) != 1 for values in parameters.values()):
                    raise RequestError("event query parameters must be singular")
                after_raw = parameters.get("after", [None])[0]
                if after_raw is None:
                    after_sequence = None
                elif not after_raw.isascii() or not after_raw.isdigit():
                    raise RequestError("event cursor must be a non-negative integer")
                else:
                    after_sequence = int(after_raw)
                limit_raw = parameters.get("limit", [str(MAX_DEVELOPMENT_EVENT_PAGE)])[0]
                wait_raw = parameters.get("wait_ms", ["0"])[0]
                if not limit_raw.isascii() or not limit_raw.isdigit():
                    raise RequestError("event limit must be an integer")
                if not wait_raw.isascii() or not wait_raw.isdigit():
                    raise RequestError("event wait_ms must be an integer")
                limit = int(limit_raw)
                wait_ms = int(wait_raw)
                if not 1 <= limit <= MAX_DEVELOPMENT_EVENT_PAGE:
                    raise RequestError("event limit is outside the supported bound")
                if not 0 <= wait_ms <= int(MAX_DEVELOPMENT_EVENT_WAIT_SECONDS * 1000):
                    raise RequestError("event wait_ms is outside the supported bound")
                result = self.server.store.read_events(
                    after_sequence=after_sequence,
                    limit=limit,
                    wait_seconds=wait_ms / 1000,
                )
                if parsed_path.path == DAEMON_V2_DEVELOPMENT_EVENTS_PATH:
                    result = {**result, "schema_version": "2"}
            except (RequestError, ValueError) as exc:
                self._json_error(HTTPStatus.BAD_REQUEST, "invalid_request", str(exc))
            except EventCursorExpiredError as exc:
                self._json_error(HTTPStatus.GONE, "event_cursor_expired", str(exc))
            else:
                self._json(HTTPStatus.OK, result)
            return
        if self.path == "/openevo-dev-agent/v1/capabilities":
            if self.server.evolution_runner is None:
                self._json_error(
                    HTTPStatus.SERVICE_UNAVAILABLE,
                    "capabilities_unavailable",
                    "development evolution runner is unavailable",
                )
            else:
                self._json(HTTPStatus.OK, self.server.evolution_runner.capabilities())
            return
        if self.path == "/openevo-dev-agent/v1/runtime-capabilities":
            self._json(HTTPStatus.OK, self.server.runner.runtime_capabilities())
            return
        session_match = SESSION_PATH_PATTERN.fullmatch(self.path)
        if session_match:
            session_id = session_match.group(1)
            if not ID_PATTERN.fullmatch(session_id):
                self._json_error(
                    HTTPStatus.BAD_REQUEST, "invalid_request", "session_id is invalid"
                )
                return
            try:
                session = self.server.store.get_session(session_id)
            except KeyError:
                self._json_error(HTTPStatus.NOT_FOUND, "not_found", "session not found")
            else:
                self._json(HTTPStatus.OK, {"schema_version": "1", "session": session})
            return
        self._json_error(HTTPStatus.NOT_FOUND, "not_found", "endpoint not found")

    def do_POST(self) -> None:  # noqa: N802
        if not self._authorized():
            return
        if self.path == DAEMON_V2_DEVELOPMENT_PROJECTS_PATH:
            try:
                request = DevelopmentProjectCreateV2.model_validate(self._read_json())
                project = self.server.store.create_project(
                    {
                        "project_id": request.project_id,
                        "display_name": request.display_name,
                        "config": request.config.model_dump(mode="json"),
                    }
                )
                response = DevelopmentProjectAuthorityV2.model_validate(
                    {"schema_version": "2", **project}
                )
            except ValidationError as exc:
                self._json_error_v2(HTTPStatus.BAD_REQUEST, "invalid_request", str(exc))
            except StateConflictError as exc:
                self._json_error_v2(HTTPStatus.CONFLICT, "state_conflict", str(exc))
            else:
                self._json(HTTPStatus.CREATED, response.model_dump(mode="json"))
            return
        activate_project_v2_match = DAEMON_V2_DEVELOPMENT_PROJECT_ACTIVATE_PATH_PATTERN.fullmatch(
            self.path
        )
        if activate_project_v2_match:
            project_id = activate_project_v2_match.group(1)
            try:
                if not ID_PATTERN.fullmatch(project_id):
                    raise RequestError("project_id is invalid")
                DevelopmentProjectActivateV2.model_validate(self._read_json())
                self.server.store.activate_project(project_id)
                project = self.server.store.project(project_id)
                response = DevelopmentProjectAuthorityV2.model_validate(
                    {"schema_version": "2", **project}
                )
            except ValidationError as exc:
                self._json_error_v2(HTTPStatus.BAD_REQUEST, "invalid_request", str(exc))
            except RequestError as exc:
                self._json_error_v2(HTTPStatus.BAD_REQUEST, "invalid_request", str(exc))
            except KeyError:
                self._json_error_v2(HTTPStatus.NOT_FOUND, "not_found", "project not found")
            else:
                self._json(HTTPStatus.OK, response.model_dump(mode="json"))
            return
        if self.path == DAEMON_V2_DEVELOPMENT_TASKS_PATH:
            try:
                request = DevelopmentTaskCreateV2.model_validate(self._read_json())
                task_id = f"dev-session-{hashlib.sha256(request.action_id.encode('utf-8')).hexdigest()[:16]}"
                self.server.sessions.submit(
                    {
                        "project_id": request.project_id,
                        "project_head_id": request.project_head_id,
                        "project_name": request.project_name,
                        "task_title": request.task_title,
                        "instruction": request.instruction,
                    },
                    session_id=task_id,
                )
                response = self.server.store.task_presentation_v2(task_id)
            except ValidationError as exc:
                self._json_error_v2(HTTPStatus.BAD_REQUEST, "invalid_request", str(exc))
            except KeyError:
                self._json_error_v2(HTTPStatus.NOT_FOUND, "not_found", "project not found")
            except StateConflictError as exc:
                self._json_error_v2(HTTPStatus.CONFLICT, "state_conflict", str(exc))
            else:
                self._json(HTTPStatus.ACCEPTED, response.model_dump(mode="json"))
            return
        cancel_task_v2_match = DAEMON_V2_DEVELOPMENT_TASK_CANCEL_PATH_PATTERN.fullmatch(self.path)
        if cancel_task_v2_match:
            task_id = cancel_task_v2_match.group(1)
            try:
                if not ID_PATTERN.fullmatch(task_id):
                    raise RequestError("task_id is invalid")
                DevelopmentTaskCancelV2.model_validate(self._read_json())
                self.server.sessions.cancel(task_id)
                response = self.server.store.task_presentation_v2(task_id)
            except ValidationError as exc:
                self._json_error_v2(HTTPStatus.BAD_REQUEST, "invalid_request", str(exc))
            except RequestError as exc:
                self._json_error_v2(HTTPStatus.BAD_REQUEST, "invalid_request", str(exc))
            except KeyError:
                self._json_error_v2(HTTPStatus.NOT_FOUND, "not_found", "task not found")
            except StateConflictError as exc:
                self._json_error_v2(HTTPStatus.CONFLICT, "state_conflict", str(exc))
            else:
                self._json(HTTPStatus.ACCEPTED, response.model_dump(mode="json"))
            return
        if self.path == DAEMON_V2_DEVELOPMENT_EVOLUTION_RUNS_PATH:
            try:
                request = DevelopmentEvolutionRunCreateV2.model_validate(self._read_json())
                run = self.server.sessions.submit_evolution(
                    {
                        "action_id": request.action_id,
                        "project_id": request.project_id,
                        "session_ids": request.source_task_ids,
                        "selections": [
                            {
                                "target_id": selection.target_id,
                                "method": selection.method,
                                "config": selection.config,
                            }
                            for selection in request.selections
                        ],
                    }
                )
                response = self.server.store.evolution_run_v2(run["run_id"])
            except ValidationError as exc:
                self._json_error_v2(HTTPStatus.BAD_REQUEST, "invalid_request", str(exc))
            except RequestError as exc:
                self._json_error_v2(HTTPStatus.BAD_REQUEST, "invalid_request", str(exc))
            except KeyError:
                self._json_error_v2(HTTPStatus.NOT_FOUND, "not_found", "project not found")
            except StateConflictError as exc:
                self._json_error_v2(HTTPStatus.CONFLICT, "state_conflict", str(exc))
            else:
                self._json(HTTPStatus.ACCEPTED, response.model_dump(mode="json"))
            return
        retry_job_v2_match = DAEMON_V2_DEVELOPMENT_EVOLUTION_JOB_RETRY_PATH_PATTERN.fullmatch(
            self.path
        )
        if retry_job_v2_match:
            job_id = retry_job_v2_match.group(1)
            try:
                if not ID_PATTERN.fullmatch(job_id):
                    raise RequestError("job_id is invalid")
                request = DevelopmentEvolutionJobRetryV2.model_validate(self._read_json())
                self.server.sessions.retry_evolution(
                    job_id,
                    action_id=request.action_id,
                )
                response = self.server.store.evolution_job_v2(job_id)
            except ValidationError as exc:
                self._json_error_v2(HTTPStatus.BAD_REQUEST, "invalid_request", str(exc))
            except RequestError as exc:
                self._json_error_v2(HTTPStatus.BAD_REQUEST, "invalid_request", str(exc))
            except KeyError:
                self._json_error_v2(HTTPStatus.NOT_FOUND, "not_found", "Evolution Job not found")
            except StateConflictError as exc:
                self._json_error_v2(HTTPStatus.CONFLICT, "state_conflict", str(exc))
            else:
                self._json(HTTPStatus.ACCEPTED, response.model_dump(mode="json"))
            return
        apply_v2_match = DAEMON_V2_DEVELOPMENT_EVOLUTION_RUN_APPLY_PATH_PATTERN.fullmatch(
            self.path
        )
        if apply_v2_match:
            run_id = apply_v2_match.group(1)
            try:
                if not ID_PATTERN.fullmatch(run_id):
                    raise RequestError("run_id is invalid")
                DevelopmentEvolutionRunApplyV2.model_validate(self._read_json())
                self.server.sessions.apply_evolution(run_id)
                response = self.server.store.evolution_run_v2(run_id)
            except ValidationError as exc:
                self._json_error_v2(HTTPStatus.BAD_REQUEST, "invalid_request", str(exc))
            except RequestError as exc:
                self._json_error_v2(HTTPStatus.BAD_REQUEST, "invalid_request", str(exc))
            except KeyError:
                self._json_error_v2(HTTPStatus.NOT_FOUND, "not_found", "Evolution Run not found")
            except StateConflictError as exc:
                self._json_error_v2(HTTPStatus.CONFLICT, "state_conflict", str(exc))
            else:
                self._json(HTTPStatus.OK, response.model_dump(mode="json"))
            return
        if self.path == "/openevo-dev-agent/v1/projects":
            try:
                project = self.server.store.create_project(
                    validate_project_request(self._read_json())
                )
            except RequestError as exc:
                self._json_error(HTTPStatus.BAD_REQUEST, "invalid_request", str(exc))
            except StateConflictError as exc:
                self._json_error(HTTPStatus.CONFLICT, "state_conflict", str(exc))
            else:
                self._json(HTTPStatus.CREATED, {"schema_version": "1", **project})
            return
        activate_match = ACTIVATE_PATH_PATTERN.fullmatch(self.path)
        if activate_match:
            project_id = activate_match.group(1)
            if not ID_PATTERN.fullmatch(project_id):
                self._json_error(
                    HTTPStatus.BAD_REQUEST, "invalid_request", "project_id is invalid"
                )
                return
            try:
                payload = self._read_json()
                if payload != {"schema_version": "1"}:
                    raise RequestError("activation request must contain only schema_version '1'")
                self.server.store.activate_project(project_id)
            except RequestError as exc:
                self._json_error(HTTPStatus.BAD_REQUEST, "invalid_request", str(exc))
            except KeyError:
                self._json_error(HTTPStatus.NOT_FOUND, "not_found", "project not found")
            else:
                self._json(HTTPStatus.OK, {"schema_version": "1", "project_id": project_id})
            return
        retry_match = EVOLUTION_JOB_RETRY_PATH_PATTERN.fullmatch(self.path)
        if retry_match:
            job_id = retry_match.group(1)
            if not ID_PATTERN.fullmatch(job_id):
                self._json_error(HTTPStatus.BAD_REQUEST, "invalid_request", "job_id is invalid")
                return
            try:
                payload = self._read_json()
                if payload != {"schema_version": "1"}:
                    raise RequestError("retry request must contain only schema_version '1'")
                job = self.server.sessions.retry_evolution(job_id)
            except RequestError as exc:
                self._json_error(HTTPStatus.BAD_REQUEST, "invalid_request", str(exc))
            except KeyError:
                self._json_error(HTTPStatus.NOT_FOUND, "not_found", "Evolution Job not found")
            except StateConflictError as exc:
                self._json_error(HTTPStatus.CONFLICT, "state_conflict", str(exc))
            else:
                self._json(HTTPStatus.ACCEPTED, {"schema_version": "1", "job": job})
            return
        if self.path == "/openevo-dev-agent/v1/evolution-runs":
            try:
                run = self.server.sessions.submit_evolution(
                    validate_evolution_run_request(self._read_json())
                )
            except RequestError as exc:
                self._json_error(HTTPStatus.BAD_REQUEST, "invalid_request", str(exc))
            except KeyError:
                self._json_error(HTTPStatus.NOT_FOUND, "not_found", "Project not found")
            except StateConflictError as exc:
                self._json_error(HTTPStatus.CONFLICT, "state_conflict", str(exc))
            else:
                self._json(HTTPStatus.ACCEPTED, {"schema_version": "1", "run": run})
            return
        apply_match = EVOLUTION_RUN_APPLY_PATH_PATTERN.fullmatch(self.path)
        if apply_match:
            run_id = apply_match.group(1)
            if not ID_PATTERN.fullmatch(run_id):
                self._json_error(HTTPStatus.BAD_REQUEST, "invalid_request", "run_id is invalid")
                return
            try:
                payload = self._read_json()
                if payload != {"schema_version": "1"}:
                    raise RequestError("apply request must contain only schema_version '1'")
                run = self.server.sessions.apply_evolution(run_id)
            except RequestError as exc:
                self._json_error(HTTPStatus.BAD_REQUEST, "invalid_request", str(exc))
            except KeyError:
                self._json_error(HTTPStatus.NOT_FOUND, "not_found", "Evolution Run not found")
            except StateConflictError as exc:
                self._json_error(HTTPStatus.CONFLICT, "state_conflict", str(exc))
            else:
                self._json(HTTPStatus.OK, {"schema_version": "1", "run": run})
            return
        cancel_match = SESSION_CANCEL_PATH_PATTERN.fullmatch(self.path)
        if cancel_match:
            session_id = cancel_match.group(1)
            if not ID_PATTERN.fullmatch(session_id):
                self._json_error(
                    HTTPStatus.BAD_REQUEST, "invalid_request", "session_id is invalid"
                )
                return
            try:
                payload = self._read_json()
                if payload != {"schema_version": "1"}:
                    raise RequestError("cancellation request must contain only schema_version '1'")
                session = self.server.sessions.cancel(session_id)
            except RequestError as exc:
                self._json_error(HTTPStatus.BAD_REQUEST, "invalid_request", str(exc))
            except KeyError:
                self._json_error(HTTPStatus.NOT_FOUND, "not_found", "session not found")
            except StateConflictError as exc:
                self._json_error(HTTPStatus.CONFLICT, "state_conflict", str(exc))
            else:
                self._json(HTTPStatus.ACCEPTED, {"schema_version": "1", "session": session})
            return
        if self.path != "/openevo-dev-agent/v1/sessions":
            self._json_error(HTTPStatus.NOT_FOUND, "not_found", "endpoint not found")
            return
        self._run_session()

    def do_PUT(self) -> None:  # noqa: N802
        if not self._authorized():
            return
        parsed_path = urlsplit(self.path)
        project_v2_match = DAEMON_V2_DEVELOPMENT_PROJECT_PATH_PATTERN.fullmatch(parsed_path.path)
        if project_v2_match:
            project_id = project_v2_match.group(1)
            try:
                if not ID_PATTERN.fullmatch(project_id):
                    raise RequestError("project_id is invalid")
                request = DevelopmentProjectUpdateV2.model_validate(self._read_json())
                project = self.server.store.update_project(
                    project_id,
                    {
                        "display_name": request.display_name,
                        "config": request.config.model_dump(mode="json"),
                    },
                )
                response = DevelopmentProjectAuthorityV2.model_validate(
                    {"schema_version": "2", **project}
                )
            except ValidationError as exc:
                self._json_error_v2(HTTPStatus.BAD_REQUEST, "invalid_request", str(exc))
            except RequestError as exc:
                self._json_error_v2(HTTPStatus.BAD_REQUEST, "invalid_request", str(exc))
            except KeyError:
                self._json_error_v2(HTTPStatus.NOT_FOUND, "not_found", "project not found")
            except StateConflictError as exc:
                self._json_error_v2(HTTPStatus.CONFLICT, "state_conflict", str(exc))
            else:
                self._json(HTTPStatus.OK, response.model_dump(mode="json"))
            return
        workspace_v2_match = DAEMON_V2_WORKSPACE_FILES_PATH_PATTERN.fullmatch(parsed_path.path)
        if workspace_v2_match:
            project_id = workspace_v2_match.group(1)
            try:
                if not ID_PATTERN.fullmatch(project_id):
                    raise RequestError("project_id is invalid")
                relative_path, overwrite = self._workspace_query(
                    parsed_path.query, allow_overwrite=True
                )
                expected_digest = self.headers.get("X-OpenEvo-Content-SHA256", "")
                if not re.fullmatch(r"[0-9a-f]{64}", expected_digest):
                    raise RequestError("X-OpenEvo-Content-SHA256 is required")
                payload = self._read_bytes(MAX_WORKSPACE_UPLOAD_FILE_BYTES)
                if not hmac.compare_digest(hashlib.sha256(payload).hexdigest(), expected_digest):
                    raise RequestError("uploaded workspace file digest does not match")
                mutation = self.server.sessions.upload_workspace_file_v2(
                    project_id, relative_path, payload, overwrite=overwrite
                )
            except RequestError as exc:
                self._json_error_v2(HTTPStatus.BAD_REQUEST, "invalid_request", str(exc))
            except KeyError:
                self._json_error_v2(HTTPStatus.NOT_FOUND, "not_found", "project not found")
            except StateConflictError as exc:
                self._json_error_v2(HTTPStatus.CONFLICT, "state_conflict", str(exc))
            else:
                self._json(HTTPStatus.CREATED, mutation.model_dump(mode="json"))
            return
        workspace_match = WORKSPACE_FILES_PATH_PATTERN.fullmatch(parsed_path.path)
        if workspace_match:
            project_id = workspace_match.group(1)
            if not ID_PATTERN.fullmatch(project_id):
                self._json_error(
                    HTTPStatus.BAD_REQUEST, "invalid_request", "project_id is invalid"
                )
                return
            try:
                relative_path, overwrite = self._workspace_query(
                    parsed_path.query,
                    allow_overwrite=True,
                )
                entry = self.server.sessions.upload_workspace_file(
                    project_id,
                    relative_path,
                    self._read_bytes(MAX_WORKSPACE_UPLOAD_FILE_BYTES),
                    overwrite=overwrite,
                )
            except RequestError as exc:
                self._json_error(HTTPStatus.BAD_REQUEST, "invalid_request", str(exc))
            except KeyError:
                self._json_error(HTTPStatus.NOT_FOUND, "not_found", "project not found")
            except StateConflictError as exc:
                self._json_error(HTTPStatus.CONFLICT, "state_conflict", str(exc))
            else:
                self._json(
                    HTTPStatus.CREATED,
                    {"schema_version": "1", "project_id": project_id, "entry": entry},
                )
            return
        project_match = PROJECT_PATH_PATTERN.fullmatch(parsed_path.path)
        if not project_match:
            self._json_error(HTTPStatus.NOT_FOUND, "not_found", "endpoint not found")
            return
        project_id = project_match.group(1)
        if not ID_PATTERN.fullmatch(project_id):
            self._json_error(HTTPStatus.BAD_REQUEST, "invalid_request", "project_id is invalid")
            return
        try:
            project = self.server.store.update_project(
                project_id,
                validate_project_request(self._read_json(), updating=True),
            )
        except RequestError as exc:
            self._json_error(HTTPStatus.BAD_REQUEST, "invalid_request", str(exc))
        except KeyError:
            self._json_error(HTTPStatus.NOT_FOUND, "not_found", "project not found")
        else:
            self._json(HTTPStatus.OK, {"schema_version": "1", **project})

    def do_DELETE(self) -> None:  # noqa: N802
        if not self._authorized():
            return
        parsed_path = urlsplit(self.path)
        workspace_match = DAEMON_V2_WORKSPACE_FILES_PATH_PATTERN.fullmatch(parsed_path.path)
        if not workspace_match:
            self._json_error_v2(HTTPStatus.NOT_FOUND, "not_found", "endpoint not found")
            return
        project_id = workspace_match.group(1)
        try:
            if not ID_PATTERN.fullmatch(project_id):
                raise RequestError("project_id is invalid")
            relative_path, _ = self._workspace_query(parsed_path.query, allow_overwrite=False)
            result = self.server.sessions.delete_workspace_file_v2(project_id, relative_path)
        except RequestError as exc:
            self._json_error_v2(HTTPStatus.BAD_REQUEST, "invalid_request", str(exc))
        except KeyError:
            self._json_error_v2(HTTPStatus.NOT_FOUND, "not_found", "workspace file not found")
        except StateConflictError as exc:
            self._json_error_v2(HTTPStatus.CONFLICT, "state_conflict", str(exc))
        else:
            self._json(HTTPStatus.OK, result.model_dump(mode="json"))

    def _run_session(self) -> None:
        try:
            request = validate_request(self._read_json())
        except RequestError as exc:
            self._json_error(HTTPStatus.BAD_REQUEST, "invalid_request", str(exc))
            return
        try:
            session_id = self.server.sessions.submit(request)
        except KeyError:
            self._json_error(HTTPStatus.NOT_FOUND, "not_found", "project not found")
        except StateConflictError as exc:
            code = "agent_busy" if "another development session" in str(exc) else "state_conflict"
            self._json_error(HTTPStatus.CONFLICT, code, str(exc))
        else:
            self._json(
                HTTPStatus.ACCEPTED,
                {
                    "schema_version": "1",
                    "session_id": session_id,
                    "state": "running",
                    "status_url": f"/openevo-dev-agent/v1/sessions/{session_id}",
                },
            )

    def _read_json(self) -> object:
        try:
            content_length = int(self.headers.get("Content-Length", ""))
        except ValueError as exc:
            raise RequestError("Content-Length is invalid") from exc
        if content_length <= 0:
            raise RequestError("request body is empty")
        if content_length > MAX_REQUEST_BYTES:
            raise RequestError("request body is too large")
        try:
            return json.loads(self.rfile.read(content_length).decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise RequestError(f"request body is not valid UTF-8 JSON: {exc}") from exc

    def _read_bytes(self, maximum: int) -> bytes:
        try:
            content_length = int(self.headers.get("Content-Length", ""))
        except ValueError as exc:
            raise RequestError("Content-Length is invalid") from exc
        if content_length < 0:
            raise RequestError("Content-Length is invalid")
        if content_length > maximum:
            raise RequestError(f"request body exceeds the {maximum // (1024 * 1024)} MiB limit")
        payload = self.rfile.read(content_length)
        if len(payload) != content_length:
            raise RequestError("request body ended before Content-Length bytes were received")
        return payload

    @staticmethod
    def _workspace_query(query: str, *, allow_overwrite: bool) -> tuple[str, bool]:
        try:
            parameters = parse_qs(query, keep_blank_values=True, strict_parsing=True)
        except ValueError as exc:
            raise RequestError("workspace file query is invalid") from exc
        allowed = {"path", "overwrite"} if allow_overwrite else {"path"}
        if set(parameters) - allowed:
            raise RequestError("workspace file query contains unknown fields")
        paths = parameters.get("path", [])
        if len(paths) != 1 or not paths[0]:
            raise RequestError("workspace file query requires one path")
        overwrite_values = parameters.get("overwrite", [])
        if not allow_overwrite:
            return paths[0], False
        if len(overwrite_values) > 1 or (
            overwrite_values and overwrite_values[0] not in {"true", "false"}
        ):
            raise RequestError("overwrite must be true or false")
        return paths[0], overwrite_values == ["true"]

    @staticmethod
    def _artifact_page_query(
        query: str,
        *,
        allow_filters: bool,
        maximum_limit: int,
    ) -> tuple[str | None, str | None, str | None, int]:
        try:
            parameters = parse_qs(query, keep_blank_values=True, strict_parsing=True)
        except ValueError as exc:
            raise RequestError("artifact query is invalid") from exc
        allowed = {"after", "limit"}
        if allow_filters:
            allowed.update({"project_id", "task_id"})
        if set(parameters) - allowed:
            raise RequestError("artifact query contains unsupported parameters")
        if any(len(values) != 1 for values in parameters.values()):
            raise RequestError("artifact query parameters must be singular")

        project_id = parameters.get("project_id", [None])[0]
        task_id = parameters.get("task_id", [None])[0]
        after_artifact_id = parameters.get("after", [None])[0]
        for field_name, value in (
            ("project_id", project_id),
            ("task_id", task_id),
            ("artifact cursor", after_artifact_id),
        ):
            if value is not None and not ID_PATTERN.fullmatch(value):
                raise RequestError(f"{field_name} is invalid")

        limit_raw = parameters.get("limit", [str(maximum_limit)])[0]
        if not limit_raw.isascii() or not limit_raw.isdigit():
            raise RequestError("artifact limit must be an integer")
        limit = int(limit_raw)
        if not 1 <= limit <= maximum_limit:
            raise RequestError("artifact limit is outside the supported bound")
        return project_id, task_id, after_artifact_id, limit

    def _authorized(self) -> bool:
        expected = f"Bearer {self.server.token}"
        actual = self.headers.get("Authorization", "")
        if not hmac.compare_digest(actual, expected):
            self._json_error(
                HTTPStatus.UNAUTHORIZED, "unauthorized", "valid bearer token required"
            )
            return False
        return True

    def _json_error(self, status: HTTPStatus, code: str, message: str) -> None:
        self._json(status, {"schema_version": "1", "error": {"code": code, "message": message}})

    def _json_error_v2(self, status: HTTPStatus, code: str, message: str) -> None:
        self._json(
            status,
            {
                "schema_version": "2",
                "error": {"code": code, "message": message, "retryable": False},
            },
        )

    def _json(self, status: HTTPStatus, payload: object) -> None:
        body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        self.send_response(status.value)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _binary(
        self,
        payload: bytes,
        media_type: str,
        file_name: str,
        *,
        content_sha256: str | None = None,
    ) -> None:
        self.send_response(HTTPStatus.OK.value)
        self.send_header("Content-Type", media_type)
        self.send_header("Content-Length", str(len(payload)))
        self.send_header(
            "Content-Disposition",
            f"attachment; filename*=UTF-8''{quote(file_name, safe='')}",
        )
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        if content_sha256 is not None:
            self.send_header("X-OpenEvo-Content-SHA256", content_sha256)
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, format: str, *args: object) -> None:
        print(f"[{self.log_date_time_string()}] {format % args}", flush=True)


@dataclass(frozen=True)
class ProductDaemonComposition:
    """Fully initialized product services and their loopback HTTP owner."""

    server: DevelopmentAgentServer
    state_path: Path
    evolution_model: str
    evidence_failures: tuple[str, ...]


def create_product_daemon(
    *,
    host: str = "127.0.0.1",
    port: int,
    token: str,
    codex_binary: str,
    timeout_seconds: int,
    state_path: Path,
    model: str | None = None,
    evolution_model: str | None = None,
) -> ProductDaemonComposition:
    """Compose every durable and process-local service owned by the daemon."""

    if host not in {"127.0.0.1", "localhost", "::1"}:
        raise ValueError("daemon must bind to loopback")
    if not 1 <= port <= 65535:
        raise ValueError("port must be between 1 and 65535")
    if not 10 <= timeout_seconds <= 1_800:
        raise ValueError("timeout_seconds must be between 10 and 1800")
    normalized_token = token.strip()
    if len(normalized_token) < 32:
        raise ValueError("daemon token must contain at least 32 characters")
    resolved_codex_binary = shutil.which(codex_binary)
    if not resolved_codex_binary:
        raise ValueError(f"Codex executable was not found: {codex_binary}")

    runner = CodexRunner(resolved_codex_binary, timeout_seconds, model)
    runner.check_ready()
    resolved_state_path = state_path.expanduser().resolve()
    store = DevelopmentStateStore(resolved_state_path)
    resolved_evolution_model = evolution_model or model or "gpt-5.5"
    evolution_runner = DocumentEvolutionRunner(
        state_root=resolved_state_path.parent,
        codex_binary=resolved_codex_binary,
        model=resolved_evolution_model,
        timeout_seconds=timeout_seconds,
    )
    evolution_runner.check_ready()
    evidence_failures = tuple(evolution_runner.seal_completed_session_datasets(store))
    server = DevelopmentAgentServer(
        (host, port),
        normalized_token,
        runner,
        store,
        evolution_runner,
    )
    return ProductDaemonComposition(
        server=server,
        state_path=resolved_state_path,
        evolution_model=resolved_evolution_model,
        evidence_failures=evidence_failures,
    )


def serve_product_daemon(composition: ProductDaemonComposition) -> None:
    """Serve a composed daemon until the process receives an interrupt."""

    for failure in composition.evidence_failures:
        print(f"Could not seal legacy Evolution evidence: {failure}", flush=True)
    host, port = composition.server.server_address
    print(f"OpenEvo product daemon listening on {host}:{port}", flush=True)
    print(f"OpenEvo state database: {composition.state_path}", flush=True)
    print(
        "Evolution methods are discovered from the Core development catalog "
        f"and executed with model {composition.evolution_model}.",
        flush=True,
    )
    print("It is loopback-only; connect through an SSH local-forward tunnel.", flush=True)
    try:
        composition.server.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        print("Stopping OpenEvo product daemon.", flush=True)
    finally:
        composition.server.server_close()


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="python -m openevo.daemon.product_app",
        description="Run the authoritative OpenEvo product daemon on loopback",
    )
    parser.add_argument("--port", type=int, default=8787)
    parser.add_argument("--timeout-seconds", type=int, default=DEFAULT_TIMEOUT_SECONDS)
    parser.add_argument("--codex-binary", default="codex")
    parser.add_argument(
        "--state-path",
        type=Path,
        default=Path.home() / ".openevo" / "dev-agent" / "state.sqlite3",
        help="SQLite database used for development Project and Session history",
    )
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    token = os.environ.get("OPENEVO_DEV_AGENT_TOKEN", "").strip()
    model = os.environ.get("OPENEVO_DEV_CODEX_MODEL", "").strip() or None
    evolution_model = (
        os.environ.get("OPENEVO_DEV_EVOLUTION_MODEL", "").strip() or model or "gpt-5.5"
    )
    try:
        composition = create_product_daemon(
            port=args.port,
            token=token,
            codex_binary=args.codex_binary,
            timeout_seconds=args.timeout_seconds,
            state_path=args.state_path,
            model=model,
            evolution_model=evolution_model,
        )
    except (EvolutionRunError, ValueError) as exc:
        raise SystemExit(str(exc)) from exc
    serve_product_daemon(composition)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
