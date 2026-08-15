#!/usr/bin/env python3
"""Development-only loopback daemon for real Codex turns and text-memory evolution.

This is intentionally not the release OpenEvo Daemon and must never be exposed directly to a
network. It reuses the real text_memory_reflector implementation without claiming the sealed
release orchestration contract. Bind it to loopback and reach it only through an SSH tunnel.
"""

from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Iterator


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
SOURCE_ROOT = REPOSITORY_ROOT / "src"
if os.fspath(SOURCE_ROOT) not in sys.path:
    sys.path.insert(0, os.fspath(SOURCE_ROOT))


MAX_REQUEST_BYTES = 256 * 1024
MAX_CAPTURE_BYTES = 2 * 1024 * 1024
DEFAULT_TIMEOUT_SECONDS = 300
ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
ALLOWED_REQUEST_FIELDS = {
    "schema_version",
    "project_id",
    "project_name",
    "task_title",
    "instruction",
}
ALLOWED_PROJECT_FIELDS = {"schema_version", "project_id", "display_name", "config"}
ALLOWED_PROJECT_UPDATE_FIELDS = {"schema_version", "display_name", "config"}
PROJECT_PATH_PATTERN = re.compile(r"^/openevo-dev-agent/v1/projects/([^/]+)$")
ACTIVATE_PATH_PATTERN = re.compile(r"^/openevo-dev-agent/v1/projects/([^/]+)/activate$")


class RequestError(ValueError):
    pass


class AgentRunError(RuntimeError):
    pass


class EvolutionRunError(RuntimeError):
    pass


