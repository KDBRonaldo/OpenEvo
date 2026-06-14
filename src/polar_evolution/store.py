from __future__ import annotations

from contextlib import contextmanager
import json
import math
from pathlib import Path
import sqlite3
from typing import Any, Iterator

from polar_evolution.files import ArtifactFileStore
from polar_evolution.ids import new_id
from polar_evolution.models import (
    ArtifactRegisterRequest,
    ArtifactResponse,
    ArtifactState,
    EventIngestRequest,
    EventIngestResponse,
)
from polar_evolution.time import utc_now_iso


SCHEMA = """
PRAGMA journal_mode=WAL;
CREATE TABLE IF NOT EXISTS events (
    event_id TEXT PRIMARY KEY,
    source TEXT NOT NULL,
    event_type TEXT NOT NULL,
    source_event_id TEXT NOT NULL,
    created_at TEXT NOT NULL,
    ingested_at TEXT NOT NULL,
    task_id TEXT,
    session_id TEXT,
    policy_version TEXT,
    rollout_step INTEGER,
    agent_harness TEXT,
    agent_model TEXT,
    base_model TEXT,
    status TEXT,
    reward REAL,
    payload_path TEXT NOT NULL,
    UNIQUE(source, event_type, source_event_id)
);
CREATE TABLE IF NOT EXISTS datasets (
    dataset_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    purpose TEXT NOT NULL,
    state TEXT NOT NULL,
    created_at TEXT NOT NULL,
    query_json TEXT NOT NULL,
    manifest_path TEXT NOT NULL,
    event_count INTEGER NOT NULL,
    trace_count INTEGER NOT NULL,
    artifact_id TEXT
);
CREATE TABLE IF NOT EXISTS dataset_events (
    dataset_id TEXT NOT NULL,
    event_id TEXT NOT NULL,
    PRIMARY KEY(dataset_id, event_id)
);
CREATE TABLE IF NOT EXISTS jobs (
    job_id TEXT PRIMARY KEY,
    job_type TEXT NOT NULL,
    method TEXT NOT NULL,
    state TEXT NOT NULL,
    priority INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    claimed_by TEXT,
    lease_id TEXT,
    lease_expires_at TEXT,
    input_artifact_ids_json TEXT NOT NULL,
    config_json TEXT NOT NULL,
    error TEXT,
    attempt_count INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS artifacts (
    artifact_id TEXT PRIMARY KEY,
    type TEXT NOT NULL,
    name TEXT NOT NULL,
    version INTEGER NOT NULL,
    state TEXT NOT NULL,
    created_at TEXT NOT NULL,
    uri TEXT NOT NULL,
    manifest_path TEXT NOT NULL,
    lineage_json TEXT NOT NULL,
    compatibility_json TEXT NOT NULL,
    scores_json TEXT NOT NULL,
    tags_json TEXT NOT NULL,
    promoted INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS artifact_lineage (
    parent_artifact_id TEXT NOT NULL,
    child_artifact_id TEXT NOT NULL,
    relation TEXT NOT NULL,
    PRIMARY KEY(parent_artifact_id, child_artifact_id, relation)
);
CREATE TABLE IF NOT EXISTS contexts (
    context_id TEXT PRIMARY KEY,
    created_at TEXT NOT NULL,
    request_json TEXT NOT NULL,
    response_json TEXT NOT NULL,
    selected_artifact_ids_json TEXT NOT NULL
);
"""

MAX_ARTIFACT_ID_ATTEMPTS = 10


