from __future__ import annotations

from contextlib import ExitStack, contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
import hashlib
import json
import math
import os
from pathlib import Path
import re
import sqlite3
import stat
from typing import Any, Callable, Iterator, NotRequired, TypedDict
from urllib.parse import urlparse, urlunparse

from openevo.evolution.agent_system import normalize_agent_system_target_path
from openevo.evolution.artifact_payloads import ArtifactPayloadService
from openevo.evolution.context import (
    artifact_manifest,
    artifact_matches,
    artifact_type,
    requested_context_artifact_order,
    requested_context_artifact_ids,
    request_uses_subscription_auth,
    sort_candidates,
)
from openevo.evolution.context_projection import (
    MAX_ARTIFACT_ROUTING_JSON_BYTES,
    MAX_CONTEXT_ARTIFACT_NAME_BYTES,
    MAX_CONTEXT_ARTIFACT_URI_BYTES,
    MAX_CONTEXT_PROJECTION_CANDIDATES,
    ContextProjectionResolveRequest,
    ContextProjectionResolveResponse,
    ContextProjectionResolver,
)
from openevo.evolution.context_materialization import (
    MAX_CONTEXT_MANIFEST_BYTES,
    ContextMaterializer,
    MaterializedBlobLease,
    MaterializedContext,
    PersistedContextDiscardReceipt,
    _PRESERVED_ENTRY_PREFIX,
    _PrivateFileIdentity,
    _private_file_identity,
    _remove_materialized_entry_if_identity,
)
from openevo.evolution.context_snapshot_recovery import (
    MAX_CONTEXT_SNAPSHOT_BYTES,
    MAX_CONTEXT_SNAPSHOT_ENTRIES,
    MAX_CONTEXT_SNAPSHOT_INVENTORY_BYTES,
    migrate_legacy_context_snapshot_modes,
    reconcile_context_snapshots,
    write_context_snapshot,
)
from openevo.evolution.files import ARTIFACT_TYPE_DIRECTORIES, ArtifactFileStore
from openevo.evolution.ids import new_id
from openevo.evolution.materialization_root_lock import get_materialization_root_lock
from openevo.evolution.store_schema_identity import classify_store_schema
from openevo.evolution.models import (
    ArtifactPromotionUpdateRequest,
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
    FeedbackApplicationCreateRequest,
    FeedbackApplicationResponse,
    FeedbackApplicationTargetType,
    HumanFeedbackCreateRequest,
    HumanFeedbackResponse,
    HumanFeedbackStatus,
    HumanQueryDecisionCreateRequest,
    HumanQueryDecisionResponse,
    JobCreateRequest,
    JobCreateResponse,
    JobState,
    ReviewAdjudicationRequest,
    ReviewClaimRequest,
    ReviewPacketResponse,
    ReviewRequestCreateRequest,
    ReviewRequestResponse,
    ReviewStatus,
    WorkerClaimRequest,
    WorkerClaimInputArtifact,
    WorkerClaimResponse,
    WorkerCompleteRequest,
    WorkerFailRequest,
    WorkerHeartbeatRequest,
)
from openevo.evolution.planned_jobs import (
    PlanBoundJobCreateRequest,
    PlanBoundJobRetryRequest,
    materialize_plan_bound_job,
    validate_plan_against_snapshot,
)
from openevo.evolution.revisions import (
    AdmissionQueueReason,
    AdmissionStatus,
    ExecutionSnapshotRecord,
    ExecutionSnapshotV1,
    MAX_EXECUTION_SNAPSHOT_BYTES,
    MAX_REVISION_MANIFEST_BYTES,
    RevisionCapacityError,
    RevisionConflictError,
    RevisionIntegrityError,
    RevisionManifestV1,
    RevisionNotFoundError,
    RevisionRecord,
    TaskAdmissionConflictError,
    TaskAdmissionIntent,
    TaskAdmissionRecord,
    TaskAdmissionRequest,
    TaskExecutionEnvelopeV1,
    VerifiedExecutionSnapshot,
    admission_id_for_request,
    bind_task_admission,
    execution_snapshot_id_for_snapshot,
    require_verified_execution_snapshot,
    revision_id_for_manifest,
)
from openevo.evolution.time import utc_now_iso
from openevo.evolution.framework.contracts import (
    MAX_CONTRACT_JSON_BYTES,
    MAX_HANDLER_ARTIFACTS,
    canonical_digest,
    canonical_json,
)
from openevo.evolution.framework.builtins import (
    VerifiedExecutableRegistry,
    require_verified_executable_registry,
)
from openevo.evolution.framework.execution import (
    MethodExecutionEnvelope,
    worker_input_artifact_digest,
)
from openevo.evolution.framework.plan import EvolutionPlan, ResolvedEvolutionSelection
from openevo.evolution.framework.registry import RegistrySnapshot


SCHEMA = """
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
CREATE TABLE IF NOT EXISTS dataset_create_requests (
    idempotency_key TEXT PRIMARY KEY,
    request_sha256 TEXT NOT NULL CHECK (length(request_sha256) = 64),
    request_json TEXT NOT NULL,
    dataset_id TEXT NOT NULL UNIQUE,
    response_json TEXT,
    recovery_file_count INTEGER CHECK(
        recovery_file_count IS NULL OR (
            typeof(recovery_file_count) = 'integer'
            AND recovery_file_count >= 0
        )
    ),
    recovery_byte_size INTEGER CHECK(
        recovery_byte_size IS NULL OR (
            typeof(recovery_byte_size) = 'integer'
            AND recovery_byte_size >= 0
        )
    ),
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS dataset_events (
    dataset_id TEXT NOT NULL,
    event_id TEXT NOT NULL,
    PRIMARY KEY(dataset_id, event_id)
);
CREATE TABLE IF NOT EXISTS evolution_plans (
    plan_id TEXT PRIMARY KEY,
    schema_version TEXT NOT NULL,
    registry_snapshot_digest TEXT NOT NULL,
    plan_digest TEXT NOT NULL,
    plan_json TEXT NOT NULL,
    created_at TEXT NOT NULL
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
    lease_duration_seconds INTEGER,
    input_artifact_ids_json TEXT NOT NULL,
    config_json TEXT NOT NULL,
    plan_id TEXT,
    target_id TEXT,
    method_identity_digest TEXT,
    execution_envelope_json TEXT,
    execution_envelope_digest TEXT,
    declared_output_artifact_types_json TEXT,
    error TEXT,
    attempt_count INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS plan_bound_job_retry_requests (
    job_id TEXT NOT NULL,
    retry_request_id TEXT NOT NULL,
    plan_id TEXT NOT NULL,
    target_id TEXT NOT NULL,
    claim_attempt_before INTEGER NOT NULL CHECK (claim_attempt_before >= 0),
    created_at TEXT NOT NULL,
    PRIMARY KEY(job_id, retry_request_id),
    FOREIGN KEY(job_id) REFERENCES jobs(job_id) ON DELETE RESTRICT
);
CREATE TABLE IF NOT EXISTS plan_bound_job_transition_bindings (
    job_id TEXT PRIMARY KEY,
    successor_transition_id TEXT NOT NULL,
    predecessor_successor_transition_id TEXT,
    created_at TEXT NOT NULL,
    FOREIGN KEY(job_id) REFERENCES jobs(job_id) ON DELETE RESTRICT
);
CREATE INDEX IF NOT EXISTS idx_plan_bound_job_transition_successor
ON plan_bound_job_transition_bindings(successor_transition_id, job_id);
CREATE TABLE IF NOT EXISTS successor_transition_discards (
    successor_transition_id TEXT PRIMARY KEY,
    receipt_sha256 TEXT NOT NULL CHECK(length(receipt_sha256) = 64),
    receipt_json TEXT NOT NULL,
    discarded_at TEXT NOT NULL
) STRICT;
CREATE TABLE IF NOT EXISTS artifacts (
    artifact_id TEXT PRIMARY KEY,
    type TEXT NOT NULL,
    name TEXT NOT NULL,
    version INTEGER NOT NULL,
    state TEXT NOT NULL,
    created_at TEXT NOT NULL,
    uri TEXT NOT NULL,
    manifest_path TEXT NOT NULL,
    manifest_json TEXT NOT NULL,
    lineage_json TEXT NOT NULL,
    compatibility_json TEXT NOT NULL,
    scores_json TEXT NOT NULL,
    tags_json TEXT NOT NULL,
    promoted INTEGER NOT NULL,
    staging_job_id TEXT
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
CREATE TABLE IF NOT EXISTS context_materializations (
    context_id TEXT PRIMARY KEY,
    created_at TEXT NOT NULL,
    registry_digest TEXT NOT NULL,
    request_digest TEXT NOT NULL,
    manifest_json TEXT NOT NULL,
    FOREIGN KEY(context_id) REFERENCES contexts(context_id) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS execution_snapshots (
    execution_snapshot_id TEXT PRIMARY KEY,
    created_at TEXT NOT NULL,
    snapshot_digest TEXT NOT NULL UNIQUE,
    producer_id TEXT NOT NULL,
    snapshot_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS revisions (
    revision_id TEXT PRIMARY KEY,
    stream_id TEXT NOT NULL,
    generation INTEGER NOT NULL CHECK(typeof(generation) = 'integer' AND generation >= 0),
    predecessor_revision_id TEXT,
    created_at TEXT NOT NULL,
    manifest_digest TEXT NOT NULL UNIQUE,
    manifest_json TEXT NOT NULL,
    context_id TEXT NOT NULL,
    context_manifest_digest TEXT NOT NULL,
    registry_digest TEXT NOT NULL,
    execution_snapshot_id TEXT NOT NULL,
    execution_snapshot_digest TEXT NOT NULL,
    adapter_set_digest TEXT NOT NULL,
    UNIQUE(stream_id, generation),
    CHECK(
        (generation = 0 AND predecessor_revision_id IS NULL)
        OR (generation > 0 AND predecessor_revision_id IS NOT NULL)
    ),
    FOREIGN KEY(predecessor_revision_id) REFERENCES revisions(revision_id),
    FOREIGN KEY(context_id) REFERENCES context_materializations(context_id),
    FOREIGN KEY(execution_snapshot_id)
        REFERENCES execution_snapshots(execution_snapshot_id)
);
CREATE TABLE IF NOT EXISTS revision_streams (
    stream_id TEXT PRIMARY KEY,
    active_revision_id TEXT NOT NULL UNIQUE,
    active_generation INTEGER NOT NULL CHECK(
        typeof(active_generation) = 'integer' AND active_generation >= 0
    ),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    FOREIGN KEY(active_revision_id) REFERENCES revisions(revision_id)
);
CREATE TABLE IF NOT EXISTS task_admissions (
    admission_id TEXT PRIMARY KEY,
    stream_id TEXT NOT NULL,
    task_id TEXT NOT NULL,
    idempotency_key TEXT NOT NULL,
    required_generation INTEGER NOT NULL CHECK(
        typeof(required_generation) = 'integer' AND required_generation >= 0
    ),
    request_digest TEXT NOT NULL UNIQUE,
    request_json TEXT NOT NULL,
    status TEXT NOT NULL CHECK(
        status IN ('queued', 'admitted', 'completed', 'failed', 'cancelled')
    ),
    reason TEXT,
    pinned_revision_id TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    finished_at TEXT,
    UNIQUE(stream_id, task_id),
    UNIQUE(stream_id, idempotency_key),
    CHECK(
        (status = 'queued'
            AND reason = 'required_revision_uncommitted'
            AND pinned_revision_id IS NULL
            AND finished_at IS NULL)
        OR (status = 'admitted'
            AND reason IS NULL
            AND pinned_revision_id IS NOT NULL
            AND finished_at IS NULL)
        OR (status IN ('completed', 'failed')
            AND reason IS NULL
            AND pinned_revision_id IS NOT NULL
            AND finished_at IS NOT NULL)
        OR (status = 'cancelled'
            AND reason IS NULL
            AND finished_at IS NOT NULL)
    ),
    FOREIGN KEY(stream_id) REFERENCES revision_streams(stream_id),
    FOREIGN KEY(pinned_revision_id) REFERENCES revisions(revision_id)
);
CREATE INDEX IF NOT EXISTS idx_task_admissions_active_revision
ON task_admissions(pinned_revision_id) WHERE status = 'admitted';
CREATE TABLE IF NOT EXISTS review_packets (
    packet_id TEXT PRIMARY KEY,
    packet_hash TEXT NOT NULL UNIQUE,
    packet_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS review_requests (
    review_id TEXT PRIMARY KEY,
    review_type TEXT NOT NULL,
    status TEXT NOT NULL,
    artifact_ids_json TEXT NOT NULL,
    candidate_ids_json TEXT NOT NULL,
    job_id TEXT,
    task_id TEXT,
    round_index INTEGER,
    method TEXT,
    artifact_type TEXT,
    packet_id TEXT NOT NULL,
    packet_hash TEXT NOT NULL,
    artifact_hashes_json TEXT NOT NULL,
    query_decision_id TEXT,
    assigned_to TEXT,
    reviewer_role TEXT,
    adjudication_rationale TEXT,
    priority INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS human_feedback (
    feedback_id TEXT PRIMARY KEY,
    review_id TEXT NOT NULL,
    reviewer_id TEXT NOT NULL,
    reviewer_role TEXT,
    status TEXT NOT NULL,
    decision TEXT NOT NULL,
    score REAL,
    confidence REAL,
    rationale TEXT NOT NULL,
    raw_payload_json TEXT NOT NULL,
    normalized_payload_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS feedback_applications (
    application_id TEXT PRIMARY KEY,
    feedback_id TEXT NOT NULL,
    target_type TEXT NOT NULL,
    target_id TEXT NOT NULL,
    consumed_by_method TEXT NOT NULL,
    consumed_in_job_id TEXT,
    effect_summary TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS human_query_decisions (
    query_decision_id TEXT PRIMARY KEY,
    artifact_ids_json TEXT NOT NULL,
    candidate_ids_json TEXT NOT NULL,
    task_id TEXT,
    round_index INTEGER,
    method TEXT,
    decision TEXT NOT NULL,
    reason_codes_json TEXT NOT NULL,
    estimated_value_of_information REAL,
    estimated_human_cost REAL,
    budget_context_json TEXT NOT NULL,
    actual_latency_seconds REAL,
    feedback_changed_promotion INTEGER,
    feedback_changed_next_candidate INTEGER,
    downstream_delta REAL,
    review_id TEXT,
    created_at TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_review_requests_query_decision_id_unique
ON review_requests(query_decision_id)
WHERE query_decision_id IS NOT NULL;
"""

MAX_ARTIFACT_ID_ATTEMPTS = 10
MAX_INTERNAL_JOB_OUTPUTS = MAX_HANDLER_ARTIFACTS
MAX_INTERNAL_JOB_RESULT_BYTES = 4 * 1024 * 1024
MAX_PLAN_BOUND_JOB_RETRY_REQUESTS = 100_000
MAX_PLAN_BOUND_JOB_TRANSITION_BINDINGS = 100_000
MAX_PLAN_BOUND_JOB_TRANSITION_BINDING_BYTES = 64 * 1024 * 1024
MAX_SUCCESSOR_TRANSITION_DISCARDS = 100_000
MAX_SUCCESSOR_TRANSITION_DISCARD_RECEIPT_BYTES = 128 * 1024
MAX_SUCCESSOR_TRANSITION_DISCARD_AGGREGATE_BYTES = 256 * 1024 * 1024
MAX_DATASET_CREATE_REQUESTS = 100_000
MAX_DATASET_CREATE_REQUEST_BYTES = 1024 * 1024
MAX_DATASET_CREATE_RESPONSE_BYTES = 16 * 1024
MAX_DATASET_CREATE_RECOVERY_BYTES = 128 * 1024 * 1024
MAX_DATASET_EVENT_PAYLOAD_BYTES = 64 * 1024 * 1024
MAX_DATASET_MANIFEST_BYTES = 4 * 1024 * 1024
MAX_DATASET_RECORDS_BYTES = 128 * 1024 * 1024
MAX_DATASET_ARTIFACT_MANIFEST_BYTES = 8 * 1024 * 1024
MAX_DATASET_MATERIALIZATION_BYTES = 256 * 1024 * 1024
MAX_DATASET_VERIFICATION_BYTES = 256 * 1024 * 1024
MAX_DATASET_STARTUP_RECOVERY_BYTES = 512 * 1024 * 1024
MAX_DATASET_READ_FILES = 1_000_000


class DatasetNotFoundError(ValueError):
    """The requested sealed dataset does not exist."""


class DatasetIntegrityError(ValueError):
    """Persisted dataset authorities do not form one exact sealed closure."""


class _InternalJobOutput(TypedDict):
    artifact_id: str
    type: str
    name: str
    manifest: dict[str, object]
    lineage: dict[str, object]
    compatibility: dict[str, object]
    scores: dict[str, object]
    promoted: bool
    created_at: str
    payload_manifest_digest: str
    payload_byte_size: int
    payload_file_count: int


class _InternalJobResult(TypedDict):
    artifact_ids: list[str]
    error: str | None
    job_id: str
    retryable: bool | None
    state: str
    successor_transition_id: str | None
    outputs: NotRequired[list[_InternalJobOutput]]


MAX_DATASET_ID_ATTEMPTS = 10
MAX_CONTEXT_ID_ATTEMPTS = 10
MAX_CONTEXT_MATERIALIZATION_RECOVERY_ROWS = 16_384
MAX_CONTEXT_MATERIALIZATION_RECOVERY_BYTES = 256 * 1024 * 1024
MAX_CONTEXT_MATERIALIZATION_ROW_BYTES = 4 * 1024 * 1024
MAX_EXECUTION_SNAPSHOT_RECOVERY_ROWS = 4_096
MAX_EXECUTION_SNAPSHOT_RECOVERY_BYTES = 64 * 1024 * 1024
MAX_EXECUTION_SNAPSHOT_ROW_BYTES = 128 * 1024
MAX_REVISION_RECOVERY_ROWS = 16_384
MAX_REVISION_RECOVERY_BYTES = 64 * 1024 * 1024
MAX_REVISION_ROW_BYTES = MAX_REVISION_MANIFEST_BYTES + 16 * 1024
MAX_REVISION_STREAM_RECOVERY_ROWS = 4_096
MAX_REVISION_STREAM_RECOVERY_BYTES = 4 * 1024 * 1024
MAX_REVISION_STREAM_ROW_BYTES = 16 * 1024
MAX_TASK_ADMISSION_RECOVERY_ROWS = 65_536
MAX_TASK_ADMISSION_RECOVERY_BYTES = 128 * 1024 * 1024
MAX_TASK_ADMISSION_ROW_BYTES = 128 * 1024
_TASK_ADMISSION_TEXT_BLOB_COLUMNS = (
    "admission_id",
    "stream_id",
    "task_id",
    "idempotency_key",
    "request_digest",
    "request_json",
    "status",
    "reason",
    "pinned_revision_id",
    "created_at",
    "updated_at",
    "finished_at",
)
RECOVERY_FETCH_ROWS = 128
_STORE_IDENTITY_FILENAME = ".openevo-store.json"
_STORE_IDENTITY_MAX_BYTES = 4096
_STORE_ID_RE = re.compile(r"store_[0-9a-f]{16}\Z", re.ASCII)
_STORE_IDENTITY_SCHEMA = """
CREATE TABLE store_identity (
    singleton INTEGER PRIMARY KEY CHECK(singleton = 1),
    store_id TEXT NOT NULL UNIQUE,
    artifact_root TEXT NOT NULL,
    binding_state TEXT NOT NULL CHECK(binding_state IN ('pending', 'bound'))
)
"""
ACTIVE_JOB_STATES = {str(JobState.CLAIMED), str(JobState.RUNNING)}
OPENEVO_SESSION_EVENT_SOURCE = "openevo"
OPENEVO_SESSION_EVENT_TYPE = "openevo.session_completed"
_ARTIFACT_MANIFEST_DIRECTORIES = frozenset(ARTIFACT_TYPE_DIRECTORIES.values())


def _after_store_identity_schema_created(conn: sqlite3.Connection) -> None:
    """Private no-op hook used by deterministic identity-bootstrap tests."""

    del conn


def _stat_identity(value: os.stat_result) -> tuple[int, int, int, int, int, int, int]:
    return (
        value.st_dev,
        value.st_ino,
        value.st_mode,
        value.st_nlink,
        value.st_size,
        value.st_mtime_ns,
        value.st_ctime_ns,
    )


def _directory_binding_identity(
    value: os.stat_result,
) -> tuple[int, int, int, int, int]:
    return (
        value.st_dev,
        value.st_ino,
        value.st_mode,
        value.st_uid,
        value.st_gid,
    )


@dataclass(frozen=True, slots=True)
class _OrphanMaterialization:
    name: str
    identity: _PrivateFileIdentity


@dataclass(slots=True)
class _RecoveryBudget:
    label: str
    max_rows: int
    max_bytes: int
    max_row_bytes: int
    rows: int = 0
    bytes: int = 0

    def consume_lengths(self, lengths: tuple[int, ...]) -> None:
        if any(type(length) is not int or length < 0 for length in lengths):
            raise RevisionIntegrityError(f"{self.label} has an invalid SQL byte length")
        row_bytes = sum(lengths)
        self.rows += 1
        self.bytes += row_bytes
        if row_bytes > self.max_row_bytes:
            raise RevisionIntegrityError(f"{self.label} exceeds the row byte limit")
        if self.rows > self.max_rows:
            raise RevisionIntegrityError(f"{self.label} exceeds the row limit")
        if self.bytes > self.max_bytes:
            raise RevisionIntegrityError(f"{self.label} exceeds the aggregate byte limit")


@dataclass(slots=True)
class _DatasetReadBudget:
    label: str
    max_files: int
    max_bytes: int
    files: int = 0
    bytes: int = 0

    def consume(self, byte_size: int, *, max_file_bytes: int) -> None:
        if type(byte_size) is not int or byte_size < 0:
            raise DatasetIntegrityError(f"{self.label} has an invalid file size")
        self.files += 1
        self.bytes += byte_size
        if byte_size > max_file_bytes:
            raise DatasetIntegrityError(f"{self.label} exceeds the file byte limit")
        if self.files > self.max_files:
            raise DatasetIntegrityError(f"{self.label} exceeds the file count limit")
        if self.bytes > self.max_bytes:
            raise DatasetIntegrityError(
                f"{self.label} exceeds the aggregate byte limit"
            )


@dataclass(frozen=True, slots=True)
class _GuardedTextSpec:
    column: str
    output: str
    max_bytes: int
    nullable: bool = False


@dataclass(frozen=True, slots=True)
class _GuardedRowPlan:
    key: str
    lengths: tuple[int, ...]


@dataclass(frozen=True, slots=True)
class _MaterializationRecoveryIdentity:
    context_id: str
    manifest_digest: str
    registry_digest: str
    request_digest: str
    base_model: str | None
    execution_mode: str
    capture_mode: str
    artifact_ids: tuple[str, ...]
    adapter_set_digest: str
    successor_transition_id: str | None


@dataclass(frozen=True, slots=True)
class _ExecutionSnapshotRecoveryIdentity:
    snapshot_digest: str
    producer_id: str
    model_id: str
    execution_mode: str
    capture_mode: str


@dataclass(frozen=True, slots=True)
class _RevisionRecoveryIdentity:
    revision_id: str
    stream_id: str
    generation: int
    predecessor_revision_id: str | None
    project_snapshot_id: str
    workspace_snapshot_id: str
    context_id: str
    context_artifact_ids: tuple[str, ...]
    context_artifact_set_digest: str
    execution_snapshot_id: str
    execution_mode: str
    capture_mode: str


@dataclass(frozen=True, slots=True)
class _AuthoritativeRevisionClosure:
    revision_id: str
    manifest: RevisionManifestV1
    identity: _RevisionRecoveryIdentity


def _sqlite_text_blob_bytes(values: sqlite3.Row | tuple[object, ...]) -> int:
    total = 0
    for value in values:
        if isinstance(value, str):
            total += len(value.encode("utf-8"))
        elif isinstance(value, bytes | bytearray | memoryview):
            total += len(value)
    return total


def _scan_guarded_row_plans(
    conn: sqlite3.Connection,
    *,
    from_sql: str,
    key_spec: _GuardedTextSpec,
    text_specs: tuple[_GuardedTextSpec, ...],
    order_by_sql: str,
    budget: _RecoveryBudget,
) -> list[_GuardedRowPlan]:
    key_length = f"length(CAST({key_spec.column} AS BLOB))"
    selections = [
        "CASE WHEN typeof({column}) = 'text' AND {length} <= {maximum} "
        "THEN {column} END AS __row_key".format(
            column=key_spec.column,
            length=key_length,
            maximum=key_spec.max_bytes,
        )
    ]
    for index, spec in enumerate(text_specs):
        length = f"length(CAST({spec.column} AS BLOB))"
        selections.append(
            f"CASE WHEN {spec.column} IS NULL THEN -1 ELSE {length} END AS __length_{index}"
        )
    cursor = conn.execute(
        f"SELECT {', '.join(selections)} FROM {from_sql} ORDER BY {order_by_sql}"
    )
    plans: list[_GuardedRowPlan] = []
    while True:
        rows = cursor.fetchmany(RECOVERY_FETCH_ROWS)
        if not rows:
            return plans
        for row in rows:
            key = row["__row_key"]
            if not isinstance(key, str):
                raise RevisionIntegrityError(f"{budget.label} row key is invalid")
            lengths: list[int] = []
            for index, spec in enumerate(text_specs):
                length = row[f"__length_{index}"]
                if type(length) is not int or length < -1:
                    raise RevisionIntegrityError(f"{budget.label} has an invalid SQL byte length")
                if length == -1:
                    if not spec.nullable:
                        raise RevisionIntegrityError(
                            f"{budget.label} has a null required text value"
                        )
                    lengths.append(-1)
                    continue
                if length > spec.max_bytes:
                    raise RevisionIntegrityError(f"{budget.label} exceeds the value byte limit")
                lengths.append(length)
            budget.consume_lengths(tuple(0 if length == -1 else length for length in lengths))
            plans.append(_GuardedRowPlan(key=key, lengths=tuple(lengths)))


def _guarded_text_select(
    spec: _GuardedTextSpec,
    expected_bytes: int,
) -> tuple[str, str]:
    if expected_bytes == -1 and spec.nullable:
        valid = f"{spec.column} IS NULL"
    else:
        valid = (
            f"typeof({spec.column}) = 'text' "
            f"AND length(CAST({spec.column} AS BLOB)) = {expected_bytes} "
            f"AND length(CAST({spec.column} AS BLOB)) <= {spec.max_bytes}"
        )
    return (
        f"CASE WHEN {valid} THEN {spec.column} END AS {spec.output}",
        f"CASE WHEN {valid} THEN 1 ELSE 0 END AS __guard_{spec.output}",
    )


def _fetch_guarded_row(
    conn: sqlite3.Connection,
    *,
    from_sql: str,
    where_sql: str,
    key: object,
    text_specs: tuple[_GuardedTextSpec, ...],
    lengths: tuple[int, ...],
    scalar_selections: tuple[str, ...] = (),
    label: str,
) -> sqlite3.Row:
    selections = list(scalar_selections)
    for spec, expected_bytes in zip(text_specs, lengths, strict=True):
        value_sql, guard_sql = _guarded_text_select(spec, expected_bytes)
        selections.extend((value_sql, guard_sql))
    try:
        row = conn.execute(
            f"SELECT {', '.join(selections)} FROM {from_sql} WHERE {where_sql}",
            (key,),
        ).fetchone()
    except sqlite3.Error as exc:
        raise RevisionIntegrityError(f"{label} guarded read failed") from exc
    if row is None:
        raise RevisionIntegrityError(f"{label} row disappeared during recovery")
    for spec, expected_bytes in zip(text_specs, lengths, strict=True):
        if row[f"__guard_{spec.output}"] != 1:
            raise RevisionIntegrityError(f"{label} SQL byte guard failed")
        value = row[spec.output]
        if value is None:
            if not spec.nullable or expected_bytes != -1:
                raise RevisionIntegrityError(f"{label} guarded text is invalid")
            continue
        if (
            not isinstance(value, str)
            or len(value.encode("utf-8")) != expected_bytes
            or expected_bytes > spec.max_bytes
        ):
            raise RevisionIntegrityError(f"{label} guarded text byte length changed")
    return row


def _read_guarded_materialized_context_row(
    conn: sqlite3.Connection,
    context_id: str,
) -> sqlite3.Row | None:
    specs = (
        _GuardedTextSpec("context_id", "context_id", 256),
        _GuardedTextSpec("registry_digest", "registry_digest", 64),
        _GuardedTextSpec("request_digest", "request_digest", 64),
        _GuardedTextSpec(
            "manifest_json",
            "manifest_json",
            MAX_CONTEXT_MATERIALIZATION_ROW_BYTES,
        ),
    )
    selections = [
        "CASE WHEN typeof(context_id) = 'text' "
        "AND length(CAST(context_id AS BLOB)) <= 256 "
        "THEN context_id END AS __row_key"
    ]
    selections.extend(
        f"CASE WHEN {spec.column} IS NULL THEN -1 "
        f"ELSE length(CAST({spec.column} AS BLOB)) END AS __length_{index}"
        for index, spec in enumerate(specs)
    )
    row = conn.execute(
        f"SELECT {', '.join(selections)} FROM context_materializations WHERE context_id = ?",
        (context_id,),
    ).fetchone()
    if row is None:
        return None
    if row["__row_key"] != context_id:
        raise RevisionIntegrityError("materialized context row key is invalid")
    lengths: list[int] = []
    for index, spec in enumerate(specs):
        length = row[f"__length_{index}"]
        if type(length) is not int or length < 0 or length > spec.max_bytes:
            raise RevisionIntegrityError(
                "materialized context exceeds its guarded value byte limit"
            )
        lengths.append(length)
    budget = _RecoveryBudget(
        label="materialized context transport",
        max_rows=1,
        max_bytes=MAX_CONTEXT_MATERIALIZATION_ROW_BYTES,
        max_row_bytes=MAX_CONTEXT_MATERIALIZATION_ROW_BYTES,
    )
    budget.consume_lengths(tuple(lengths))
    return _fetch_guarded_row(
        conn,
        from_sql="context_materializations",
        where_sql="context_id = ?",
        key=context_id,
        text_specs=specs,
        lengths=tuple(lengths),
        label="materialized context transport",
    )


def _read_guarded_materialized_context_rows(
    conn: sqlite3.Connection,
    *,
    label: str,
) -> list[sqlite3.Row]:
    specs = (
        _GuardedTextSpec("context_id", "context_id", 256),
        _GuardedTextSpec("registry_digest", "registry_digest", 64),
        _GuardedTextSpec("request_digest", "request_digest", 64),
        _GuardedTextSpec(
            "manifest_json",
            "manifest_json",
            MAX_CONTEXT_MATERIALIZATION_ROW_BYTES,
        ),
    )
    plans = _scan_guarded_row_plans(
        conn,
        from_sql="context_materializations",
        key_spec=specs[0],
        text_specs=specs,
        order_by_sql="context_id",
        budget=_RecoveryBudget(
            label=label,
            max_rows=MAX_CONTEXT_MATERIALIZATION_RECOVERY_ROWS,
            max_bytes=MAX_CONTEXT_MATERIALIZATION_RECOVERY_BYTES,
            max_row_bytes=MAX_CONTEXT_MATERIALIZATION_ROW_BYTES,
        ),
    )
    return [
        _fetch_guarded_row(
            conn,
            from_sql="context_materializations",
            where_sql="context_id = ?",
            key=plan.key,
            text_specs=specs,
            lengths=plan.lengths,
            label=label,
        )
        for plan in plans
    ]


def _read_guarded_store_identity_row(conn: sqlite3.Connection) -> sqlite3.Row:
    specs = (
        _GuardedTextSpec("store_id", "store_id", 64),
        _GuardedTextSpec(
            "artifact_root",
            "artifact_root",
            _STORE_IDENTITY_MAX_BYTES,
        ),
        _GuardedTextSpec("binding_state", "binding_state", 16),
    )
    selections = ["CASE WHEN typeof(singleton) = 'integer' THEN singleton END AS singleton"]
    for index, spec in enumerate(specs):
        selections.append(
            f"CASE WHEN {spec.column} IS NULL THEN -1 "
            f"ELSE length(CAST({spec.column} AS BLOB)) END AS __length_{index}"
        )
    try:
        cursor = conn.execute(
            f"SELECT {', '.join(selections)} FROM store_identity ORDER BY singleton"
        )
        rows = cursor.fetchmany(2)
    except sqlite3.Error as exc:
        raise ValueError("evolution database store identity is unavailable") from exc
    if len(rows) != 1 or type(rows[0]["singleton"]) is not int:
        raise ValueError("evolution database store identity is invalid")
    lengths = tuple(rows[0][f"__length_{index}"] for index in range(len(specs)))
    budget = _RecoveryBudget(
        label="store identity recovery",
        max_rows=1,
        max_bytes=sum(spec.max_bytes for spec in specs),
        max_row_bytes=sum(spec.max_bytes for spec in specs),
    )
    try:
        for length, spec in zip(lengths, specs, strict=True):
            if type(length) is not int or length < 0 or length > spec.max_bytes:
                raise RevisionIntegrityError(
                    "store identity recovery exceeds the value byte limit"
                )
        budget.consume_lengths(lengths)
        return _fetch_guarded_row(
            conn,
            from_sql="store_identity",
            where_sql="singleton = ?",
            key=rows[0]["singleton"],
            text_specs=specs,
            lengths=lengths,
            scalar_selections=("singleton",),
            label="store identity recovery",
        )
    except RevisionIntegrityError as exc:
        raise ValueError("evolution database store identity is invalid") from exc


def _ledger_payload_usage(
    conn: sqlite3.Connection,
    table: str,
    text_blob_columns: tuple[str, ...],
) -> tuple[int, int]:
    byte_expression = " + ".join(
        f"COALESCE(length(CAST({column} AS BLOB)), 0)" for column in text_blob_columns
    )
    row = conn.execute(
        f"SELECT COUNT(*), COALESCE(SUM({byte_expression}), 0) FROM {table}"
    ).fetchone()
    return int(row[0]), int(row[1])


def _enforce_ledger_capacity(
    conn: sqlite3.Connection,
    *,
    table: str,
    text_blob_columns: tuple[str, ...],
    new_text_blob_values: tuple[object, ...],
    max_rows: int,
    max_bytes: int,
    max_row_bytes: int,
    label: str,
) -> None:
    new_bytes = _sqlite_text_blob_bytes(new_text_blob_values)
    if new_bytes > max_row_bytes:
        raise RevisionCapacityError(f"{label} row byte capacity is exhausted")
    rows, used_bytes = _ledger_payload_usage(conn, table, text_blob_columns)
    if rows >= max_rows:
        raise RevisionCapacityError(f"{label} row capacity is exhausted")
    if used_bytes + new_bytes > max_bytes:
        raise RevisionCapacityError(f"{label} byte capacity is exhausted")


def _enforce_ledger_update_capacity(
    conn: sqlite3.Connection,
    *,
    table: str,
    key_column: str,
    key_value: str,
    text_blob_columns: tuple[str, ...],
    new_text_blob_values: tuple[object, ...],
    max_bytes: int,
    max_row_bytes: int,
    label: str,
) -> int:
    new_bytes = _sqlite_text_blob_bytes(new_text_blob_values)
    if new_bytes > max_row_bytes:
        raise RevisionCapacityError(f"{label} row byte capacity is exhausted")
    byte_expression = " + ".join(
        f"COALESCE(length(CAST({column} AS BLOB)), 0)" for column in text_blob_columns
    )
    old = conn.execute(
        f"SELECT {byte_expression} FROM {table} WHERE {key_column} = ?",
        (key_value,),
    ).fetchone()
    if old is None or type(old[0]) is not int or old[0] < 0:
        raise RevisionIntegrityError(f"{label} update source is invalid")
    _rows, used_bytes = _ledger_payload_usage(conn, table, text_blob_columns)
    if used_bytes < old[0]:
        raise RevisionIntegrityError(f"{label} persisted byte accounting is invalid")
    if used_bytes - old[0] + new_bytes > max_bytes:
        raise RevisionCapacityError(f"{label} byte capacity is exhausted")
    return new_bytes


def _enforce_task_admission_transition_capacity(
    conn: sqlite3.Connection,
    row: sqlite3.Row,
    *,
    status: AdmissionStatus,
    reason: AdmissionQueueReason | None,
    pinned_revision_id: str | None,
    updated_at: str,
    finished_at: str | None,
) -> int:
    overrides = {
        "status": str(status),
        "reason": None if reason is None else str(reason),
        "pinned_revision_id": pinned_revision_id,
        "updated_at": updated_at,
        "finished_at": finished_at,
    }
    new_values = tuple(
        overrides[column] if column in overrides else row[column]
        for column in _TASK_ADMISSION_TEXT_BLOB_COLUMNS
    )
    return _enforce_ledger_update_capacity(
        conn,
        table="task_admissions",
        key_column="admission_id",
        key_value=str(row["admission_id"]),
        text_blob_columns=_TASK_ADMISSION_TEXT_BLOB_COLUMNS,
        new_text_blob_values=new_values,
        max_bytes=MAX_TASK_ADMISSION_RECOVERY_BYTES,
        max_row_bytes=MAX_TASK_ADMISSION_ROW_BYTES,
        label="task admission ledger",
    )


def _verify_task_admission_transition_capacity(
    conn: sqlite3.Connection,
    row: sqlite3.Row,
    *,
    expected_row_bytes: int,
) -> None:
    actual_row_bytes = _sqlite_text_blob_bytes(
        tuple(row[column] for column in _TASK_ADMISSION_TEXT_BLOB_COLUMNS)
    )
    if actual_row_bytes != expected_row_bytes:
        raise RevisionIntegrityError("task admission transition byte accounting is inconsistent")
    if actual_row_bytes > MAX_TASK_ADMISSION_ROW_BYTES:
        raise RevisionCapacityError("task admission ledger row byte capacity is exhausted")
    _rows, used_bytes = _ledger_payload_usage(
        conn,
        "task_admissions",
        _TASK_ADMISSION_TEXT_BLOB_COLUMNS,
    )
    if used_bytes > MAX_TASK_ADMISSION_RECOVERY_BYTES:
        raise RevisionCapacityError("task admission ledger byte capacity is exhausted")


def _enforce_materialization_capacity(
    conn: sqlite3.Connection,
    new_text_blob_values: tuple[object, ...],
) -> None:
    new_bytes = _sqlite_text_blob_bytes(new_text_blob_values)
    if new_bytes > MAX_CONTEXT_MATERIALIZATION_ROW_BYTES:
        raise RevisionCapacityError(
            "context materialization ledger row byte capacity is exhausted"
        )
    row = conn.execute(
        """
        SELECT COUNT(*), COALESCE(SUM(
            length(CAST(context_materializations.context_id AS BLOB))
            + length(CAST(context_materializations.created_at AS BLOB))
            + length(CAST(context_materializations.registry_digest AS BLOB))
            + length(CAST(context_materializations.request_digest AS BLOB))
            + length(CAST(context_materializations.manifest_json AS BLOB))
            + length(CAST(contexts.request_json AS BLOB))
            + length(CAST(contexts.response_json AS BLOB))
        ), 0)
        FROM context_materializations JOIN contexts USING (context_id)
        """
    ).fetchone()
    if int(row[0]) >= MAX_CONTEXT_MATERIALIZATION_RECOVERY_ROWS:
        raise RevisionCapacityError("context materialization ledger row capacity is exhausted")
    if int(row[1]) + new_bytes > MAX_CONTEXT_MATERIALIZATION_RECOVERY_BYTES:
        raise RevisionCapacityError("context materialization ledger byte capacity is exhausted")


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


def _dataset_create_request_json(request: DatasetCreateRequest) -> str:
    encoded = json.dumps(
        request.model_dump(mode="json", exclude_none=True),
        ensure_ascii=True,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    if len(encoded.encode("utf-8")) > MAX_DATASET_CREATE_REQUEST_BYTES:
        raise ValueError("dataset create request exceeds its byte limit")
    return encoded


def _dataset_create_response_json(response: DatasetCreateResponse) -> str:
    encoded = json.dumps(
        response.model_dump(mode="json"),
        ensure_ascii=True,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    if len(encoded.encode("utf-8")) > MAX_DATASET_CREATE_RESPONSE_BYTES:
        raise ValueError("dataset create response exceeds its byte limit")
    return encoded


def _dataset_id_for_idempotency_key(idempotency_key: str) -> str:
    return (
        "ds-"
        + hashlib.sha256(
            f"openevo-dataset\0{idempotency_key}".encode("utf-8")
        ).hexdigest()
    )


def _canonical_json_hash(payload: dict[str, Any]) -> str:
    return "sha256:" + hashlib.sha256(_json_dumps(payload).encode("utf-8")).hexdigest()


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


def _parse_canonical_utc_iso(value: object, *, label: str) -> datetime:
    if not isinstance(value, str):
        raise ValueError(f"{label} must be a canonical UTC timestamp")
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() != timedelta(0):
        raise ValueError(f"{label} must be a canonical UTC timestamp")
    normalized = _utc_dt_to_iso(parsed)
    if value != normalized:
        raise ValueError(f"{label} must be a canonical UTC timestamp")
    return parsed


def _strict_sqlite_integer(value: object, *, label: str) -> int:
    if type(value) is not int:
        raise ValueError(f"{label} must be stored as an INTEGER")
    return value


_SUCCESSOR_TRANSITION_ID_RE = re.compile(
    r"[A-Za-z0-9][A-Za-z0-9._-]{0,255}"
)
_MAX_DISCARDED_MATERIALIZED_CONTEXTS = 100


def _successor_transition_discard_receipt(
    successor_transition_id: str,
    *,
    artifact_ids: list[str],
    materialized_context_ids: list[str],
) -> dict[str, object]:
    if (
        _SUCCESSOR_TRANSITION_ID_RE.fullmatch(
            successor_transition_id
        )
        is None
    ):
        raise ValueError("successor transition identity is invalid")
    for values, maximum, label in (
        (
            artifact_ids,
            MAX_INTERNAL_JOB_OUTPUTS,
            "discarded artifact",
        ),
        (
            materialized_context_ids,
            _MAX_DISCARDED_MATERIALIZED_CONTEXTS,
            "discarded materialized context",
        ),
    ):
        if (
            len(values) > maximum
            or any(
                not isinstance(value, str)
                or _SUCCESSOR_TRANSITION_ID_RE.fullmatch(
                    value
                )
                is None
                for value in values
            )
        ):
            raise ValueError(
                f"{label} inventory is invalid"
            )
        if (
            values != sorted(values)
            or len(values) != len(set(values))
        ):
            raise ValueError(
                f"{label} inventory is invalid"
            )
    return {
        "successor_transition_id": successor_transition_id,
        "discarded_artifact_ids": artifact_ids,
        "discarded_materialized_context_ids": (
            materialized_context_ids
        ),
    }


def _successor_transition_discard_receipt_json(
    receipt: dict[str, object],
) -> str:
    encoded = json.dumps(
        receipt,
        ensure_ascii=True,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    if (
        len(encoded.encode("utf-8"))
        > MAX_SUCCESSOR_TRANSITION_DISCARD_RECEIPT_BYTES
    ):
        raise ValueError(
            "successor transition discard receipt exceeds its byte bound"
        )
    return encoded


def _load_successor_transition_discard(
    conn: sqlite3.Connection,
    successor_transition_id: str,
) -> dict[str, object] | None:
    size_row = conn.execute(
        "SELECT typeof(receipt_json) AS receipt_type, "
        "length(CAST(receipt_json AS BLOB)) AS receipt_bytes, "
        "typeof(receipt_sha256) AS sha_type, "
        "length(CAST(receipt_sha256 AS BLOB)) AS sha_bytes, "
        "typeof(discarded_at) AS discarded_at_type, "
        "length(CAST(discarded_at AS BLOB)) AS discarded_at_bytes "
        "FROM successor_transition_discards "
        "WHERE successor_transition_id = ?",
        (successor_transition_id,),
    ).fetchone()
    if size_row is None:
        return None
    if (
        size_row["receipt_type"] != "text"
        or type(size_row["receipt_bytes"]) is not int
        or not 1
        <= int(size_row["receipt_bytes"])
        <= MAX_SUCCESSOR_TRANSITION_DISCARD_RECEIPT_BYTES
        or size_row["sha_type"] != "text"
        or size_row["sha_bytes"] != 64
        or size_row["discarded_at_type"] != "text"
        or type(size_row["discarded_at_bytes"]) is not int
        or not 1 <= int(size_row["discarded_at_bytes"]) <= 64
    ):
        raise ValueError(
            "successor transition discard receipt is malformed"
        )
    row = conn.execute(
        "SELECT CASE WHEN typeof(receipt_json) = 'text' "
        "AND length(CAST(receipt_json AS BLOB)) = ? "
        "THEN receipt_json END AS receipt_json, "
        "CASE WHEN typeof(receipt_sha256) = 'text' "
        "AND length(CAST(receipt_sha256 AS BLOB)) = 64 "
        "THEN receipt_sha256 END AS receipt_sha256, "
        "CASE WHEN typeof(discarded_at) = 'text' "
        "AND length(CAST(discarded_at AS BLOB)) = ? "
        "THEN discarded_at END AS discarded_at "
        "FROM successor_transition_discards "
        "WHERE successor_transition_id = ?",
        (
            int(size_row["receipt_bytes"]),
            int(size_row["discarded_at_bytes"]),
            successor_transition_id,
        ),
    ).fetchone()
    if (
        row is None
        or not isinstance(row["receipt_json"], str)
        or not isinstance(row["receipt_sha256"], str)
        or not isinstance(row["discarded_at"], str)
        or len(row["receipt_json"].encode("utf-8"))
        != int(size_row["receipt_bytes"])
    ):
        raise ValueError(
            "successor transition discard receipt changed during read"
        )
    try:
        raw = json.loads(row["receipt_json"])
        _parse_canonical_utc_iso(
            row["discarded_at"],
            label="successor transition discard timestamp",
        )
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise ValueError(
            "successor transition discard receipt is invalid"
        ) from exc
    if (
        type(raw) is not dict
        or set(raw) != {
            "successor_transition_id",
            "discarded_artifact_ids",
            "discarded_materialized_context_ids",
        }
        or raw.get("successor_transition_id")
        != successor_transition_id
        or type(raw.get("discarded_artifact_ids")) is not list
        or type(raw.get("discarded_materialized_context_ids"))
        is not list
    ):
        raise ValueError(
            "successor transition discard receipt is invalid"
        )
    receipt = _successor_transition_discard_receipt(
        successor_transition_id,
        artifact_ids=list(raw["discarded_artifact_ids"]),
        materialized_context_ids=list(
            raw["discarded_materialized_context_ids"]
        ),
    )
    canonical = _successor_transition_discard_receipt_json(
        receipt
    )
    if (
        canonical != row["receipt_json"]
        or hashlib.sha256(
            canonical.encode("utf-8")
        ).hexdigest()
        != row["receipt_sha256"]
    ):
        raise ValueError(
            "successor transition discard receipt is inconsistent"
        )
    return receipt


def _require_successor_transition_not_discarded(
    conn: sqlite3.Connection,
    successor_transition_id: str | None,
) -> None:
    if (
        successor_transition_id is not None
        and conn.execute(
            "SELECT 1 FROM successor_transition_discards "
            "WHERE successor_transition_id = ?",
            (successor_transition_id,),
        ).fetchone()
        is not None
    ):
        raise ValueError("successor transition was discarded")


def _bounded_regular_file_bytes(
    path: Path,
    *,
    budget: _DatasetReadBudget,
    max_file_bytes: int,
    label: str,
) -> bytes:
    flags = os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags)
    except FileNotFoundError as exc:
        raise DatasetIntegrityError(f"{label} is missing: {path}") from exc
    except OSError as exc:
        raise DatasetIntegrityError(f"{label} could not be opened safely: {path}") from exc
    try:
        before = os.fstat(descriptor)
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_uid != os.geteuid()
            or before.st_nlink != 1
        ):
            raise DatasetIntegrityError(f"{label} is not an owned private file: {path}")
        budget.consume(int(before.st_size), max_file_bytes=max_file_bytes)
        chunks: list[bytes] = []
        remaining = int(before.st_size)
        while remaining:
            chunk = os.read(descriptor, min(1024 * 1024, remaining))
            if not chunk:
                raise DatasetIntegrityError(f"{label} changed while being read: {path}")
            chunks.append(chunk)
            remaining -= len(chunk)
        if os.read(descriptor, 1):
            raise DatasetIntegrityError(f"{label} grew while being read: {path}")
        after = os.fstat(descriptor)
        if (
            before.st_dev,
            before.st_ino,
            before.st_mode,
            before.st_uid,
            before.st_nlink,
            before.st_size,
        ) != (
            after.st_dev,
            after.st_ino,
            after.st_mode,
            after.st_uid,
            after.st_nlink,
            after.st_size,
        ):
            raise DatasetIntegrityError(f"{label} identity changed while being read: {path}")
        return b"".join(chunks)
    finally:
        os.close(descriptor)


def _entry_stat(
    directory_descriptor: int,
    name: str,
) -> os.stat_result | None:
    try:
        return os.stat(
            name,
            dir_fd=directory_descriptor,
            follow_symlinks=False,
        )
    except FileNotFoundError:
        return None


def _dataset_file_identity(
    entry: os.stat_result,
) -> tuple[int, int, int, int, int, int, int, int, int]:
    return (
        int(entry.st_dev),
        int(entry.st_ino),
        int(entry.st_mode),
        int(entry.st_uid),
        int(entry.st_gid),
        int(entry.st_nlink),
        int(entry.st_size),
        int(entry.st_mtime_ns),
        int(entry.st_ctime_ns),
    )


def _dataset_file_stable_identity(
    entry: os.stat_result,
) -> tuple[int, int, int, int, int, int, int]:
    return (
        int(entry.st_dev),
        int(entry.st_ino),
        int(entry.st_uid),
        int(entry.st_gid),
        int(entry.st_nlink),
        int(entry.st_size),
        int(entry.st_mtime_ns),
    )


def _require_legacy_dataset_mode_candidate(
    entry: os.stat_result,
    *,
    max_file_bytes: int,
    label: str,
) -> int:
    permissions = stat.S_IMODE(entry.st_mode)
    if not stat.S_ISREG(entry.st_mode):
        raise DatasetIntegrityError(f"{label} is not a regular file")
    if entry.st_uid != os.geteuid():
        raise DatasetIntegrityError(f"{label} is not owned by the effective user")
    if entry.st_nlink != 1:
        raise DatasetIntegrityError(f"{label} is not link-count-one")
    if permissions & 0o7000:
        raise DatasetIntegrityError(f"{label} has special permission bits")
    if permissions & 0o111:
        raise DatasetIntegrityError(f"{label} has executable permission bits")
    if permissions & 0o600 != 0o600:
        raise DatasetIntegrityError(f"{label} is not owner-readable and writable")
    if permissions & 0o022:
        raise DatasetIntegrityError(f"{label} is group/other writable")
    if entry.st_size < 0 or entry.st_size > max_file_bytes:
        raise DatasetIntegrityError(f"{label} exceeds its byte limit")
    return permissions


def _require_dataset_file_path_identity(
    directory_descriptor: int,
    name: str,
    expected: os.stat_result,
    *,
    label: str,
) -> None:
    observed = _entry_stat(directory_descriptor, name)
    if (
        observed is None
        or _dataset_file_identity(observed)
        != _dataset_file_identity(expected)
    ):
        raise DatasetIntegrityError(f"{label} identity changed")


def _before_legacy_dataset_fchmod(
    directory_descriptor: int,
    dataset_id: str,
    name: str,
    descriptor: int,
) -> None:
    """Private no-op hook for deterministic dataset migration race tests."""

    del directory_descriptor, dataset_id, name, descriptor


def _migrate_legacy_dataset_file_mode(
    directory_descriptor: int,
    *,
    dataset_id: str,
    name: str,
    max_file_bytes: int,
) -> bool:
    label = f"legacy dataset {dataset_id!r} file {name!r}"
    path_entry = _entry_stat(directory_descriptor, name)
    if path_entry is None:
        raise DatasetIntegrityError(f"{label} is missing")
    path_permissions = _require_legacy_dataset_mode_candidate(
        path_entry,
        max_file_bytes=max_file_bytes,
        label=label,
    )
    flags = os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_NONBLOCK
    try:
        descriptor = os.open(
            name,
            flags,
            dir_fd=directory_descriptor,
        )
    except OSError as exc:
        raise DatasetIntegrityError(f"{label} could not be opened safely") from exc
    try:
        opened = os.fstat(descriptor)
        opened_permissions = _require_legacy_dataset_mode_candidate(
            opened,
            max_file_bytes=max_file_bytes,
            label=label,
        )
        if (
            _dataset_file_identity(opened)
            != _dataset_file_identity(path_entry)
            or opened_permissions != path_permissions
        ):
            raise DatasetIntegrityError(f"{label} changed before it was opened")
        _require_dataset_file_path_identity(
            directory_descriptor,
            name,
            opened,
            label=label,
        )
        if opened_permissions == 0o600:
            return False
        _before_legacy_dataset_fchmod(
            directory_descriptor,
            dataset_id,
            name,
            descriptor,
        )
        before_chmod = os.fstat(descriptor)
        before_permissions = _require_legacy_dataset_mode_candidate(
            before_chmod,
            max_file_bytes=max_file_bytes,
            label=label,
        )
        if (
            _dataset_file_identity(before_chmod)
            != _dataset_file_identity(opened)
            or before_permissions != opened_permissions
        ):
            raise DatasetIntegrityError(f"{label} changed before chmod")
        _require_dataset_file_path_identity(
            directory_descriptor,
            name,
            before_chmod,
            label=label,
        )
        os.fchmod(descriptor, 0o600)
        os.fsync(descriptor)
        migrated = os.fstat(descriptor)
        if (
            stat.S_IMODE(migrated.st_mode) != 0o600
            or _dataset_file_stable_identity(migrated)
            != _dataset_file_stable_identity(opened)
        ):
            raise DatasetIntegrityError(f"{label} changed during migration")
        _require_dataset_file_path_identity(
            directory_descriptor,
            name,
            migrated,
            label=label,
        )
        return True
    finally:
        os.close(descriptor)


def _require_owned_regular_entry(
    entry: os.stat_result,
    *,
    label: str,
    allowed_link_counts: set[int],
) -> None:
    if (
        not stat.S_ISREG(entry.st_mode)
        or entry.st_uid != os.geteuid()
        or entry.st_nlink not in allowed_link_counts
    ):
        raise DatasetIntegrityError(f"{label} is not an owned regular file")


def _unlink_entry_if_identity(
    directory_descriptor: int,
    name: str,
    identity: os.stat_result,
    *,
    label: str,
) -> None:
    current = _entry_stat(directory_descriptor, name)
    if current is None:
        return
    if (
        current.st_dev,
        current.st_ino,
        current.st_mode,
        current.st_uid,
        current.st_nlink,
    ) != (
        identity.st_dev,
        identity.st_ino,
        identity.st_mode,
        identity.st_uid,
        identity.st_nlink,
    ):
        raise DatasetIntegrityError(f"{label} identity changed before cleanup")
    os.unlink(name, dir_fd=directory_descriptor)


def _write_bytes_strict_exclusive(
    files: ArtifactFileStore,
    path: Path,
    payload: bytes,
) -> Path:
    path = Path(os.path.abspath(path))
    try:
        path.relative_to(files.root)
    except ValueError as exc:
        raise ValueError(f"path escapes artifact root: {path}") from exc
    path.parent.mkdir(parents=True, exist_ok=True)
    directory_flags = os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_DIRECTORY
    directory_descriptor = os.open(path.parent, directory_flags)
    target_name = path.name
    pending_name = f".{target_name}.pending"
    pending_identity: os.stat_result | None = None
    target_linked = False
    try:
        if _entry_stat(directory_descriptor, target_name) is not None:
            raise FileExistsError(path)
        pending = _entry_stat(directory_descriptor, pending_name)
        if pending is not None:
            _require_owned_regular_entry(
                pending,
                label="staged dataset file",
                allowed_link_counts={1},
            )
            _unlink_entry_if_identity(
                directory_descriptor,
                pending_name,
                pending,
                label="staged dataset file",
            )
            os.fsync(directory_descriptor)
        descriptor = os.open(
            pending_name,
            os.O_WRONLY
            | os.O_CREAT
            | os.O_EXCL
            | os.O_CLOEXEC
            | os.O_NOFOLLOW,
            0o600,
            dir_fd=directory_descriptor,
        )
        try:
            offset = 0
            while offset < len(payload):
                written = os.write(descriptor, payload[offset:])
                if written <= 0:
                    raise OSError("staged dataset file write made no progress")
                offset += written
            os.fsync(descriptor)
            pending_identity = os.fstat(descriptor)
            _require_owned_regular_entry(
                pending_identity,
                label="staged dataset file",
                allowed_link_counts={1},
            )
            if int(pending_identity.st_size) != len(payload):
                raise DatasetIntegrityError("staged dataset file size is inconsistent")
        finally:
            os.close(descriptor)
        os.link(
            pending_name,
            target_name,
            src_dir_fd=directory_descriptor,
            dst_dir_fd=directory_descriptor,
            follow_symlinks=False,
        )
        target_linked = True
        os.fsync(directory_descriptor)
        linked_pending = _entry_stat(directory_descriptor, pending_name)
        linked_target = _entry_stat(directory_descriptor, target_name)
        if (
            linked_pending is None
            or linked_target is None
            or linked_pending.st_dev != linked_target.st_dev
            or linked_pending.st_ino != linked_target.st_ino
            or linked_pending.st_nlink != 2
            or linked_target.st_nlink != 2
        ):
            raise DatasetIntegrityError("dataset file publication identity is inconsistent")
        _unlink_entry_if_identity(
            directory_descriptor,
            pending_name,
            linked_pending,
            label="staged dataset file",
        )
        os.fsync(directory_descriptor)
    except BaseException:
        if target_linked:
            try:
                linked_pending = _entry_stat(
                    directory_descriptor,
                    pending_name,
                )
                linked_target = _entry_stat(
                    directory_descriptor,
                    target_name,
                )
                if (
                    linked_pending is None
                    or linked_target is None
                    or linked_pending.st_dev != linked_target.st_dev
                    or linked_pending.st_ino != linked_target.st_ino
                    or linked_pending.st_nlink != 2
                    or linked_target.st_nlink != 2
                ):
                    raise DatasetIntegrityError(
                        "failed dataset publication could not be fenced"
                    )
                _unlink_entry_if_identity(
                    directory_descriptor,
                    pending_name,
                    linked_pending,
                    label="staged dataset file",
                )
                remaining_target = _entry_stat(
                    directory_descriptor,
                    target_name,
                )
                assert remaining_target is not None
                _unlink_entry_if_identity(
                    directory_descriptor,
                    target_name,
                    remaining_target,
                    label="failed dataset publication",
                )
                os.fsync(directory_descriptor)
            except (OSError, DatasetIntegrityError):
                pass
        elif pending_identity is not None:
            try:
                _unlink_entry_if_identity(
                    directory_descriptor,
                    pending_name,
                    pending_identity,
                    label="staged dataset file",
                )
                os.fsync(directory_descriptor)
            except (OSError, DatasetIntegrityError):
                pass
        raise
    finally:
        os.close(directory_descriptor)
    return path


def _write_json_strict_exclusive(
    files: ArtifactFileStore,
    path: Path,
    payload: dict[str, Any],
) -> Path:
    serialized = _json_dumps(payload, indent=2)
    return _write_bytes_strict_exclusive(
        files,
        path,
        serialized.encode("utf-8"),
    )


def _context_snapshot_bytes(
    request_payload: dict[str, object],
    response_payload: dict[str, object],
) -> bytes:
    return _json_dumps(
        {
            "request": request_payload,
            "response": response_payload,
        },
        indent=2,
    ).encode("utf-8")


def _write_jsonl_strict_exclusive(
    files: ArtifactFileStore,
    path: Path,
    records: list[dict[str, Any]],
) -> Path:
    return _write_bytes_strict_exclusive(
        files,
        path,
        _dataset_records_bytes(records),
    )


def _dataset_records_bytes(records: list[dict[str, Any]]) -> bytes:
    return "".join(
        f"{_json_dumps(record)}\n"
        for record in records
    ).encode("utf-8")


def _reconcile_dataset_pending_file(path: Path) -> bool:
    path.parent.mkdir(parents=True, exist_ok=True)
    directory_descriptor = os.open(
        path.parent,
        os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_DIRECTORY,
    )
    target_name = path.name
    pending_name = f".{target_name}.pending"
    try:
        target = _entry_stat(directory_descriptor, target_name)
        pending = _entry_stat(directory_descriptor, pending_name)
        if target is None:
            if pending is not None:
                _require_owned_regular_entry(
                    pending,
                    label="staged dataset file",
                    allowed_link_counts={1},
                )
            return False
        _require_owned_regular_entry(
            target,
            label="published dataset file",
            allowed_link_counts={1, 2},
        )
        if pending is not None:
            _require_owned_regular_entry(
                pending,
                label="staged dataset file",
                allowed_link_counts={1, 2},
            )
            if target.st_nlink == 2:
                if (
                    pending.st_nlink != 2
                    or pending.st_dev != target.st_dev
                    or pending.st_ino != target.st_ino
                ):
                    raise DatasetIntegrityError(
                        "dataset file publication links are inconsistent"
                    )
            elif pending.st_nlink != 1 or (
                pending.st_dev == target.st_dev
                and pending.st_ino == target.st_ino
            ):
                raise DatasetIntegrityError(
                    "dataset staged file inventory is inconsistent"
                )
            _unlink_entry_if_identity(
                directory_descriptor,
                pending_name,
                pending,
                label="staged dataset file",
            )
            os.fsync(directory_descriptor)
            target = _entry_stat(directory_descriptor, target_name)
            assert target is not None
        _require_owned_regular_entry(
            target,
            label="published dataset file",
            allowed_link_counts={1},
        )
        return True
    finally:
        os.close(directory_descriptor)


def _ensure_dataset_json_file(
    files: ArtifactFileStore,
    path: Path,
    payload: dict[str, Any],
    *,
    budget: _DatasetReadBudget,
    max_file_bytes: int,
    label: str,
) -> None:
    expected = _json_dumps(payload, indent=2).encode("utf-8")
    if not _reconcile_dataset_pending_file(path):
        _write_json_strict_exclusive(files, path, payload)
        if not _reconcile_dataset_pending_file(path):
            raise DatasetIntegrityError(f"{label} publication is missing")
    actual = _bounded_regular_file_bytes(
        path,
        budget=budget,
        max_file_bytes=max_file_bytes,
        label=label,
    )
    if actual != expected:
        raise DatasetIntegrityError(f"{label} differs from its exact authority")


def _ensure_dataset_jsonl_file(
    files: ArtifactFileStore,
    path: Path,
    records: list[dict[str, Any]],
    *,
    budget: _DatasetReadBudget,
    max_file_bytes: int,
    label: str,
) -> None:
    expected = _dataset_records_bytes(records)
    if not _reconcile_dataset_pending_file(path):
        _write_jsonl_strict_exclusive(files, path, records)
        if not _reconcile_dataset_pending_file(path):
            raise DatasetIntegrityError(f"{label} publication is missing")
    actual = _bounded_regular_file_bytes(
        path,
        budget=budget,
        max_file_bytes=max_file_bytes,
        label=label,
    )
    if actual != expected:
        raise DatasetIntegrityError(f"{label} differs from its exact authority")


def _normalize_feedback_payload(
    request: HumanFeedbackCreateRequest,
    *,
    reviewer_role: str | None = None,
) -> dict[str, Any]:
    normalized: dict[str, Any] = {"decision": request.decision}
    if request.score is not None:
        normalized["score"] = request.score
    if request.confidence is not None:
        normalized["confidence"] = request.confidence
    if request.rationale:
        normalized["rationale"] = request.rationale
    if reviewer_role:
        normalized["reviewer_role"] = reviewer_role
    for field in (
        "observed_issues",
        "suggested_changes",
        "risks",
        "validation_checks",
        "labels",
    ):
        value = getattr(request, field)
        if value:
            normalized[field] = value
    return normalized


_HUMAN_FEEDBACK_DATASET_STATUSES = {
    HumanFeedbackStatus.AVAILABLE_FOR_EVOLUTION.value,
}
_HUMAN_FEEDBACK_LIST_FIELDS = (
    "observed_issues",
    "suggested_changes",
    "risks",
    "validation_checks",
    "labels",
)
_LOCAL_ARTIFACT_URI_LABEL = "[LOCAL_ARTIFACT_URI]"
_LOCAL_ARTIFACT_PATH_LABEL = "[LOCAL_ARTIFACT_PATH]"
_REDACTED_LABEL = "[REDACTED]"
_URI_RE = re.compile(
    r"\b[A-Za-z][A-Za-z0-9+.-]*://(?:<redacted>|[^\s\"'<>])+",
    re.IGNORECASE,
)
_RELATIVE_URI_REF_RE = re.compile(
    r"(?<![\w:/])(?:[A-Za-z0-9._~!$&'()*+,;=@%-]+/)*"
    r"[A-Za-z0-9._~!$&'()*+,;=@%-]+[?#](?:<redacted>|[^\s\"'<>])+"
)
_QUERY_OR_FRAGMENT_REF_RE = re.compile(r"(?<![\w])(?:[?#](?:<redacted>|[^\s\"'<>])+)")
_POSIX_ABSOLUTE_PATH_RE = re.compile(r"(?<![\w:/])/(?!/)(?:[^\s,;:/]+/)*[^\s,;]+")
_WINDOWS_UNC_PATH_RE = re.compile(
    r"\\\\[^\s\\/:*?\"<>|,;]+\\(?:[^\\/:*?\"<>|\r\n,;]+\\)*[^\s\\/:*?\"<>|,;]+"
)
_WINDOWS_ABSOLUTE_PATH_RE = re.compile(
    r"\b[A-Za-z]:[\\/](?:[^\\/:*?\"<>|\r\n,;]+[\\/])*[^\s\\/:*?\"<>|,;]+"
)
_SECRET_ASSIGNMENT_RE = re.compile(
    r"\b[A-Za-z0-9_]*(?:"
    r"api[_-]?key|access[_-]?key(?:[_-]?id)?|accesskeyid|token|password|secret|"
    r"authorization"
    r")[A-Za-z0-9_]*\s*[:=]\s*(?:bearer|basic)?\s*[^\s,;]+",
    re.IGNORECASE,
)
_SENSITIVE_KEY_RE = re.compile(
    r"(api[_-]?key|access[_-]?key(?:[_-]?id)?|accesskeyid|token|password|secret|"
    r"authorization)",
    re.IGNORECASE,
)
_AUTHORIZATION_VALUE_RE = re.compile(
    r"\bAuthorization\s*:\s*(?:Bearer|Basic)?\s*[^\s,;]+",
    re.IGNORECASE,
)
_BEARER_VALUE_RE = re.compile(
    r"\bBearer\s+[A-Za-z0-9._~+/=-]+",
    re.IGNORECASE,
)
_SENSITIVE_SCHEME_VALUE_RE = re.compile(
    r"\b(?:bearer|basic)\s*:\s*[^\s,;]+",
    re.IGNORECASE,
)
_AWS_ACCESS_KEY_RE = re.compile(r"\b(?:AKIA|ASIA)[A-Z0-9]{12,}\b")


def _sanitize_review_boundary_payload(value: Any, *, uri_context: bool = False) -> Any:
    if isinstance(value, dict):
        sanitized: dict[str, Any] = {}
        for key, child in value.items():
            text_key = str(key)
            sanitized_key = _sanitize_review_boundary_key(text_key)
            if not sanitized_key:
                continue
            if _SENSITIVE_KEY_RE.search(text_key):
                sanitized[sanitized_key] = _REDACTED_LABEL
                continue
            sanitized[sanitized_key] = _sanitize_review_boundary_payload(
                child,
                uri_context=uri_context or _is_uri_field_key(text_key),
            )
        return sanitized
    if isinstance(value, list):
        return [_sanitize_review_boundary_payload(item, uri_context=uri_context) for item in value]
    if isinstance(value, tuple):
        return [_sanitize_review_boundary_payload(item, uri_context=uri_context) for item in value]
    if isinstance(value, str):
        return _sanitize_review_boundary_text(value, uri_context=uri_context)
    return value


def _sanitize_review_boundary_key(key: str) -> str:
    text = key.strip()
    if not text:
        return ""
    if _looks_like_absolute_local_path(text):
        return _LOCAL_ARTIFACT_PATH_LABEL
    if _looks_like_uri_reference(text):
        return _sanitize_uri_reference(text)
    return _sanitize_review_boundary_text(text)


def _sanitize_review_target_ids(values: Any) -> list[str]:
    if not isinstance(values, list):
        return []
    sanitized: list[str] = []
    for value in values:
        if not isinstance(value, str):
            continue
        text = _sanitize_review_metadata_text(value)
        if text:
            sanitized.append(text)
    return sanitized


def _sanitize_review_artifact_hashes(value: Any) -> dict[str, str]:
    if not isinstance(value, dict):
        return {}
    sanitized: dict[str, str] = {}
    for key, child in value.items():
        if not isinstance(key, str) or not isinstance(child, str):
            continue
        sanitized_key = _sanitize_review_metadata_text(key)
        sanitized_value = _sanitize_review_metadata_text(child)
        if sanitized_key and sanitized_value:
            sanitized[sanitized_key] = sanitized_value
    return sanitized


def _sanitize_review_metadata_text(value: str) -> str:
    text = value.strip()
    if not text:
        return ""
    if _looks_like_absolute_local_path(text):
        return _LOCAL_ARTIFACT_PATH_LABEL
    if _has_sensitive_uri_scheme(text):
        return _REDACTED_LABEL
    if _looks_like_uri_reference(text):
        return _sanitize_uri_reference(text)
    return _sanitize_review_boundary_text(text)


def _has_sensitive_uri_scheme(value: str) -> bool:
    parsed = urlparse(value.strip())
    return _is_sensitive_uri_scheme(parsed.scheme)


def _is_sensitive_uri_scheme(scheme: str) -> bool:
    normalized = scheme.lower()
    if not normalized:
        return False
    return normalized in {"bearer", "basic"} or bool(_SENSITIVE_KEY_RE.search(normalized))


def _is_uri_field_key(key: object) -> bool:
    text = str(key).strip().lower()
    normalized = re.sub(r"[^a-z0-9]+", "_", text)
    parts = [part for part in normalized.split("_") if part]
    if any(part in {"uri", "uris", "url", "urls", "path", "paths"} for part in parts):
        return True
    return text.endswith(("uri", "uris", "url", "urls", "path", "paths"))


def _sanitize_review_boundary_text(value: str, *, uri_context: bool = False) -> str:
    text = value.strip()
    if not text:
        return ""
    if uri_context and _looks_like_absolute_local_path(text):
        return _LOCAL_ARTIFACT_PATH_LABEL
    if uri_context and _looks_like_uri_reference(text):
        return _sanitize_uri_reference(text)
    text = _URI_RE.sub(_sanitize_uri_match, text)
    text = _RELATIVE_URI_REF_RE.sub(_sanitize_relative_uri_match, text)
    text = _QUERY_OR_FRAGMENT_REF_RE.sub("<redacted>", text)
    text = _POSIX_ABSOLUTE_PATH_RE.sub(_redact_posix_path_match, text)
    text = _WINDOWS_UNC_PATH_RE.sub(_LOCAL_ARTIFACT_PATH_LABEL, text)
    text = _WINDOWS_ABSOLUTE_PATH_RE.sub(_LOCAL_ARTIFACT_PATH_LABEL, text)
    text = _AUTHORIZATION_VALUE_RE.sub(_REDACTED_LABEL, text)
    text = _BEARER_VALUE_RE.sub(_REDACTED_LABEL, text)
    text = _SENSITIVE_SCHEME_VALUE_RE.sub(_REDACTED_LABEL, text)
    text = _SECRET_ASSIGNMENT_RE.sub(_REDACTED_LABEL, text)
    text = _AWS_ACCESS_KEY_RE.sub(_REDACTED_LABEL, text)
    return text.strip()


def _sanitize_uri_match(match: re.Match[str]) -> str:
    return _sanitize_uri_reference(match.group(0).rstrip(".,);]"))


def _sanitize_relative_uri_match(match: re.Match[str]) -> str:
    candidate = match.group(0).rstrip(".,);]")
    if _looks_like_uri_reference(candidate):
        return _sanitize_uri_reference(candidate)
    return match.group(0)


def _sanitize_uri_reference(uri: str) -> str:
    parsed = urlparse(uri)
    if _is_sensitive_uri_scheme(parsed.scheme):
        return _REDACTED_LABEL
    if parsed.scheme == "file":
        return _LOCAL_ARTIFACT_URI_LABEL
    host = parsed.hostname or ""
    try:
        port = parsed.port
    except ValueError:
        port = None
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    netloc = f"{host}:{port}" if port is not None else host
    query = "<redacted>" if parsed.query or parsed.fragment else ""
    return urlunparse(
        (
            parsed.scheme,
            netloc,
            parsed.path,
            parsed.params,
            query,
            "",
        )
    )


def _looks_like_uri_reference(value: str) -> bool:
    stripped = value.strip()
    if not stripped or any(char.isspace() for char in stripped):
        return False
    parsed = urlparse(stripped)
    return bool(
        (parsed.scheme and (parsed.netloc or parsed.path))
        or parsed.netloc
        or ((parsed.query or parsed.fragment) and parsed.path)
    )


def _looks_like_absolute_local_path(value: str) -> bool:
    stripped = value.strip()
    if not stripped or any(char in stripped for char in "\r\n\x00"):
        return False
    if _WINDOWS_UNC_PATH_RE.fullmatch(stripped) or _WINDOWS_ABSOLUTE_PATH_RE.fullmatch(stripped):
        return True
    return stripped.startswith("/")


def _redact_human_feedback_text(value: str) -> str:
    return _sanitize_review_boundary_text(value)


def _redact_posix_path_match(match: re.Match[str]) -> str:
    return _LOCAL_ARTIFACT_PATH_LABEL


def _sanitize_human_feedback_for_dataset(value: Any) -> list[dict[str, Any]]:
    if isinstance(value, dict):
        items = [value]
    elif isinstance(value, list):
        items = [item for item in value if isinstance(item, dict)]
    else:
        return []

    sanitized_items: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    for item in items:
        status = item.get("status")
        if not isinstance(status, str) or status not in _HUMAN_FEEDBACK_DATASET_STATUSES:
            continue
        source = item.get("normalized_payload")
        if not isinstance(source, dict):
            source = item
        sanitized: dict[str, Any] = {}
        feedback_id = item.get("feedback_id") or source.get("feedback_id")
        if isinstance(feedback_id, str) and feedback_id.strip():
            sanitized["feedback_id"] = _redact_human_feedback_text(feedback_id)
        sanitized["status"] = HumanFeedbackStatus.AVAILABLE_FOR_EVOLUTION.value
        decision = source.get("decision")
        if isinstance(decision, str) and decision.strip():
            sanitized["decision"] = _redact_human_feedback_text(decision)
        confidence = source.get("confidence")
        if isinstance(confidence, int | float):
            sanitized["confidence"] = float(confidence)
        score = _bounded_human_feedback_score(source.get("score"))
        if score is None and source is not item:
            score = _bounded_human_feedback_score(item.get("score"))
        if score is not None:
            sanitized["score"] = score
        for field in _HUMAN_FEEDBACK_LIST_FIELDS:
            values = _string_list_for_dataset_feedback(source.get(field))
            if values:
                sanitized[field] = values
        if sanitized:
            dedupe_key = str(sanitized.get("feedback_id") or json.dumps(sanitized, sort_keys=True))
            if dedupe_key in seen_ids:
                continue
            seen_ids.add(dedupe_key)
            sanitized_items.append(sanitized)
    return sanitized_items


def _string_list_for_dataset_feedback(value: Any) -> list[str]:
    if isinstance(value, str):
        text = _redact_human_feedback_text(value)
        return [text] if text else []
    if isinstance(value, list | tuple):
        values: list[str] = []
        for item in value:
            if not isinstance(item, str):
                continue
            text = _redact_human_feedback_text(item)
            if text:
                values.append(text)
        return values
    return []


def _string_list(value: Any) -> list[str]:
    if isinstance(value, str):
        text = value.strip()
        return [text] if text else []
    if isinstance(value, list | tuple):
        return [item.strip() for item in value if isinstance(item, str) and item.strip()]
    return []


def _feedback_application_target_type(manifest: dict[str, Any]) -> str:
    value = str(
        manifest.get("human_feedback_application_target_type")
        or manifest.get("feedback_application_target_type")
        or FeedbackApplicationTargetType.PROMPT_SEED.value
    ).strip()
    allowed = {item.value for item in FeedbackApplicationTargetType}
    return value if value in allowed else FeedbackApplicationTargetType.PROMPT_SEED.value


def _feedback_application_effect_summary(
    manifest: dict[str, Any],
    *,
    artifact: ArtifactResponse,
    method: str,
) -> str:
    value = manifest.get("human_feedback_application_summary") or manifest.get(
        "feedback_application_summary"
    )
    if isinstance(value, str) and value.strip():
        return value
    return (
        f"Consumed human feedback while running {method} to produce "
        f"{artifact.type} artifact {artifact.artifact_id}."
    )


def _bounded_human_feedback_score(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    score = float(value)
    if not math.isfinite(score) or score < 0.0 or score > 1.0:
        return None
    return score


def _add_sanitized_human_feedback(
    sanitized: list[dict[str, Any]],
    seen: set[str],
    value: Any,
) -> None:
    for item in _sanitize_human_feedback_for_dataset(value):
        feedback_id = item.get("feedback_id")
        if isinstance(feedback_id, str) and feedback_id:
            existing = next(
                (
                    candidate
                    for candidate in sanitized
                    if candidate.get("feedback_id") == feedback_id
                ),
                None,
            )
            if existing is not None:
                _merge_sanitized_human_feedback(existing, item)
                continue
            seen.add(feedback_id)
            sanitized.append(item)
            continue
        dedupe_key = json.dumps(item, sort_keys=True)
        if dedupe_key in seen:
            continue
        seen.add(dedupe_key)
        sanitized.append(item)


def _merge_sanitized_human_feedback(
    target: dict[str, Any],
    source: dict[str, Any],
) -> None:
    target["status"] = HumanFeedbackStatus.AVAILABLE_FOR_EVOLUTION.value
    for field in ("feedback_id", "decision", "confidence", "score"):
        if field not in target and source.get(field) not in (None, "", []):
            target[field] = source[field]
    for field in _HUMAN_FEEDBACK_LIST_FIELDS:
        values = source.get(field)
        if not isinstance(values, list):
            continue
        target_values = target.setdefault(field, [])
        if not isinstance(target_values, list):
            continue
        for value in values:
            if isinstance(value, str) and value not in target_values:
                target_values.append(value)


def _pop_human_feedback_aliases(
    mapping: dict[str, Any],
    sanitized: list[dict[str, Any]],
    seen: set[str],
    *,
    keys: tuple[str, ...],
) -> None:
    for key in keys:
        if key in mapping:
            _add_sanitized_human_feedback(sanitized, seen, mapping.pop(key))


def _sanitize_evolution_feedback_mapping(
    value: Any,
    sanitized: list[dict[str, Any]],
    seen: set[str],
) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return None
    _pop_human_feedback_aliases(value, sanitized, seen, keys=("human", "human_feedback"))
    return value


def _sanitize_human_feedback_in_event_payload(event_payload: dict[str, Any]) -> None:
    sanitized: list[dict[str, Any]] = []
    seen: set[str] = set()

    _pop_human_feedback_aliases(event_payload, sanitized, seen, keys=("human", "human_feedback"))
    _sanitize_evolution_feedback_mapping(
        event_payload.get("evolution_feedback"),
        sanitized,
        seen,
    )

    session_result = event_payload.get("session_result")
    if isinstance(session_result, dict):
        _pop_human_feedback_aliases(
            session_result,
            sanitized,
            seen,
            keys=("human", "human_feedback"),
        )
        _sanitize_evolution_feedback_mapping(
            session_result.get("evolution_feedback"),
            sanitized,
            seen,
        )
        metadata = session_result.get("metadata")
        if isinstance(metadata, dict):
            _pop_human_feedback_aliases(
                metadata,
                sanitized,
                seen,
                keys=("human", "human_feedback"),
            )
            _sanitize_evolution_feedback_mapping(
                metadata.get("evolution_feedback"),
                sanitized,
                seen,
            )

    if sanitized:
        if not isinstance(session_result, dict):
            session_result = {}
            event_payload["session_result"] = session_result
        metadata = session_result.get("metadata")
        if not isinstance(metadata, dict):
            metadata = {}
            session_result["metadata"] = metadata
        existing_evolution_feedback = metadata.get("evolution_feedback")
        if isinstance(existing_evolution_feedback, dict):
            evolution_feedback = existing_evolution_feedback
        else:
            evolution_feedback = {}
            if _non_empty_evolution_feedback_value(existing_evolution_feedback):
                evolution_feedback["shared"] = existing_evolution_feedback
            metadata["evolution_feedback"] = evolution_feedback
        evolution_feedback["human"] = sanitized
    else:
        if isinstance(session_result, dict):
            metadata = session_result.get("metadata")
            if isinstance(metadata, dict):
                evolution_feedback = metadata.get("evolution_feedback")
                if isinstance(evolution_feedback, dict):
                    evolution_feedback.pop("human", None)


def _non_empty_evolution_feedback_value(value: Any) -> bool:
    if isinstance(value, dict) or value is None:
        return False
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, list | tuple | set):
        return bool(value)
    return True


_REVIEW_STATUSES = {status.value for status in ReviewStatus}
_ADJUDICATION_TRANSITIONS = {
    ReviewStatus.SUBMITTED.value: {
        ReviewStatus.VALIDATED.value,
        ReviewStatus.ADJUDICATED.value,
        ReviewStatus.NEEDS_REVISION.value,
        ReviewStatus.REJECTED_INVALID.value,
    },
    ReviewStatus.VALIDATED.value: {
        ReviewStatus.ADJUDICATED.value,
        ReviewStatus.CONFLICT.value,
    },
    ReviewStatus.ADJUDICATED.value: {ReviewStatus.ARCHIVED_ONLY.value},
}
_RESOLVABLE_REVIEW_STATUSES = {
    ReviewStatus.SUBMITTED.value,
    ReviewStatus.VALIDATED.value,
    ReviewStatus.ADJUDICATED.value,
    ReviewStatus.NEEDS_REVISION.value,
    ReviewStatus.REJECTED_INVALID.value,
    ReviewStatus.CONFLICT.value,
}
_STALEABLE_REVIEW_STATUSES = {
    ReviewStatus.CREATED.value,
    ReviewStatus.QUEUED.value,
    ReviewStatus.SUBMITTED.value,
}
_CONSUMABLE_FEEDBACK_STATUSES = {
    HumanFeedbackStatus.AVAILABLE_FOR_EVOLUTION.value,
    HumanFeedbackStatus.CONSUMED.value,
}
_ACTIVE_FEEDBACK_STATUSES = (
    HumanFeedbackStatus.SUBMITTED.value,
    HumanFeedbackStatus.VALIDATED.value,
    HumanFeedbackStatus.NORMALIZED.value,
    HumanFeedbackStatus.REDACTED.value,
    HumanFeedbackStatus.INDEXED.value,
    HumanFeedbackStatus.AVAILABLE_FOR_EVOLUTION.value,
)


@dataclass(frozen=True, slots=True)
class _ValidatedPlanBoundJobIdentity:
    plan: EvolutionPlan
    selection: ResolvedEvolutionSelection
    envelope: MethodExecutionEnvelope
    input_artifact_ids: tuple[str, ...]
    output_artifact_types: tuple[str, ...]
    successor_transition_id: str | None
    predecessor_successor_transition_id: str | None


@dataclass(frozen=True, slots=True)
class _ValidatedPlanBoundJob:
    plan: EvolutionPlan
    selection: ResolvedEvolutionSelection
    envelope: MethodExecutionEnvelope
    input_artifact_ids: tuple[str, ...]
    input_artifacts: tuple[WorkerClaimInputArtifact, ...]
    output_artifact_types: tuple[str, ...]
    successor_transition_id: str | None
    predecessor_successor_transition_id: str | None


def _store_owned_execution_lineage(
    *,
    job_id: str,
    validated: (
        _ValidatedPlanBoundJob
        | _ValidatedPlanBoundJobIdentity
    ),
) -> dict[str, Any]:
    lineage: dict[str, Any] = {
        "job_id": job_id,
        "plan_id": validated.plan.plan_id,
        "plan_digest": validated.envelope.plan_digest,
        "target_id": validated.selection.target_id,
        "method_id": validated.selection.method_id,
        "method_identity_digest": (
            validated.selection.method_identity_digest
        ),
        "execution_envelope_digest": canonical_digest(
            validated.envelope
        ),
        "registry_snapshot_digest": (
            validated.plan.registry_snapshot_digest
        ),
        "input_bindings": [
            binding.model_dump(mode="json")
            for binding in validated.envelope.input_bindings
        ],
        "declared_output_artifact_types": list(
            validated.output_artifact_types
        ),
    }
    if validated.successor_transition_id is not None:
        lineage["successor_transition_id"] = (
            validated.successor_transition_id
        )
    return lineage


def _require_sealed_transition_artifact_authority(
    artifact: sqlite3.Row,
    *,
    job_id: str,
    validated: (
        _ValidatedPlanBoundJob
        | _ValidatedPlanBoundJobIdentity
    ),
) -> None:
    expected_lineage = _store_owned_execution_lineage(
        job_id=job_id,
        validated=validated,
    )
    try:
        lineage = _internal_job_output_object(
            artifact,
            "lineage_json",
            "lineage",
        )
    except ValueError as exc:
        raise ValueError(
            "sealed transition artifact authority is invalid"
        ) from exc
    execution = lineage.get("openevo_execution")
    if (
        validated.successor_transition_id is None
        or artifact["state"] != str(ArtifactState.SEALED)
        or artifact["staging_job_id"] != job_id
        or artifact["type"]
        not in validated.output_artifact_types
        or not isinstance(execution, dict)
        or execution != expected_lineage
        or _json_dumps(lineage) != artifact["lineage_json"]
    ):
        raise ValueError(
            "sealed transition artifact authority is invalid"
        )


def _require_review_transition(
    row: sqlite3.Row,
    *,
    review_id: str,
    action: str,
    allowed_statuses: set[str],
) -> str:
    status = str(row["status"])
    if status not in allowed_statuses:
        raise ValueError(f"cannot {action} review {review_id} from status {status}")
    return status


def _archive_active_feedback(
    conn: sqlite3.Connection,
    *,
    review_id: str,
    status: HumanFeedbackStatus,
) -> None:
    conn.execute(
        """
        UPDATE human_feedback
        SET status = ?
        WHERE review_id = ?
          AND status IN (?, ?, ?, ?, ?, ?)
        """,
        (status.value, review_id, *_ACTIVE_FEEDBACK_STATUSES),
    )


def _insert_human_query_decision_row(
    conn: sqlite3.Connection,
    *,
    query_decision_id: str,
    request_payload: dict[str, Any],
    review_id: str | None,
    created_at: str,
) -> None:
    conn.execute(
        """
        INSERT INTO human_query_decisions (
            query_decision_id, artifact_ids_json, candidate_ids_json,
            task_id, round_index, method, decision, reason_codes_json,
            estimated_value_of_information, estimated_human_cost,
            budget_context_json, actual_latency_seconds,
            feedback_changed_promotion, feedback_changed_next_candidate,
            downstream_delta, review_id, created_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            query_decision_id,
            _json_dumps(request_payload["artifact_ids"]),
            _json_dumps(request_payload["candidate_ids"]),
            request_payload["task_id"],
            request_payload["round_index"],
            request_payload["method"],
            request_payload["decision"],
            _json_dumps(request_payload["reason_codes"]),
            request_payload["estimated_value_of_information"],
            request_payload["estimated_human_cost"],
            _json_dumps(request_payload["budget_context"]),
            None,
            None,
            None,
            None,
            review_id,
            created_at,
        ),
    )


class EvolutionStore:
    def __init__(
        self,
        *,
        db_path: str | Path,
        artifact_root: str | Path,
        registry_snapshot: RegistrySnapshot | None = None,
        executable_registry: VerifiedExecutableRegistry | None = None,
    ) -> None:
        verified_registry = (
            None
            if executable_registry is None
            else require_verified_executable_registry(executable_registry)
        )
        if verified_registry is not None:
            if (
                registry_snapshot is not None
                and registry_snapshot.registry_digest != verified_registry.snapshot.registry_digest
            ):
                raise ValueError("registry snapshot does not match executable registry")
            registry_snapshot = verified_registry.snapshot
        self.db_path = Path(db_path)
        self.files = ArtifactFileStore(artifact_root)
        self._dataset_materialization_root = self.files.root / "datasets"
        self._dataset_materialization_lock = get_materialization_root_lock(
            self._dataset_materialization_root
        )
        self._dataset_recovery_accounting_migration = False
        self._context_materialization_root = self.files.root / "context_materializations"
        self._context_materialization_lock = get_materialization_root_lock(
            self._context_materialization_root
        )
        self._bound_store_id: str | None = None
        self._artifact_root_binding_identity: tuple[int, int, int, int, int] | None = None
        self._materialization_root_binding_identity: tuple[int, int, int, int, int] | None = None
        self._dataset_root_binding_identity: tuple[int, int, int, int, int] | None = None
        self._registry_snapshot = registry_snapshot
        self._executable_registry = verified_registry
        self._context_projection_resolver = (
            None
            if verified_registry is None
            else ContextProjectionResolver(self.files.root, verified_registry)
        )
        self._context_materializer = (
            None
            if verified_registry is None
            else ContextMaterializer(
                self.files.root,
                self._context_materialization_root,
                verified_registry,
            )
        )

    def _bind_registry_snapshot(self, snapshot: RegistrySnapshot) -> None:
        if self._registry_snapshot is None:
            self._registry_snapshot = snapshot
            return
        if self._registry_snapshot.registry_digest != snapshot.registry_digest:
            raise ValueError("store is already bound to a different registry snapshot")

    def initialize(self) -> None:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.files.initialize()
        orphan_manifest_paths: list[Path] = []
        orphan_materializations: list[_OrphanMaterialization] = []
        with (
            self._locked_context_materialization_root() as materialization_root_fd,
            self._locked_dataset_materialization_root() as dataset_root_fd,
        ):
            with self.connect() as conn:
                store_id, artifact_root_identity = self._ensure_store_identity(
                    conn,
                    materialization_root_descriptor=materialization_root_fd,
                )
                self._bound_store_id = store_id
                self._artifact_root_binding_identity = artifact_root_identity
                self._materialization_root_binding_identity = _directory_binding_identity(
                    os.fstat(materialization_root_fd)
                )
                self._dataset_root_binding_identity = (
                    _directory_binding_identity(
                        os.fstat(dataset_root_fd)
                    )
                )
                self._verify_bound_materialization_root(materialization_root_fd)
                self._verify_bound_dataset_root(dataset_root_fd)
                conn.execute("PRAGMA journal_mode=WAL")
                conn.execute("BEGIN IMMEDIATE")
                try:
                    self._verify_bound_store_identity(conn)
                    self._install_base_schema(conn)
                    self._ensure_schema(conn)
                    self._verify_plan_bound_job_retry_requests(conn)
                    self._verify_plan_bound_job_transition_bindings(conn)
                    dataset_recovery_budget = _DatasetReadBudget(
                        label="dataset startup recovery",
                        max_files=MAX_DATASET_READ_FILES,
                        max_bytes=MAX_DATASET_STARTUP_RECOVERY_BYTES,
                    )
                    self._verify_dataset_create_requests(
                        conn,
                        recovery_budget=dataset_recovery_budget,
                    )
                    self._verify_unjournaled_datasets(
                        conn,
                        recovery_budget=dataset_recovery_budget,
                    )
                    self._migrate_legacy_dataset_file_modes(
                        conn,
                        dataset_root_fd,
                    )
                    self._verify_bound_dataset_root(dataset_root_fd)
                    self._verify_bound_store_identity(conn)
                    expected_context_snapshots = self._expected_context_snapshot_bytes(conn)
                    with self._opened_bound_artifact_root() as artifact_root_fd:
                        migrate_legacy_context_snapshot_modes(artifact_root_fd)
                        reconcile_context_snapshots(
                            artifact_root_fd,
                            expected_context_snapshots,
                        )
                    self._requeue_expired_jobs(conn, datetime.now(UTC))
                    self._delete_recoverable_staged_artifacts(conn)
                    orphan_manifest_paths = self._orphan_managed_artifact_manifests(
                        conn,
                    )
                    orphan_materializations = self._orphan_context_materializations(
                        conn,
                        materialization_root_fd,
                    )
                    materialized_contexts = self._verify_referenced_context_materializations(
                        conn,
                        materialization_root_fd,
                    )
                    self._verify_successor_transition_discards(
                        conn,
                        materialized_contexts,
                    )
                    execution_snapshots = self._verify_execution_snapshots(conn)
                    self._verify_revision_ledger(
                        conn,
                        materialized_contexts,
                        execution_snapshots,
                    )
                    self._verify_bound_store_identity(conn)
                    conn.commit()
                except BaseException:
                    try:
                        conn.rollback()
                    except sqlite3.Error:
                        pass
                    raise
            self._remove_orphan_context_materializations(
                orphan_materializations,
                materialization_root_fd,
            )
        self._unlink_artifact_manifests(orphan_manifest_paths)

    @staticmethod
    def _install_base_schema(conn: sqlite3.Connection) -> None:
        pending: list[str] = []
        for line in SCHEMA.splitlines(keepends=True):
            pending.append(line)
            statement = "".join(pending)
            if not sqlite3.complete_statement(statement):
                continue
            if statement.strip():
                conn.execute(statement)
            pending.clear()
        if "".join(pending).strip():
            raise RuntimeError("base schema contains an incomplete SQL statement")

    @contextmanager
    def _locked_context_materialization_root(self) -> Iterator[int]:
        with ExitStack() as stack:
            try:
                descriptor = stack.enter_context(self._context_materialization_lock.locked())
            except OSError as exc:
                raise ValueError(
                    "context materialization root could not be opened safely"
                ) from exc
            if self._bound_store_id is not None:
                self._verify_bound_materialization_root(descriptor)
            yield descriptor
            if self._bound_store_id is not None:
                self._verify_bound_materialization_root(descriptor)

    @contextmanager
    def _locked_dataset_materialization_root(self) -> Iterator[int]:
        try:
            with self._dataset_materialization_lock.locked() as descriptor:
                opened = os.fstat(descriptor)
                if (
                    not stat.S_ISDIR(opened.st_mode)
                    or opened.st_uid != os.geteuid()
                ):
                    raise DatasetIntegrityError(
                        "dataset materialization root must be an owned directory"
                    )
                if self._bound_store_id is not None:
                    self._verify_bound_dataset_root(descriptor)
                yield descriptor
                if self._bound_store_id is not None:
                    self._verify_bound_dataset_root(descriptor)
        except OSError as exc:
            raise DatasetIntegrityError(
                "dataset materialization root could not be opened safely"
            ) from exc

    def _migrate_legacy_dataset_file_modes(
        self,
        conn: sqlite3.Connection,
        dataset_root_descriptor: int,
    ) -> None:
        max_rows = MAX_DATASET_CREATE_REQUESTS * 2
        rows = conn.execute(
            "SELECT dataset_id, manifest_path FROM datasets "
            "WHERE artifact_id IS NOT NULL "
            "ORDER BY dataset_id LIMIT ?",
            (max_rows + 1,),
        ).fetchall()
        if len(rows) > max_rows:
            raise DatasetIntegrityError(
                "sealed dataset mode migration exceeds its recovery bound"
            )
        for row in rows:
            dataset_id = str(row["dataset_id"])
            if (
                re.fullmatch(r"[A-Za-z][A-Za-z0-9_.-]{0,127}", dataset_id)
                is None
            ):
                raise DatasetIntegrityError(
                    "sealed dataset mode migration has an invalid dataset ID"
                )
            expected_manifest_path = self.files.dataset_manifest_path(
                dataset_id
            )
            if row["manifest_path"] != str(expected_manifest_path):
                raise DatasetIntegrityError(
                    f"sealed dataset path is inconsistent: {dataset_id}"
                )
            directory_label = f"legacy dataset directory {dataset_id!r}"
            path_entry = _entry_stat(
                dataset_root_descriptor,
                dataset_id,
            )
            if path_entry is None:
                raise DatasetIntegrityError(
                    f"{directory_label} is missing"
                )
            permissions = stat.S_IMODE(path_entry.st_mode)
            if (
                not stat.S_ISDIR(path_entry.st_mode)
                or path_entry.st_uid != os.geteuid()
                or permissions & 0o7000
                or permissions & 0o022
                or permissions & 0o700 != 0o700
            ):
                raise DatasetIntegrityError(
                    f"{directory_label} is not an owned safe directory"
                )
            try:
                directory_descriptor = os.open(
                    dataset_id,
                    os.O_RDONLY
                    | os.O_CLOEXEC
                    | os.O_NOFOLLOW
                    | os.O_NONBLOCK
                    | os.O_DIRECTORY,
                    dir_fd=dataset_root_descriptor,
                )
            except OSError as exc:
                raise DatasetIntegrityError(
                    f"{directory_label} could not be opened safely"
                ) from exc
            try:
                opened = os.fstat(directory_descriptor)
                if (
                    _dataset_file_identity(opened)
                    != _dataset_file_identity(path_entry)
                ):
                    raise DatasetIntegrityError(
                        f"{directory_label} changed before it was opened"
                    )
                _require_dataset_file_path_identity(
                    dataset_root_descriptor,
                    dataset_id,
                    opened,
                    label=directory_label,
                )
                _migrate_legacy_dataset_file_mode(
                    directory_descriptor,
                    dataset_id=dataset_id,
                    name=expected_manifest_path.name,
                    max_file_bytes=MAX_DATASET_MANIFEST_BYTES,
                )
                _migrate_legacy_dataset_file_mode(
                    directory_descriptor,
                    dataset_id=dataset_id,
                    name="records.jsonl",
                    max_file_bytes=MAX_DATASET_RECORDS_BYTES,
                )
                os.fsync(directory_descriptor)
                after = os.fstat(directory_descriptor)
                if (
                    _dataset_file_identity(after)
                    != _dataset_file_identity(opened)
                ):
                    raise DatasetIntegrityError(
                        f"{directory_label} changed during migration"
                    )
                _require_dataset_file_path_identity(
                    dataset_root_descriptor,
                    dataset_id,
                    after,
                    label=directory_label,
                )
            finally:
                os.close(directory_descriptor)
        os.fsync(dataset_root_descriptor)

    def _ensure_store_identity(
        self,
        conn: sqlite3.Connection,
        *,
        materialization_root_descriptor: int,
    ) -> tuple[str, tuple[int, int, int, int, int]]:
        root_descriptor = os.open(
            self.files.root,
            os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_DIRECTORY,
        )
        try:
            root_marker_store_id = self._read_store_identity_marker(root_descriptor)
            materialization_marker_store_id = self._read_store_identity_marker(
                materialization_root_descriptor
            )
            marker_store_ids = {
                value
                for value in (
                    root_marker_store_id,
                    materialization_marker_store_id,
                )
                if value is not None
            }
            created_identity = False
            try:
                conn.execute("BEGIN IMMEDIATE")
                schema = classify_store_schema(conn)
                if schema.kind == "invalid":
                    raise ValueError("evolution database schema identity is not recognized")
                table_exists = schema.identity == "exact"
                if not table_exists:
                    if schema.kind not in {"empty", "legacy", "current"}:
                        raise ValueError("evolution database schema identity is not recognized")
                    if marker_store_ids:
                        raise ValueError(
                            "artifact root is already bound to a different evolution database"
                        )
                    has_unclaimed_managed_state = self._has_unclaimed_managed_state(
                        root_descriptor,
                        materialization_root_descriptor,
                    )
                    if has_unclaimed_managed_state and (
                        schema.kind == "empty"
                        or not schema.underlying.can_claim_managed_recovery_state
                    ):
                        raise ValueError(
                            "fresh evolution database cannot claim non-empty managed state"
                        )
                    database_store_id = new_id("store")
                    if _STORE_ID_RE.fullmatch(database_store_id) is None:
                        raise RuntimeError("generated evolution store identity is invalid")
                    conn.execute(_STORE_IDENTITY_SCHEMA)
                    _after_store_identity_schema_created(conn)
                    conn.execute(
                        "INSERT INTO store_identity "
                        "(singleton, store_id, artifact_root, binding_state) "
                        "VALUES (1, ?, ?, 'pending')",
                        (database_store_id, os.fspath(self.files.root)),
                    )
                    created_identity = True
                else:
                    if schema.kind not in {
                        "identity_only",
                        "legacy_identity_crash_window",
                        "current_identity",
                    }:
                        raise ValueError("evolution database schema identity is not recognized")
                    has_unclaimed_managed_state = self._has_unclaimed_managed_state(
                        root_descriptor,
                        materialization_root_descriptor,
                    )
                    if has_unclaimed_managed_state and (
                        schema.kind == "identity_only"
                        or not schema.underlying.can_claim_managed_recovery_state
                    ):
                        raise ValueError(
                            "unverified evolution database cannot claim non-empty managed state"
                        )
                row = _read_guarded_store_identity_row(conn)
                conn.commit()
            except BaseException:
                try:
                    conn.rollback()
                except sqlite3.Error:
                    pass
                raise
            if row["singleton"] != 1:
                raise ValueError("evolution database store identity is invalid")
            database_store_id = str(row["store_id"])
            if database_store_id is not None and _STORE_ID_RE.fullmatch(database_store_id) is None:
                raise ValueError("evolution database store identity is invalid")
            if str(row["artifact_root"]) != os.fspath(self.files.root):
                raise ValueError("evolution database is bound to a different artifact root")
            binding_state = str(row["binding_state"])
            if binding_state not in {None, "pending", "bound"}:
                raise ValueError("evolution database store identity is invalid")

            if created_identity and binding_state != "pending":
                raise ValueError("evolution database store identity is invalid")

            if binding_state == "bound":
                if root_marker_store_id is None or materialization_marker_store_id is None:
                    raise ValueError("bound artifact root identity marker is missing")
                if marker_store_ids != {database_store_id}:
                    raise ValueError("artifact root is bound to a different evolution database")
                return database_store_id, _directory_binding_identity(os.fstat(root_descriptor))

            if root_marker_store_id is None:
                self._write_store_identity_marker(
                    root_descriptor,
                    database_store_id,
                )
            elif root_marker_store_id != database_store_id:
                raise ValueError("artifact root is bound to a different evolution database")
            if materialization_marker_store_id is None:
                self._write_store_identity_marker(
                    materialization_root_descriptor,
                    database_store_id,
                )
            elif materialization_marker_store_id != database_store_id:
                raise ValueError("artifact root is bound to a different evolution database")
            try:
                conn.execute("BEGIN IMMEDIATE")
                updated = conn.execute(
                    "UPDATE store_identity SET binding_state = 'bound' "
                    "WHERE singleton = 1 AND store_id = ? AND binding_state = 'pending'",
                    (database_store_id,),
                )
                if updated.rowcount != 1:
                    raise ValueError("evolution database store identity changed during binding")
                conn.commit()
            except BaseException:
                try:
                    conn.rollback()
                except sqlite3.Error:
                    pass
                raise
            return database_store_id, _directory_binding_identity(os.fstat(root_descriptor))
        finally:
            os.close(root_descriptor)

    def _has_unclaimed_managed_state(
        self,
        artifact_root_descriptor: int,
        materialization_root_descriptor: int,
    ) -> bool:
        contexts_descriptor: int | None = None
        try:
            contexts_descriptor = os.open(
                "contexts",
                os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_DIRECTORY,
                dir_fd=artifact_root_descriptor,
            )
            with os.scandir(contexts_descriptor) as entries:
                if any(True for _entry in entries):
                    return True
            with os.scandir(materialization_root_descriptor) as entries:
                if any(entry.name != _STORE_IDENTITY_FILENAME for entry in entries):
                    return True
        except OSError as exc:
            raise ValueError(
                "evolution managed recovery state could not be enumerated safely"
            ) from exc
        finally:
            if contexts_descriptor is not None:
                os.close(contexts_descriptor)
        return bool(self._managed_artifact_manifests())

    @contextmanager
    def _opened_bound_artifact_root(self) -> Iterator[int]:
        try:
            descriptor = os.open(
                self.files.root,
                os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_DIRECTORY,
            )
        except OSError as exc:
            raise ValueError("evolution store artifact root could not be verified") from exc
        try:
            self._verify_bound_artifact_root_descriptor(descriptor)
            yield descriptor
            self._verify_bound_artifact_root_descriptor(descriptor)
        finally:
            os.close(descriptor)

    def _verify_bound_artifact_root_descriptor(self, descriptor: int) -> None:
        store_id = self._bound_store_id
        expected = self._artifact_root_binding_identity
        if store_id is None or expected is None:
            raise ValueError("evolution store identity has not been initialized")
        if _directory_binding_identity(os.fstat(descriptor)) != expected:
            raise ValueError("evolution store artifact root identity changed")
        if self._read_store_identity_marker(descriptor) != store_id:
            raise ValueError("evolution store artifact root identity marker changed")

    def _verify_bound_materialization_root(self, descriptor: int) -> None:
        store_id = self._bound_store_id
        expected_artifact_root = self._artifact_root_binding_identity
        expected_materialization_root = self._materialization_root_binding_identity
        if (
            store_id is None
            or expected_artifact_root is None
            or expected_materialization_root is None
        ):
            raise ValueError("evolution store identity has not been initialized")
        artifact_root_descriptor: int | None = None
        try:
            artifact_root_descriptor = os.open(
                self.files.root,
                os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_DIRECTORY,
            )
            self._verify_bound_artifact_root_descriptor(artifact_root_descriptor)
            if _directory_binding_identity(os.fstat(descriptor)) != expected_materialization_root:
                raise ValueError("evolution store root identity changed")
            child = os.stat(
                "context_materializations",
                dir_fd=artifact_root_descriptor,
                follow_symlinks=False,
            )
            if _directory_binding_identity(child) != expected_materialization_root:
                raise ValueError("context materialization root binding changed")
            opened = os.fstat(descriptor)
            if (
                opened.st_uid != os.geteuid()
                or not stat.S_ISDIR(opened.st_mode)
                or stat.S_IMODE(opened.st_mode) != 0o700
            ):
                raise ValueError("context materialization root is not private")
            if self._read_store_identity_marker(descriptor) != store_id:
                raise ValueError("evolution store root identity marker changed")
        except OSError as exc:
            raise ValueError("evolution store root binding could not be verified") from exc
        finally:
            if artifact_root_descriptor is not None:
                os.close(artifact_root_descriptor)

    def _verify_bound_dataset_root(self, descriptor: int) -> None:
        expected_artifact_root = self._artifact_root_binding_identity
        expected_dataset_root = self._dataset_root_binding_identity
        if (
            self._bound_store_id is None
            or expected_artifact_root is None
            or expected_dataset_root is None
        ):
            raise DatasetIntegrityError(
                "evolution store identity has not been initialized"
            )
        artifact_root_descriptor: int | None = None
        try:
            artifact_root_descriptor = os.open(
                self.files.root,
                os.O_RDONLY
                | os.O_CLOEXEC
                | os.O_NOFOLLOW
                | os.O_DIRECTORY,
            )
            self._verify_bound_artifact_root_descriptor(
                artifact_root_descriptor
            )
            opened = os.fstat(descriptor)
            child = os.stat(
                "datasets",
                dir_fd=artifact_root_descriptor,
                follow_symlinks=False,
            )
            if (
                _directory_binding_identity(opened)
                != expected_dataset_root
                or _directory_binding_identity(child)
                != expected_dataset_root
            ):
                raise DatasetIntegrityError(
                    "dataset materialization root binding changed"
                )
            permissions = stat.S_IMODE(opened.st_mode)
            if (
                not stat.S_ISDIR(opened.st_mode)
                or opened.st_uid != os.geteuid()
                or permissions & 0o7000
                or permissions & 0o022
                or permissions & 0o700 != 0o700
            ):
                raise DatasetIntegrityError(
                    "dataset materialization root is not an owned safe directory"
                )
        except OSError as exc:
            raise DatasetIntegrityError(
                "dataset materialization root binding could not be verified"
            ) from exc
        finally:
            if artifact_root_descriptor is not None:
                os.close(artifact_root_descriptor)

    def _verify_bound_store_identity(self, conn: sqlite3.Connection) -> None:
        store_id = self._bound_store_id
        if store_id is None:
            raise ValueError("evolution store identity has not been initialized")
        row = _read_guarded_store_identity_row(conn)
        if (
            row["singleton"] != 1
            or str(row["store_id"]) != store_id
            or str(row["artifact_root"]) != os.fspath(self.files.root)
            or str(row["binding_state"]) != "bound"
        ):
            raise ValueError("evolution database store identity changed")

    @staticmethod
    def _read_store_identity_marker(root_descriptor: int) -> str | None:
        try:
            descriptor = os.open(
                _STORE_IDENTITY_FILENAME,
                os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW,
                dir_fd=root_descriptor,
            )
        except FileNotFoundError:
            return None
        except OSError as exc:
            raise ValueError("artifact root store identity could not be opened safely") from exc
        try:
            opened = os.fstat(descriptor)
            if (
                not stat.S_ISREG(opened.st_mode)
                or opened.st_nlink != 1
                or opened.st_size <= 0
                or opened.st_size > _STORE_IDENTITY_MAX_BYTES
            ):
                raise ValueError("artifact root store identity is invalid")
            chunks: list[bytes] = []
            size = 0
            while chunk := os.read(descriptor, 1024):
                size += len(chunk)
                if size > _STORE_IDENTITY_MAX_BYTES:
                    raise ValueError("artifact root store identity is invalid")
                chunks.append(chunk)
            after = os.fstat(descriptor)
            try:
                path_after = os.stat(
                    _STORE_IDENTITY_FILENAME,
                    dir_fd=root_descriptor,
                    follow_symlinks=False,
                )
            except OSError as exc:
                raise ValueError("artifact root store identity changed while being read") from exc
        finally:
            os.close(descriptor)
        if (
            size != opened.st_size
            or _stat_identity(opened) != _stat_identity(after)
            or _stat_identity(after) != _stat_identity(path_after)
        ):
            raise ValueError("artifact root store identity changed while being read")
        try:
            payload = json.loads(b"".join(chunks))
        except (UnicodeDecodeError, ValueError) as exc:
            raise ValueError("artifact root store identity is invalid") from exc
        if (
            not isinstance(payload, dict)
            or set(payload) != {"contract_version", "store_id"}
            or payload.get("contract_version") != "1"
            or not isinstance(payload.get("store_id"), str)
            or _STORE_ID_RE.fullmatch(payload["store_id"]) is None
        ):
            raise ValueError("artifact root store identity is invalid")
        return payload["store_id"]

    @staticmethod
    def _write_store_identity_marker(root_descriptor: int, store_id: str) -> None:
        if _STORE_ID_RE.fullmatch(store_id) is None:
            raise ValueError("evolution database store identity is invalid")
        encoded = _json_dumps({"contract_version": "1", "store_id": store_id}).encode("utf-8")
        descriptor: int | None = None
        created = False
        try:
            descriptor = os.open(
                _STORE_IDENTITY_FILENAME,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | os.O_NOFOLLOW,
                0o600,
                dir_fd=root_descriptor,
            )
            created = True
            view = memoryview(encoded)
            while view:
                written = os.write(descriptor, view)
                if written <= 0:
                    raise OSError("store identity write made no progress")
                view = view[written:]
            os.fsync(descriptor)
            opened = os.fstat(descriptor)
            if (
                not stat.S_ISREG(opened.st_mode)
                or opened.st_nlink != 1
                or opened.st_size != len(encoded)
            ):
                raise ValueError("artifact root store identity was not written safely")
            try:
                path_opened = os.stat(
                    _STORE_IDENTITY_FILENAME,
                    dir_fd=root_descriptor,
                    follow_symlinks=False,
                )
            except OSError as exc:
                raise ValueError(
                    "artifact root store identity path changed while being written"
                ) from exc
            if _stat_identity(opened) != _stat_identity(path_opened):
                raise ValueError("artifact root store identity path changed while being written")
            os.fsync(root_descriptor)
        except BaseException:
            if created:
                try:
                    fixed = os.fstat(descriptor) if descriptor is not None else None
                    current = os.stat(
                        _STORE_IDENTITY_FILENAME,
                        dir_fd=root_descriptor,
                        follow_symlinks=False,
                    )
                    if fixed is not None and (
                        fixed.st_dev,
                        fixed.st_ino,
                    ) == (
                        current.st_dev,
                        current.st_ino,
                    ):
                        os.unlink(_STORE_IDENTITY_FILENAME, dir_fd=root_descriptor)
                        os.fsync(root_descriptor)
                except OSError:
                    pass
            raise
        finally:
            if descriptor is not None:
                os.close(descriptor)

    def _orphan_context_materializations(
        self,
        conn: sqlite3.Connection,
        root_descriptor: int,
    ) -> list[_OrphanMaterialization]:
        context_id_spec = _GuardedTextSpec(
            column="context_id",
            output="context_id",
            max_bytes=256,
        )
        plans = _scan_guarded_row_plans(
            conn,
            from_sql="context_materializations",
            key_spec=context_id_spec,
            text_specs=(context_id_spec,),
            order_by_sql="context_id",
            budget=_RecoveryBudget(
                label="context materialization reference recovery",
                max_rows=MAX_CONTEXT_MATERIALIZATION_RECOVERY_ROWS,
                max_bytes=MAX_CONTEXT_MATERIALIZATION_RECOVERY_BYTES,
                max_row_bytes=256,
            ),
        )
        referenced = {plan.key for plan in plans}
        try:
            with os.scandir(root_descriptor) as entries:
                result: list[_OrphanMaterialization] = []
                for entry in entries:
                    if entry.name == _STORE_IDENTITY_FILENAME:
                        continue
                    if entry.name.startswith(_PRESERVED_ENTRY_PREFIX):
                        raise ValueError("preserved materialization requires manual recovery")
                    if entry.name in referenced:
                        continue
                    result.append(
                        _OrphanMaterialization(
                            name=entry.name,
                            identity=_private_file_identity(entry.stat(follow_symlinks=False)),
                        )
                    )
                return sorted(result, key=lambda candidate: candidate.name)
        except OSError as exc:
            raise ValueError(
                "context materialization root could not be enumerated safely"
            ) from exc

    def _verify_referenced_context_materializations(
        self,
        conn: sqlite3.Connection,
        root_descriptor: int,
    ) -> dict[str, _MaterializationRecoveryIdentity]:
        from_sql = "context_materializations JOIN contexts USING (context_id)"
        specs = (
            _GuardedTextSpec(
                "context_materializations.context_id",
                "context_id",
                256,
            ),
            _GuardedTextSpec(
                "context_materializations.registry_digest",
                "registry_digest",
                64,
            ),
            _GuardedTextSpec(
                "context_materializations.request_digest",
                "request_digest",
                64,
            ),
            _GuardedTextSpec(
                "context_materializations.manifest_json",
                "manifest_json",
                MAX_CONTEXT_MATERIALIZATION_ROW_BYTES,
            ),
            _GuardedTextSpec(
                "contexts.request_json",
                "request_json",
                MAX_CONTEXT_MATERIALIZATION_ROW_BYTES,
            ),
            _GuardedTextSpec(
                "contexts.response_json",
                "response_json",
                MAX_CONTEXT_MATERIALIZATION_ROW_BYTES,
            ),
        )
        materializer = self._context_materializer
        if (
            materializer is None
            and conn.execute("SELECT 1 FROM context_materializations LIMIT 1").fetchone()
            is not None
        ):
            raise ValueError("persisted materializations require a verified executable registry")
        verified: dict[str, _MaterializationRecoveryIdentity] = {}
        budget = _RecoveryBudget(
            label="context materialization recovery",
            max_rows=MAX_CONTEXT_MATERIALIZATION_RECOVERY_ROWS,
            max_bytes=MAX_CONTEXT_MATERIALIZATION_RECOVERY_BYTES,
            max_row_bytes=MAX_CONTEXT_MATERIALIZATION_ROW_BYTES,
        )
        plans = _scan_guarded_row_plans(
            conn,
            from_sql=from_sql,
            key_spec=specs[0],
            text_specs=specs,
            order_by_sql="context_materializations.context_id",
            budget=budget,
        )
        for plan in plans:
            row = _fetch_guarded_row(
                conn,
                from_sql=from_sql,
                where_sql="context_materializations.context_id = ?",
                key=plan.key,
                text_specs=specs,
                lengths=plan.lengths,
                label="context materialization recovery",
            )
            manifest = self._materialized_context_from_row(row)
            try:
                request = ContextProjectionResolveRequest.model_validate_json(
                    str(row["request_json"])
                )
                response = MaterializedContext.model_validate_json(str(row["response_json"]))
            except (TypeError, ValueError) as exc:
                raise ValueError("persisted materialized context snapshot is invalid") from exc
            if canonical_digest(request) != manifest.request_digest or response != manifest:
                raise ValueError("persisted materialized context snapshot is inconsistent")
            if materializer is None:
                raise ValueError(
                    "persisted materializations require a verified executable registry"
                )
            materializer.verify_persisted_materialization(
                manifest,
                materialization_root_descriptor=root_descriptor,
            )
            verified[manifest.context_id] = _MaterializationRecoveryIdentity(
                context_id=manifest.context_id,
                manifest_digest=canonical_digest(manifest),
                registry_digest=manifest.registry_digest,
                request_digest=manifest.request_digest,
                base_model=request.base_model,
                execution_mode=str(request.execution_profile.execution_mode),
                capture_mode=str(request.execution_profile.capture_mode),
                artifact_ids=manifest.selection.artifact_ids,
                adapter_set_digest=canonical_digest(manifest.adapter_merge_spec.adapters),
                successor_transition_id=(
                    manifest.successor_transition_id
                ),
            )
        return verified

    def _verify_execution_snapshots(
        self,
        conn: sqlite3.Connection,
    ) -> dict[str, _ExecutionSnapshotRecoveryIdentity]:
        verified: dict[str, _ExecutionSnapshotRecoveryIdentity] = {}
        budget = _RecoveryBudget(
            label="execution snapshot recovery",
            max_rows=MAX_EXECUTION_SNAPSHOT_RECOVERY_ROWS,
            max_bytes=MAX_EXECUTION_SNAPSHOT_RECOVERY_BYTES,
            max_row_bytes=MAX_EXECUTION_SNAPSHOT_ROW_BYTES,
        )
        specs = (
            _GuardedTextSpec(
                "execution_snapshot_id",
                "execution_snapshot_id",
                128,
            ),
            _GuardedTextSpec("created_at", "created_at", 64),
            _GuardedTextSpec("snapshot_digest", "snapshot_digest", 64),
            _GuardedTextSpec("producer_id", "producer_id", 128),
            _GuardedTextSpec(
                "snapshot_json",
                "snapshot_json",
                MAX_EXECUTION_SNAPSHOT_BYTES,
            ),
        )
        plans = _scan_guarded_row_plans(
            conn,
            from_sql="execution_snapshots",
            key_spec=specs[0],
            text_specs=specs,
            order_by_sql="execution_snapshot_id",
            budget=budget,
        )
        for plan in plans:
            row = _fetch_guarded_row(
                conn,
                from_sql="execution_snapshots",
                where_sql="execution_snapshot_id = ?",
                key=plan.key,
                text_specs=specs,
                lengths=plan.lengths,
                label="execution snapshot recovery",
            )
            record = self._execution_snapshot_record_from_row(row)
            verified[record.execution_snapshot_id] = _ExecutionSnapshotRecoveryIdentity(
                snapshot_digest=record.snapshot_digest,
                producer_id=record.producer_id,
                model_id=record.snapshot.model.model_id,
                execution_mode=str(record.snapshot.execution_mode),
                capture_mode=str(record.snapshot.capture_mode),
            )
        return verified

    @staticmethod
    def _validate_recovered_admission_sources(
        request: TaskAdmissionRequest,
        *,
        materialized_contexts: dict[str, _MaterializationRecoveryIdentity],
        execution_snapshots: dict[str, _ExecutionSnapshotRecoveryIdentity],
    ) -> None:
        snapshot = execution_snapshots.get(request.execution_snapshot_id)
        materialization = materialized_contexts.get(request.context_id)
        if (
            request.project_id != request.stream_id
            or snapshot is None
            or materialization is None
            or snapshot.execution_mode != str(request.execution_mode)
            or snapshot.capture_mode != str(request.capture_mode)
            or materialization.execution_mode != str(request.execution_mode)
            or materialization.capture_mode != str(request.capture_mode)
            or materialization.base_model != snapshot.model_id
            or materialization.artifact_ids != request.context_artifact_ids
            or canonical_digest(materialization.artifact_ids)
            != request.context_artifact_set_digest
        ):
            raise RevisionIntegrityError("task admission envelope sources are inconsistent")

    def _verify_revision_ledger(
        self,
        conn: sqlite3.Connection,
        materialized_contexts: dict[str, _MaterializationRecoveryIdentity],
        execution_snapshots: dict[str, _ExecutionSnapshotRecoveryIdentity],
    ) -> None:
        revisions: dict[str, _RevisionRecoveryIdentity] = {}
        revisions_by_stream: dict[str, dict[int, str]] = {}
        revision_budget = _RecoveryBudget(
            label="revision recovery",
            max_rows=MAX_REVISION_RECOVERY_ROWS,
            max_bytes=MAX_REVISION_RECOVERY_BYTES,
            max_row_bytes=MAX_REVISION_ROW_BYTES,
        )
        revision_specs = (
            _GuardedTextSpec("revision_id", "revision_id", 128),
            _GuardedTextSpec("stream_id", "stream_id", 128),
            _GuardedTextSpec(
                "predecessor_revision_id",
                "predecessor_revision_id",
                128,
                nullable=True,
            ),
            _GuardedTextSpec("created_at", "created_at", 64),
            _GuardedTextSpec("manifest_digest", "manifest_digest", 64),
            _GuardedTextSpec(
                "manifest_json",
                "manifest_json",
                MAX_REVISION_MANIFEST_BYTES,
            ),
            _GuardedTextSpec("context_id", "context_id", 256),
            _GuardedTextSpec(
                "context_manifest_digest",
                "context_manifest_digest",
                64,
            ),
            _GuardedTextSpec("registry_digest", "registry_digest", 64),
            _GuardedTextSpec(
                "execution_snapshot_id",
                "execution_snapshot_id",
                128,
            ),
            _GuardedTextSpec(
                "execution_snapshot_digest",
                "execution_snapshot_digest",
                64,
            ),
            _GuardedTextSpec("adapter_set_digest", "adapter_set_digest", 64),
        )
        revision_plans = _scan_guarded_row_plans(
            conn,
            from_sql="revisions",
            key_spec=revision_specs[0],
            text_specs=revision_specs,
            order_by_sql="stream_id, generation",
            budget=revision_budget,
        )
        for plan in revision_plans:
            row = _fetch_guarded_row(
                conn,
                from_sql="revisions",
                where_sql="revision_id = ?",
                key=plan.key,
                text_specs=revision_specs,
                lengths=plan.lengths,
                scalar_selections=(
                    "CASE WHEN typeof(generation) = 'integer' THEN generation END AS generation",
                ),
                label="revision recovery",
            )
            manifest = self._revision_manifest_from_row(row)
            self._validate_revision_materialization_binding(
                manifest,
                materialized_contexts.get(manifest.context.context_id),
            )
            execution_snapshot = execution_snapshots.get(manifest.execution_snapshot_id)
            if (
                execution_snapshot is None
                or execution_snapshot.snapshot_digest != manifest.execution_snapshot_digest
            ):
                raise RevisionIntegrityError(
                    "revision execution snapshot is not registered exactly"
                )
            revision_id = str(row["revision_id"])
            revisions[revision_id] = _RevisionRecoveryIdentity(
                revision_id=revision_id,
                stream_id=manifest.stream_id,
                generation=manifest.generation,
                predecessor_revision_id=manifest.predecessor_revision_id,
                project_snapshot_id=manifest.project_snapshot.snapshot_id,
                workspace_snapshot_id=manifest.workspace_snapshot.snapshot_id,
                context_id=manifest.context.context_id,
                context_artifact_ids=manifest.context.artifact_ids,
                context_artifact_set_digest=canonical_digest(manifest.context.artifact_ids),
                execution_snapshot_id=manifest.execution_snapshot_id,
                execution_mode=str(manifest.execution_snapshot.execution_mode),
                capture_mode=str(manifest.execution_snapshot.capture_mode),
            )
            revisions_by_stream.setdefault(manifest.stream_id, {})[manifest.generation] = (
                revision_id
            )

        streams: dict[str, tuple[str, int]] = {}
        stream_budget = _RecoveryBudget(
            label="revision stream recovery",
            max_rows=MAX_REVISION_STREAM_RECOVERY_ROWS,
            max_bytes=MAX_REVISION_STREAM_RECOVERY_BYTES,
            max_row_bytes=MAX_REVISION_STREAM_ROW_BYTES,
        )
        stream_specs = (
            _GuardedTextSpec("stream_id", "stream_id", 128),
            _GuardedTextSpec("active_revision_id", "active_revision_id", 128),
            _GuardedTextSpec("created_at", "created_at", 64),
            _GuardedTextSpec("updated_at", "updated_at", 64),
        )
        stream_plans = _scan_guarded_row_plans(
            conn,
            from_sql="revision_streams",
            key_spec=stream_specs[0],
            text_specs=stream_specs,
            order_by_sql="stream_id",
            budget=stream_budget,
        )
        for plan in stream_plans:
            row = _fetch_guarded_row(
                conn,
                from_sql="revision_streams",
                where_sql="stream_id = ?",
                key=plan.key,
                text_specs=stream_specs,
                lengths=plan.lengths,
                scalar_selections=(
                    "CASE WHEN typeof(active_generation) = 'integer' "
                    "THEN active_generation END AS active_generation",
                ),
                label="revision stream recovery",
            )
            stream_id = str(row["stream_id"])
            active_revision_id = str(row["active_revision_id"])
            try:
                active_generation = _strict_sqlite_integer(
                    row["active_generation"],
                    label="revision stream active generation",
                )
                created_at = _parse_canonical_utc_iso(
                    row["created_at"],
                    label="revision stream created_at",
                )
                updated_at = _parse_canonical_utc_iso(
                    row["updated_at"],
                    label="revision stream updated_at",
                )
            except (TypeError, ValueError) as exc:
                raise RevisionIntegrityError("revision stream record is invalid") from exc
            active_revision = revisions.get(active_revision_id)
            stream_revisions = revisions_by_stream.get(stream_id)
            if (
                active_generation < 0
                or updated_at < created_at
                or active_revision is None
                or active_revision.stream_id != stream_id
                or active_revision.generation != active_generation
                or not stream_revisions
                or max(stream_revisions) != active_generation
            ):
                raise RevisionIntegrityError("revision stream active identity is inconsistent")
            streams[stream_id] = (active_revision_id, active_generation)
        if set(revisions_by_stream) != set(streams):
            raise RevisionIntegrityError("revision ledger contains an orphan stream")

        for revision_id, revision in revisions.items():
            if revision.generation == 0:
                if revision.predecessor_revision_id is not None:
                    raise RevisionIntegrityError("genesis revision has a predecessor")
                continue
            predecessor = revisions.get(revision.predecessor_revision_id or "")
            if (
                predecessor is None
                or predecessor.stream_id != revision.stream_id
                or predecessor.generation != revision.generation - 1
                or revisions_by_stream[revision.stream_id].get(revision.generation) != revision_id
            ):
                raise RevisionIntegrityError("revision predecessor chain is inconsistent")

        admission_budget = _RecoveryBudget(
            label="task admission recovery",
            max_rows=MAX_TASK_ADMISSION_RECOVERY_ROWS,
            max_bytes=MAX_TASK_ADMISSION_RECOVERY_BYTES,
            max_row_bytes=MAX_TASK_ADMISSION_ROW_BYTES,
        )
        admission_specs = (
            _GuardedTextSpec("admission_id", "admission_id", 128),
            _GuardedTextSpec("stream_id", "stream_id", 128),
            _GuardedTextSpec("task_id", "task_id", 128),
            _GuardedTextSpec("idempotency_key", "idempotency_key", 128),
            _GuardedTextSpec("request_digest", "request_digest", 64),
            _GuardedTextSpec("request_json", "request_json", 64 * 1024),
            _GuardedTextSpec("status", "status", 32),
            _GuardedTextSpec("reason", "reason", 128, nullable=True),
            _GuardedTextSpec(
                "pinned_revision_id",
                "pinned_revision_id",
                128,
                nullable=True,
            ),
            _GuardedTextSpec("created_at", "created_at", 64),
            _GuardedTextSpec("updated_at", "updated_at", 64),
            _GuardedTextSpec("finished_at", "finished_at", 64, nullable=True),
        )
        admission_plans = _scan_guarded_row_plans(
            conn,
            from_sql="task_admissions",
            key_spec=admission_specs[0],
            text_specs=admission_specs,
            order_by_sql="stream_id, task_id",
            budget=admission_budget,
        )
        for plan in admission_plans:
            row = _fetch_guarded_row(
                conn,
                from_sql="task_admissions",
                where_sql="admission_id = ?",
                key=plan.key,
                text_specs=admission_specs,
                lengths=plan.lengths,
                scalar_selections=(
                    "CASE WHEN typeof(required_generation) = 'integer' "
                    "THEN required_generation END AS required_generation",
                ),
                label="task admission recovery",
            )
            record = self._task_admission_from_row(row)
            stream = streams.get(record.request.stream_id)
            if stream is None:
                raise RevisionIntegrityError("task admission references an unknown stream")
            self._validate_recovered_admission_sources(
                record.request,
                materialized_contexts=materialized_contexts,
                execution_snapshots=execution_snapshots,
            )
            active_revision_id, active_generation = stream
            if record.pinned_revision_id is not None:
                pinned = revisions.get(record.pinned_revision_id)
                if (
                    pinned is None
                    or pinned.stream_id != record.request.stream_id
                    or pinned.generation != record.request.required_generation
                ):
                    raise RevisionIntegrityError("task admission pin is inconsistent")
                try:
                    self._validate_admission_request_against_revision(record.request, pinned)
                except TaskAdmissionConflictError as exc:
                    raise RevisionIntegrityError(
                        "task admission pin identity is inconsistent"
                    ) from exc
            else:
                self._validate_unpinned_admission_authority(
                    record,
                    active_generation=active_generation,
                    active_revision=revisions.get(active_revision_id),
                )

    @staticmethod
    def _expected_context_snapshot_bytes(
        conn: sqlite3.Connection,
    ) -> dict[str, bytes]:
        specs = (
            _GuardedTextSpec("context_id", "context_id", 256),
            _GuardedTextSpec(
                "request_json",
                "request_json",
                MAX_CONTEXT_SNAPSHOT_BYTES,
            ),
            _GuardedTextSpec(
                "response_json",
                "response_json",
                MAX_CONTEXT_SNAPSHOT_BYTES,
            ),
        )
        plans = _scan_guarded_row_plans(
            conn,
            from_sql="contexts",
            key_spec=specs[0],
            text_specs=specs,
            order_by_sql="context_id",
            budget=_RecoveryBudget(
                label="context snapshot recovery",
                max_rows=MAX_CONTEXT_SNAPSHOT_ENTRIES,
                max_bytes=MAX_CONTEXT_SNAPSHOT_INVENTORY_BYTES,
                max_row_bytes=(2 * MAX_CONTEXT_SNAPSHOT_BYTES) + 256,
            ),
        )
        expected: dict[str, bytes] = {}
        for plan in plans:
            row = _fetch_guarded_row(
                conn,
                from_sql="contexts",
                where_sql="context_id = ?",
                key=plan.key,
                text_specs=specs,
                lengths=plan.lengths,
                label="context snapshot recovery",
            )
            context_id = row["context_id"]
            try:
                request_payload = json.loads(row["request_json"])
                response_payload = json.loads(row["response_json"])
            except ValueError as exc:
                raise ValueError("persisted context snapshot JSON is invalid") from exc
            if not isinstance(request_payload, dict) or not isinstance(response_payload, dict):
                raise ValueError("persisted context snapshot payload is invalid")
            snapshot_bytes = _context_snapshot_bytes(
                request_payload,
                response_payload,
            )
            if len(snapshot_bytes) > MAX_CONTEXT_SNAPSHOT_BYTES:
                raise ValueError("persisted context snapshot exceeds its byte limit")
            expected[context_id] = snapshot_bytes
        return expected

    def _remove_orphan_context_materializations(
        self,
        candidates: list[_OrphanMaterialization],
        root_descriptor: int,
    ) -> None:
        if not candidates:
            return
        mismatched: list[str] = []
        for candidate in candidates:
            result = _remove_materialized_entry_if_identity(
                root_descriptor,
                candidate.name,
                candidate.identity,
            )
            if result == "mismatch":
                mismatched.append(candidate.name)
        if mismatched:
            raise ValueError(
                "orphan materialization identity changed during cleanup and was preserved"
            )

    def _ensure_schema(self, conn: sqlite3.Connection) -> None:
        dataset_request_columns = {
            str(row["name"])
            for row in conn.execute(
                "PRAGMA table_info(dataset_create_requests)"
            ).fetchall()
        }
        recovery_columns = {
            "recovery_file_count",
            "recovery_byte_size",
        }
        present_recovery_columns = (
            dataset_request_columns & recovery_columns
        )
        if present_recovery_columns and present_recovery_columns != recovery_columns:
            raise ValueError(
                "dataset recovery accounting schema is incomplete"
            )
        if not present_recovery_columns:
            conn.execute(
                "ALTER TABLE dataset_create_requests ADD COLUMN "
                "recovery_file_count INTEGER CHECK("
                "recovery_file_count IS NULL OR ("
                "typeof(recovery_file_count) = 'integer' "
                "AND recovery_file_count >= 0))"
            )
            conn.execute(
                "ALTER TABLE dataset_create_requests ADD COLUMN "
                "recovery_byte_size INTEGER CHECK("
                "recovery_byte_size IS NULL OR ("
                "typeof(recovery_byte_size) = 'integer' "
                "AND recovery_byte_size >= 0))"
            )
            self._dataset_recovery_accounting_migration = True
        artifact_columns = {
            str(row["name"]) for row in conn.execute("PRAGMA table_info(artifacts)").fetchall()
        }
        if "staging_job_id" not in artifact_columns:
            conn.execute("ALTER TABLE artifacts ADD COLUMN staging_job_id TEXT")
        if "manifest_json" not in artifact_columns:
            # Existing artifacts are not backfilled from mutable legacy files.
            conn.execute("ALTER TABLE artifacts ADD COLUMN manifest_json TEXT")
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_artifacts_staging_job "
            "ON artifacts(staging_job_id) WHERE staging_job_id IS NOT NULL"
        )
        job_columns = {
            str(row["name"]) for row in conn.execute("PRAGMA table_info(jobs)").fetchall()
        }
        if "lease_duration_seconds" not in job_columns:
            conn.execute("ALTER TABLE jobs ADD COLUMN lease_duration_seconds INTEGER")
            job_columns.add("lease_duration_seconds")
        for column_name in (
            "plan_id",
            "target_id",
            "method_identity_digest",
            "execution_envelope_json",
            "execution_envelope_digest",
            "declared_output_artifact_types_json",
        ):
            if column_name not in job_columns:
                conn.execute(f"ALTER TABLE jobs ADD COLUMN {column_name} TEXT")
        legacy_active_leases = conn.execute(
            """
            SELECT job_id, updated_at, lease_expires_at
            FROM jobs
            WHERE state IN (?, ?)
              AND lease_duration_seconds IS NULL
              AND lease_expires_at IS NOT NULL
            """,
            (str(JobState.CLAIMED), str(JobState.RUNNING)),
        ).fetchall()
        for row in legacy_active_leases:
            lease_duration_seconds = self._infer_legacy_lease_duration_seconds(row)
            if lease_duration_seconds is not None:
                conn.execute(
                    """
                    UPDATE jobs SET lease_duration_seconds = ? WHERE job_id = ?
                    """,
                    (lease_duration_seconds, row["job_id"]),
                )
        duplicate_plan_target = conn.execute(
            """
            SELECT plan_id, target_id
            FROM jobs
            WHERE plan_id IS NOT NULL
            GROUP BY plan_id, target_id
            HAVING COUNT(*) > 1
            LIMIT 1
            """
        ).fetchone()
        if duplicate_plan_target is not None:
            raise RuntimeError("plan-bound jobs contain duplicate plan/target identity")
        conn.execute("DROP INDEX IF EXISTS idx_jobs_plan_target")
        conn.execute(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS idx_jobs_plan_target_unique
            ON jobs(plan_id, target_id)
            WHERE plan_id IS NOT NULL
            """
        )
        review_columns = {
            str(row["name"])
            for row in conn.execute("PRAGMA table_info(review_requests)").fetchall()
        }
        if "adjudication_rationale" not in review_columns:
            conn.execute("ALTER TABLE review_requests ADD COLUMN adjudication_rationale TEXT")
        conn.execute(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS idx_review_requests_query_decision_id_unique
            ON review_requests(query_decision_id)
            WHERE query_decision_id IS NOT NULL
            """
        )
        conn.execute(
            """
            DELETE FROM feedback_applications
            WHERE rowid NOT IN (
                SELECT MIN(rowid)
                FROM feedback_applications
                GROUP BY
                    feedback_id,
                    target_type,
                    target_id,
                    consumed_by_method,
                    COALESCE(consumed_in_job_id, '')
            )
            """
        )
        conn.execute(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS idx_feedback_applications_natural_key_unique
            ON feedback_applications(
                feedback_id,
                target_type,
                target_id,
                consumed_by_method,
                COALESCE(consumed_in_job_id, '')
            )
            """
        )

    @staticmethod
    def _verify_plan_bound_job_retry_requests(
        conn: sqlite3.Connection,
    ) -> None:
        rows = conn.execute(
            "SELECT retry.job_id, retry.retry_request_id, retry.plan_id, "
            "retry.target_id, retry.claim_attempt_before, retry.created_at, "
            "jobs.plan_id AS job_plan_id, jobs.target_id AS job_target_id, "
            "jobs.attempt_count "
            "FROM plan_bound_job_retry_requests AS retry "
            "JOIN jobs ON jobs.job_id = retry.job_id "
            "ORDER BY retry.job_id, retry.retry_request_id LIMIT ?",
            (MAX_PLAN_BOUND_JOB_RETRY_REQUESTS + 1,),
        ).fetchall()
        if len(rows) > MAX_PLAN_BOUND_JOB_RETRY_REQUESTS:
            raise ValueError("plan-bound job retry receipt inventory exceeds its bound")
        for row in rows:
            request = PlanBoundJobRetryRequest(
                retry_request_id=str(row["retry_request_id"]),
                plan_id=str(row["plan_id"]),
                target_id=str(row["target_id"]),
            )
            try:
                datetime.fromisoformat(
                    str(row["created_at"]).replace("Z", "+00:00")
                )
            except ValueError as exc:
                raise ValueError(
                    "plan-bound job retry receipt timestamp is invalid"
                ) from exc
            if (
                request.plan_id != row["job_plan_id"]
                or request.target_id != row["job_target_id"]
                or type(row["claim_attempt_before"]) is not int
                or type(row["attempt_count"]) is not int
                or not 0
                <= int(row["claim_attempt_before"])
                <= int(row["attempt_count"])
            ):
                raise ValueError(
                    "plan-bound job retry receipt differs from job authority"
                )

    @staticmethod
    def _verify_plan_bound_job_transition_bindings(
        conn: sqlite3.Connection,
    ) -> None:
        inventory = conn.execute(
            """
            SELECT COUNT(*) AS row_count,
                   COALESCE(SUM(
                       length(CAST(job_id AS BLOB))
                       + length(CAST(successor_transition_id AS BLOB))
                       + COALESCE(length(CAST(
                           predecessor_successor_transition_id AS BLOB
                         )), 0)
                       + length(CAST(created_at AS BLOB))
                   ), 0) AS total_bytes,
                   COALESCE(MAX(length(CAST(job_id AS BLOB))), 0)
                     AS max_job_id_bytes,
                   COALESCE(MAX(length(CAST(
                       successor_transition_id AS BLOB
                     ))), 0) AS max_successor_id_bytes,
                   COALESCE(MAX(COALESCE(length(CAST(
                       predecessor_successor_transition_id AS BLOB
                     )), 0)), 0) AS max_predecessor_id_bytes,
                   COALESCE(MAX(length(CAST(created_at AS BLOB))), 0)
                     AS max_created_at_bytes
            FROM plan_bound_job_transition_bindings
            """
        ).fetchone()
        assert inventory is not None
        if (
            int(inventory["row_count"])
            > MAX_PLAN_BOUND_JOB_TRANSITION_BINDINGS
            or int(inventory["total_bytes"])
            > MAX_PLAN_BOUND_JOB_TRANSITION_BINDING_BYTES
            or int(inventory["max_job_id_bytes"]) > 256
            or int(inventory["max_successor_id_bytes"]) > 256
            or int(inventory["max_predecessor_id_bytes"]) > 256
            or int(inventory["max_created_at_bytes"]) > 64
        ):
            raise ValueError(
                "plan-bound transition binding inventory exceeds its bounds"
            )
        rows = conn.execute(
            """
            SELECT binding.job_id, binding.successor_transition_id,
                   binding.predecessor_successor_transition_id,
                   binding.created_at, jobs.plan_id
            FROM plan_bound_job_transition_bindings AS binding
            JOIN jobs ON jobs.job_id = binding.job_id
            WHERE length(CAST(binding.job_id AS BLOB))
                    BETWEEN 1 AND 256
              AND length(CAST(
                    binding.successor_transition_id AS BLOB
                  )) BETWEEN 1 AND 256
              AND (
                    binding.predecessor_successor_transition_id IS NULL
                    OR length(CAST(
                        binding.predecessor_successor_transition_id AS BLOB
                      )) BETWEEN 1 AND 256
                  )
              AND length(CAST(binding.created_at AS BLOB))
                    BETWEEN 1 AND 64
            ORDER BY binding.job_id
            LIMIT ?
            """,
            (MAX_PLAN_BOUND_JOB_TRANSITION_BINDINGS + 1,),
        ).fetchall()
        if len(rows) != int(inventory["row_count"]):
            raise ValueError(
                "plan-bound transition binding inventory is malformed"
            )
        stable_id = re.compile(
            r"^[A-Za-z0-9][A-Za-z0-9._-]{0,255}$"
        )
        for row in rows:
            predecessor = row[
                "predecessor_successor_transition_id"
            ]
            try:
                created_at = datetime.fromisoformat(
                    str(row["created_at"]).replace("Z", "+00:00")
                )
            except ValueError as exc:
                raise ValueError(
                    "plan-bound transition binding timestamp is invalid"
                ) from exc
            if (
                row["plan_id"] is None
                or stable_id.fullmatch(str(row["job_id"])) is None
                or stable_id.fullmatch(
                    str(row["successor_transition_id"])
                )
                is None
                or (
                    predecessor is not None
                    and stable_id.fullmatch(str(predecessor)) is None
                )
                or created_at.tzinfo is None
            ):
                raise ValueError(
                    "plan-bound transition binding differs from job authority"
                )

        invalid_sealed = conn.execute(
            """
            SELECT 1
            FROM artifacts
            LEFT JOIN plan_bound_job_transition_bindings AS binding
              ON binding.job_id = artifacts.staging_job_id
            LEFT JOIN jobs
              ON jobs.job_id = artifacts.staging_job_id
            LEFT JOIN evolution_plans AS plans
              ON plans.plan_id = jobs.plan_id
            WHERE artifacts.state = ?
              AND (
                    artifacts.staging_job_id IS NULL
                    OR binding.job_id IS NULL
                    OR jobs.state != ?
                    OR jobs.plan_id IS NULL
                    OR jobs.target_id IS NULL
                    OR plans.plan_id IS NULL
                    OR json_valid(plans.plan_json) != 1
                    OR (
                      SELECT COUNT(*)
                      FROM json_each(
                        CASE
                          WHEN json_valid(plans.plan_json) = 1
                          THEN plans.plan_json
                          ELSE '{"selections":[]}'
                        END,
                        '$.selections'
                      ) AS selection
                      WHERE json_extract(
                              selection.value,
                              '$.target_id'
                            ) = jobs.target_id
                        AND json_extract(
                              selection.value,
                              '$.method_id'
                            ) = jobs.method
                    ) != 1
                    OR NOT EXISTS (
                      SELECT 1
                      FROM json_each(
                        CASE
                          WHEN json_valid(
                            jobs.declared_output_artifact_types_json
                          ) = 1
                          THEN
                            jobs.declared_output_artifact_types_json
                          ELSE '[]'
                        END
                      ) AS declared_type
                      WHERE declared_type.type = 'text'
                        AND declared_type.value = artifacts.type
                    )
                    OR CASE
                      WHEN json_valid(artifacts.lineage_json) != 1
                        OR json_valid(
                             jobs.execution_envelope_json
                           ) != 1
                        OR json_valid(
                             jobs.declared_output_artifact_types_json
                           ) != 1
                      THEN 1
                      WHEN json_type(
                             artifacts.lineage_json,
                             '$.openevo_execution'
                           ) != 'object'
                      THEN 1
                      ELSE json_extract(
                        artifacts.lineage_json,
                        '$.openevo_execution'
                      ) IS NOT json_object(
                        'declared_output_artifact_types',
                        json(
                          jobs.declared_output_artifact_types_json
                        ),
                        'execution_envelope_digest',
                        jobs.execution_envelope_digest,
                        'input_bindings',
                        json_extract(
                          jobs.execution_envelope_json,
                          '$.input_bindings'
                        ),
                        'job_id',
                        artifacts.staging_job_id,
                        'method_id',
                        jobs.method,
                        'method_identity_digest',
                        jobs.method_identity_digest,
                        'plan_digest',
                        plans.plan_digest,
                        'plan_id',
                        jobs.plan_id,
                        'registry_snapshot_digest',
                        plans.registry_snapshot_digest,
                        'successor_transition_id',
                        binding.successor_transition_id,
                        'target_id',
                        jobs.target_id
                      )
                    END
                  )
            LIMIT 1
            """,
            (
                str(ArtifactState.SEALED),
                str(JobState.SUCCEEDED),
            ),
        ).fetchone()
        if invalid_sealed is not None:
            raise ValueError(
                "sealed transition artifact authority is invalid"
            )
        leaked_bound_output = conn.execute(
            """
            SELECT 1
            FROM artifacts
            JOIN plan_bound_job_transition_bindings AS binding
              ON json_extract(
                   artifacts.lineage_json,
                   '$.openevo_execution.job_id'
                 ) = binding.job_id
            JOIN jobs ON jobs.job_id = binding.job_id
            WHERE jobs.state = ?
              AND artifacts.state != ?
            LIMIT 1
            """,
            (
                str(JobState.SUCCEEDED),
                str(ArtifactState.SEALED),
            ),
        ).fetchone()
        if leaked_bound_output is not None:
            raise ValueError(
                "transition-bound job output escaped its sealed lifecycle"
            )

    @staticmethod
    def _verify_successor_transition_discards(
        conn: sqlite3.Connection,
        materialized_contexts: dict[
            str,
            _MaterializationRecoveryIdentity,
        ],
    ) -> None:
        inventory = conn.execute(
            "SELECT COUNT(*) AS row_count, "
            "COALESCE(SUM("
            "length(CAST(successor_transition_id AS BLOB)) + "
            "length(CAST(receipt_sha256 AS BLOB)) + "
            "length(CAST(receipt_json AS BLOB)) + "
            "length(CAST(discarded_at AS BLOB))"
            "), 0) AS total_bytes, "
            "COALESCE(MAX(length(CAST("
            "successor_transition_id AS BLOB))), 0) "
            "AS max_transition_id_bytes, "
            "COALESCE(MAX(length(CAST(receipt_sha256 AS BLOB))), 0) "
            "AS max_sha_bytes, "
            "COALESCE(MAX(length(CAST(receipt_json AS BLOB))), 0) "
            "AS max_receipt_bytes, "
            "COALESCE(MAX(length(CAST(discarded_at AS BLOB))), 0) "
            "AS max_discarded_at_bytes "
            "FROM successor_transition_discards"
        ).fetchone()
        assert inventory is not None
        if (
            int(inventory["row_count"])
            > MAX_SUCCESSOR_TRANSITION_DISCARDS
            or int(inventory["total_bytes"])
            > MAX_SUCCESSOR_TRANSITION_DISCARD_AGGREGATE_BYTES
            or int(inventory["max_transition_id_bytes"]) > 256
            or int(inventory["max_sha_bytes"]) > 64
            or int(inventory["max_receipt_bytes"])
            > MAX_SUCCESSOR_TRANSITION_DISCARD_RECEIPT_BYTES
            or int(inventory["max_discarded_at_bytes"]) > 64
        ):
            raise ValueError(
                "successor transition discard inventory exceeds "
                "its bounds"
            )
        rows = conn.execute(
            "SELECT CASE WHEN "
            "typeof(successor_transition_id) = 'text' "
            "AND length(CAST(successor_transition_id AS BLOB)) "
            "BETWEEN 1 AND 256 THEN successor_transition_id END "
            "AS successor_transition_id "
            "FROM successor_transition_discards "
            "ORDER BY successor_transition_id LIMIT ?",
            (MAX_SUCCESSOR_TRANSITION_DISCARDS + 1,),
        ).fetchall()
        if len(rows) != int(inventory["row_count"]):
            raise ValueError(
                "successor transition discard inventory is malformed"
            )
        transition_ids: set[str] = set()
        for row in rows:
            transition_id = row["successor_transition_id"]
            if (
                not isinstance(transition_id, str)
                or _SUCCESSOR_TRANSITION_ID_RE.fullmatch(
                    transition_id
                )
                is None
                or _load_successor_transition_discard(
                    conn,
                    transition_id,
                )
                is None
            ):
                raise ValueError(
                    "successor transition discard inventory is invalid"
                )
            transition_ids.add(transition_id)
        if any(
            identity.successor_transition_id in transition_ids
            for identity in materialized_contexts.values()
        ):
            raise ValueError(
                "discarded successor transition retained a "
                "materialized context"
            )
        active_job = conn.execute(
            "SELECT 1 FROM jobs "
            "JOIN plan_bound_job_transition_bindings AS binding "
            "ON binding.job_id = jobs.job_id "
            "JOIN successor_transition_discards AS discard "
            "ON discard.successor_transition_id = "
            "binding.successor_transition_id "
            "WHERE jobs.state IN (?, ?, ?) LIMIT 1",
            (
                str(JobState.PENDING),
                str(JobState.CLAIMED),
                str(JobState.RUNNING),
            ),
        ).fetchone()
        if active_job is not None:
            raise ValueError(
                "discarded successor transition retained active work"
            )
        retained_output = conn.execute(
            "SELECT 1 FROM artifacts "
            "JOIN plan_bound_job_transition_bindings AS binding "
            "ON binding.job_id = artifacts.staging_job_id "
            "JOIN successor_transition_discards AS discard "
            "ON discard.successor_transition_id = "
            "binding.successor_transition_id LIMIT 1"
        ).fetchone()
        if retained_output is not None:
            raise ValueError(
                "discarded successor transition retained output"
            )
        cancelled_lease = conn.execute(
            "SELECT 1 FROM jobs "
            "JOIN plan_bound_job_transition_bindings AS binding "
            "ON binding.job_id = jobs.job_id "
            "JOIN successor_transition_discards AS discard "
            "ON discard.successor_transition_id = "
            "binding.successor_transition_id "
            "WHERE jobs.state = ? AND ("
            "jobs.claimed_by IS NOT NULL "
            "OR jobs.lease_id IS NOT NULL "
            "OR jobs.lease_expires_at IS NOT NULL "
            "OR jobs.lease_duration_seconds IS NOT NULL"
            ") LIMIT 1",
            (str(JobState.CANCELLED),),
        ).fetchone()
        if cancelled_lease is not None:
            raise ValueError(
                "discarded successor transition retained a lease"
            )

    def _verify_dataset_create_requests(
        self,
        conn: sqlite3.Connection,
        *,
        recovery_budget: _DatasetReadBudget,
    ) -> None:
        inventory = conn.execute(
            "SELECT COUNT(*) AS row_count, "
            "COALESCE(SUM("
            "length(CAST(idempotency_key AS BLOB)) + "
            "length(CAST(request_sha256 AS BLOB)) + "
            "length(CAST(request_json AS BLOB)) + "
            "length(CAST(dataset_id AS BLOB)) + "
            "COALESCE(length(CAST(response_json AS BLOB)), 0) + "
            "length(CAST(created_at AS BLOB))"
            "), 0) AS total_bytes, "
            "COALESCE(MAX(length(CAST(idempotency_key AS BLOB))), 0) "
            "AS max_idempotency_key_bytes, "
            "COALESCE(MAX(length(CAST(request_sha256 AS BLOB))), 0) "
            "AS max_request_sha256_bytes, "
            "COALESCE(MAX(length(CAST(request_json AS BLOB))), 0) "
            "AS max_request_bytes, "
            "COALESCE(MAX(length(CAST(dataset_id AS BLOB))), 0) "
            "AS max_dataset_id_bytes, "
            "COALESCE(MAX(COALESCE(length(CAST(response_json AS BLOB)), 0)), 0) "
            "AS max_response_bytes, "
            "COALESCE(MAX(length(CAST(created_at AS BLOB))), 0) "
            "AS max_created_at_bytes, "
            "COALESCE(SUM(CASE "
            "WHEN recovery_file_count IS NULL THEN 0 "
            "WHEN typeof(recovery_file_count) = 'integer' "
            "AND recovery_file_count >= 0 THEN recovery_file_count "
            "ELSE ? END), 0) AS recovery_file_count, "
            "COALESCE(SUM(CASE "
            "WHEN recovery_byte_size IS NULL THEN 0 "
            "WHEN typeof(recovery_byte_size) = 'integer' "
            "AND recovery_byte_size >= 0 THEN recovery_byte_size "
            "ELSE ? END), 0) AS recovery_byte_size "
            "FROM dataset_create_requests"
            ,
            (
                MAX_DATASET_READ_FILES + 1,
                MAX_DATASET_STARTUP_RECOVERY_BYTES + 1,
            ),
        ).fetchone()
        assert inventory is not None
        if (
            int(inventory["recovery_file_count"])
            > MAX_DATASET_READ_FILES
        ):
            raise ValueError(
                "dataset startup recovery exceeds the file count limit"
            )
        if (
            int(inventory["recovery_byte_size"])
            > MAX_DATASET_STARTUP_RECOVERY_BYTES
        ):
            raise ValueError(
                "dataset startup recovery exceeds the aggregate byte limit"
            )
        if (
            int(inventory["row_count"]) > MAX_DATASET_CREATE_REQUESTS
            or int(inventory["total_bytes"]) > MAX_DATASET_CREATE_RECOVERY_BYTES
            or int(inventory["max_idempotency_key_bytes"]) > 128
            or int(inventory["max_request_sha256_bytes"]) != (
                0 if int(inventory["row_count"]) == 0 else 64
            )
            or int(inventory["max_request_bytes"])
            > MAX_DATASET_CREATE_REQUEST_BYTES
            or int(inventory["max_dataset_id_bytes"]) > 67
            or int(inventory["max_response_bytes"])
            > MAX_DATASET_CREATE_RESPONSE_BYTES
            or int(inventory["max_created_at_bytes"]) > 64
        ):
            raise ValueError(
                "dataset create request inventory exceeds its recovery bounds"
            )
        rows = conn.execute(
            "SELECT idempotency_key, request_sha256, request_json, dataset_id, "
            "response_json, recovery_file_count, recovery_byte_size, created_at "
            "FROM dataset_create_requests "
            "WHERE length(CAST(idempotency_key AS BLOB)) BETWEEN 1 AND 128 "
            "AND length(CAST(request_sha256 AS BLOB)) = 64 "
            "AND length(CAST(request_json AS BLOB)) "
            "BETWEEN 1 AND ? "
            "AND length(CAST(dataset_id AS BLOB)) BETWEEN 1 AND 67 "
            "AND COALESCE(length(CAST(response_json AS BLOB)), 0) <= ? "
            "AND length(CAST(created_at AS BLOB)) BETWEEN 1 AND 64 "
            "ORDER BY idempotency_key LIMIT ?",
            (
                MAX_DATASET_CREATE_REQUEST_BYTES,
                MAX_DATASET_CREATE_RESPONSE_BYTES,
                MAX_DATASET_CREATE_REQUESTS + 1,
            ),
        ).fetchall()
        if len(rows) != int(inventory["row_count"]):
            raise ValueError("dataset create request inventory is malformed")
        for row in rows:
            idempotency_key = str(row["idempotency_key"])
            request_json = str(row["request_json"])
            request_sha256 = hashlib.sha256(
                request_json.encode("utf-8")
            ).hexdigest()
            try:
                request = DatasetCreateRequest.model_validate_json(request_json)
                created_at = datetime.fromisoformat(
                    str(row["created_at"]).replace("Z", "+00:00")
                )
            except (TypeError, ValueError) as exc:
                raise ValueError(
                    "dataset create request receipt is invalid"
                ) from exc
            if created_at.tzinfo is None:
                raise ValueError(
                    "dataset create request receipt timestamp is invalid"
                )
            dataset_id = _dataset_id_for_idempotency_key(idempotency_key)
            if (
                request.idempotency_key != idempotency_key
                or _dataset_create_request_json(request) != request_json
                or row["request_sha256"] != request_sha256
                or row["dataset_id"] != dataset_id
            ):
                raise ValueError(
                    "dataset create request receipt differs from its identity"
                )
            recovery_file_count = row["recovery_file_count"]
            recovery_byte_size = row["recovery_byte_size"]
            if (
                (recovery_file_count is None)
                != (recovery_byte_size is None)
                or (
                    recovery_file_count is not None
                    and (
                        type(recovery_file_count) is not int
                        or int(recovery_file_count) < 0
                        or type(recovery_byte_size) is not int
                        or int(recovery_byte_size) < 0
                    )
                )
            ):
                raise ValueError(
                    "dataset recovery accounting is malformed"
                )
            response_json = row["response_json"]
            pending = conn.execute(
                "SELECT * FROM datasets WHERE dataset_id = ?",
                (dataset_id,),
            ).fetchone()
            has_sealed_authority = (
                pending is not None
                and pending["artifact_id"] is not None
            )
            reconciled_candidate = False
            files_before = recovery_budget.files
            bytes_before = recovery_budget.bytes
            authority_response: DatasetCreateResponse | None = None
            if response_json is None:
                if not has_sealed_authority:
                    if pending is not None:
                        authority_response = (
                            self._reconcile_pending_dataset_artifact_candidate(
                                conn,
                                dataset_row=pending,
                                budget=recovery_budget,
                            )
                        )
                        reconciled_candidate = authority_response is not None
                        has_sealed_authority = reconciled_candidate
                    if not has_sealed_authority:
                        if recovery_file_count is not None:
                            raise ValueError(
                                "pending dataset has sealed recovery accounting"
                            )
                        continue
                expected_response = None
            else:
                try:
                    expected_response = (
                        DatasetCreateResponse.model_validate_json(
                            str(response_json)
                        )
                    )
                except ValueError as exc:
                    raise ValueError(
                        "dataset create request response is invalid"
                    ) from exc
                if (
                    _dataset_create_response_json(expected_response)
                    != response_json
                    or expected_response.dataset_id != dataset_id
                ):
                    raise ValueError(
                        "dataset create request response differs from dataset authority"
                    )
            if authority_response is None:
                authority_response = self._get_dataset_from_connection(
                    conn,
                    dataset_id,
                    budget=recovery_budget,
                )
            actual_file_count = recovery_budget.files - files_before
            actual_byte_size = recovery_budget.bytes - bytes_before
            if recovery_file_count is None:
                if (
                    not self._dataset_recovery_accounting_migration
                    and not reconciled_candidate
                ):
                    raise ValueError(
                        "sealed dataset recovery accounting is missing"
                    )
                updated = conn.execute(
                    "UPDATE dataset_create_requests "
                    "SET recovery_file_count = ?, recovery_byte_size = ? "
                    "WHERE idempotency_key = ? "
                    "AND recovery_file_count IS NULL "
                    "AND recovery_byte_size IS NULL",
                    (
                        actual_file_count,
                        actual_byte_size,
                        idempotency_key,
                    ),
                )
                if updated.rowcount != 1:
                    raise ValueError(
                        "dataset recovery accounting migration lost its fence"
                    )
                recovery_file_count = actual_file_count
                recovery_byte_size = actual_byte_size
            if expected_response is None:
                sealed_response_json = _dataset_create_response_json(
                    authority_response
                )
                updated = conn.execute(
                    "UPDATE dataset_create_requests SET response_json = ? "
                    "WHERE idempotency_key = ? AND response_json IS NULL",
                    (
                        sealed_response_json,
                        idempotency_key,
                    ),
                )
                if updated.rowcount != 1:
                    raise ValueError(
                        "dataset response recovery lost its fence"
                    )
                expected_response = authority_response
            if (
                recovery_file_count != actual_file_count
                or recovery_byte_size != actual_byte_size
                or authority_response != expected_response
            ):
                raise ValueError(
                    "dataset create request response differs from dataset authority"
                )

    def _verify_unjournaled_datasets(
        self,
        conn: sqlite3.Connection,
        *,
        recovery_budget: _DatasetReadBudget,
    ) -> None:
        rows = conn.execute(
            "SELECT datasets.* FROM datasets "
            "LEFT JOIN dataset_create_requests "
            "ON dataset_create_requests.dataset_id = datasets.dataset_id "
            "WHERE dataset_create_requests.dataset_id IS NULL "
            "ORDER BY datasets.dataset_id LIMIT ?",
            (MAX_DATASET_CREATE_REQUESTS + 1,),
        ).fetchall()
        if len(rows) > MAX_DATASET_CREATE_REQUESTS:
            raise DatasetIntegrityError(
                "unjournaled dataset inventory exceeds its recovery bound"
            )
        for row in rows:
            dataset_id = str(row["dataset_id"])
            if row["artifact_id"] is None:
                recovered = self._reconcile_pending_dataset_artifact_candidate(
                    conn,
                    dataset_row=row,
                    budget=recovery_budget,
                )
                if recovered is None:
                    raise DatasetIntegrityError(
                        f"unjournaled dataset is incomplete: {dataset_id}"
                    )
                continue
            self._get_dataset_from_connection(
                conn,
                dataset_id,
                budget=recovery_budget,
            )

    def _reconcile_pending_dataset_artifact_candidate(
        self,
        conn: sqlite3.Connection,
        *,
        dataset_row: sqlite3.Row,
        budget: _DatasetReadBudget,
    ) -> DatasetCreateResponse | None:
        dataset_id = str(dataset_row["dataset_id"])
        if dataset_row["artifact_id"] is not None:
            raise DatasetIntegrityError(
                "pending dataset candidate is already bound"
            )
        candidates = self._dataset_artifact_candidate_rows(
            conn,
            dataset_id=dataset_id,
            manifest_path=Path(str(dataset_row["manifest_path"])),
        )
        if not candidates:
            return None
        if len(candidates) != 1:
            raise DatasetIntegrityError(
                "pending dataset has multiple artifact candidates"
            )
        manifest, _records, _events = (
            self._validate_dataset_storage_from_connection(
                conn,
                dataset_row,
                budget=budget,
            )
        )
        candidate = candidates[0]
        self._validate_dataset_artifact_row(
            candidate,
            dataset_row=dataset_row,
            manifest=manifest,
            budget=budget,
        )
        artifact_id = str(candidate["artifact_id"])
        updated = conn.execute(
            "UPDATE datasets SET artifact_id = ? "
            "WHERE dataset_id = ? AND artifact_id IS NULL",
            (
                artifact_id,
                dataset_id,
            ),
        )
        if updated.rowcount != 1:
            raise DatasetIntegrityError(
                "pending dataset artifact recovery lost its fence"
            )
        return DatasetCreateResponse(
            dataset_id=dataset_id,
            artifact_id=artifact_id,
            event_count=int(dataset_row["event_count"]),
            trace_count=int(dataset_row["trace_count"]),
        )

    def create_review_request(
        self,
        request: ReviewRequestCreateRequest,
    ) -> ReviewRequestResponse:
        raw_payload = request.model_dump(mode="python")
        _validate_finite_floats(raw_payload, "request")
        request_payload = request.model_dump(mode="json")
        inline_query_decision = request_payload.get("query_decision")
        inline_query_decision_payload: dict[str, Any] | None = None
        if inline_query_decision is not None:
            if request_payload["query_decision_id"] is not None:
                raise ValueError(
                    "review request cannot include both query_decision and query_decision_id"
                )
            inline_request = HumanQueryDecisionCreateRequest.model_validate(inline_query_decision)
            _validate_finite_floats(
                inline_request.model_dump(mode="python"),
                "query_decision",
            )
            inline_query_decision_payload = inline_request.model_dump(mode="json")
        packet = _sanitize_review_boundary_payload(request_payload["packet"])
        request_payload["packet"] = packet
        packet_hash = _canonical_json_hash(packet)
        packet_json = _json_dumps(packet)
        request_payload["artifact_ids"] = _sanitize_review_target_ids(
            request_payload["artifact_ids"]
        )
        request_payload["candidate_ids"] = _sanitize_review_target_ids(
            request_payload["candidate_ids"]
        )
        request_payload["artifact_hashes"] = _sanitize_review_artifact_hashes(
            request_payload["artifact_hashes"]
        )
        artifact_ids_json = _json_dumps(request_payload["artifact_ids"])
        candidate_ids_json = _json_dumps(request_payload["candidate_ids"])
        artifact_hashes_json = _json_dumps(request_payload["artifact_hashes"])
        review_id = new_id("rev")
        now = utc_now_iso()

        with self.connect() as conn:
            try:
                conn.execute("BEGIN IMMEDIATE")
                if inline_query_decision_payload is not None:
                    query_decision_id = new_id("hqd")
                    _insert_human_query_decision_row(
                        conn,
                        query_decision_id=query_decision_id,
                        request_payload=inline_query_decision_payload,
                        review_id=review_id,
                        created_at=now,
                    )
                    request_payload["query_decision_id"] = query_decision_id
                if (
                    request_payload["query_decision_id"] is not None
                    and inline_query_decision_payload is None
                ):
                    query_row = conn.execute(
                        """
                        SELECT query_decision_id, review_id
                        FROM human_query_decisions
                        WHERE query_decision_id = ?
                        """,
                        (request_payload["query_decision_id"],),
                    ).fetchone()
                    if query_row is None:
                        raise ValueError(
                            f"unknown query decision: {request_payload['query_decision_id']}"
                        )
                    if query_row["review_id"] is not None:
                        raise ValueError(
                            f"query decision already linked to review: {query_row['review_id']}"
                        )
                    existing_review = conn.execute(
                        """
                        SELECT review_id
                        FROM review_requests
                        WHERE query_decision_id = ?
                        """,
                        (request_payload["query_decision_id"],),
                    ).fetchone()
                    if existing_review is not None:
                        raise ValueError(
                            "query decision already linked to review: "
                            f"{existing_review['review_id']}"
                        )
                packet_row = conn.execute(
                    "SELECT packet_id FROM review_packets WHERE packet_hash = ?",
                    (packet_hash,),
                ).fetchone()
                if packet_row is None:
                    packet_id = new_id("rpacket")
                    conn.execute(
                        """
                        INSERT INTO review_packets (
                            packet_id, packet_hash, packet_json, created_at
                        )
                        VALUES (?, ?, ?, ?)
                        """,
                        (packet_id, packet_hash, packet_json, now),
                    )
                else:
                    packet_id = str(packet_row["packet_id"])

                conn.execute(
                    """
                    INSERT INTO review_requests (
                        review_id, review_type, status, artifact_ids_json,
                        candidate_ids_json, job_id, task_id, round_index, method,
                        artifact_type, packet_id, packet_hash, artifact_hashes_json,
                        query_decision_id, assigned_to, reviewer_role,
                        adjudication_rationale, priority, created_at, updated_at
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        review_id,
                        request_payload["review_type"],
                        ReviewStatus.QUEUED.value,
                        artifact_ids_json,
                        candidate_ids_json,
                        request_payload["job_id"],
                        request_payload["task_id"],
                        request_payload["round_index"],
                        request_payload["method"],
                        request_payload["artifact_type"],
                        packet_id,
                        packet_hash,
                        artifact_hashes_json,
                        request_payload["query_decision_id"],
                        None,
                        None,
                        None,
                        request_payload["priority"],
                        now,
                        now,
                    ),
                )
                if request_payload["query_decision_id"] is not None:
                    conn.execute(
                        """
                        UPDATE human_query_decisions
                        SET review_id = ?
                        WHERE query_decision_id = ?
                        """,
                        (review_id, request_payload["query_decision_id"]),
                    )
                conn.commit()
            except Exception:
                try:
                    conn.rollback()
                except sqlite3.Error:
                    pass
                raise
        return self.get_review_request(review_id)

    def get_review_packet(self, packet_id: str) -> ReviewPacketResponse:
        with self.connect() as conn:
            row = conn.execute(
                """
                SELECT *
                FROM review_packets
                WHERE packet_id = ?
                """,
                (packet_id,),
            ).fetchone()
        if row is None:
            raise ValueError(f"unknown review packet: {packet_id}")
        return _review_packet_response_from_row(row)

    def list_review_packets(self) -> list[ReviewPacketResponse]:
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT *
                FROM review_packets
                ORDER BY created_at ASC, packet_id ASC
                """
            ).fetchall()
        return [_review_packet_response_from_row(row) for row in rows]

    def get_review_request(self, review_id: str) -> ReviewRequestResponse:
        with self.connect() as conn:
            row = self._review_request_row(conn, review_id)
        if row is None:
            raise ValueError(f"unknown review: {review_id}")
        return _review_request_response_from_row(row)

    def list_review_requests(
        self,
        *,
        status: str | None = None,
        task_id: str | None = None,
        assigned_to: str | None = None,
    ) -> list[ReviewRequestResponse]:
        clauses: list[str] = []
        params: list[object] = []
        if status is not None:
            clauses.append("rr.status = ?")
            params.append(status)
        if task_id is not None:
            clauses.append("rr.task_id = ?")
            params.append(task_id)
        if assigned_to is not None:
            clauses.append("rr.assigned_to = ?")
            params.append(assigned_to)
        where = "WHERE " + " AND ".join(clauses) if clauses else ""
        with self.connect() as conn:
            rows = conn.execute(
                f"""
                SELECT rr.*, rp.packet_json
                FROM review_requests rr
                JOIN review_packets rp ON rp.packet_id = rr.packet_id
                {where}
                ORDER BY rr.priority DESC, rr.created_at ASC, rr.review_id ASC
                """,
                params,
            ).fetchall()
        return [_review_request_response_from_row(row) for row in rows]

    def claim_review_request(
        self,
        review_id: str,
        request: ReviewClaimRequest,
    ) -> ReviewRequestResponse:
        now = utc_now_iso()
        with self.connect() as conn:
            try:
                conn.execute("BEGIN IMMEDIATE")
                row = self._review_request_row(conn, review_id)
                if row is None:
                    raise ValueError(f"unknown review: {review_id}")
                _require_review_transition(
                    row,
                    review_id=review_id,
                    action="claim",
                    allowed_statuses={ReviewStatus.QUEUED.value, ReviewStatus.IN_REVIEW.value},
                )
                reviewer_role = request.reviewer_role
                if row["status"] == ReviewStatus.IN_REVIEW.value:
                    assigned_to = row["assigned_to"]
                    if assigned_to is not None and assigned_to != request.reviewer_id:
                        raise ValueError(
                            f"review already claimed by another reviewer: {review_id}"
                        )
                    existing_role = row["reviewer_role"]
                    if (
                        existing_role is not None
                        and request.reviewer_role is not None
                        and existing_role != request.reviewer_role
                    ):
                        raise ValueError(
                            f"review already claimed with a different reviewer role: {review_id}"
                        )
                    reviewer_role = existing_role or request.reviewer_role
                conn.execute(
                    """
                    UPDATE review_requests
                    SET status = ?, assigned_to = ?, reviewer_role = ?, updated_at = ?
                    WHERE review_id = ?
                    """,
                    (
                        ReviewStatus.IN_REVIEW.value,
                        request.reviewer_id,
                        reviewer_role,
                        now,
                        review_id,
                    ),
                )
                conn.commit()
            except Exception:
                try:
                    conn.rollback()
                except sqlite3.Error:
                    pass
                raise
        return self.get_review_request(review_id)

    def submit_human_feedback(
        self,
        review_id: str,
        request: HumanFeedbackCreateRequest,
    ) -> HumanFeedbackResponse:
        raw_payload = request.model_dump(mode="python")
        _validate_finite_floats(raw_payload, "request")
        request_payload = request.model_dump(mode="json")
        raw_payload_json = _json_dumps(request_payload["raw_payload"])
        feedback_id = new_id("hfb")
        now = utc_now_iso()
        status = HumanFeedbackStatus.AVAILABLE_FOR_EVOLUTION.value

        with self.connect() as conn:
            try:
                conn.execute("BEGIN IMMEDIATE")
                review_row = self._review_request_row(conn, review_id)
                if review_row is None:
                    raise ValueError(f"unknown review: {review_id}")
                _require_review_transition(
                    review_row,
                    review_id=review_id,
                    action="submit feedback for",
                    allowed_statuses={ReviewStatus.IN_REVIEW.value},
                )
                assigned_to = review_row["assigned_to"]
                if assigned_to is not None and assigned_to != request_payload["reviewer_id"]:
                    raise ValueError(f"review claimed by a different reviewer: {review_id}")
                effective_reviewer_role = request_payload["reviewer_role"]
                claimed_reviewer_role = review_row["reviewer_role"]
                if claimed_reviewer_role is not None:
                    if (
                        request_payload["reviewer_role"] is not None
                        and request_payload["reviewer_role"] != claimed_reviewer_role
                    ):
                        raise ValueError(
                            f"feedback reviewer role does not match claimed role: {review_id}"
                        )
                    effective_reviewer_role = str(claimed_reviewer_role)
                normalized_payload = _sanitize_review_boundary_payload(
                    _normalize_feedback_payload(
                        request,
                        reviewer_role=effective_reviewer_role,
                    )
                )
                if not isinstance(normalized_payload, dict):
                    normalized_payload = {}
                stored_rationale = _sanitize_review_boundary_text(
                    request_payload["rationale"] or ""
                )
                normalized_payload_json = _json_dumps(normalized_payload)
                conn.execute(
                    """
                    INSERT INTO human_feedback (
                        feedback_id, review_id, reviewer_id, reviewer_role, status,
                        decision, score, confidence, rationale, raw_payload_json,
                        normalized_payload_json, created_at
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        feedback_id,
                        review_id,
                        request_payload["reviewer_id"],
                        effective_reviewer_role,
                        status,
                        request_payload["decision"],
                        request_payload["score"],
                        request_payload["confidence"],
                        stored_rationale,
                        raw_payload_json,
                        normalized_payload_json,
                        now,
                    ),
                )
                conn.execute(
                    """
                    UPDATE review_requests
                    SET status = ?, updated_at = ?
                    WHERE review_id = ?
                    """,
                    (ReviewStatus.SUBMITTED.value, now, review_id),
                )
                conn.commit()
            except Exception:
                try:
                    conn.rollback()
                except sqlite3.Error:
                    pass
                raise
        with self.connect() as conn:
            row = conn.execute(
                "SELECT * FROM human_feedback WHERE feedback_id = ?",
                (feedback_id,),
            ).fetchone()
        if row is None:
            raise ValueError(f"unknown feedback: {feedback_id}")
        return _human_feedback_response_from_row(row)

    def list_human_feedback(
        self,
        *,
        review_id: str | None = None,
    ) -> list[HumanFeedbackResponse]:
        with self.connect() as conn:
            if review_id is not None:
                if self._review_request_row(conn, review_id) is None:
                    raise ValueError(f"unknown review: {review_id}")
                rows = conn.execute(
                    """
                    SELECT * FROM human_feedback
                    WHERE review_id = ?
                    ORDER BY created_at ASC, feedback_id ASC
                    """,
                    (review_id,),
                ).fetchall()
            else:
                rows = conn.execute(
                    """
                    SELECT * FROM human_feedback
                    ORDER BY created_at ASC, feedback_id ASC
                    """
                ).fetchall()
        return [_human_feedback_response_from_row(row) for row in rows]

    def adjudicate_review_request(
        self,
        review_id: str,
        request: ReviewAdjudicationRequest,
    ) -> ReviewRequestResponse:
        target_status = str(request.status)
        rationale = (
            None
            if request.rationale is None
            else _sanitize_review_boundary_text(request.rationale)
        )
        now = utc_now_iso()
        with self.connect() as conn:
            try:
                conn.execute("BEGIN IMMEDIATE")
                row = self._review_request_row(conn, review_id)
                if row is None:
                    raise ValueError(f"unknown review: {review_id}")
                current_status = str(row["status"])
                allowed_targets = _ADJUDICATION_TRANSITIONS.get(current_status, set())
                if target_status not in allowed_targets:
                    raise ValueError(
                        f"cannot adjudicate review {review_id} from status "
                        f"{current_status} to {target_status}"
                    )
                conn.execute(
                    """
                    UPDATE review_requests
                    SET status = ?, adjudication_rationale = ?, updated_at = ?
                    WHERE review_id = ?
                    """,
                    (target_status, rationale, now, review_id),
                )
                if target_status == ReviewStatus.REJECTED_INVALID.value:
                    _archive_active_feedback(
                        conn,
                        review_id=review_id,
                        status=HumanFeedbackStatus.REJECTED_INVALID,
                    )
                elif target_status == ReviewStatus.ARCHIVED_ONLY.value:
                    _archive_active_feedback(
                        conn,
                        review_id=review_id,
                        status=HumanFeedbackStatus.ARCHIVED_ONLY,
                    )
                conn.commit()
            except Exception:
                try:
                    conn.rollback()
                except sqlite3.Error:
                    pass
                raise
        return self.get_review_request(review_id)

    def resolve_review_request(self, review_id: str) -> ReviewRequestResponse:
        now = utc_now_iso()
        with self.connect() as conn:
            try:
                conn.execute("BEGIN IMMEDIATE")
                row = self._review_request_row(conn, review_id)
                if row is None:
                    raise ValueError(f"unknown review: {review_id}")
                _require_review_transition(
                    row,
                    review_id=review_id,
                    action="resolve",
                    allowed_statuses=_RESOLVABLE_REVIEW_STATUSES,
                )
                conn.execute(
                    """
                    UPDATE review_requests
                    SET status = ?, updated_at = ?
                    WHERE review_id = ?
                    """,
                    (ReviewStatus.RESOLVED.value, now, review_id),
                )
                conn.commit()
            except Exception:
                try:
                    conn.rollback()
                except sqlite3.Error:
                    pass
                raise
        return self.get_review_request(review_id)

    def mark_review_stale(self, review_id: str) -> ReviewRequestResponse:
        now = utc_now_iso()
        with self.connect() as conn:
            try:
                conn.execute("BEGIN IMMEDIATE")
                row = self._review_request_row(conn, review_id)
                if row is None:
                    raise ValueError(f"unknown review: {review_id}")
                status = str(row["status"])
                if status not in _STALEABLE_REVIEW_STATUSES:
                    raise ValueError(f"cannot mark review {review_id} stale from status {status}")
                conn.execute(
                    """
                    UPDATE review_requests
                    SET status = ?, updated_at = ?
                    WHERE review_id = ?
                    """,
                    (ReviewStatus.STALE.value, now, review_id),
                )
                conn.execute(
                    """
                    UPDATE human_feedback
                    SET status = ?
                    WHERE review_id = ?
                      AND status IN (?, ?)
                    """,
                    (
                        HumanFeedbackStatus.ARCHIVED_ONLY.value,
                        review_id,
                        HumanFeedbackStatus.AVAILABLE_FOR_EVOLUTION.value,
                        HumanFeedbackStatus.CONSUMED.value,
                    ),
                )
                conn.commit()
            except Exception:
                try:
                    conn.rollback()
                except sqlite3.Error:
                    pass
                raise
        return self.get_review_request(review_id)

    def create_feedback_application(
        self,
        request: FeedbackApplicationCreateRequest,
    ) -> FeedbackApplicationResponse:
        request_payload = request.model_dump(mode="json")
        for key in ("target_id", "consumed_by_method", "consumed_in_job_id"):
            value = request_payload.get(key)
            if isinstance(value, str):
                request_payload[key] = _sanitize_review_metadata_text(value)
        request_payload["effect_summary"] = _sanitize_review_boundary_text(
            request_payload["effect_summary"]
        )
        application_id = new_id("hfa")
        now = utc_now_iso()
        with self.connect() as conn:
            try:
                conn.execute("BEGIN IMMEDIATE")
                feedback_row = self._feedback_row(conn, request.feedback_id)
                if feedback_row is None:
                    raise ValueError(f"unknown feedback: {request.feedback_id}")
                if feedback_row["status"] not in _CONSUMABLE_FEEDBACK_STATUSES:
                    raise ValueError(
                        f"feedback is not available for evolution: {request.feedback_id}"
                    )
                review_row = conn.execute(
                    """
                    SELECT status
                    FROM review_requests
                    WHERE review_id = ?
                    """,
                    (feedback_row["review_id"],),
                ).fetchone()
                if review_row is None:
                    raise ValueError(f"unknown review: {feedback_row['review_id']}")
                if review_row["status"] in {
                    ReviewStatus.STALE.value,
                    ReviewStatus.REJECTED_INVALID.value,
                    ReviewStatus.ARCHIVED_ONLY.value,
                }:
                    raise ValueError(
                        f"feedback parent review is not available for evolution: {request.feedback_id}"
                    )
                existing_application = conn.execute(
                    """
                    SELECT *
                    FROM feedback_applications
                    WHERE feedback_id = ?
                      AND target_type = ?
                      AND target_id = ?
                      AND consumed_by_method = ?
                      AND COALESCE(consumed_in_job_id, '') = COALESCE(?, '')
                    """,
                    (
                        request_payload["feedback_id"],
                        request_payload["target_type"],
                        request_payload["target_id"],
                        request_payload["consumed_by_method"],
                        request_payload["consumed_in_job_id"],
                    ),
                ).fetchone()
                if existing_application is not None:
                    if existing_application["effect_summary"] != request_payload["effect_summary"]:
                        raise ValueError(
                            "feedback application already exists with a different effect summary: "
                            f"{request.feedback_id}"
                        )
                    conn.commit()
                    return _feedback_application_response_from_row(existing_application)
                conn.execute(
                    """
                    INSERT INTO feedback_applications (
                        application_id, feedback_id, target_type, target_id,
                        consumed_by_method, consumed_in_job_id, effect_summary,
                        created_at
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        application_id,
                        request_payload["feedback_id"],
                        request_payload["target_type"],
                        request_payload["target_id"],
                        request_payload["consumed_by_method"],
                        request_payload["consumed_in_job_id"],
                        request_payload["effect_summary"],
                        now,
                    ),
                )
                if feedback_row["status"] == HumanFeedbackStatus.AVAILABLE_FOR_EVOLUTION.value:
                    conn.execute(
                        """
                        UPDATE human_feedback
                        SET status = ?
                        WHERE feedback_id = ?
                        """,
                        (HumanFeedbackStatus.CONSUMED.value, request.feedback_id),
                    )
                conn.commit()
            except Exception:
                try:
                    conn.rollback()
                except sqlite3.Error:
                    pass
                raise
        with self.connect() as conn:
            row = conn.execute(
                "SELECT * FROM feedback_applications WHERE application_id = ?",
                (application_id,),
            ).fetchone()
        if row is None:
            raise ValueError(f"unknown feedback application: {application_id}")
        return _feedback_application_response_from_row(row)

    def list_feedback_applications(
        self,
        *,
        feedback_id: str | None = None,
    ) -> list[FeedbackApplicationResponse]:
        with self.connect() as conn:
            if feedback_id is not None:
                if self._feedback_row(conn, feedback_id) is None:
                    raise ValueError(f"unknown feedback: {feedback_id}")
                rows = conn.execute(
                    """
                    SELECT * FROM feedback_applications
                    WHERE feedback_id = ?
                    ORDER BY created_at ASC, application_id ASC
                    """,
                    (feedback_id,),
                ).fetchall()
            else:
                rows = conn.execute(
                    """
                    SELECT * FROM feedback_applications
                    ORDER BY created_at ASC, application_id ASC
                    """
                ).fetchall()
        return [_feedback_application_response_from_row(row) for row in rows]

    def create_human_query_decision(
        self,
        request: HumanQueryDecisionCreateRequest,
    ) -> HumanQueryDecisionResponse:
        raw_payload = request.model_dump(mode="python")
        _validate_finite_floats(raw_payload, "request")
        request_payload = request.model_dump(mode="json")
        query_decision_id = new_id("hqd")
        now = utc_now_iso()
        with self.connect() as conn:
            try:
                conn.execute("BEGIN IMMEDIATE")
                _insert_human_query_decision_row(
                    conn,
                    query_decision_id=query_decision_id,
                    request_payload=request_payload,
                    review_id=None,
                    created_at=now,
                )
                conn.commit()
            except Exception:
                try:
                    conn.rollback()
                except sqlite3.Error:
                    pass
                raise
        return self.get_human_query_decision(query_decision_id)

    def get_human_query_decision(
        self,
        query_decision_id: str,
    ) -> HumanQueryDecisionResponse:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT * FROM human_query_decisions WHERE query_decision_id = ?",
                (query_decision_id,),
            ).fetchone()
        if row is None:
            raise ValueError(f"unknown query decision: {query_decision_id}")
        return _human_query_decision_response_from_row(row)

    def _review_request_row(
        self,
        conn: sqlite3.Connection,
        review_id: str,
    ) -> sqlite3.Row | None:
        return conn.execute(
            """
            SELECT rr.*, rp.packet_json
            FROM review_requests rr
            JOIN review_packets rp ON rp.packet_id = rr.packet_id
            WHERE rr.review_id = ?
            """,
            (review_id,),
        ).fetchone()

    def _feedback_row(
        self,
        conn: sqlite3.Connection,
        feedback_id: str,
    ) -> sqlite3.Row | None:
        return conn.execute(
            "SELECT * FROM human_feedback WHERE feedback_id = ?",
            (feedback_id,),
        ).fetchone()

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
        return self._register_artifact(
            request,
            initial_state=ArtifactState.ACTIVE,
        )

    def _register_artifact(
        self,
        request: ArtifactRegisterRequest,
        *,
        initial_state: ArtifactState,
        staging_job_id: str | None = None,
    ) -> ArtifactResponse:
        if (initial_state is ArtifactState.STAGED) != (staging_job_id is not None):
            raise ValueError("staged artifact ownership must match its state")
        raw_payload = request.model_dump(mode="python")
        for field in ("manifest", "lineage", "compatibility", "scores", "tags"):
            _validate_finite_floats(raw_payload[field], field)

        request_payload = request.model_dump(mode="json")
        artifact_type = str(request_payload["type"])
        if artifact_type == str(ArtifactType.AGENT_SYSTEM):
            manifest = dict(request_payload["manifest"])
            manifest["target_path"] = normalize_agent_system_target_path(
                manifest.get("target_path")
            )
            request_payload["manifest"] = manifest
        lineage_json = _json_dumps(request_payload["lineage"])
        manifest_json = _json_dumps(request_payload["manifest"])
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
                            manifest_path, manifest_json, lineage_json,
                            compatibility_json, scores_json, tags_json, promoted,
                            staging_job_id
                        )
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            artifact_id,
                            artifact_type,
                            request_payload["name"],
                            1,
                            str(initial_state),
                            created_at,
                            request_payload["uri"],
                            str(manifest_path),
                            manifest_json,
                            lineage_json,
                            compatibility_json,
                            scores_json,
                            tags_json,
                            1 if request_payload["promoted"] else 0,
                            staging_job_id,
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
                            manifest_path.with_name(
                                f".{manifest_path.name}.pending"
                            ).unlink(missing_ok=True)
                        except OSError:
                            pass
                    raise
            return ArtifactResponse(
                artifact_id=artifact_id,
                type=artifact_type,
                name=request_payload["name"],
                version=1,
                state=initial_state,
                uri=request_payload["uri"],
                manifest=request_payload["manifest"],
                compatibility=request_payload["compatibility"],
                scores=request_payload["scores"],
                tags=request_payload["tags"],
                promoted=request_payload["promoted"],
            )
        raise RuntimeError("could not allocate unique artifact id")

    def get_artifact(self, artifact_id: str) -> ArtifactResponse:
        with self.connect() as conn:
            row = conn.execute(
                """
                SELECT * FROM artifacts
                WHERE artifact_id = ? AND state NOT IN (?, ?)
                """,
                (
                    artifact_id,
                    str(ArtifactState.STAGED),
                    str(ArtifactState.SEALED),
                ),
            ).fetchone()
        if row is None:
            raise ValueError(f"unknown artifact: {artifact_id}")
        return _artifact_response_from_row(row)

    def get_internal_successor_artifact(
        self,
        successor_transition_id: str,
        artifact_id: str,
    ) -> ArtifactResponse:
        """Read one exact head-owned output without making it fallback-eligible."""

        with self.connect() as conn:
            try:
                conn.execute("BEGIN")
                _require_successor_transition_not_discarded(
                    conn,
                    successor_transition_id,
                )
                row = conn.execute(
                    "SELECT * FROM artifacts "
                    "WHERE artifact_id = ?",
                    (artifact_id,),
                ).fetchone()
                if (
                    row is None
                    or row["state"]
                    != str(ArtifactState.SEALED)
                    or not isinstance(
                        row["staging_job_id"],
                        str,
                    )
                ):
                    raise ValueError(
                        "sealed transition artifact authority is invalid"
                    )
                job_id = str(row["staging_job_id"])
                job_row = conn.execute(
                    "SELECT * FROM jobs WHERE job_id = ?",
                    (job_id,),
                ).fetchone()
                binding = conn.execute(
                    "SELECT successor_transition_id "
                    "FROM plan_bound_job_transition_bindings "
                    "WHERE job_id = ?",
                    (job_id,),
                ).fetchone()
                if (
                    job_row is None
                    or job_row["state"]
                    != str(JobState.SUCCEEDED)
                    or binding is None
                    or binding["successor_transition_id"]
                    != successor_transition_id
                ):
                    raise ValueError(
                        "sealed transition artifact authority is invalid"
                    )
                validated = (
                    self._validate_plan_bound_job_contract(
                        conn,
                        job_row,
                    )
                )
                if (
                    validated.successor_transition_id
                    != successor_transition_id
                ):
                    raise ValueError(
                        "sealed transition artifact authority is invalid"
                    )
                _require_sealed_transition_artifact_authority(
                    row,
                    job_id=job_id,
                    validated=validated,
                )
                conn.commit()
            except BaseException:
                try:
                    conn.rollback()
                except sqlite3.Error:
                    pass
                raise
        return _artifact_response_from_row(row)

    def discard_successor_transition_outputs(
        self,
        successor_transition_id: str,
    ) -> dict[str, object]:
        """Idempotently remove non-active outputs after abandon/supersession."""

        if (
            not isinstance(successor_transition_id, str)
            or re.fullmatch(
                r"[A-Za-z0-9][A-Za-z0-9._-]{0,255}",
                successor_transition_id,
            )
            is None
        ):
            raise ValueError("successor transition identity is invalid")
        if self._bound_store_id is None:
            raise ValueError("evolution store identity has not been initialized")

        manifest_paths: list[Path] = []
        artifact_ids: list[str] = []
        materialized_context_ids: list[str] = []
        discard_receipts: list[PersistedContextDiscardReceipt] = []
        orphan_materializations: list[_OrphanMaterialization] = []
        expected_context_snapshots: dict[str, bytes] = {}
        persisted_receipt: dict[str, object] | None = None
        materializer = self._context_materializer
        with self._locked_context_materialization_root() as materialization_root_fd:
            with self.connect() as conn:
                try:
                    conn.execute("BEGIN IMMEDIATE")
                    self._verify_bound_store_identity(conn)
                    self._verify_bound_materialization_root(
                        materialization_root_fd
                    )
                    orphan_materializations = (
                        self._orphan_context_materializations(
                            conn,
                            materialization_root_fd,
                        )
                    )
                    self._verify_referenced_context_materializations(
                        conn,
                        materialization_root_fd,
                    )
                    materializations = [
                        self._materialized_context_from_row(row)
                        for row in _read_guarded_materialized_context_rows(
                            conn,
                            label=(
                                "successor transition materialization "
                                "discard"
                            ),
                        )
                    ]
                    selected_materializations = [
                        manifest
                        for manifest in materializations
                        if manifest.successor_transition_id
                        == successor_transition_id
                    ]
                    existing_receipt = (
                        _load_successor_transition_discard(
                            conn,
                            successor_transition_id,
                        )
                    )
                    bound_job_rows = conn.execute(
                        "SELECT jobs.job_id, jobs.state "
                        "FROM jobs JOIN "
                        "plan_bound_job_transition_bindings AS binding "
                        "ON binding.job_id = jobs.job_id "
                        "WHERE binding.successor_transition_id = ? "
                        "ORDER BY jobs.job_id LIMIT ?",
                        (
                            successor_transition_id,
                            (
                                MAX_PLAN_BOUND_JOB_TRANSITION_BINDINGS
                                + 1
                            ),
                        ),
                    ).fetchall()
                    if (
                        len(bound_job_rows)
                        > MAX_PLAN_BOUND_JOB_TRANSITION_BINDINGS
                    ):
                        raise ValueError(
                            "transition job inventory exceeds its bound"
                        )
                    retained_output = conn.execute(
                        "SELECT 1 FROM artifacts JOIN "
                        "plan_bound_job_transition_bindings AS binding "
                        "ON binding.job_id = artifacts.staging_job_id "
                        "WHERE binding.successor_transition_id = ? "
                        "LIMIT 1",
                        (successor_transition_id,),
                    ).fetchone()
                    active_job_ids = [
                        str(row["job_id"])
                        for row in bound_job_rows
                        if str(row["state"])
                        in {
                            str(JobState.PENDING),
                            str(JobState.CLAIMED),
                            str(JobState.RUNNING),
                        }
                    ]
                    if existing_receipt is not None:
                        if (
                            selected_materializations
                            or retained_output is not None
                            or active_job_ids
                        ):
                            raise ValueError(
                                "discarded successor transition "
                                "regained output authority"
                            )
                        persisted_receipt = existing_receipt
                        selected_materializations = []
                        bound_job_rows = []
                        active_job_ids = []
                    if selected_materializations and materializer is None:
                        raise ValueError(
                            "transition materialization discard requires "
                            "a verified executable registry"
                        )
                    for manifest in selected_materializations:
                        if manifest.predecessor_project_head_id is None:
                            raise ValueError(
                                "transition materialization lost its "
                                "predecessor authority"
                            )
                        if (
                            conn.execute(
                                "SELECT 1 FROM revisions "
                                "WHERE context_id = ? LIMIT 1",
                                (manifest.context_id,),
                            ).fetchone()
                            is not None
                        ):
                            raise ValueError(
                                "transition materialization was already "
                                "consumed"
                            )
                        assert materializer is not None
                        discard_receipts.append(
                            materializer.prepare_persisted_discard(
                                manifest,
                                materialization_root_descriptor=(
                                    materialization_root_fd
                                ),
                            )
                        )
                        materialized_context_ids.append(
                            manifest.context_id
                        )

                    for row in bound_job_rows:
                        manifest_paths.extend(
                            self._delete_staged_artifacts_for_job(
                                conn,
                                str(row["job_id"]),
                            )
                        )
                    if active_job_ids:
                        placeholders = ",".join(
                            "?" for _ in active_job_ids
                        )
                        cancelled = conn.execute(
                            f"UPDATE jobs SET state = ?, "
                            f"updated_at = ?, error = ?, "
                            f"claimed_by = NULL, lease_id = NULL, "
                            f"lease_expires_at = NULL, "
                            f"lease_duration_seconds = NULL "
                            f"WHERE job_id IN ({placeholders}) "
                            f"AND state IN (?, ?, ?)",
                            (
                                str(JobState.CANCELLED),
                                utc_now_iso(),
                                (
                                    "successor transition outputs "
                                    "were discarded"
                                ),
                                *active_job_ids,
                                str(JobState.PENDING),
                                str(JobState.CLAIMED),
                                str(JobState.RUNNING),
                            ),
                        )
                        if cancelled.rowcount != len(active_job_ids):
                            raise ValueError(
                                "transition job lifecycle changed "
                                "during discard"
                            )
                    rows = conn.execute(
                        """
                        SELECT artifacts.artifact_id,
                               artifacts.manifest_path
                        FROM artifacts
                        JOIN plan_bound_job_transition_bindings AS binding
                          ON binding.job_id = artifacts.staging_job_id
                        JOIN jobs
                          ON jobs.job_id = artifacts.staging_job_id
                        WHERE binding.successor_transition_id = ?
                          AND artifacts.state = ?
                          AND jobs.state = ?
                        ORDER BY artifacts.artifact_id
                        LIMIT ?
                        """,
                        (
                            successor_transition_id,
                            str(ArtifactState.SEALED),
                            str(JobState.SUCCEEDED),
                            MAX_INTERNAL_JOB_OUTPUTS + 1,
                        ),
                    ).fetchall()
                    if len(rows) > MAX_INTERNAL_JOB_OUTPUTS:
                        raise ValueError(
                            "transition output inventory exceeds its bound"
                        )
                    artifact_ids = [
                        str(row["artifact_id"])
                        for row in rows
                    ]
                    if artifact_ids:
                        placeholders = ",".join(
                            "?" for _ in artifact_ids
                        )
                        downstream = conn.execute(
                            f"""
                            SELECT 1
                            FROM artifact_lineage
                            WHERE parent_artifact_id IN ({placeholders})
                            LIMIT 1
                            """,
                            artifact_ids,
                        ).fetchone()
                        if downstream is not None:
                            raise ValueError(
                                "transition output was already consumed"
                            )
                        conn.execute(
                            f"""
                            DELETE FROM artifact_lineage
                            WHERE child_artifact_id IN ({placeholders})
                            """,
                            artifact_ids,
                        )
                        deleted = conn.execute(
                            f"""
                            DELETE FROM artifacts
                            WHERE artifact_id IN ({placeholders})
                              AND state = ?
                            """,
                            (
                                *artifact_ids,
                                str(ArtifactState.SEALED),
                            ),
                        )
                        if deleted.rowcount != len(artifact_ids):
                            raise ValueError(
                                "transition output lifecycle changed "
                                "during discard"
                            )
                        manifest_paths = [
                            *manifest_paths,
                            *(
                                Path(str(row["manifest_path"]))
                                for row in rows
                            ),
                        ]
                    if materialized_context_ids:
                        placeholders = ",".join(
                            "?" for _ in materialized_context_ids
                        )
                        deleted_contexts = conn.execute(
                            f"DELETE FROM contexts "
                            f"WHERE context_id IN ({placeholders})",
                            materialized_context_ids,
                        )
                        if (
                            deleted_contexts.rowcount
                            != len(materialized_context_ids)
                        ):
                            raise ValueError(
                                "transition materialization lifecycle "
                                "changed during discard"
                            )
                    if (
                        conn.execute(
                            "SELECT 1 FROM artifacts JOIN "
                            "plan_bound_job_transition_bindings "
                            "AS binding ON binding.job_id = "
                            "artifacts.staging_job_id "
                            "WHERE binding.successor_transition_id = ? "
                            "LIMIT 1",
                            (successor_transition_id,),
                        ).fetchone()
                        is not None
                    ):
                        raise ValueError(
                            "transition output lifecycle is invalid "
                            "during discard"
                        )
                    if existing_receipt is None:
                        persisted_receipt = (
                            _successor_transition_discard_receipt(
                                successor_transition_id,
                                artifact_ids=artifact_ids,
                                materialized_context_ids=(
                                    materialized_context_ids
                                ),
                            )
                        )
                        receipt_json = (
                            _successor_transition_discard_receipt_json(
                                persisted_receipt
                            )
                        )
                        discarded_at = utc_now_iso()
                        inventory = conn.execute(
                            "SELECT COUNT(*) AS row_count, "
                            "COALESCE(SUM("
                            "length(CAST(successor_transition_id AS BLOB)) "
                            "+ length(CAST(receipt_sha256 AS BLOB)) "
                            "+ length(CAST(receipt_json AS BLOB)) "
                            "+ length(CAST(discarded_at AS BLOB))"
                            "), 0) AS total_bytes "
                            "FROM successor_transition_discards"
                        ).fetchone()
                        assert inventory is not None
                        added_bytes = (
                            len(
                                successor_transition_id.encode(
                                    "utf-8"
                                )
                            )
                            + 64
                            + len(receipt_json.encode("utf-8"))
                            + len(discarded_at.encode("utf-8"))
                        )
                        if (
                            int(inventory["row_count"])
                            >= MAX_SUCCESSOR_TRANSITION_DISCARDS
                            or int(inventory["total_bytes"])
                            + added_bytes
                            > (
                                MAX_SUCCESSOR_TRANSITION_DISCARD_AGGREGATE_BYTES
                            )
                        ):
                            raise ValueError(
                                "successor transition discard capacity "
                                "is exhausted"
                            )
                        conn.execute(
                            "INSERT INTO "
                            "successor_transition_discards("
                            "successor_transition_id, "
                            "receipt_sha256, receipt_json, discarded_at"
                            ") VALUES (?, ?, ?, ?)",
                            (
                                successor_transition_id,
                                hashlib.sha256(
                                    receipt_json.encode("utf-8")
                                ).hexdigest(),
                                receipt_json,
                                discarded_at,
                            ),
                        )
                        if (
                            _load_successor_transition_discard(
                                conn,
                                successor_transition_id,
                            )
                            != persisted_receipt
                        ):
                            raise ValueError(
                                "successor transition discard receipt "
                                "changed during commit"
                            )
                    expected_context_snapshots = (
                        self._expected_context_snapshot_bytes(conn)
                    )
                    manifest_paths.extend(
                        self._orphan_managed_artifact_manifests(
                            conn
                        )
                    )
                    self._verify_bound_store_identity(conn)
                    self._verify_bound_materialization_root(
                        materialization_root_fd
                    )
                    conn.commit()
                except BaseException:
                    try:
                        conn.rollback()
                    except sqlite3.Error:
                        pass
                    raise

            if materializer is not None:
                for receipt in discard_receipts:
                    result = materializer.discard_persisted(
                        receipt,
                        materialization_root_descriptor=(
                            materialization_root_fd
                        ),
                    )
                    if result == "mismatch":
                        raise ValueError(
                            "transition materialization identity changed "
                            "during discard"
                        )
            self._remove_orphan_context_materializations(
                orphan_materializations,
                materialization_root_fd,
            )
            with self._opened_bound_artifact_root() as artifact_root_fd:
                reconcile_context_snapshots(
                    artifact_root_fd,
                    expected_context_snapshots,
                )
            self._verify_bound_materialization_root(
                materialization_root_fd
            )
            with self.connect() as conn:
                self._verify_bound_store_identity(conn)
        self._unlink_artifact_manifests(
            list(dict.fromkeys(manifest_paths))
        )
        if persisted_receipt is None:
            raise ValueError(
                "successor transition discard receipt was not committed"
            )
        return persisted_receipt

    def update_artifact_promotion(
        self,
        artifact_id: str,
        *,
        promoted: bool,
    ) -> ArtifactResponse:
        with self.connect() as conn:
            try:
                conn.execute("BEGIN IMMEDIATE")
                row = conn.execute(
                    """
                    SELECT * FROM artifacts
                    WHERE artifact_id = ? AND state NOT IN (?, ?)
                    """,
                    (
                        artifact_id,
                        str(ArtifactState.STAGED),
                        str(ArtifactState.SEALED),
                    ),
                ).fetchone()
                if row is None:
                    raise ValueError(f"unknown artifact: {artifact_id}")
                manifest_path = Path(str(row["manifest_path"]))
                try:
                    manifest_payload = json.loads(manifest_path.read_text(encoding="utf-8"))
                except FileNotFoundError as exc:
                    raise ValueError(
                        f"artifact {artifact_id} manifest file is missing: {manifest_path}"
                    ) from exc
                except json.JSONDecodeError as exc:
                    raise ValueError(
                        f"artifact {artifact_id} manifest file is not valid JSON: {manifest_path}"
                    ) from exc
                if not isinstance(manifest_payload, dict):
                    raise ValueError(
                        f"artifact {artifact_id} manifest file is not a JSON object: "
                        f"{manifest_path}"
                    )
                manifest_payload["promoted"] = bool(promoted)
                manifest_path.write_text(
                    _json_dumps(manifest_payload, indent=2),
                    encoding="utf-8",
                )
                conn.execute(
                    "UPDATE artifacts SET promoted = ? WHERE artifact_id = ?",
                    (1 if promoted else 0, artifact_id),
                )
                conn.commit()
            except Exception:
                try:
                    conn.rollback()
                except sqlite3.Error:
                    pass
                raise
        return self.get_artifact(artifact_id)

    def update_artifact_promotion_from_request(
        self,
        artifact_id: str,
        request: ArtifactPromotionUpdateRequest,
    ) -> ArtifactResponse:
        return self.update_artifact_promotion(
            artifact_id,
            promoted=request.promoted,
        )

    def _promoted_artifact_rows(
        self,
        *,
        maximum: int | None = None,
        artifact_types: set[str] | None = None,
        artifact_ids: set[str] | None = None,
    ) -> list[dict[str, object]]:
        type_clause = ""
        type_parameters: tuple[str, ...] = ()
        if artifact_types is not None:
            ordered_types = tuple(sorted(artifact_types))
            if not ordered_types:
                return []
            type_clause = " AND type IN (" + ", ".join("?" for _ in ordered_types) + ")"
            type_parameters = ordered_types
        artifact_clause = ""
        artifact_parameters: tuple[str, ...] = ()
        if artifact_ids is not None:
            ordered_artifact_ids = tuple(sorted(artifact_ids))
            if not ordered_artifact_ids:
                return []
            artifact_clause = (
                " AND artifact_id IN (" + ", ".join("?" for _ in ordered_artifact_ids) + ")"
            )
            artifact_parameters = ordered_artifact_ids
        parameters: tuple[object, ...] = (
            str(ArtifactState.ACTIVE),
            str(ArtifactState.EXPERIMENTAL),
            *type_parameters,
            *artifact_parameters,
        )
        projection_eligible_expression = """
            uri LIKE 'file:%'
            AND length(CAST(uri AS BLOB)) <= ?
            AND length(CAST(name AS BLOB)) <= ?
            AND manifest_json IS NOT NULL
            AND manifest_json <> ''
            AND length(CAST(manifest_json AS BLOB)) <= ?
            AND length(CAST(compatibility_json AS BLOB)) <= ?
            AND json_valid(compatibility_json) = 1
            AND substr(ltrim(compatibility_json), 1, 1) = '{'
            AND length(CAST(scores_json AS BLOB)) <= ?
            AND json_valid(scores_json) = 1
            AND substr(ltrim(scores_json), 1, 1) = '{'
        """
        projection_compatibility_expression = f"""
            length(CAST(compatibility_json AS BLOB)) <= {MAX_ARTIFACT_ROUTING_JSON_BYTES}
            AND json_valid(compatibility_json) = 1
            AND substr(ltrim(compatibility_json), 1, 1) = '{{'
        """
        with self.connect() as conn:
            if maximum is not None:
                eligible_rows = conn.execute(
                    f"""
                    SELECT artifact_id, type, name, state, created_at, uri,
                           manifest_json, compatibility_json, scores_json, promoted
                    FROM artifacts
                    WHERE promoted = 1 AND state IN (?, ?)
                    {type_clause}
                    {artifact_clause}
                      AND ({projection_eligible_expression})
                    LIMIT ?
                    """,  # noqa: S608 - placeholders bind every dynamic value.
                    (
                        *parameters,
                        MAX_CONTEXT_ARTIFACT_URI_BYTES,
                        MAX_CONTEXT_ARTIFACT_NAME_BYTES,
                        MAX_CONTRACT_JSON_BYTES,
                        MAX_ARTIFACT_ROUTING_JSON_BYTES,
                        MAX_ARTIFACT_ROUTING_JSON_BYTES,
                        maximum + 1,
                    ),
                ).fetchall()
                if len(eligible_rows) > maximum:
                    raise ValueError("context projection exceeds the promoted candidate budget")
                remaining = maximum - len(eligible_rows)
                skipped_rows = []
                if remaining:
                    skipped_rows = conn.execute(
                        f"""
                        SELECT artifact_id, type, artifact_id AS name, state,
                               created_at, '' AS uri, NULL AS manifest_json,
                               CASE
                                   WHEN ({projection_compatibility_expression})
                                       THEN compatibility_json
                                   ELSE NULL
                               END AS compatibility_json,
                               '{{}}' AS scores_json,
                               promoted,
                               CASE
                                   WHEN NOT ({projection_compatibility_expression})
                                       THEN 'metadata_policy_rejected'
                                   WHEN manifest_json IS NULL OR manifest_json = ''
                                       THEN 'unbound_legacy_metadata'
                                   WHEN lower(substr(uri, 1, 5)) <> 'file:'
                                       THEN 'unsupported_uri_scheme'
                                   ELSE 'metadata_policy_rejected'
                               END AS projection_skip_reason
                        FROM artifacts
                        WHERE promoted = 1 AND state IN (?, ?)
                        {type_clause}
                        {artifact_clause}
                          AND NOT ({projection_eligible_expression})
                        LIMIT ?
                        """,  # noqa: S608 - placeholders bind every dynamic value.
                        (
                            *parameters,
                            MAX_CONTEXT_ARTIFACT_URI_BYTES,
                            MAX_CONTEXT_ARTIFACT_NAME_BYTES,
                            MAX_CONTRACT_JSON_BYTES,
                            MAX_ARTIFACT_ROUTING_JSON_BYTES,
                            MAX_ARTIFACT_ROUTING_JSON_BYTES,
                            remaining,
                        ),
                    ).fetchall()
                rows = [*eligible_rows, *skipped_rows]
            else:
                rows = conn.execute(
                    f"""
                    SELECT * FROM artifacts
                    WHERE promoted = 1 AND state IN (?, ?)
                    {type_clause}
                    {artifact_clause}
                    """,  # noqa: S608 - placeholders bind every dynamic value.
                    parameters,
                ).fetchall()
        return [dict(row) for row in rows]

    def _requested_context_artifact_rows(
        self,
        artifact_ids: tuple[str, ...],
    ) -> list[dict[str, object]]:
        if not artifact_ids:
            return []
        if len(artifact_ids) > 256 or any(
            not artifact_id or len(artifact_id.encode("utf-8")) > 256
            for artifact_id in artifact_ids
        ):
            raise ValueError("requested context artifact inventory is invalid")
        if len(artifact_ids) != len(set(artifact_ids)):
            raise ValueError("requested context artifact inventory contains duplicates")
        placeholders = ", ".join("?" for _ in artifact_ids)
        with self.connect() as conn:
            rows = conn.execute(
                f"""
                SELECT * FROM artifacts
                WHERE state IN (?, ?)
                  AND artifact_id IN ({placeholders})
                """,  # noqa: S608 - placeholders bind every artifact identity.
                (
                    str(ArtifactState.ACTIVE),
                    str(ArtifactState.EXPERIMENTAL),
                    *artifact_ids,
                ),
            ).fetchall()
        values = {str(row["artifact_id"]): dict(row) for row in rows}
        if set(values) != set(artifact_ids):
            raise ValueError("requested context artifact is unavailable")
        return [values[artifact_id] for artifact_id in artifact_ids]

    def _successor_transition_artifact_rows(
        self,
        successor_transition_id: str,
        artifact_ids: tuple[str, ...],
        *,
        artifact_types: set[str],
        owner_transition_ids: tuple[str, ...] | None = None,
    ) -> list[dict[str, object]]:
        if (
            not artifact_ids
            or len(artifact_ids) > MAX_CONTEXT_PROJECTION_CANDIDATES
            or len(artifact_ids) != len(set(artifact_ids))
            or any(
                not artifact_id
                or len(artifact_id.encode("utf-8")) > 256
                for artifact_id in artifact_ids
            )
        ):
            raise ValueError(
                "successor context artifact inventory is invalid"
            )
        if owner_transition_ids is None:
            owner_transition_ids = tuple(
                successor_transition_id for _ in artifact_ids
            )
        if (
            len(owner_transition_ids) != len(artifact_ids)
            or any(
                not transition_id
                or len(transition_id.encode("utf-8")) > 256
                for transition_id in owner_transition_ids
            )
        ):
            raise ValueError(
                "successor context artifact authority is invalid"
            )
        expected_owners = dict(
            zip(artifact_ids, owner_transition_ids, strict=True)
        )
        ordered_types = tuple(sorted(artifact_types))
        if not ordered_types:
            raise ValueError(
                "successor context has no executable artifact types"
            )
        artifact_placeholders = ", ".join("?" for _ in artifact_ids)
        type_placeholders = ", ".join("?" for _ in ordered_types)
        with self.connect() as conn:
            rows = conn.execute(
                f"""
                SELECT artifacts.artifact_id, artifacts.type,
                       artifacts.name, artifacts.state,
                       artifacts.created_at, artifacts.uri,
                       artifacts.manifest_json,
                       artifacts.compatibility_json,
                       artifacts.scores_json, artifacts.promoted,
                       binding.successor_transition_id
                         AS sealed_owner_transition_id
                FROM artifacts
                LEFT JOIN plan_bound_job_transition_bindings AS binding
                  ON binding.job_id = artifacts.staging_job_id
                LEFT JOIN jobs
                  ON jobs.job_id = artifacts.staging_job_id
                WHERE artifacts.artifact_id IN ({artifact_placeholders})
                  AND artifacts.type IN ({type_placeholders})
                  AND artifacts.promoted = 1
                  AND (
                    (
                      artifacts.state = ?
                      AND jobs.state = ?
                    )
                    OR artifacts.state IN (?, ?)
                  )
                  AND artifacts.uri LIKE 'file:%'
                  AND length(CAST(artifacts.uri AS BLOB)) <= ?
                  AND length(CAST(artifacts.name AS BLOB)) <= ?
                  AND artifacts.manifest_json IS NOT NULL
                  AND artifacts.manifest_json <> ''
                  AND length(
                        CAST(artifacts.manifest_json AS BLOB)
                      ) <= ?
                  AND json_valid(artifacts.manifest_json) = 1
                  AND substr(
                        ltrim(artifacts.manifest_json), 1, 1
                      ) = '{{'
                  AND length(
                        CAST(artifacts.compatibility_json AS BLOB)
                      ) <= ?
                  AND json_valid(artifacts.compatibility_json) = 1
                  AND substr(
                        ltrim(artifacts.compatibility_json), 1, 1
                      ) = '{{'
                  AND length(
                        CAST(artifacts.scores_json AS BLOB)
                      ) <= ?
                  AND json_valid(artifacts.scores_json) = 1
                  AND substr(
                        ltrim(artifacts.scores_json), 1, 1
                      ) = '{{'
                """,  # noqa: S608 - placeholders bind both closed inventories.
                (
                    *artifact_ids,
                    *ordered_types,
                    str(ArtifactState.SEALED),
                    str(JobState.SUCCEEDED),
                    str(ArtifactState.ACTIVE),
                    str(ArtifactState.EXPERIMENTAL),
                    MAX_CONTEXT_ARTIFACT_URI_BYTES,
                    MAX_CONTEXT_ARTIFACT_NAME_BYTES,
                    MAX_CONTRACT_JSON_BYTES,
                    MAX_ARTIFACT_ROUTING_JSON_BYTES,
                    MAX_ARTIFACT_ROUTING_JSON_BYTES,
                ),
            ).fetchall()
        values = {
            str(row["artifact_id"]): dict(row)
            for row in rows
        }
        if set(values) != set(artifact_ids):
            raise ValueError(
                "successor context artifact is unavailable"
            )
        for artifact_id, row in values.items():
            state = str(row["state"])
            if (
                state == str(ArtifactState.SEALED)
                and row.get("sealed_owner_transition_id")
                != expected_owners[artifact_id]
            ):
                raise ValueError(
                    "successor context artifact is unavailable"
                )
        return [values[artifact_id] for artifact_id in artifact_ids]

    def resolve_context_projections(
        self,
        request: ContextProjectionResolveRequest,
    ) -> ContextProjectionResolveResponse:
        raw_payload = request.model_dump(mode="python")
        _validate_finite_floats(raw_payload, "request")
        resolver = self._context_projection_resolver
        if resolver is None:
            raise ValueError("context projection requires a verified executable registry")
        registry = self._executable_registry
        if registry is None:  # Kept explicit for type narrowing and fail-closed wiring.
            raise ValueError("context projection requires a verified executable registry")
        requested_artifact_ids = requested_context_artifact_ids(request.compatibility_facts())
        rows = self._promoted_artifact_rows(
            maximum=MAX_CONTEXT_PROJECTION_CANDIDATES,
            artifact_types={target.artifact_type for target in registry.snapshot.targets.values()},
            artifact_ids=requested_artifact_ids,
        )
        for _ in range(MAX_CONTEXT_ID_ATTEMPTS):
            context_id = new_id("ctx")
            response = resolver.resolve(
                request,
                rows,
                context_id=context_id,
            )
            request_payload = request.model_dump(mode="json")
            response_payload = response.model_dump(mode="json")
            selected_ids = list(response.selection.artifact_ids)
            if self._persist_context(
                context_id=context_id,
                request_payload=request_payload,
                response_payload=response_payload,
                selected_ids=selected_ids,
            ):
                return response
        raise RuntimeError("could not allocate unique context id")

    def resolve_materialized_context(
        self,
        request: ContextProjectionResolveRequest,
    ) -> MaterializedContext:
        """Resolve and atomically materialize one registry-bound internal context."""

        raw_payload = request.model_dump(mode="python")
        _validate_finite_floats(raw_payload, "request")
        resolver = self._context_projection_resolver
        materializer = self._context_materializer
        registry = self._executable_registry
        if resolver is None or materializer is None or registry is None:
            raise ValueError("materialized context requires a verified executable registry")
        requested_artifact_ids = requested_context_artifact_ids(request.compatibility_facts())
        artifact_types = {
            target.artifact_type
            for target in registry.snapshot.targets.values()
        }
        sealed_artifact_authority: dict[str, str] | None = None
        if request.successor_transition_id is not None:
            with self.connect() as conn:
                _require_successor_transition_not_discarded(
                    conn,
                    request.successor_transition_id,
                )
            evolution = request.metadata.evolution
            ordered_artifact_ids = (
                None
                if evolution is None
                else evolution.context_artifact_ids
            )
            if ordered_artifact_ids is None:
                raise ValueError(
                    "successor materialization requires exact artifacts"
                )
            owner_transition_ids = (
                evolution.context_artifact_owner_transition_ids
            )
            rows = self._successor_transition_artifact_rows(
                request.successor_transition_id,
                ordered_artifact_ids,
                artifact_types=artifact_types,
                owner_transition_ids=owner_transition_ids,
            )
            sealed_artifact_authority = {
                str(row["artifact_id"]): str(
                    row["sealed_owner_transition_id"]
                )
                for row in rows
                if str(row["state"]) == str(ArtifactState.SEALED)
            }
        else:
            rows = self._promoted_artifact_rows(
                maximum=MAX_CONTEXT_PROJECTION_CANDIDATES,
                artifact_types=artifact_types,
                artifact_ids=requested_artifact_ids,
            )
        for _ in range(MAX_CONTEXT_ID_ATTEMPTS):
            context_id = new_id("ctx")
            response = resolver.resolve(
                request,
                rows,
                context_id=context_id,
                sealed_artifact_authority=sealed_artifact_authority,
            )
            with self._locked_context_materialization_root() as materialization_root_fd:
                with self.connect() as conn:
                    self._verify_bound_store_identity(conn)
                try:
                    publication = materializer.materialize_for_publication(
                        request,
                        response,
                        rows,
                        materialization_root_descriptor=materialization_root_fd,
                        sealed_artifact_authority=(
                            sealed_artifact_authority
                        ),
                    )
                except ValueError as exc:
                    if str(exc) == "materialized context already exists":
                        continue
                    raise
                result = publication.materialized_context

                def verify_publication() -> None:
                    self._verify_bound_materialization_root(materialization_root_fd)
                    materializer.verify_publication(
                        publication,
                        materialization_root_descriptor=materialization_root_fd,
                    )

                try:
                    self._verify_bound_materialization_root(materialization_root_fd)
                    persisted = self._persist_context(
                        context_id=context_id,
                        request_payload=request.model_dump(mode="json"),
                        response_payload=result.model_dump(mode="json"),
                        selected_ids=list(result.selection.artifact_ids),
                        materialization=result,
                        precommit=verify_publication,
                    )
                except BaseException as exc:
                    try:
                        persistence_state = self._materialized_context_persistence_state(
                            context_id,
                            result,
                        )
                    except (OSError, sqlite3.Error, ValueError) as state_error:
                        persistence_state = "unknown"
                        exc.add_note(
                            "materialized context persistence state could not be proven; "
                            f"publication cleanup was skipped: {state_error}"
                        )
                    if persistence_state != "absent":
                        exc.add_note(
                            "materialized context publication cleanup was skipped after "
                            f"persistence state {persistence_state!r}"
                        )
                        raise
                    try:
                        cleanup = materializer.discard_publication(
                            publication,
                            materialization_root_descriptor=materialization_root_fd,
                        )
                    except ValueError as cleanup_error:
                        exc.add_note(f"materialized context cleanup failed: {cleanup_error}")
                    else:
                        if cleanup not in {"removed", "missing"}:
                            exc.add_note(
                                f"materialized context cleanup was safely deferred: {cleanup}"
                            )
                    raise
                if persisted:
                    return result
                cleanup = materializer.discard_publication(
                    publication,
                    materialization_root_descriptor=materialization_root_fd,
                )
                if cleanup == "mismatch":
                    raise ValueError(
                        "materialized context path changed while resolving an ID collision"
                    )
        raise RuntimeError("could not allocate unique context id")

    def _materialized_context_persistence_state(
        self,
        context_id: str,
        materialization: MaterializedContext,
    ) -> str:
        expected_response = _json_dumps(materialization.model_dump(mode="json"))
        expected_manifest = canonical_json(materialization)
        with self.connect() as conn:
            context_row = conn.execute(
                "SELECT response_json FROM contexts WHERE context_id = ?",
                (context_id,),
            ).fetchone()
            materialization_row = conn.execute(
                "SELECT manifest_json FROM context_materializations WHERE context_id = ?",
                (context_id,),
            ).fetchone()
        if context_row is None and materialization_row is None:
            return "absent"
        if (
            context_row is not None
            and materialization_row is not None
            and str(context_row["response_json"]) == expected_response
            and str(materialization_row["manifest_json"]) == expected_manifest
        ):
            return "committed"
        return "unknown"

    def get_materialized_context(self, context_id: str) -> MaterializedContext:
        """Read one canonical materialization without exposing its filesystem path."""

        if (
            not isinstance(context_id, str)
            or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,255}", context_id) is None
        ):
            raise ValueError("materialized context identity is invalid")
        materializer = self._context_materializer
        if materializer is None:
            raise ValueError("materialized context access requires a verified executable registry")
        if self._bound_store_id is None:
            raise ValueError("evolution store identity has not been initialized")
        with self._locked_context_materialization_root() as materialization_root_fd:
            try:
                with self.connect() as conn:
                    self._verify_bound_store_identity(conn)
                    row = _read_guarded_materialized_context_row(conn, context_id)
                if row is None:
                    raise ValueError("materialized context is not persisted")
                manifest = self._materialized_context_from_row(row)
                materializer.verify_persisted_materialization(
                    manifest,
                    materialization_root_descriptor=materialization_root_fd,
                )
                return manifest
            finally:
                self._verify_bound_materialization_root(materialization_root_fd)
                with self.connect() as conn:
                    self._verify_bound_store_identity(conn)

    @contextmanager
    def open_materialized_blob(
        self,
        context_id: str,
        blob_id: str,
    ) -> Iterator[MaterializedBlobLease]:
        """Open one DB-authorized materialized blob through the bound store root."""

        materializer = self._context_materializer
        if materializer is None:
            raise ValueError("materialized blob access requires a verified executable registry")
        if self._bound_store_id is None:
            raise ValueError("evolution store identity has not been initialized")
        with self._locked_context_materialization_root() as materialization_root_fd:
            with self.connect() as conn:
                self._verify_bound_store_identity(conn)
                row = _read_guarded_materialized_context_row(conn, context_id)
            if row is None:
                raise ValueError("materialized context is not persisted")
            manifest = self._materialized_context_from_row(row)
            if manifest.context_id != context_id:
                raise ValueError("persisted materialized context identity is inconsistent")
            try:
                with materializer._open_blob(
                    context_id,
                    blob_id,
                    expected_manifest=manifest,
                    materialization_root_descriptor=materialization_root_fd,
                ) as lease:
                    yield lease
            finally:
                self._verify_bound_materialization_root(materialization_root_fd)
                with self.connect() as conn:
                    self._verify_bound_store_identity(conn)

    @staticmethod
    def _materialized_context_from_row(row: sqlite3.Row) -> MaterializedContext:
        manifest_json = row["manifest_json"]
        if (
            not isinstance(manifest_json, str)
            or len(manifest_json.encode("utf-8")) > MAX_CONTEXT_MANIFEST_BYTES
        ):
            raise ValueError("persisted materialized context manifest is invalid")
        try:
            manifest = MaterializedContext.model_validate_json(manifest_json)
        except (TypeError, ValueError) as exc:
            raise ValueError("persisted materialized context manifest is invalid") from exc
        if manifest_json != canonical_json(manifest):
            raise ValueError("persisted materialized context manifest is not canonical")
        if (
            manifest.context_id != str(row["context_id"])
            or manifest.registry_digest != str(row["registry_digest"])
            or manifest.request_digest != str(row["request_digest"])
        ):
            raise ValueError("persisted materialized context identity is inconsistent")
        return manifest

    @staticmethod
    def _execution_snapshot_record_from_row(row: sqlite3.Row) -> ExecutionSnapshotRecord:
        snapshot_json = row["snapshot_json"]
        if (
            not isinstance(snapshot_json, str)
            or len(snapshot_json.encode("utf-8")) > MAX_EXECUTION_SNAPSHOT_BYTES
        ):
            raise RevisionIntegrityError("persisted execution snapshot is invalid")
        try:
            snapshot = ExecutionSnapshotV1.model_validate_json(snapshot_json)
            created_at = _parse_canonical_utc_iso(
                row["created_at"],
                label="execution snapshot created_at",
            )
        except (TypeError, ValueError) as exc:
            raise RevisionIntegrityError("persisted execution snapshot is invalid") from exc
        digest = canonical_digest(snapshot)
        if (
            snapshot_json != canonical_json(snapshot)
            or row["execution_snapshot_id"] != f"exec-{digest}"
            or row["snapshot_digest"] != digest
        ):
            raise RevisionIntegrityError("persisted execution snapshot identity is inconsistent")
        try:
            return ExecutionSnapshotRecord(
                execution_snapshot_id=str(row["execution_snapshot_id"]),
                snapshot_digest=str(row["snapshot_digest"]),
                producer_id=str(row["producer_id"]),
                snapshot=snapshot,
                created_at=created_at,
            )
        except (TypeError, ValueError) as exc:
            raise RevisionIntegrityError("persisted execution snapshot is invalid") from exc

    @staticmethod
    def _revision_manifest_from_row(row: sqlite3.Row) -> RevisionManifestV1:
        manifest_json = row["manifest_json"]
        if (
            not isinstance(manifest_json, str)
            or len(manifest_json.encode("utf-8")) > MAX_REVISION_MANIFEST_BYTES
        ):
            raise RevisionIntegrityError("persisted revision manifest is invalid")
        try:
            manifest = RevisionManifestV1.model_validate_json(manifest_json)
            _strict_sqlite_integer(
                row["generation"],
                label="revision generation",
            )
            _parse_canonical_utc_iso(
                row["created_at"],
                label="revision created_at",
            )
        except (TypeError, ValueError) as exc:
            raise RevisionIntegrityError("persisted revision manifest is invalid") from exc
        manifest_digest = canonical_digest(manifest)
        if manifest_json != canonical_json(manifest):
            raise RevisionIntegrityError("persisted revision manifest is not canonical")
        expected = {
            "revision_id": revision_id_for_manifest(manifest),
            "stream_id": manifest.stream_id,
            "generation": manifest.generation,
            "predecessor_revision_id": manifest.predecessor_revision_id,
            "manifest_digest": manifest_digest,
            "context_id": manifest.context.context_id,
            "context_manifest_digest": manifest.context.manifest_digest,
            "registry_digest": manifest.context.registry_digest,
            "execution_snapshot_id": manifest.execution_snapshot_id,
            "execution_snapshot_digest": manifest.execution_snapshot_digest,
            "adapter_set_digest": canonical_digest(manifest.adapters),
        }
        if any(row[key] != value for key, value in expected.items()):
            raise RevisionIntegrityError("persisted revision identity is inconsistent")
        return manifest

    @classmethod
    def _revision_record_from_row(
        cls,
        row: sqlite3.Row,
        *,
        active: bool,
    ) -> RevisionRecord:
        manifest = cls._revision_manifest_from_row(row)
        try:
            return RevisionRecord(
                revision_id=str(row["revision_id"]),
                manifest_digest=str(row["manifest_digest"]),
                manifest=manifest,
                created_at=_parse_canonical_utc_iso(
                    row["created_at"],
                    label="revision created_at",
                ),
                active=active,
            )
        except (TypeError, ValueError) as exc:
            raise RevisionIntegrityError("persisted revision record is invalid") from exc

    @staticmethod
    def _task_admission_from_row(row: sqlite3.Row) -> TaskAdmissionRecord:
        request_json = row["request_json"]
        if not isinstance(request_json, str) or len(request_json.encode("utf-8")) > 64 * 1024:
            raise RevisionIntegrityError("persisted task admission request is invalid")
        try:
            request = TaskAdmissionRequest.model_validate_json(request_json)
            _strict_sqlite_integer(
                row["required_generation"],
                label="task admission required generation",
            )
            created_at = _parse_canonical_utc_iso(
                row["created_at"],
                label="task admission created_at",
            )
            updated_at = _parse_canonical_utc_iso(
                row["updated_at"],
                label="task admission updated_at",
            )
            finished_at = (
                None
                if row["finished_at"] is None
                else _parse_canonical_utc_iso(
                    row["finished_at"],
                    label="task admission finished_at",
                )
            )
        except (TypeError, ValueError) as exc:
            raise RevisionIntegrityError("persisted task admission request is invalid") from exc
        request_digest = canonical_digest(request)
        expected = {
            "admission_id": admission_id_for_request(request),
            "stream_id": request.stream_id,
            "task_id": request.task_id,
            "idempotency_key": request.idempotency_key,
            "required_generation": request.required_generation,
            "request_digest": request_digest,
        }
        if request_json != canonical_json(request) or any(
            row[key] != value for key, value in expected.items()
        ):
            raise RevisionIntegrityError("persisted task admission identity is inconsistent")
        try:
            return TaskAdmissionRecord(
                admission_id=str(row["admission_id"]),
                request_digest=str(row["request_digest"]),
                request=request,
                status=str(row["status"]),
                reason=None if row["reason"] is None else str(row["reason"]),
                pinned_revision_id=(
                    None if row["pinned_revision_id"] is None else str(row["pinned_revision_id"])
                ),
                created_at=created_at,
                updated_at=updated_at,
                finished_at=finished_at,
            )
        except (TypeError, ValueError) as exc:
            raise RevisionIntegrityError("persisted task admission record is invalid") from exc

    @staticmethod
    def _validate_revision_materialization_binding(
        manifest: RevisionManifestV1,
        binding: _MaterializationRecoveryIdentity | None,
    ) -> None:
        if binding is None:
            raise RevisionIntegrityError("revision materialized context is not persisted")
        if (
            manifest.context.context_id != binding.context_id
            or manifest.context.manifest_digest != binding.manifest_digest
            or manifest.context.registry_digest != binding.registry_digest
            or manifest.context.request_digest != binding.request_digest
        ):
            raise RevisionIntegrityError("revision materialized context identity does not match")
        if (
            binding.base_model is None
            or binding.base_model != manifest.execution_snapshot.model.model_id
        ):
            raise RevisionIntegrityError("revision model does not match materialized context")
        if binding.execution_mode != str(manifest.execution_snapshot.execution_mode):
            raise RevisionIntegrityError(
                "revision serving mode does not match materialized context"
            )
        if binding.capture_mode != str(manifest.execution_snapshot.capture_mode):
            raise RevisionIntegrityError(
                "revision capture mode does not match materialized context"
            )
        if manifest.context.artifact_ids != binding.artifact_ids:
            raise RevisionIntegrityError(
                "revision context artifacts do not match materialized context"
            )
        if canonical_digest(manifest.adapters) != binding.adapter_set_digest:
            raise RevisionIntegrityError("revision adapters do not match materialized context")

    def _load_materialization_binding(
        self,
        conn: sqlite3.Connection,
        context_id: str,
    ) -> _MaterializationRecoveryIdentity:
        row = conn.execute(
            """
            SELECT context_materializations.context_id,
                   context_materializations.registry_digest,
                   context_materializations.request_digest,
                   context_materializations.manifest_json,
                   contexts.request_json,
                   contexts.response_json
            FROM context_materializations
            JOIN contexts USING (context_id)
            WHERE context_materializations.context_id = ?
            """,
            (context_id,),
        ).fetchone()
        if row is None:
            raise RevisionIntegrityError("revision materialized context is not persisted")
        try:
            materialized = self._materialized_context_from_row(row)
            request = ContextProjectionResolveRequest.model_validate_json(str(row["request_json"]))
            response = MaterializedContext.model_validate_json(str(row["response_json"]))
        except (TypeError, ValueError) as exc:
            raise RevisionIntegrityError(
                "revision materialized context snapshot is invalid"
            ) from exc
        if canonical_digest(request) != materialized.request_digest or response != materialized:
            raise RevisionIntegrityError("revision materialized context snapshot is inconsistent")
        binding = _MaterializationRecoveryIdentity(
            context_id=materialized.context_id,
            manifest_digest=canonical_digest(materialized),
            registry_digest=materialized.registry_digest,
            request_digest=materialized.request_digest,
            base_model=request.base_model,
            execution_mode=str(request.execution_profile.execution_mode),
            capture_mode=str(request.execution_profile.capture_mode),
            artifact_ids=materialized.selection.artifact_ids,
            adapter_set_digest=canonical_digest(materialized.adapter_merge_spec.adapters),
            successor_transition_id=(
                materialized.successor_transition_id
            ),
        )
        return binding

    def _validate_revision_materialization(
        self,
        conn: sqlite3.Connection,
        manifest: RevisionManifestV1,
    ) -> None:
        binding = self._load_materialization_binding(
            conn,
            manifest.context.context_id,
        )
        self._validate_revision_materialization_binding(manifest, binding)

    def _load_execution_snapshot(
        self,
        conn: sqlite3.Connection,
        execution_snapshot_id: str,
    ) -> ExecutionSnapshotRecord:
        row = conn.execute(
            "SELECT * FROM execution_snapshots WHERE execution_snapshot_id = ?",
            (execution_snapshot_id,),
        ).fetchone()
        if row is None:
            raise RevisionIntegrityError("revision execution snapshot is not registered")
        return self._execution_snapshot_record_from_row(row)

    def _validate_revision_execution_snapshot(
        self,
        conn: sqlite3.Connection,
        manifest: RevisionManifestV1,
    ) -> ExecutionSnapshotRecord:
        record = self._load_execution_snapshot(conn, manifest.execution_snapshot_id)
        if (
            record.snapshot_digest != manifest.execution_snapshot_digest
            or record.snapshot != manifest.execution_snapshot
        ):
            raise RevisionIntegrityError(
                "revision execution snapshot does not match the registered snapshot"
            )
        return record

    def register_execution_snapshot(
        self,
        verified_snapshot: VerifiedExecutionSnapshot,
    ) -> ExecutionSnapshotRecord:
        """Persist one canonical identity already sealed by a verified producer."""

        verified_snapshot = require_verified_execution_snapshot(verified_snapshot)
        producer_id = verified_snapshot.producer_id
        snapshot = verified_snapshot.snapshot
        snapshot = ExecutionSnapshotV1.model_validate(snapshot.model_dump(mode="python"))
        snapshot_json = canonical_json(snapshot)
        snapshot_digest = canonical_digest(snapshot)
        snapshot_id = execution_snapshot_id_for_snapshot(snapshot)
        with self.connect() as conn:
            try:
                conn.execute("BEGIN IMMEDIATE")
                self._verify_bound_store_identity(conn)
                existing = conn.execute(
                    "SELECT * FROM execution_snapshots WHERE execution_snapshot_id = ? "
                    "OR snapshot_digest = ?",
                    (snapshot_id, snapshot_digest),
                ).fetchone()
                if existing is not None:
                    record = self._execution_snapshot_record_from_row(existing)
                    if record.snapshot != snapshot or record.producer_id != producer_id:
                        raise RevisionConflictError("execution snapshot identity is already bound")
                    conn.commit()
                    return record
                now = utc_now_iso()
                _enforce_ledger_capacity(
                    conn,
                    table="execution_snapshots",
                    text_blob_columns=(
                        "execution_snapshot_id",
                        "created_at",
                        "snapshot_digest",
                        "producer_id",
                        "snapshot_json",
                    ),
                    new_text_blob_values=(
                        snapshot_id,
                        now,
                        snapshot_digest,
                        producer_id,
                        snapshot_json,
                    ),
                    max_rows=MAX_EXECUTION_SNAPSHOT_RECOVERY_ROWS,
                    max_bytes=MAX_EXECUTION_SNAPSHOT_RECOVERY_BYTES,
                    max_row_bytes=MAX_EXECUTION_SNAPSHOT_ROW_BYTES,
                    label="execution snapshot ledger",
                )
                conn.execute(
                    "INSERT INTO execution_snapshots (execution_snapshot_id, created_at, "
                    "snapshot_digest, producer_id, snapshot_json) VALUES (?, ?, ?, ?, ?)",
                    (snapshot_id, now, snapshot_digest, producer_id, snapshot_json),
                )
                row = conn.execute(
                    "SELECT * FROM execution_snapshots WHERE execution_snapshot_id = ?",
                    (snapshot_id,),
                ).fetchone()
                if row is None:
                    raise RevisionIntegrityError(
                        "created execution snapshot could not be read back"
                    )
                record = self._execution_snapshot_record_from_row(row)
                self._verify_bound_store_identity(conn)
                conn.commit()
                return record
            except BaseException:
                try:
                    conn.rollback()
                except sqlite3.Error:
                    pass
                raise

    @staticmethod
    def _revision_recovery_identity(
        revision_id: str,
        manifest: RevisionManifestV1,
    ) -> _RevisionRecoveryIdentity:
        return _RevisionRecoveryIdentity(
            revision_id=revision_id,
            stream_id=manifest.stream_id,
            generation=manifest.generation,
            predecessor_revision_id=manifest.predecessor_revision_id,
            project_snapshot_id=manifest.project_snapshot.snapshot_id,
            workspace_snapshot_id=manifest.workspace_snapshot.snapshot_id,
            context_id=manifest.context.context_id,
            context_artifact_ids=manifest.context.artifact_ids,
            context_artifact_set_digest=canonical_digest(manifest.context.artifact_ids),
            execution_snapshot_id=manifest.execution_snapshot_id,
            execution_mode=str(manifest.execution_snapshot.execution_mode),
            capture_mode=str(manifest.execution_snapshot.capture_mode),
        )

    @staticmethod
    def _validate_admission_request_against_revision(
        request: TaskAdmissionRequest,
        revision: _RevisionRecoveryIdentity,
    ) -> None:
        if request.project_id != request.stream_id or request.project_id != revision.stream_id:
            raise TaskAdmissionConflictError(
                "task admission project does not match the revision stream"
            )
        if request.project_snapshot.snapshot_id != revision.project_snapshot_id:
            raise TaskAdmissionConflictError(
                "task admission project snapshot does not match the pinned revision"
            )
        if request.workspace_snapshot.snapshot_id != revision.workspace_snapshot_id:
            raise TaskAdmissionConflictError(
                "task admission workspace snapshot does not match the pinned revision"
            )
        if request.execution_snapshot_id != revision.execution_snapshot_id:
            raise TaskAdmissionConflictError(
                "task admission execution snapshot does not match the pinned revision"
            )
        if str(request.execution_mode) != revision.execution_mode:
            raise TaskAdmissionConflictError(
                "task admission execution mode does not match the pinned revision"
            )
        if str(request.capture_mode) != revision.capture_mode:
            raise TaskAdmissionConflictError(
                "task admission capture mode does not match the pinned revision"
            )
        if request.context_id != revision.context_id:
            raise TaskAdmissionConflictError(
                "task admission context does not match the pinned revision"
            )
        if (
            request.context_artifact_ids != revision.context_artifact_ids
            or request.context_artifact_set_digest != revision.context_artifact_set_digest
        ):
            raise TaskAdmissionConflictError(
                "task admission context artifact set does not match the pinned revision"
            )

    @classmethod
    def _validate_unpinned_admission_authority(
        cls,
        record: TaskAdmissionRecord,
        *,
        active_generation: int,
        active_revision: _RevisionRecoveryIdentity | None,
    ) -> None:
        if record.pinned_revision_id is not None:
            raise RevisionIntegrityError("unpinned admission validator received a pin")
        if record.status is AdmissionStatus.CANCELLED:
            return
        if record.status is not AdmissionStatus.QUEUED:
            raise RevisionIntegrityError("task admission pin is missing")
        if record.request.required_generation not in {
            active_generation,
            active_generation + 1,
        }:
            raise RevisionIntegrityError("queued task admission generation is inconsistent")
        if record.request.required_generation != active_generation:
            return
        if active_revision is None:
            raise RevisionIntegrityError("revision stream active head is missing")
        try:
            cls._validate_admission_request_against_revision(
                record.request,
                active_revision,
            )
        except TaskAdmissionConflictError as exc:
            raise RevisionIntegrityError(
                "unpinned task admission identity is inconsistent"
            ) from exc

    def _validate_revision_authority(
        self,
        conn: sqlite3.Connection,
        revision_id: str,
        *,
        expected_stream_id: str | None = None,
        expected_generation: int | None = None,
    ) -> _AuthoritativeRevisionClosure:
        row = conn.execute(
            "SELECT * FROM revisions WHERE revision_id = ?",
            (revision_id,),
        ).fetchone()
        if row is None:
            raise RevisionIntegrityError("authoritative revision row is missing")
        manifest = self._revision_manifest_from_row(row)
        if (expected_stream_id is not None and manifest.stream_id != expected_stream_id) or (
            expected_generation is not None and manifest.generation != expected_generation
        ):
            raise RevisionIntegrityError("authoritative stream revision identity is inconsistent")
        self._validate_revision_materialization(conn, manifest)
        self._validate_revision_execution_snapshot(conn, manifest)
        return _AuthoritativeRevisionClosure(
            revision_id=revision_id,
            manifest=manifest,
            identity=self._revision_recovery_identity(revision_id, manifest),
        )

    def _validate_stream_authority(
        self,
        conn: sqlite3.Connection,
        stream_id: str,
    ) -> tuple[_AuthoritativeRevisionClosure, int]:
        row = conn.execute(
            "SELECT stream_id, active_revision_id, active_generation, created_at, "
            "updated_at FROM revision_streams WHERE stream_id = ?",
            (stream_id,),
        ).fetchone()
        if row is None:
            raise RevisionNotFoundError("revision stream does not exist")
        try:
            active_generation = _strict_sqlite_integer(
                row["active_generation"],
                label="revision stream active generation",
            )
            created_at = _parse_canonical_utc_iso(
                row["created_at"],
                label="revision stream created_at",
            )
            updated_at = _parse_canonical_utc_iso(
                row["updated_at"],
                label="revision stream updated_at",
            )
        except (TypeError, ValueError) as exc:
            raise RevisionIntegrityError("revision stream record is invalid") from exc
        if str(row["stream_id"]) != stream_id or updated_at < created_at or active_generation < 0:
            raise RevisionIntegrityError("revision stream active identity is inconsistent")
        active_revision_id = row["active_revision_id"]
        if not isinstance(active_revision_id, str):
            raise RevisionIntegrityError("revision stream active identity is inconsistent")
        closure = self._validate_revision_authority(
            conn,
            active_revision_id,
            expected_stream_id=stream_id,
            expected_generation=active_generation,
        )
        return closure, active_generation

    def _validate_admission_request_sources(
        self,
        conn: sqlite3.Connection,
        request: TaskAdmissionRequest,
    ) -> None:
        if request.project_id != request.stream_id:
            raise RevisionIntegrityError(
                "task admission project does not match the revision stream"
            )
        snapshot = self._load_execution_snapshot(
            conn,
            request.execution_snapshot_id,
        ).snapshot
        binding = self._load_materialization_binding(conn, request.context_id)
        if (
            snapshot.execution_mode != request.execution_mode
            or snapshot.capture_mode != request.capture_mode
            or binding.execution_mode != str(request.execution_mode)
            or binding.capture_mode != str(request.capture_mode)
            or binding.base_model != snapshot.model.model_id
            or binding.artifact_ids != request.context_artifact_ids
            or canonical_digest(binding.artifact_ids) != request.context_artifact_set_digest
        ):
            raise RevisionIntegrityError("task admission envelope sources are inconsistent")

    def _validate_admission_authority(
        self,
        conn: sqlite3.Connection,
        record: TaskAdmissionRecord,
    ) -> tuple[_AuthoritativeRevisionClosure, int]:
        active, active_generation = self._validate_stream_authority(
            conn,
            record.request.stream_id,
        )
        self._validate_admission_request_sources(conn, record.request)
        if record.pinned_revision_id is not None:
            pinned = self._validate_revision_authority(
                conn,
                record.pinned_revision_id,
                expected_stream_id=record.request.stream_id,
                expected_generation=record.request.required_generation,
            )
            try:
                self._validate_admission_request_against_revision(
                    record.request,
                    pinned.identity,
                )
            except TaskAdmissionConflictError as exc:
                raise RevisionIntegrityError(
                    "task admission pin identity is inconsistent"
                ) from exc
        else:
            self._validate_unpinned_admission_authority(
                record,
                active_generation=active_generation,
                active_revision=active.identity,
            )
        return active, active_generation

    def _validate_successor_admission_barrier(
        self,
        conn: sqlite3.Connection,
        *,
        active: _AuthoritativeRevisionClosure,
        active_generation: int,
        successor_revision_id: str,
        successor_manifest: RevisionManifestV1,
    ) -> None:
        successor = self._revision_recovery_identity(
            successor_revision_id,
            successor_manifest,
        )
        rows = conn.execute(
            """
            SELECT * FROM task_admissions
            WHERE stream_id = ? AND status = 'queued'
            ORDER BY admission_id
            """,
            (successor_manifest.stream_id,),
        )
        for row in rows:
            record = self._task_admission_from_row(row)
            self._validate_admission_request_sources(conn, record.request)
            self._validate_unpinned_admission_authority(
                record,
                active_generation=active_generation,
                active_revision=active.identity,
            )
            if record.request.required_generation == active_generation:
                raise RevisionConflictError(
                    "active-generation queued admissions must be pinned or cancelled "
                    "before successor activation"
                )
            try:
                self._validate_admission_request_against_revision(
                    record.request,
                    successor,
                )
            except TaskAdmissionConflictError as exc:
                raise RevisionConflictError(
                    "successor revision does not match a queued task admission"
                ) from exc

    def create_genesis_revision(self, manifest: RevisionManifestV1) -> RevisionRecord:
        """Create one explicit generation-zero stream head, or return its exact retry."""

        manifest = RevisionManifestV1.model_validate(manifest.model_dump(mode="python"))
        if manifest.generation != 0 or manifest.predecessor_revision_id is not None:
            raise RevisionConflictError("genesis revision must be generation zero")
        manifest_json = canonical_json(manifest)
        manifest_digest = canonical_digest(manifest)
        revision_id = revision_id_for_manifest(manifest)
        now = utc_now_iso()
        with self.connect() as conn:
            try:
                conn.execute("BEGIN IMMEDIATE")
                self._verify_bound_store_identity(conn)
                existing_stream = conn.execute(
                    "SELECT 1 FROM revision_streams WHERE stream_id = ?",
                    (manifest.stream_id,),
                ).fetchone()
                if existing_stream is not None:
                    active, active_generation = self._validate_stream_authority(
                        conn,
                        manifest.stream_id,
                    )
                    if active_generation != 0 or active.manifest != manifest:
                        raise RevisionConflictError(
                            "revision stream is already bound to a different genesis"
                        )
                    row = conn.execute(
                        "SELECT * FROM revisions WHERE revision_id = ?",
                        (active.revision_id,),
                    ).fetchone()
                    if row is None:
                        raise RevisionIntegrityError("revision stream active head is missing")
                    record = self._revision_record_from_row(row, active=True)
                    conn.commit()
                    return record
                self._validate_revision_materialization(conn, manifest)
                self._validate_revision_execution_snapshot(conn, manifest)
                collision = conn.execute(
                    "SELECT 1 FROM revisions WHERE revision_id = ? OR manifest_digest = ?",
                    (revision_id, manifest_digest),
                ).fetchone()
                if collision is not None:
                    raise RevisionConflictError("revision identity is already bound")
                _enforce_ledger_capacity(
                    conn,
                    table="revisions",
                    text_blob_columns=(
                        "revision_id",
                        "stream_id",
                        "predecessor_revision_id",
                        "created_at",
                        "manifest_digest",
                        "manifest_json",
                        "context_id",
                        "context_manifest_digest",
                        "registry_digest",
                        "execution_snapshot_id",
                        "execution_snapshot_digest",
                        "adapter_set_digest",
                    ),
                    new_text_blob_values=(
                        revision_id,
                        manifest.stream_id,
                        manifest.predecessor_revision_id,
                        now,
                        manifest_digest,
                        manifest_json,
                        manifest.context.context_id,
                        manifest.context.manifest_digest,
                        manifest.context.registry_digest,
                        manifest.execution_snapshot_id,
                        manifest.execution_snapshot_digest,
                        canonical_digest(manifest.adapters),
                    ),
                    max_rows=MAX_REVISION_RECOVERY_ROWS,
                    max_bytes=MAX_REVISION_RECOVERY_BYTES,
                    max_row_bytes=MAX_REVISION_ROW_BYTES,
                    label="revision ledger",
                )
                _enforce_ledger_capacity(
                    conn,
                    table="revision_streams",
                    text_blob_columns=(
                        "stream_id",
                        "active_revision_id",
                        "created_at",
                        "updated_at",
                    ),
                    new_text_blob_values=(manifest.stream_id, revision_id, now, now),
                    max_rows=MAX_REVISION_STREAM_RECOVERY_ROWS,
                    max_bytes=MAX_REVISION_STREAM_RECOVERY_BYTES,
                    max_row_bytes=MAX_REVISION_STREAM_ROW_BYTES,
                    label="revision stream ledger",
                )
                conn.execute(
                    """
                    INSERT INTO revisions (
                        revision_id, stream_id, generation, predecessor_revision_id,
                        created_at, manifest_digest, manifest_json, context_id,
                        context_manifest_digest, registry_digest, execution_snapshot_id,
                        execution_snapshot_digest, adapter_set_digest
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        revision_id,
                        manifest.stream_id,
                        manifest.generation,
                        manifest.predecessor_revision_id,
                        now,
                        manifest_digest,
                        manifest_json,
                        manifest.context.context_id,
                        manifest.context.manifest_digest,
                        manifest.context.registry_digest,
                        manifest.execution_snapshot_id,
                        manifest.execution_snapshot_digest,
                        canonical_digest(manifest.adapters),
                    ),
                )
                conn.execute(
                    """
                    INSERT INTO revision_streams (
                        stream_id, active_revision_id, active_generation, created_at, updated_at
                    )
                    VALUES (?, ?, 0, ?, ?)
                    """,
                    (manifest.stream_id, revision_id, now, now),
                )
                row = conn.execute(
                    "SELECT * FROM revisions WHERE revision_id = ?",
                    (revision_id,),
                ).fetchone()
                if row is None:
                    raise RevisionIntegrityError("created revision could not be read back")
                record = self._revision_record_from_row(row, active=True)
                self._verify_bound_store_identity(conn)
                conn.commit()
                return record
            except BaseException:
                try:
                    conn.rollback()
                except sqlite3.Error:
                    pass
                raise

    def activate_successor_revision(self, manifest: RevisionManifestV1) -> RevisionRecord:
        """Atomically advance one stream to its exact, fully bound successor."""

        manifest = RevisionManifestV1.model_validate(manifest.model_dump(mode="python"))
        if manifest.generation == 0 or manifest.predecessor_revision_id is None:
            raise RevisionConflictError("successor revision must follow an existing revision")
        manifest_json = canonical_json(manifest)
        manifest_digest = canonical_digest(manifest)
        revision_id = revision_id_for_manifest(manifest)
        with self.connect() as conn:
            try:
                conn.execute("BEGIN IMMEDIATE")
                self._verify_bound_store_identity(conn)
                active, active_generation = self._validate_stream_authority(
                    conn,
                    manifest.stream_id,
                )

                existing = conn.execute(
                    "SELECT * FROM revisions WHERE revision_id = ?",
                    (revision_id,),
                ).fetchone()
                if existing is not None:
                    persisted = self._validate_revision_authority(
                        conn,
                        revision_id,
                        expected_stream_id=manifest.stream_id,
                        expected_generation=manifest.generation,
                    )
                    if persisted.manifest != manifest:
                        raise RevisionConflictError("revision identity is already bound")
                    if manifest.generation > active_generation:
                        raise RevisionIntegrityError(
                            "persisted revision is ahead of the authoritative stream"
                        )
                    cursor = active
                    cursor_generation = active_generation
                    while cursor_generation > manifest.generation:
                        predecessor_id = cursor.manifest.predecessor_revision_id
                        if predecessor_id is None:
                            raise RevisionIntegrityError(
                                "authoritative revision chain is incomplete"
                            )
                        cursor_generation -= 1
                        cursor = self._validate_revision_authority(
                            conn,
                            predecessor_id,
                            expected_stream_id=manifest.stream_id,
                            expected_generation=cursor_generation,
                        )
                    if cursor.revision_id != revision_id:
                        raise RevisionConflictError(
                            "revision is not part of the authoritative stream"
                        )
                    record = self._revision_record_from_row(
                        existing,
                        active=revision_id == active.revision_id,
                    )
                    self._verify_bound_store_identity(conn)
                    conn.commit()
                    return record

                if manifest.generation != active_generation + 1:
                    raise RevisionConflictError(
                        "successor generation must immediately follow the active revision"
                    )
                if manifest.predecessor_revision_id != active.revision_id:
                    raise RevisionConflictError(
                        "successor predecessor must be the active revision"
                    )
                self._validate_revision_materialization(conn, manifest)
                self._validate_revision_execution_snapshot(conn, manifest)
                self._validate_successor_admission_barrier(
                    conn,
                    active=active,
                    active_generation=active_generation,
                    successor_revision_id=revision_id,
                    successor_manifest=manifest,
                )
                collision = conn.execute(
                    "SELECT 1 FROM revisions WHERE manifest_digest = ?",
                    (manifest_digest,),
                ).fetchone()
                if collision is not None:
                    raise RevisionConflictError("revision identity is already bound")

                stream_row = conn.execute(
                    "SELECT stream_id, active_revision_id, active_generation, created_at, "
                    "updated_at FROM revision_streams WHERE stream_id = ?",
                    (manifest.stream_id,),
                ).fetchone()
                if stream_row is None:
                    raise RevisionIntegrityError("revision stream disappeared during activation")
                stream_created_at = stream_row["created_at"]
                stream_updated_at = stream_row["updated_at"]
                if not isinstance(stream_created_at, str) or not isinstance(
                    stream_updated_at,
                    str,
                ):
                    raise RevisionIntegrityError("revision stream timestamps are invalid")
                now = utc_now_iso()
                if _parse_canonical_utc_iso(
                    now,
                    label="successor activation timestamp",
                ) < _parse_canonical_utc_iso(
                    stream_updated_at,
                    label="revision stream updated_at",
                ):
                    now = stream_updated_at

                revision_columns = (
                    "revision_id",
                    "stream_id",
                    "predecessor_revision_id",
                    "created_at",
                    "manifest_digest",
                    "manifest_json",
                    "context_id",
                    "context_manifest_digest",
                    "registry_digest",
                    "execution_snapshot_id",
                    "execution_snapshot_digest",
                    "adapter_set_digest",
                )
                revision_values = (
                    revision_id,
                    manifest.stream_id,
                    manifest.predecessor_revision_id,
                    now,
                    manifest_digest,
                    manifest_json,
                    manifest.context.context_id,
                    manifest.context.manifest_digest,
                    manifest.context.registry_digest,
                    manifest.execution_snapshot_id,
                    manifest.execution_snapshot_digest,
                    canonical_digest(manifest.adapters),
                )
                _enforce_ledger_capacity(
                    conn,
                    table="revisions",
                    text_blob_columns=revision_columns,
                    new_text_blob_values=revision_values,
                    max_rows=MAX_REVISION_RECOVERY_ROWS,
                    max_bytes=MAX_REVISION_RECOVERY_BYTES,
                    max_row_bytes=MAX_REVISION_ROW_BYTES,
                    label="revision ledger",
                )
                _enforce_ledger_update_capacity(
                    conn,
                    table="revision_streams",
                    key_column="stream_id",
                    key_value=manifest.stream_id,
                    text_blob_columns=(
                        "stream_id",
                        "active_revision_id",
                        "created_at",
                        "updated_at",
                    ),
                    new_text_blob_values=(
                        manifest.stream_id,
                        revision_id,
                        stream_created_at,
                        now,
                    ),
                    max_bytes=MAX_REVISION_STREAM_RECOVERY_BYTES,
                    max_row_bytes=MAX_REVISION_STREAM_ROW_BYTES,
                    label="revision stream ledger",
                )
                conn.execute(
                    """
                    INSERT INTO revisions (
                        revision_id, stream_id, generation, predecessor_revision_id,
                        created_at, manifest_digest, manifest_json, context_id,
                        context_manifest_digest, registry_digest, execution_snapshot_id,
                        execution_snapshot_digest, adapter_set_digest
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        revision_id,
                        manifest.stream_id,
                        manifest.generation,
                        manifest.predecessor_revision_id,
                        now,
                        manifest_digest,
                        manifest_json,
                        manifest.context.context_id,
                        manifest.context.manifest_digest,
                        manifest.context.registry_digest,
                        manifest.execution_snapshot_id,
                        manifest.execution_snapshot_digest,
                        canonical_digest(manifest.adapters),
                    ),
                )
                updated = conn.execute(
                    """
                    UPDATE revision_streams
                    SET active_revision_id = ?, active_generation = ?, updated_at = ?
                    WHERE stream_id = ? AND active_revision_id = ? AND active_generation = ?
                    """,
                    (
                        revision_id,
                        manifest.generation,
                        now,
                        manifest.stream_id,
                        active.revision_id,
                        active_generation,
                    ),
                )
                if updated.rowcount != 1:
                    raise RevisionIntegrityError(
                        "revision stream changed during successor activation"
                    )
                new_active, new_generation = self._validate_stream_authority(
                    conn,
                    manifest.stream_id,
                )
                if new_active.revision_id != revision_id or new_generation != manifest.generation:
                    raise RevisionIntegrityError("successor activation readback is inconsistent")
                row = conn.execute(
                    "SELECT * FROM revisions WHERE revision_id = ?",
                    (revision_id,),
                ).fetchone()
                if row is None:
                    raise RevisionIntegrityError("activated revision could not be read back")
                record = self._revision_record_from_row(row, active=True)
                self._verify_bound_store_identity(conn)
                conn.commit()
                return record
            except BaseException:
                try:
                    conn.rollback()
                except sqlite3.Error:
                    pass
                raise

    def get_active_revision(self, stream_id: str) -> RevisionRecord:
        if re.fullmatch(r"[A-Za-z][A-Za-z0-9_.-]{0,127}", stream_id) is None:
            raise ValueError("revision stream ID is invalid")
        with self.connect() as conn:
            try:
                conn.execute("BEGIN")
                self._verify_bound_store_identity(conn)
                active, _active_generation = self._validate_stream_authority(
                    conn,
                    stream_id,
                )
                row = conn.execute(
                    "SELECT * FROM revisions WHERE revision_id = ?",
                    (active.revision_id,),
                ).fetchone()
                if row is None:
                    raise RevisionIntegrityError("revision stream active head is missing")
                record = self._revision_record_from_row(row, active=True)
                self._verify_bound_store_identity(conn)
                conn.commit()
                return record
            except BaseException:
                try:
                    conn.rollback()
                except sqlite3.Error:
                    pass
                raise

    def admit_task(
        self,
        intent: TaskAdmissionIntent,
        envelope: TaskExecutionEnvelopeV1,
    ) -> TaskAdmissionRecord:
        """Persist one exact task request and pin or queue it without stale fallback."""

        intent = TaskAdmissionIntent.model_validate(intent.model_dump(mode="python"))
        envelope = TaskExecutionEnvelopeV1.model_validate(envelope.model_dump(mode="python"))
        request = bind_task_admission(intent, envelope)
        request_json = canonical_json(request)
        request_digest = canonical_digest(request)
        admission_id = admission_id_for_request(request)
        with self.connect() as conn:
            try:
                conn.execute("BEGIN IMMEDIATE")
                self._verify_bound_store_identity(conn)
                task_row = conn.execute(
                    "SELECT * FROM task_admissions WHERE stream_id = ? AND task_id = ?",
                    (request.stream_id, request.task_id),
                ).fetchone()
                key_row = conn.execute(
                    """
                    SELECT * FROM task_admissions
                    WHERE stream_id = ? AND idempotency_key = ?
                    """,
                    (request.stream_id, request.idempotency_key),
                ).fetchone()
                if task_row is not None and str(task_row["request_digest"]) != request_digest:
                    raise TaskAdmissionConflictError(
                        "task identity is already bound to a different admission request"
                    )
                if key_row is not None and str(key_row["request_digest"]) != request_digest:
                    raise TaskAdmissionConflictError(
                        "idempotency key is already bound to a different admission request"
                    )
                if (
                    task_row is not None
                    and key_row is not None
                    and task_row["admission_id"] != key_row["admission_id"]
                ):
                    raise RevisionIntegrityError(
                        "task and idempotency identities resolve to different admissions"
                    )
                existing = task_row if task_row is not None else key_row
                active, active_generation = self._validate_stream_authority(
                    conn,
                    request.stream_id,
                )
                if existing is not None:
                    record = self._task_admission_from_row(existing)
                    if record.request != request:
                        raise RevisionIntegrityError(
                            "persisted admission does not match its retry request"
                        )
                    self._validate_admission_authority(conn, record)
                    if record.status is AdmissionStatus.QUEUED:
                        if active_generation == request.required_generation:
                            now = utc_now_iso()
                            expected_row_bytes = _enforce_task_admission_transition_capacity(
                                conn,
                                existing,
                                status=AdmissionStatus.ADMITTED,
                                reason=None,
                                pinned_revision_id=active.revision_id,
                                updated_at=now,
                                finished_at=None,
                            )
                            updated = conn.execute(
                                """
                                UPDATE task_admissions
                                SET status = 'admitted', reason = NULL,
                                    pinned_revision_id = ?, updated_at = ?
                                WHERE admission_id = ? AND status = 'queued'
                                """,
                                (active.revision_id, now, record.admission_id),
                            )
                            if updated.rowcount != 1:
                                raise RevisionIntegrityError(
                                    "queued task admission transition was not applied"
                                )
                            existing = conn.execute(
                                "SELECT * FROM task_admissions WHERE admission_id = ?",
                                (record.admission_id,),
                            ).fetchone()
                            if existing is None:
                                raise RevisionIntegrityError("task admission disappeared")
                            _verify_task_admission_transition_capacity(
                                conn,
                                existing,
                                expected_row_bytes=expected_row_bytes,
                            )
                            record = self._task_admission_from_row(existing)
                            self._validate_admission_authority(conn, record)
                    self._verify_bound_store_identity(conn)
                    conn.commit()
                    return record
                if request.project_id != request.stream_id:
                    raise TaskAdmissionConflictError(
                        "task admission project does not match the revision stream"
                    )
                if request.required_generation < active_generation:
                    raise TaskAdmissionConflictError(
                        "required generation is older than the active revision"
                    )
                if request.required_generation > active_generation + 1:
                    raise TaskAdmissionConflictError("required revision has a generation gap")
                now = utc_now_iso()
                if request.required_generation == active_generation:
                    self._validate_admission_request_against_revision(
                        request,
                        active.identity,
                    )
                    status = AdmissionStatus.ADMITTED
                    reason = None
                    pinned_revision_id = active.revision_id
                else:
                    try:
                        self._validate_admission_request_sources(conn, request)
                    except RevisionIntegrityError as exc:
                        raise TaskAdmissionConflictError(str(exc)) from exc
                    status = AdmissionStatus.QUEUED
                    reason = AdmissionQueueReason.REQUIRED_REVISION_UNCOMMITTED
                    pinned_revision_id = None
                _enforce_ledger_capacity(
                    conn,
                    table="task_admissions",
                    text_blob_columns=_TASK_ADMISSION_TEXT_BLOB_COLUMNS,
                    new_text_blob_values=(
                        admission_id,
                        request.stream_id,
                        request.task_id,
                        request.idempotency_key,
                        request_digest,
                        request_json,
                        str(status),
                        None if reason is None else str(reason),
                        pinned_revision_id,
                        now,
                        now,
                        None,
                    ),
                    max_rows=MAX_TASK_ADMISSION_RECOVERY_ROWS,
                    max_bytes=MAX_TASK_ADMISSION_RECOVERY_BYTES,
                    max_row_bytes=MAX_TASK_ADMISSION_ROW_BYTES,
                    label="task admission ledger",
                )
                conn.execute(
                    """
                    INSERT INTO task_admissions (
                        admission_id, stream_id, task_id, idempotency_key,
                        required_generation, request_digest, request_json, status,
                        reason, pinned_revision_id, created_at, updated_at, finished_at
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL)
                    """,
                    (
                        admission_id,
                        request.stream_id,
                        request.task_id,
                        request.idempotency_key,
                        request.required_generation,
                        request_digest,
                        request_json,
                        str(status),
                        None if reason is None else str(reason),
                        pinned_revision_id,
                        now,
                        now,
                    ),
                )
                row = conn.execute(
                    "SELECT * FROM task_admissions WHERE admission_id = ?",
                    (admission_id,),
                ).fetchone()
                if row is None:
                    raise RevisionIntegrityError("created task admission could not be read back")
                record = self._task_admission_from_row(row)
                self._validate_admission_authority(conn, record)
                self._verify_bound_store_identity(conn)
                conn.commit()
                return record
            except BaseException:
                try:
                    conn.rollback()
                except sqlite3.Error:
                    pass
                raise

    def get_task_admission(self, admission_id: str) -> TaskAdmissionRecord:
        if re.fullmatch(r"adm-[0-9a-f]{64}", admission_id) is None:
            raise ValueError("task admission ID is invalid")
        with self.connect() as conn:
            try:
                conn.execute("BEGIN")
                self._verify_bound_store_identity(conn)
                row = conn.execute(
                    "SELECT * FROM task_admissions WHERE admission_id = ?",
                    (admission_id,),
                ).fetchone()
                if row is None:
                    raise RevisionNotFoundError("task admission does not exist")
                record = self._task_admission_from_row(row)
                self._validate_admission_authority(conn, record)
                self._verify_bound_store_identity(conn)
                conn.commit()
                return record
            except BaseException:
                try:
                    conn.rollback()
                except sqlite3.Error:
                    pass
                raise

    def finish_task_admission(
        self,
        admission_id: str,
        status: AdmissionStatus,
    ) -> TaskAdmissionRecord:
        if re.fullmatch(r"adm-[0-9a-f]{64}", admission_id) is None:
            raise ValueError("task admission ID is invalid")
        try:
            terminal = AdmissionStatus(status)
        except ValueError as exc:
            raise TaskAdmissionConflictError("task terminal status is invalid") from exc
        if terminal not in {
            AdmissionStatus.COMPLETED,
            AdmissionStatus.FAILED,
            AdmissionStatus.CANCELLED,
        }:
            raise TaskAdmissionConflictError("task status is not terminal")
        with self.connect() as conn:
            try:
                conn.execute("BEGIN IMMEDIATE")
                self._verify_bound_store_identity(conn)
                row = conn.execute(
                    "SELECT * FROM task_admissions WHERE admission_id = ?",
                    (admission_id,),
                ).fetchone()
                if row is None:
                    raise RevisionNotFoundError("task admission does not exist")
                record = self._task_admission_from_row(row)
                self._validate_admission_authority(conn, record)
                if record.status in {
                    AdmissionStatus.COMPLETED,
                    AdmissionStatus.FAILED,
                    AdmissionStatus.CANCELLED,
                }:
                    if record.status is not terminal:
                        raise TaskAdmissionConflictError(
                            "task admission already has a different terminal state"
                        )
                    conn.commit()
                    return record
                if (
                    record.status is AdmissionStatus.QUEUED
                    and terminal is not AdmissionStatus.CANCELLED
                ):
                    raise TaskAdmissionConflictError("queued task admission can only be cancelled")
                now = utc_now_iso()
                expected_row_bytes = _enforce_task_admission_transition_capacity(
                    conn,
                    row,
                    status=terminal,
                    reason=None,
                    pinned_revision_id=record.pinned_revision_id,
                    updated_at=now,
                    finished_at=now,
                )
                updated = conn.execute(
                    """
                    UPDATE task_admissions
                    SET status = ?, reason = NULL, updated_at = ?, finished_at = ?
                    WHERE admission_id = ?
                    """,
                    (str(terminal), now, now, admission_id),
                )
                if updated.rowcount != 1:
                    raise RevisionIntegrityError(
                        "task admission terminal transition was not applied"
                    )
                row = conn.execute(
                    "SELECT * FROM task_admissions WHERE admission_id = ?",
                    (admission_id,),
                ).fetchone()
                if row is None:
                    raise RevisionIntegrityError("task admission disappeared")
                _verify_task_admission_transition_capacity(
                    conn,
                    row,
                    expected_row_bytes=expected_row_bytes,
                )
                result = self._task_admission_from_row(row)
                self._validate_admission_authority(conn, result)
                self._verify_bound_store_identity(conn)
                conn.commit()
                return result
            except BaseException:
                try:
                    conn.rollback()
                except sqlite3.Error:
                    pass
                raise

    def active_revision_lease_count(self, revision_id: str) -> int:
        if re.fullmatch(r"rev-[0-9a-f]{64}", revision_id) is None:
            raise ValueError("revision ID is invalid")
        with self.connect() as conn:
            self._verify_bound_store_identity(conn)
            if (
                conn.execute(
                    "SELECT 1 FROM revisions WHERE revision_id = ?",
                    (revision_id,),
                ).fetchone()
                is None
            ):
                raise RevisionNotFoundError("revision does not exist")
            row = conn.execute(
                """
                SELECT COUNT(*) AS count
                FROM task_admissions
                WHERE pinned_revision_id = ? AND status = 'admitted'
                """,
                (revision_id,),
            ).fetchone()
        return int(row["count"])

    def resolve_context(self, request: ContextResolveRequest) -> ContextResolveResponse:
        raw_payload = request.model_dump(mode="python")
        _validate_finite_floats(raw_payload, "request")

        requested_artifact_order = requested_context_artifact_order(request)
        requested_artifact_ids = (
            None if requested_artifact_order is None else set(requested_artifact_order)
        )
        rows = (
            self._promoted_artifact_rows()
            if requested_artifact_order is None
            else self._requested_context_artifact_rows(requested_artifact_order)
        )
        rows = [
            row
            for row in rows
            if _artifact_id_allowed(row, requested_artifact_ids) and artifact_matches(request, row)
        ]
        if (
            requested_artifact_ids is not None
            and {str(row["artifact_id"]) for row in rows} != requested_artifact_ids
        ):
            raise ValueError("requested context artifact is incompatible")
        if requested_artifact_order is None:
            rows = sort_candidates(rows)

        selected_memory: list[dict[str, object]] = []
        rendered_parts: list[str] = []
        memory_chars = 0
        selected_agent_system: list[dict[str, object]] = []
        agent_system_parts: list[str] = []
        agent_system_chars = 0
        skills: list[dict[str, object]] = []
        adapters: list[dict[str, object]] = []
        selected_ids: list[str] = []
        selected_inventory: list[dict[str, object]] = []

        with ArtifactPayloadService(self.files.root) as payloads:
            for row in rows:
                kind = artifact_type(row)
                artifact_id = str(row["artifact_id"])
                manifest = artifact_manifest(row)
                try:
                    snapshot = payloads.issue_snapshot(
                        artifact_id=artifact_id,
                        artifact_type=str(kind),
                        name=str(row["name"]),
                        uri=str(row["uri"]),
                        manifest=manifest,
                        scores=_context_artifact_scores(row),
                        rank_index=0,
                    )
                except (OSError, ValueError):
                    if requested_artifact_ids is not None:
                        raise ValueError("requested context artifact payload is invalid")
                    continue
                inventory = {
                    "artifact_id": artifact_id,
                    "artifact_type": str(kind),
                    "content_sha256": snapshot.payload_manifest_digest,
                    "payload_entries": [
                        entry.model_dump(mode="json") for entry in snapshot.payload_entries
                    ],
                }
                selected = False
                if (
                    kind == ArtifactType.TEXT_MEMORY
                    and memory_chars < request.limits.max_memory_chars
                ):
                    separator_chars = 2 if rendered_parts else 0
                    remaining = request.limits.max_memory_chars - memory_chars - separator_chars
                    if remaining > 0:
                        text = _read_context_payload_text(
                            payloads,
                            snapshot,
                            manifest,
                            max_chars=remaining,
                        )
                        if text:
                            rendered_parts.append(text)
                            memory_chars += separator_chars + len(text)
                            selected_memory.append(
                                {
                                    "artifact_id": artifact_id,
                                    "name": row["name"],
                                    "rendered_text": text,
                                }
                            )
                            selected = True
                elif (
                    kind == ArtifactType.AGENT_SYSTEM
                    and agent_system_chars < request.limits.max_agent_system_chars
                ):
                    separator_chars = 2 if agent_system_parts else 0
                    remaining = (
                        request.limits.max_agent_system_chars
                        - agent_system_chars
                        - separator_chars
                    )
                    if remaining > 0:
                        text = _read_context_payload_text(
                            payloads,
                            snapshot,
                            manifest,
                            max_chars=remaining,
                        )
                        try:
                            target_path = normalize_agent_system_target_path(
                                manifest.get("target_path")
                            )
                        except ValueError:
                            if requested_artifact_ids is not None:
                                raise ValueError("requested agent-system target is invalid")
                        else:
                            if text:
                                agent_system_parts.append(text)
                                agent_system_chars += separator_chars + len(text)
                                selected_agent_system.append(
                                    {
                                        "artifact_id": artifact_id,
                                        "name": row["name"],
                                        "target_path": target_path,
                                        "rendered_text": text,
                                    }
                                )
                                selected = True
                elif (
                    kind == ArtifactType.SKILL_BUNDLE
                    and len(skills) < request.limits.max_skill_bundles
                ):
                    if not any(
                        entry.relative_path == "SKILL.md" for entry in snapshot.payload_entries
                    ):
                        if requested_artifact_ids is not None:
                            raise ValueError("requested skill bundle has no root SKILL.md")
                    else:
                        skills.append(
                            {
                                "artifact_id": artifact_id,
                                "name": row["name"],
                                "uri": row["uri"],
                            }
                        )
                        selected = True
                elif (
                    kind == ArtifactType.PARAMETRIC_MEMORY
                    and len(adapters) < request.limits.max_adapters
                    and not request_uses_subscription_auth(request)
                ):
                    adapter_format = manifest.get("adapter_format")
                    if not isinstance(adapter_format, str) or not adapter_format:
                        adapter_format = "lora"
                    adapter_id = manifest.get("adapter_id")
                    if not isinstance(adapter_id, str) or not adapter_id.strip():
                        adapter_id = row["name"]
                    adapters.append(
                        {
                            "artifact_id": artifact_id,
                            "adapter_id": adapter_id,
                            "uri": row["uri"],
                            "weight": 1.0,
                            "format": adapter_format,
                        }
                    )
                    selected = True
                if selected:
                    selected_ids.append(artifact_id)
                    selected_inventory.append(inventory)

        if (
            requested_artifact_order is not None
            and tuple(selected_ids) != requested_artifact_order
        ):
            raise ValueError("requested context artifact was not selected exactly")

        for _ in range(MAX_CONTEXT_ID_ATTEMPTS):
            context_id = new_id("ctx")
            response = ContextResolveResponse(
                context_id=context_id,
                memory={
                    "artifact_ids": [str(item["artifact_id"]) for item in selected_memory],
                    "rendered_text": "\n\n".join(rendered_parts),
                    "items": selected_memory,
                },
                agent_system={
                    "artifact_ids": [str(item["artifact_id"]) for item in selected_agent_system],
                    "rendered_text": "\n\n".join(agent_system_parts),
                    "target_path": (
                        selected_agent_system[0]["target_path"]
                        if selected_agent_system
                        else "AGENTS.md"
                    ),
                    "targets": selected_agent_system,
                },
                skills=skills,
                adapter_merge_spec=AdapterMergeSpec(
                    base_model=request.base_model,
                    merge_mode="runtime_lora" if adapters else "reference_only",
                    adapters=adapters,
                ),
                selection={
                    "artifact_ids": selected_ids,
                    "artifacts": selected_inventory,
                    "reasons": [
                        "matched requested compatible artifacts"
                        if requested_artifact_ids is not None
                        else "matched promoted compatible artifacts"
                    ],
                },
            )
            request_payload = request.model_dump(mode="json")
            response_payload = response.model_dump(mode="json")
            if self._persist_context(
                context_id=context_id,
                request_payload=request_payload,
                response_payload=response_payload,
                selected_ids=selected_ids,
            ):
                return response
        raise RuntimeError("could not allocate unique context id")

    def get_context_runtime_authority(self, context_id: str) -> ContextResolveResponse:
        """Read one immutable legacy context through a bounded canonical boundary."""

        if not context_id or len(context_id.encode("utf-8")) > 256:
            raise ValueError("context runtime authority identity is invalid")
        with self.connect() as conn:
            conn.execute("BEGIN")
            self._verify_bound_store_identity(conn)
            size_row = conn.execute(
                "SELECT CASE WHEN typeof(response_json) = 'text' "
                "THEN length(CAST(response_json AS BLOB)) END, "
                "CASE WHEN typeof(selected_artifact_ids_json) = 'text' "
                "THEN length(CAST(selected_artifact_ids_json AS BLOB)) END "
                "FROM contexts WHERE context_id = ?",
                (context_id,),
            ).fetchone()
            if (
                size_row is None
                or not isinstance(size_row[0], int)
                or isinstance(size_row[0], bool)
                or not isinstance(size_row[1], int)
                or isinstance(size_row[1], bool)
                or size_row[0] < 0
                or size_row[1] < 0
            ):
                raise ValueError("context runtime authority does not exist")
            response_bytes = size_row[0]
            selected_bytes = size_row[1]
            if (
                response_bytes > MAX_CONTEXT_SNAPSHOT_BYTES
                or selected_bytes > MAX_CONTEXT_SNAPSHOT_BYTES
            ):
                raise ValueError("context runtime authority exceeds its byte bound")
            row = conn.execute(
                "SELECT CASE WHEN typeof(response_json) = 'text' "
                "AND length(CAST(response_json AS BLOB)) = ? "
                "THEN response_json END AS response_json, "
                "CASE WHEN typeof(selected_artifact_ids_json) = 'text' "
                "AND length(CAST(selected_artifact_ids_json AS BLOB)) = ? "
                "THEN selected_artifact_ids_json END AS selected_json "
                "FROM contexts WHERE context_id = ?",
                (response_bytes, selected_bytes, context_id),
            ).fetchone()
            self._verify_bound_store_identity(conn)
            conn.commit()
        if (
            row is None
            or not isinstance(row["response_json"], str)
            or not isinstance(row["selected_json"], str)
            or len(row["response_json"].encode("utf-8")) != response_bytes
            or len(row["selected_json"].encode("utf-8")) != selected_bytes
        ):
            raise ValueError("context runtime authority changed while it was read")
        try:
            response = ContextResolveResponse.model_validate_json(row["response_json"])
            selected_ids = json.loads(row["selected_json"])
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise ValueError("context runtime authority is invalid") from exc
        canonical = _json_dumps(response.model_dump(mode="json"))
        selection_ids = response.selection.get("artifact_ids")
        if (
            row["response_json"] != canonical
            or response.context_id != context_id
            or not isinstance(selected_ids, list)
            or selection_ids != selected_ids
        ):
            raise ValueError("context runtime authority is inconsistent")
        return response

    def _persist_context(
        self,
        *,
        context_id: str,
        request_payload: dict[str, object],
        response_payload: dict[str, object],
        selected_ids: list[str],
        materialization: MaterializedContext | None = None,
        precommit: Callable[[], None] | None = None,
    ) -> bool:
        request_json = _json_dumps(request_payload)
        response_json = _json_dumps(response_payload)
        selected_ids_json = _json_dumps(selected_ids)
        snapshot_bytes = _context_snapshot_bytes(request_payload, response_payload)
        with self._locked_context_materialization_root() as materialization_root_fd:
            with self.connect() as conn:
                try:
                    conn.execute("BEGIN IMMEDIATE")
                    self._verify_bound_store_identity(conn)
                    if materialization is not None:
                        _require_successor_transition_not_discarded(
                            conn,
                            materialization.successor_transition_id,
                        )
                    existing = conn.execute(
                        "SELECT 1 FROM contexts WHERE context_id = ?",
                        (context_id,),
                    ).fetchone()
                    if existing is not None:
                        conn.rollback()
                        return False
                    now = utc_now_iso()
                    materialization_json = (
                        None if materialization is None else canonical_json(materialization)
                    )
                    if materialization is not None and materialization_json is not None:
                        _enforce_materialization_capacity(
                            conn,
                            (
                                context_id,
                                now,
                                materialization.registry_digest,
                                materialization.request_digest,
                                materialization_json,
                                request_json,
                                response_json,
                            ),
                        )
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
                            now,
                            request_json,
                            response_json,
                            selected_ids_json,
                        ),
                    )
                    if materialization is not None:
                        conn.execute(
                            """
                            INSERT INTO context_materializations (
                                context_id, created_at, registry_digest,
                                request_digest, manifest_json
                            )
                            VALUES (?, ?, ?, ?, ?)
                            """,
                            (
                                context_id,
                                now,
                                materialization.registry_digest,
                                materialization.request_digest,
                                materialization_json,
                            ),
                        )
                    with self._opened_bound_artifact_root() as artifact_root_fd:
                        try:
                            write_context_snapshot(
                                artifact_root_fd,
                                context_id,
                                snapshot_bytes,
                            )
                        except FileExistsError:
                            conn.rollback()
                            return False
                        self._verify_bound_store_identity(conn)
                        if precommit is not None:
                            precommit()
                        self._verify_bound_materialization_root(materialization_root_fd)
                        conn.commit()
                except BaseException:
                    try:
                        conn.rollback()
                    except sqlite3.Error:
                        pass
                    raise
        return True

    def create_plan_bound_job(
        self,
        request: PlanBoundJobCreateRequest,
        *,
        snapshot: RegistrySnapshot,
    ) -> JobCreateResponse:
        self._bind_registry_snapshot(snapshot)
        artifact_ids_by_binding = {
            binding.binding_id: list(binding.artifact_ids) for binding in request.input_bindings
        }
        plan_json = canonical_json(request.plan)
        plan_digest = canonical_digest(request.plan)
        job_id = new_id("job")
        now = utc_now_iso()
        with self.connect() as conn:
            try:
                conn.execute("BEGIN IMMEDIATE")
                _require_successor_transition_not_discarded(
                    conn,
                    request.successor_transition_id,
                )
                artifacts_by_binding = {
                    binding_id: [
                        WorkerClaimInputArtifact.model_validate(artifact)
                        for artifact in self._worker_claim_input_artifacts_from_connection(
                            conn,
                            artifact_ids,
                            sealed_successor_transition_id=(
                                request.predecessor_successor_transition_id
                            ),
                        )
                    ]
                    for binding_id, artifact_ids in artifact_ids_by_binding.items()
                }
                for binding_id, artifact_ids in artifact_ids_by_binding.items():
                    if len(artifacts_by_binding[binding_id]) != len(artifact_ids):
                        raise ValueError(
                            f"planned input binding {binding_id!r} contains an unknown artifact"
                        )
                materialized = materialize_plan_bound_job(
                    request,
                    snapshot=snapshot,
                    artifacts_by_binding=artifacts_by_binding,
                )
                input_artifact_ids = list(materialized.envelope.input_artifact_ids())
                config = materialized.envelope.legacy_flat_config()
                _validate_finite_floats(config, "config")
                input_artifact_ids_json = _json_dumps(input_artifact_ids)
                config_json = _json_dumps(config)
                envelope_json = canonical_json(materialized.envelope)
                envelope_digest = canonical_digest(materialized.envelope)
                output_types_json = _json_dumps(list(materialized.output_artifact_types))
                existing_plan = conn.execute(
                    """
                    SELECT schema_version, registry_snapshot_digest, plan_digest, plan_json
                    FROM evolution_plans WHERE plan_id = ?
                    """,
                    (request.plan.plan_id,),
                ).fetchone()
                if existing_plan is None:
                    conn.execute(
                        """
                        INSERT INTO evolution_plans (
                            plan_id, schema_version, registry_snapshot_digest,
                            plan_digest, plan_json, created_at
                        )
                        VALUES (?, ?, ?, ?, ?, ?)
                        """,
                        (
                            request.plan.plan_id,
                            request.plan.schema_version,
                            request.plan.registry_snapshot_digest,
                            plan_digest,
                            plan_json,
                            now,
                        ),
                    )
                elif (
                    str(existing_plan["schema_version"]) != request.plan.schema_version
                    or str(existing_plan["registry_snapshot_digest"])
                    != request.plan.registry_snapshot_digest
                    or str(existing_plan["plan_digest"]) != plan_digest
                    or str(existing_plan["plan_json"]) != plan_json
                ):
                    raise ValueError("plan_id is already bound to a different plan")

                selection = request.selection()
                existing_job = conn.execute(
                    "SELECT * FROM jobs WHERE plan_id = ? AND target_id = ?",
                    (request.plan.plan_id, request.target_id),
                ).fetchone()
                if existing_job is not None:
                    expected = {
                        "job_type": request.job_type,
                        "method": selection.method_id,
                        "priority": request.priority,
                        "input_artifact_ids_json": input_artifact_ids_json,
                        "config_json": config_json,
                        "method_identity_digest": materialized.method_identity_digest,
                        "execution_envelope_json": envelope_json,
                        "execution_envelope_digest": envelope_digest,
                        "declared_output_artifact_types_json": output_types_json,
                    }
                    if any(existing_job[key] != value for key, value in expected.items()):
                        raise ValueError("plan target is already bound to a different job request")
                    self._bind_plan_bound_job_transition(
                        conn,
                        job_id=str(existing_job["job_id"]),
                        successor_transition_id=request.successor_transition_id,
                        predecessor_successor_transition_id=(
                            request.predecessor_successor_transition_id
                        ),
                        created_at=now,
                    )
                    conn.commit()
                    return JobCreateResponse(
                        job_id=str(existing_job["job_id"]),
                        state=JobState(str(existing_job["state"])),
                    )
                conn.execute(
                    """
                    INSERT INTO jobs (
                        job_id, job_type, method, state, priority, created_at,
                        updated_at, input_artifact_ids_json, config_json,
                        plan_id, target_id, method_identity_digest,
                        execution_envelope_json, execution_envelope_digest,
                        declared_output_artifact_types_json, attempt_count
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        job_id,
                        request.job_type,
                        selection.method_id,
                        str(JobState.PENDING),
                        request.priority,
                        now,
                        now,
                        input_artifact_ids_json,
                        config_json,
                        request.plan.plan_id,
                        request.target_id,
                        materialized.method_identity_digest,
                        envelope_json,
                        envelope_digest,
                        output_types_json,
                        0,
                    ),
                )
                self._bind_plan_bound_job_transition(
                    conn,
                    job_id=job_id,
                    successor_transition_id=request.successor_transition_id,
                    predecessor_successor_transition_id=(
                        request.predecessor_successor_transition_id
                    ),
                    created_at=now,
                )
                conn.commit()
            except Exception:
                try:
                    conn.rollback()
                except sqlite3.Error:
                    pass
                raise
        return JobCreateResponse(job_id=job_id, state=JobState.PENDING)

    def _bind_plan_bound_job_transition(
        self,
        conn: sqlite3.Connection,
        *,
        job_id: str,
        successor_transition_id: str | None,
        predecessor_successor_transition_id: str | None,
        created_at: str,
    ) -> None:
        _require_successor_transition_not_discarded(
            conn,
            successor_transition_id,
        )
        existing = conn.execute(
            """
            SELECT successor_transition_id, predecessor_successor_transition_id
            FROM plan_bound_job_transition_bindings
            WHERE job_id = ?
            """,
            (job_id,),
        ).fetchone()
        expected = (
            successor_transition_id,
            predecessor_successor_transition_id,
        )
        if existing is not None:
            observed = (
                existing["successor_transition_id"],
                existing["predecessor_successor_transition_id"],
            )
            if observed != expected:
                raise ValueError(
                    "plan target is already bound to a different successor transition"
                )
            return
        if successor_transition_id is None:
            return
        conn.execute(
            """
            INSERT INTO plan_bound_job_transition_bindings (
                job_id, successor_transition_id,
                predecessor_successor_transition_id, created_at
            )
            VALUES (?, ?, ?, ?)
            """,
            (
                job_id,
                successor_transition_id,
                predecessor_successor_transition_id,
                created_at,
            ),
        )

        # v0.1.9 may attach an exact pre-upgrade job that already completed.
        # Move only that job's unconsumed outputs behind the transition gate.
        output_rows = conn.execute(
            """
            SELECT artifact_id, state
            FROM artifacts
            WHERE json_extract(
                lineage_json,
                '$.openevo_execution.job_id'
            ) = ?
            """,
            (job_id,),
        ).fetchall()
        output_ids = [str(row["artifact_id"]) for row in output_rows]
        unexpected = [
            str(row["artifact_id"])
            for row in output_rows
            if row["state"]
            not in {
                str(ArtifactState.ACTIVE),
                str(ArtifactState.SEALED),
            }
        ]
        if unexpected:
            raise ValueError(
                "legacy transition job has an invalid output lifecycle"
            )
        if output_ids:
            placeholders = ",".join("?" for _ in output_ids)
            downstream = conn.execute(
                f"""
                SELECT 1
                FROM artifact_lineage
                WHERE parent_artifact_id IN ({placeholders})
                LIMIT 1
                """,
                output_ids,
            ).fetchone()
            if downstream is not None:
                raise ValueError(
                    "legacy transition job output was already consumed"
                )
            conn.execute(
                f"""
                UPDATE artifacts
                SET state = ?, staging_job_id = ?
                WHERE artifact_id IN ({placeholders})
                """,
                (
                    str(ArtifactState.SEALED),
                    job_id,
                    *output_ids,
                ),
            )

    def retry_plan_bound_job(
        self,
        job_id: str,
        request: PlanBoundJobRetryRequest,
        *,
        snapshot: RegistrySnapshot,
    ) -> JobCreateResponse:
        """Requeue one exact failed plan-bound job after full authority validation."""

        self._bind_registry_snapshot(snapshot)
        manifest_paths: list[Path] = []
        with self.connect() as conn:
            try:
                conn.execute("BEGIN IMMEDIATE")
                row = conn.execute(
                    "SELECT * FROM jobs WHERE job_id = ?",
                    (job_id,),
                ).fetchone()
                if row is None:
                    raise ValueError(f"unknown job: {job_id}")
                validated = self._validate_plan_bound_job_contract(
                    conn,
                    row,
                )
                _require_successor_transition_not_discarded(
                    conn,
                    validated.successor_transition_id,
                )
                if (
                    row["plan_id"] != request.plan_id
                    or row["target_id"] != request.target_id
                ):
                    raise ValueError(
                        "plan-bound retry differs from the persisted job authority"
                    )
                state = JobState(str(row["state"]))
                prior = conn.execute(
                    "SELECT plan_id, target_id, claim_attempt_before "
                    "FROM plan_bound_job_retry_requests "
                    "WHERE job_id = ? AND retry_request_id = ?",
                    (job_id, request.retry_request_id),
                ).fetchone()
                if prior is not None:
                    if (
                        prior["plan_id"] != request.plan_id
                        or prior["target_id"] != request.target_id
                        or type(prior["claim_attempt_before"]) is not int
                        or not 0
                        <= int(prior["claim_attempt_before"])
                        <= int(row["attempt_count"])
                    ):
                        raise ValueError(
                            "plan-bound retry receipt differs from job authority"
                        )
                    conn.commit()
                    return JobCreateResponse(job_id=job_id, state=state)
                if state in {
                    JobState.CANCELLED,
                    JobState.EXPIRED,
                }:
                    if any(
                        row[field] is not None
                        for field in (
                            "claimed_by",
                            "lease_id",
                            "lease_expires_at",
                            "lease_duration_seconds",
                        )
                    ):
                        raise ValueError(
                            "terminal plan-bound job retains an active lease"
                        )
                    if int(
                        conn.execute(
                            "SELECT COUNT(*) FROM plan_bound_job_retry_requests"
                        ).fetchone()[0]
                    ) >= MAX_PLAN_BOUND_JOB_RETRY_REQUESTS:
                        raise ValueError(
                            "plan-bound job retry receipt capacity is exhausted"
                        )
                    conn.execute(
                        "INSERT INTO plan_bound_job_retry_requests("
                        "job_id, retry_request_id, plan_id, target_id, "
                        "claim_attempt_before, created_at) "
                        "VALUES (?, ?, ?, ?, ?, ?)",
                        (
                            job_id,
                            request.retry_request_id,
                            request.plan_id,
                            request.target_id,
                            int(row["attempt_count"]),
                            utc_now_iso(),
                        ),
                    )
                    manifest_paths = self._delete_staged_artifacts_for_job(
                        conn,
                        job_id,
                    )
                    cursor = conn.execute(
                        "UPDATE jobs SET state = ?, updated_at = ?, error = NULL "
                        "WHERE job_id = ? AND state = ?",
                        (
                            str(JobState.PENDING),
                            utc_now_iso(),
                            job_id,
                            str(state),
                        ),
                    )
                    if cursor.rowcount != 1:
                        raise ValueError(
                            "plan-bound retry authority changed before requeue"
                        )
                    state = JobState.PENDING
                elif state is JobState.FAILED:
                    raise ValueError(
                        "non-retryable plan-bound job requires a replacement plan"
                    )
                else:
                    raise ValueError(
                        "new plan-bound retry requires a terminal failed job"
                    )
                conn.commit()
            except Exception:
                try:
                    conn.rollback()
                except sqlite3.Error:
                    pass
                raise
        self._unlink_artifact_manifests(manifest_paths)
        return JobCreateResponse(job_id=job_id, state=state)

    def create_job(self, request: JobCreateRequest) -> JobCreateResponse:
        raw_payload = request.model_dump(mode="python")
        _validate_finite_floats(raw_payload["config"], "config")
        request_payload = request.model_dump(mode="json")
        input_artifact_ids = request_payload["input_artifact_ids"]
        input_artifact_ids_json = _json_dumps(input_artifact_ids)
        config_json = _json_dumps(request_payload["config"])

        job_id = new_id("job")
        now = utc_now_iso()
        with self.connect() as conn:
            try:
                conn.execute("BEGIN IMMEDIATE")
                self._validate_input_artifacts_exist(conn, input_artifact_ids)
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
            except Exception:
                try:
                    conn.rollback()
                except sqlite3.Error:
                    pass
                raise
        return JobCreateResponse(job_id=job_id, state=JobState.PENDING)

    def _require_sealed_input_artifact_authority(
        self,
        conn: sqlite3.Connection,
        artifact: sqlite3.Row,
        *,
        successor_transition_id: str | None,
    ) -> None:
        if artifact["state"] != str(ArtifactState.SEALED):
            return
        staging_job_id = artifact["staging_job_id"]
        if (
            successor_transition_id is None
            or not isinstance(staging_job_id, str)
        ):
            raise ValueError(
                "sealed transition artifact authority is invalid"
            )
        job_row = conn.execute(
            "SELECT * FROM jobs WHERE job_id = ?",
            (staging_job_id,),
        ).fetchone()
        binding = conn.execute(
            "SELECT successor_transition_id "
            "FROM plan_bound_job_transition_bindings "
            "WHERE job_id = ?",
            (staging_job_id,),
        ).fetchone()
        if (
            job_row is None
            or job_row["state"] != str(JobState.SUCCEEDED)
            or binding is None
            or binding["successor_transition_id"]
            != successor_transition_id
        ):
            raise ValueError(
                "sealed transition artifact authority is invalid"
            )
        validated = self._validate_plan_bound_job_identity(
            conn,
            job_row,
        )
        if (
            validated.successor_transition_id
            != successor_transition_id
        ):
            raise ValueError(
                "sealed transition artifact authority is invalid"
            )
        _require_sealed_transition_artifact_authority(
            artifact,
            job_id=staging_job_id,
            validated=validated,
        )

    def _validate_input_artifacts_exist(
        self,
        conn: sqlite3.Connection,
        artifact_ids: list[str],
        *,
        sealed_successor_transition_id: str | None = None,
    ) -> None:
        unique_ids = list(dict.fromkeys(artifact_ids))
        if not unique_ids:
            return
        rows = conn.execute(
            "SELECT * FROM artifacts "
            "WHERE artifact_id IN (%s)"
            % ",".join("?" for _ in unique_ids),  # noqa: S608
            unique_ids,
        ).fetchall()
        values = {
            str(row["artifact_id"]): row
            for row in rows
            if row["state"] != str(ArtifactState.STAGED)
        }
        missing_ids = [
            artifact_id
            for artifact_id in unique_ids
            if artifact_id not in values
        ]
        if missing_ids:
            label = "artifact_id" if len(missing_ids) == 1 else "artifact_ids"
            raise ValueError(f"unknown input {label}: {', '.join(missing_ids)}")
        for artifact_id in unique_ids:
            try:
                self._require_sealed_input_artifact_authority(
                    conn,
                    values[artifact_id],
                    successor_transition_id=(
                        sealed_successor_transition_id
                    ),
                )
            except ValueError as exc:
                raise ValueError(
                    "unknown artifact: sealed transition artifact "
                    "authority is invalid"
                ) from exc

    def _validate_plan_bound_job_identity(
        self,
        conn: sqlite3.Connection,
        row: sqlite3.Row,
    ) -> _ValidatedPlanBoundJobIdentity:
        plan_id = row["plan_id"]
        if not isinstance(plan_id, str) or not plan_id:
            raise ValueError("plan-bound job is missing plan identity")
        plan_row = conn.execute(
            "SELECT * FROM evolution_plans WHERE plan_id = ?",
            (plan_id,),
        ).fetchone()
        if plan_row is None:
            raise ValueError(f"job references unknown plan: {plan_id}")
        plan = EvolutionPlan.model_validate_json(str(plan_row["plan_json"]))
        snapshot = self._registry_snapshot
        if snapshot is None:
            raise RuntimeError("plan-bound execution requires an active registry snapshot")
        validate_plan_against_snapshot(plan, snapshot)
        plan_digest = canonical_digest(plan)
        if (
            plan.plan_id != plan_id
            or canonical_json(plan) != str(plan_row["plan_json"])
            or plan_digest != str(plan_row["plan_digest"])
            or plan.schema_version != str(plan_row["schema_version"])
            or plan.registry_snapshot_digest != str(plan_row["registry_snapshot_digest"])
        ):
            raise ValueError("persisted evolution plan identity is invalid")
        selections = tuple(
            selection for selection in plan.selections if selection.target_id == row["target_id"]
        )
        if len(selections) != 1:
            raise ValueError("plan-bound job target is not selected by its plan")
        selection = selections[0]
        if (
            selection.method_id != row["method"]
            or selection.method_identity_digest != row["method_identity_digest"]
        ):
            raise ValueError("plan-bound job method identity is invalid")
        descriptor = snapshot.methods[selection.method_id]
        if (
            snapshot.identity_digest_for("method", selection.method_id)
            != selection.method_identity_digest
        ):
            raise ValueError("plan-bound job method registry identity is invalid")

        envelope_json = str(row["execution_envelope_json"])
        envelope = MethodExecutionEnvelope.model_validate_json(envelope_json)
        if (
            canonical_json(envelope) != envelope_json
            or canonical_digest(envelope) != row["execution_envelope_digest"]
            or envelope.plan_id != plan.plan_id
            or envelope.plan_digest != plan_digest
            or envelope.registry_snapshot_digest != plan.registry_snapshot_digest
            or envelope.target_id != selection.target_id
            or envelope.method_id != selection.method_id
            or envelope.method_identity_digest != selection.method_identity_digest
            or envelope.user_config() != selection.config()
        ):
            raise ValueError("plan-bound job execution envelope is invalid")

        config = json.loads(str(row["config_json"]))
        if not isinstance(config, dict) or envelope.legacy_flat_config() != config:
            raise ValueError("plan-bound job config does not match its execution envelope")
        transition_binding = conn.execute(
            """
            SELECT successor_transition_id, predecessor_successor_transition_id
            FROM plan_bound_job_transition_bindings
            WHERE job_id = ?
            """,
            (str(row["job_id"]),),
        ).fetchone()
        successor_transition_id = (
            None
            if transition_binding is None
            else str(transition_binding["successor_transition_id"])
        )
        predecessor_successor_transition_id = (
            None
            if transition_binding is None
            or transition_binding["predecessor_successor_transition_id"]
            is None
            else str(
                transition_binding["predecessor_successor_transition_id"]
            )
        )
        input_artifact_ids = json.loads(str(row["input_artifact_ids_json"]))
        if not isinstance(input_artifact_ids, list) or any(
            not isinstance(artifact_id, str) or not artifact_id
            for artifact_id in input_artifact_ids
        ) or tuple(input_artifact_ids) != envelope.input_artifact_ids():
            raise ValueError("job input artifact IDs are invalid")

        output_types = json.loads(str(row["declared_output_artifact_types_json"]))
        if (
            not isinstance(output_types, list)
            or any(not isinstance(value, str) or not value for value in output_types)
            or tuple(output_types) != envelope.output_artifact_types
            or tuple(output_types) != descriptor.output_artifact_types
        ):
            raise ValueError("plan-bound job output artifact types are invalid")
        return _ValidatedPlanBoundJobIdentity(
            plan=plan,
            selection=selection,
            envelope=envelope,
            input_artifact_ids=tuple(input_artifact_ids),
            output_artifact_types=tuple(output_types),
            successor_transition_id=successor_transition_id,
            predecessor_successor_transition_id=(
                predecessor_successor_transition_id
            ),
        )

    def _validate_plan_bound_job_contract(
        self,
        conn: sqlite3.Connection,
        row: sqlite3.Row,
    ) -> _ValidatedPlanBoundJob:
        identity = self._validate_plan_bound_job_identity(
            conn,
            row,
        )
        input_artifacts = tuple(
            WorkerClaimInputArtifact.model_validate(artifact)
            for artifact in self._worker_claim_input_artifacts_from_connection(
                conn,
                list(identity.input_artifact_ids),
                sealed_successor_transition_id=(
                    identity.predecessor_successor_transition_id
                ),
            )
        )
        if (
            len(input_artifacts)
            != len(identity.input_artifact_ids)
            or identity.input_artifact_ids
            != identity.envelope.input_artifact_ids()
            or tuple(
                worker_input_artifact_digest(artifact)
                for artifact in input_artifacts
            )
            != identity.envelope.input_artifact_digests()
        ):
            raise ValueError(
                "plan-bound job input artifact snapshots are invalid"
            )
        return _ValidatedPlanBoundJob(
            plan=identity.plan,
            selection=identity.selection,
            envelope=identity.envelope,
            input_artifact_ids=identity.input_artifact_ids,
            input_artifacts=input_artifacts,
            output_artifact_types=(
                identity.output_artifact_types
            ),
            successor_transition_id=(
                identity.successor_transition_id
            ),
            predecessor_successor_transition_id=(
                identity.predecessor_successor_transition_id
            ),
        )

    def claim_job(self, request: WorkerClaimRequest) -> WorkerClaimResponse:
        now_dt = datetime.now(UTC)
        lease_expires_at = _utc_dt_to_iso(now_dt + timedelta(seconds=request.lease_seconds))
        base_where = "state = ?"
        base_params: list[object] = [str(JobState.PENDING)]
        base_where += (
            " AND NOT EXISTS ("
            "SELECT 1 FROM "
            "plan_bound_job_transition_bindings AS binding "
            "JOIN successor_transition_discards AS discard "
            "ON discard.successor_transition_id = "
            "binding.successor_transition_id "
            "WHERE binding.job_id = jobs.job_id)"
        )
        if request.capabilities:
            base_where += f" AND job_type IN ({','.join('?' for _ in request.capabilities)})"
            base_params.extend(request.capabilities)
        if request.method_capabilities is not None:
            if not request.method_capabilities:
                return WorkerClaimResponse(job=None)
            base_where += (
                " AND method IN (" + ",".join("?" for _ in request.method_capabilities) + ")"
            )
            base_params.extend(request.method_capabilities)

        where = base_where
        params = list(base_params)
        method_identities = request.method_identity_capabilities
        if method_identities is None:
            where += " AND plan_id IS NULL"
        elif method_identities:
            identity_terms = " OR ".join(
                "(method = ? AND method_identity_digest = ?)" for _ in method_identities
            )
            where += f" AND (plan_id IS NULL OR ({identity_terms}))"
            for method_id, identity_digest in method_identities.items():
                params.extend((method_id, identity_digest))
        else:
            where += " AND plan_id IS NULL"

        with self.connect() as conn:
            try:
                conn.execute("BEGIN IMMEDIATE")
                manifest_paths = self._requeue_expired_jobs(conn, now_dt)
                manifest_paths.extend(
                    self._quarantine_invalid_identity_mismatches(
                        conn,
                        where=base_where,
                        params=base_params,
                        method_identities=method_identities,
                    )
                )
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
                    conn.commit()
                    self._unlink_artifact_manifests(manifest_paths)
                    return WorkerClaimResponse(job=None)

                lease_id = new_id("lease")
                plan_payload: dict[str, Any] | None = None
                registry_snapshot_digest: str | None = None
                execution_envelope_payload: dict[str, Any] | None = None
                if row["plan_id"] is not None:
                    try:
                        validated = self._validate_plan_bound_job_contract(conn, row)
                    except (KeyError, TypeError, ValueError):
                        manifest_paths.extend(self._quarantine_plan_bound_job(conn, row))
                        conn.commit()
                        self._unlink_artifact_manifests(manifest_paths)
                        return WorkerClaimResponse(job=None)
                    input_artifacts = [
                        artifact.model_dump(mode="json") for artifact in validated.input_artifacts
                    ]
                    config = validated.envelope.legacy_flat_config()
                    plan_payload = validated.plan.model_dump(mode="json")
                    registry_snapshot_digest = validated.plan.registry_snapshot_digest
                    execution_envelope_payload = validated.envelope.model_dump(mode="json")
                elif any(
                    row[column] is not None
                    for column in (
                        "target_id",
                        "method_identity_digest",
                        "execution_envelope_json",
                        "execution_envelope_digest",
                        "declared_output_artifact_types_json",
                    )
                ):
                    raise ValueError("unplanned job contains partial plan identity")
                else:
                    input_artifact_ids = json.loads(str(row["input_artifact_ids_json"]))
                    if not isinstance(input_artifact_ids, list) or any(
                        not isinstance(artifact_id, str) or not artifact_id
                        for artifact_id in input_artifact_ids
                    ):
                        raise ValueError("job input artifact IDs are invalid")
                    input_artifacts = self._worker_claim_input_artifacts_from_connection(
                        conn,
                        input_artifact_ids,
                    )
                    config = json.loads(str(row["config_json"]))
                    if not isinstance(config, dict):
                        raise ValueError("job config is not a JSON object")

                response = WorkerClaimResponse(
                    job={
                        "job_id": row["job_id"],
                        "lease_id": lease_id,
                        "job_type": row["job_type"],
                        "method": row["method"],
                        "input_artifacts": input_artifacts,
                        "config": config,
                        "priority": row["priority"],
                        "state": JobState.CLAIMED,
                        "plan": plan_payload,
                        "target_id": row["target_id"],
                        "registry_snapshot_digest": registry_snapshot_digest,
                        "method_identity_digest": row["method_identity_digest"],
                        "execution_envelope": execution_envelope_payload,
                        "execution_envelope_digest": row["execution_envelope_digest"],
                    }
                )
                cursor = conn.execute(
                    """
                    UPDATE jobs
                    SET state = ?, claimed_by = ?, lease_id = ?, lease_expires_at = ?,
                        lease_duration_seconds = ?, updated_at = ?,
                        attempt_count = attempt_count + 1
                    WHERE job_id = ? AND state = ?
                    """,
                    (
                        str(JobState.CLAIMED),
                        request.worker_id,
                        lease_id,
                        lease_expires_at,
                        request.lease_seconds,
                        utc_now_iso(),
                        row["job_id"],
                        str(JobState.PENDING),
                    ),
                )
                if cursor.rowcount != 1:
                    conn.rollback()
                    return WorkerClaimResponse(job=None)
                conn.commit()
                self._unlink_artifact_manifests(manifest_paths)
            except Exception:
                try:
                    conn.rollback()
                except sqlite3.Error:
                    pass
                raise
        return response

    def _quarantine_invalid_identity_mismatches(
        self,
        conn: sqlite3.Connection,
        *,
        where: str,
        params: list[object],
        method_identities: dict[str, str] | None,
    ) -> list[Path]:
        if not method_identities:
            return []
        identity_terms = " OR ".join(
            "(method = ? AND method_identity_digest = ?)" for _ in method_identities
        )
        identity_params: list[object] = []
        for method_id, identity_digest in method_identities.items():
            identity_params.extend((method_id, identity_digest))
        rows = conn.execute(
            f"""
            SELECT * FROM jobs
            WHERE {where} AND plan_id IS NOT NULL AND NOT ({identity_terms})
            ORDER BY priority DESC, created_at ASC, job_id ASC
            """,
            [*params, *identity_params],
        ).fetchall()
        manifest_paths: list[Path] = []
        for row in rows:
            try:
                self._validate_plan_bound_job_contract(conn, row)
            except (KeyError, TypeError, ValueError):
                manifest_paths.extend(self._quarantine_plan_bound_job(conn, row))
        return manifest_paths

    def _quarantine_plan_bound_job(
        self,
        conn: sqlite3.Connection,
        row: sqlite3.Row,
    ) -> list[Path]:
        manifest_paths = self._delete_staged_artifacts_for_job(
            conn,
            str(row["job_id"]),
        )
        conn.execute(
            """
            UPDATE jobs
            SET state = ?, updated_at = ?, error = ?, claimed_by = NULL,
                lease_id = NULL, lease_expires_at = NULL,
                lease_duration_seconds = NULL
            WHERE job_id = ? AND state = ?
            """,
            (
                str(JobState.FAILED),
                utc_now_iso(),
                "plan-bound job contract validation failed",
                row["job_id"],
                str(JobState.PENDING),
            ),
        )
        return manifest_paths

    def _delete_staged_artifacts_for_job(
        self,
        conn: sqlite3.Connection,
        job_id: str,
        *,
        keep_artifact_ids: list[str] | None = None,
    ) -> list[Path]:
        keep_ids = list(dict.fromkeys(keep_artifact_ids or []))
        keep_clause = ""
        params: list[object] = [str(ArtifactState.STAGED), job_id]
        if keep_ids:
            keep_clause = " AND artifact_id NOT IN (%s)" % ",".join("?" for _ in keep_ids)
            params.extend(keep_ids)
        rows = conn.execute(
            f"""
            SELECT artifact_id, manifest_path
            FROM artifacts
            WHERE state = ? AND staging_job_id = ?
            {keep_clause}
            """,
            params,
        ).fetchall()
        artifact_ids = [str(row["artifact_id"]) for row in rows]
        if artifact_ids:
            placeholders = ",".join("?" for _ in artifact_ids)
            conn.execute(
                f"""
                DELETE FROM artifact_lineage
                WHERE parent_artifact_id IN ({placeholders})
                   OR child_artifact_id IN ({placeholders})
                """,
                (*artifact_ids, *artifact_ids),
            )
            conn.execute(
                f"DELETE FROM artifacts WHERE artifact_id IN ({placeholders})",
                artifact_ids,
            )
        return [Path(str(row["manifest_path"])) for row in rows]

    def _delete_recoverable_staged_artifacts(
        self,
        conn: sqlite3.Connection,
    ) -> list[Path]:
        rows = conn.execute(
            """
            SELECT artifacts.artifact_id, artifacts.manifest_path
            FROM artifacts
            LEFT JOIN jobs ON jobs.job_id = artifacts.staging_job_id
            WHERE artifacts.state = ?
              AND (
                artifacts.staging_job_id IS NULL
                OR jobs.job_id IS NULL
                OR jobs.state NOT IN (?, ?)
              )
            """,
            (
                str(ArtifactState.STAGED),
                str(JobState.CLAIMED),
                str(JobState.RUNNING),
            ),
        ).fetchall()
        artifact_ids = [str(row["artifact_id"]) for row in rows]
        if artifact_ids:
            placeholders = ",".join("?" for _ in artifact_ids)
            conn.execute(
                f"DELETE FROM artifacts WHERE artifact_id IN ({placeholders})",
                artifact_ids,
            )
        return [Path(str(row["manifest_path"])) for row in rows]

    def _managed_artifact_manifests(self) -> set[Path]:
        artifacts_root = self.files.root / "artifacts"
        managed: set[Path] = set()
        if artifacts_root.is_symlink() or not artifacts_root.is_dir():
            return managed
        for directory_name in _ARTIFACT_MANIFEST_DIRECTORIES:
            type_directory = artifacts_root / directory_name
            if type_directory.is_symlink() or not type_directory.is_dir():
                continue
            for artifact_directory in type_directory.iterdir():
                if artifact_directory.is_symlink() or not artifact_directory.is_dir():
                    continue
                manifest_path = artifact_directory / "manifest.json"
                if manifest_path.is_symlink() or not manifest_path.is_file():
                    continue
                managed_path = self._managed_artifact_manifest_path(manifest_path)
                if managed_path is not None:
                    managed.add(managed_path)
        return managed

    def _managed_artifact_manifest_path(self, manifest_path: Path) -> Path | None:
        artifacts_root = self.files.root / "artifacts"
        candidate = Path(os.path.abspath(manifest_path))
        try:
            relative = candidate.relative_to(artifacts_root)
        except ValueError:
            return None
        if (
            len(relative.parts) != 3
            or relative.parts[0] not in _ARTIFACT_MANIFEST_DIRECTORIES
            or relative.parts[2] != "manifest.json"
        ):
            return None
        current = artifacts_root
        if current.is_symlink():
            return None
        for part in relative.parts[:-1]:
            current /= part
            if current.is_symlink():
                return None
        if candidate.is_symlink():
            return None
        return candidate

    def _orphan_managed_artifact_manifests(
        self,
        conn: sqlite3.Connection,
    ) -> list[Path]:
        referenced_paths = set()
        for row in conn.execute("SELECT manifest_path FROM artifacts").fetchall():
            managed_path = self._managed_artifact_manifest_path(Path(str(row["manifest_path"])))
            if managed_path is not None:
                referenced_paths.add(managed_path)
        return sorted(self._managed_artifact_manifests() - referenced_paths)

    def _unlink_artifact_manifests(self, manifest_paths: list[Path]) -> None:
        for manifest_path in dict.fromkeys(manifest_paths):
            managed_path = self._managed_artifact_manifest_path(manifest_path)
            if managed_path is None:
                continue
            try:
                managed_path.unlink(missing_ok=True)
            except OSError:
                continue
            parent = managed_path.parent
            if parent.is_symlink() or self.files.root not in parent.parents:
                continue
            try:
                parent.rmdir()
            except OSError:
                pass

    @staticmethod
    def _infer_legacy_lease_duration_seconds(row: sqlite3.Row) -> int | None:
        try:
            updated_at = _parse_utc_iso(str(row["updated_at"]))
            lease_expires_at = _parse_utc_iso(str(row["lease_expires_at"]))
        except (TypeError, ValueError):
            return None
        duration_seconds = math.ceil((lease_expires_at - updated_at).total_seconds())
        return duration_seconds if duration_seconds >= 1 else None

    def _requeue_expired_jobs(
        self,
        conn: sqlite3.Connection,
        now: datetime,
    ) -> list[Path]:
        rows = conn.execute(
            """
            SELECT job_id, lease_expires_at
            FROM jobs
            WHERE state IN (?, ?) AND lease_expires_at IS NOT NULL
            """,
            (str(JobState.CLAIMED), str(JobState.RUNNING)),
        ).fetchall()
        now = now.astimezone(UTC)
        manifest_paths: list[Path] = []
        for row in rows:
            try:
                lease_expires_at = _parse_utc_iso(str(row["lease_expires_at"]))
            except ValueError:
                manifest_paths.extend(
                    self._delete_staged_artifacts_for_job(
                        conn,
                        str(row["job_id"]),
                    )
                )
                conn.execute(
                    """
                    UPDATE jobs
                    SET state = ?, claimed_by = NULL, lease_id = NULL,
                        lease_expires_at = NULL, lease_duration_seconds = NULL,
                        updated_at = ?, error = ?
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
                manifest_paths.extend(
                    self._delete_staged_artifacts_for_job(
                        conn,
                        str(row["job_id"]),
                    )
                )
                conn.execute(
                    """
                    UPDATE jobs
                    SET state = ?, claimed_by = NULL, lease_id = NULL,
                        lease_expires_at = NULL, lease_duration_seconds = NULL,
                        updated_at = ?,
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
        return manifest_paths

    def _worker_claim_input_artifacts(self, artifact_ids: list[str]) -> list[dict[str, Any]]:
        with self.connect() as conn:
            return self._worker_claim_input_artifacts_from_connection(
                conn,
                artifact_ids,
            )

    def _worker_claim_input_artifacts_from_connection(
        self,
        conn: sqlite3.Connection,
        artifact_ids: list[str],
        *,
        sealed_successor_transition_id: str | None = None,
    ) -> list[dict[str, Any]]:
        artifacts: list[dict[str, Any]] = []
        for artifact_id in artifact_ids:
            artifact = conn.execute(
                """
                SELECT artifacts.*
                FROM artifacts
                WHERE artifacts.artifact_id = ?
                  AND artifacts.state != ?
                """,
                (
                    artifact_id,
                    str(ArtifactState.STAGED),
                ),
            ).fetchone()
            if artifact is not None:
                try:
                    self._require_sealed_input_artifact_authority(
                        conn,
                        artifact,
                        successor_transition_id=(
                            sealed_successor_transition_id
                        ),
                    )
                except ValueError as exc:
                    raise ValueError(
                        "unknown artifact: sealed transition artifact "
                        "authority is invalid"
                    ) from exc
                manifest_sha256: str | None = None
                records_byte_size: int | None = None
                records_sha256: str | None = None
                manifest_json = artifact["manifest_json"]
                if isinstance(manifest_json, str) and manifest_json:
                    try:
                        artifact_manifest = json.loads(manifest_json)
                    except ValueError as exc:
                        raise ValueError(
                            "input artifact manifest is invalid"
                        ) from exc
                    if (
                        not isinstance(artifact_manifest, dict)
                        or _json_dumps(artifact_manifest)
                        != manifest_json
                    ):
                        raise ValueError(
                            "input artifact manifest is not canonical"
                        )
                    manifest_sha256 = canonical_digest(
                        artifact_manifest
                    )
                if artifact["type"] == str(ArtifactType.DATASET):
                    dataset_rows = conn.execute(
                        "SELECT dataset_id FROM datasets "
                        "WHERE artifact_id = ? LIMIT 2",
                        (artifact_id,),
                    ).fetchall()
                    if len(dataset_rows) > 1:
                        raise DatasetIntegrityError(
                            "dataset artifact has multiple dataset authorities"
                        )
                    if dataset_rows:
                        response = self._get_dataset_from_connection(
                            conn,
                            str(dataset_rows[0]["dataset_id"]),
                        )
                        if response.artifact_id != artifact_id:
                            raise DatasetIntegrityError(
                                "dataset artifact authority is inconsistent"
                            )
                        if not isinstance(artifact_manifest, dict):
                            raise DatasetIntegrityError(
                                "dataset artifact manifest is unavailable"
                            )
                        manifest_path = self.files.dataset_manifest_path(
                            str(dataset_rows[0]["dataset_id"])
                        )
                        records_path = manifest_path.with_name("records.jsonl")
                        records_byte_size_value = artifact_manifest.get(
                            "records_byte_size"
                        )
                        records_sha256_value = artifact_manifest.get(
                            "records_sha256"
                        )
                        if (
                            type(records_byte_size_value) is int
                            and isinstance(records_sha256_value, str)
                            and re.fullmatch(
                                r"[0-9a-f]{64}",
                                records_sha256_value,
                            )
                            is not None
                        ):
                            records_byte_size = records_byte_size_value
                            records_sha256 = records_sha256_value
                        else:
                            receipt_budget = _DatasetReadBudget(
                                label="legacy dataset worker receipt",
                                max_files=1,
                                max_bytes=MAX_DATASET_RECORDS_BYTES,
                            )
                            records_bytes = _bounded_regular_file_bytes(
                                records_path,
                                budget=receipt_budget,
                                max_file_bytes=MAX_DATASET_RECORDS_BYTES,
                                label="legacy dataset records",
                            )
                            records_byte_size = len(records_bytes)
                            records_sha256 = hashlib.sha256(
                                records_bytes
                            ).hexdigest()
                artifacts.append(
                    {
                        "artifact_id": artifact["artifact_id"],
                        "type": artifact["type"],
                        "uri": artifact["uri"],
                        "name": artifact["name"],
                        "manifest_sha256": manifest_sha256,
                        "records_byte_size": records_byte_size,
                        "records_sha256": records_sha256,
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

        with self.connect() as conn:
            try:
                conn.execute("BEGIN IMMEDIATE")
                row = self._assert_job_lease(conn, job_id, request.lease_id)
                lease_duration_seconds = row["lease_duration_seconds"]
                if lease_duration_seconds is None:
                    lease_duration_seconds = self._infer_legacy_lease_duration_seconds(row)
                if not isinstance(lease_duration_seconds, int) or lease_duration_seconds < 1:
                    raise JobLeaseError(f"invalid lease duration for job: {job_id}")
                renewed_expires_at = datetime.now(UTC) + timedelta(seconds=lease_duration_seconds)
                lease_expires_at = _utc_dt_to_iso(renewed_expires_at)
                conn.execute(
                    """
                    UPDATE jobs
                    SET state = ?, updated_at = ?, lease_expires_at = ?,
                        lease_duration_seconds = ?
                    WHERE job_id = ?
                    """,
                    (
                        str(JobState.RUNNING),
                        utc_now_iso(),
                        lease_expires_at,
                        lease_duration_seconds,
                        job_id,
                    ),
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

        force_unpromoted_outputs = False
        store_owned_execution_lineage: dict[str, Any] | None = None
        planned_job = False
        successor_transition_id: str | None = None
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                job_row = self._assert_job_lease(conn, job_id, request.lease_id)
                job_config = json.loads(str(job_row["config_json"]))
                if not isinstance(job_config, dict):
                    raise ValueError("job config is not a JSON object")
                force_unpromoted_outputs = job_config.get("promoted") is False
                if job_row["plan_id"] is not None:
                    planned_job = True
                    validated = self._validate_plan_bound_job_contract(conn, job_row)
                    successor_transition_id = (
                        validated.successor_transition_id
                    )
                    _require_successor_transition_not_discarded(
                        conn,
                        successor_transition_id,
                    )
                    declared_output_types = validated.output_artifact_types
                    unexpected_output_types = sorted(
                        {
                            str(artifact.type)
                            for artifact in request.artifacts
                            if str(artifact.type) not in declared_output_types
                        }
                    )
                    if unexpected_output_types:
                        raise ValueError(
                            "plan-bound job returned undeclared artifact type(s): "
                            + ", ".join(unexpected_output_types)
                        )
                    store_owned_execution_lineage = (
                        _store_owned_execution_lineage(
                            job_id=job_id,
                            validated=validated,
                        )
                    )
                conn.rollback()
            except Exception:
                try:
                    conn.rollback()
                except sqlite3.Error:
                    pass
                raise

        registered_artifact_ids: list[str] = []
        registered_artifacts: list[ArtifactResponse] = []
        stale_manifest_paths: list[Path] = []
        try:
            for artifact_request in request.artifacts:
                if store_owned_execution_lineage is not None:
                    artifact_payload = artifact_request.model_dump(mode="python")
                    artifact_payload["lineage"] = {
                        **artifact_payload["lineage"],
                        "openevo_execution": store_owned_execution_lineage,
                    }
                    artifact_request = ArtifactRegisterRequest.model_validate(artifact_payload)
                request_to_register = (
                    artifact_request.model_copy(update={"promoted": False})
                    if force_unpromoted_outputs
                    else artifact_request
                )
                artifact = self._register_artifact(
                    request_to_register,
                    initial_state=ArtifactState.STAGED,
                    staging_job_id=job_id,
                )
                registered_artifact_ids.append(artifact.artifact_id)
                registered_artifacts.append(artifact)

            with self.connect() as conn:
                try:
                    conn.execute("BEGIN IMMEDIATE")
                    job_row = self._assert_job_lease(conn, job_id, request.lease_id)
                    if (job_row["plan_id"] is not None) != planned_job:
                        raise ValueError("job plan identity changed during completion")
                    if planned_job:
                        final_validation = self._validate_plan_bound_job_contract(
                            conn,
                            job_row,
                        )
                        if (
                            final_validation.successor_transition_id
                            != successor_transition_id
                        ):
                            raise ValueError(
                                "job successor transition identity changed during completion"
                            )
                        _require_successor_transition_not_discarded(
                            conn,
                            successor_transition_id,
                        )
                        input_artifact_ids = list(final_validation.input_artifact_ids)
                    else:
                        input_artifact_ids = json.loads(str(job_row["input_artifact_ids_json"]))
                        if not isinstance(input_artifact_ids, list):
                            raise ValueError("job input artifact IDs are invalid")
                    unique_input_artifact_ids = list(dict.fromkeys(input_artifact_ids))
                    self._validate_input_artifacts_exist(
                        conn,
                        unique_input_artifact_ids,
                        sealed_successor_transition_id=(
                            final_validation.predecessor_successor_transition_id
                            if planned_job
                            else None
                        ),
                    )
                    stale_manifest_paths = self._delete_staged_artifacts_for_job(
                        conn,
                        job_id,
                        keep_artifact_ids=registered_artifact_ids,
                    )
                    for input_artifact_id in unique_input_artifact_ids:
                        for output_artifact_id in registered_artifact_ids:
                            conn.execute(
                                """
                                INSERT OR IGNORE INTO artifact_lineage (
                                    parent_artifact_id, child_artifact_id, relation
                                )
                                VALUES (?, ?, ?)
                                """,
                                (input_artifact_id, output_artifact_id, "job_input"),
                            )
                    if registered_artifact_ids:
                        placeholders = ",".join("?" for _ in registered_artifact_ids)
                        published = conn.execute(
                            f"""
                            UPDATE artifacts
                            SET state = ?, staging_job_id = ?
                            WHERE state = ? AND staging_job_id = ?
                              AND artifact_id IN ({placeholders})
                            """,
                            (
                                str(
                                    ArtifactState.SEALED
                                    if successor_transition_id is not None
                                    else ArtifactState.ACTIVE
                                ),
                                (
                                    job_id
                                    if successor_transition_id is not None
                                    else None
                                ),
                                str(ArtifactState.STAGED),
                                job_id,
                                *registered_artifact_ids,
                            ),
                        )
                        if published.rowcount != len(registered_artifact_ids):
                            raise ValueError("job output artifacts are not staged for publish")
                    if successor_transition_id is None:
                        method = str(job_row["method"])
                        for artifact in registered_artifacts:
                            self._materialize_feedback_applications_for_artifact(
                                conn,
                                artifact=artifact,
                                job_id=job_id,
                                method=method,
                            )
                    conn.execute(
                        """
                        UPDATE jobs
                        SET state = ?, updated_at = ?, claimed_by = NULL, lease_id = NULL,
                            lease_expires_at = NULL, lease_duration_seconds = NULL,
                            error = NULL
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

        self._unlink_artifact_manifests(stale_manifest_paths)
        return {
            "job_id": job_id,
            "state": str(JobState.SUCCEEDED),
            "artifact_ids": registered_artifact_ids,
        }

    def get_internal_job_result(self, job_id: str) -> _InternalJobResult:
        """Return the closed terminal observation used by Core's managed run owner."""

        with self.connect() as conn:
            try:
                conn.execute("BEGIN")
                row = conn.execute(
                    "SELECT job_id, state, error FROM jobs WHERE job_id = ?",
                    (job_id,),
                ).fetchone()
                if row is None:
                    raise ValueError(f"unknown job: {job_id}")
                transition_binding = conn.execute(
                    """
                    SELECT successor_transition_id
                    FROM plan_bound_job_transition_bindings
                    WHERE job_id = ?
                    """,
                    (job_id,),
                ).fetchone()
                successor_transition_id = (
                    None
                    if transition_binding is None
                    else str(
                        transition_binding["successor_transition_id"]
                    )
                )
                artifacts = []
                if row["state"] == str(JobState.SUCCEEDED):
                    if successor_transition_id is None:
                        artifacts = conn.execute(
                            """
                            SELECT artifact_id, type, name, state,
                                   created_at, uri, manifest_json,
                                   lineage_json, compatibility_json,
                                   scores_json, promoted,
                                   staging_job_id
                            FROM artifacts
                            WHERE state = ?
                              AND json_extract(
                                    lineage_json,
                                    '$.openevo_execution.job_id'
                                  ) = ?
                            ORDER BY created_at ASC, artifact_id ASC
                            LIMIT ?
                            """,
                            (
                                str(ArtifactState.ACTIVE),
                                job_id,
                                MAX_INTERNAL_JOB_OUTPUTS + 1,
                            ),
                        ).fetchall()
                    else:
                        job_authority = conn.execute(
                            "SELECT * FROM jobs WHERE job_id = ?",
                            (job_id,),
                        ).fetchone()
                        if job_authority is None:
                            raise ValueError(
                                "sealed transition artifact authority is invalid"
                            )
                        validated = (
                            self._validate_plan_bound_job_contract(
                                conn,
                                job_authority,
                            )
                        )
                        candidates = conn.execute(
                            """
                            SELECT artifact_id, type, name, state,
                                   created_at, uri, manifest_json,
                                   lineage_json, compatibility_json,
                                   scores_json, promoted,
                                   staging_job_id
                            FROM artifacts
                            WHERE state = ?
                              AND (
                                staging_job_id = ?
                                OR CASE
                                  WHEN json_valid(lineage_json) = 1
                                  THEN json_extract(
                                    lineage_json,
                                    '$.openevo_execution.job_id'
                                  ) = ?
                                  ELSE 0
                                END
                              )
                            ORDER BY created_at ASC, artifact_id ASC
                            LIMIT ?
                            """,
                            (
                                str(ArtifactState.SEALED),
                                job_id,
                                job_id,
                                MAX_INTERNAL_JOB_OUTPUTS + 1,
                            ),
                        ).fetchall()
                        for artifact in candidates:
                            _require_sealed_transition_artifact_authority(
                                artifact,
                                job_id=job_id,
                                validated=validated,
                            )
                        artifacts = candidates
                conn.commit()
            except BaseException:
                try:
                    conn.rollback()
                except sqlite3.Error:
                    pass
                raise
        if len(artifacts) > MAX_INTERNAL_JOB_OUTPUTS:
            raise ValueError("job output artifact count exceeds the internal bound")
        result: _InternalJobResult = {
            "artifact_ids": [],
            "error": "evolution_job_failed" if row["error"] is not None else None,
            "job_id": str(row["job_id"]),
            "retryable": (
                False
                if row["state"] == str(JobState.FAILED)
                else True
                if row["state"]
                in {
                    str(JobState.CANCELLED),
                    str(JobState.EXPIRED),
                }
                else None
            ),
            "state": str(row["state"]),
            "successor_transition_id": successor_transition_id,
        }
        if row["state"] != str(JobState.SUCCEEDED):
            return result

        outputs: list[_InternalJobOutput] = []
        with ArtifactPayloadService(self.files.root) as payloads:
            for rank_index, artifact in enumerate(artifacts):
                manifest = _internal_job_output_object(artifact, "manifest_json", "manifest")
                lineage = _internal_job_output_object(artifact, "lineage_json", "lineage")
                compatibility = _internal_job_output_object(
                    artifact,
                    "compatibility_json",
                    "compatibility",
                )
                scores = _internal_job_output_object(artifact, "scores_json", "scores")
                promoted = artifact["promoted"]
                if type(promoted) is not int or promoted not in (0, 1):
                    raise ValueError("job output promoted value is invalid")
                snapshot = payloads.issue_snapshot(
                    artifact_id=str(artifact["artifact_id"]),
                    artifact_type=str(artifact["type"]),
                    name=str(artifact["name"]),
                    uri=str(artifact["uri"]),
                    manifest=manifest,
                    scores=scores,
                    rank_index=rank_index,
                )
                outputs.append(
                    {
                        "artifact_id": snapshot.artifact_id,
                        "type": snapshot.artifact_type,
                        "name": snapshot.name,
                        "manifest": manifest,
                        "lineage": lineage,
                        "compatibility": compatibility,
                        "scores": scores,
                        "promoted": bool(promoted),
                        "created_at": str(artifact["created_at"]),
                        "payload_manifest_digest": snapshot.payload_manifest_digest,
                        "payload_byte_size": sum(
                            entry.size_bytes for entry in snapshot.payload_entries
                        ),
                        "payload_file_count": len(snapshot.payload_entries),
                    }
                )

        result["artifact_ids"] = [output["artifact_id"] for output in outputs]
        result["outputs"] = outputs
        if len(_json_dumps(result).encode("utf-8")) > MAX_INTERNAL_JOB_RESULT_BYTES:
            raise ValueError("job result exceeds the internal serialized byte bound")
        return result

    def _materialize_feedback_applications_for_artifact(
        self,
        conn: sqlite3.Connection,
        *,
        artifact: ArtifactResponse,
        job_id: str,
        method: str,
    ) -> None:
        feedback_ids = _string_list(artifact.manifest.get("human_feedback_ids"))
        if not feedback_ids:
            return
        target_type = _feedback_application_target_type(artifact.manifest)
        consumed_by_method = method
        effect_summary = _feedback_application_effect_summary(
            artifact.manifest,
            artifact=artifact,
            method=consumed_by_method,
        )
        now = utc_now_iso()
        for feedback_id in dict.fromkeys(feedback_ids):
            feedback_row = self._feedback_row(conn, feedback_id)
            if feedback_row is None:
                continue
            if feedback_row["status"] not in _CONSUMABLE_FEEDBACK_STATUSES:
                continue
            review_row = conn.execute(
                """
                SELECT status
                FROM review_requests
                WHERE review_id = ?
                """,
                (feedback_row["review_id"],),
            ).fetchone()
            if review_row is None or review_row["status"] in {
                ReviewStatus.STALE.value,
                ReviewStatus.REJECTED_INVALID.value,
                ReviewStatus.ARCHIVED_ONLY.value,
            }:
                continue
            request_payload = {
                "feedback_id": feedback_id,
                "target_type": target_type,
                "target_id": _sanitize_review_metadata_text(artifact.artifact_id),
                "consumed_by_method": _sanitize_review_metadata_text(consumed_by_method),
                "consumed_in_job_id": _sanitize_review_metadata_text(job_id),
                "effect_summary": _sanitize_review_boundary_text(effect_summary),
            }
            existing_application = conn.execute(
                """
                SELECT 1
                FROM feedback_applications
                WHERE feedback_id = ?
                  AND target_type = ?
                  AND target_id = ?
                  AND consumed_by_method = ?
                  AND COALESCE(consumed_in_job_id, '') = COALESCE(?, '')
                """,
                (
                    request_payload["feedback_id"],
                    request_payload["target_type"],
                    request_payload["target_id"],
                    request_payload["consumed_by_method"],
                    request_payload["consumed_in_job_id"],
                ),
            ).fetchone()
            if existing_application is None:
                conn.execute(
                    """
                    INSERT INTO feedback_applications (
                        application_id, feedback_id, target_type, target_id,
                        consumed_by_method, consumed_in_job_id, effect_summary,
                        created_at
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        new_id("hfa"),
                        request_payload["feedback_id"],
                        request_payload["target_type"],
                        request_payload["target_id"],
                        request_payload["consumed_by_method"],
                        request_payload["consumed_in_job_id"],
                        request_payload["effect_summary"],
                        now,
                    ),
                )
            if feedback_row["status"] == HumanFeedbackStatus.AVAILABLE_FOR_EVOLUTION.value:
                conn.execute(
                    """
                    UPDATE human_feedback
                    SET status = ?
                    WHERE feedback_id = ?
                    """,
                    (HumanFeedbackStatus.CONSUMED.value, feedback_id),
                )

    def fail_job(self, job_id: str, request: WorkerFailRequest) -> dict[str, object]:
        next_state = JobState.PENDING if request.retryable else JobState.FAILED
        error = request.error
        manifest_paths: list[Path] = []
        with self.connect() as conn:
            try:
                conn.execute("BEGIN IMMEDIATE")
                self._assert_job_lease(conn, job_id, request.lease_id)
                manifest_paths = self._delete_staged_artifacts_for_job(conn, job_id)
                conn.execute(
                    """
                    UPDATE jobs
                    SET state = ?, updated_at = ?, claimed_by = NULL, lease_id = NULL,
                        lease_expires_at = NULL, lease_duration_seconds = NULL,
                        error = ?
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
        self._unlink_artifact_manifests(manifest_paths)
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
                    lineage_reference = conn.execute(
                        """
                        SELECT 1 FROM artifact_lineage
                        WHERE parent_artifact_id = ? OR child_artifact_id = ?
                        LIMIT 1
                        """,
                        (artifact_id, artifact_id),
                    ).fetchone()
                    job_reference = any(
                        artifact_id in json.loads(str(row["input_artifact_ids_json"]))
                        for row in conn.execute(
                            "SELECT input_artifact_ids_json FROM jobs"
                        ).fetchall()
                    )
                    if lineage_reference is not None or job_reference:
                        conn.rollback()
                        continue
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
                        lease_expires_at = NULL, lease_duration_seconds = NULL,
                        error = ?
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
            clauses.append("event_type IN (%s)" % ",".join("?" for _ in request.query.event_types))
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
        if request.query.source:
            clauses.append("source = ?")
            params.append(request.query.source)
        if request.query.source_event_id:
            clauses.append("source_event_id = ?")
            params.append(request.query.source_event_id)
        if request.query.task_id:
            clauses.append("task_id = ?")
            params.append(request.query.task_id)
        if request.query.session_id:
            clauses.append("session_id = ?")
            params.append(request.query.session_id)
        where = " AND ".join(clauses) if clauses else "1 = 1"
        return conn.execute(
            f"SELECT * FROM events WHERE {where} ORDER BY ingested_at, event_id LIMIT ?",
            (*params, request.limits.max_events),
        ).fetchall()

    def _read_event_payload_file(
        self,
        row: dict[str, Any],
        *,
        budget: _DatasetReadBudget | None = None,
    ) -> dict[str, Any]:
        event_id = str(row["event_id"])
        payload_path = Path(str(row["payload_path"]))
        if payload_path != self.files.event_payload_path(event_id):
            raise DatasetIntegrityError(
                f"event {event_id} payload path differs from its authority"
            )
        if budget is None:
            budget = _DatasetReadBudget(
                label=f"event {event_id} payload",
                max_files=1,
                max_bytes=MAX_DATASET_EVENT_PAYLOAD_BYTES,
            )
        try:
            payload_bytes = _bounded_regular_file_bytes(
                payload_path,
                budget=budget,
                max_file_bytes=MAX_DATASET_EVENT_PAYLOAD_BYTES,
                label=f"event {event_id} payload file",
            )
            payload_text = payload_bytes.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ValueError(
                f"event {event_id} payload file is not valid UTF-8: {payload_path}"
            ) from exc
        try:
            payload = json.loads(payload_text)
        except json.JSONDecodeError as exc:
            raise ValueError(
                f"event {event_id} payload file is not valid JSON: {payload_path}"
            ) from exc

        if not isinstance(payload, dict):
            return {}
        if payload_bytes != json.dumps(
            payload,
            indent=2,
            sort_keys=True,
        ).encode("utf-8"):
            raise DatasetIntegrityError(
                f"event {event_id} payload file is not canonical"
            )
        return payload

    def _traces_from_event_payload(self, event_payload: dict[str, Any]) -> list[Any]:
        session_result = event_payload.get("session_result")
        if not isinstance(session_result, dict):
            return []
        trajectory = session_result.get("trajectory")
        if not isinstance(trajectory, dict):
            return []
        traces = trajectory.get("traces")
        if not isinstance(traces, list):
            return []
        return traces

    def _dataset_record_for_event_row(
        self,
        row: dict[str, Any],
        *,
        payload: dict[str, Any] | None = None,
        budget: _DatasetReadBudget | None = None,
    ) -> dict[str, Any]:
        if payload is None:
            payload = self._read_event_payload_file(row, budget=budget)
        event_payload = payload.get("payload")
        if not isinstance(event_payload, dict):
            event_payload = {}
        _sanitize_human_feedback_in_event_payload(event_payload)
        traces = self._traces_from_event_payload(event_payload)
        return {
            "event_id": row["event_id"],
            "source": row["source"],
            "event_type": row["event_type"],
            "source_event_id": row["source_event_id"],
            "created_at": row["created_at"],
            "ingested_at": row["ingested_at"],
            "task_id": row["task_id"],
            "session_id": row["session_id"],
            "policy_version": row["policy_version"],
            "rollout_step": row["rollout_step"],
            "agent_harness": row["agent_harness"],
            "agent_model": row["agent_model"],
            "base_model": row["base_model"],
            "status": row["status"],
            "reward": row["reward"],
            "trace_count": len(traces),
            "traces": traces,
            "payload": event_payload,
        }

    def _source_event_evidence_for_event_row(
        self,
        row: dict[str, Any],
        *,
        payload: dict[str, Any] | None = None,
        budget: _DatasetReadBudget | None = None,
    ) -> dict[str, Any] | None:
        if payload is None:
            payload = self._read_event_payload_file(row, budget=budget)
        event_payload = payload.get("payload")
        if not isinstance(event_payload, dict):
            return None
        session_result = event_payload.get("session_result")
        if not isinstance(session_result, dict):
            return None
        session_result_sha256 = hashlib.sha256(
            json.dumps(
                session_result,
                ensure_ascii=True,
                allow_nan=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        return {
            "event_id": row["event_id"],
            "source": row["source"],
            "event_type": row["event_type"],
            "source_event_id": row["source_event_id"],
            "task_id": row["task_id"],
            "session_id": row["session_id"],
            "session_result_sha256": session_result_sha256,
        }

    def _trace_count_for_event_row(self, row: dict[str, Any]) -> int:
        return int(self._dataset_record_for_event_row(row)["trace_count"])

    def create_dataset(self, request: DatasetCreateRequest) -> DatasetCreateResponse:
        with self._locked_dataset_materialization_root():
            return self._create_dataset_locked(request)

    def _create_dataset_locked(
        self,
        request: DatasetCreateRequest,
    ) -> DatasetCreateResponse:
        if request.idempotency_key is None:
            return self._create_dataset_materialization(request)
        request_json = _dataset_create_request_json(request)
        request_sha256 = hashlib.sha256(
            request_json.encode("utf-8")
        ).hexdigest()
        dataset_id = _dataset_id_for_idempotency_key(
            request.idempotency_key
        )
        with self.connect() as conn:
            try:
                conn.execute("BEGIN IMMEDIATE")
                prior = conn.execute(
                    "SELECT request_sha256, request_json, dataset_id, "
                    "response_json FROM dataset_create_requests "
                    "WHERE idempotency_key = ?",
                    (request.idempotency_key,),
                ).fetchone()
                if prior is None:
                    if int(
                        conn.execute(
                            "SELECT COUNT(*) FROM dataset_create_requests"
                        ).fetchone()[0]
                    ) >= MAX_DATASET_CREATE_REQUESTS:
                        raise ValueError(
                            "dataset create request capacity is exhausted"
                        )
                    conn.execute(
                        "INSERT INTO dataset_create_requests("
                        "idempotency_key, request_sha256, request_json, "
                        "dataset_id, response_json, created_at) "
                        "VALUES (?, ?, ?, ?, NULL, ?)",
                        (
                            request.idempotency_key,
                            request_sha256,
                            request_json,
                            dataset_id,
                            utc_now_iso(),
                        ),
                    )
                    response_json = None
                else:
                    if (
                        prior["request_sha256"] != request_sha256
                        or prior["request_json"] != request_json
                        or prior["dataset_id"] != dataset_id
                    ):
                        raise ValueError(
                            "dataset idempotency key is bound to another request"
                        )
                    response_json = prior["response_json"]
                conn.commit()
            except Exception:
                try:
                    conn.rollback()
                except sqlite3.Error:
                    pass
                raise
        if response_json is not None:
            response = DatasetCreateResponse.model_validate_json(
                str(response_json)
            )
            if self.get_dataset(response.dataset_id) != response:
                raise ValueError(
                    "dataset create response differs from dataset authority"
                )
            return response
        response = self._create_dataset_materialization(
            request,
            reserved_dataset_id=dataset_id,
        )
        response_json = _dataset_create_response_json(response)
        with self.connect() as conn:
            try:
                conn.execute("BEGIN IMMEDIATE")
                cursor = conn.execute(
                    "UPDATE dataset_create_requests SET response_json = ? "
                    "WHERE idempotency_key = ? AND request_sha256 = ? "
                    "AND request_json = ? AND dataset_id = ? "
                    "AND recovery_file_count IS NOT NULL "
                    "AND recovery_byte_size IS NOT NULL "
                    "AND response_json IS NULL",
                    (
                        response_json,
                        request.idempotency_key,
                        request_sha256,
                        request_json,
                        dataset_id,
                    ),
                )
                if cursor.rowcount != 1:
                    prior = conn.execute(
                        "SELECT response_json FROM dataset_create_requests "
                        "WHERE idempotency_key = ?",
                        (request.idempotency_key,),
                    ).fetchone()
                    if prior is None or prior["response_json"] != response_json:
                        raise ValueError(
                            "dataset create request authority changed before commit"
                        )
                conn.commit()
            except Exception:
                try:
                    conn.rollback()
                except sqlite3.Error:
                    pass
                raise
        return response

    def _create_dataset_materialization(
        self,
        request: DatasetCreateRequest,
        *,
        reserved_dataset_id: str | None = None,
    ) -> DatasetCreateResponse:
        raw_payload = request.model_dump(mode="python")
        _validate_finite_floats(raw_payload["query"], "query")
        _validate_finite_floats(raw_payload["limits"], "limits")
        if request.query.task_tags:
            raise ValueError("query.task_tags is not supported until events store task tags")

        request_payload = request.model_dump(mode="json", exclude_none=True)
        request_payload.pop("idempotency_key", None)
        query_json = _json_dumps(request_payload["query"])

        with self.connect() as conn:
            rows = [dict(row) for row in self._event_rows_for_dataset(conn, request)]

        materialization_budget = _DatasetReadBudget(
            label="dataset materialization",
            max_files=MAX_DATASET_READ_FILES,
            max_bytes=MAX_DATASET_MATERIALIZATION_BYTES,
        )
        event_ids: list[str] = []
        dataset_records: list[dict[str, Any]] = []
        source_event_evidence: list[dict[str, Any] | None] = []
        trace_count = 0
        for row in rows:
            if trace_count >= request.limits.max_traces:
                break
            payload = self._read_event_payload_file(
                row,
                budget=materialization_budget,
            )
            evidence = self._source_event_evidence_for_event_row(
                row,
                payload=payload,
            )
            record = self._dataset_record_for_event_row(
                row,
                payload=payload,
            )
            trace_count += int(record["trace_count"])
            event_ids.append(str(record["event_id"]))
            dataset_records.append(record)
            source_event_evidence.append(evidence)
        records_bytes = _dataset_records_bytes(dataset_records)
        records_sha256 = hashlib.sha256(records_bytes).hexdigest()

        attempts = 1 if reserved_dataset_id is not None else MAX_DATASET_ID_ATTEMPTS
        for _ in range(attempts):
            dataset_id = reserved_dataset_id or new_id("ds")
            created_at = utc_now_iso()
            manifest_path = self.files.dataset_manifest_path(dataset_id)
            records_path = manifest_path.with_name("records.jsonl")
            manifest = {
                "dataset_id": dataset_id,
                "name": request_payload["name"],
                "purpose": request_payload["purpose"],
                "query": request_payload["query"],
                "limits": request_payload["limits"],
                "event_ids": event_ids,
                "event_count": len(event_ids),
                "trace_count": trace_count,
                "records_path": records_path.name,
                "records_uri": records_path.as_uri(),
                "records_byte_size": len(records_bytes),
                "records_sha256": records_sha256,
            }
            if request.idempotency_key is not None:
                manifest["create_identity"] = request.idempotency_key
            if (
                request.idempotency_key is not None
                and len(source_event_evidence) == 1
                and source_event_evidence[0] is not None
            ):
                manifest["source_event_evidence"] = source_event_evidence[0]
            _validate_finite_floats(manifest, "manifest")

            with self.connect() as conn:
                try:
                    conn.execute("BEGIN IMMEDIATE")
                    existing = conn.execute(
                        "SELECT * FROM datasets WHERE dataset_id = ?",
                        (dataset_id,),
                    ).fetchone()
                    if existing is not None and reserved_dataset_id is not None:
                        conn.rollback()
                        return self._resume_dataset_materialization(
                            request=request,
                            dataset_id=dataset_id,
                            manifest=manifest,
                            dataset_records=dataset_records,
                        )
                    if existing is not None:
                        conn.rollback()
                        continue
                    if reserved_dataset_id is None and (
                        manifest_path.exists()
                        or records_path.exists()
                        or manifest_path.with_name(
                            f".{manifest_path.name}.pending"
                        ).exists()
                        or records_path.with_name(
                            f".{records_path.name}.pending"
                        ).exists()
                    ):
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
                    conn.commit()
                except Exception:
                    try:
                        conn.rollback()
                    except sqlite3.Error:
                        pass
                    raise

            try:
                self._ensure_dataset_materialization_files(
                    manifest_path=manifest_path,
                    manifest=manifest,
                    dataset_records=dataset_records,
                )
            except Exception:
                if reserved_dataset_id is None:
                    self._cleanup_dataset_create_failure(
                        dataset_id,
                        manifest_path,
                    )
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
                if reserved_dataset_id is None:
                    self._cleanup_dataset_create_failure(
                        dataset_id,
                        manifest_path,
                    )
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

            return self.get_dataset(dataset_id)
        raise RuntimeError("could not allocate unique dataset id")

    def _ensure_dataset_materialization_files(
        self,
        *,
        manifest_path: Path,
        manifest: dict[str, Any],
        dataset_records: list[dict[str, Any]],
    ) -> None:
        budget = _DatasetReadBudget(
            label="dataset materialization publication",
            max_files=2,
            max_bytes=MAX_DATASET_MATERIALIZATION_BYTES,
        )
        _ensure_dataset_json_file(
            self.files,
            manifest_path,
            manifest,
            budget=budget,
            max_file_bytes=MAX_DATASET_MANIFEST_BYTES,
            label="dataset manifest",
        )
        _ensure_dataset_jsonl_file(
            self.files,
            manifest_path.with_name("records.jsonl"),
            dataset_records,
            budget=budget,
            max_file_bytes=MAX_DATASET_RECORDS_BYTES,
            label="dataset records",
        )

    def _resume_dataset_materialization(
        self,
        *,
        request: DatasetCreateRequest,
        dataset_id: str,
        manifest: dict[str, Any],
        dataset_records: list[dict[str, Any]],
    ) -> DatasetCreateResponse:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT * FROM datasets WHERE dataset_id = ?",
                (dataset_id,),
            ).fetchone()
        if row is None:
            raise DatasetIntegrityError(
                "reserved dataset authority disappeared"
            )
        self._validate_dataset_resume_row(
            row,
            request=request,
            manifest=manifest,
        )
        self._ensure_dataset_materialization_files(
            manifest_path=Path(str(row["manifest_path"])),
            manifest=manifest,
            dataset_records=dataset_records,
        )
        verification_budget = _DatasetReadBudget(
            label="dataset retry verification",
            max_files=MAX_DATASET_READ_FILES,
            max_bytes=MAX_DATASET_VERIFICATION_BYTES,
        )
        with self.connect() as conn:
            persisted_manifest, persisted_records, _event_rows = (
                self._validate_dataset_storage_from_connection(
                    conn,
                    row,
                    budget=verification_budget,
                )
            )
            candidate_rows = self._dataset_artifact_candidate_rows(
                conn,
                dataset_id=dataset_id,
                manifest_path=Path(str(row["manifest_path"])),
            )
        if (
            persisted_manifest != manifest
            or persisted_records != dataset_records
        ):
            raise DatasetIntegrityError(
                "reserved dataset differs from its idempotent request"
            )
        artifact_id = (
            None if row["artifact_id"] is None else str(row["artifact_id"])
        )
        if len(candidate_rows) > 1:
            raise DatasetIntegrityError(
                "reserved dataset has multiple active artifact candidates"
            )
        candidate_ids: list[str] = []
        if candidate_rows:
            candidate = candidate_rows[0]
            self._validate_dataset_artifact_row(
                candidate,
                dataset_row=row,
                manifest=persisted_manifest,
                budget=verification_budget,
            )
            candidate_ids.append(str(candidate["artifact_id"]))
        if artifact_id is not None:
            if candidate_ids != [artifact_id]:
                raise DatasetIntegrityError(
                    "reserved dataset artifact inventory is inconsistent"
                )
            return self.get_dataset(dataset_id)
        if candidate_ids:
            artifact_id = candidate_ids[0]
        else:
            manifest_path = Path(str(row["manifest_path"]))
            artifact = self.register_artifact(
                ArtifactRegisterRequest(
                    type=ArtifactType.DATASET,
                    name=request.name,
                    uri=manifest_path.as_uri(),
                    manifest=manifest,
                    lineage={"event_ids": manifest["event_ids"]},
                    compatibility={"purpose": request.purpose},
                    tags=[request.purpose],
                    promoted=True,
                )
            )
            artifact_id = artifact.artifact_id
        self._backfill_dataset_artifact_id(dataset_id, artifact_id)
        return self.get_dataset(dataset_id)

    def _validate_dataset_resume_row(
        self,
        row: sqlite3.Row,
        *,
        request: DatasetCreateRequest,
        manifest: dict[str, Any],
    ) -> None:
        dataset_id = str(row["dataset_id"])
        request_payload = request.model_dump(
            mode="json",
            exclude_none=True,
        )
        request_payload.pop("idempotency_key", None)
        manifest_path = self.files.dataset_manifest_path(dataset_id)
        if (
            row["name"] != request_payload["name"]
            or row["purpose"] != request_payload["purpose"]
            or row["state"] != "active"
            or row["query_json"] != _json_dumps(request_payload["query"])
            or row["manifest_path"] != str(manifest_path)
            or type(row["event_count"]) is not int
            or int(row["event_count"]) != manifest["event_count"]
            or type(row["trace_count"]) is not int
            or int(row["trace_count"]) != manifest["trace_count"]
        ):
            raise DatasetIntegrityError(
                "reserved dataset row differs from its idempotent request"
            )
        with self.connect() as conn:
            membership = conn.execute(
                "SELECT event_id FROM dataset_events WHERE dataset_id = ? "
                "ORDER BY event_id",
                (dataset_id,),
            ).fetchall()
        if [str(item["event_id"]) for item in membership] != sorted(
            manifest["event_ids"]
        ):
            raise DatasetIntegrityError(
                "reserved dataset membership differs from its idempotent request"
            )

    def get_dataset(self, dataset_id: str) -> DatasetCreateResponse:
        if not isinstance(dataset_id, str) or not dataset_id:
            raise DatasetNotFoundError("dataset ID is invalid")
        with self.connect() as conn:
            try:
                conn.execute("BEGIN")
                response = self._get_dataset_from_connection(conn, dataset_id)
                conn.commit()
                return response
            except Exception:
                try:
                    conn.rollback()
                except sqlite3.Error:
                    pass
                raise

    def _get_dataset_from_connection(
        self,
        conn: sqlite3.Connection,
        dataset_id: str,
        *,
        budget: _DatasetReadBudget | None = None,
    ) -> DatasetCreateResponse:
        if budget is None:
            budget = _DatasetReadBudget(
                label="dataset verification",
                max_files=MAX_DATASET_READ_FILES,
                max_bytes=MAX_DATASET_VERIFICATION_BYTES,
            )
        row = conn.execute(
            "SELECT * FROM datasets WHERE dataset_id = ?",
            (dataset_id,),
        ).fetchone()
        if row is None or row["artifact_id"] is None:
            raise DatasetNotFoundError(f"unknown dataset: {dataset_id}")
        manifest, _records, _event_rows = (
            self._validate_dataset_storage_from_connection(
                conn,
                row,
                budget=budget,
            )
        )
        artifact_id = str(row["artifact_id"])
        candidates = self._dataset_artifact_candidate_rows(
            conn,
            dataset_id=dataset_id,
            manifest_path=Path(str(row["manifest_path"])),
        )
        if (
            len(candidates) != 1
            or str(candidates[0]["artifact_id"]) != artifact_id
        ):
            raise DatasetIntegrityError(
                f"dataset artifact inventory is inconsistent: {dataset_id}"
            )
        self._validate_dataset_artifact_row(
            candidates[0],
            dataset_row=row,
            manifest=manifest,
            budget=budget,
        )
        return DatasetCreateResponse(
            dataset_id=dataset_id,
            artifact_id=artifact_id,
            event_count=int(row["event_count"]),
            trace_count=int(row["trace_count"]),
        )

    def _validate_dataset_storage_from_connection(
        self,
        conn: sqlite3.Connection,
        row: sqlite3.Row,
        *,
        budget: _DatasetReadBudget,
    ) -> tuple[
        dict[str, Any],
        list[dict[str, Any]],
        dict[str, sqlite3.Row],
    ]:
        dataset_id = str(row["dataset_id"])
        manifest_path = Path(str(row["manifest_path"]))
        expected_manifest_path = self.files.dataset_manifest_path(dataset_id)
        if (
            row["state"] != "active"
            or manifest_path != expected_manifest_path
            or type(row["event_count"]) is not int
            or type(row["trace_count"]) is not int
            or int(row["event_count"]) < 0
            or int(row["trace_count"]) < 0
        ):
            raise DatasetIntegrityError(
                f"dataset row is inconsistent: {dataset_id}"
            )
        try:
            query = json.loads(str(row["query_json"]))
            manifest_bytes = _bounded_regular_file_bytes(
                manifest_path,
                budget=budget,
                max_file_bytes=MAX_DATASET_MANIFEST_BYTES,
                label="dataset manifest",
            )
            manifest = json.loads(manifest_bytes.decode("utf-8"))
        except DatasetIntegrityError:
            raise
        except (OSError, TypeError, UnicodeDecodeError, ValueError) as exc:
            raise DatasetIntegrityError(
                f"dataset manifest is invalid: {dataset_id}"
            ) from exc
        if not isinstance(query, dict) or not isinstance(manifest, dict):
            raise DatasetIntegrityError(
                f"dataset manifest is invalid: {dataset_id}"
            )
        if manifest_bytes != _json_dumps(
            manifest,
            indent=2,
        ).encode("utf-8"):
            raise DatasetIntegrityError(
                f"dataset manifest is not canonical: {dataset_id}"
            )
        try:
            normalized_request = DatasetCreateRequest(
                name=str(row["name"]),
                purpose=str(row["purpose"]),
                query=query,
                limits=manifest.get("limits"),
            )
        except ValueError as exc:
            raise DatasetIntegrityError(
                f"dataset manifest contract is invalid: {dataset_id}"
            ) from exc
        normalized = normalized_request.model_dump(
            mode="json",
        )
        normalized.pop("idempotency_key", None)
        allowed_query_keys = {
            "source",
            "event_types",
            "status",
            "reward_min",
            "policy_version",
            "task_tags",
            "source_event_id",
            "task_id",
            "session_id",
        }
        if (
            not set(query).issubset(allowed_query_keys)
            or any(
                normalized["query"].get(key) != value
                for key, value in query.items()
            )
            or _json_dumps(query) != row["query_json"]
            or normalized["limits"] != manifest.get("limits")
        ):
            raise DatasetIntegrityError(
                f"dataset query authority is inconsistent: {dataset_id}"
            )
        allowed_manifest_keys = {
            "dataset_id",
            "name",
            "purpose",
            "query",
            "limits",
            "event_ids",
            "event_count",
            "trace_count",
            "records_path",
            "records_uri",
            "records_byte_size",
            "records_sha256",
            "create_identity",
            "source_event_evidence",
        }
        event_ids = manifest.get("event_ids")
        records_path = manifest_path.with_name("records.jsonl")
        records_byte_size = manifest.get("records_byte_size")
        records_sha256 = manifest.get("records_sha256")
        records_receipt_present = (
            records_byte_size is not None
            and records_sha256 is not None
        )
        records_receipt_absent = (
            "records_byte_size" not in manifest
            and "records_sha256" not in manifest
        )
        has_create_receipt = (
            conn.execute(
                "SELECT 1 FROM dataset_create_requests "
                "WHERE dataset_id = ? LIMIT 1",
                (dataset_id,),
            ).fetchone()
            is not None
        )
        legacy_unjournaled_manifest = (
            records_receipt_absent
            and not has_create_receipt
            and "create_identity" not in manifest
        )
        if (
            not set(manifest).issubset(allowed_manifest_keys)
            or not isinstance(event_ids, list)
            or not all(isinstance(item, str) and item for item in event_ids)
            or len(event_ids) != len(set(event_ids))
            or manifest.get("dataset_id") != dataset_id
            or manifest.get("name") != row["name"]
            or manifest.get("purpose") != row["purpose"]
            or manifest.get("query") != query
            or manifest.get("event_count") != int(row["event_count"])
            or manifest.get("trace_count") != int(row["trace_count"])
            or len(event_ids) != int(row["event_count"])
            or manifest.get("records_path") != records_path.name
            or manifest.get("records_uri") != records_path.as_uri()
            or (
                not legacy_unjournaled_manifest
                and (
                    not records_receipt_present
                    or type(records_byte_size) is not int
                    or int(records_byte_size) < 0
                    or not isinstance(records_sha256, str)
                    or re.fullmatch(
                        r"[0-9a-f]{64}",
                        records_sha256,
                    )
                    is None
                )
            )
        ):
            raise DatasetIntegrityError(
                f"dataset manifest differs from its row: {dataset_id}"
            )
        membership_rows = conn.execute(
            "SELECT event_id FROM dataset_events WHERE dataset_id = ? "
            "ORDER BY event_id",
            (dataset_id,),
        ).fetchall()
        membership_ids = [str(item["event_id"]) for item in membership_rows]
        if sorted(event_ids) != membership_ids:
            raise DatasetIntegrityError(
                f"dataset event membership is inconsistent: {dataset_id}"
            )
        event_rows = conn.execute(
            "SELECT events.* FROM dataset_events "
            "JOIN events ON events.event_id = dataset_events.event_id "
            "WHERE dataset_events.dataset_id = ?",
            (dataset_id,),
        ).fetchall()
        event_by_id = {
            str(event["event_id"]): event
            for event in event_rows
        }
        if set(event_by_id) != set(event_ids):
            raise DatasetIntegrityError(
                f"dataset source event authority is inconsistent: {dataset_id}"
            )
        try:
            records_bytes = _bounded_regular_file_bytes(
                records_path,
                budget=budget,
                max_file_bytes=MAX_DATASET_RECORDS_BYTES,
                label="dataset records",
            )
            records = [
                json.loads(line)
                for line in records_bytes.decode("utf-8").splitlines()
                if line.strip()
            ]
        except DatasetIntegrityError:
            raise
        except (OSError, UnicodeDecodeError, ValueError) as exc:
            raise DatasetIntegrityError(
                f"dataset records are invalid: {dataset_id}"
            ) from exc
        if (
            len(records) != len(event_ids)
            or not all(isinstance(record, dict) for record in records)
            or records_bytes
            != "".join(
                f"{_json_dumps(record)}\n"
                for record in records
            ).encode("utf-8")
            or (
                records_receipt_present
                and (
                    records_byte_size != len(records_bytes)
                    or records_sha256
                    != hashlib.sha256(records_bytes).hexdigest()
                )
            )
            or [
                str(record.get("event_id"))
                for record in records
            ]
            != event_ids
        ):
            raise DatasetIntegrityError(
                f"dataset records differ from event membership: {dataset_id}"
            )
        trace_count = 0
        event_columns = (
            "event_id",
            "source",
            "event_type",
            "source_event_id",
            "created_at",
            "ingested_at",
            "task_id",
            "session_id",
            "policy_version",
            "rollout_step",
            "agent_harness",
            "agent_model",
            "base_model",
            "status",
            "reward",
        )
        source_evidence: dict[str, dict[str, Any] | None] = {}
        for record in records:
            source_row = event_by_id[str(record["event_id"])]
            source_row_dict = dict(source_row)
            payload = self._read_event_payload_file(
                source_row_dict,
                budget=budget,
            )
            source_evidence[str(record["event_id"])] = (
                self._source_event_evidence_for_event_row(
                    source_row_dict,
                    payload=payload,
                )
            )
            expected_record = self._dataset_record_for_event_row(
                source_row_dict,
                payload=payload,
            )
            traces = expected_record["traces"]
            if record != expected_record or any(
                record.get(column) != source_row[column]
                for column in event_columns
            ):
                raise DatasetIntegrityError(
                    f"dataset record authority is inconsistent: {dataset_id}"
                )
            trace_count += len(traces)
        if trace_count != int(row["trace_count"]):
            raise DatasetIntegrityError(
                f"dataset trace count is inconsistent: {dataset_id}"
            )
        evidence = manifest.get("source_event_evidence")
        if evidence is not None:
            if (
                len(event_ids) != 1
                or evidence != source_evidence[event_ids[0]]
            ):
                raise DatasetIntegrityError(
                    f"dataset source evidence is inconsistent: {dataset_id}"
                )
        return manifest, records, event_by_id

    def _dataset_artifact_candidate_rows(
        self,
        conn: sqlite3.Connection,
        *,
        dataset_id: str,
        manifest_path: Path,
    ) -> list[sqlite3.Row]:
        return conn.execute(
            "SELECT * FROM artifacts WHERE type = ? AND state != ? "
            "AND (uri = ? OR (json_valid(manifest_json) = 1 "
            "AND json_extract(manifest_json, '$.dataset_id') = ?)) "
            "ORDER BY artifact_id LIMIT 2",
            (
                str(ArtifactType.DATASET),
                str(ArtifactState.STAGED),
                manifest_path.as_uri(),
                dataset_id,
            ),
        ).fetchall()

    def _validate_dataset_artifact_row(
        self,
        artifact_row: sqlite3.Row,
        *,
        dataset_row: sqlite3.Row,
        manifest: dict[str, Any],
        budget: _DatasetReadBudget,
    ) -> None:
        dataset_id = str(dataset_row["dataset_id"])
        artifact_id = str(artifact_row["artifact_id"])
        manifest_path = Path(str(dataset_row["manifest_path"]))
        artifact_manifest_path = Path(str(artifact_row["manifest_path"]))
        lineage = {"event_ids": manifest["event_ids"]}
        compatibility = {"purpose": str(dataset_row["purpose"])}
        expected_wrapper = {
            "artifact_id": artifact_id,
            "type": str(ArtifactType.DATASET),
            "name": str(dataset_row["name"]),
            "uri": manifest_path.as_uri(),
            "manifest": manifest,
            "lineage": lineage,
            "compatibility": compatibility,
            "scores": {},
            "tags": [str(dataset_row["purpose"])],
            "promoted": True,
        }
        try:
            artifact_wrapper_bytes = _bounded_regular_file_bytes(
                artifact_manifest_path,
                budget=budget,
                max_file_bytes=MAX_DATASET_ARTIFACT_MANIFEST_BYTES,
                label="dataset artifact manifest",
            )
            artifact_wrapper = json.loads(
                artifact_wrapper_bytes.decode("utf-8")
            )
        except DatasetIntegrityError:
            raise
        except (OSError, UnicodeDecodeError, ValueError) as exc:
            raise DatasetIntegrityError(
                f"dataset artifact manifest is invalid: {dataset_id}"
            ) from exc
        if (
            artifact_row["type"] != str(ArtifactType.DATASET)
            or artifact_row["state"] != str(ArtifactState.ACTIVE)
            or artifact_row["name"] != dataset_row["name"]
            or int(artifact_row["version"]) != 1
            or artifact_row["uri"] != manifest_path.as_uri()
            or artifact_manifest_path
            != self.files.artifact_manifest_path(
                str(ArtifactType.DATASET),
                artifact_id,
            )
            or artifact_row["manifest_json"] != _json_dumps(manifest)
            or artifact_row["lineage_json"] != _json_dumps(lineage)
            or artifact_row["compatibility_json"]
            != _json_dumps(compatibility)
            or artifact_row["scores_json"] != _json_dumps({})
            or artifact_row["tags_json"]
            != _json_dumps([str(dataset_row["purpose"])])
            or artifact_row["promoted"] != 1
            or artifact_row["staging_job_id"] is not None
            or artifact_wrapper != expected_wrapper
            or artifact_wrapper_bytes
            != _json_dumps(
                artifact_wrapper,
                indent=2,
            ).encode("utf-8")
        ):
            raise DatasetIntegrityError(
                f"dataset artifact differs from dataset authority: {dataset_id}"
            )

    def _backfill_dataset_artifact_id(self, dataset_id: str, artifact_id: str) -> None:
        budget = _DatasetReadBudget(
            label="dataset recovery admission",
            max_files=MAX_DATASET_READ_FILES,
            max_bytes=MAX_DATASET_VERIFICATION_BYTES,
        )
        with self.connect() as conn:
            try:
                conn.execute("BEGIN IMMEDIATE")
                cursor = conn.execute(
                    "UPDATE datasets SET artifact_id = ? "
                    "WHERE dataset_id = ? "
                    "AND (artifact_id IS NULL OR artifact_id = ?)",
                    (artifact_id, dataset_id, artifact_id),
                )
                if cursor.rowcount != 1:
                    raise DatasetIntegrityError(
                        "dataset already binds a different artifact"
                    )
                self._get_dataset_from_connection(
                    conn,
                    dataset_id,
                    budget=budget,
                )
                request_row = conn.execute(
                    "SELECT idempotency_key, recovery_file_count, "
                    "recovery_byte_size FROM dataset_create_requests "
                    "WHERE dataset_id = ?",
                    (dataset_id,),
                ).fetchone()
                if request_row is not None:
                    recovery_file_count = request_row[
                        "recovery_file_count"
                    ]
                    recovery_byte_size = request_row[
                        "recovery_byte_size"
                    ]
                    if (
                        (recovery_file_count is None)
                        != (recovery_byte_size is None)
                    ):
                        raise DatasetIntegrityError(
                            "dataset recovery accounting is incomplete"
                        )
                    if recovery_file_count is None:
                        totals = conn.execute(
                            "SELECT "
                            "COALESCE(SUM(recovery_file_count), 0), "
                            "COALESCE(SUM(recovery_byte_size), 0) "
                            "FROM dataset_create_requests"
                        ).fetchone()
                        if (
                            int(totals[0]) + budget.files
                            > MAX_DATASET_READ_FILES
                            or int(totals[1]) + budget.bytes
                            > MAX_DATASET_STARTUP_RECOVERY_BYTES
                        ):
                            raise DatasetIntegrityError(
                                "dataset startup recovery capacity is exhausted"
                            )
                        updated = conn.execute(
                            "UPDATE dataset_create_requests "
                            "SET recovery_file_count = ?, "
                            "recovery_byte_size = ? "
                            "WHERE idempotency_key = ? "
                            "AND recovery_file_count IS NULL "
                            "AND recovery_byte_size IS NULL",
                            (
                                budget.files,
                                budget.bytes,
                                request_row["idempotency_key"],
                            ),
                        )
                        if updated.rowcount != 1:
                            raise DatasetIntegrityError(
                                "dataset recovery admission lost its fence"
                            )
                    elif (
                        type(recovery_file_count) is not int
                        or type(recovery_byte_size) is not int
                        or recovery_file_count != budget.files
                        or recovery_byte_size != budget.bytes
                    ):
                        raise DatasetIntegrityError(
                            "dataset recovery accounting differs from authority"
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
        artifact_deleted = False
        dataset_deleted = False
        with self.connect() as conn:
            try:
                conn.execute("BEGIN IMMEDIATE")
                dataset_row = conn.execute(
                    "SELECT artifact_id, manifest_path FROM datasets "
                    "WHERE dataset_id = ?",
                    (dataset_id,),
                ).fetchone()
                if (
                    dataset_row is not None
                    and dataset_row["manifest_path"] != str(dataset_manifest_path)
                ):
                    raise DatasetIntegrityError(
                        "dataset cleanup target differs from dataset authority"
                    )
                authoritative_artifact_id = (
                    None
                    if dataset_row is None
                    or dataset_row["artifact_id"] is None
                    else str(dataset_row["artifact_id"])
                )
                if (
                    artifact_id is not None
                    and authoritative_artifact_id != artifact_id
                ):
                    artifact_row = conn.execute(
                        "SELECT type, uri, manifest_path FROM artifacts "
                        "WHERE artifact_id = ?",
                        (artifact_id,),
                    ).fetchone()
                    referenced = conn.execute(
                        "SELECT 1 FROM datasets WHERE artifact_id = ? LIMIT 1",
                        (artifact_id,),
                    ).fetchone()
                    if artifact_row is not None:
                        expected_artifact_manifest_path = (
                            self.files.artifact_manifest_path(
                                str(ArtifactType.DATASET),
                                artifact_id,
                            )
                        )
                        if (
                            artifact_row["type"] != str(ArtifactType.DATASET)
                            or artifact_row["uri"]
                            != dataset_manifest_path.as_uri()
                            or artifact_row["manifest_path"]
                            != str(expected_artifact_manifest_path)
                            or referenced is not None
                        ):
                            raise DatasetIntegrityError(
                                "dataset cleanup artifact is not an unbound candidate"
                            )
                        artifact_manifest_path = Path(
                            str(artifact_row["manifest_path"])
                        )
                        conn.execute(
                            "DELETE FROM artifact_lineage "
                            "WHERE parent_artifact_id = ? "
                            "OR child_artifact_id = ?",
                            (artifact_id, artifact_id),
                        )
                        deleted = conn.execute(
                            "DELETE FROM artifacts WHERE artifact_id = ? "
                            "AND type = ? AND uri = ?",
                            (
                                artifact_id,
                                str(ArtifactType.DATASET),
                                dataset_manifest_path.as_uri(),
                            ),
                        )
                        artifact_deleted = deleted.rowcount == 1
                if (
                    dataset_row is not None
                    and authoritative_artifact_id is None
                ):
                    conn.execute(
                        "DELETE FROM dataset_events WHERE dataset_id = ?",
                        (dataset_id,),
                    )
                    deleted = conn.execute(
                        "DELETE FROM datasets WHERE dataset_id = ? "
                        "AND artifact_id IS NULL",
                        (dataset_id,),
                    )
                    dataset_deleted = deleted.rowcount == 1
                    if not dataset_deleted:
                        raise DatasetIntegrityError(
                            "dataset cleanup lost its null-artifact fence"
                        )
                conn.commit()
            except Exception:
                try:
                    conn.rollback()
                except sqlite3.Error:
                    pass
                raise

        if dataset_deleted:
            dataset_manifest_path.unlink(missing_ok=True)
            dataset_manifest_path.with_name("records.jsonl").unlink(missing_ok=True)
            dataset_manifest_path.with_name(
                f".{dataset_manifest_path.name}.pending"
            ).unlink(missing_ok=True)
            records_path = dataset_manifest_path.with_name("records.jsonl")
            records_path.with_name(
                f".{records_path.name}.pending"
            ).unlink(missing_ok=True)
        if artifact_deleted and artifact_manifest_path is not None:
            artifact_manifest_path.unlink(missing_ok=True)
            artifact_manifest_path.with_name(
                f".{artifact_manifest_path.name}.pending"
            ).unlink(missing_ok=True)

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        try:
            yield conn
        finally:
            conn.close()


def _artifact_id_allowed(
    row: dict[str, object],
    requested_artifact_ids: set[str] | None,
) -> bool:
    if requested_artifact_ids is None:
        return True
    artifact_id = row.get("artifact_id")
    return isinstance(artifact_id, str) and artifact_id in requested_artifact_ids


def _context_artifact_scores(row: dict[str, object]) -> dict[str, object]:
    try:
        value = json.loads(str(row["scores_json"]))
    except (KeyError, TypeError, json.JSONDecodeError) as exc:
        raise ValueError("context artifact scores are invalid") from exc
    if not isinstance(value, dict):
        raise ValueError("context artifact scores must be an object")
    return value


def _read_context_payload_text(
    payloads: ArtifactPayloadService,
    snapshot: object,
    manifest: dict[str, Any],
    *,
    max_chars: int,
) -> str:
    payload_entries = getattr(snapshot, "payload_entries", ())
    payload_handle = getattr(snapshot, "payload_handle", None)
    paths = [entry.relative_path for entry in payload_entries]
    content_path = manifest.get("content_path")
    if isinstance(content_path, str) and content_path in paths:
        selected_path = content_path
    elif len(paths) == 1:
        selected_path = paths[0]
    else:
        raise ValueError("text artifact payload has no unambiguous content path")
    if not isinstance(payload_handle, str):
        raise ValueError("text artifact payload handle is invalid")
    bounded_chars = min(max_chars, 1_048_576)
    return payloads.read_utf8_prefix(
        payload_handle,
        selected_path,
        max_chars=bounded_chars,
        max_bytes=1_048_576,
    )


def _internal_job_output_object(
    row: sqlite3.Row,
    column: str,
    label: str,
) -> dict[str, object]:
    try:
        value = json.loads(str(row[column]))
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError(f"job output {label} is invalid") from exc
    if not isinstance(value, dict):
        raise ValueError(f"job output {label} must be an object")
    return value


def _artifact_response_from_row(row: sqlite3.Row) -> ArtifactResponse:
    manifest_path = Path(str(row["manifest_path"]))
    manifest_payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest = manifest_payload.get("manifest") if isinstance(manifest_payload, dict) else {}
    if not isinstance(manifest, dict):
        manifest = {}
    return ArtifactResponse(
        artifact_id=str(row["artifact_id"]),
        type=row["type"],
        name=str(row["name"]),
        version=int(row["version"]),
        state=row["state"],
        uri=str(row["uri"]),
        manifest=manifest,
        compatibility=json.loads(str(row["compatibility_json"])),
        scores=json.loads(str(row["scores_json"])),
        tags=json.loads(str(row["tags_json"])),
        promoted=bool(row["promoted"]),
    )


def _review_request_response_from_row(row: sqlite3.Row) -> ReviewRequestResponse:
    return ReviewRequestResponse(
        review_id=str(row["review_id"]),
        review_type=str(row["review_type"]),
        status=str(row["status"]),
        artifact_ids=json.loads(str(row["artifact_ids_json"])),
        candidate_ids=json.loads(str(row["candidate_ids_json"])),
        job_id=row["job_id"],
        task_id=row["task_id"],
        round_index=row["round_index"],
        method=row["method"],
        artifact_type=row["artifact_type"],
        packet_id=str(row["packet_id"]),
        packet_hash=str(row["packet_hash"]),
        packet=json.loads(str(row["packet_json"])),
        artifact_hashes=json.loads(str(row["artifact_hashes_json"])),
        query_decision_id=row["query_decision_id"],
        assigned_to=row["assigned_to"],
        reviewer_role=row["reviewer_role"],
        adjudication_rationale=row["adjudication_rationale"],
        priority=int(row["priority"]),
        created_at=str(row["created_at"]),
        updated_at=str(row["updated_at"]),
    )


def _review_packet_response_from_row(row: sqlite3.Row) -> ReviewPacketResponse:
    return ReviewPacketResponse(
        packet_id=str(row["packet_id"]),
        packet_hash=str(row["packet_hash"]),
        packet=json.loads(str(row["packet_json"])),
        created_at=str(row["created_at"]),
    )


def _human_feedback_response_from_row(row: sqlite3.Row) -> HumanFeedbackResponse:
    return HumanFeedbackResponse(
        feedback_id=str(row["feedback_id"]),
        review_id=str(row["review_id"]),
        reviewer_id=str(row["reviewer_id"]),
        reviewer_role=row["reviewer_role"],
        status=str(row["status"]),
        decision=str(row["decision"]),
        score=row["score"],
        confidence=row["confidence"],
        rationale=str(row["rationale"]),
        normalized_payload=json.loads(str(row["normalized_payload_json"])),
        created_at=str(row["created_at"]),
    )


def _feedback_application_response_from_row(row: sqlite3.Row) -> FeedbackApplicationResponse:
    return FeedbackApplicationResponse(
        application_id=str(row["application_id"]),
        feedback_id=str(row["feedback_id"]),
        target_type=str(row["target_type"]),
        target_id=str(row["target_id"]),
        consumed_by_method=str(row["consumed_by_method"]),
        consumed_in_job_id=row["consumed_in_job_id"],
        effect_summary=str(row["effect_summary"]),
        created_at=str(row["created_at"]),
    )


def _human_query_decision_response_from_row(row: sqlite3.Row) -> HumanQueryDecisionResponse:
    feedback_changed_promotion = row["feedback_changed_promotion"]
    feedback_changed_next_candidate = row["feedback_changed_next_candidate"]
    return HumanQueryDecisionResponse(
        query_decision_id=str(row["query_decision_id"]),
        artifact_ids=json.loads(str(row["artifact_ids_json"])),
        candidate_ids=json.loads(str(row["candidate_ids_json"])),
        task_id=row["task_id"],
        round_index=row["round_index"],
        method=row["method"],
        decision=str(row["decision"]),
        reason_codes=json.loads(str(row["reason_codes_json"])),
        estimated_value_of_information=row["estimated_value_of_information"],
        estimated_human_cost=row["estimated_human_cost"],
        budget_context=json.loads(str(row["budget_context_json"])),
        actual_latency_seconds=row["actual_latency_seconds"],
        feedback_changed_promotion=(
            None if feedback_changed_promotion is None else bool(feedback_changed_promotion)
        ),
        feedback_changed_next_candidate=(
            None
            if feedback_changed_next_candidate is None
            else bool(feedback_changed_next_candidate)
        ),
        downstream_delta=row["downstream_delta"],
        review_id=row["review_id"],
        created_at=str(row["created_at"]),
    )
