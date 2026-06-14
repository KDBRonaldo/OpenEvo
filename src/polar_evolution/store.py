from __future__ import annotations

from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
import json
import math
from pathlib import Path
import sqlite3
from typing import Any, Iterator

from polar_evolution.context import (
    artifact_manifest,
    artifact_matches,
    artifact_type,
    read_file_uri_text,
    sort_candidates,
)
from polar_evolution.files import ArtifactFileStore
from polar_evolution.ids import new_id
from polar_evolution.models import (
    AdapterMergeSpec,
    ArtifactRegisterRequest,
    ArtifactResponse,
    ArtifactState,
    ArtifactType,
    ContextResolveRequest,
    ContextResolveResponse,
    DatasetCreateRequest,
    DatasetCreateResponse,
    EventIngestRequest,
    EventIngestResponse,
    JobCreateRequest,
    JobCreateResponse,
    JobState,
    WorkerClaimRequest,
    WorkerClaimResponse,
    WorkerCompleteRequest,
    WorkerFailRequest,
    WorkerHeartbeatRequest,
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
MAX_DATASET_ID_ATTEMPTS = 10
MAX_CONTEXT_ID_ATTEMPTS = 10
DEFAULT_HEARTBEAT_LEASE_SECONDS = 600
ACTIVE_JOB_STATES = {str(JobState.CLAIMED), str(JobState.RUNNING)}


class JobLeaseError(ValueError):
    pass


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


def _utc_dt_to_iso(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _parse_utc_iso(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


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

    def _promoted_artifact_rows(self) -> list[dict[str, object]]:
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM artifacts
                WHERE promoted = 1 AND state IN (?, ?)
                """,
                (str(ArtifactState.ACTIVE), str(ArtifactState.EXPERIMENTAL)),
            ).fetchall()
        return [dict(row) for row in rows]

    def resolve_context(self, request: ContextResolveRequest) -> ContextResolveResponse:
        raw_payload = request.model_dump(mode="python")
        _validate_finite_floats(raw_payload, "request")

        rows = [
            row
            for row in self._promoted_artifact_rows()
            if artifact_matches(request, row)
        ]
        rows = sort_candidates(rows)

        selected_memory: list[dict[str, object]] = []
        rendered_parts: list[str] = []
        memory_chars = 0
        skills: list[dict[str, object]] = []
        adapters: list[dict[str, object]] = []
        selected_ids: list[str] = []

        for row in rows:
            kind = artifact_type(row)
            artifact_id = str(row["artifact_id"])
            if kind == ArtifactType.TEXT_MEMORY and memory_chars < request.limits.max_memory_chars:
                text = read_file_uri_text(str(row["uri"]))
                separator_chars = 2 if rendered_parts else 0
                remaining = request.limits.max_memory_chars - memory_chars - separator_chars
                if not text or remaining <= 0:
                    continue
                clipped = text[:remaining]
                rendered_parts.append(clipped)
                memory_chars += separator_chars + len(clipped)
                selected_memory.append({"artifact_id": artifact_id, "name": row["name"]})
                selected_ids.append(artifact_id)
            elif kind == ArtifactType.SKILL_BUNDLE and len(skills) < request.limits.max_skill_bundles:
                skills.append(
                    {
                        "artifact_id": artifact_id,
                        "name": row["name"],
                        "uri": row["uri"],
                    }
                )
                selected_ids.append(artifact_id)
            elif kind == ArtifactType.PARAMETRIC_MEMORY and len(adapters) < request.limits.max_adapters:
                manifest = artifact_manifest(row)
                adapter_format = manifest.get("adapter_format")
                if not isinstance(adapter_format, str) or not adapter_format:
                    adapter_format = "lora"
                adapters.append(
                    {
                        "artifact_id": artifact_id,
                        "adapter_id": row["name"],
                        "uri": row["uri"],
                        "weight": 1.0,
                        "format": adapter_format,
                    }
                )
                selected_ids.append(artifact_id)

        for _ in range(MAX_CONTEXT_ID_ATTEMPTS):
            context_id = new_id("ctx")
            response = ContextResolveResponse(
                context_id=context_id,
                memory={
                    "artifact_ids": [str(item["artifact_id"]) for item in selected_memory],
                    "rendered_text": "\n\n".join(rendered_parts),
                },
                skills=skills,
                adapter_merge_spec=AdapterMergeSpec(
                    base_model=request.base_model,
                    merge_mode="runtime_lora" if adapters else "reference_only",
                    adapters=adapters,
                ),
                selection={
                    "artifact_ids": selected_ids,
                    "reasons": ["matched promoted compatible artifacts"],
                },
            )
            request_payload = request.model_dump(mode="json")
            response_payload = response.model_dump(mode="json")
            request_json = _json_dumps(request_payload)
            response_json = _json_dumps(response_payload)
            selected_ids_json = _json_dumps(selected_ids)
            snapshot_path = self.files.context_snapshot_path(context_id)
            snapshot_created = False
            with self.connect() as conn:
                try:
                    conn.execute("BEGIN IMMEDIATE")
                    existing = conn.execute(
                        "SELECT 1 FROM contexts WHERE context_id = ?",
                        (context_id,),
                    ).fetchone()
                    if existing is not None or snapshot_path.exists():
                        conn.rollback()
                        continue
                    conn.execute(
                        """
                        INSERT INTO contexts (
                            context_id, created_at, request_json, response_json,
                            selected_artifact_ids_json
                        )
                        VALUES (?, ?, ?, ?, ?)
                        """,
                        (
                            context_id,
                            utc_now_iso(),
                            request_json,
                            response_json,
                            selected_ids_json,
                        ),
                    )
                    try:
                        _write_json_strict_exclusive(
                            self.files,
                            snapshot_path,
                            {
                                "request": request_payload,
                                "response": response_payload,
                            },
                        )
                    except FileExistsError:
                        conn.rollback()
                        continue
                    snapshot_created = True
                    conn.commit()
                except Exception:
                    try:
                        conn.rollback()
                    except sqlite3.Error:
                        pass
                    if snapshot_created:
                        try:
                            snapshot_path.unlink(missing_ok=True)
                        except OSError:
                            pass
                    raise
            return response
        raise RuntimeError("could not allocate unique context id")

    def create_job(self, request: JobCreateRequest) -> JobCreateResponse:
        raw_payload = request.model_dump(mode="python")
        _validate_finite_floats(raw_payload["config"], "config")
        request_payload = request.model_dump(mode="json")
        input_artifact_ids_json = _json_dumps(request_payload["input_artifact_ids"])
        config_json = _json_dumps(request_payload["config"])

        job_id = new_id("job")
        now = utc_now_iso()
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO jobs (
                    job_id, job_type, method, state, priority, created_at,
                    updated_at, input_artifact_ids_json, config_json,
                    attempt_count
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    job_id,
                    request_payload["job_type"],
                    request_payload["method"],
                    str(JobState.PENDING),
                    request_payload["priority"],
                    now,
                    now,
                    input_artifact_ids_json,
                    config_json,
                    0,
                ),
            )
            conn.commit()
        return JobCreateResponse(job_id=job_id, state=JobState.PENDING)

    def claim_job(self, request: WorkerClaimRequest) -> WorkerClaimResponse:
        now_dt = datetime.now(UTC)
        lease_expires_at = _utc_dt_to_iso(now_dt + timedelta(seconds=request.lease_seconds))
        where = "state = ?"
        params: list[object] = [str(JobState.PENDING)]
        if request.capabilities:
            where += f" AND job_type IN ({','.join('?' for _ in request.capabilities)})"
            params.extend(request.capabilities)

        with self.connect() as conn:
            try:
                conn.execute("BEGIN IMMEDIATE")
                self._requeue_expired_jobs(conn, now_dt)
                row = conn.execute(
                    f"""
                    SELECT * FROM jobs
                    WHERE {where}
                    ORDER BY priority DESC, created_at ASC, job_id ASC
                    LIMIT 1
                    """,
                    params,
                ).fetchone()
                if row is None:
                    conn.rollback()
                    return WorkerClaimResponse(job=None)

                lease_id = new_id("lease")
                cursor = conn.execute(
                    """
                    UPDATE jobs
                    SET state = ?, claimed_by = ?, lease_id = ?, lease_expires_at = ?,
                        updated_at = ?, attempt_count = attempt_count + 1
                    WHERE job_id = ? AND state = ?
                    """,
                    (
                        str(JobState.CLAIMED),
                        request.worker_id,
                        lease_id,
                        lease_expires_at,
                        utc_now_iso(),
                        row["job_id"],
                        str(JobState.PENDING),
                    ),
                )
                if cursor.rowcount != 1:
                    conn.rollback()
                    return WorkerClaimResponse(job=None)
                conn.commit()
            except Exception:
                try:
                    conn.rollback()
                except sqlite3.Error:
                    pass
                raise

        return WorkerClaimResponse(
            job={
                "job_id": row["job_id"],
                "lease_id": lease_id,
                "job_type": row["job_type"],
                "method": row["method"],
                "input_artifacts": self._worker_claim_input_artifacts(
                    json.loads(str(row["input_artifact_ids_json"]))
                ),
                "config": json.loads(str(row["config_json"])),
                "priority": row["priority"],
                "state": JobState.CLAIMED,
            }
        )

    def _requeue_expired_jobs(self, conn: sqlite3.Connection, now: datetime) -> None:
        rows = conn.execute(
            """
            SELECT job_id, lease_expires_at
            FROM jobs
            WHERE state IN (?, ?) AND lease_expires_at IS NOT NULL
            """,
            (str(JobState.CLAIMED), str(JobState.RUNNING)),
        ).fetchall()
        now = now.astimezone(UTC)
        for row in rows:
            try:
                lease_expires_at = _parse_utc_iso(str(row["lease_expires_at"]))
            except ValueError:
                conn.execute(
                    """
                    UPDATE jobs
                    SET state = ?, claimed_by = NULL, lease_id = NULL,
                        lease_expires_at = NULL, updated_at = ?, error = ?
                    WHERE job_id = ?
                    """,
                    (
                        str(JobState.FAILED),
                        utc_now_iso(),
                        f"invalid lease_expires_at: {row['lease_expires_at']}",
                        row["job_id"],
                    ),
                )
                continue
            if lease_expires_at <= now:
                conn.execute(
                    """
                    UPDATE jobs
                    SET state = ?, claimed_by = NULL, lease_id = NULL,
                        lease_expires_at = NULL, updated_at = ?,
                        error = COALESCE(error, ?)
                    WHERE job_id = ?
                    """,
                    (
                        str(JobState.PENDING),
                        utc_now_iso(),
                        f"lease expired at {_utc_dt_to_iso(lease_expires_at)}",
                        row["job_id"],
                    ),
                )

    def _worker_claim_input_artifacts(self, artifact_ids: list[str]) -> list[dict[str, Any]]:
        artifacts: list[dict[str, Any]] = []
        with self.connect() as conn:
            for artifact_id in artifact_ids:
                artifact = conn.execute(
                    "SELECT artifact_id, type, uri, name FROM artifacts WHERE artifact_id = ?",
                    (artifact_id,),
                ).fetchone()
                if artifact is not None:
                    artifacts.append(
                        {
                            "artifact_id": artifact["artifact_id"],
                            "type": artifact["type"],
                            "uri": artifact["uri"],
                            "name": artifact["name"],
                        }
                    )
        return artifacts

    def _assert_job_lease(
        self,
        conn: sqlite3.Connection,
        job_id: str,
        lease_id: str,
    ) -> sqlite3.Row:
        row = conn.execute("SELECT * FROM jobs WHERE job_id = ?", (job_id,)).fetchone()
        if row is None:
            raise JobLeaseError(f"unknown job: {job_id}")
        if row["state"] not in ACTIVE_JOB_STATES or row["lease_id"] != lease_id:
            raise JobLeaseError(f"invalid lease for job: {job_id}")

        lease_expires_at = row["lease_expires_at"]
        if lease_expires_at is not None:
            try:
                expires_at = _parse_utc_iso(str(lease_expires_at))
            except ValueError as exc:
                raise JobLeaseError(f"invalid lease_expires_at for job: {job_id}") from exc
            if expires_at <= datetime.now(UTC):
                raise JobLeaseError(f"lease expired for job: {job_id}")
        return row

    def heartbeat_job(
        self,
        job_id: str,
        request: WorkerHeartbeatRequest,
    ) -> dict[str, object]:
        if request.progress is not None:
            _validate_finite_floats(request.progress, "progress")

        lease_expires_at = _utc_dt_to_iso(
            datetime.now(UTC) + timedelta(seconds=DEFAULT_HEARTBEAT_LEASE_SECONDS)
        )
        with self.connect() as conn:
            try:
                conn.execute("BEGIN IMMEDIATE")
                self._assert_job_lease(conn, job_id, request.lease_id)
                conn.execute(
                    """
                    UPDATE jobs
                    SET state = ?, updated_at = ?, lease_expires_at = ?
                    WHERE job_id = ?
                    """,
                    (str(JobState.RUNNING), utc_now_iso(), lease_expires_at, job_id),
                )
                conn.commit()
            except Exception:
                try:
                    conn.rollback()
                except sqlite3.Error:
                    pass
                raise
        return {
            "job_id": job_id,
            "state": str(JobState.RUNNING),
            "progress": request.progress,
            "lease_expires_at": lease_expires_at,
        }

    def complete_job(
        self,
        job_id: str,
        request: WorkerCompleteRequest,
    ) -> dict[str, object]:
        raw_payload = request.model_dump(mode="python")
        _validate_finite_floats(raw_payload["report"], "report")

        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                self._assert_job_lease(conn, job_id, request.lease_id)
                conn.rollback()
            except Exception:
                try:
                    conn.rollback()
                except sqlite3.Error:
                    pass
                raise

        registered_artifact_ids: list[str] = []
        try:
            for artifact_request in request.artifacts:
                artifact = self.register_artifact(artifact_request)
                registered_artifact_ids.append(artifact.artifact_id)

            with self.connect() as conn:
                try:
                    conn.execute("BEGIN IMMEDIATE")
                    self._assert_job_lease(conn, job_id, request.lease_id)
                    conn.execute(
                        """
                        UPDATE jobs
                        SET state = ?, updated_at = ?, claimed_by = NULL, lease_id = NULL,
                            lease_expires_at = NULL, error = NULL
                        WHERE job_id = ?
                        """,
                        (str(JobState.SUCCEEDED), utc_now_iso(), job_id),
                    )
                    conn.commit()
                except Exception:
                    try:
                        conn.rollback()
                    except sqlite3.Error:
                        pass
                    raise
        except JobLeaseError as exc:
            if registered_artifact_ids:
                try:
                    self._cleanup_registered_artifacts(registered_artifact_ids)
                except Exception as cleanup_exc:
                    exc.add_note(f"artifact cleanup failed: {cleanup_exc}")
            raise
        except Exception as exc:
            cleanup_error: Exception | None = None
            if registered_artifact_ids:
                try:
                    self._cleanup_registered_artifacts(registered_artifact_ids)
                except Exception as cleanup_exc:
                    cleanup_error = cleanup_exc
            try:
                self._record_job_completion_failure(job_id, request.lease_id, error=exc)
            except Exception as record_exc:
                exc.add_note(f"job failure recording failed: {record_exc}")
            if cleanup_error is not None:
                exc.add_note(f"artifact cleanup failed: {cleanup_error}")
            raise

        return {
            "job_id": job_id,
            "state": str(JobState.SUCCEEDED),
            "artifact_ids": registered_artifact_ids,
        }

    def fail_job(self, job_id: str, request: WorkerFailRequest) -> dict[str, object]:
        next_state = JobState.PENDING if request.retryable else JobState.FAILED
        error = request.error
        with self.connect() as conn:
            try:
                conn.execute("BEGIN IMMEDIATE")
                self._assert_job_lease(conn, job_id, request.lease_id)
                conn.execute(
                    """
                    UPDATE jobs
                    SET state = ?, updated_at = ?, claimed_by = NULL, lease_id = NULL,
                        lease_expires_at = NULL, error = ?
                    WHERE job_id = ?
                    """,
                    (str(next_state), utc_now_iso(), error, job_id),
                )
                conn.commit()
            except Exception:
                try:
                    conn.rollback()
                except sqlite3.Error:
                    pass
                raise
        return {"job_id": job_id, "state": str(next_state), "error": error}

    def _cleanup_registered_artifacts(self, artifact_ids: list[str]) -> None:
        for artifact_id in artifact_ids:
            artifact_manifest_path: Path | None = None
            with self.connect() as conn:
                conn.execute("BEGIN IMMEDIATE")
                artifact_row = conn.execute(
                    "SELECT type, manifest_path FROM artifacts WHERE artifact_id = ?",
                    (artifact_id,),
                ).fetchone()
                if artifact_row is not None:
                    artifact_manifest_path = Path(str(artifact_row["manifest_path"]))
                    conn.execute(
                        """
                        DELETE FROM artifact_lineage
                        WHERE parent_artifact_id = ? OR child_artifact_id = ?
                        """,
                        (artifact_id, artifact_id),
                    )
                    conn.execute("DELETE FROM artifacts WHERE artifact_id = ?", (artifact_id,))
                conn.commit()
            if artifact_manifest_path is not None:
                artifact_manifest_path.unlink(missing_ok=True)

    def _record_job_completion_failure(
        self,
        job_id: str,
        lease_id: str,
        *,
        error: BaseException,
    ) -> None:
        message = str(error) or error.__class__.__name__
        with self.connect() as conn:
            try:
                conn.execute("BEGIN IMMEDIATE")
                conn.execute(
                    """
                    UPDATE jobs
                    SET state = ?, updated_at = ?, claimed_by = NULL, lease_id = NULL,
                        lease_expires_at = NULL, error = ?
                    WHERE job_id = ? AND lease_id = ? AND state IN (?, ?)
                    """,
                    (
                        str(JobState.FAILED),
                        utc_now_iso(),
                        message,
                        job_id,
                        lease_id,
                        str(JobState.CLAIMED),
                        str(JobState.RUNNING),
                    ),
                )
                conn.commit()
            except Exception:
                try:
                    conn.rollback()
                except sqlite3.Error:
                    pass
                raise

    def _event_rows_for_dataset(
        self,
        conn: sqlite3.Connection,
        request: DatasetCreateRequest,
    ) -> list[sqlite3.Row]:
        clauses: list[str] = []
        params: list[object] = []
        if request.query.event_types:
            clauses.append(
                "event_type IN (%s)" % ",".join("?" for _ in request.query.event_types)
            )
            params.extend(request.query.event_types)
        if request.query.status:
            clauses.append("status IN (%s)" % ",".join("?" for _ in request.query.status))
            params.extend(request.query.status)
        if request.query.reward_min is not None:
            clauses.append("reward >= ?")
            params.append(request.query.reward_min)
        if request.query.policy_version:
            clauses.append("policy_version = ?")
            params.append(request.query.policy_version)
        where = " AND ".join(clauses) if clauses else "1 = 1"
        return conn.execute(
            f"SELECT * FROM events WHERE {where} ORDER BY ingested_at, event_id LIMIT ?",
            (*params, request.limits.max_events),
        ).fetchall()

    def _trace_count_for_event_row(self, row: dict[str, Any]) -> int:
        event_id = str(row["event_id"])
        payload_path = Path(str(row["payload_path"]))
        try:
            payload_text = payload_path.read_text(encoding="utf-8")
        except FileNotFoundError as exc:
            raise ValueError(f"event {event_id} payload file is missing: {payload_path}") from exc
        try:
            payload = json.loads(payload_text)
        except json.JSONDecodeError as exc:
            raise ValueError(
                f"event {event_id} payload file is not valid JSON: {payload_path}"
            ) from exc

        if not isinstance(payload, dict):
            return 0
        event_payload = payload.get("payload")
        if not isinstance(event_payload, dict):
            return 0
        session_result = event_payload.get("session_result")
        if not isinstance(session_result, dict):
            return 0
        trajectory = session_result.get("trajectory")
        if not isinstance(trajectory, dict):
            return 0
        traces = trajectory.get("traces")
        if not isinstance(traces, list):
            return 0
        return len(traces)

    def create_dataset(self, request: DatasetCreateRequest) -> DatasetCreateResponse:
        raw_payload = request.model_dump(mode="python")
        _validate_finite_floats(raw_payload["query"], "query")
        _validate_finite_floats(raw_payload["limits"], "limits")
        if request.query.task_tags:
            raise ValueError("query.task_tags is not supported until events store task tags")

        request_payload = request.model_dump(mode="json")
        query_json = _json_dumps(request_payload["query"])

        with self.connect() as conn:
            rows = [dict(row) for row in self._event_rows_for_dataset(conn, request)]

        trace_count = 0
        event_ids: list[str] = []
        for row in rows:
            if trace_count >= request.limits.max_traces:
                break
            trace_count += self._trace_count_for_event_row(row)
            event_ids.append(str(row["event_id"]))

        for _ in range(MAX_DATASET_ID_ATTEMPTS):
            dataset_id = new_id("ds")
            created_at = utc_now_iso()
            manifest_path = self.files.dataset_manifest_path(dataset_id)
            manifest = {
                "dataset_id": dataset_id,
                "name": request_payload["name"],
                "purpose": request_payload["purpose"],
                "query": request_payload["query"],
                "limits": request_payload["limits"],
                "event_ids": event_ids,
                "event_count": len(event_ids),
                "trace_count": trace_count,
            }
            _validate_finite_floats(manifest, "manifest")

            manifest_created = False
            with self.connect() as conn:
                try:
                    conn.execute("BEGIN IMMEDIATE")
                    existing = conn.execute(
                        "SELECT 1 FROM datasets WHERE dataset_id = ?",
                        (dataset_id,),
                    ).fetchone()
                    if existing is not None or manifest_path.exists():
                        conn.rollback()
                        continue
                    conn.execute(
                        """
                        INSERT INTO datasets (
                            dataset_id, name, purpose, state, created_at, query_json,
                            manifest_path, event_count, trace_count, artifact_id
                        )
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            dataset_id,
                            request_payload["name"],
                            request_payload["purpose"],
                            "active",
                            created_at,
                            query_json,
                            str(manifest_path),
                            len(event_ids),
                            trace_count,
                            None,
                        ),
                    )
                    conn.executemany(
                        "INSERT INTO dataset_events (dataset_id, event_id) VALUES (?, ?)",
                        [(dataset_id, event_id) for event_id in event_ids],
                    )
                    try:
                        _write_json_strict_exclusive(self.files, manifest_path, manifest)
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

            try:
                artifact = self.register_artifact(
                    ArtifactRegisterRequest(
                        type=ArtifactType.DATASET,
                        name=request.name,
                        uri=manifest_path.as_uri(),
                        manifest=manifest,
                        lineage={"event_ids": event_ids},
                        compatibility={"purpose": request.purpose},
                        tags=[request.purpose],
                        promoted=True,
                    )
                )
            except Exception:
                self._cleanup_dataset_create_failure(dataset_id, manifest_path)
                raise

            try:
                self._backfill_dataset_artifact_id(dataset_id, artifact.artifact_id)
            except Exception:
                self._cleanup_dataset_create_failure(
                    dataset_id,
                    manifest_path,
                    artifact_id=artifact.artifact_id,
                )
                raise

            return DatasetCreateResponse(
                dataset_id=dataset_id,
                artifact_id=artifact.artifact_id,
                event_count=len(event_ids),
                trace_count=trace_count,
            )
        raise RuntimeError("could not allocate unique dataset id")

    def _backfill_dataset_artifact_id(self, dataset_id: str, artifact_id: str) -> None:
        with self.connect() as conn:
            try:
                conn.execute("BEGIN IMMEDIATE")
                conn.execute(
                    "UPDATE datasets SET artifact_id = ? WHERE dataset_id = ?",
                    (artifact_id, dataset_id),
                )
                conn.commit()
            except Exception:
                try:
                    conn.rollback()
                except sqlite3.Error:
                    pass
                raise

    def _cleanup_dataset_create_failure(
        self,
        dataset_id: str,
        dataset_manifest_path: Path,
        *,
        artifact_id: str | None = None,
    ) -> None:
        artifact_manifest_path: Path | None = None
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            if artifact_id is not None:
                artifact_manifest_path = self.files.artifact_manifest_path(
                    str(ArtifactType.DATASET),
                    artifact_id,
                )
                artifact_row = conn.execute(
                    "SELECT manifest_path FROM artifacts WHERE artifact_id = ?",
                    (artifact_id,),
                ).fetchone()
                if artifact_row is not None:
                    artifact_manifest_path = Path(str(artifact_row["manifest_path"]))
                conn.execute(
                    """
                    DELETE FROM artifact_lineage
                    WHERE parent_artifact_id = ? OR child_artifact_id = ?
                    """,
                    (artifact_id, artifact_id),
                )
                conn.execute("DELETE FROM artifacts WHERE artifact_id = ?", (artifact_id,))
            conn.execute("DELETE FROM dataset_events WHERE dataset_id = ?", (dataset_id,))
            conn.execute("DELETE FROM datasets WHERE dataset_id = ?", (dataset_id,))
            conn.commit()

        dataset_manifest_path.unlink(missing_ok=True)
        if artifact_manifest_path is not None:
            artifact_manifest_path.unlink(missing_ok=True)

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
        finally:
            conn.close()