class StateConflictError(RuntimeError):
    pass


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def canonical_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


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
    """Small SQLite authority for the development-only Project/Session loop."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = threading.RLock()
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        try:
            path.parent.chmod(0o700)
        except OSError:
            pass
        with self._connection() as connection:
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
                    error TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS development_sessions_project_created
                    ON development_sessions(project_id, created_at, session_id);
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
                """
            )
            restarted_at = utc_now()
            connection.execute(
                """
                UPDATE development_sessions
                SET state = 'failed', error = ?, updated_at = ?
                WHERE state = 'running'
                """,
                ("Development daemon restarted before this session completed.", restarted_at),
            )
        try:
            path.chmod(0o600)
        except OSError:
            pass

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.path, timeout=10)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def snapshot(self) -> dict[str, Any]:
        with self._lock, self._connection() as connection:
            projects = [
                {
                    "project_id": row["project_id"],
                    "display_name": row["display_name"],
                    "config": json.loads(row["config_json"]),
                    "created_at": row["created_at"],
                    "updated_at": row["updated_at"],
                }
                for row in connection.execute(
                    "SELECT * FROM development_projects ORDER BY created_at, project_id"
                )
            ]
            sessions = [self._session_record(row) for row in connection.execute(
                "SELECT * FROM development_sessions ORDER BY created_at, session_id"
            )]
            artifacts = [self._artifact_record(row) for row in connection.execute(
                "SELECT * FROM development_artifacts ORDER BY created_at, artifact_id"
            )]
            active_row = connection.execute(
                "SELECT value FROM development_metadata WHERE key = 'active_project_id'"
            ).fetchone()
        active_project_id = active_row["value"] if active_row else None
        if active_project_id not in {project["project_id"] for project in projects}:
            active_project_id = projects[-1]["project_id"] if projects else None
        return {
            "schema_version": "1",
            "active_project_id": active_project_id,
            "projects": projects,
            "sessions": sessions,
            "artifacts": artifacts,
        }

    def create_project(self, request: dict[str, Any]) -> dict[str, Any]:
        now = utc_now()
        with self._lock, self._connection() as connection:
            try:
                connection.execute(
                    "INSERT INTO development_projects VALUES (?, ?, ?, ?, ?)",
                    (
                        request["project_id"],
                        request["display_name"],
                        canonical_json(request["config"]),
                        now,
                        now,
                    ),
                )
            except sqlite3.IntegrityError as exc:
                raise StateConflictError("project_id already exists") from exc
            self._set_active(connection, request["project_id"])
        return {**request, "created_at": now, "updated_at": now}

    def update_project(self, project_id: str, request: dict[str, Any]) -> dict[str, Any]:
        now = utc_now()
        with self._lock, self._connection() as connection:
            cursor = connection.execute(
                """
                UPDATE development_projects
                SET display_name = ?, config_json = ?, updated_at = ?
                WHERE project_id = ?
                """,
                (request["display_name"], canonical_json(request["config"]), now, project_id),
            )
            if cursor.rowcount != 1:
                raise KeyError(project_id)
            created = connection.execute(
                "SELECT created_at FROM development_projects WHERE project_id = ?", (project_id,)
            ).fetchone()
        return {"project_id": project_id, **request, "created_at": created["created_at"], "updated_at": now}

    def activate_project(self, project_id: str) -> None:
        with self._lock, self._connection() as connection:
            if connection.execute(
                "SELECT 1 FROM development_projects WHERE project_id = ?", (project_id,)
            ).fetchone() is None:
                raise KeyError(project_id)
            self._set_active(connection, project_id)

    @staticmethod
    def _set_active(connection: sqlite3.Connection, project_id: str) -> None:
        connection.execute(
            """
            INSERT INTO development_metadata(key, value) VALUES ('active_project_id', ?)
            ON CONFLICT(key) DO UPDATE SET value = excluded.value
            """,
            (project_id,),
        )

    def start_session(self, session_id: str, request: dict[str, str]) -> None:
        now = utc_now()
        with self._lock, self._connection() as connection:
            project = connection.execute(
                "SELECT display_name FROM development_projects WHERE project_id = ?",
                (request["project_id"],),
            ).fetchone()
            if project is None:
                raise KeyError(request["project_id"])
            if project["display_name"] != request["project_name"]:
                raise StateConflictError("project_name does not match the persisted project")
            connection.execute(
                """
                INSERT INTO development_sessions(
                    session_id, project_id, task_title, instruction, response, model,
                    state, duration_ms, logs_json, error, created_at, updated_at
                ) VALUES (?, ?, ?, ?, NULL, NULL, 'running', NULL, ?, NULL, ?, ?)
                """,
                (
                    session_id,
                    request["project_id"],
                    request["task_title"],
                    request["instruction"],
                    canonical_json(["Remote development daemon admitted the session."]),
                    now,
                    now,
                ),
            )

    def complete_session(self, session_id: str, result: dict[str, Any]) -> None:
        now = utc_now()
        with self._lock, self._connection() as connection:
            connection.execute(
                """
                UPDATE development_sessions
                SET response = ?, model = ?, state = 'completed', duration_ms = ?,
                    logs_json = ?, error = NULL, updated_at = ?
                WHERE session_id = ?
                """,
                (
                    result["response"],
                    result["model"],
                    result["duration_ms"],
                    canonical_json(result["logs"]),
                    now,
                    session_id,
                ),
            )

    def append_session_log(self, session_id: str, message: str) -> list[str]:
        now = utc_now()
        with self._lock, self._connection() as connection:
            row = connection.execute(
                "SELECT logs_json FROM development_sessions WHERE session_id = ?",
                (session_id,),
            ).fetchone()
            if row is None:
                raise KeyError(session_id)
            logs = json.loads(row["logs_json"])
            logs.append(message)
            connection.execute(
                "UPDATE development_sessions SET logs_json = ?, updated_at = ? WHERE session_id = ?",
                (canonical_json(logs), now, session_id),
            )
        return logs

    def latest_memory(self, project_id: str) -> dict[str, Any] | None:
        with self._lock, self._connection() as connection:
            row = connection.execute(
                """
                SELECT * FROM development_artifacts
                WHERE project_id = ? AND artifact_type = 'text_memory'
                ORDER BY created_at DESC, artifact_id DESC
                LIMIT 1
                """,
                (project_id,),
            ).fetchone()
        return None if row is None else self._artifact_record(row)

    def project_config(self, project_id: str) -> dict[str, Any]:
        with self._lock, self._connection() as connection:
            row = connection.execute(
                "SELECT config_json FROM development_projects WHERE project_id = ?",
                (project_id,),
            ).fetchone()
        if row is None:
            raise KeyError(project_id)
        return json.loads(row["config_json"])

    def record_text_memory(
        self,
        *,
        artifact_id: str,
        project_id: str,
        session_id: str,
        content: str,
        previous_artifact_id: str | None,
    ) -> dict[str, Any]:
        encoded = content.encode("utf-8")
        created_at = utc_now()
        with self._lock, self._connection() as connection:
            connection.execute(
                """
                INSERT INTO development_artifacts(
                    artifact_id, project_id, session_id, artifact_type, method, content,
                    content_sha256, byte_size, previous_artifact_id, created_at
                ) VALUES (?, ?, ?, 'text_memory', 'text_memory_reflector', ?, ?, ?, ?, ?)
                """,
                (
                    artifact_id,
                    project_id,
                    session_id,
                    content,
                    hashlib.sha256(encoded).hexdigest(),
                    len(encoded),
                    previous_artifact_id,
                    created_at,
                ),
            )
            row = connection.execute(
                "SELECT * FROM development_artifacts WHERE artifact_id = ?",
                (artifact_id,),
            ).fetchone()
        if row is None:
            raise RuntimeError("text memory artifact was not persisted")
        return self._artifact_record(row)

    def fail_session(self, session_id: str, error: str) -> None:
        now = utc_now()
        logs = ["Remote development daemon admitted the session.", f"Codex failed: {error}"]
        with self._lock, self._connection() as connection:
            connection.execute(
                """
                UPDATE development_sessions
                SET state = 'failed', logs_json = ?, error = ?, updated_at = ?
                WHERE session_id = ?
                """,
                (canonical_json(logs), error, now, session_id),
            )

    @staticmethod
    def _session_record(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "session_id": row["session_id"],
            "project_id": row["project_id"],
            "task_title": row["task_title"],
            "instruction": row["instruction"],
            "response": row["response"],
            "model": row["model"],
            "state": row["state"],
            "duration_ms": row["duration_ms"],
            "logs": json.loads(row["logs_json"]),
            "error": row["error"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }

    @staticmethod
    def _artifact_record(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "artifact_id": row["artifact_id"],
            "project_id": row["project_id"],
            "session_id": row["session_id"],
            "artifact_type": row["artifact_type"],
            "method": row["method"],
            "content": row["content"],
            "content_sha256": row["content_sha256"],
            "byte_size": row["byte_size"],
            "previous_artifact_id": row["previous_artifact_id"],
            "created_at": row["created_at"],
        }


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


class CodexRunner:
    def __init__(self, codex_binary: str, timeout_seconds: int, model: str | None) -> None:
        self._codex_binary = codex_binary
        self._timeout_seconds = timeout_seconds
        self._model = model

    @property
    def codex_binary(self) -> str:
        return self._codex_binary

    @property
    def model(self) -> str | None:
        return self._model

    def check_ready(self) -> None:
        try:
            result = subprocess.run(
                [self._codex_binary, "login", "status"],
                check=False,
                capture_output=True,
                text=True,
                timeout=15,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise AgentRunError(f"could not check Codex login status: {exc}") from exc
        status_output = "\n".join(part for part in (result.stdout, result.stderr) if part)
        if result.returncode != 0 or "Logged in" not in status_output:
            detail = status_output.strip()[:500]
            raise AgentRunError(f"Codex is not logged in: {detail or 'login status failed'}")

    def run(self, request: dict[str, str]) -> dict[str, Any]:
        evolved_memory = request.get("evolved_memory", "").strip()
        memory_section = (
            "\nEvolved memory from earlier sessions in this project:\n"
            f"{evolved_memory}\n"
            "Apply this memory only when relevant. Do not mention that it was injected.\n"
            if evolved_memory
            else ""
        )
        prompt = (
            "You are answering one user message inside an OpenEvo development session. "
            "Do not edit files or run shell commands. Return only the helpful answer that should "
            "be shown to the user.\n\n"
            f"Project: {request['project_name']}\n"
            f"Session: {request['task_title']}\n\n"
            f"{memory_section}"
            f"User message:\n{request['instruction']}\n"
        )
        started = time.monotonic()
        with tempfile.TemporaryDirectory(prefix="openevo-dev-agent-") as temporary_directory:
            workdir = Path(temporary_directory)
            output_path = workdir / "last-message.txt"
            argv = [
                self._codex_binary,
                "exec",
                "--json",
                "--ignore-user-config",
                "--ignore-rules",
                "--ephemeral",
                "--sandbox",
                "read-only",
                "--disable",
                "shell_tool",
                "--skip-git-repo-check",
                "--cd",
                os.fspath(workdir),
                "--output-last-message",
                os.fspath(output_path),
            ]
            if self._model:
                argv.extend(("--model", self._model))
            argv.append("-")
            try:
                completed = subprocess.run(
                    argv,
                    input=prompt,
                    check=False,
                    capture_output=True,
                    text=True,
                    timeout=self._timeout_seconds,
                )
            except subprocess.TimeoutExpired as exc:
                raise AgentRunError(f"Codex exceeded the {self._timeout_seconds}s timeout") from exc
            except OSError as exc:
                raise AgentRunError(f"Codex could not be started: {exc}") from exc

            stdout_bytes = len(completed.stdout.encode("utf-8"))
            stderr_bytes = len(completed.stderr.encode("utf-8"))
            if stdout_bytes + stderr_bytes > MAX_CAPTURE_BYTES:
                raise AgentRunError("Codex process output exceeded the development safety limit")
            if completed.returncode != 0:
                detail = (completed.stderr or completed.stdout).strip()[-4_000:]
                raise AgentRunError(f"Codex exited with code {completed.returncode}: {detail}")
            try:
                response = output_path.read_text(encoding="utf-8").strip()
            except OSError as exc:
                raise AgentRunError(f"Codex did not publish a final response: {exc}") from exc
            if not response:
                raise AgentRunError("Codex published an empty final response")
            if len(response.encode("utf-8")) > MAX_REQUEST_BYTES:
                raise AgentRunError("Codex final response exceeded the development safety limit")

        duration_ms = round((time.monotonic() - started) * 1000)
        return {
            "schema_version": "1",
            "response": response,
            "model": self._model,
            "duration_ms": duration_ms,
            "logs": [
                "Remote development daemon admitted the session.",
                *extract_event_logs(completed.stdout),
                f"Codex completed the session in {duration_ms} ms.",
            ],
        }


class TextMemoryEvolutionRunner:
    """Development adapter around OpenEvo's real text_memory_reflector method."""

    def __init__(
        self,
        *,
        state_root: Path,
        codex_binary: str,
        model: str,
        timeout_seconds: int,
    ) -> None:
        self._artifact_root = state_root / "evolution-artifacts"
        self._artifact_root.mkdir(mode=0o700, parents=True, exist_ok=True)
        self._codex_binary = codex_binary
        self._model = model
        self._timeout_seconds = timeout_seconds

    def check_ready(self) -> None:
        if sys.version_info < (3, 11):
            raise EvolutionRunError(
                "real text memory evolution requires Python 3.11 or newer; use `uv run python`"
            )
        try:
            from openevo.evolution.methods import run_method  # noqa: F401
            from openevo.evolution.models import WorkerClaimedJob  # noqa: F401
        except (ImportError, ModuleNotFoundError) as exc:
            raise EvolutionRunError(
                "OpenEvo Python dependencies are unavailable; run this daemon with `uv run python`"
            ) from exc

    def evolve(
        self,
        *,
        session_id: str,
        request: dict[str, str],
        result: dict[str, Any],
        store: DevelopmentStateStore,
    ) -> dict[str, Any] | None:
        config = store.project_config(request["project_id"])
        selection = (
            config.get("evolution", {}).get("targets", {}).get("text_memory")
            if isinstance(config, dict)
            else None
        )
        if not isinstance(selection, dict) or selection.get("method") != "text_memory_reflector":
            # Migrate projects created by the earlier no-evolution development bridge.
            selection = {"enabled": True, "method": "text_memory_reflector", "config": {}}
        if selection.get("enabled") is not True:
            return None

        try:
            from openevo.evolution.methods import run_method
            from openevo.evolution.models import WorkerClaimedJob
        except (ImportError, ModuleNotFoundError) as exc:
            raise EvolutionRunError(
                "OpenEvo Python dependencies are unavailable; run this daemon with `uv run python`"
            ) from exc

        dataset_dir = self._artifact_root / "datasets" / session_id
        dataset_dir.mkdir(mode=0o700, parents=True, exist_ok=False)
        records_path = dataset_dir / "records.jsonl"
        record = {
            "event_id": f"{session_id}-completed",
            "task_id": session_id,
            "session_id": session_id,
            "status": "COMPLETED",
            "reward": 1.0,
            "traces": [{
                "prompt_messages": [{"role": "user", "content": request["instruction"]}],
                "response_messages": [{"role": "assistant", "content": result["response"]}],
                "metadata": {
                    "capture_mode": "transcript",
                    "token_level_metrics_available": False,
                },
            }],
        }
        records_path.write_text(canonical_json(record) + "\n", encoding="utf-8")
        manifest_path = dataset_dir / "manifest.json"
        manifest_path.write_text(
            canonical_json({
                "dataset_id": f"dataset-{session_id}",
                "name": f"{request['task_title']} transcript",
                "records_path": records_path.name,
                "records_uri": records_path.resolve().as_uri(),
                "event_count": 1,
                "capture_mode": "transcript",
                "token_level_metrics_available": False,
            }),
            encoding="utf-8",
        )

        previous = store.latest_memory(request["project_id"])
        inputs: list[dict[str, Any]] = [{
            "artifact_id": f"dataset-{session_id}",
            "type": "dataset",
            "uri": manifest_path.resolve().as_uri(),
            "name": f"{request['task_title']} transcript",
        }]
        if previous is not None:
            previous_path = dataset_dir / "previous-memory.md"
            previous_path.write_text(previous["content"], encoding="utf-8")
            inputs.append({
                "artifact_id": previous["artifact_id"],
                "type": "text_memory",
                "uri": previous_path.resolve().as_uri(),
                "name": "previous evolved memory",
            })

        method_config = selection.get("config")
        method_config = dict(method_config) if isinstance(method_config, dict) else {}
        method_config.update({
            "name": f"{request['project_name']} evolved memory",
            "promoted": True,
            "reflector_llm": {
                "provider": "codex_cli",
                "model": self._model,
                "codex_bin": self._codex_binary,
                "timeout_seconds": self._timeout_seconds,
            },
        })
        try:
            [artifact] = run_method(
                WorkerClaimedJob(
                    job_id=f"job-text-memory-{session_id}",
                    lease_id=f"lease-text-memory-{session_id}",
                    job_type="reference",
                    method="text_memory_reflector",
                    input_artifacts=inputs,
                    config=method_config,
                ),
                artifact_root=self._artifact_root,
            )
            if str(artifact.type) not in {"text_memory", "ArtifactType.TEXT_MEMORY"}:
                raise EvolutionRunError("text_memory_reflector returned the wrong artifact type")
            if not artifact.uri.startswith("file://"):
                raise EvolutionRunError("text_memory_reflector returned a non-file artifact")
            memory_path = Path(artifact.uri.removeprefix("file://"))
            memory_content = memory_path.read_text(encoding="utf-8")
        except EvolutionRunError:
            raise
        except Exception as exc:
            raise EvolutionRunError(f"text_memory_reflector failed: {exc}") from exc

        artifact_id = f"dev-text-memory-{session_id.removeprefix('dev-session-')}"
        return store.record_text_memory(
            artifact_id=artifact_id,
            project_id=request["project_id"],
            session_id=session_id,
            content=memory_content,
            previous_artifact_id=None if previous is None else previous["artifact_id"],
        )


class DevelopmentAgentServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(
        self,
        address: tuple[str, int],
        token: str,
        runner: CodexRunner,
        store: DevelopmentStateStore,
        evolution_runner: TextMemoryEvolutionRunner | None = None,
    ) -> None:
        super().__init__(address, DevelopmentAgentHandler)
        self.token = token
        self.runner = runner
        self.store = store
        self.evolution_runner = evolution_runner
        self.turn_lock = threading.Lock()


class DevelopmentAgentHandler(BaseHTTPRequestHandler):
    server: DevelopmentAgentServer
    server_version = "OpenEvoDevelopmentAgent/1"

    def do_GET(self) -> None:  # noqa: N802
        if not self._authorized():
            return
        if self.path == "/openevo-dev-agent/health":
            self._json(HTTPStatus.OK, {"schema_version": "1", "status": "ready"})
            return
        if self.path == "/openevo-dev-agent/v1/state":
            self._json(HTTPStatus.OK, self.server.store.snapshot())
            return
        self._json_error(HTTPStatus.NOT_FOUND, "not_found", "endpoint not found")

    def do_POST(self) -> None:  # noqa: N802
        if not self._authorized():
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
                self._json_error(HTTPStatus.BAD_REQUEST, "invalid_request", "project_id is invalid")
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
        if self.path != "/openevo-dev-agent/v1/sessions":
            self._json_error(HTTPStatus.NOT_FOUND, "not_found", "endpoint not found")
            return
        self._run_session()

    def do_PUT(self) -> None:  # noqa: N802
        if not self._authorized():
            return
        project_match = PROJECT_PATH_PATTERN.fullmatch(self.path)
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

    def _run_session(self) -> None:
        try:
            request = validate_request(self._read_json())
        except RequestError as exc:
            self._json_error(HTTPStatus.BAD_REQUEST, "invalid_request", str(exc))
            return
        if not self.server.turn_lock.acquire(blocking=False):
            self._json_error(HTTPStatus.CONFLICT, "agent_busy", "another development session is running")
            return
        session_id = f"dev-session-{secrets.token_hex(8)}"
        try:
            try:
                self.server.store.start_session(session_id, request)
            except KeyError:
                self._json_error(HTTPStatus.NOT_FOUND, "not_found", "project not found")
                return
            except StateConflictError as exc:
                self._json_error(HTTPStatus.CONFLICT, "state_conflict", str(exc))
                return
            previous_memory = self.server.store.latest_memory(request["project_id"])
            execution_request = {
                **request,
                **(
                    {"evolved_memory": previous_memory["content"]}
                    if previous_memory is not None
                    else {}
                ),
            }
            result = self.server.runner.run(execution_request)
        except AgentRunError as exc:
            self.server.store.fail_session(session_id, str(exc))
            self._json_error(HTTPStatus.BAD_GATEWAY, "codex_failed", str(exc))
        else:
            result = {**result, "session_id": session_id}
            self.server.store.complete_session(session_id, result)
            if self.server.evolution_runner is not None:
                try:
                    evolved = self.server.evolution_runner.evolve(
                        session_id=session_id,
                        request=request,
                        result=result,
                        store=self.server.store,
                    )
                except EvolutionRunError as exc:
                    result["evolution_error"] = str(exc)
                    result["logs"] = self.server.store.append_session_log(
                        session_id,
                        f"Text memory evolution failed: {exc}",
                    )
                else:
                    if evolved is not None:
                        result["evolution_artifact"] = evolved
                        result["logs"] = self.server.store.append_session_log(
                            session_id,
                            "OpenEvo text_memory_reflector published evolved memory for the next session.",
                        )
            self._json(HTTPStatus.OK, result)
        finally:
            self.server.turn_lock.release()

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

    def _authorized(self) -> bool:
        expected = f"Bearer {self.server.token}"
        actual = self.headers.get("Authorization", "")
        if not hmac.compare_digest(actual, expected):
            self._json_error(HTTPStatus.UNAUTHORIZED, "unauthorized", "valid bearer token required")
            return False
        return True

    def _json_error(self, status: HTTPStatus, code: str, message: str) -> None:
        self._json(status, {"schema_version": "1", "error": {"code": code, "message": message}})

    def _json(self, status: HTTPStatus, payload: object) -> None:
        body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        self.send_response(status.value)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: object) -> None:
        print(f"[{self.log_date_time_string()}] {format % args}", flush=True)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run the development-only OpenEvo Codex bridge")
    parser.add_argument("--port", type=int, default=8787)
    parser.add_argument("--timeout-seconds", type=int, default=DEFAULT_TIMEOUT_SECONDS)
    parser.add_argument("--codex-binary", default="codex")
    parser.add_argument(
        "--state-path",
        type=Path,
        default=Path.home() / ".openevo" / "dev-agent" / "state.sqlite3",
        help="SQLite database used for development Project and Session history",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if not 1 <= args.port <= 65535:
        raise SystemExit("--port must be between 1 and 65535")
    if not 10 <= args.timeout_seconds <= 1_800:
        raise SystemExit("--timeout-seconds must be between 10 and 1800")
    token = os.environ.get("OPENEVO_DEV_AGENT_TOKEN", "").strip()
    if len(token) < 32:
        raise SystemExit("OPENEVO_DEV_AGENT_TOKEN must contain at least 32 characters")
    codex_binary = shutil.which(args.codex_binary)
    if not codex_binary:
        raise SystemExit(f"Codex executable was not found: {args.codex_binary}")
    model = os.environ.get("OPENEVO_DEV_CODEX_MODEL", "").strip() or None
    runner = CodexRunner(codex_binary, args.timeout_seconds, model)
    runner.check_ready()
    state_path = args.state_path.expanduser().resolve()
    store = DevelopmentStateStore(state_path)
    evolution_model = (
        os.environ.get("OPENEVO_DEV_EVOLUTION_MODEL", "").strip()
        or model
        or "gpt-5.5"
    )
    evolution_runner = TextMemoryEvolutionRunner(
        state_root=state_path.parent,
        codex_binary=codex_binary,
        model=evolution_model,
        timeout_seconds=args.timeout_seconds,
    )
    try:
        evolution_runner.check_ready()
    except EvolutionRunError as exc:
        raise SystemExit(str(exc)) from exc
    server = DevelopmentAgentServer(
        ("127.0.0.1", args.port),
        token,
        runner,
        store,
        evolution_runner,
    )
    print(f"Development agent daemon listening on 127.0.0.1:{args.port}", flush=True)
    print(f"Development state database: {state_path}", flush=True)
    print(f"Real text_memory_reflector enabled with model {evolution_model}.", flush=True)
    print("It is loopback-only; connect through an SSH local-forward tunnel.", flush=True)
    try:
        server.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        print("Stopping development agent daemon.", flush=True)
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