def _text_metadata(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        return value
    try:
        return json.dumps(value, sort_keys=True)
    except TypeError:
        return str(value)


def _json_dumps(value: Any, *, indent: int | None = None) -> str:
    return json.dumps(value, indent=indent, sort_keys=True, allow_nan=False)


def _validate_finite_floats(value: Any, path: str) -> None:
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError(f"non-finite float at {path}: {value!r}")
        return
    if isinstance(value, dict):
        for key, child in value.items():
            _validate_finite_floats(child, f"{path}.{key}")
        return
    if isinstance(value, list | tuple):
        for index, child in enumerate(value):
            _validate_finite_floats(child, f"{path}[{index}]")


def _write_json_strict_exclusive(
    files: ArtifactFileStore,
    path: Path,
    payload: dict[str, Any],
) -> Path:
    path = path.resolve()
    if files.root != path and files.root not in path.parents:
        raise ValueError(f"path escapes artifact root: {path}")
    serialized = _json_dumps(payload, indent=2)
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with path.open("x", encoding="utf-8") as handle:
            handle.write(serialized)
    except FileExistsError:
        raise
    except Exception:
        if path.exists():
            path.unlink(missing_ok=True)
        raise
    return path


class EvolutionStore:
    def __init__(self, *, db_path: str | Path, artifact_root: str | Path) -> None:
        self.db_path = Path(db_path)
        self.files = ArtifactFileStore(artifact_root)

    def initialize(self) -> None:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.files.initialize()
        with self.connect() as conn:
            conn.executescript(SCHEMA)
            conn.commit()

    def ingest_event(self, request: EventIngestRequest) -> EventIngestResponse:
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            existing = conn.execute(
                """
                SELECT event_id FROM events
                WHERE source = ? AND event_type = ? AND source_event_id = ?
                """,
                (request.source, request.event_type, request.source_event_id),
            ).fetchone()
            if existing is not None:
                conn.rollback()
                return EventIngestResponse(
                    event_id=str(existing["event_id"]),
                    ingested=False,
                    duplicate=True,
                )

            event_id = new_id("evt")
            request_payload = json.loads(json.dumps(request.model_dump(mode="json")))
            created_at = request_payload["created_at"] or utc_now_iso()
            ingested_at = utc_now_iso()
            payload_path = self.files.event_payload_path(event_id)
            self.files.write_json(payload_path, request_payload)
            agent_harness = _text_metadata(request.agent.get("harness"))
            agent_model = _text_metadata(request.agent.get("model_name"))
            conn.execute(
                """
                INSERT INTO events (
                    event_id, source, event_type, source_event_id, created_at,
                    ingested_at, task_id, session_id, policy_version,
                    rollout_step, agent_harness, agent_model, base_model,
                    status, reward, payload_path
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    event_id,
                    request.source,
                    request.event_type,
                    request.source_event_id,
                    created_at,
                    ingested_at,
                    request.task_id,
                    request.session_id,
                    request.policy_version,
                    request.rollout_step,
                    agent_harness,
                    agent_model,
                    request.base_model,
                    request.status,
                    request.reward,
                    str(payload_path),
                ),
            )
            conn.commit()
            return EventIngestResponse(event_id=event_id, ingested=True, duplicate=False)

    def register_artifact(self, request: ArtifactRegisterRequest) -> ArtifactResponse:
        raw_payload = request.model_dump(mode="python")
        for field in ("manifest", "lineage", "compatibility", "scores", "tags"):
            _validate_finite_floats(raw_payload[field], field)

        request_payload = request.model_dump(mode="json")
        artifact_type = str(request_payload["type"])
        lineage_json = _json_dumps(request_payload["lineage"])
        compatibility_json = _json_dumps(request_payload["compatibility"])
        scores_json = _json_dumps(request_payload["scores"])
        tags_json = _json_dumps(request_payload["tags"])

        for _ in range(MAX_ARTIFACT_ID_ATTEMPTS):
            artifact_id = new_id("art")
            created_at = utc_now_iso()
            manifest_path = self.files.artifact_manifest_path(artifact_type, artifact_id)
            manifest_payload = {
                "artifact_id": artifact_id,
                "type": artifact_type,
                "name": request_payload["name"],
                "uri": request_payload["uri"],
                "manifest": request_payload["manifest"],
                "lineage": request_payload["lineage"],
                "compatibility": request_payload["compatibility"],
                "scores": request_payload["scores"],
                "tags": request_payload["tags"],
                "promoted": request_payload["promoted"],
            }
            manifest_created = False
            with self.connect() as conn:
                try:
                    conn.execute("BEGIN IMMEDIATE")
                    existing = conn.execute(
                        "SELECT 1 FROM artifacts WHERE artifact_id = ?",
                        (artifact_id,),
                    ).fetchone()
                    if existing is not None or manifest_path.exists():
                        conn.rollback()
                        continue
                    conn.execute(
                        """
                        INSERT INTO artifacts (
                            artifact_id, type, name, version, state, created_at, uri,
                            manifest_path, lineage_json, compatibility_json, scores_json,
                            tags_json, promoted
                        )
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            artifact_id,
                            artifact_type,
                            request_payload["name"],
                            1,
                            str(ArtifactState.ACTIVE),
                            created_at,
                            request_payload["uri"],
                            str(manifest_path),
                            lineage_json,
                            compatibility_json,
                            scores_json,
                            tags_json,
                            1 if request_payload["promoted"] else 0,
                        ),
                    )
                    try:
                        _write_json_strict_exclusive(
                            self.files,
                            manifest_path,
                            manifest_payload,
                        )
                    except FileExistsError:
                        conn.rollback()
                        continue
                    manifest_created = True
                    conn.commit()
                except Exception:
                    try:
                        conn.rollback()
                    except sqlite3.Error:
                        pass
                    if manifest_created:
                        try:
                            manifest_path.unlink(missing_ok=True)
                        except OSError:
                            pass
                    raise
            return ArtifactResponse(
                artifact_id=artifact_id,
                type=artifact_type,
                name=request_payload["name"],
                version=1,
                state=ArtifactState.ACTIVE,
                uri=request_payload["uri"],
                manifest=request_payload["manifest"],
                compatibility=request_payload["compatibility"],
                scores=request_payload["scores"],
                tags=request_payload["tags"],
                promoted=request_payload["promoted"],
            )
        raise RuntimeError("could not allocate unique artifact id")

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
        finally:
            conn.close()
