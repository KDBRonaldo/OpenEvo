"""Durable private ledger for Core-owned science run execution."""

from __future__ import annotations

import base64
from collections.abc import Callable, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone
from enum import StrEnum
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import sqlite3
import stat
import threading
from typing import Iterator, TypeVar

from pydantic import BaseModel

from openevo.backend.contracts.v1 import models as m
from openevo.backend.contracts.v2 import models as m2
from openevo.backend.science_execution_v2 import (
    MAX_CAPTURED_SESSION_RESULT_BYTES,
    ScienceAttemptExecutionEvidenceV2,
    ScienceAttemptExecutionReceiptV2,
    ScienceAttemptExecutionRecordV2,
    canonical_subscription_session_result,
    science_attempt_execution_receipt_sha256,
    science_session_result_bytes,
    science_session_result_sha256,
)
from openevo.backend.science_successor import (
    ScienceSuccessorCleanupReceiptV2,
    ScienceSuccessorPlanV2,
    ScienceSuccessorTransitionAttemptV2,
    science_successor_plan_sha256,
)
from openevo.evolution.revisions import (
    AtomicEvolutionAbandonManifestV2,
    AtomicSuccessorCommitV2,
    AtomicSuccessorManifestV2,
    SuccessorArtifactContributionV2,
    TaskArtifactPublicationV2,
    atomic_successor_manifest_sha256,
)
from openevo.internal_auth import (
    GenerationBoundRunAdmissionCheck,
    RunAdmissionOperation,
)
from openevo.rollout.models import SessionResult


_T = TypeVar("_T", bound=BaseModel)
_SCHEMA = """
CREATE TABLE IF NOT EXISTS metadata (
    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
    schema_version INTEGER NOT NULL CHECK (schema_version = 1)
) STRICT;
CREATE TABLE IF NOT EXISTS runs (
    run_id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL,
    idempotency_key TEXT NOT NULL,
    request_digest TEXT NOT NULL CHECK (length(request_digest) = 64),
    request_json BLOB NOT NULL,
    run_json BLOB NOT NULL,
    input_context_json BLOB NOT NULL,
    result_json BLOB,
    resource_version INTEGER NOT NULL CHECK (resource_version >= 1),
    deleted INTEGER NOT NULL DEFAULT 0 CHECK (deleted IN (0, 1)),
    UNIQUE(project_id, idempotency_key)
) STRICT;
CREATE TABLE IF NOT EXISTS pending_run_creates (
    project_id TEXT NOT NULL,
    idempotency_key TEXT NOT NULL,
    run_id TEXT NOT NULL UNIQUE,
    request_digest TEXT NOT NULL CHECK (length(request_digest) = 64),
    request_json BLOB NOT NULL,
    PRIMARY KEY(project_id, idempotency_key)
) STRICT;
CREATE TABLE IF NOT EXISTS mutations (
    operation_id TEXT NOT NULL,
    run_id TEXT NOT NULL,
    idempotency_key TEXT NOT NULL,
    request_digest TEXT NOT NULL CHECK (length(request_digest) = 64),
    response_json BLOB,
    status_code INTEGER NOT NULL,
    PRIMARY KEY(operation_id, run_id, idempotency_key),
    FOREIGN KEY(run_id) REFERENCES runs(run_id) ON DELETE CASCADE
) STRICT;
CREATE TABLE IF NOT EXISTS timeline (
    run_id TEXT NOT NULL,
    sequence INTEGER NOT NULL CHECK (sequence >= 0),
    entry_json BLOB NOT NULL,
    PRIMARY KEY(run_id, sequence),
    FOREIGN KEY(run_id) REFERENCES runs(run_id) ON DELETE CASCADE
) STRICT;
CREATE TABLE IF NOT EXISTS logs (
    run_id TEXT NOT NULL,
    sequence INTEGER NOT NULL CHECK (sequence >= 0),
    entry_json BLOB NOT NULL,
    PRIMARY KEY(run_id, sequence),
    FOREIGN KEY(run_id) REFERENCES runs(run_id) ON DELETE CASCADE
) STRICT;
CREATE TABLE IF NOT EXISTS artifacts (
    artifact_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL,
    revision_id TEXT NOT NULL,
    artifact_json BLOB NOT NULL,
    FOREIGN KEY(run_id) REFERENCES runs(run_id) ON DELETE CASCADE
) STRICT;
CREATE TABLE IF NOT EXISTS revision_contexts (
    revision_id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL,
    context_json BLOB NOT NULL
) STRICT;
CREATE TABLE IF NOT EXISTS admissions (
    run_id TEXT NOT NULL,
    operation TEXT NOT NULL,
    task_id TEXT NOT NULL,
    session_id TEXT NOT NULL,
    generation_digest TEXT NOT NULL CHECK (length(generation_digest) = 64),
    registry_digest TEXT NOT NULL CHECK (length(registry_digest) = 64),
    framework_lock_digest TEXT NOT NULL CHECK (length(framework_lock_digest) = 64),
    payload_sha256 TEXT NOT NULL CHECK (length(payload_sha256) = 64),
    PRIMARY KEY(run_id, operation, task_id, session_id),
    FOREIGN KEY(run_id) REFERENCES runs(run_id) ON DELETE CASCADE
) STRICT;
CREATE INDEX IF NOT EXISTS admissions_task_idx ON admissions(task_id, operation);
CREATE UNIQUE INDEX IF NOT EXISTS admissions_rollout_task_idx
ON admissions(task_id)
WHERE operation = 'rollout_task_submit';
"""
_MAX_RUNS = 10_000
_MAX_DOCUMENT_BYTES = 4 * 1024 * 1024
_MAX_TIMELINE_ROWS = 100_000
_MAX_TIMELINE_PER_RUN = 10_000
_MAX_LOG_ROWS = 1_000_000
_MAX_LOGS_PER_RUN = 50_000
_MAX_ARTIFACT_ROWS = 100_000
_MAX_ARTIFACTS_PER_RUN = 1_024
_MAX_ADMISSION_ROWS = 100_000
_MAX_ADMISSIONS_PER_RUN = 4_096
_ACTIVE_STATUSES = frozenset({m.RunStatus.PREPARING, m.RunStatus.RUNNING, m.RunStatus.CANCELLING})
_IN_FLIGHT_STATUSES = _ACTIVE_STATUSES | {m.RunStatus.QUEUED}

_V2_SCHEMA = """
CREATE TABLE IF NOT EXISTS metadata (
    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
    schema_version INTEGER NOT NULL CHECK (schema_version = 2)
) STRICT;
CREATE TABLE IF NOT EXISTS action_receipt_migrations (
    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
    migration_version INTEGER NOT NULL
        CHECK (migration_version = 1)
) STRICT;
CREATE TABLE IF NOT EXISTS project_admission_authorities (
    project_id TEXT PRIMARY KEY,
    authority_json BLOB NOT NULL,
    resource_version INTEGER NOT NULL CHECK (resource_version >= 1)
) STRICT;
CREATE TABLE IF NOT EXISTS project_heads (
    project_head_id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL,
    generation INTEGER NOT NULL CHECK (generation >= 0),
    predecessor_project_head_id TEXT,
    manifest_sha256 TEXT NOT NULL CHECK (length(manifest_sha256) = 64),
    head_json BLOB NOT NULL,
    UNIQUE(project_id, generation),
    FOREIGN KEY(predecessor_project_head_id)
        REFERENCES project_heads(project_head_id) ON DELETE RESTRICT
) STRICT;
CREATE TABLE IF NOT EXISTS tasks (
    task_id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL,
    task_admission_id TEXT NOT NULL UNIQUE,
    admission_sha256 TEXT NOT NULL CHECK (length(admission_sha256) = 64),
    idempotency_key TEXT NOT NULL,
    request_sha256 TEXT NOT NULL CHECK (length(request_sha256) = 64),
    request_json BLOB NOT NULL,
    task_json BLOB NOT NULL,
    closed INTEGER NOT NULL DEFAULT 0 CHECK (closed IN (0, 1)),
    resource_version INTEGER NOT NULL CHECK (resource_version >= 1),
    UNIQUE(project_id, idempotency_key)
) STRICT;
CREATE TABLE IF NOT EXISTS task_admissions (
    task_admission_id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL UNIQUE,
    admission_sha256 TEXT NOT NULL UNIQUE CHECK (length(admission_sha256) = 64),
    admission_json BLOB NOT NULL,
    FOREIGN KEY(task_id) REFERENCES tasks(task_id) ON DELETE RESTRICT
) STRICT;
CREATE TABLE IF NOT EXISTS task_close_receipts (
    task_id TEXT PRIMARY KEY,
    close_request_id TEXT NOT NULL UNIQUE
        CHECK (length(close_request_id) BETWEEN 1 AND 128),
    expected_etag TEXT
        CHECK (expected_etag IS NULL OR length(expected_etag) = 66),
    task_admission_id TEXT NOT NULL
        CHECK (length(task_admission_id) BETWEEN 1 AND 128),
    admission_sha256 TEXT NOT NULL
        CHECK (length(admission_sha256) = 64),
    FOREIGN KEY(task_id) REFERENCES tasks(task_id) ON DELETE RESTRICT,
    FOREIGN KEY(task_admission_id)
        REFERENCES task_admissions(task_admission_id) ON DELETE RESTRICT
) STRICT;
CREATE TABLE IF NOT EXISTS legacy_task_close_exemptions (
    task_id TEXT PRIMARY KEY,
    task_admission_id TEXT NOT NULL UNIQUE
        CHECK (length(task_admission_id) BETWEEN 1 AND 128),
    admission_sha256 TEXT NOT NULL UNIQUE
        CHECK (length(admission_sha256) = 64),
    FOREIGN KEY(task_id) REFERENCES tasks(task_id) ON DELETE RESTRICT,
    FOREIGN KEY(task_admission_id)
        REFERENCES task_admissions(task_admission_id) ON DELETE RESTRICT
) STRICT;
CREATE TABLE IF NOT EXISTS attempts (
    attempt_id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL,
    ordinal INTEGER NOT NULL CHECK (ordinal BETWEEN 1 AND 100),
    attempt_json BLOB NOT NULL,
    UNIQUE(task_id, ordinal),
    FOREIGN KEY(task_id) REFERENCES tasks(task_id) ON DELETE RESTRICT
) STRICT;
CREATE TABLE IF NOT EXISTS attempt_append_requests (
    task_id TEXT NOT NULL,
    idempotency_key TEXT NOT NULL,
    request_sha256 TEXT NOT NULL CHECK (length(request_sha256) = 64),
    request_json BLOB NOT NULL,
    attempt_id TEXT NOT NULL UNIQUE,
    PRIMARY KEY(task_id, idempotency_key),
    FOREIGN KEY(task_id) REFERENCES tasks(task_id) ON DELETE RESTRICT,
    FOREIGN KEY(attempt_id) REFERENCES attempts(attempt_id) ON DELETE RESTRICT
) STRICT;
CREATE TABLE IF NOT EXISTS attempt_executions (
    attempt_id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL,
    state TEXT NOT NULL,
    receipt_sha256 TEXT,
    session_result_sha256 TEXT,
    session_result_json BLOB,
    execution_json BLOB NOT NULL,
    resource_version INTEGER NOT NULL CHECK (resource_version >= 1),
    FOREIGN KEY(attempt_id) REFERENCES attempts(attempt_id) ON DELETE RESTRICT,
    FOREIGN KEY(task_id) REFERENCES tasks(task_id) ON DELETE RESTRICT,
    CHECK (
        (session_result_sha256 IS NULL AND session_result_json IS NULL)
        OR (length(session_result_sha256) = 64 AND session_result_json IS NOT NULL)
    )
) STRICT;
CREATE TABLE IF NOT EXISTS attempt_run_admissions (
    attempt_id TEXT NOT NULL,
    task_id TEXT NOT NULL,
    operation TEXT NOT NULL,
    rollout_task_id TEXT NOT NULL,
    session_id TEXT NOT NULL,
    generation_digest TEXT NOT NULL CHECK (length(generation_digest) = 64),
    registry_digest TEXT NOT NULL CHECK (length(registry_digest) = 64),
    framework_lock_digest TEXT NOT NULL CHECK (length(framework_lock_digest) = 64),
    payload_sha256 TEXT NOT NULL CHECK (length(payload_sha256) = 64),
    PRIMARY KEY(operation, rollout_task_id, session_id),
    FOREIGN KEY(attempt_id) REFERENCES attempts(attempt_id) ON DELETE RESTRICT,
    FOREIGN KEY(task_id) REFERENCES tasks(task_id) ON DELETE RESTRICT
) STRICT;
CREATE UNIQUE INDEX IF NOT EXISTS one_rollout_admission_per_attempt
ON attempt_run_admissions(attempt_id)
WHERE operation = 'rollout_task_submit';
CREATE TABLE IF NOT EXISTS successor_transitions (
    successor_transition_id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL,
    task_id TEXT NOT NULL UNIQUE,
    plan_sha256 TEXT NOT NULL CHECK (length(plan_sha256) = 64),
    plan_json BLOB NOT NULL,
    transition_json BLOB NOT NULL,
    resource_version INTEGER NOT NULL CHECK (resource_version >= 1),
    FOREIGN KEY(task_id) REFERENCES tasks(task_id) ON DELETE RESTRICT
) STRICT;
CREATE TABLE IF NOT EXISTS successor_transition_attempts (
    transition_attempt_id TEXT PRIMARY KEY,
    successor_transition_id TEXT NOT NULL,
    ordinal INTEGER NOT NULL CHECK (ordinal BETWEEN 1 AND 100),
    retry_request_id TEXT NOT NULL,
    attempt_json BLOB NOT NULL,
    UNIQUE(successor_transition_id, ordinal),
    UNIQUE(successor_transition_id, retry_request_id),
    FOREIGN KEY(successor_transition_id)
        REFERENCES successor_transitions(successor_transition_id) ON DELETE RESTRICT
) STRICT;
CREATE TABLE IF NOT EXISTS successor_commits (
    successor_transition_id TEXT PRIMARY KEY,
    successor_project_head_id TEXT,
    composition_semantics_version INTEGER
        CHECK (composition_semantics_version IN (1, 2)),
    legacy_manifest_sha256 TEXT
        CHECK (
            legacy_manifest_sha256 IS NULL
            OR length(legacy_manifest_sha256) = 64
        ),
    manifest_sha256 TEXT NOT NULL UNIQUE CHECK (length(manifest_sha256) = 64),
    commit_json BLOB NOT NULL,
    FOREIGN KEY(successor_transition_id)
        REFERENCES successor_transitions(successor_transition_id) ON DELETE RESTRICT
) STRICT;
CREATE TABLE IF NOT EXISTS successor_cleanup_receipts (
    successor_transition_id TEXT PRIMARY KEY,
    receipt_sha256 TEXT NOT NULL
        CHECK (length(receipt_sha256) = 64),
    receipt_json BLOB NOT NULL,
    FOREIGN KEY(successor_transition_id)
        REFERENCES successor_transitions(successor_transition_id)
        ON DELETE RESTRICT
) STRICT;
CREATE TABLE IF NOT EXISTS successor_abandon_receipts (
    successor_transition_id TEXT PRIMARY KEY,
    abandon_request_id TEXT NOT NULL UNIQUE
        CHECK (length(abandon_request_id) BETWEEN 1 AND 128),
    expected_project_head_id TEXT NOT NULL
        CHECK (length(expected_project_head_id) BETWEEN 1 AND 128),
    transition_attempt_id TEXT NOT NULL UNIQUE
        CHECK (length(transition_attempt_id) BETWEEN 1 AND 128),
    FOREIGN KEY(successor_transition_id)
        REFERENCES successor_transitions(successor_transition_id)
        ON DELETE RESTRICT,
    FOREIGN KEY(transition_attempt_id)
        REFERENCES successor_transition_attempts(transition_attempt_id)
        ON DELETE RESTRICT
) STRICT;
CREATE TABLE IF NOT EXISTS legacy_successor_abandon_exemptions (
    successor_transition_id TEXT PRIMARY KEY,
    expected_project_head_id TEXT NOT NULL
        CHECK (length(expected_project_head_id) BETWEEN 1 AND 128),
    transition_attempt_id TEXT NOT NULL UNIQUE
        CHECK (length(transition_attempt_id) BETWEEN 1 AND 128),
    FOREIGN KEY(successor_transition_id)
        REFERENCES successor_transitions(successor_transition_id)
        ON DELETE RESTRICT,
    FOREIGN KEY(transition_attempt_id)
        REFERENCES successor_transition_attempts(transition_attempt_id)
        ON DELETE RESTRICT
) STRICT;
CREATE TABLE IF NOT EXISTS events (
    sequence INTEGER PRIMARY KEY CHECK (sequence >= 1),
    event_id TEXT NOT NULL UNIQUE,
    event_type TEXT NOT NULL,
    project_id TEXT NOT NULL,
    task_id TEXT,
    event_json BLOB NOT NULL,
    FOREIGN KEY(task_id) REFERENCES tasks(task_id) ON DELETE RESTRICT
) STRICT;
CREATE INDEX IF NOT EXISTS events_task_sequence_idx ON events(task_id, sequence);
CREATE UNIQUE INDEX IF NOT EXISTS one_open_task_per_project
ON tasks(project_id)
WHERE closed = 0;
"""
_MAX_V2_TASKS = 10_000
_MAX_V2_PROJECTS = 10_000
_MAX_V2_PROJECT_HEADS = _MAX_V2_PROJECTS + _MAX_V2_TASKS
_MAX_V2_EVENT_REPLAY = 10_000
_MAX_V2_EVENT_ROWS = 100_000
_MAX_V2_EVENTS_PER_TASK = 10_000
_MAX_V2_RUN_ADMISSIONS = 100_000
_MAX_V2_RUN_ADMISSIONS_PER_ATTEMPT = 4_096
_MAX_V2_TASK_LOG_ENTRIES = 10_000
_MAX_V2_TASK_LOG_TRANSCRIPT_BYTES = 1024 * 1024
_MAX_V2_SUCCESSOR_ATTEMPTS_PER_TRANSITION = 100
_MAX_V2_SUCCESSOR_COMMIT_AGGREGATE_BYTES = _MAX_V2_TASKS * _MAX_DOCUMENT_BYTES
_V2_ACTION_RECEIPT_SCHEMA_TABLES = frozenset(
    {
        "action_receipt_migrations",
        "task_close_receipts",
        "legacy_task_close_exemptions",
        "successor_abandon_receipts",
        "legacy_successor_abandon_exemptions",
    }
)
_V2_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z", re.ASCII)


def _before_v2_successor_commit(*_args: object, **_kwargs: object) -> None:
    """Test-only fault boundary immediately before the atomic successor writes."""


class ScienceRunStoreError(RuntimeError):
    pass


class ScienceRunNotFound(ScienceRunStoreError):
    pass


class ScienceRunConflict(ScienceRunStoreError):
    pass


class ScienceRunIdempotencyConflict(ScienceRunConflict):
    pass


class ScienceProjectInFlight(ScienceRunConflict):
    pass


class ScienceRunPreconditionFailed(ScienceRunStoreError):
    pass


class ScienceTaskStoreV2Error(RuntimeError):
    pass


class ScienceTaskNotFoundV2(ScienceTaskStoreV2Error):
    pass


class ScienceAttemptNotFoundV2(ScienceTaskNotFoundV2):
    pass


class ScienceTaskConflictV2(ScienceTaskStoreV2Error):
    pass


class ScienceTaskIdempotencyConflictV2(ScienceTaskConflictV2):
    pass


class ScienceTaskProjectInFlightV2(ScienceTaskConflictV2):
    pass


class ScienceTaskNotReadyV2(ScienceTaskConflictV2):
    def __init__(self, blockers: tuple[ScienceProjectReadinessBlockerV2, ...]) -> None:
        super().__init__("project is not ready for task admission")
        self.blockers = blockers


class ScienceTaskStaleSubmissionV2(ScienceTaskConflictV2):
    pass


class ScienceTaskPreconditionFailedV2(ScienceTaskConflictV2):
    pass


class ScienceTaskETagChangedV2(ScienceTaskPreconditionFailedV2):
    pass


class ScienceTaskTerminalV2(ScienceTaskConflictV2):
    pass


class ScienceEventCursorExpiredV2(ScienceTaskStoreV2Error):
    pass


class ScienceProjectReadinessBlockerV2(StrEnum):
    PROJECT_CONFIG_REBIND = "project_config_rebind"
    SUCCESSOR_TRANSITION = "successor_transition"
    SETTINGS_TRANSITION = "settings_transition"
    CONTEXT_REBIND = "context_rebind"
    WORKSPACE_PUBLICATION = "workspace_publication"


_STORE_OWNED_PROJECT_READINESS_BLOCKERS = frozenset(
    {
        ScienceProjectReadinessBlockerV2.PROJECT_CONFIG_REBIND,
        ScienceProjectReadinessBlockerV2.SUCCESSOR_TRANSITION,
    }
)


@dataclass(frozen=True, slots=True)
class ScienceProjectAdmissionAuthorityV2:
    """Exact same-database authority checked before creating a v2 Task."""

    project_id: str
    active_project_head: m2.ProjectHeadRefV2
    project_config_sha256: str
    workspace_snapshot: m2.WorkspaceSnapshotRefV2
    normalized_evolution_intent_sha256: str
    blockers: tuple[ScienceProjectReadinessBlockerV2, ...] = ()

    def __post_init__(self) -> None:
        if _V2_ID_RE.fullmatch(self.project_id) is None:
            raise ValueError("v2 project admission authority ID is invalid")
        for value, label in (
            (self.project_config_sha256, "project config"),
            (self.normalized_evolution_intent_sha256, "evolution intent"),
        ):
            if re.fullmatch(r"[0-9a-f]{64}", value) is None:
                raise ValueError(f"v2 {label} digest is invalid")
        head = m2.ProjectHeadRefV2.model_validate(
            self.active_project_head.model_dump(mode="python")
        )
        workspace = m2.WorkspaceSnapshotRefV2.model_validate(
            self.workspace_snapshot.model_dump(mode="python")
        )
        if head.project_id != self.project_id or workspace.project_id != self.project_id:
            raise ValueError("v2 project admission authority crosses project identities")
        if workspace != head.workspace_snapshot:
            raise ValueError(
                "v2 project admission authority workspace differs from its active head"
            )
        if (
            not isinstance(self.blockers, tuple)
            or any(type(item) is not ScienceProjectReadinessBlockerV2 for item in self.blockers)
            or tuple(sorted(self.blockers, key=str)) != self.blockers
            or len(set(self.blockers)) != len(self.blockers)
        ):
            raise ValueError("v2 project readiness blockers must be sorted and unique")

    @property
    def project_etag(self) -> str:
        return _v2_authority_etag(self)


@dataclass(frozen=True, slots=True)
class _TaskCloseReceiptV2:
    task_id: str
    close_request_id: str
    expected_etag: str | None
    task_admission_id: str
    admission_sha256: str


@dataclass(frozen=True, slots=True)
class _SuccessorAbandonReceiptV2:
    successor_transition_id: str
    abandon_request_id: str
    expected_project_head_id: str
    transition_attempt_id: str


@dataclass(frozen=True, slots=True)
class _LegacyTaskCloseExemptionV2:
    task_id: str
    task_admission_id: str
    admission_sha256: str


@dataclass(frozen=True, slots=True)
class _LegacySuccessorAbandonExemptionV2:
    successor_transition_id: str
    expected_project_head_id: str
    transition_attempt_id: str


@dataclass(frozen=True, slots=True)
class ScienceRunCreateAdmission:
    project_id: str
    idempotency_key: str
    run_id: str
    request_digest: str
    request_json: bytes = field(repr=False)
    _owner: object = field(repr=False, compare=False)


@dataclass(frozen=True, slots=True)
class RolloutTaskAdmissionAuthority:
    task_id: str
    generation_digest: str
    registry_digest: str
    framework_lock_digest: str
    payload_sha256: str


@dataclass(frozen=True, slots=True)
class ProjectInFlightOwner:
    project_id: str
    run_id: str
    source: str


class ProjectInFlightCoordinator:
    """Process-local ordering around the durable science-run project authority."""

    def __init__(self, store: ScienceRunStore) -> None:
        self._store = store

    @contextmanager
    def locked(self) -> Iterator[None]:
        with self._store.coordination_lock():
            yield

    @contextmanager
    def guard_project_mutation(
        self,
        project_id: str,
        *,
        exact_replay: Callable[[], bool],
    ) -> Iterator[None]:
        with self._store.coordination_lock():
            if not exact_replay():
                owner = self._store.project_in_flight_owner(project_id)
                if owner is not None:
                    raise ScienceProjectInFlight(
                        "project has an admitted task or successor transition in flight"
                    )
            yield


class ScienceRunStore:
    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)
        self._lock = threading.RLock()
        self._create_condition = threading.Condition(self._lock)
        self._create_admissions: dict[tuple[str, str], ScienceRunCreateAdmission] = {}
        self._closed = False
        self._prepare_root()
        self.database = self.root / "science-runs.sqlite3"
        if self.database.exists() and self.database.is_symlink():
            raise ScienceRunStoreError("science run database must not be a symlink")
        with self._reader() as connection:
            connection.executescript(_SCHEMA)
            connection.execute(
                "INSERT OR IGNORE INTO metadata(singleton, schema_version) VALUES (1, 1)"
            )
            connection.commit()
        os.chmod(self.database, 0o600)
        self._verify_database()

    def close(self) -> None:
        with self._create_condition:
            self._closed = True
            self._create_condition.notify_all()

    def begin_create_run(
        self,
        *,
        request: m.RunCreateV1,
        idempotency_key: str,
    ) -> tuple[m.RunV1 | None, ScienceRunCreateAdmission | None]:
        request_json = _model_bytes(request)
        if len(request_json) > _MAX_DOCUMENT_BYTES:
            raise ScienceRunConflict("science run request exceeds its bound")
        request_digest = hashlib.sha256(request_json).hexdigest()
        identity = (request.project_id, idempotency_key)
        with self._create_condition:
            while True:
                with self._reader() as connection:
                    existing = _existing_create_run(
                        connection,
                        project_id=request.project_id,
                        idempotency_key=idempotency_key,
                        request_digest=request_digest,
                        request_json=request_json,
                    )
                if existing is not None:
                    return existing, None
                with self._reader() as connection:
                    durable_pending = _pending_create_run(
                        connection,
                        project_id=request.project_id,
                        idempotency_key=idempotency_key,
                        request_digest=request_digest,
                        request_json=request_json,
                    )
                pending = self._create_admissions.get(identity)
                if pending is None:
                    if durable_pending is None:
                        run_id = f"run-{secrets.token_hex(16)}"
                        with self._transaction() as connection:
                            existing = _existing_create_run(
                                connection,
                                project_id=request.project_id,
                                idempotency_key=idempotency_key,
                                request_digest=request_digest,
                                request_json=request_json,
                            )
                            if existing is not None:
                                return existing, None
                            durable_pending = _pending_create_run(
                                connection,
                                project_id=request.project_id,
                                idempotency_key=idempotency_key,
                                request_digest=request_digest,
                                request_json=request_json,
                            )
                            owner = _project_in_flight_owner(
                                connection,
                                request.project_id,
                            )
                            if owner is not None and (
                                durable_pending is None
                                or owner.run_id != durable_pending
                                or owner.source != "pending_create"
                            ):
                                raise ScienceProjectInFlight(
                                    "project has an admitted task or successor transition "
                                    "in flight"
                                )
                            if durable_pending is None:
                                count = int(
                                    connection.execute(
                                        "SELECT (SELECT COUNT(*) FROM runs) + "
                                        "(SELECT COUNT(*) FROM pending_run_creates)"
                                    ).fetchone()[0]
                                )
                                if count >= _MAX_RUNS:
                                    raise ScienceRunConflict("science run capacity is exhausted")
                                connection.execute(
                                    "INSERT INTO pending_run_creates("
                                    "project_id, idempotency_key, run_id, request_digest, "
                                    "request_json) VALUES (?, ?, ?, ?, ?)",
                                    (
                                        request.project_id,
                                        idempotency_key,
                                        run_id,
                                        request_digest,
                                        request_json,
                                    ),
                                )
                                durable_pending = run_id
                    admission = ScienceRunCreateAdmission(
                        project_id=request.project_id,
                        idempotency_key=idempotency_key,
                        run_id=durable_pending,
                        request_digest=request_digest,
                        request_json=request_json,
                        _owner=object(),
                    )
                    self._create_admissions[identity] = admission
                    return None, admission
                if (
                    pending.request_digest != request_digest
                    or pending.request_json != request_json
                ):
                    raise ScienceRunIdempotencyConflict("run idempotency key was reused")
                self._create_condition.wait()
                if self._closed:
                    raise ScienceRunStoreError("science run store is closed")

    def commit_create_run(
        self,
        admission: ScienceRunCreateAdmission,
        *,
        run: m.RunV1,
        input_context: Mapping[str, Sequence[str]],
    ) -> tuple[m.RunV1, bool]:
        context_json = _context_bytes(input_context)
        identity = (admission.project_id, admission.idempotency_key)
        with self._create_condition:
            if self._create_admissions.get(identity) is not admission:
                raise ScienceRunStoreError("science run create admission ownership is invalid")
            if run.id != admission.run_id:
                raise ScienceRunStoreError("science run create identity changed before commit")
            try:
                with self._transaction() as connection:
                    existing = _existing_create_run(
                        connection,
                        project_id=admission.project_id,
                        idempotency_key=admission.idempotency_key,
                        request_digest=admission.request_digest,
                        request_json=admission.request_json,
                    )
                    if existing is not None:
                        return existing, True
                    pending_run_id = _pending_create_run(
                        connection,
                        project_id=admission.project_id,
                        idempotency_key=admission.idempotency_key,
                        request_digest=admission.request_digest,
                        request_json=admission.request_json,
                    )
                    if pending_run_id != admission.run_id:
                        raise ScienceRunStoreError(
                            "science run create authority changed before commit"
                        )
                    count = int(connection.execute("SELECT COUNT(*) FROM runs").fetchone()[0])
                    if count >= _MAX_RUNS:
                        raise ScienceRunConflict("science run capacity is exhausted")
                    connection.execute(
                        "INSERT INTO runs(run_id, project_id, idempotency_key, "
                        "request_digest, request_json, run_json, input_context_json, "
                        "resource_version) VALUES (?, ?, ?, ?, ?, ?, ?, 1)",
                        (
                            run.id,
                            admission.project_id,
                            admission.idempotency_key,
                            admission.request_digest,
                            admission.request_json,
                            _model_bytes(run),
                            context_json,
                        ),
                    )
                    deleted = connection.execute(
                        "DELETE FROM pending_run_creates WHERE project_id = ? "
                        "AND idempotency_key = ? AND run_id = ?",
                        (
                            admission.project_id,
                            admission.idempotency_key,
                            admission.run_id,
                        ),
                    ).rowcount
                    if deleted != 1:
                        raise ScienceRunStoreError("science run create authority was not consumed")
                    return run, False
            finally:
                self._release_create_admission(admission)

    def abort_create_run(self, admission: ScienceRunCreateAdmission) -> None:
        with self._create_condition:
            identity = (admission.project_id, admission.idempotency_key)
            if self._create_admissions.get(identity) is admission:
                with self._transaction() as connection:
                    row = connection.execute(
                        "SELECT run_id, request_digest, request_json "
                        "FROM pending_run_creates WHERE project_id = ? "
                        "AND idempotency_key = ?",
                        identity,
                    ).fetchone()
                    if row is not None:
                        if (
                            row["run_id"] != admission.run_id
                            or row["request_digest"] != admission.request_digest
                            or bytes(row["request_json"]) != admission.request_json
                        ):
                            raise ScienceRunStoreError(
                                "science run create authority changed before abort"
                            )
                        connection.execute(
                            "DELETE FROM pending_run_creates WHERE project_id = ? "
                            "AND idempotency_key = ? AND run_id = ?",
                            (*identity, admission.run_id),
                        )
            self._release_create_admission(admission)

    def _release_create_admission(self, admission: ScienceRunCreateAdmission) -> None:
        identity = (admission.project_id, admission.idempotency_key)
        if self._create_admissions.get(identity) is admission:
            del self._create_admissions[identity]
            self._create_condition.notify_all()

    def create_run(
        self,
        *,
        request: m.RunCreateV1,
        idempotency_key: str,
        run: m.RunV1,
        input_context: Mapping[str, Sequence[str]],
    ) -> tuple[m.RunV1, bool]:
        existing, admission = self.begin_create_run(
            request=request,
            idempotency_key=idempotency_key,
        )
        if existing is not None:
            return existing, True
        assert admission is not None
        try:
            return self.commit_create_run(
                admission,
                run=run,
                input_context=input_context,
            )
        finally:
            self.abort_create_run(admission)

    def get_run(self, run_id: str, *, include_deleted: bool = False) -> m.RunV1:
        with self._lock, self._reader() as connection:
            row = connection.execute(
                "SELECT run_json, deleted FROM runs WHERE run_id = ?", (run_id,)
            ).fetchone()
        if row is None or (bool(row["deleted"]) and not include_deleted):
            raise ScienceRunNotFound("science run was not found")
        return _model(m.RunV1, row["run_json"])

    def request_for_run(self, run_id: str) -> m.RunCreateV1:
        with self._lock, self._reader() as connection:
            row = connection.execute(
                "SELECT request_json FROM runs WHERE run_id = ? AND deleted = 0",
                (run_id,),
            ).fetchone()
        if row is None:
            raise ScienceRunNotFound("science run was not found")
        return _model(m.RunCreateV1, row["request_json"])

    def input_context(self, run_id: str) -> dict[str, list[str]]:
        with self._lock, self._reader() as connection:
            row = connection.execute(
                "SELECT input_context_json FROM runs WHERE run_id = ? AND deleted = 0",
                (run_id,),
            ).fetchone()
        if row is None:
            raise ScienceRunNotFound("science run was not found")
        return _context_value(row["input_context_json"])

    def result_for_run(self, run_id: str) -> dict[str, object] | None:
        with self._lock, self._reader() as connection:
            row = connection.execute(
                "SELECT result_json FROM runs WHERE run_id = ? AND deleted = 0",
                (run_id,),
            ).fetchone()
        if row is None:
            raise ScienceRunNotFound("science run was not found")
        if row["result_json"] is None:
            return None
        return _object_value(row["result_json"], label="science run result")

    def store_result(self, run_id: str, result: Mapping[str, object]) -> None:
        payload = _json_bytes(result)
        with self._lock, self._transaction() as connection:
            row = connection.execute(
                "SELECT result_json, deleted FROM runs WHERE run_id = ?",
                (run_id,),
            ).fetchone()
            if row is None or bool(row["deleted"]):
                raise ScienceRunNotFound("science run was not found")
            existing = row["result_json"]
            if existing is not None and bytes(existing) != payload:
                raise ScienceRunConflict("science run result changed after persistence")
            if existing is None:
                connection.execute(
                    "UPDATE runs SET result_json = ? WHERE run_id = ?",
                    (payload, run_id),
                )

    def list_runs(self) -> list[m.RunV1]:
        with self._lock, self._reader() as connection:
            rows = connection.execute(
                "SELECT run_json FROM runs WHERE deleted = 0 ORDER BY run_id LIMIT ?",
                (_MAX_RUNS + 1,),
            ).fetchall()
        if len(rows) > _MAX_RUNS:
            raise ScienceRunStoreError("science run inventory exceeds its bound")
        return [_model(m.RunV1, row["run_json"]) for row in rows]

    def mutate_run(
        self,
        run_id: str,
        transform: Callable[[m.RunV1, int], m.RunV1],
        *,
        expected_etag: str | None = None,
        result: Mapping[str, object] | None = None,
    ) -> m.RunV1:
        with self._lock, self._transaction() as connection:
            row = connection.execute(
                "SELECT run_json, resource_version, deleted FROM runs WHERE run_id = ?",
                (run_id,),
            ).fetchone()
            if row is None or bool(row["deleted"]):
                raise ScienceRunNotFound("science run was not found")
            current = _model(m.RunV1, row["run_json"])
            if expected_etag is not None and current.etag != expected_etag:
                raise ScienceRunPreconditionFailed("science run ETag changed")
            version = int(row["resource_version"]) + 1
            updated = transform(current, version)
            result_json = None if result is None else _json_bytes(result)
            connection.execute(
                "UPDATE runs SET run_json = ?, resource_version = ?, "
                "result_json = COALESCE(?, result_json) WHERE run_id = ?",
                (_model_bytes(updated), version, result_json, run_id),
            )
            return updated

    def mutate_run_with_evidence(
        self,
        run_id: str,
        transform: Callable[[m.RunV1, int], m.RunV1],
        *,
        timeline_builders: Sequence[Callable[[m.RunV1, int], m.TimelineEntryV1]] = (),
        log_builders: Sequence[Callable[[m.RunV1, int], m.LogEntryV1]] = (),
    ) -> m.RunV1:
        with self._lock, self._transaction() as connection:
            row = connection.execute(
                "SELECT run_json, resource_version, deleted FROM runs WHERE run_id = ?",
                (run_id,),
            ).fetchone()
            if row is None or bool(row["deleted"]):
                raise ScienceRunNotFound("science run was not found")
            current = _model(m.RunV1, row["run_json"])
            version = int(row["resource_version"]) + 1
            updated = transform(current, version)
            timeline_row = connection.execute(
                "SELECT COUNT(*), COALESCE(MAX(sequence), -1) FROM timeline WHERE run_id = ?",
                (run_id,),
            ).fetchone()
            log_row = connection.execute(
                "SELECT COUNT(*), COALESCE(MAX(sequence), -1) FROM logs WHERE run_id = ?",
                (run_id,),
            ).fetchone()
            timeline_count = int(timeline_row[0])
            log_count = int(log_row[0])
            total_timeline = int(connection.execute("SELECT COUNT(*) FROM timeline").fetchone()[0])
            total_logs = int(connection.execute("SELECT COUNT(*) FROM logs").fetchone()[0])
            if (
                timeline_count + len(timeline_builders) > _MAX_TIMELINE_PER_RUN
                or total_timeline + len(timeline_builders) > _MAX_TIMELINE_ROWS
            ):
                raise ScienceRunConflict("science run timeline capacity is exhausted")
            if (
                log_count + len(log_builders) > _MAX_LOGS_PER_RUN
                or total_logs + len(log_builders) > _MAX_LOG_ROWS
            ):
                raise ScienceRunConflict("science run log capacity is exhausted")
            next_timeline = int(timeline_row[1]) + 1
            for offset, build in enumerate(timeline_builders):
                sequence = next_timeline + offset
                entry = build(updated, sequence)
                if entry.run_id != run_id or entry.sequence != sequence:
                    raise ScienceRunStoreError("science run timeline builder changed its identity")
                connection.execute(
                    "INSERT INTO timeline(run_id, sequence, entry_json) VALUES (?, ?, ?)",
                    (run_id, sequence, _model_bytes(entry)),
                )
            next_log = int(log_row[1]) + 1
            for offset, build in enumerate(log_builders):
                sequence = next_log + offset
                entry = build(updated, sequence)
                if entry.run_id != run_id or entry.sequence != sequence:
                    raise ScienceRunStoreError("science run log builder changed its identity")
                connection.execute(
                    "INSERT INTO logs(run_id, sequence, entry_json) VALUES (?, ?, ?)",
                    (run_id, sequence, _model_bytes(entry)),
                )
            connection.execute(
                "UPDATE runs SET run_json = ?, resource_version = ? WHERE run_id = ?",
                (_model_bytes(updated), version, run_id),
            )
            return updated

    def apply_mutation(
        self,
        operation_id: str,
        run_id: str,
        idempotency_key: str,
        request_digest: str,
        *,
        expected_etag: str,
        status_code: int,
        transform: Callable[[m.RunV1, int], m.RunV1 | None],
        deleted: bool = False,
        claim_project: bool = False,
        timeline_builders: Sequence[Callable[[m.RunV1, int], m.TimelineEntryV1]] = (),
    ) -> tuple[m.RunV1 | None, bool]:
        with self._lock, self._transaction() as connection:
            row = connection.execute(
                "SELECT request_digest, response_json, status_code FROM mutations "
                "WHERE operation_id = ? AND run_id = ? AND idempotency_key = ?",
                (operation_id, run_id, idempotency_key),
            ).fetchone()
            if row is not None:
                if row["request_digest"] != request_digest:
                    raise ScienceRunIdempotencyConflict("run mutation idempotency key was reused")
                if int(row["status_code"]) != status_code:
                    raise ScienceRunStoreError("persisted mutation status changed")
                response = (
                    None if row["response_json"] is None else _model(m.RunV1, row["response_json"])
                )
                return response, True
            run_row = connection.execute(
                "SELECT run_json, resource_version, deleted FROM runs WHERE run_id = ?",
                (run_id,),
            ).fetchone()
            if run_row is None or bool(run_row["deleted"]):
                raise ScienceRunNotFound("science run was not found")
            current = _model(m.RunV1, run_row["run_json"])
            if current.etag != expected_etag:
                raise ScienceRunPreconditionFailed("science run ETag changed")
            if claim_project:
                owner = _project_in_flight_owner(
                    connection,
                    current.project_id,
                    allowed_run_id=run_id,
                )
                if owner is not None:
                    raise ScienceProjectInFlight(
                        "project has another admitted task or successor transition in flight"
                    )
            version = int(run_row["resource_version"]) + 1
            response = transform(current, version)
            if timeline_builders:
                if response is None:
                    raise ScienceRunStoreError("run mutation timeline requires a response")
                timeline_row = connection.execute(
                    "SELECT COUNT(*), COALESCE(MAX(sequence), -1) FROM timeline WHERE run_id = ?",
                    (run_id,),
                ).fetchone()
                timeline_count = int(timeline_row[0])
                total_timeline = int(
                    connection.execute("SELECT COUNT(*) FROM timeline").fetchone()[0]
                )
                if (
                    timeline_count + len(timeline_builders) > _MAX_TIMELINE_PER_RUN
                    or total_timeline + len(timeline_builders) > _MAX_TIMELINE_ROWS
                ):
                    raise ScienceRunConflict("science run timeline capacity is exhausted")
                next_sequence = int(timeline_row[1]) + 1
                for offset, build in enumerate(timeline_builders):
                    sequence = next_sequence + offset
                    entry = build(response, sequence)
                    if entry.run_id != run_id or entry.sequence != sequence:
                        raise ScienceRunStoreError(
                            "science run timeline builder changed its identity"
                        )
                    connection.execute(
                        "INSERT INTO timeline(run_id, sequence, entry_json) VALUES (?, ?, ?)",
                        (run_id, sequence, _model_bytes(entry)),
                    )
            if response is not None:
                connection.execute(
                    "UPDATE runs SET run_json = ?, resource_version = ? WHERE run_id = ?",
                    (_model_bytes(response), version, run_id),
                )
            connection.execute(
                "INSERT INTO mutations(operation_id, run_id, idempotency_key, "
                "request_digest, response_json, status_code) VALUES (?, ?, ?, ?, ?, ?)",
                (
                    operation_id,
                    run_id,
                    idempotency_key,
                    request_digest,
                    None if response is None else _model_bytes(response),
                    status_code,
                ),
            )
            if deleted:
                connection.execute("UPDATE runs SET deleted = 1 WHERE run_id = ?", (run_id,))
            return response, False

    def append_timeline(
        self,
        run_id: str,
        build: Callable[[int], m.TimelineEntryV1],
    ) -> m.TimelineEntryV1:
        with self._lock, self._transaction() as connection:
            _require_live_run(connection, run_id)
            total = int(connection.execute("SELECT COUNT(*) FROM timeline").fetchone()[0])
            if total >= _MAX_TIMELINE_ROWS:
                raise ScienceRunConflict("science run timeline capacity is exhausted")
            row = connection.execute(
                "SELECT COUNT(*), COALESCE(MAX(sequence), -1) FROM timeline WHERE run_id = ?",
                (run_id,),
            ).fetchone()
            if int(row[0]) >= _MAX_TIMELINE_PER_RUN:
                raise ScienceRunConflict("science run timeline capacity is exhausted")
            sequence = int(row[1]) + 1
            entry = build(sequence)
            if entry.run_id != run_id or entry.sequence != sequence:
                raise ScienceRunStoreError("science run timeline builder changed its identity")
            connection.execute(
                "INSERT INTO timeline(run_id, sequence, entry_json) VALUES (?, ?, ?)",
                (entry.run_id, entry.sequence, _model_bytes(entry)),
            )
            return entry

    def timeline(self, run_id: str) -> list[m.TimelineEntryV1]:
        self.get_run(run_id)
        with self._lock, self._reader() as connection:
            rows = connection.execute(
                "SELECT entry_json FROM timeline WHERE run_id = ? ORDER BY sequence",
                (run_id,),
            ).fetchall()
        return [_model(m.TimelineEntryV1, row["entry_json"]) for row in rows]

    def append_log(
        self,
        run_id: str,
        build: Callable[[int], m.LogEntryV1],
    ) -> m.LogEntryV1:
        with self._lock, self._transaction() as connection:
            _require_live_run(connection, run_id)
            total = int(connection.execute("SELECT COUNT(*) FROM logs").fetchone()[0])
            if total >= _MAX_LOG_ROWS:
                raise ScienceRunConflict("science run log capacity is exhausted")
            row = connection.execute(
                "SELECT COUNT(*), COALESCE(MAX(sequence), -1) FROM logs WHERE run_id = ?",
                (run_id,),
            ).fetchone()
            if int(row[0]) >= _MAX_LOGS_PER_RUN:
                raise ScienceRunConflict("science run log capacity is exhausted")
            sequence = int(row[1]) + 1
            entry = build(sequence)
            if entry.run_id != run_id or entry.sequence != sequence:
                raise ScienceRunStoreError("science run log builder changed its identity")
            connection.execute(
                "INSERT INTO logs(run_id, sequence, entry_json) VALUES (?, ?, ?)",
                (entry.run_id, entry.sequence, _model_bytes(entry)),
            )
            return entry

    def logs(self, run_id: str) -> list[m.LogEntryV1]:
        self.get_run(run_id)
        with self._lock, self._reader() as connection:
            rows = connection.execute(
                "SELECT entry_json FROM logs WHERE run_id = ? ORDER BY sequence",
                (run_id,),
            ).fetchall()
        return [_model(m.LogEntryV1, row["entry_json"]) for row in rows]

    def store_artifacts(
        self,
        run_id: str,
        revision: m.RevisionRefV1,
        artifacts: Sequence[m.ArtifactSummaryV1],
    ) -> None:
        if len(artifacts) > _MAX_ARTIFACTS_PER_RUN:
            raise ScienceRunConflict("science run artifact capacity is exhausted")
        if len({artifact.id for artifact in artifacts}) != len(artifacts):
            raise ValueError("science run artifact IDs must be unique")
        with self._lock, self._transaction() as connection:
            run = _require_live_run(connection, run_id)
            if revision.project_id != run.project_id:
                raise ValueError("science run artifact revision belongs to another project")
            total = int(connection.execute("SELECT COUNT(*) FROM artifacts").fetchone()[0])
            existing_count = int(
                connection.execute(
                    "SELECT COUNT(*) FROM artifacts WHERE run_id = ?", (run_id,)
                ).fetchone()[0]
            )
            new_count = 0
            for artifact in artifacts:
                if artifact.run_id != run_id or artifact.project_id != run.project_id:
                    raise ValueError("science run artifact identity is invalid")
                payload = _model_bytes(artifact)
                existing = connection.execute(
                    "SELECT run_id, revision_id, artifact_json FROM artifacts "
                    "WHERE artifact_id = ?",
                    (artifact.id,),
                ).fetchone()
                if existing is not None:
                    if (
                        existing["run_id"] != run_id
                        or existing["revision_id"] != revision.id
                        or bytes(existing["artifact_json"]) != payload
                    ):
                        raise ScienceRunConflict("science run artifact identity was reused")
                    continue
                new_count += 1
                if existing_count + new_count > _MAX_ARTIFACTS_PER_RUN:
                    raise ScienceRunConflict("science run artifact capacity is exhausted")
                if total + new_count > _MAX_ARTIFACT_ROWS:
                    raise ScienceRunConflict("science run artifact capacity is exhausted")
                connection.execute(
                    "INSERT INTO artifacts(artifact_id, run_id, revision_id, artifact_json) "
                    "VALUES (?, ?, ?, ?)",
                    (artifact.id, run_id, revision.id, payload),
                )

    def artifacts_for_run(self, run_id: str) -> list[m.ArtifactSummaryV1]:
        self.get_run(run_id)
        with self._lock, self._reader() as connection:
            rows = connection.execute(
                "SELECT artifact_json FROM artifacts WHERE run_id = ? ORDER BY artifact_id",
                (run_id,),
            ).fetchall()
        return [_artifact_model(row["artifact_json"]) for row in rows]

    def artifacts_by_ids(self, artifact_ids: Sequence[str]) -> list[m.ArtifactSummaryV1]:
        if not artifact_ids:
            return []
        if len(artifact_ids) > 1024:
            raise ScienceRunStoreError("context artifact inventory exceeds its bound")
        unique_ids = list(dict.fromkeys(artifact_ids))
        placeholders = ",".join("?" for _ in unique_ids)
        with self._lock, self._reader() as connection:
            rows = connection.execute(
                f"SELECT artifact_id, artifact_json FROM artifacts "
                f"WHERE artifact_id IN ({placeholders})",
                unique_ids,
            ).fetchall()
        values = {row["artifact_id"]: _artifact_model(row["artifact_json"]) for row in rows}
        if set(values) != set(unique_ids):
            raise ScienceRunStoreError("revision context references an unknown artifact")
        return [values[item] for item in artifact_ids]

    def set_revision_context(
        self,
        project_id: str,
        revision_id: str,
        context: Mapping[str, Sequence[str]],
    ) -> None:
        payload = _context_bytes(context)
        with self._lock, self._transaction() as connection:
            existing = connection.execute(
                "SELECT project_id, context_json FROM revision_contexts WHERE revision_id = ?",
                (revision_id,),
            ).fetchone()
            if existing is not None:
                if (
                    existing["project_id"] != project_id
                    or bytes(existing["context_json"]) != payload
                ):
                    raise ScienceRunConflict("revision context identity was reused")
                return
            connection.execute(
                "INSERT INTO revision_contexts(revision_id, project_id, context_json) "
                "VALUES (?, ?, ?)",
                (revision_id, project_id, payload),
            )

    def revision_context(self, project_id: str, revision_id: str) -> dict[str, list[str]]:
        with self._lock, self._reader() as connection:
            row = connection.execute(
                "SELECT context_json FROM revision_contexts WHERE revision_id = ? "
                "AND project_id = ?",
                (revision_id, project_id),
            ).fetchone()
        return {} if row is None else _context_value(row["context_json"])

    def register_admission(
        self,
        *,
        run_id: str,
        operation: str,
        task_id: str,
        session_id: str | None,
        generation_digest: str,
        registry_digest: str,
        framework_lock_digest: str,
        payload_sha256: str,
        allow_create: bool,
    ) -> bool:
        for label, value in (
            ("run operation", operation),
            ("task ID", task_id),
            ("session ID", session_id or ""),
        ):
            if not isinstance(value, str) or len(value.encode("utf-8")) > 256:
                raise ValueError(f"science run admission {label} is invalid")
        for digest in (
            generation_digest,
            registry_digest,
            framework_lock_digest,
            payload_sha256,
        ):
            if len(digest) != 64 or any(
                character not in "0123456789abcdef" for character in digest
            ):
                raise ValueError("science run admission digest is invalid")
        normalized_session = session_id or ""
        with self._lock, self._transaction() as connection:
            run = _require_live_run(connection, run_id)
            if run.status not in _ACTIVE_STATUSES:
                return False
            row = connection.execute(
                "SELECT generation_digest, registry_digest, framework_lock_digest, "
                "payload_sha256 FROM admissions WHERE run_id = ? AND operation = ? "
                "AND task_id = ? AND session_id = ?",
                (run_id, operation, task_id, normalized_session),
            ).fetchone()
            if row is not None:
                return all(
                    row[key] == value
                    for key, value in (
                        ("generation_digest", generation_digest),
                        ("registry_digest", registry_digest),
                        ("framework_lock_digest", framework_lock_digest),
                        ("payload_sha256", payload_sha256),
                    )
                )
            if not allow_create:
                return False
            per_run = int(
                connection.execute(
                    "SELECT COUNT(*) FROM admissions WHERE run_id = ?", (run_id,)
                ).fetchone()[0]
            )
            total = int(connection.execute("SELECT COUNT(*) FROM admissions").fetchone()[0])
            if per_run >= _MAX_ADMISSIONS_PER_RUN or total >= _MAX_ADMISSION_ROWS:
                raise ScienceRunConflict("science run admission capacity is exhausted")
            connection.execute(
                "INSERT INTO admissions(run_id, operation, task_id, session_id, "
                "generation_digest, registry_digest, framework_lock_digest, payload_sha256) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    run_id,
                    operation,
                    task_id,
                    normalized_session,
                    generation_digest,
                    registry_digest,
                    framework_lock_digest,
                    payload_sha256,
                ),
            )
            return True

    def run_for_admitted_task(self, task_id: str) -> str | None:
        with self._lock, self._reader() as connection:
            rows = connection.execute(
                "SELECT run_id FROM admissions WHERE task_id = ? AND operation = ? LIMIT 2",
                (task_id, "rollout_task_submit"),
            ).fetchall()
        if len(rows) > 1:
            raise ScienceRunStoreError("rollout task admission identity is ambiguous")
        return None if not rows else str(rows[0]["run_id"])

    def rollout_task_admission(self, run_id: str) -> RolloutTaskAdmissionAuthority | None:
        self.get_run(run_id)
        with self._lock, self._reader() as connection:
            rows = connection.execute(
                "SELECT task_id, generation_digest, registry_digest, "
                "framework_lock_digest, payload_sha256 FROM admissions "
                "WHERE run_id = ? AND operation = ? LIMIT 2",
                (run_id, "rollout_task_submit"),
            ).fetchall()
        if len(rows) > 1:
            raise ScienceRunStoreError("rollout task admission identity is ambiguous")
        if not rows:
            return None
        row = rows[0]
        task_id = str(row["task_id"])
        if not task_id or len(task_id.encode("utf-8")) > 256:
            raise ScienceRunStoreError("rollout task admission identity is invalid")
        values = tuple(
            str(row[key])
            for key in (
                "generation_digest",
                "registry_digest",
                "framework_lock_digest",
                "payload_sha256",
            )
        )
        if any(
            len(value) != 64 or any(character not in "0123456789abcdef" for character in value)
            for value in values
        ):
            raise ScienceRunStoreError("rollout task admission digest is invalid")
        return RolloutTaskAdmissionAuthority(task_id, *values)

    def active_run_ids(self) -> list[str]:
        return [run.id for run in self.list_runs() if run.status in _ACTIVE_STATUSES]

    def queued_run_ids(self) -> list[str]:
        return [run.id for run in self.list_runs() if run.status is m.RunStatus.QUEUED]

    @contextmanager
    def coordination_lock(self) -> Iterator[None]:
        with self._lock:
            yield

    def project_in_flight_owner(self, project_id: str) -> ProjectInFlightOwner | None:
        with self._lock, self._reader() as connection:
            return _project_in_flight_owner(connection, project_id)

    def _prepare_root(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        metadata = self.root.stat(follow_symlinks=False)
        if (
            not stat.S_ISDIR(metadata.st_mode)
            or metadata.st_uid != os.geteuid()
            or stat.S_IMODE(metadata.st_mode) != 0o700
        ):
            raise ScienceRunStoreError("science run root must be a private owned directory")

    def _verify_database(self) -> None:
        metadata = self.database.stat(follow_symlinks=False)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid != os.geteuid()
            or metadata.st_nlink != 1
            or stat.S_IMODE(metadata.st_mode) != 0o600
        ):
            raise ScienceRunStoreError("science run database is not a private regular file")
        with self._reader() as connection:
            row = connection.execute(
                "SELECT schema_version FROM metadata WHERE singleton = 1"
            ).fetchone()
            if row is None or int(row["schema_version"]) != 1:
                raise ScienceRunStoreError("science run schema identity is invalid")
            pending = connection.execute(
                "SELECT project_id, idempotency_key, run_id, request_digest, request_json "
                "FROM pending_run_creates LIMIT ?",
                (_MAX_RUNS + 1,),
            ).fetchall()
            if len(pending) > _MAX_RUNS:
                raise ScienceRunStoreError("pending science run inventory exceeds its bound")
            for item in pending:
                request_json = bytes(item["request_json"])
                if len(request_json) > _MAX_DOCUMENT_BYTES:
                    raise ScienceRunStoreError("pending science run request exceeds its bound")
                request = _model(m.RunCreateV1, request_json)
                if (
                    request.project_id != item["project_id"]
                    or hashlib.sha256(request_json).hexdigest() != item["request_digest"]
                    or not isinstance(item["idempotency_key"], str)
                    or not item["idempotency_key"]
                    or _pending_create_run(
                        connection,
                        project_id=str(item["project_id"]),
                        idempotency_key=str(item["idempotency_key"]),
                        request_digest=str(item["request_digest"]),
                        request_json=request_json,
                    )
                    != item["run_id"]
                ):
                    raise ScienceRunStoreError("pending science run authority is invalid")
                overlap = connection.execute(
                    "SELECT 1 FROM runs WHERE run_id = ? OR "
                    "(project_id = ? AND idempotency_key = ?) LIMIT 1",
                    (item["run_id"], item["project_id"], item["idempotency_key"]),
                ).fetchone()
                if overlap is not None:
                    raise ScienceRunStoreError("pending science run authority overlaps a run")
            projects = connection.execute(
                "SELECT project_id FROM pending_run_creates "
                "UNION SELECT project_id FROM runs LIMIT ?",
                (_MAX_RUNS + 1,),
            ).fetchall()
            if len(projects) > _MAX_RUNS:
                raise ScienceRunStoreError("science run project inventory exceeds its bound")
            for project in projects:
                project_id = project["project_id"]
                if not isinstance(project_id, str) or not project_id:
                    raise ScienceRunStoreError("science run project identity is invalid")
                _project_in_flight_owner(connection, project_id)

    @contextmanager
    def _transaction(self) -> Iterator[sqlite3.Connection]:
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            yield connection
            connection.execute("COMMIT")
        except BaseException:
            try:
                connection.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            raise
        finally:
            connection.close()

    @contextmanager
    def _reader(self) -> Iterator[sqlite3.Connection]:
        connection = self._connect()
        try:
            yield connection
        finally:
            connection.close()

    def _connect(self) -> sqlite3.Connection:
        if self._closed:
            raise ScienceRunStoreError("science run store is closed")
        connection = sqlite3.connect(self.database, timeout=10.0)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 10000")
        return connection


class ScienceTaskStoreV2:
    """Separate durable owner for immutable v2 Task/admission/Attempt state."""

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)
        self._lock = threading.RLock()
        self._closed = False
        self._prepare_root()
        self.database = self.root / "science-tasks-v2.sqlite3"
        if self.database.exists() and self.database.is_symlink():
            raise ScienceTaskStoreV2Error("v2 science task database must not be a symlink")
        with self._reader() as connection:
            try:
                connection.execute("BEGIN IMMEDIATE")
                receipt_schema_preexisting = _v2_action_receipt_schema_preexisting(connection)
                _execute_v2_schema(connection)
                _migrate_v2_attempt_execution_results(connection)
                _backfill_v2_successor_transition_attempts(connection)
                connection.execute(
                    "INSERT OR IGNORE INTO metadata(singleton, schema_version) VALUES (1, 2)"
                )
                _backfill_v2_project_heads(connection)
                _migrate_v2_successor_commit_authority(connection)
                _migrate_v2_action_receipt_authority(
                    connection,
                    schema_preexisting=(receipt_schema_preexisting),
                )
                connection.commit()
            except BaseException:
                try:
                    connection.rollback()
                except sqlite3.Error:
                    pass
                raise
        os.chmod(self.database, 0o600)
        self._verify_database()

    def close(self) -> None:
        with self._lock:
            self._closed = True

    def begin_project_admission_authority_rebind(
        self,
        expected: ScienceProjectAdmissionAuthorityV2,
    ) -> ScienceProjectAdmissionAuthorityV2:
        """Durably block Task admission before a cross-store config rebind."""

        expected = _validate_v2_project_authority(expected)
        if expected.blockers:
            raise ScienceTaskProjectInFlightV2(
                "project has another immutable transition in flight"
            )
        blocked = replace(
            expected,
            blockers=(ScienceProjectReadinessBlockerV2.PROJECT_CONFIG_REBIND,),
        )
        with self._lock, self._transaction() as connection:
            current = _load_v2_project_authority(connection, expected.project_id)
            if current == blocked:
                return current
            if current != expected:
                if current.blockers:
                    raise ScienceTaskProjectInFlightV2(
                        "project has another immutable transition in flight"
                    )
                raise ScienceTaskPreconditionFailedV2(
                    "v2 project admission authority changed"
                )
            open_task = connection.execute(
                "SELECT 1 FROM tasks WHERE project_id = ? AND closed = 0 LIMIT 1",
                (expected.project_id,),
            ).fetchone()
            if open_task is not None:
                raise ScienceTaskProjectInFlightV2(
                    "project has an immutable v2 Task in flight"
                )
            updated = connection.execute(
                "UPDATE project_admission_authorities SET authority_json = ?, "
                "resource_version = resource_version + 1 WHERE project_id = ?",
                (_v2_authority_bytes(blocked), expected.project_id),
            )
            if updated.rowcount != 1:
                raise ScienceTaskStoreV2Error(
                    "v2 config rebind lost project admission authority"
                )
            stored = _load_v2_project_authority(connection, expected.project_id)
            if stored != blocked:
                raise ScienceTaskStoreV2Error(
                    "v2 config rebind blocker changed during commit"
                )
            return stored

    def finish_project_admission_authority_rebind(
        self,
        desired: ScienceProjectAdmissionAuthorityV2,
    ) -> ScienceProjectAdmissionAuthorityV2:
        """Stage desired next-Task intent while retaining its durable blocker."""

        desired = _validate_v2_project_authority(desired)
        if desired.blockers:
            raise ValueError("v2 desired config authority must be ready")
        staged = replace(
            desired,
            blockers=(ScienceProjectReadinessBlockerV2.PROJECT_CONFIG_REBIND,),
        )
        with self._lock, self._transaction() as connection:
            current = _load_v2_project_authority(connection, desired.project_id)
            if current == desired or current == staged:
                return current
            if current.blockers != (
                ScienceProjectReadinessBlockerV2.PROJECT_CONFIG_REBIND,
            ):
                raise ScienceTaskPreconditionFailedV2(
                    "v2 config rebind blocker changed"
                )
            if (
                current.project_id != desired.project_id
                or current.active_project_head != desired.active_project_head
                or current.workspace_snapshot != desired.workspace_snapshot
            ):
                raise ScienceTaskPreconditionFailedV2(
                    "v2 config rebind active authority changed"
                )
            open_task = connection.execute(
                "SELECT 1 FROM tasks WHERE project_id = ? AND closed = 0 LIMIT 1",
                (desired.project_id,),
            ).fetchone()
            if open_task is not None:
                raise ScienceTaskProjectInFlightV2(
                    "project has an immutable v2 Task in flight"
                )
            updated = connection.execute(
                "UPDATE project_admission_authorities SET authority_json = ?, "
                "resource_version = resource_version + 1 WHERE project_id = ?",
                (_v2_authority_bytes(staged), desired.project_id),
            )
            if updated.rowcount != 1:
                raise ScienceTaskStoreV2Error(
                    "v2 config rebind lost project admission authority"
                )
            stored = _load_v2_project_authority(connection, desired.project_id)
            if stored != staged:
                raise ScienceTaskStoreV2Error(
                    "v2 staged config authority changed during commit"
                )
            return stored

    def release_project_admission_authority_rebind(
        self,
        desired: ScienceProjectAdmissionAuthorityV2,
    ) -> ScienceProjectAdmissionAuthorityV2:
        """Release only the exact catalog-published next-Task authority."""

        desired = _validate_v2_project_authority(desired)
        if desired.blockers:
            raise ValueError("v2 desired config authority must be ready")
        staged = replace(
            desired,
            blockers=(ScienceProjectReadinessBlockerV2.PROJECT_CONFIG_REBIND,),
        )
        with self._lock, self._transaction() as connection:
            current = _load_v2_project_authority(connection, desired.project_id)
            if current == desired:
                return current
            if current != staged:
                raise ScienceTaskPreconditionFailedV2(
                    "v2 staged config authority changed"
                )
            open_task = connection.execute(
                "SELECT 1 FROM tasks WHERE project_id = ? AND closed = 0 LIMIT 1",
                (desired.project_id,),
            ).fetchone()
            if open_task is not None:
                raise ScienceTaskProjectInFlightV2(
                    "project has an immutable v2 Task in flight"
                )
            updated = connection.execute(
                "UPDATE project_admission_authorities SET authority_json = ?, "
                "resource_version = resource_version + 1 WHERE project_id = ?",
                (_v2_authority_bytes(desired), desired.project_id),
            )
            if updated.rowcount != 1:
                raise ScienceTaskStoreV2Error(
                    "v2 config rebind release lost project admission authority"
                )
            stored = _load_v2_project_authority(connection, desired.project_id)
            if stored != desired:
                raise ScienceTaskStoreV2Error(
                    "v2 config rebind release changed during commit"
                )
            return stored

    def abort_project_admission_authority_rebind(
        self,
        project_id: str,
    ) -> ScienceProjectAdmissionAuthorityV2:
        """Release an orphaned blocker when the catalog never prepared a rebind."""

        project_id = _v2_resource_id(project_id, label="project")
        with self._lock, self._transaction() as connection:
            current = _load_v2_project_authority(connection, project_id)
            blocker = ScienceProjectReadinessBlockerV2.PROJECT_CONFIG_REBIND
            if blocker not in current.blockers:
                return current
            if current.blockers != (blocker,):
                raise ScienceTaskPreconditionFailedV2(
                    "v2 config rebind blocker is not exclusive"
                )
            ready = replace(current, blockers=())
            updated = connection.execute(
                "UPDATE project_admission_authorities SET authority_json = ?, "
                "resource_version = resource_version + 1 WHERE project_id = ?",
                (_v2_authority_bytes(ready), project_id),
            )
            if updated.rowcount != 1:
                raise ScienceTaskStoreV2Error(
                    "v2 config rebind abort lost project admission authority"
                )
            stored = _load_v2_project_authority(connection, project_id)
            if stored != ready:
                raise ScienceTaskStoreV2Error(
                    "v2 config rebind abort changed during commit"
                )
            return stored

    def publish_project_admission_authority(
        self,
        authority: ScienceProjectAdmissionAuthorityV2,
        *,
        expected_project_head_id: str | None = None,
    ) -> ScienceProjectAdmissionAuthorityV2:
        authority = _validate_v2_project_authority(authority)
        payload = _v2_authority_bytes(authority)
        with self._lock, self._transaction() as connection:
            row = connection.execute(
                "SELECT authority_json, resource_version FROM project_admission_authorities "
                "WHERE project_id = ?",
                (authority.project_id,),
            ).fetchone()
            if row is None:
                if expected_project_head_id is not None:
                    raise ScienceTaskPreconditionFailedV2(
                        "v2 project admission authority does not exist"
                    )
                if _STORE_OWNED_PROJECT_READINESS_BLOCKERS.intersection(
                    authority.blockers
                ):
                    raise ScienceTaskPreconditionFailedV2(
                        "v2 project readiness authority is store-owned"
                    )
                count = int(
                    connection.execute(
                        "SELECT COUNT(*) FROM project_admission_authorities"
                    ).fetchone()[0]
                )
                if count >= _MAX_V2_PROJECTS:
                    raise ScienceTaskConflictV2(
                        "v2 project admission authority capacity is exhausted"
                    )
                connection.execute(
                    "INSERT INTO project_admission_authorities("
                    "project_id, authority_json, resource_version) VALUES (?, ?, 1)",
                    (authority.project_id, payload),
                )
                _store_v2_project_head(connection, authority.active_project_head)
                return authority
            current = _v2_authority_from_bytes(bytes(row["authority_json"]))
            if payload == bytes(row["authority_json"]):
                return current
            if (
                authority.active_project_head
                != current.active_project_head
            ):
                raise ScienceTaskPreconditionFailedV2(
                    "v2 active project head is store-owned"
                )
            if (
                authority.project_config_sha256
                != current.project_config_sha256
                or authority.normalized_evolution_intent_sha256
                != current.normalized_evolution_intent_sha256
                or authority.workspace_snapshot != current.workspace_snapshot
            ):
                raise ScienceTaskPreconditionFailedV2(
                    "v2 project admission identity is store-owned"
                )
            if (
                _STORE_OWNED_PROJECT_READINESS_BLOCKERS.intersection(
                    current.blockers
                )
                or _STORE_OWNED_PROJECT_READINESS_BLOCKERS.intersection(
                    authority.blockers
                )
            ):
                raise ScienceTaskPreconditionFailedV2(
                    "v2 project readiness authority is store-owned"
                )
            if (
                expected_project_head_id is None
                or current.active_project_head.project_head_id != expected_project_head_id
            ):
                raise ScienceTaskPreconditionFailedV2("v2 project admission authority changed")
            open_task = connection.execute(
                "SELECT 1 FROM tasks WHERE project_id = ? AND closed = 0 LIMIT 1",
                (authority.project_id,),
            ).fetchone()
            if open_task is not None:
                raise ScienceTaskProjectInFlightV2("project has an immutable v2 Task in flight")
            connection.execute(
                "UPDATE project_admission_authorities SET authority_json = ?, "
                "resource_version = resource_version + 1 WHERE project_id = ?",
                (payload, authority.project_id),
            )
            _store_v2_project_head(connection, authority.active_project_head)
            return authority

    def project_admission_authority(
        self,
        project_id: str,
    ) -> ScienceProjectAdmissionAuthorityV2:
        project_id = _v2_resource_id(project_id, label="project")
        with self._lock, self._reader() as connection:
            row = connection.execute(
                "SELECT authority_json FROM project_admission_authorities WHERE project_id = ?",
                (project_id,),
            ).fetchone()
            if row is None:
                raise ScienceTaskNotFoundV2("v2 project admission authority was not found")
            authority = _v2_authority_from_bytes(bytes(row["authority_json"]))
            active = _load_v2_project_head(
                connection,
                authority.active_project_head.project_head_id,
            )
            if active != authority.active_project_head:
                raise ScienceTaskStoreV2Error(
                    "v2 project admission authority does not bind its active head"
                )
            return authority

    def active_project_head(self, project_id: str) -> m2.ProjectHeadRefV2:
        return self.project_admission_authority(project_id).active_project_head

    def list_project_heads(self, project_id: str) -> list[m2.ProjectHeadRefV2]:
        project_id = _v2_resource_id(project_id, label="project")
        with self._lock, self._reader() as connection:
            rows = connection.execute(
                "SELECT project_head_id FROM project_heads WHERE project_id = ? "
                "ORDER BY generation LIMIT ?",
                (project_id, _MAX_V2_TASKS + 2),
            ).fetchall()
            if len(rows) > _MAX_V2_TASKS + 1:
                raise ScienceTaskStoreV2Error("v2 project-head history exceeds its bound")
            heads = [
                _load_v2_project_head(connection, str(row["project_head_id"])) for row in rows
            ]
            _validate_v2_project_head_chain(heads)
            return heads

    def get_project_head(self, project_head_id: str) -> m2.ProjectHeadRefV2:
        project_head_id = _v2_resource_id(project_head_id, label="project head")
        with self._lock, self._reader() as connection:
            head = _load_v2_project_head(connection, project_head_id)
            authority = _load_v2_project_authority(connection, head.project_id)
            heads = self._project_heads_in_connection(connection, head.project_id)
            if not heads or heads[-1] != authority.active_project_head:
                raise ScienceTaskStoreV2Error(
                    "v2 project-head history does not bind active authority"
                )
            return head

    def _project_heads_in_connection(
        self,
        connection: sqlite3.Connection,
        project_id: str,
    ) -> list[m2.ProjectHeadRefV2]:
        rows = connection.execute(
            "SELECT project_head_id FROM project_heads WHERE project_id = ? "
            "ORDER BY generation LIMIT ?",
            (project_id, _MAX_V2_TASKS + 2),
        ).fetchall()
        if len(rows) > _MAX_V2_TASKS + 1:
            raise ScienceTaskStoreV2Error("v2 project-head history exceeds its bound")
        heads = [_load_v2_project_head(connection, str(row["project_head_id"])) for row in rows]
        _validate_v2_project_head_chain(heads)
        return heads

    def start_successor_transition(
        self,
        *,
        task_id: str,
        accepted_attempt_id: str,
        plan: ScienceSuccessorPlanV2,
        now: datetime,
    ) -> m2.SuccessorTransitionV2:
        task_id = _v2_resource_id(task_id, label="task")
        accepted_attempt_id = _v2_resource_id(
            accepted_attempt_id,
            label="accepted Attempt",
        )
        plan = _validate_v2_model(ScienceSuccessorPlanV2, plan)
        plan_sha256 = science_successor_plan_sha256(plan)
        timestamp = _v2_timestamp(now)
        with self._lock, self._transaction() as connection:
            task = _load_v2_task_closure(connection, task_id)
            direct_transition = (
                task.state == "admitted"
                and task.authoritative_attempt_id is None
                and task.successor_transition is None
            )
            captured_execution = _load_v2_attempt_execution_optional(
                connection,
                accepted_attempt_id,
            )
            captured_transition = (
                task.state == "waiting_for_successor"
                and task.authoritative_attempt_id == accepted_attempt_id
                and task.successor_transition is None
                and captured_execution is not None
                and captured_execution.state == "captured"
                and captured_execution.successor_plan == plan
            )
            if not direct_transition and not captured_transition:
                raise ScienceTaskTerminalV2("v2 Task already has authoritative result ownership")
            attempts = {attempt.attempt_id: attempt for attempt in task.attempts}
            accepted_attempt = attempts.get(accepted_attempt_id)
            if accepted_attempt is None:
                raise ScienceTaskPreconditionFailedV2(
                    "accepted Attempt does not belong to the immutable v2 Task"
                )
            if (
                plan.project_id != task.project_id
                or plan.task_id != task.task_id
                or plan.task_admission_id != task.admission.task_admission_id
                or plan.admission_sha256 != task.admission.admission_sha256
                or plan.accepted_attempt_id != accepted_attempt.attempt_id
                or plan.predecessor_project_head_id
                != task.admission.predecessor_project_head.project_head_id
                or plan.normalized_evolution_intent_sha256
                != task.admission.normalized_evolution_intent_sha256
            ):
                raise ScienceTaskPreconditionFailedV2(
                    "successor plan does not bind the immutable v2 Task"
                )
            authority = _load_v2_project_authority(connection, task.project_id)
            if (
                authority.blockers
                or authority.active_project_head != task.admission.predecessor_project_head
            ):
                raise ScienceTaskNotReadyV2(
                    authority.blockers or (ScienceProjectReadinessBlockerV2.SUCCESSOR_TRANSITION,)
                )
            transition_id = f"successor-{secrets.token_hex(16)}"
            reference = m2.SuccessorTransitionRefV2(
                successor_transition_id=transition_id,
                project_id=task.project_id,
                kind="run_result",
                predecessor_project_head=task.admission.predecessor_project_head,
                expected_successor_generation=(
                    task.admission.predecessor_project_head.generation + 1
                ),
                plan_sha256=plan_sha256,
                task_admission=task.admission,
                accepted_attempt=accepted_attempt,
                successor_project_head=None,
            )
            transition = m2.SuccessorTransitionV2(
                transition=reference,
                state="pending",
                progress_completed=0,
                progress_total=6,
                error=None,
                created_at=timestamp,
                updated_at=timestamp,
            )
            updated_task = _replace_v2_task(
                task,
                authoritative_attempt_id=accepted_attempt.attempt_id,
                successor_transition=reference,
                state="waiting_for_successor",
                updated_at=timestamp,
            )
            blocked_authority = ScienceProjectAdmissionAuthorityV2(
                project_id=authority.project_id,
                active_project_head=authority.active_project_head,
                project_config_sha256=authority.project_config_sha256,
                workspace_snapshot=authority.workspace_snapshot,
                normalized_evolution_intent_sha256=(authority.normalized_evolution_intent_sha256),
                blockers=(ScienceProjectReadinessBlockerV2.SUCCESSOR_TRANSITION,),
            )
            connection.execute(
                "INSERT INTO successor_transitions("
                "successor_transition_id, project_id, task_id, plan_sha256, "
                "plan_json, transition_json, resource_version) "
                "VALUES (?, ?, ?, ?, ?, ?, 1)",
                (
                    transition_id,
                    task.project_id,
                    task.task_id,
                    plan_sha256,
                    _v2_model_bytes(plan),
                    _v2_model_bytes(transition),
                ),
            )
            initial_attempt = ScienceSuccessorTransitionAttemptV2(
                transition_attempt_id=f"successor-attempt-{secrets.token_hex(16)}",
                successor_transition_id=transition_id,
                ordinal=1,
                retry_request_id="initial",
                state="running",
                error=None,
                created_at=timestamp,
                updated_at=timestamp,
            )
            connection.execute(
                "INSERT INTO successor_transition_attempts("
                "transition_attempt_id, successor_transition_id, ordinal, "
                "retry_request_id, attempt_json) VALUES (?, ?, ?, ?, ?)",
                (
                    initial_attempt.transition_attempt_id,
                    transition_id,
                    initial_attempt.ordinal,
                    initial_attempt.retry_request_id,
                    _v2_model_bytes(initial_attempt),
                ),
            )
            connection.execute(
                "UPDATE tasks SET task_json = ?, resource_version = resource_version + 1 "
                "WHERE task_id = ?",
                (_v2_model_bytes(updated_task), task.task_id),
            )
            connection.execute(
                "UPDATE project_admission_authorities SET authority_json = ?, "
                "resource_version = resource_version + 1 WHERE project_id = ?",
                (_v2_authority_bytes(blocked_authority), task.project_id),
            )
            _append_v2_event(
                connection,
                model_type=m2.TransitionChangedEventV2,
                event_type="transition_changed",
                project_id=task.project_id,
                task_id=task.task_id,
                occurred_at=timestamp,
                payload={
                    "transition": reference,
                    "state": transition.state,
                    "progress_completed": transition.progress_completed,
                    "progress_total": transition.progress_total,
                },
            )
            return _load_v2_successor_transition(connection, transition_id)

    def advance_successor_transition(
        self,
        successor_transition_id: str,
        *,
        expected_transition_attempt_id: str,
        state: str,
        now: datetime,
    ) -> m2.SuccessorTransitionV2:
        successor_transition_id = _v2_resource_id(
            successor_transition_id,
            label="successor transition",
        )
        expected_transition_attempt_id = _v2_resource_id(
            expected_transition_attempt_id,
            label="successor transition attempt",
        )
        phases = (
            "pending",
            "sealing_dataset",
            "running_methods",
            "validating",
            "materializing",
            "committing",
        )
        if state not in phases[1:]:
            raise ValueError("v2 successor transition phase is invalid")
        with self._lock, self._transaction() as connection:
            transition = _load_v2_successor_transition(
                connection,
                successor_transition_id,
            )
            _require_v2_successor_transition_attempt(
                connection,
                successor_transition_id=successor_transition_id,
                expected_transition_attempt_id=(expected_transition_attempt_id),
                expected_state="running",
            )
            current_index = phases.index(transition.state)
            next_index = phases.index(state)
            if next_index != current_index + 1:
                raise ScienceTaskPreconditionFailedV2(
                    "v2 successor transition phase is not adjacent"
                )
            updated = _replace_v2_successor_transition(
                transition,
                state=state,
                progress_completed=next_index,
                updated_at=_v2_successor_timestamp(transition, now),
            )
            connection.execute(
                "UPDATE successor_transitions SET transition_json = ?, "
                "resource_version = resource_version + 1 "
                "WHERE successor_transition_id = ?",
                (_v2_model_bytes(updated), successor_transition_id),
            )
            admission = updated.transition.task_admission
            _append_v2_event(
                connection,
                model_type=m2.TransitionChangedEventV2,
                event_type="transition_changed",
                project_id=updated.transition.project_id,
                task_id=None if admission is None else admission.task_id,
                occurred_at=updated.updated_at,
                payload={
                    "transition": updated.transition,
                    "state": updated.state,
                    "progress_completed": updated.progress_completed,
                    "progress_total": updated.progress_total,
                },
            )
            return _load_v2_successor_transition(
                connection,
                successor_transition_id,
            )

    def record_dataset_sealed(
        self,
        successor_transition_id: str,
        *,
        expected_transition_attempt_id: str,
        dataset_id: str,
        dataset_sha256: str,
        now: datetime,
    ) -> m2.DatasetSealedEventV2:
        successor_transition_id = _v2_resource_id(
            successor_transition_id,
            label="successor transition",
        )
        expected_transition_attempt_id = _v2_resource_id(
            expected_transition_attempt_id,
            label="successor transition attempt",
        )
        dataset_id = _v2_resource_id(dataset_id, label="dataset")
        if (
            not isinstance(dataset_sha256, str)
            or re.fullmatch(r"[0-9a-f]{64}", dataset_sha256, flags=re.ASCII) is None
        ):
            raise ValueError("v2 dataset digest is invalid")
        with self._lock, self._transaction() as connection:
            transition = _load_v2_successor_transition(
                connection,
                successor_transition_id,
            )
            transition_attempt = _require_v2_successor_transition_attempt(
                connection,
                successor_transition_id=successor_transition_id,
                expected_transition_attempt_id=(expected_transition_attempt_id),
                expected_state="running",
            )
            admission = transition.transition.task_admission
            attempt = transition.transition.accepted_attempt
            if transition.state != "sealing_dataset" or admission is None or attempt is None:
                raise ScienceTaskPreconditionFailedV2(
                    "v2 dataset event does not bind the sealing transition"
                )
            prior = connection.execute(
                "SELECT event_json FROM events WHERE event_type = 'dataset_sealed' "
                "AND task_id = ?",
                (admission.task_id,),
            ).fetchone()
            if prior is not None:
                event = _load_v2_event_bytes(bytes(prior["event_json"]))
                if (
                    not isinstance(event, m2.DatasetSealedEventV2)
                    or event.dataset_id != dataset_id
                    or event.dataset_sha256 != dataset_sha256
                    or transition_attempt.dataset_id != dataset_id
                    or transition_attempt.dataset_sha256 != dataset_sha256
                ):
                    raise ScienceTaskConflictV2("v2 Task already binds another sealed dataset")
                return event
            event = _append_v2_event(
                connection,
                model_type=m2.DatasetSealedEventV2,
                event_type="dataset_sealed",
                project_id=transition.transition.project_id,
                task_id=admission.task_id,
                occurred_at=_v2_timestamp(now),
                payload={
                    "task_id": admission.task_id,
                    "task_admission_id": admission.task_admission_id,
                    "attempt_id": attempt.attempt_id,
                    "dataset_id": dataset_id,
                    "dataset_sha256": dataset_sha256,
                },
            )
            if not isinstance(event, m2.DatasetSealedEventV2):
                raise ScienceTaskStoreV2Error("v2 dataset event has the wrong type")
            sealed_attempt = _replace_v2_successor_transition_attempt(
                transition_attempt,
                dataset_id=dataset_id,
                dataset_sha256=dataset_sha256,
                updated_at=_v2_timestamp(now),
            )
            _store_v2_successor_transition_attempt(
                connection,
                sealed_attempt,
            )
            return event

    def fail_successor_transition(
        self,
        successor_transition_id: str,
        *,
        expected_transition_attempt_id: str,
        error: m2.ApiErrorV2,
        now: datetime,
    ) -> m2.SuccessorTransitionV2:
        successor_transition_id = _v2_resource_id(
            successor_transition_id,
            label="successor transition",
        )
        expected_transition_attempt_id = _v2_resource_id(
            expected_transition_attempt_id,
            label="successor transition attempt",
        )
        error = _validate_v2_model(m2.ApiErrorV2, error)
        with self._lock, self._transaction() as connection:
            transition = _load_v2_successor_transition(
                connection,
                successor_transition_id,
            )
            timestamp = _v2_successor_timestamp(transition, now)
            if transition.state == "failed":
                _require_v2_successor_transition_attempt(
                    connection,
                    successor_transition_id=successor_transition_id,
                    expected_transition_attempt_id=(expected_transition_attempt_id),
                    expected_state="failed",
                )
                return transition
            if transition.state in {"committed", "cancelled", "superseded"}:
                raise ScienceTaskTerminalV2("terminal v2 successor transition cannot be failed")
            task = _load_v2_task_closure(
                connection,
                transition.transition.task_admission.task_id,  # type: ignore[union-attr]
            )
            if task.successor_transition != transition.transition:
                raise ScienceTaskStoreV2Error("v2 Task and successor transition ownership differ")
            active_attempt = _require_v2_successor_transition_attempt(
                connection,
                successor_transition_id=successor_transition_id,
                expected_transition_attempt_id=(expected_transition_attempt_id),
                expected_state="running",
            )
            updated_transition = _replace_v2_successor_transition(
                transition,
                state="failed",
                error=error,
                updated_at=timestamp,
            )
            failed_attempt = _replace_v2_successor_transition_attempt(
                active_attempt,
                state="failed",
                error=error,
                updated_at=timestamp,
            )
            updated_task = _replace_v2_task(
                task,
                state="failed",
                updated_at=timestamp,
            )
            connection.execute(
                "UPDATE successor_transitions SET transition_json = ?, "
                "resource_version = resource_version + 1 "
                "WHERE successor_transition_id = ?",
                (_v2_model_bytes(updated_transition), successor_transition_id),
            )
            connection.execute(
                "UPDATE tasks SET task_json = ?, resource_version = resource_version + 1 "
                "WHERE task_id = ?",
                (_v2_model_bytes(updated_task), task.task_id),
            )
            _store_v2_successor_transition_attempt(
                connection,
                failed_attempt,
            )
            _append_v2_event(
                connection,
                model_type=m2.TransitionChangedEventV2,
                event_type="transition_changed",
                project_id=updated_transition.transition.project_id,
                task_id=task.task_id,
                occurred_at=updated_transition.updated_at,
                payload={
                    "transition": updated_transition.transition,
                    "state": updated_transition.state,
                    "progress_completed": updated_transition.progress_completed,
                    "progress_total": updated_transition.progress_total,
                },
            )
            return _load_v2_successor_transition(
                connection,
                successor_transition_id,
            )

    def retry_successor_transition(
        self,
        successor_transition_id: str,
        *,
        expected_project_head_id: str,
        retry_request_id: str,
        now: datetime,
        allow_in_progress_recovery: bool = False,
    ) -> m2.SuccessorTransitionV2:
        successor_transition_id = _v2_resource_id(
            successor_transition_id,
            label="successor transition",
        )
        expected_project_head_id = _v2_resource_id(
            expected_project_head_id,
            label="expected project head",
        )
        retry_request_id = _v2_resource_id(
            retry_request_id,
            label="successor retry request",
        )
        with self._lock, self._transaction() as connection:
            transition = _load_v2_successor_transition(
                connection,
                successor_transition_id,
            )
            reference = transition.transition
            admission = reference.task_admission
            if (
                admission is None
                or expected_project_head_id != reference.predecessor_project_head.project_head_id
            ):
                raise ScienceTaskPreconditionFailedV2(
                    "v2 successor retry no longer matches its predecessor"
                )
            task = _load_v2_task_closure(connection, admission.task_id)
            authority = _load_v2_project_authority(connection, reference.project_id)
            attempts = _load_v2_successor_transition_attempts(
                connection,
                successor_transition_id,
            )
            matching_attempts = [
                attempt for attempt in attempts if attempt.retry_request_id == retry_request_id
            ]
            if matching_attempts:
                if len(matching_attempts) != 1 or matching_attempts[0] != attempts[-1]:
                    raise ScienceTaskPreconditionFailedV2(
                        "v2 successor retry request is no longer current"
                    )
                if transition.state == "committed":
                    if reference.successor_project_head is None or task.state != "completed":
                        raise ScienceTaskPreconditionFailedV2(
                            "v2 successor retry completion authority changed"
                        )
                elif transition.state == "failed":
                    if (
                        task.state != "failed"
                        or attempts[-1].state != "failed"
                        or attempts[-1].error != transition.error
                    ):
                        raise ScienceTaskPreconditionFailedV2(
                            "v2 successor retry failure authority changed"
                        )
                    if (
                        allow_in_progress_recovery
                        and transition.error is not None
                        and transition.error.code == "successor_transition_interrupted"
                    ):
                        if (
                            transition.error.retryable is not True
                            or task.successor_transition != reference
                            or authority.active_project_head != reference.predecessor_project_head
                            or authority.blockers
                            != (ScienceProjectReadinessBlockerV2.SUCCESSOR_TRANSITION,)
                        ):
                            raise ScienceTaskPreconditionFailedV2(
                                "v2 interrupted successor retry authority changed"
                            )
                        dataset_event = _load_v2_dataset_event_for_task(
                            connection,
                            task.task_id,
                        )
                        expected_dataset_id = (
                            None if dataset_event is None else dataset_event.dataset_id
                        )
                        expected_dataset_sha256 = (
                            None if dataset_event is None else dataset_event.dataset_sha256
                        )
                        current_attempt = attempts[-1]
                        if (
                            current_attempt.dataset_id != expected_dataset_id
                            or current_attempt.dataset_sha256 != expected_dataset_sha256
                            or current_attempt.commit_manifest_sha256 is not None
                        ):
                            raise ScienceTaskStoreV2Error(
                                "v2 interrupted successor retry dataset authority changed"
                            )
                        resumed_state = "pending" if dataset_event is None else "running_methods"
                        resumed_progress = 0 if dataset_event is None else 2
                        timestamp = _v2_successor_timestamp(
                            transition,
                            now,
                        )
                        resumed_transition = _replace_v2_successor_transition(
                            transition,
                            state=resumed_state,
                            progress_completed=(resumed_progress),
                            error=None,
                            updated_at=timestamp,
                        )
                        resumed_task = _replace_v2_task(
                            task,
                            state="waiting_for_successor",
                            updated_at=timestamp,
                        )
                        resumed_attempt = _replace_v2_successor_transition_attempt(
                            current_attempt,
                            state="running",
                            error=None,
                            updated_at=timestamp,
                        )
                        connection.execute(
                            "UPDATE successor_transitions SET "
                            "transition_json = ?, resource_version = "
                            "resource_version + 1 WHERE "
                            "successor_transition_id = ?",
                            (
                                _v2_model_bytes(resumed_transition),
                                successor_transition_id,
                            ),
                        )
                        connection.execute(
                            "UPDATE tasks SET task_json = ?, "
                            "resource_version = resource_version + 1 "
                            "WHERE task_id = ?",
                            (
                                _v2_model_bytes(resumed_task),
                                task.task_id,
                            ),
                        )
                        _store_v2_successor_transition_attempt(
                            connection,
                            resumed_attempt,
                        )
                        _append_v2_event(
                            connection,
                            model_type=(m2.TransitionChangedEventV2),
                            event_type="transition_changed",
                            project_id=reference.project_id,
                            task_id=task.task_id,
                            occurred_at=timestamp,
                            payload={
                                "transition": reference,
                                "state": resumed_transition.state,
                                "progress_completed": (resumed_transition.progress_completed),
                                "progress_total": (resumed_transition.progress_total),
                            },
                        )
                        return _load_v2_successor_transition(
                            connection,
                            successor_transition_id,
                        )
                elif (
                    authority.active_project_head != reference.predecessor_project_head
                    or authority.blockers
                    != (ScienceProjectReadinessBlockerV2.SUCCESSOR_TRANSITION,)
                    or task.state != "waiting_for_successor"
                    or attempts[-1].state != "running"
                ):
                    raise ScienceTaskPreconditionFailedV2("v2 successor retry authority changed")
                return transition
            if transition.state != "failed":
                raise ScienceTaskTerminalV2("v2 successor transition is not failed")
            if (
                transition.error is None
                or transition.error.retryable is not True
                or not attempts
                or attempts[-1].state != "failed"
                or attempts[-1].error != transition.error
            ):
                raise ScienceTaskTerminalV2("v2 successor transition failure is not retryable")
            if (
                task.state != "failed"
                or task.successor_transition != reference
                or authority.active_project_head != reference.predecessor_project_head
                or authority.blockers != (ScienceProjectReadinessBlockerV2.SUCCESSOR_TRANSITION,)
            ):
                raise ScienceTaskPreconditionFailedV2(
                    "v2 failed successor retry authority changed"
                )
            dataset_event = _load_v2_dataset_event_for_task(
                connection,
                task.task_id,
            )
            resumed_state = "pending" if dataset_event is None else "running_methods"
            resumed_progress = 0 if dataset_event is None else 2
            timestamp = _v2_successor_timestamp(transition, now)
            if len(attempts) >= _MAX_V2_SUCCESSOR_ATTEMPTS_PER_TRANSITION:
                raise ScienceTaskConflictV2(
                    "v2 successor transition attempt capacity is exhausted"
                )
            retry_attempt = ScienceSuccessorTransitionAttemptV2(
                transition_attempt_id=(f"successor-attempt-{secrets.token_hex(16)}"),
                successor_transition_id=successor_transition_id,
                ordinal=len(attempts) + 1,
                retry_request_id=retry_request_id,
                state="running",
                error=None,
                dataset_id=(None if dataset_event is None else dataset_event.dataset_id),
                dataset_sha256=(None if dataset_event is None else dataset_event.dataset_sha256),
                commit_manifest_sha256=None,
                created_at=timestamp,
                updated_at=timestamp,
            )
            resumed_transition = _replace_v2_successor_transition(
                transition,
                state=resumed_state,
                progress_completed=resumed_progress,
                error=None,
                updated_at=timestamp,
            )
            resumed_task = _replace_v2_task(
                task,
                state="waiting_for_successor",
                updated_at=timestamp,
            )
            connection.execute(
                "UPDATE successor_transitions SET transition_json = ?, "
                "resource_version = resource_version + 1 "
                "WHERE successor_transition_id = ?",
                (_v2_model_bytes(resumed_transition), successor_transition_id),
            )
            connection.execute(
                "UPDATE tasks SET task_json = ?, resource_version = resource_version + 1 "
                "WHERE task_id = ?",
                (_v2_model_bytes(resumed_task), task.task_id),
            )
            connection.execute(
                "INSERT INTO successor_transition_attempts("
                "transition_attempt_id, successor_transition_id, ordinal, "
                "retry_request_id, attempt_json) VALUES (?, ?, ?, ?, ?)",
                (
                    retry_attempt.transition_attempt_id,
                    successor_transition_id,
                    retry_attempt.ordinal,
                    retry_attempt.retry_request_id,
                    _v2_model_bytes(retry_attempt),
                ),
            )
            _append_v2_event(
                connection,
                model_type=m2.TransitionChangedEventV2,
                event_type="transition_changed",
                project_id=reference.project_id,
                task_id=task.task_id,
                occurred_at=timestamp,
                payload={
                    "transition": reference,
                    "state": resumed_transition.state,
                    "progress_completed": resumed_transition.progress_completed,
                    "progress_total": resumed_transition.progress_total,
                },
            )
            return _load_v2_successor_transition(
                connection,
                successor_transition_id,
            )

    def abandon_successor_transition(
        self,
        successor_transition_id: str,
        *,
        expected_project_head_id: str,
        abandon_request_id: str,
        expected_transition_attempt_id: str | None,
        successor: m2.ProjectHeadRefV2 | None,
        commit: AtomicSuccessorCommitV2 | None,
        now: datetime,
        allow_cancelled_recovery: bool = False,
    ) -> m2.SuccessorTransitionV2:
        successor_transition_id = _v2_resource_id(
            successor_transition_id,
            label="successor transition",
        )
        expected_project_head_id = _v2_resource_id(
            expected_project_head_id,
            label="expected project head",
        )
        abandon_request_id = _v2_resource_id(
            abandon_request_id,
            label="successor abandon request",
        )
        if expected_transition_attempt_id is not None:
            expected_transition_attempt_id = _v2_resource_id(
                expected_transition_attempt_id,
                label="successor transition attempt",
            )
        if (successor is None) != (commit is None):
            raise ValueError("v2 evolution abandon successor receipt is incomplete")
        if successor is not None:
            successor = _validate_v2_model(m2.ProjectHeadRefV2, successor)
        if commit is not None:
            commit = _validate_v2_model(AtomicSuccessorCommitV2, commit)
        with self._lock, self._transaction() as connection:
            transition = _load_v2_successor_transition(
                connection,
                successor_transition_id,
            )
            reference = transition.transition
            admission = reference.task_admission
            if (
                admission is None
                or expected_project_head_id != reference.predecessor_project_head.project_head_id
            ):
                raise ScienceTaskPreconditionFailedV2(
                    "v2 successor abandonment no longer matches its predecessor"
                )
            task = _load_v2_task_closure(connection, admission.task_id)
            authority = _load_v2_project_authority(connection, reference.project_id)
            abandon_receipt = _load_v2_successor_abandon_receipt(
                connection,
                successor_transition_id,
            )
            legacy_abandon = _load_v2_legacy_successor_abandon_exemption(
                connection,
                successor_transition_id,
            )
            if abandon_receipt is not None and legacy_abandon is not None:
                raise ScienceTaskStoreV2Error("v2 successor exposes conflicting abandon authority")
            if legacy_abandon is not None:
                legacy_attempts = _load_v2_successor_transition_attempts(
                    connection,
                    successor_transition_id,
                )
                if (
                    transition.state != "cancelled"
                    or legacy_abandon.expected_project_head_id
                    != reference.predecessor_project_head.project_head_id
                    or legacy_attempts[-1].transition_attempt_id
                    != legacy_abandon.transition_attempt_id
                    or legacy_attempts[-1].state != "failed"
                ):
                    raise ScienceTaskStoreV2Error(
                        "legacy v2 successor abandon authority is inconsistent"
                    )
            if transition.state == "cancelled" and allow_cancelled_recovery:
                if (
                    abandon_receipt is None
                    or abandon_receipt.abandon_request_id != abandon_request_id
                    or abandon_receipt.expected_project_head_id != expected_project_head_id
                ):
                    raise ScienceTaskTerminalV2(
                        "cancelled v2 successor belongs to another abandon action"
                    )
                _require_v2_successor_transition_attempt(
                    connection,
                    successor_transition_id=(successor_transition_id),
                    expected_transition_attempt_id=(abandon_receipt.transition_attempt_id),
                    expected_state="failed",
                )
                cleanup_receipt = _load_v2_successor_cleanup_receipt(
                    connection,
                    successor_transition_id,
                )
                expected_blockers = (
                    (ScienceProjectReadinessBlockerV2.SUCCESSOR_TRANSITION,)
                    if cleanup_receipt is None
                    else ()
                )
                if (
                    task.state != "completed"
                    or reference.successor_project_head is None
                    or authority.active_project_head != reference.successor_project_head
                    or authority.blockers != expected_blockers
                ):
                    raise ScienceTaskPreconditionFailedV2(
                        "v2 successor abandonment authority changed"
                    )
                existing = _load_v2_successor_commit(
                    connection,
                    successor_transition_id,
                )
                if type(existing.manifest) is not AtomicEvolutionAbandonManifestV2:
                    raise ScienceTaskStoreV2Error(
                        "cancelled v2 successor has the wrong publication receipt"
                    )
                return transition
            if abandon_receipt is not None:
                raise ScienceTaskStoreV2Error(
                    "non-cancelled v2 successor exposes an abandon receipt"
                )
            if expected_transition_attempt_id is None:
                raise ScienceTaskPreconditionFailedV2(
                    "v2 evolution abandon lacks its failed attempt fence"
                )
            failed_attempt = _require_v2_successor_transition_attempt(
                connection,
                successor_transition_id=successor_transition_id,
                expected_transition_attempt_id=(expected_transition_attempt_id),
                expected_state="failed",
            )
            if (
                transition.state != "failed"
                or task.state != "failed"
                or task.successor_transition != reference
                or authority.active_project_head != reference.predecessor_project_head
                or authority.blockers != (ScienceProjectReadinessBlockerV2.SUCCESSOR_TRANSITION,)
            ):
                raise ScienceTaskTerminalV2("v2 successor transition cannot be abandoned")
            if successor is None or commit is None:
                raise ScienceTaskPreconditionFailedV2(
                    "v2 evolution abandon lacks its accepted workspace publication"
                )
            if type(commit.manifest) is not AtomicEvolutionAbandonManifestV2:
                raise ScienceTaskPreconditionFailedV2(
                    "v2 evolution abandon has the wrong atomic receipt"
                )
            timestamp = _v2_successor_timestamp(transition, now)
            abandoned_reference = m2.SuccessorTransitionRefV2.model_validate(
                {
                    **reference.model_dump(mode="python"),
                    "successor_project_head": successor,
                }
            )
            cancelled_transition = _replace_v2_successor_transition(
                transition,
                transition=abandoned_reference,
                state="cancelled",
                error=None,
                updated_at=timestamp,
            )
            completed_task = _replace_v2_task(
                task,
                successor_transition=abandoned_reference,
                state="completed",
                updated_at=timestamp,
            )
            next_authority = ScienceProjectAdmissionAuthorityV2(
                project_id=authority.project_id,
                active_project_head=successor,
                project_config_sha256=authority.project_config_sha256,
                workspace_snapshot=successor.workspace_snapshot,
                normalized_evolution_intent_sha256=(authority.normalized_evolution_intent_sha256),
                blockers=(ScienceProjectReadinessBlockerV2.SUCCESSOR_TRANSITION,),
            )
            _validate_v2_successor_commit_closure(
                connection=connection,
                task=task,
                transition=transition,
                successor=successor,
                commit=commit,
                publication_timestamp=timestamp,
                require_task_artifacts=True,
            )
            if (
                int(connection.execute("SELECT COUNT(*) FROM project_heads").fetchone()[0])
                >= _MAX_V2_PROJECT_HEADS
            ):
                raise ScienceTaskConflictV2("v2 project-head capacity is exhausted")

            _before_v2_successor_commit(
                successor_transition_id,
                successor,
                commit,
            )
            _store_v2_project_head(connection, successor)
            connection.execute(
                "INSERT INTO successor_commits("
                "successor_transition_id, successor_project_head_id, "
                "composition_semantics_version, legacy_manifest_sha256, "
                "manifest_sha256, commit_json) "
                "VALUES (?, ?, 2, NULL, ?, ?)",
                (
                    successor_transition_id,
                    successor.project_head_id,
                    commit.manifest_sha256,
                    _v2_model_bytes(commit),
                ),
            )
            connection.execute(
                "UPDATE successor_transitions SET transition_json = ?, "
                "resource_version = resource_version + 1 "
                "WHERE successor_transition_id = ?",
                (_v2_model_bytes(cancelled_transition), successor_transition_id),
            )
            connection.execute(
                "UPDATE tasks SET task_json = ?, closed = 1, "
                "resource_version = resource_version + 1 WHERE task_id = ?",
                (_v2_model_bytes(completed_task), task.task_id),
            )
            connection.execute(
                "UPDATE project_admission_authorities SET authority_json = ?, "
                "resource_version = resource_version + 1 WHERE project_id = ?",
                (_v2_authority_bytes(next_authority), authority.project_id),
            )
            connection.execute(
                "INSERT INTO successor_abandon_receipts("
                "successor_transition_id, abandon_request_id, "
                "expected_project_head_id, transition_attempt_id"
                ") VALUES (?, ?, ?, ?)",
                (
                    successor_transition_id,
                    abandon_request_id,
                    expected_project_head_id,
                    failed_attempt.transition_attempt_id,
                ),
            )
            _append_v2_event(
                connection,
                model_type=m2.ProjectHeadActivatedEventV2,
                event_type="project_head_activated",
                project_id=successor.project_id,
                task_id=task.task_id,
                occurred_at=timestamp,
                payload={
                    "successor_transition_id": successor_transition_id,
                    "project_head": successor,
                },
            )
            _append_v2_event(
                connection,
                model_type=m2.TransitionChangedEventV2,
                event_type="transition_changed",
                project_id=reference.project_id,
                task_id=task.task_id,
                occurred_at=timestamp,
                payload={
                    "transition": abandoned_reference,
                    "state": cancelled_transition.state,
                    "progress_completed": cancelled_transition.progress_completed,
                    "progress_total": cancelled_transition.progress_total,
                },
            )
            return _load_v2_successor_transition(
                connection,
                successor_transition_id,
            )

    def successor_abandon_evidence(
        self,
        successor_transition_id: str,
    ) -> tuple[m2.SuccessorTransitionV2, ScienceSuccessorPlanV2]:
        successor_transition_id = _v2_resource_id(
            successor_transition_id,
            label="successor transition",
        )
        with self._lock, self._reader() as connection:
            transition = _load_v2_successor_transition(
                connection,
                successor_transition_id,
            )
            if transition.state != "failed":
                raise ScienceTaskTerminalV2("v2 successor transition is not ready to abandon")
            row = connection.execute(
                "SELECT plan_sha256, plan_json FROM successor_transitions "
                "WHERE successor_transition_id = ?",
                (successor_transition_id,),
            ).fetchone()
            if row is None:
                raise ScienceTaskNotFoundV2("v2 successor transition was not found")
            plan = _v2_model_from_bytes(
                ScienceSuccessorPlanV2,
                bytes(row["plan_json"]),
            )
            if science_successor_plan_sha256(plan) != row["plan_sha256"]:
                raise ScienceTaskStoreV2Error("persisted v2 successor plan digest is inconsistent")
            return transition, plan

    def successor_cleanup_evidence(
        self,
        successor_transition_id: str,
    ) -> tuple[m2.SuccessorTransitionV2, ScienceSuccessorPlanV2]:
        successor_transition_id = _v2_resource_id(
            successor_transition_id,
            label="successor transition",
        )
        with self._lock, self._reader() as connection:
            transition = _load_v2_successor_transition(
                connection,
                successor_transition_id,
            )
            if transition.state != "cancelled":
                raise ScienceTaskPreconditionFailedV2(
                    "v2 successor transition has no abandoned cleanup authority"
                )
            row = connection.execute(
                "SELECT plan_sha256 FROM successor_transitions WHERE successor_transition_id = ?",
                (successor_transition_id,),
            ).fetchone()
            plan_payload = _load_guarded_v2_blob(
                connection,
                table="successor_transitions",
                key_column="successor_transition_id",
                key=successor_transition_id,
                blob_column="plan_json",
                label="cancelled v2 successor plan",
            )
            if row is None or plan_payload is None:
                raise ScienceTaskNotFoundV2("v2 successor transition was not found")
            plan = _v2_model_from_bytes(
                ScienceSuccessorPlanV2,
                plan_payload,
            )
            if science_successor_plan_sha256(plan) != row["plan_sha256"]:
                raise ScienceTaskStoreV2Error(
                    "persisted cancelled v2 successor plan digest is inconsistent"
                )
            return transition, plan

    def successor_cleanup_receipt(
        self,
        successor_transition_id: str,
    ) -> ScienceSuccessorCleanupReceiptV2 | None:
        successor_transition_id = _v2_resource_id(
            successor_transition_id,
            label="successor transition",
        )
        with self._lock, self._reader() as connection:
            return _load_v2_successor_cleanup_receipt(
                connection,
                successor_transition_id,
            )

    def pending_successor_cleanup_ids(self) -> list[str]:
        with self._lock, self._reader() as connection:
            rows = connection.execute(
                "SELECT successor_transition_id FROM "
                "successor_transitions ORDER BY successor_transition_id "
                "LIMIT ?",
                (_MAX_V2_TASKS + 1,),
            ).fetchall()
            if len(rows) > _MAX_V2_TASKS:
                raise ScienceTaskStoreV2Error(
                    "v2 successor transition inventory exceeds its bound"
                )
            pending: list[str] = []
            for row in rows:
                transition_id = str(row["successor_transition_id"])
                transition = _load_v2_successor_transition(
                    connection,
                    transition_id,
                )
                receipt = _load_v2_successor_cleanup_receipt(
                    connection,
                    transition_id,
                )
                if transition.state == "cancelled":
                    if receipt is None:
                        pending.append(transition_id)
                elif receipt is not None:
                    raise ScienceTaskStoreV2Error(
                        "non-cancelled v2 successor exposes a cleanup receipt"
                    )
            return pending

    def record_successor_cleanup(
        self,
        receipt: ScienceSuccessorCleanupReceiptV2,
    ) -> ScienceSuccessorCleanupReceiptV2:
        receipt = _validate_v2_model(
            ScienceSuccessorCleanupReceiptV2,
            receipt,
        )
        payload = _v2_model_bytes(receipt)
        payload_sha256 = hashlib.sha256(payload).hexdigest()
        with self._lock, self._transaction() as connection:
            transition = _load_v2_successor_transition(
                connection,
                receipt.successor_transition_id,
            )
            if transition.state != "cancelled":
                raise ScienceTaskPreconditionFailedV2(
                    "v2 successor cleanup requires a cancelled transition"
                )
            existing = _load_v2_successor_cleanup_receipt(
                connection,
                receipt.successor_transition_id,
            )
            if existing is not None:
                if existing != receipt:
                    raise ScienceTaskPreconditionFailedV2(
                        "v2 successor cleanup receipt is immutable"
                    )
                return existing
            successor = transition.transition.successor_project_head
            if successor is None:
                raise ScienceTaskStoreV2Error("cancelled v2 successor has no published head")
            authority = _load_v2_project_authority(
                connection,
                transition.transition.project_id,
            )
            if authority.active_project_head != successor or authority.blockers != (
                ScienceProjectReadinessBlockerV2.SUCCESSOR_TRANSITION,
            ):
                raise ScienceTaskPreconditionFailedV2(
                    "v2 successor cleanup no longer owns readiness"
                )
            connection.execute(
                "INSERT INTO successor_cleanup_receipts("
                "successor_transition_id, receipt_sha256, receipt_json"
                ") VALUES (?, ?, ?)",
                (
                    receipt.successor_transition_id,
                    payload_sha256,
                    payload,
                ),
            )
            ready_authority = ScienceProjectAdmissionAuthorityV2(
                project_id=authority.project_id,
                active_project_head=authority.active_project_head,
                project_config_sha256=(authority.project_config_sha256),
                workspace_snapshot=(authority.workspace_snapshot),
                normalized_evolution_intent_sha256=(authority.normalized_evolution_intent_sha256),
                blockers=(),
            )
            updated = connection.execute(
                "UPDATE project_admission_authorities "
                "SET authority_json = ?, "
                "resource_version = resource_version + 1 "
                "WHERE project_id = ?",
                (
                    _v2_authority_bytes(ready_authority),
                    authority.project_id,
                ),
            )
            if updated.rowcount != 1:
                raise ScienceTaskStoreV2Error(
                    "v2 successor cleanup lost project readiness authority"
                )
            stored = _load_v2_successor_cleanup_receipt(
                connection,
                receipt.successor_transition_id,
            )
            if stored != receipt:
                raise ScienceTaskStoreV2Error("v2 successor cleanup receipt changed during commit")
            if (
                _load_v2_project_authority(
                    connection,
                    authority.project_id,
                )
                != ready_authority
            ):
                raise ScienceTaskStoreV2Error(
                    "v2 successor cleanup readiness changed during commit"
                )
            return stored

    def commit_successor_transition(
        self,
        successor_transition_id: str,
        *,
        expected_transition_attempt_id: str,
        successor: m2.ProjectHeadRefV2,
        commit: AtomicSuccessorCommitV2,
        now: datetime,
    ) -> m2.SuccessorTransitionV2:
        successor_transition_id = _v2_resource_id(
            successor_transition_id,
            label="successor transition",
        )
        expected_transition_attempt_id = _v2_resource_id(
            expected_transition_attempt_id,
            label="successor transition attempt",
        )
        successor = _validate_v2_model(m2.ProjectHeadRefV2, successor)
        commit = _validate_v2_model(AtomicSuccessorCommitV2, commit)
        with self._lock, self._transaction() as connection:
            transition = _load_v2_successor_transition(
                connection,
                successor_transition_id,
            )
            timestamp = _v2_successor_timestamp(transition, now)
            if transition.state != "committing":
                raise ScienceTaskPreconditionFailedV2(
                    "v2 successor transition is not ready to commit"
                )
            active_attempt = _require_v2_successor_transition_attempt(
                connection,
                successor_transition_id=successor_transition_id,
                expected_transition_attempt_id=(expected_transition_attempt_id),
                expected_state="running",
            )
            reference = transition.transition
            if reference.task_admission is None or reference.accepted_attempt is None:
                raise ScienceTaskStoreV2Error("v2 run-result transition lost its Task ownership")
            task = _load_v2_task_closure(
                connection,
                reference.task_admission.task_id,
            )
            authority = _load_v2_project_authority(connection, task.project_id)
            if (
                task.state != "waiting_for_successor"
                or task.authoritative_attempt_id != reference.accepted_attempt.attempt_id
                or task.successor_transition != reference
                or authority.active_project_head != reference.predecessor_project_head
                or authority.blockers != (ScienceProjectReadinessBlockerV2.SUCCESSOR_TRANSITION,)
            ):
                raise ScienceTaskPreconditionFailedV2("v2 successor commit authority changed")
            _validate_v2_successor_commit_closure(
                connection=connection,
                task=task,
                transition=transition,
                successor=successor,
                commit=commit,
                publication_timestamp=timestamp,
                require_task_artifacts=True,
            )
            if (
                int(connection.execute("SELECT COUNT(*) FROM project_heads").fetchone()[0])
                >= _MAX_V2_PROJECT_HEADS
            ):
                raise ScienceTaskConflictV2("v2 project-head capacity is exhausted")

            _before_v2_successor_commit(
                successor_transition_id,
                successor,
                commit,
            )
            _store_v2_project_head(connection, successor)
            connection.execute(
                "INSERT INTO successor_commits("
                "successor_transition_id, successor_project_head_id, "
                "composition_semantics_version, legacy_manifest_sha256, "
                "manifest_sha256, commit_json) "
                "VALUES (?, ?, 2, NULL, ?, ?)",
                (
                    successor_transition_id,
                    successor.project_head_id,
                    commit.manifest_sha256,
                    _v2_model_bytes(commit),
                ),
            )
            committed_reference = m2.SuccessorTransitionRefV2.model_validate(
                {
                    **reference.model_dump(mode="python"),
                    "successor_project_head": successor,
                }
            )
            committed_transition = _replace_v2_successor_transition(
                transition,
                transition=committed_reference,
                state="committed",
                progress_completed=transition.progress_total,
                error=None,
                updated_at=timestamp,
            )
            committed_attempt = _replace_v2_successor_transition_attempt(
                active_attempt,
                state="committed",
                commit_manifest_sha256=commit.manifest_sha256,
                updated_at=timestamp,
            )
            completed_task = _replace_v2_task(
                task,
                successor_transition=committed_reference,
                state="completed",
                updated_at=timestamp,
            )
            next_authority = ScienceProjectAdmissionAuthorityV2(
                project_id=authority.project_id,
                active_project_head=successor,
                project_config_sha256=authority.project_config_sha256,
                workspace_snapshot=successor.workspace_snapshot,
                normalized_evolution_intent_sha256=(authority.normalized_evolution_intent_sha256),
                blockers=(),
            )
            connection.execute(
                "UPDATE successor_transitions SET transition_json = ?, "
                "resource_version = resource_version + 1 "
                "WHERE successor_transition_id = ?",
                (_v2_model_bytes(committed_transition), successor_transition_id),
            )
            connection.execute(
                "UPDATE tasks SET task_json = ?, closed = 1, "
                "resource_version = resource_version + 1 WHERE task_id = ?",
                (_v2_model_bytes(completed_task), task.task_id),
            )
            _store_v2_successor_transition_attempt(
                connection,
                committed_attempt,
            )
            connection.execute(
                "UPDATE project_admission_authorities SET authority_json = ?, "
                "resource_version = resource_version + 1 WHERE project_id = ?",
                (_v2_authority_bytes(next_authority), authority.project_id),
            )
            _append_v2_event(
                connection,
                model_type=m2.EvolutionRevisionCommittedEventV2,
                event_type="evolution_revision_committed",
                project_id=successor.project_id,
                task_id=task.task_id,
                occurred_at=timestamp,
                payload={
                    "successor_transition_id": successor_transition_id,
                    "evolution_revision": successor.evolution_revision,
                },
            )
            _append_v2_event(
                connection,
                model_type=m2.RuntimeContextCommittedEventV2,
                event_type="runtime_context_committed",
                project_id=successor.project_id,
                task_id=task.task_id,
                occurred_at=timestamp,
                payload={
                    "successor_transition_id": successor_transition_id,
                    "runtime_context_snapshot": successor.runtime_context_snapshot,
                },
            )
            _append_v2_event(
                connection,
                model_type=m2.ProjectHeadActivatedEventV2,
                event_type="project_head_activated",
                project_id=successor.project_id,
                task_id=task.task_id,
                occurred_at=timestamp,
                payload={
                    "successor_transition_id": successor_transition_id,
                    "project_head": successor,
                },
            )
            _append_v2_event(
                connection,
                model_type=m2.TransitionChangedEventV2,
                event_type="transition_changed",
                project_id=successor.project_id,
                task_id=task.task_id,
                occurred_at=timestamp,
                payload={
                    "transition": committed_reference,
                    "state": committed_transition.state,
                    "progress_completed": committed_transition.progress_completed,
                    "progress_total": committed_transition.progress_total,
                },
            )
            return _load_v2_successor_transition(
                connection,
                successor_transition_id,
            )

    def get_successor_transition(
        self,
        successor_transition_id: str,
    ) -> m2.SuccessorTransitionV2:
        successor_transition_id = _v2_resource_id(
            successor_transition_id,
            label="successor transition",
        )
        with self._lock, self._reader() as connection:
            return _load_v2_successor_transition(
                connection,
                successor_transition_id,
            )

    def successor_transition_attempts(
        self,
        successor_transition_id: str,
    ) -> list[ScienceSuccessorTransitionAttemptV2]:
        successor_transition_id = _v2_resource_id(
            successor_transition_id,
            label="successor transition",
        )
        with self._lock, self._reader() as connection:
            _load_v2_successor_transition(
                connection,
                successor_transition_id,
            )
            return _load_v2_successor_transition_attempts(
                connection,
                successor_transition_id,
            )

    def current_successor_transition_attempt(
        self,
        successor_transition_id: str,
    ) -> ScienceSuccessorTransitionAttemptV2:
        return self.successor_transition_attempts(successor_transition_id)[-1]

    def successor_retry_evidence(
        self,
        successor_transition_id: str,
    ) -> tuple[
        m2.SuccessorTransitionV2,
        ScienceSuccessorPlanV2,
        m2.DatasetSealedEventV2 | None,
    ]:
        successor_transition_id = _v2_resource_id(
            successor_transition_id,
            label="successor transition",
        )
        with self._lock, self._reader() as connection:
            transition = _load_v2_successor_transition(
                connection,
                successor_transition_id,
            )
            if transition.state not in {"pending", "running_methods"}:
                raise ScienceTaskPreconditionFailedV2(
                    "v2 successor transition is not ready to resume"
                )
            row = connection.execute(
                "SELECT task_id, plan_sha256, plan_json "
                "FROM successor_transitions WHERE successor_transition_id = ?",
                (successor_transition_id,),
            ).fetchone()
            if row is None:
                raise ScienceTaskNotFoundV2("v2 successor transition was not found")
            plan = _v2_model_from_bytes(
                ScienceSuccessorPlanV2,
                bytes(row["plan_json"]),
            )
            if science_successor_plan_sha256(plan) != row["plan_sha256"]:
                raise ScienceTaskStoreV2Error("persisted v2 successor plan digest is inconsistent")
            dataset_event = _load_v2_dataset_event_for_task(
                connection,
                str(row["task_id"]),
            )
            if (transition.state == "pending") != (dataset_event is None):
                raise ScienceTaskStoreV2Error("v2 successor resume checkpoint is inconsistent")
            return transition, plan, dataset_event

    def list_successor_transitions(
        self,
        project_id: str,
    ) -> list[m2.SuccessorTransitionV2]:
        project_id = _v2_resource_id(project_id, label="project")
        with self._lock, self._reader() as connection:
            _load_v2_project_authority(connection, project_id)
            rows = connection.execute(
                "SELECT successor_transition_id FROM successor_transitions "
                "WHERE project_id = ? ORDER BY successor_transition_id LIMIT ?",
                (project_id, _MAX_V2_TASKS + 1),
            ).fetchall()
            if len(rows) > _MAX_V2_TASKS:
                raise ScienceTaskStoreV2Error(
                    "v2 successor transition inventory exceeds its bound"
                )
            return [
                _load_v2_successor_transition(
                    connection,
                    str(row["successor_transition_id"]),
                )
                for row in rows
            ]

    def get_successor_transition_for_task(
        self,
        task_id: str,
    ) -> m2.SuccessorTransitionV2:
        task_id = _v2_resource_id(task_id, label="task")
        with self._lock, self._reader() as connection:
            row = connection.execute(
                "SELECT successor_transition_id FROM successor_transitions WHERE task_id = ?",
                (task_id,),
            ).fetchone()
            if row is None:
                raise ScienceTaskNotFoundV2("v2 Task successor transition was not found")
            return _load_v2_successor_transition(
                connection,
                str(row["successor_transition_id"]),
            )

    def successor_commit(
        self,
        successor_transition_id: str,
    ) -> AtomicSuccessorCommitV2 | None:
        successor_transition_id = _v2_resource_id(
            successor_transition_id,
            label="successor transition",
        )
        with self._lock, self._reader() as connection:
            transition = _load_v2_successor_transition(
                connection,
                successor_transition_id,
            )
            row = connection.execute(
                "SELECT manifest_sha256, commit_json FROM successor_commits "
                "WHERE successor_transition_id = ?",
                (successor_transition_id,),
            ).fetchone()
            if row is None:
                if transition.state in {"committed", "cancelled"}:
                    raise ScienceTaskStoreV2Error(
                        "published v2 successor transition has no commit receipt"
                    )
                return None
            commit = _v2_model_from_bytes(
                AtomicSuccessorCommitV2,
                bytes(row["commit_json"]),
            )
            valid_terminal = (
                transition.state == "committed"
                and type(commit.manifest) is AtomicSuccessorManifestV2
            ) or (
                transition.state == "cancelled"
                and type(commit.manifest) is AtomicEvolutionAbandonManifestV2
            )
            if row["manifest_sha256"] != commit.manifest_sha256 or not valid_terminal:
                raise ScienceTaskStoreV2Error("v2 successor commit row is inconsistent")
            return commit

    def task_artifact_publications(
        self,
        task_id: str,
    ) -> tuple[TaskArtifactPublicationV2, ...]:
        """Read one Task's public-safe artifact receipts from one consistent closure."""

        task_id = _v2_resource_id(task_id, label="task")
        with self._lock, self._reader() as connection:
            task = _load_v2_task_closure(connection, task_id)
            reference = task.successor_transition
            if reference is None:
                return ()
            transition = _load_v2_successor_transition(
                connection,
                reference.successor_transition_id,
            )
            if transition.state not in {"committed", "cancelled"}:
                return ()
            commit = _load_v2_successor_commit(
                connection,
                reference.successor_transition_id,
            )
            if (
                commit.manifest.task_id != task.task_id
                or commit.manifest.project_id != task.project_id
            ):
                raise ScienceTaskStoreV2Error(
                    "v2 Task artifact publication differs from its Task closure"
                )
            return commit.manifest.task_artifacts

    def project_artifact_publication(
        self,
        project_id: str,
        artifact_id: str,
    ) -> TaskArtifactPublicationV2 | None:
        """Resolve one project-scoped artifact from immutable successor receipts."""

        project_id = _v2_resource_id(project_id, label="project")
        artifact_id = _v2_resource_id(artifact_id, label="artifact")
        with self._lock, self._reader() as connection:
            _load_v2_project_authority(connection, project_id)
            rows = connection.execute(
                "SELECT successor_commits.successor_transition_id "
                "FROM successor_commits "
                "JOIN successor_transitions USING(successor_transition_id) "
                "WHERE successor_transitions.project_id = ? "
                "ORDER BY successor_commits.successor_transition_id LIMIT ?",
                (project_id, _MAX_V2_TASKS + 1),
            ).fetchall()
            if len(rows) > _MAX_V2_TASKS:
                raise ScienceTaskStoreV2Error(
                    "v2 project artifact publication inventory exceeds its bound"
                )
            match: TaskArtifactPublicationV2 | None = None
            for row in rows:
                transition_id = str(row["successor_transition_id"])
                transition = _load_v2_successor_transition(
                    connection,
                    transition_id,
                )
                commit = _load_v2_successor_commit(connection, transition_id)
                if (
                    transition.transition.project_id != project_id
                    or commit.manifest.project_id != project_id
                ):
                    raise ScienceTaskStoreV2Error(
                        "v2 project artifact publication differs from its project closure"
                    )
                for publication in commit.manifest.task_artifacts:
                    if publication.artifact_id != artifact_id:
                        continue
                    if match is not None:
                        raise ScienceTaskStoreV2Error(
                            "v2 project artifact identity was published more than once"
                        )
                    match = publication
            return match

    def successor_commit_for_project_head(
        self,
        project_head_id: str,
    ) -> AtomicSuccessorCommitV2 | None:
        """Return the exact atomic receipt that published a non-genesis head."""

        project_head_id = _v2_resource_id(project_head_id, label="project head")
        with self._lock, self._reader() as connection:
            return _load_v2_successor_commit_for_project_head(
                connection,
                project_head_id,
            )

    def prior_dataset_artifact_ids_for_head(
        self,
        project_head_id: str,
    ) -> tuple[str, ...]:
        """Recover canonical prior dataset identity from the immutable head chain."""

        project_head_id = _v2_resource_id(project_head_id, label="project head")
        dataset_ids: list[str] = []
        seen_heads: set[str] = set()
        current_id: str | None = project_head_id
        while current_id is not None:
            if current_id in seen_heads or len(seen_heads) > _MAX_V2_TASKS:
                raise ScienceTaskStoreV2Error("v2 project-head chain is cyclic")
            seen_heads.add(current_id)
            head = self.get_project_head(current_id)
            commit = self.successor_commit_for_project_head(current_id)
            if commit is not None and type(commit.manifest) is AtomicSuccessorManifestV2:
                dataset_ids.append(commit.manifest.dataset_artifact_id)
            current_id = head.predecessor_project_head_id
        values = tuple(sorted(dataset_ids))
        if len(values) != len(set(values)):
            raise ScienceTaskStoreV2Error("v2 project-head chain reuses a dataset artifact")
        return values

    def nonterminal_successor_transition_ids(self) -> list[str]:
        terminal = ("committed", "failed", "cancelled", "superseded")
        with self._lock, self._reader() as connection:
            rows = connection.execute(
                "SELECT successor_transition_id, transition_json "
                "FROM successor_transitions ORDER BY successor_transition_id"
            ).fetchall()
            result: list[str] = []
            for row in rows:
                transition = _v2_model_from_bytes(
                    m2.SuccessorTransitionV2,
                    bytes(row["transition_json"]),
                )
                if transition.state not in terminal:
                    result.append(str(row["successor_transition_id"]))
            return result

    def resumable_successor_transition_ids(self) -> list[str]:
        with self._lock, self._reader() as connection:
            rows = connection.execute(
                "SELECT successor_transition_id, transition_json "
                "FROM successor_transitions ORDER BY successor_transition_id"
            ).fetchall()
            result: list[str] = []
            for row in rows:
                transition = _v2_model_from_bytes(
                    m2.SuccessorTransitionV2,
                    bytes(row["transition_json"]),
                )
                if transition.state in {"pending", "running_methods"}:
                    result.append(str(row["successor_transition_id"]))
            return result

    def submit_task(
        self,
        *,
        request: m2.TaskSubmitRequestV2,
        idempotency_key: str,
        now: datetime,
    ) -> tuple[m2.TaskV2, bool]:
        request = _validate_v2_model(m2.TaskSubmitRequestV2, request)
        idempotency_key = _v2_idempotency_key(idempotency_key)
        request_json = _v2_model_bytes(request)
        request_sha256 = hashlib.sha256(request_json).hexdigest()
        with self._lock, self._transaction() as connection:
            existing = connection.execute(
                "SELECT task_id, request_sha256, request_json FROM tasks "
                "WHERE project_id = ? AND idempotency_key = ?",
                (request.project_id, idempotency_key),
            ).fetchone()
            if existing is not None:
                if (
                    existing["request_sha256"] != request_sha256
                    or bytes(existing["request_json"]) != request_json
                ):
                    raise ScienceTaskIdempotencyConflictV2("v2 Task idempotency key was reused")
                return _load_v2_task_closure(connection, str(existing["task_id"])), True

            authority_row = connection.execute(
                "SELECT authority_json FROM project_admission_authorities WHERE project_id = ?",
                (request.project_id,),
            ).fetchone()
            if authority_row is None:
                raise ScienceTaskNotReadyV2(
                    (ScienceProjectReadinessBlockerV2.SUCCESSOR_TRANSITION,)
                )
            authority = _v2_authority_from_bytes(bytes(authority_row["authority_json"]))
            if authority.blockers:
                raise ScienceTaskNotReadyV2(authority.blockers)
            _validate_v2_submission_authority(request, authority)
            if (
                connection.execute(
                    "SELECT 1 FROM tasks WHERE project_id = ? AND closed = 0 LIMIT 1",
                    (request.project_id,),
                ).fetchone()
                is not None
            ):
                raise ScienceTaskProjectInFlightV2("project has an immutable v2 Task in flight")
            if int(connection.execute("SELECT COUNT(*) FROM tasks").fetchone()[0]) >= (
                _MAX_V2_TASKS
            ):
                raise ScienceTaskConflictV2("v2 Task capacity is exhausted")

            task = _new_v2_task(request=request, authority=authority, now=now)
            admission = task.admission
            first_attempt = task.attempts[0]
            connection.execute(
                "INSERT INTO tasks(task_id, project_id, task_admission_id, "
                "admission_sha256, idempotency_key, request_sha256, request_json, "
                "task_json, closed, resource_version) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0, 1)",
                (
                    task.task_id,
                    task.project_id,
                    admission.task_admission_id,
                    admission.admission_sha256,
                    idempotency_key,
                    request_sha256,
                    request_json,
                    _v2_model_bytes(task),
                ),
            )
            connection.execute(
                "INSERT INTO task_admissions(task_admission_id, task_id, admission_sha256, "
                "admission_json) VALUES (?, ?, ?, ?)",
                (
                    admission.task_admission_id,
                    task.task_id,
                    admission.admission_sha256,
                    _v2_model_bytes(admission),
                ),
            )
            connection.execute(
                "INSERT INTO attempts(attempt_id, task_id, ordinal, attempt_json) "
                "VALUES (?, ?, ?, ?)",
                (
                    first_attempt.attempt_id,
                    task.task_id,
                    first_attempt.ordinal,
                    _v2_model_bytes(first_attempt),
                ),
            )
            _append_v2_event(
                connection,
                model_type=m2.TaskAdmittedEventV2,
                event_type="task_admitted",
                project_id=task.project_id,
                task_id=task.task_id,
                occurred_at=task.created_at,
                payload={"admission": admission},
            )
            _append_v2_event(
                connection,
                model_type=m2.AttemptAppendedEventV2,
                event_type="attempt_appended",
                project_id=first_attempt.project_id,
                task_id=first_attempt.task_id,
                occurred_at=first_attempt.created_at,
                payload={"attempt": first_attempt},
            )
            return _load_v2_task_closure(connection, task.task_id), False

    def append_attempt(
        self,
        *,
        task_id: str,
        request: m2.AttemptAppendRequestV2,
        idempotency_key: str,
        now: datetime,
    ) -> tuple[m2.AttemptRefV2, bool]:
        task_id = _v2_resource_id(task_id, label="task")
        request = _validate_v2_model(m2.AttemptAppendRequestV2, request)
        idempotency_key = _v2_idempotency_key(idempotency_key)
        request_json = _v2_model_bytes(request)
        request_sha256 = hashlib.sha256(request_json).hexdigest()
        with self._lock, self._transaction() as connection:
            replay = connection.execute(
                "SELECT request_sha256, request_json, attempt_id "
                "FROM attempt_append_requests WHERE task_id = ? AND idempotency_key = ?",
                (task_id, idempotency_key),
            ).fetchone()
            if replay is not None:
                if (
                    replay["request_sha256"] != request_sha256
                    or bytes(replay["request_json"]) != request_json
                ):
                    raise ScienceTaskIdempotencyConflictV2("v2 Attempt idempotency key was reused")
                return _load_v2_attempt(
                    connection,
                    task_id=task_id,
                    attempt_id=str(replay["attempt_id"]),
                ), True

            task = _load_v2_task_closure(connection, task_id)
            if (
                task.state not in {"admitted", "failed", "cancelled"}
                or task.authoritative_attempt_id is not None
                or task.successor_transition is not None
            ):
                raise ScienceTaskTerminalV2("v2 Task cannot accept another Attempt")
            if (
                request.task_admission_id != task.admission.task_admission_id
                or request.admission_sha256 != task.admission.admission_sha256
            ):
                raise ScienceTaskPreconditionFailedV2(
                    "v2 Attempt request does not bind the immutable admission"
                )
            prior = task.attempts[-1]
            if (
                request.expected_previous_attempt_id != prior.attempt_id
                or request.expected_next_ordinal != prior.ordinal + 1
            ):
                raise ScienceTaskPreconditionFailedV2("v2 Attempt append precondition changed")
            attempt = m2.AttemptRefV2(
                attempt_id=f"attempt-{secrets.token_hex(16)}",
                ordinal=request.expected_next_ordinal,
                task_id=task.task_id,
                task_admission_id=task.admission.task_admission_id,
                admission_sha256=task.admission.admission_sha256,
                project_id=task.project_id,
                predecessor_project_head_id=(
                    task.admission.predecessor_project_head.project_head_id
                ),
                created_at=_v2_timestamp(now),
            )
            updated = _replace_v2_task(
                task,
                attempts=[*task.attempts, attempt],
                updated_at=_v2_timestamp(now),
            )
            connection.execute(
                "INSERT INTO attempts(attempt_id, task_id, ordinal, attempt_json) "
                "VALUES (?, ?, ?, ?)",
                (
                    attempt.attempt_id,
                    task.task_id,
                    attempt.ordinal,
                    _v2_model_bytes(attempt),
                ),
            )
            connection.execute(
                "INSERT INTO attempt_append_requests(task_id, idempotency_key, "
                "request_sha256, request_json, attempt_id) VALUES (?, ?, ?, ?, ?)",
                (
                    task.task_id,
                    idempotency_key,
                    request_sha256,
                    request_json,
                    attempt.attempt_id,
                ),
            )
            connection.execute(
                "UPDATE tasks SET task_json = ?, resource_version = resource_version + 1 "
                "WHERE task_id = ?",
                (_v2_model_bytes(updated), task.task_id),
            )
            recovered = _load_v2_task_closure(connection, task.task_id)
            if recovered.attempts[-1] != attempt:
                raise ScienceTaskStoreV2Error("v2 Attempt append readback is inconsistent")
            _append_v2_event(
                connection,
                model_type=m2.AttemptAppendedEventV2,
                event_type="attempt_appended",
                project_id=attempt.project_id,
                task_id=attempt.task_id,
                occurred_at=attempt.created_at,
                payload={"attempt": attempt},
            )
            return attempt, False

    def begin_attempt_execution(
        self,
        *,
        task_id: str,
        attempt_id: str,
        now: datetime,
    ) -> ScienceAttemptExecutionRecordV2:
        """Move the latest immutable Attempt into Daemon-owned preparation."""

        task_id = _v2_resource_id(task_id, label="task")
        attempt_id = _v2_resource_id(attempt_id, label="attempt")
        timestamp = _v2_timestamp(now)
        with self._lock, self._transaction() as connection:
            task = _load_v2_task_closure(connection, task_id)
            attempt = _load_v2_attempt(
                connection,
                task_id=task_id,
                attempt_id=attempt_id,
            )
            existing = _load_v2_attempt_execution_optional(connection, attempt_id)
            if existing is not None:
                if existing.task_id != task_id:
                    raise ScienceTaskStoreV2Error("v2 Attempt execution belongs to another Task")
                if existing.state == "preparing":
                    return existing
                raise ScienceTaskTerminalV2("v2 Attempt execution preparation already advanced")
            if (
                task.state not in {"admitted", "failed", "cancelled"}
                or task.authoritative_attempt_id is not None
                or task.successor_transition is not None
                or attempt != task.attempts[-1]
            ):
                raise ScienceTaskTerminalV2(
                    "v2 Attempt cannot begin execution in the current Task state"
                )
            active = connection.execute(
                "SELECT 1 FROM attempt_executions WHERE task_id = ? "
                "AND state IN ('preparing', 'running', 'cancelling') LIMIT 1",
                (task_id,),
            ).fetchone()
            if active is not None:
                raise ScienceTaskConflictV2("v2 Task already has an active Attempt execution")
            record = ScienceAttemptExecutionRecordV2(
                task_id=task_id,
                attempt_id=attempt_id,
                state="preparing",
                created_at=timestamp,
                updated_at=timestamp,
            )
            updated_task = _replace_v2_task(
                task,
                state="preparing",
                updated_at=timestamp,
            )
            connection.execute(
                "INSERT INTO attempt_executions(attempt_id, task_id, state, "
                "receipt_sha256, session_result_sha256, session_result_json, "
                "execution_json, resource_version) "
                "VALUES (?, ?, ?, NULL, NULL, NULL, ?, 1)",
                (attempt_id, task_id, record.state, _v2_model_bytes(record)),
            )
            connection.execute(
                "UPDATE tasks SET task_json = ?, resource_version = resource_version + 1 "
                "WHERE task_id = ?",
                (_v2_model_bytes(updated_task), task_id),
            )
            return _load_v2_attempt_execution(connection, attempt_id)

    def mark_attempt_running(
        self,
        *,
        task_id: str,
        attempt_id: str,
        now: datetime,
    ) -> ScienceAttemptExecutionRecordV2:
        task_id = _v2_resource_id(task_id, label="task")
        attempt_id = _v2_resource_id(attempt_id, label="attempt")
        with self._lock, self._transaction() as connection:
            task = _load_v2_task_closure(connection, task_id)
            record = _load_v2_attempt_execution(connection, attempt_id)
            _validate_v2_execution_ownership(task, record)
            if record.state == "running":
                return record
            if record.state != "preparing" or task.state != "preparing":
                raise ScienceTaskTerminalV2("v2 Attempt cannot enter running state")
            timestamp = _v2_timestamp(now)
            updated_record = _replace_v2_execution_record(
                record,
                state="running",
                updated_at=timestamp,
            )
            updated_task = _replace_v2_task(
                task,
                state="running",
                updated_at=timestamp,
            )
            _update_v2_attempt_execution(connection, updated_record)
            connection.execute(
                "UPDATE tasks SET task_json = ?, resource_version = resource_version + 1 "
                "WHERE task_id = ?",
                (_v2_model_bytes(updated_task), task_id),
            )
            return _load_v2_attempt_execution(connection, attempt_id)

    def record_terminal_attempt(
        self,
        *,
        task_id: str,
        attempt_id: str,
        receipt: ScienceAttemptExecutionReceiptV2,
        evidence: ScienceAttemptExecutionEvidenceV2,
        successor_plan: ScienceSuccessorPlanV2,
        terminal_result: SessionResult,
        now: datetime,
    ) -> ScienceAttemptExecutionRecordV2:
        """Atomically elect exactly one verified Attempt result as authoritative."""

        task_id = _v2_resource_id(task_id, label="task")
        attempt_id = _v2_resource_id(attempt_id, label="attempt")
        receipt = _validate_v2_model(ScienceAttemptExecutionReceiptV2, receipt)
        evidence = _validate_v2_model(ScienceAttemptExecutionEvidenceV2, evidence)
        successor_plan = _validate_v2_model(
            ScienceSuccessorPlanV2,
            successor_plan,
        )
        terminal_result = canonical_subscription_session_result(terminal_result)
        result_json = science_session_result_bytes(terminal_result)
        result_sha256 = hashlib.sha256(result_json).hexdigest()
        with self._lock, self._transaction() as connection:
            task = _load_v2_task_closure(connection, task_id)
            record = _load_v2_attempt_execution(connection, attempt_id)
            _validate_v2_execution_ownership(task, record)
            _validate_v2_terminal_execution_closure(
                connection=connection,
                task=task,
                attempt_id=attempt_id,
                receipt=receipt,
                evidence=evidence,
                successor_plan=successor_plan,
                terminal_result=terminal_result,
            )
            if record.state == "captured":
                if (
                    record.receipt == receipt
                    and record.evidence == evidence
                    and record.successor_plan == successor_plan
                    and _load_v2_captured_session_result(
                        connection,
                        attempt_id,
                    )
                    == terminal_result
                ):
                    return record
                raise ScienceTaskConflictV2("v2 Attempt terminal evidence changed after capture")
            if (
                record.state not in {"preparing", "running"}
                or task.state not in {"preparing", "running"}
                or task.authoritative_attempt_id is not None
                or task.successor_transition is not None
            ):
                raise ScienceTaskTerminalV2(
                    "v2 Attempt terminal result lost authoritative ownership"
                )
            timestamp = _v2_timestamp(now)
            captured = _replace_v2_execution_record(
                record,
                state="captured",
                receipt=receipt,
                evidence=evidence,
                successor_plan=successor_plan,
                error_code=None,
                updated_at=timestamp,
            )
            authoritative = _replace_v2_task(
                task,
                authoritative_attempt_id=attempt_id,
                state="waiting_for_successor",
                updated_at=timestamp,
            )
            _update_v2_attempt_execution(connection, captured)
            updated_result = connection.execute(
                "UPDATE attempt_executions SET session_result_sha256 = ?, "
                "session_result_json = ? WHERE attempt_id = ? AND task_id = ? "
                "AND session_result_sha256 IS NULL AND session_result_json IS NULL",
                (result_sha256, result_json, attempt_id, task_id),
            ).rowcount
            if updated_result != 1:
                raise ScienceTaskStoreV2Error("v2 Attempt result persistence lost ownership")
            connection.execute(
                "UPDATE tasks SET task_json = ?, resource_version = resource_version + 1 "
                "WHERE task_id = ?",
                (_v2_model_bytes(authoritative), task_id),
            )
            return _load_v2_attempt_execution(connection, attempt_id)

    def get_captured_session_result(
        self,
        task_id: str,
        attempt_id: str,
    ) -> SessionResult:
        task_id = _v2_resource_id(task_id, label="task")
        attempt_id = _v2_resource_id(attempt_id, label="attempt")
        with self._lock, self._reader() as connection:
            record = _load_v2_attempt_execution(connection, attempt_id)
            if record.task_id != task_id or record.state != "captured":
                raise ScienceTaskTerminalV2("v2 Attempt has no authoritative captured result")
            return _load_v2_captured_session_result(connection, attempt_id)

    def request_attempt_cancellation(
        self,
        *,
        task_id: str,
        attempt_id: str,
        now: datetime,
    ) -> ScienceAttemptExecutionRecordV2:
        task_id = _v2_resource_id(task_id, label="task")
        attempt_id = _v2_resource_id(attempt_id, label="attempt")
        with self._lock, self._transaction() as connection:
            task = _load_v2_task_closure(connection, task_id)
            record = _load_v2_attempt_execution(connection, attempt_id)
            _validate_v2_execution_ownership(task, record)
            if record.state in {"cancelling", "cancelled"}:
                return record
            if (
                record.state not in {"preparing", "running"}
                or task.state not in {"preparing", "running"}
                or task.authoritative_attempt_id is not None
            ):
                raise ScienceTaskTerminalV2("v2 Attempt cancellation lost authoritative ownership")
            timestamp = _v2_timestamp(now)
            cancelling = _replace_v2_execution_record(
                record,
                state="cancelling",
                updated_at=timestamp,
            )
            cancelling_task = _replace_v2_task(
                task,
                state="cancelling",
                updated_at=timestamp,
            )
            _update_v2_attempt_execution(connection, cancelling)
            connection.execute(
                "UPDATE tasks SET task_json = ?, resource_version = resource_version + 1 "
                "WHERE task_id = ?",
                (_v2_model_bytes(cancelling_task), task_id),
            )
            return _load_v2_attempt_execution(connection, attempt_id)

    def request_attempt_cancellation_action(
        self,
        *,
        task_id: str,
        attempt_id: str,
        request: m2.TaskActionRequestV2,
        expected_etag: str,
        allow_cancelled_recovery: bool,
        now: datetime,
    ) -> ScienceAttemptExecutionRecordV2:
        """Atomically validate public mutation authority and request cancellation."""

        task_id = _v2_resource_id(task_id, label="task")
        attempt_id = _v2_resource_id(attempt_id, label="attempt")
        request = _validate_v2_model(m2.TaskActionRequestV2, request)
        if re.fullmatch(r'"[0-9a-f]{64}"', expected_etag, flags=re.ASCII) is None:
            raise ValueError("v2 Task expected ETag is invalid")
        if type(allow_cancelled_recovery) is not bool:
            raise TypeError("v2 Attempt cancellation recovery flag must be exact bool")
        with self._lock, self._transaction() as connection:
            task = _load_v2_task_closure(connection, task_id)
            attempt = _load_v2_attempt(
                connection,
                task_id=task_id,
                attempt_id=attempt_id,
            )
            if (
                request.task_admission_id != task.admission.task_admission_id
                or request.admission_sha256 != task.admission.admission_sha256
            ):
                raise ScienceTaskPreconditionFailedV2(
                    "v2 Attempt cancellation does not bind the immutable admission"
                )
            if attempt != task.attempts[-1]:
                raise ScienceTaskTerminalV2("only the latest v2 Attempt may be cancelled")
            record = _load_v2_attempt_execution_optional(connection, attempt_id)
            if record is not None and record.task_id != task_id:
                raise ScienceTaskStoreV2Error("v2 Attempt execution belongs to another Task")
            if (
                allow_cancelled_recovery
                and record is not None
                and record.state in {"cancelling", "cancelled"}
                and task.state in {"cancelling", "cancelled"}
                and task.authoritative_attempt_id is None
                and task.successor_transition is None
            ):
                return record
            if task.etag != expected_etag:
                raise ScienceTaskETagChangedV2("v2 Task ETag changed")
            if record is not None and record.state in {"cancelling", "cancelled"}:
                raise ScienceTaskTerminalV2(
                    "v2 Attempt cancellation belongs to another action"
                )
            timestamp = _v2_timestamp(now)
            if record is None:
                if (
                    task.state not in {"admitted", "failed", "cancelled"}
                    or task.authoritative_attempt_id is not None
                    or task.successor_transition is not None
                ):
                    raise ScienceTaskTerminalV2(
                        "v2 Attempt cannot be cancelled in the current Task state"
                    )
                active = connection.execute(
                    "SELECT 1 FROM attempt_executions WHERE task_id = ? "
                    "AND state IN ('preparing', 'running', 'cancelling') LIMIT 1",
                    (task_id,),
                ).fetchone()
                if active is not None:
                    raise ScienceTaskConflictV2(
                        "v2 Task already has an active Attempt execution"
                    )
                cancelling = ScienceAttemptExecutionRecordV2(
                    task_id=task_id,
                    attempt_id=attempt_id,
                    state="cancelling",
                    created_at=timestamp,
                    updated_at=timestamp,
                )
                connection.execute(
                    "INSERT INTO attempt_executions(attempt_id, task_id, state, "
                    "receipt_sha256, session_result_sha256, session_result_json, "
                    "execution_json, resource_version) "
                    "VALUES (?, ?, ?, NULL, NULL, NULL, ?, 1)",
                    (attempt_id, task_id, cancelling.state, _v2_model_bytes(cancelling)),
                )
            else:
                _validate_v2_execution_ownership(task, record)
                if (
                    record.state not in {"preparing", "running"}
                    or task.state not in {"preparing", "running"}
                    or task.authoritative_attempt_id is not None
                ):
                    raise ScienceTaskTerminalV2(
                        "v2 Attempt cancellation lost authoritative ownership"
                    )
                cancelling = _replace_v2_execution_record(
                    record,
                    state="cancelling",
                    updated_at=timestamp,
                )
                _update_v2_attempt_execution(connection, cancelling)
            cancelling_task = _replace_v2_task(
                task,
                state="cancelling",
                updated_at=timestamp,
            )
            connection.execute(
                "UPDATE tasks SET task_json = ?, resource_version = resource_version + 1 "
                "WHERE task_id = ?",
                (_v2_model_bytes(cancelling_task), task_id),
            )
            return _load_v2_attempt_execution(connection, attempt_id)

    def finish_attempt_cancelled(
        self,
        *,
        task_id: str,
        attempt_id: str,
        now: datetime,
    ) -> ScienceAttemptExecutionRecordV2:
        task_id = _v2_resource_id(task_id, label="task")
        attempt_id = _v2_resource_id(attempt_id, label="attempt")
        with self._lock, self._transaction() as connection:
            task = _load_v2_task_closure(connection, task_id)
            record = _load_v2_attempt_execution(connection, attempt_id)
            _validate_v2_execution_ownership(task, record)
            if record.state == "cancelled":
                return record
            if record.state != "cancelling" or task.state != "cancelling":
                raise ScienceTaskTerminalV2("v2 Attempt cannot complete cancellation")
            timestamp = _v2_timestamp(now)
            cancelled = _replace_v2_execution_record(
                record,
                state="cancelled",
                updated_at=timestamp,
            )
            cancelled_task = _replace_v2_task(
                task,
                state="cancelled",
                updated_at=timestamp,
            )
            _update_v2_attempt_execution(connection, cancelled)
            connection.execute(
                "UPDATE tasks SET task_json = ?, resource_version = resource_version + 1 "
                "WHERE task_id = ?",
                (_v2_model_bytes(cancelled_task), task_id),
            )
            return _load_v2_attempt_execution(connection, attempt_id)

    def finish_attempt_failed(
        self,
        *,
        task_id: str,
        attempt_id: str,
        error_code: str,
        now: datetime,
    ) -> ScienceAttemptExecutionRecordV2:
        task_id = _v2_resource_id(task_id, label="task")
        attempt_id = _v2_resource_id(attempt_id, label="attempt")
        if (
            not isinstance(error_code, str)
            or re.fullmatch(r"[a-z][a-z0-9_]{0,127}", error_code, flags=re.ASCII) is None
        ):
            raise ValueError("v2 Attempt execution error code is invalid")
        with self._lock, self._transaction() as connection:
            task = _load_v2_task_closure(connection, task_id)
            record = _load_v2_attempt_execution(connection, attempt_id)
            _validate_v2_execution_ownership(task, record)
            if record.state == "failed":
                if record.error_code != error_code:
                    raise ScienceTaskConflictV2(
                        "v2 Attempt execution failure changed after persistence"
                    )
                return record
            if (
                record.state not in {"preparing", "running", "cancelling"}
                or task.authoritative_attempt_id is not None
                or task.successor_transition is not None
            ):
                raise ScienceTaskTerminalV2("v2 Attempt execution is already terminal")
            timestamp = _v2_timestamp(now)
            failed = _replace_v2_execution_record(
                record,
                state="failed",
                error_code=error_code,
                updated_at=timestamp,
            )
            failed_task = _replace_v2_task(
                task,
                state="failed",
                updated_at=timestamp,
            )
            _update_v2_attempt_execution(connection, failed)
            connection.execute(
                "UPDATE tasks SET task_json = ?, resource_version = resource_version + 1 "
                "WHERE task_id = ?",
                (_v2_model_bytes(failed_task), task_id),
            )
            return _load_v2_attempt_execution(connection, attempt_id)

    def get_attempt_execution(
        self,
        task_id: str,
        attempt_id: str,
    ) -> ScienceAttemptExecutionRecordV2:
        task_id = _v2_resource_id(task_id, label="task")
        attempt_id = _v2_resource_id(attempt_id, label="attempt")
        with self._lock, self._reader() as connection:
            task = _load_v2_task_closure(connection, task_id)
            record = _load_v2_attempt_execution(connection, attempt_id)
            _validate_v2_execution_ownership(task, record)
            return record

    def get_attempt_execution_optional(
        self,
        task_id: str,
        attempt_id: str,
    ) -> ScienceAttemptExecutionRecordV2 | None:
        task_id = _v2_resource_id(task_id, label="task")
        attempt_id = _v2_resource_id(attempt_id, label="attempt")
        with self._lock, self._reader() as connection:
            task = _load_v2_task_closure(connection, task_id)
            _load_v2_attempt(
                connection,
                task_id=task_id,
                attempt_id=attempt_id,
            )
            record = _load_v2_attempt_execution_optional(connection, attempt_id)
            if record is not None:
                _validate_v2_execution_ownership(task, record)
            return record

    def captured_attempt_executions(self) -> list[ScienceAttemptExecutionRecordV2]:
        with self._lock, self._reader() as connection:
            rows = connection.execute(
                "SELECT attempt_id FROM attempt_executions WHERE state = 'captured' "
                "ORDER BY attempt_id LIMIT ?",
                (_MAX_V2_TASKS + 1,),
            ).fetchall()
            if len(rows) > _MAX_V2_TASKS:
                raise ScienceTaskStoreV2Error("v2 captured Attempt inventory exceeds its bound")
            return [_load_v2_attempt_execution(connection, str(row["attempt_id"])) for row in rows]

    def captured_attempt_ids(self) -> list[str]:
        return [record.attempt_id for record in self.captured_attempt_executions()]

    def unstarted_attempts(self) -> list[tuple[m2.TaskV2, m2.AttemptRefV2]]:
        """Return latest Attempts that have immutable admission but no execution."""

        with self._lock, self._reader() as connection:
            rows = connection.execute(
                "SELECT task_id FROM tasks WHERE closed = 0 ORDER BY task_id LIMIT ?",
                (_MAX_V2_TASKS + 1,),
            ).fetchall()
            if len(rows) > _MAX_V2_TASKS:
                raise ScienceTaskStoreV2Error("v2 unstarted Attempt inventory exceeds its bound")
            pending: list[tuple[m2.TaskV2, m2.AttemptRefV2]] = []
            for row in rows:
                task = _load_v2_task_closure(connection, str(row["task_id"]))
                if (
                    task.state not in {"admitted", "failed", "cancelled"}
                    or task.authoritative_attempt_id is not None
                    or task.successor_transition is not None
                ):
                    continue
                attempt = task.attempts[-1]
                if (
                    _load_v2_attempt_execution_optional(
                        connection,
                        attempt.attempt_id,
                    )
                    is None
                ):
                    pending.append((task, attempt))
            return pending

    def recover_interrupted_attempts(self, *, now: datetime) -> list[str]:
        """Fail in-process executions closed; captured results remain resumable."""

        timestamp = _v2_timestamp(now)
        with self._lock, self._transaction() as connection:
            rows = connection.execute(
                "SELECT attempt_id FROM attempt_executions "
                "WHERE state IN ('preparing', 'running', 'cancelling') "
                "ORDER BY attempt_id LIMIT ?",
                (_MAX_V2_TASKS + 1,),
            ).fetchall()
            if len(rows) > _MAX_V2_TASKS:
                raise ScienceTaskStoreV2Error("v2 interrupted Attempt inventory exceeds its bound")
            recovered: list[str] = []
            for row in rows:
                attempt_id = str(row["attempt_id"])
                record = _load_v2_attempt_execution(connection, attempt_id)
                task = _load_v2_task_closure(connection, record.task_id)
                _validate_v2_execution_ownership(task, record)
                if task.authoritative_attempt_id is not None:
                    raise ScienceTaskStoreV2Error(
                        "interrupted v2 Attempt unexpectedly owns a result"
                    )
                failed = _replace_v2_execution_record(
                    record,
                    state="failed",
                    error_code="daemon_restarted_during_attempt",
                    updated_at=timestamp,
                )
                failed_task = _replace_v2_task(
                    task,
                    state="failed",
                    updated_at=timestamp,
                )
                _update_v2_attempt_execution(connection, failed)
                connection.execute(
                    "UPDATE tasks SET task_json = ?, "
                    "resource_version = resource_version + 1 WHERE task_id = ?",
                    (_v2_model_bytes(failed_task), task.task_id),
                )
                recovered.append(attempt_id)
            return recovered

    def register_attempt_run_admission(
        self,
        *,
        task_id: str,
        attempt_id: str,
        check: GenerationBoundRunAdmissionCheck,
        allow_create: bool,
    ) -> bool:
        """Pre-register the exact rollout payload for one active Attempt."""

        task_id = _v2_resource_id(task_id, label="task")
        attempt_id = _v2_resource_id(attempt_id, label="attempt")
        if type(check) is not GenerationBoundRunAdmissionCheck:
            raise TypeError("v2 run admission check has the wrong type")
        if type(allow_create) is not bool:
            raise TypeError("v2 run admission create flag must be exact bool")
        if check.task_id is None:
            raise ValueError("v2 run admission requires a rollout Task identity")
        normalized_session = check.session_id or ""
        with self._lock, self._transaction() as connection:
            task = _load_v2_task_closure(connection, task_id)
            record = _load_v2_attempt_execution(connection, attempt_id)
            _validate_v2_execution_ownership(task, record)
            row = _v2_run_admission_row(connection, check)
            if row is not None:
                if record.state not in {
                    "preparing",
                    "running",
                    "cancelling",
                } or task.state not in {"preparing", "running", "cancelling"}:
                    return False
                if _v2_run_admission_matches(row, check) and (
                    row["task_id"] == task_id and row["attempt_id"] == attempt_id
                ):
                    return True
                raise ScienceTaskConflictV2(
                    "v2 run admission identity was reused with different authority"
                )
            if not allow_create:
                raise ScienceTaskConflictV2("v2 run admission is not pre-authorized")
            if (
                check.operation is not RunAdmissionOperation.ROLLOUT_TASK_SUBMIT
                or normalized_session
                or record.state not in {"preparing", "running"}
                or task.state not in {"preparing", "running"}
                or check.registry_digest != task.admission.registry_sha256
            ):
                raise ScienceTaskPreconditionFailedV2(
                    "v2 rollout admission does not bind an active Attempt"
                )
            _insert_v2_run_admission(
                connection,
                task_id=task_id,
                attempt_id=attempt_id,
                check=check,
            )
            return True

    def verify_attempt_run_admission(
        self,
        check: GenerationBoundRunAdmissionCheck,
    ) -> bool:
        """Verify, and for a gateway child operation durably derive, authority."""

        if type(check) is not GenerationBoundRunAdmissionCheck:
            raise TypeError("v2 run admission check has the wrong type")
        if check.task_id is None:
            return False
        with self._lock, self._transaction() as connection:
            row = _v2_run_admission_row(connection, check)
            if row is not None:
                record = _load_v2_attempt_execution(
                    connection,
                    str(row["attempt_id"]),
                )
                task = _load_v2_task_closure(connection, str(row["task_id"]))
                _validate_v2_execution_ownership(task, record)
                return (
                    record.state in {"preparing", "running", "cancelling"}
                    and task.state in {"preparing", "running", "cancelling"}
                    and _v2_run_admission_matches(row, check)
                )
            if check.operation is RunAdmissionOperation.ROLLOUT_TASK_SUBMIT:
                return False
            if check.session_id is None:
                return False
            rollout = connection.execute(
                "SELECT * FROM attempt_run_admissions WHERE operation = ? "
                "AND rollout_task_id = ? AND session_id = '' LIMIT 2",
                (RunAdmissionOperation.ROLLOUT_TASK_SUBMIT.value, check.task_id),
            ).fetchall()
            if len(rollout) != 1:
                return False
            parent = rollout[0]
            if any(
                parent[column] != value
                for column, value in (
                    ("generation_digest", check.generation_digest),
                    ("registry_digest", check.registry_digest),
                    ("framework_lock_digest", check.framework_lock_digest),
                )
            ):
                return False
            record = _load_v2_attempt_execution(
                connection,
                str(parent["attempt_id"]),
            )
            task = _load_v2_task_closure(connection, str(parent["task_id"]))
            _validate_v2_execution_ownership(task, record)
            if record.state not in {"preparing", "running", "cancelling"} or task.state not in {
                "preparing",
                "running",
                "cancelling",
            }:
                return False
            _insert_v2_run_admission(
                connection,
                task_id=task.task_id,
                attempt_id=record.attempt_id,
                check=check,
            )
            return True

    def close_task(
        self,
        task_id: str,
        request: m2.TaskActionRequestV2,
        *,
        close_request_id: str,
        now: datetime,
        expected_etag: str | None = None,
        allow_closed_recovery: bool = False,
    ) -> m2.TaskV2:
        task_id = _v2_resource_id(task_id, label="task")
        close_request_id = _v2_resource_id(
            close_request_id,
            label="Task close request",
        )
        request = _validate_v2_model(m2.TaskActionRequestV2, request)
        if (
            expected_etag is not None
            and re.fullmatch(r'"[0-9a-f]{64}"', expected_etag, flags=re.ASCII) is None
        ):
            raise ValueError("v2 Task expected ETag is invalid")
        if type(allow_closed_recovery) is not bool:
            raise TypeError("v2 Task close recovery flag must be exact bool")
        with self._lock, self._transaction() as connection:
            task = _load_v2_task_closure(connection, task_id)
            receipt = _load_v2_task_close_receipt(
                connection,
                task_id,
            )
            legacy_close = _load_v2_legacy_task_close_exemption(
                connection,
                task_id,
            )
            if receipt is not None and legacy_close is not None:
                raise ScienceTaskStoreV2Error("v2 Task exposes conflicting close authority")
            if receipt is not None and (
                receipt.task_admission_id != task.admission.task_admission_id
                or receipt.admission_sha256 != task.admission.admission_sha256
            ):
                raise ScienceTaskStoreV2Error("v2 Task close authority is inconsistent")
            if legacy_close is not None and (
                task.state != "closed"
                or legacy_close.task_admission_id != task.admission.task_admission_id
                or legacy_close.admission_sha256 != task.admission.admission_sha256
            ):
                raise ScienceTaskStoreV2Error("legacy v2 Task close authority is inconsistent")
            if (
                request.task_admission_id != task.admission.task_admission_id
                or request.admission_sha256 != task.admission.admission_sha256
            ):
                raise ScienceTaskPreconditionFailedV2(
                    "v2 Task close does not bind the immutable admission"
                )
            if task.state == "closed":
                if allow_closed_recovery and receipt == _TaskCloseReceiptV2(
                    task_id=task.task_id,
                    close_request_id=close_request_id,
                    expected_etag=expected_etag,
                    task_admission_id=(request.task_admission_id),
                    admission_sha256=(request.admission_sha256),
                ):
                    return task
                if expected_etag is not None and task.etag != expected_etag:
                    raise ScienceTaskETagChangedV2("v2 Task ETag changed")
                raise ScienceTaskTerminalV2("closed v2 Task belongs to another close action")
            if receipt is not None or legacy_close is not None:
                raise ScienceTaskStoreV2Error("non-closed v2 Task exposes a close receipt")
            if expected_etag is not None and task.etag != expected_etag:
                raise ScienceTaskETagChangedV2("v2 Task ETag changed")
            if task.authoritative_attempt_id is not None or task.successor_transition is not None:
                raise ScienceTaskTerminalV2(
                    "authoritative v2 Task ownership cannot be closed or rewritten"
                )
            updated = _replace_v2_task(
                task,
                state="closed",
                updated_at=_v2_timestamp(now),
            )
            connection.execute(
                "UPDATE tasks SET task_json = ?, closed = 1, "
                "resource_version = resource_version + 1 WHERE task_id = ?",
                (_v2_model_bytes(updated), task.task_id),
            )
            connection.execute(
                "INSERT INTO task_close_receipts("
                "task_id, close_request_id, expected_etag, "
                "task_admission_id, admission_sha256"
                ") VALUES (?, ?, ?, ?, ?)",
                (
                    task.task_id,
                    close_request_id,
                    expected_etag,
                    request.task_admission_id,
                    request.admission_sha256,
                ),
            )
            return _load_v2_task_closure(connection, task.task_id)

    def get_task(self, task_id: str) -> m2.TaskV2:
        task_id = _v2_resource_id(task_id, label="task")
        with self._lock, self._reader() as connection:
            return _load_v2_task_closure(connection, task_id)

    def list_tasks(self, *, project_id: str | None = None) -> list[m2.TaskV2]:
        if project_id is not None:
            project_id = _v2_resource_id(project_id, label="project")
        with self._lock, self._reader() as connection:
            if project_id is None:
                rows = connection.execute(
                    "SELECT task_id FROM tasks ORDER BY task_id LIMIT ?",
                    (_MAX_V2_TASKS + 1,),
                ).fetchall()
            else:
                rows = connection.execute(
                    "SELECT task_id FROM tasks WHERE project_id = ? ORDER BY task_id LIMIT ?",
                    (project_id, _MAX_V2_TASKS + 1),
                ).fetchall()
            if len(rows) > _MAX_V2_TASKS:
                raise ScienceTaskStoreV2Error("v2 Task inventory exceeds its bound")
            return [_load_v2_task_closure(connection, str(row["task_id"])) for row in rows]

    def get_admission(self, task_id: str) -> m2.TaskAdmissionRefV2:
        return self.get_task(task_id).admission

    def list_attempts(self, task_id: str) -> list[m2.AttemptRefV2]:
        return list(self.get_task(task_id).attempts)

    def get_attempt(self, task_id: str, attempt_id: str) -> m2.AttemptRefV2:
        task_id = _v2_resource_id(task_id, label="task")
        attempt_id = _v2_resource_id(attempt_id, label="attempt")
        with self._lock, self._reader() as connection:
            _load_v2_task_closure(connection, task_id)
            return _load_v2_attempt(
                connection,
                task_id=task_id,
                attempt_id=attempt_id,
            )

    def list_events(
        self,
        *,
        project_id: str | None = None,
        after_event_id: str | None = None,
    ) -> list[m2.EventEnvelopeV2]:
        if project_id is not None:
            project_id = _v2_resource_id(project_id, label="project")
        if after_event_id is not None:
            after_event_id = _v2_resource_id(after_event_id, label="event")
        with self._lock, self._reader() as connection:
            if project_id is None:
                bounds = connection.execute(
                    "SELECT MIN(sequence) AS minimum, MAX(sequence) AS maximum FROM events"
                ).fetchone()
            else:
                bounds = connection.execute(
                    "SELECT MIN(sequence) AS minimum, MAX(sequence) AS maximum FROM events "
                    "WHERE project_id = ?",
                    (project_id,),
                ).fetchone()
            minimum = bounds["minimum"]
            maximum = bounds["maximum"]
            if minimum is None or maximum is None:
                if after_event_id is not None:
                    raise ScienceEventCursorExpiredV2("v2 event replay cursor is not retained")
                return []
            replay_floor = max(
                int(minimum),
                int(maximum) - _MAX_V2_EVENT_REPLAY + 1,
            )
            after_sequence = replay_floor - 1
            if after_event_id is not None:
                if project_id is None:
                    cursor = connection.execute(
                        "SELECT sequence FROM events WHERE event_id = ?",
                        (after_event_id,),
                    ).fetchone()
                else:
                    cursor = connection.execute(
                        "SELECT sequence FROM events WHERE event_id = ? AND project_id = ?",
                        (after_event_id, project_id),
                    ).fetchone()
                if cursor is None:
                    raise ScienceEventCursorExpiredV2("v2 event replay cursor is not retained")
                after_sequence = int(cursor["sequence"])
                if after_sequence < replay_floor:
                    raise ScienceEventCursorExpiredV2("v2 event replay cursor is not retained")
            if project_id is None:
                rows = connection.execute(
                    "SELECT task_id, event_json FROM events WHERE sequence > ? "
                    "ORDER BY sequence LIMIT ?",
                    (after_sequence, _MAX_V2_EVENT_REPLAY + 1),
                ).fetchall()
            else:
                rows = connection.execute(
                    "SELECT task_id, event_json FROM events WHERE sequence > ? "
                    "AND project_id = ? ORDER BY sequence LIMIT ?",
                    (after_sequence, project_id, _MAX_V2_EVENT_REPLAY + 1),
                ).fetchall()
            if len(rows) > _MAX_V2_EVENT_REPLAY:
                raise ScienceTaskStoreV2Error("v2 event replay exceeds its bound")
            events = [_load_and_validate_v2_event(connection, row) for row in rows]
            for task_id in sorted(
                {str(row["task_id"]) for row in rows if row["task_id"] is not None}
            ):
                _load_and_validate_v2_task_event_history(connection, task_id)
            return events

    def list_task_events(self, task_id: str) -> list[m2.EventEnvelopeV2]:
        task_id = _v2_resource_id(task_id, label="task")
        with self._lock, self._reader() as connection:
            _load_v2_task_closure(connection, task_id)
            return _load_and_validate_v2_task_event_history(connection, task_id)

    def list_task_logs(self, task_id: str) -> list[m2.LogEntryV2]:
        """Project durable Task state and captured transcript into bounded logs."""

        task_id = _v2_resource_id(task_id, label="task")
        with self._lock, self._reader() as connection:
            task = _load_v2_task_closure(connection, task_id)
            events = _load_and_validate_v2_task_event_history(connection, task_id)
            entries: list[tuple[str, str, str]] = [
                (task.created_at, "system", "Task admitted by Core."),
            ]
            transcript_bytes = 0
            for attempt in task.attempts:
                entries.append(
                    (
                        attempt.created_at,
                        "system",
                        f"Attempt {attempt.ordinal} admitted.",
                    )
                )
                record = _load_v2_attempt_execution_optional(
                    connection,
                    attempt.attempt_id,
                )
                if record is None:
                    continue
                entries.append(
                    (
                        record.created_at,
                        "system",
                        f"Attempt {attempt.ordinal} entered managed preparation.",
                    )
                )
                entries.append(
                    (
                        record.updated_at,
                        "system",
                        _v2_attempt_log_message(attempt.ordinal, record),
                    )
                )
                if record.state != "captured":
                    continue
                result = _load_v2_captured_session_result(
                    connection,
                    attempt.attempt_id,
                )
                completed_at = (
                    record.receipt.completed_at
                    if record.receipt is not None
                    else record.updated_at
                )
                transcript_count = 0
                for trace in result.trajectory.traces:
                    for message in trace.response_messages:
                        rendered = _v2_transcript_log_message(message)
                        if rendered is None:
                            continue
                        encoded = rendered.encode("utf-8")
                        if (
                            len(entries) >= _MAX_V2_TASK_LOG_ENTRIES - 2
                            or transcript_bytes + len(encoded)
                            > _MAX_V2_TASK_LOG_TRANSCRIPT_BYTES
                        ):
                            entries.append(
                                (
                                    completed_at,
                                    "system",
                                    "Additional transcript output was omitted by the Core log bound.",
                                )
                            )
                            break
                        entries.append((completed_at, "transcript", rendered))
                        transcript_bytes += len(encoded)
                        transcript_count += 1
                    else:
                        continue
                    break
                if transcript_count == 0:
                    entries.append(
                        (
                            completed_at,
                            "system",
                            "The captured transcript contains no displayable response text.",
                        )
                    )

            for event in events:
                message = _v2_task_event_log_message(event)
                if message is not None:
                    entries.append((event.occurred_at, "system", message))
            if task.state != "admitted":
                entries.append(
                    (
                        task.updated_at,
                        "system",
                        _v2_task_state_log_message(task.state),
                    )
                )
            if len(entries) > _MAX_V2_TASK_LOG_ENTRIES:
                entries = entries[: _MAX_V2_TASK_LOG_ENTRIES - 1] + [
                    (
                        task.updated_at,
                        "system",
                        "Additional Task lifecycle output was omitted by the Core log bound.",
                    )
                ]
            return [
                m2.LogEntryV2(
                    sequence=sequence,
                    occurred_at=occurred_at,
                    stream=stream,
                    message=message,
                )
                for sequence, (occurred_at, stream, message) in enumerate(
                    entries,
                    start=1,
                )
            ]

    def ownership_counts(self) -> tuple[int, int, int]:
        with self._lock, self._reader() as connection:
            return tuple(
                int(connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
                for table in ("tasks", "task_admissions", "attempts")
            )  # type: ignore[return-value]

    def _prepare_root(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        metadata = self.root.stat(follow_symlinks=False)
        if (
            not stat.S_ISDIR(metadata.st_mode)
            or metadata.st_uid != os.geteuid()
            or stat.S_IMODE(metadata.st_mode) != 0o700
        ):
            raise ScienceTaskStoreV2Error("v2 science task root must be a private owned directory")

    def _verify_database(self) -> None:
        metadata = self.database.stat(follow_symlinks=False)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid != os.geteuid()
            or metadata.st_nlink != 1
            or stat.S_IMODE(metadata.st_mode) != 0o600
        ):
            raise ScienceTaskStoreV2Error("v2 science task database is not a private regular file")
        with self._reader() as connection:
            row = connection.execute(
                "SELECT schema_version FROM metadata WHERE singleton = 1"
            ).fetchone()
            if row is None or int(row["schema_version"]) != 2:
                raise ScienceTaskStoreV2Error("v2 science task schema identity is invalid")
            if not _v2_action_receipt_schema_preexisting(connection):
                raise ScienceTaskStoreV2Error("v2 action receipt authority schema is missing")
            receipt_migration_rows = connection.execute(
                "SELECT singleton, migration_version FROM action_receipt_migrations LIMIT 2"
            ).fetchall()
            if (
                len(receipt_migration_rows) != 1
                or int(receipt_migration_rows[0]["singleton"]) != 1
                or int(receipt_migration_rows[0]["migration_version"]) != 1
            ):
                raise ScienceTaskStoreV2Error("v2 action receipt migration authority is invalid")
            authorities = connection.execute(
                "SELECT project_id, authority_json FROM project_admission_authorities "
                "ORDER BY project_id LIMIT ?",
                (_MAX_V2_PROJECTS + 1,),
            ).fetchall()
            if len(authorities) > _MAX_V2_PROJECTS:
                raise ScienceTaskStoreV2Error(
                    "v2 project admission authority inventory exceeds its bound"
                )
            head_inventory = connection.execute(
                "SELECT project_head_id, project_id FROM project_heads "
                "ORDER BY project_head_id LIMIT ?",
                (_MAX_V2_PROJECT_HEADS + 1,),
            ).fetchall()
            if len(head_inventory) > _MAX_V2_PROJECT_HEADS:
                raise ScienceTaskStoreV2Error("v2 project-head inventory exceeds its bound")
            authority_projects = {str(item["project_id"]) for item in authorities}
            head_projects = {str(item["project_id"]) for item in head_inventory}
            if head_projects != authority_projects:
                raise ScienceTaskStoreV2Error(
                    "v2 project-head inventory has no exact project authority"
                )
            for authority_row in authorities:
                authority = _v2_authority_from_bytes(bytes(authority_row["authority_json"]))
                if (
                    authority.project_id != authority_row["project_id"]
                    or _load_v2_project_head(
                        connection,
                        authority.active_project_head.project_head_id,
                    )
                    != authority.active_project_head
                ):
                    raise ScienceTaskStoreV2Error(
                        "v2 project admission authority row is inconsistent"
                    )
                head_rows = connection.execute(
                    "SELECT project_head_id FROM project_heads WHERE project_id = ? "
                    "ORDER BY generation LIMIT ?",
                    (authority.project_id, _MAX_V2_TASKS + 2),
                ).fetchall()
                if len(head_rows) > _MAX_V2_TASKS + 1:
                    raise ScienceTaskStoreV2Error("v2 project-head history exceeds its bound")
                heads = [
                    _load_v2_project_head(connection, str(item["project_head_id"]))
                    for item in head_rows
                ]
                _validate_v2_project_head_chain(heads)
                for head in heads:
                    commit = (
                        _load_v2_successor_commit_for_project_head(
                            connection,
                            head.project_head_id,
                        )
                    )
                    if (
                        head.generation == 0
                        and commit is not None
                    ) or (
                        head.generation > 0
                        and commit is None
                    ):
                        raise ScienceTaskStoreV2Error(
                            "v2 project head lacks exact "
                            "successor receipt authority"
                        )
                if not heads or heads[-1] != authority.active_project_head:
                    raise ScienceTaskStoreV2Error(
                        "v2 project authority is not the project-head tip"
                    )
            tasks = connection.execute(
                "SELECT task_id FROM tasks ORDER BY task_id LIMIT ?",
                (_MAX_V2_TASKS + 1,),
            ).fetchall()
            if len(tasks) > _MAX_V2_TASKS:
                raise ScienceTaskStoreV2Error("v2 Task inventory exceeds its bound")
            for task_row in tasks:
                task = _load_v2_task_closure(
                    connection,
                    str(task_row["task_id"]),
                )
                close_receipt = _load_v2_task_close_receipt(
                    connection,
                    task.task_id,
                )
                legacy_close = _load_v2_legacy_task_close_exemption(
                    connection,
                    task.task_id,
                )
                if task.state == "closed":
                    if (close_receipt is None) == (legacy_close is None):
                        raise ScienceTaskStoreV2Error(
                            "closed v2 Task lacks exactly one close authority receipt"
                        )
                elif close_receipt is not None or legacy_close is not None:
                    raise ScienceTaskStoreV2Error("non-closed v2 Task exposes close authority")
            close_receipt_rows = connection.execute(
                "SELECT task_id FROM task_close_receipts ORDER BY task_id LIMIT ?",
                (_MAX_V2_TASKS + 1,),
            ).fetchall()
            if len(close_receipt_rows) > _MAX_V2_TASKS:
                raise ScienceTaskStoreV2Error("v2 Task close receipt inventory exceeds its bound")
            for close_receipt_row in close_receipt_rows:
                task_id = str(close_receipt_row["task_id"])
                receipt = _load_v2_task_close_receipt(
                    connection,
                    task_id,
                )
                task = _load_v2_task_closure(
                    connection,
                    task_id,
                )
                if (
                    receipt is None
                    or task.state != "closed"
                    or receipt.task_admission_id != task.admission.task_admission_id
                    or receipt.admission_sha256 != task.admission.admission_sha256
                ):
                    raise ScienceTaskStoreV2Error(
                        "v2 Task close receipt has no exact closed authority"
                    )
            legacy_close_rows = connection.execute(
                "SELECT task_id FROM legacy_task_close_exemptions ORDER BY task_id LIMIT ?",
                (_MAX_V2_TASKS + 1,),
            ).fetchall()
            if len(legacy_close_rows) > _MAX_V2_TASKS:
                raise ScienceTaskStoreV2Error(
                    "legacy v2 Task close exemption inventory exceeds its bound"
                )
            for legacy_close_row in legacy_close_rows:
                task_id = str(legacy_close_row["task_id"])
                exemption = _load_v2_legacy_task_close_exemption(
                    connection,
                    task_id,
                )
                task = _load_v2_task_closure(
                    connection,
                    task_id,
                )
                if (
                    exemption is None
                    or task.state != "closed"
                    or exemption.task_admission_id != task.admission.task_admission_id
                    or exemption.admission_sha256 != task.admission.admission_sha256
                ):
                    raise ScienceTaskStoreV2Error(
                        "legacy v2 Task close exemption has no exact closed authority"
                    )
            execution_rows = connection.execute(
                "SELECT attempt_id FROM attempt_executions ORDER BY attempt_id LIMIT ?",
                (_MAX_V2_TASKS * 100 + 1,),
            ).fetchall()
            if len(execution_rows) > _MAX_V2_TASKS * 100:
                raise ScienceTaskStoreV2Error("v2 Attempt execution inventory exceeds its bound")
            active_by_task: dict[str, str] = {}
            for execution_row in execution_rows:
                record = _load_v2_attempt_execution(
                    connection,
                    str(execution_row["attempt_id"]),
                )
                task = _load_v2_task_closure(connection, record.task_id)
                _validate_v2_execution_ownership(task, record)
                if record.state == "captured":
                    if (
                        record.receipt is None
                        or record.evidence is None
                        or record.successor_plan is None
                    ):
                        raise ScienceTaskStoreV2Error(
                            "captured v2 Attempt has incomplete terminal evidence"
                        )
                    _validate_v2_terminal_execution_closure(
                        connection=connection,
                        task=task,
                        attempt_id=record.attempt_id,
                        receipt=record.receipt,
                        evidence=record.evidence,
                        successor_plan=record.successor_plan,
                        terminal_result=_load_v2_captured_session_result(
                            connection,
                            record.attempt_id,
                        ),
                    )
                if record.state in {"preparing", "running", "cancelling"}:
                    if record.task_id in active_by_task:
                        raise ScienceTaskStoreV2Error(
                            "v2 Task has multiple active Attempt executions"
                        )
                    active_by_task[record.task_id] = record.attempt_id
                    if (
                        task.attempts[-1].attempt_id != record.attempt_id
                        or task.state != record.state
                    ):
                        raise ScienceTaskStoreV2Error(
                            "active v2 Attempt execution and Task state differ"
                        )
            _verify_v2_run_admission_inventory(connection)
            transition_rows = connection.execute(
                "SELECT successor_transition_id FROM successor_transitions "
                "ORDER BY successor_transition_id LIMIT ?",
                (_MAX_V2_TASKS + 1,),
            ).fetchall()
            if len(transition_rows) > _MAX_V2_TASKS:
                raise ScienceTaskStoreV2Error(
                    "v2 successor transition inventory exceeds its bound"
                )
            successor_blocked_projects: set[str] = set()
            for transition_row in transition_rows:
                transition_id = str(transition_row["successor_transition_id"])
                transition = _load_v2_successor_transition(
                    connection,
                    transition_id,
                )
                cleanup_receipt = _load_v2_successor_cleanup_receipt(
                    connection,
                    transition_id,
                )
                abandon_receipt = _load_v2_successor_abandon_receipt(
                    connection,
                    transition_id,
                )
                legacy_abandon = _load_v2_legacy_successor_abandon_exemption(
                    connection,
                    transition_id,
                )
                if transition.state == "cancelled":
                    if (abandon_receipt is None) == (legacy_abandon is None):
                        raise ScienceTaskStoreV2Error(
                            "cancelled v2 successor lacks exactly one abandon authority receipt"
                        )
                elif abandon_receipt is not None or legacy_abandon is not None:
                    raise ScienceTaskStoreV2Error(
                        "non-cancelled v2 successor exposes abandon authority"
                    )
                if transition.state == "cancelled" and cleanup_receipt is None:
                    successor = transition.transition.successor_project_head
                    authority = _load_v2_project_authority(
                        connection,
                        transition.transition.project_id,
                    )
                    if (
                        successor is None
                        or authority.active_project_head != successor
                        or authority.blockers
                        != (ScienceProjectReadinessBlockerV2.SUCCESSOR_TRANSITION,)
                    ):
                        raise ScienceTaskStoreV2Error(
                            "pending v2 successor cleanup has no exact readiness blocker"
                        )
                elif transition.state != "cancelled" and cleanup_receipt is not None:
                    raise ScienceTaskStoreV2Error(
                        "non-cancelled v2 successor exposes a cleanup receipt"
                    )
                requires_successor_blocker = transition.state in {
                    "pending",
                    "sealing_dataset",
                    "running_methods",
                    "validating",
                    "materializing",
                    "committing",
                    "failed",
                } or (transition.state == "cancelled" and cleanup_receipt is None)
                if requires_successor_blocker:
                    reference = transition.transition
                    task_admission = reference.task_admission
                    if task_admission is None:
                        raise ScienceTaskStoreV2Error(
                            "v2 successor readiness blocker has no Task admission"
                        )
                    task = _load_v2_task_closure(
                        connection,
                        task_admission.task_id,
                    )
                    authority = _load_v2_project_authority(
                        connection,
                        reference.project_id,
                    )
                    expected_active_head = (
                        reference.successor_project_head
                        if transition.state == "cancelled"
                        else reference.predecessor_project_head
                    )
                    expected_task_state = (
                        "completed"
                        if transition.state == "cancelled"
                        else (
                            "failed" if transition.state == "failed" else "waiting_for_successor"
                        )
                    )
                    if (
                        reference.project_id in successor_blocked_projects
                        or expected_active_head is None
                        or task.state != expected_task_state
                        or authority.active_project_head != expected_active_head
                        or authority.blockers
                        != (ScienceProjectReadinessBlockerV2.SUCCESSOR_TRANSITION,)
                    ):
                        raise ScienceTaskStoreV2Error(
                            "v2 successor readiness blocker has no exact transition authority"
                        )
                    successor_blocked_projects.add(reference.project_id)
            observed_successor_blocked_projects = {
                str(authority_row["project_id"])
                for authority_row in authorities
                if (
                    ScienceProjectReadinessBlockerV2.SUCCESSOR_TRANSITION
                    in _v2_authority_from_bytes(bytes(authority_row["authority_json"])).blockers
                )
            }
            if observed_successor_blocked_projects != successor_blocked_projects:
                raise ScienceTaskStoreV2Error(
                    "v2 successor readiness blocker has no exact transition authority"
                )
            cleanup_rows = connection.execute(
                "SELECT successor_transition_id FROM "
                "successor_cleanup_receipts "
                "ORDER BY successor_transition_id LIMIT ?",
                (_MAX_V2_TASKS + 1,),
            ).fetchall()
            if len(cleanup_rows) > _MAX_V2_TASKS:
                raise ScienceTaskStoreV2Error(
                    "v2 successor cleanup receipt inventory exceeds its bound"
                )
            for cleanup_row in cleanup_rows:
                transition_id = str(cleanup_row["successor_transition_id"])
                receipt = _load_v2_successor_cleanup_receipt(
                    connection,
                    transition_id,
                )
                transition = _load_v2_successor_transition(
                    connection,
                    transition_id,
                )
                if receipt is None or transition.state != "cancelled":
                    raise ScienceTaskStoreV2Error(
                        "v2 successor cleanup receipt has no exact cancelled authority"
                    )
            abandon_rows = connection.execute(
                "SELECT successor_transition_id FROM "
                "successor_abandon_receipts "
                "ORDER BY successor_transition_id LIMIT ?",
                (_MAX_V2_TASKS + 1,),
            ).fetchall()
            if len(abandon_rows) > _MAX_V2_TASKS:
                raise ScienceTaskStoreV2Error(
                    "v2 successor abandon receipt inventory exceeds its bound"
                )
            for abandon_row in abandon_rows:
                transition_id = str(abandon_row["successor_transition_id"])
                receipt = _load_v2_successor_abandon_receipt(
                    connection,
                    transition_id,
                )
                transition = _load_v2_successor_transition(
                    connection,
                    transition_id,
                )
                attempts = _load_v2_successor_transition_attempts(
                    connection,
                    transition_id,
                )
                commit = _load_v2_successor_commit(
                    connection,
                    transition_id,
                )
                if (
                    receipt is None
                    or transition.state != "cancelled"
                    or type(commit.manifest) is not AtomicEvolutionAbandonManifestV2
                    or receipt.expected_project_head_id
                    != transition.transition.predecessor_project_head.project_head_id
                    or attempts[-1].transition_attempt_id != receipt.transition_attempt_id
                    or attempts[-1].state != "failed"
                ):
                    raise ScienceTaskStoreV2Error(
                        "v2 successor abandon receipt has no exact cancelled authority"
                    )
            legacy_abandon_rows = connection.execute(
                "SELECT successor_transition_id FROM "
                "legacy_successor_abandon_exemptions "
                "ORDER BY successor_transition_id LIMIT ?",
                (_MAX_V2_TASKS + 1,),
            ).fetchall()
            if len(legacy_abandon_rows) > _MAX_V2_TASKS:
                raise ScienceTaskStoreV2Error(
                    "legacy v2 successor abandon exemption inventory exceeds its bound"
                )
            for legacy_abandon_row in legacy_abandon_rows:
                transition_id = str(legacy_abandon_row["successor_transition_id"])
                exemption = _load_v2_legacy_successor_abandon_exemption(
                    connection,
                    transition_id,
                )
                transition = _load_v2_successor_transition(
                    connection,
                    transition_id,
                )
                attempts = _load_v2_successor_transition_attempts(
                    connection,
                    transition_id,
                )
                if (
                    exemption is None
                    or transition.state != "cancelled"
                    or exemption.expected_project_head_id
                    != transition.transition.predecessor_project_head.project_head_id
                    or attempts[-1].transition_attempt_id != exemption.transition_attempt_id
                    or attempts[-1].state != "failed"
                ):
                    raise ScienceTaskStoreV2Error(
                        "legacy v2 successor abandon exemption has no exact cancelled authority"
                    )
            append_rows = connection.execute(
                "SELECT task_id, idempotency_key, request_sha256, request_json, "
                "attempt_id "
                "FROM attempt_append_requests"
            ).fetchall()
            if len(append_rows) > _MAX_V2_TASKS * 99:
                raise ScienceTaskStoreV2Error(
                    "v2 Attempt append request inventory exceeds its bound"
                )
            for append_row in append_rows:
                request_json = bytes(append_row["request_json"])
                request = _v2_model_from_bytes(
                    m2.AttemptAppendRequestV2,
                    request_json,
                )
                if hashlib.sha256(request_json).hexdigest() != append_row["request_sha256"]:
                    raise ScienceTaskStoreV2Error(
                        "v2 Attempt append request digest is inconsistent"
                    )
                attempt = _load_v2_attempt(
                    connection,
                    task_id=str(append_row["task_id"]),
                    attempt_id=str(append_row["attempt_id"]),
                )
                task = _load_v2_task_closure(connection, attempt.task_id)
                if (
                    _v2_idempotency_key(str(append_row["idempotency_key"]))
                    != append_row["idempotency_key"]
                    or request.task_admission_id != attempt.task_admission_id
                    or request.admission_sha256 != attempt.admission_sha256
                    or request.expected_next_ordinal != attempt.ordinal
                    or attempt.ordinal < 2
                    or request.expected_previous_attempt_id
                    != task.attempts[attempt.ordinal - 2].attempt_id
                ):
                    raise ScienceTaskStoreV2Error(
                        "v2 Attempt append request authority is inconsistent"
                    )
            event_rows = connection.execute(
                "SELECT sequence, event_id, event_type, project_id, task_id, "
                "event_json FROM events ORDER BY sequence LIMIT ?",
                (_MAX_V2_EVENT_ROWS + 1,),
            ).fetchall()
            if len(event_rows) > _MAX_V2_EVENT_ROWS:
                raise ScienceTaskStoreV2Error("v2 event journal exceeds its bound")
            previous_sequence: int | None = None
            for event_row in event_rows:
                event = _load_v2_event_bytes(bytes(event_row["event_json"]))
                sequence = int(event_row["sequence"])
                if (
                    event.sequence != sequence
                    or event.event_id != event_row["event_id"]
                    or event.event_id != _v2_event_id(event)
                    or event.event_type != event_row["event_type"]
                    or event.project_id != event_row["project_id"]
                    or _v2_model_bytes(event) != bytes(event_row["event_json"])
                    or (previous_sequence is not None and sequence != previous_sequence + 1)
                ):
                    raise ScienceTaskStoreV2Error("persisted v2 event journal is inconsistent")
                task_id = event_row["task_id"]
                if task_id is not None:
                    task = _load_v2_task_closure(connection, str(task_id))
                    if task.project_id != event.project_id:
                        raise ScienceTaskStoreV2Error(
                            "persisted v2 event belongs to another project"
                        )
                _validate_v2_event_authority(
                    connection,
                    event,
                    None if task_id is None else str(task_id),
                )
                previous_sequence = sequence
            for task_row in tasks:
                _load_and_validate_v2_task_event_history(
                    connection,
                    str(task_row["task_id"]),
                )

    @contextmanager
    def _transaction(self) -> Iterator[sqlite3.Connection]:
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            yield connection
            connection.execute("COMMIT")
        except BaseException:
            try:
                connection.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            raise
        finally:
            connection.close()

    @contextmanager
    def _reader(self) -> Iterator[sqlite3.Connection]:
        connection = self._connect()
        try:
            yield connection
        finally:
            connection.close()

    def _connect(self) -> sqlite3.Connection:
        if self._closed:
            raise ScienceTaskStoreV2Error("v2 science task store is closed")
        connection = sqlite3.connect(self.database, timeout=10.0)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 10000")
        return connection


def _execute_v2_schema(connection: sqlite3.Connection) -> None:
    statement = ""
    for line in _V2_SCHEMA.splitlines(keepends=True):
        statement += line
        if not sqlite3.complete_statement(statement):
            continue
        sql = statement.strip()
        if sql:
            connection.execute(sql)
        statement = ""
    if statement.strip():
        raise ScienceTaskStoreV2Error("v2 science task schema script is incomplete")


def _v2_action_receipt_schema_rows(
    connection: sqlite3.Connection,
) -> tuple[tuple[object, ...], ...]:
    placeholders = ", ".join("?" for _ in _V2_ACTION_RECEIPT_SCHEMA_TABLES)
    rows = connection.execute(
        "SELECT type, name, tbl_name, sql FROM sqlite_schema "
        f"WHERE name IN ({placeholders}) "
        f"OR tbl_name IN ({placeholders}) "
        "ORDER BY type, name",
        (
            *sorted(_V2_ACTION_RECEIPT_SCHEMA_TABLES),
            *sorted(_V2_ACTION_RECEIPT_SCHEMA_TABLES),
        ),
    ).fetchall()
    return tuple(tuple(row) for row in rows)


def _expected_v2_action_receipt_schema_rows() -> tuple[tuple[object, ...], ...]:
    connection = sqlite3.connect(":memory:")
    try:
        connection.executescript(_V2_SCHEMA)
        return _v2_action_receipt_schema_rows(connection)
    finally:
        connection.close()


def _v2_action_receipt_schema_preexisting(
    connection: sqlite3.Connection,
) -> bool:
    rows = _v2_action_receipt_schema_rows(connection)
    present = {str(row[1]) for row in rows if row[0] == "table"}
    if present and present != _V2_ACTION_RECEIPT_SCHEMA_TABLES:
        raise ScienceTaskStoreV2Error(
            "v2 action receipt authority schema is only partially present"
        )
    if present and rows != _expected_v2_action_receipt_schema_rows():
        raise ScienceTaskStoreV2Error("v2 action receipt authority schema fingerprint is invalid")
    return bool(present)


def _migrate_v2_action_receipt_authority(
    connection: sqlite3.Connection,
    *,
    schema_preexisting: bool,
) -> None:
    marker_rows = connection.execute(
        "SELECT singleton, migration_version FROM action_receipt_migrations LIMIT 2"
    ).fetchall()
    if schema_preexisting:
        if (
            len(marker_rows) != 1
            or int(marker_rows[0]["singleton"]) != 1
            or int(marker_rows[0]["migration_version"]) != 1
        ):
            raise ScienceTaskStoreV2Error("v2 action receipt migration authority is invalid")
        return
    if marker_rows:
        raise ScienceTaskStoreV2Error("new v2 action receipt schema exposes a migration marker")

    task_rows = connection.execute(
        "SELECT task_id FROM tasks WHERE closed = 1 ORDER BY task_id LIMIT ?",
        (_MAX_V2_TASKS + 1,),
    ).fetchall()
    if len(task_rows) > _MAX_V2_TASKS:
        raise ScienceTaskStoreV2Error("legacy v2 Task receipt migration exceeds its bound")
    for task_row in task_rows:
        task = _load_v2_task_closure(
            connection,
            str(task_row["task_id"]),
        )
        if task.state != "closed":
            continue
        if (
            _load_v2_task_close_receipt(
                connection,
                task.task_id,
            )
            is not None
        ):
            raise ScienceTaskStoreV2Error("legacy v2 Task unexpectedly exposes a close receipt")
        connection.execute(
            "INSERT INTO legacy_task_close_exemptions("
            "task_id, task_admission_id, admission_sha256"
            ") VALUES (?, ?, ?)",
            (
                task.task_id,
                task.admission.task_admission_id,
                task.admission.admission_sha256,
            ),
        )

    transition_rows = connection.execute(
        "SELECT successor_transition_id FROM "
        "successor_transitions ORDER BY "
        "successor_transition_id LIMIT ?",
        (_MAX_V2_TASKS + 1,),
    ).fetchall()
    if len(transition_rows) > _MAX_V2_TASKS:
        raise ScienceTaskStoreV2Error("legacy v2 successor receipt migration exceeds its bound")
    for transition_row in transition_rows:
        transition_id = str(transition_row["successor_transition_id"])
        transition = _load_v2_successor_transition(
            connection,
            transition_id,
        )
        if transition.state != "cancelled":
            continue
        if (
            _load_v2_successor_abandon_receipt(
                connection,
                transition_id,
            )
            is not None
        ):
            raise ScienceTaskStoreV2Error(
                "legacy v2 successor unexpectedly exposes an abandon receipt"
            )
        attempts = _load_v2_successor_transition_attempts(
            connection,
            transition_id,
        )
        connection.execute(
            "INSERT INTO "
            "legacy_successor_abandon_exemptions("
            "successor_transition_id, "
            "expected_project_head_id, transition_attempt_id"
            ") VALUES (?, ?, ?)",
            (
                transition_id,
                transition.transition.predecessor_project_head.project_head_id,
                attempts[-1].transition_attempt_id,
            ),
        )

    connection.execute(
        "INSERT INTO action_receipt_migrations(singleton, migration_version) VALUES (1, 1)"
    )


def _migrate_v2_attempt_execution_results(connection: sqlite3.Connection) -> None:
    columns = {
        str(row["name"])
        for row in connection.execute("PRAGMA table_info(attempt_executions)").fetchall()
    }
    expected = {"session_result_sha256", "session_result_json"}
    missing = expected.difference(columns)
    if not missing:
        return
    if missing != expected:
        raise ScienceTaskStoreV2Error("v2 Attempt result schema is only partially present")
    connection.execute("ALTER TABLE attempt_executions ADD COLUMN session_result_sha256 TEXT")
    connection.execute("ALTER TABLE attempt_executions ADD COLUMN session_result_json BLOB")


def _backfill_v2_successor_transition_attempts(
    connection: sqlite3.Connection,
) -> None:
    rows = connection.execute(
        "SELECT successor_transition_id, transition_json "
        "FROM successor_transitions ORDER BY successor_transition_id"
    ).fetchall()
    for row in rows:
        transition_id = str(row["successor_transition_id"])
        count = int(
            connection.execute(
                "SELECT COUNT(*) FROM successor_transition_attempts "
                "WHERE successor_transition_id = ?",
                (transition_id,),
            ).fetchone()[0]
        )
        transition = _v2_model_from_bytes(
            m2.SuccessorTransitionV2,
            bytes(row["transition_json"]),
        )
        if not count:
            digest = hashlib.sha256(f"{transition_id}\0initial".encode("utf-8")).hexdigest()
            error = transition.error
            if transition.state in {"failed", "cancelled", "superseded"}:
                state = "failed"
                if error is None:
                    error = m2.ApiErrorV2(
                        request_id=f"successor-error-{digest[:24]}",
                        code="successor_transition_not_committed",
                        http_status=503,
                        message=("Core preserved a successor attempt that did not commit."),
                        category="transition",
                        retryable=False,
                        repair_action="repair",
                        next_action=("Inspect the preserved transition before continuing."),
                    )
            elif transition.state == "committed":
                state = "committed"
                error = None
            else:
                state = "running"
                error = None
            attempt = ScienceSuccessorTransitionAttemptV2(
                transition_attempt_id=(f"successor-attempt-{digest[:32]}"),
                successor_transition_id=transition_id,
                ordinal=1,
                retry_request_id="initial",
                state=state,
                error=error,
                created_at=transition.created_at,
                updated_at=transition.updated_at,
            )
            connection.execute(
                "INSERT INTO successor_transition_attempts("
                "transition_attempt_id, successor_transition_id, ordinal, "
                "retry_request_id, attempt_json) VALUES (?, ?, ?, ?, ?)",
                (
                    attempt.transition_attempt_id,
                    transition_id,
                    attempt.ordinal,
                    attempt.retry_request_id,
                    _v2_model_bytes(attempt),
                ),
            )
        attempts = _load_v2_successor_transition_attempts(
            connection,
            transition_id,
        )
        latest = attempts[-1]
        admission = transition.transition.task_admission
        dataset_event = (
            None
            if admission is None
            else _load_v2_dataset_event_for_task_unchecked(
                connection,
                admission.task_id,
            )
        )
        commit_row = connection.execute(
            "SELECT manifest_sha256 FROM successor_commits WHERE successor_transition_id = ?",
            (transition_id,),
        ).fetchone()
        changes: dict[str, object] = {}
        if dataset_event is not None and latest.dataset_id is None:
            changes.update(
                dataset_id=dataset_event.dataset_id,
                dataset_sha256=dataset_event.dataset_sha256,
            )
        if (
            latest.state == "committed"
            and commit_row is not None
            and latest.commit_manifest_sha256 is None
        ):
            changes["commit_manifest_sha256"] = str(commit_row["manifest_sha256"])
        if changes:
            _store_v2_successor_transition_attempt(
                connection,
                _replace_v2_successor_transition_attempt(
                    latest,
                    **changes,
                ),
            )


def _backfill_v2_project_heads(connection: sqlite3.Connection) -> None:
    rows = connection.execute(
        "SELECT authority_json FROM project_admission_authorities ORDER BY project_id"
    ).fetchall()
    for row in rows:
        authority = _v2_authority_from_bytes(bytes(row["authority_json"]))
        _store_v2_project_head(connection, authority.active_project_head)


def _load_guarded_v2_blob(
    connection: sqlite3.Connection,
    *,
    table: str,
    key_column: str,
    key: str,
    blob_column: str,
    label: str,
) -> bytes | None:
    metadata = connection.execute(
        f"SELECT length(CAST({blob_column} AS BLOB)) AS byte_size "
        f"FROM {table} WHERE {key_column} = ?",
        (key,),
    ).fetchone()
    if metadata is None:
        return None
    byte_size = int(metadata["byte_size"])
    if byte_size <= 0 or byte_size > _MAX_DOCUMENT_BYTES:
        raise ScienceTaskStoreV2Error(f"{label} exceeds its byte bound")
    row = connection.execute(
        f"SELECT CASE WHEN length(CAST({blob_column} AS BLOB)) = ? "
        f"AND length(CAST({blob_column} AS BLOB)) <= ? "
        f"THEN {blob_column} END AS payload FROM {table} "
        f"WHERE {key_column} = ?",
        (byte_size, _MAX_DOCUMENT_BYTES, key),
    ).fetchone()
    if row is None or row["payload"] is None:
        raise ScienceTaskStoreV2Error(f"{label} changed during guarded read")
    payload = bytes(row["payload"])
    if len(payload) != byte_size:
        raise ScienceTaskStoreV2Error(f"{label} byte length changed during guarded read")
    return payload


def _migrate_v2_successor_commit_authority(
    connection: sqlite3.Connection,
) -> None:
    columns = {
        str(row["name"])
        for row in connection.execute("PRAGMA table_info(successor_commits)").fetchall()
    }
    provenance_columns = {
        "composition_semantics_version",
        "legacy_manifest_sha256",
    }
    present_provenance = provenance_columns.intersection(columns)
    if present_provenance and present_provenance != provenance_columns:
        raise ScienceTaskStoreV2Error("v2 successor commit migration provenance is partial")
    upgrading_legacy_schema = not present_provenance
    if "successor_project_head_id" not in columns:
        connection.execute(
            "ALTER TABLE successor_commits ADD COLUMN successor_project_head_id TEXT"
        )
    if upgrading_legacy_schema:
        connection.execute(
            "ALTER TABLE successor_commits "
            "ADD COLUMN composition_semantics_version INTEGER "
            "CHECK (composition_semantics_version IN (1, 2))"
        )
        connection.execute(
            "ALTER TABLE successor_commits "
            "ADD COLUMN legacy_manifest_sha256 TEXT "
            "CHECK (legacy_manifest_sha256 IS NULL OR "
            "length(legacy_manifest_sha256) = 64)"
        )
    inventory = connection.execute(
        "SELECT successor_transition_id, manifest_sha256, "
        "successor_project_head_id, composition_semantics_version, "
        "legacy_manifest_sha256, "
        "length(CAST(commit_json AS BLOB)) AS byte_size "
        "FROM successor_commits ORDER BY successor_transition_id LIMIT ?",
        (_MAX_V2_TASKS + 1,),
    ).fetchall()
    if len(inventory) > _MAX_V2_TASKS:
        raise ScienceTaskStoreV2Error("v2 successor commit inventory exceeds its bound")
    aggregate_bytes = 0
    ordered: list[
        tuple[
            str,
            int,
            str,
            AtomicSuccessorCommitV2,
            str | None,
            int | None,
            str | None,
        ]
    ] = []
    for row in inventory:
        byte_size = int(row["byte_size"])
        aggregate_bytes += byte_size
        if (
            byte_size <= 0
            or byte_size > _MAX_DOCUMENT_BYTES
            or aggregate_bytes > _MAX_V2_SUCCESSOR_COMMIT_AGGREGATE_BYTES
        ):
            raise ScienceTaskStoreV2Error("v2 successor commit inventory exceeds its byte bound")
        transition_id = str(row["successor_transition_id"])
        payload = _load_guarded_v2_blob(
            connection,
            table="successor_commits",
            key_column="successor_transition_id",
            key=transition_id,
            blob_column="commit_json",
            label="v2 successor commit",
        )
        if payload is None:
            raise ScienceTaskStoreV2Error("v2 successor commit disappeared during migration")
        commit = _v2_model_from_bytes(
            AtomicSuccessorCommitV2,
            payload,
        )
        if (
            commit.manifest_sha256 != row["manifest_sha256"]
            or commit.manifest.successor_transition_id != transition_id
        ):
            raise ScienceTaskStoreV2Error("v2 successor commit row is inconsistent")
        ordered.append(
            (
                commit.manifest.project_id,
                commit.manifest.successor_generation,
                transition_id,
                commit,
                (
                    None
                    if row["successor_project_head_id"] is None
                    else str(row["successor_project_head_id"])
                ),
                (
                    None
                    if row["composition_semantics_version"] is None
                    else int(row["composition_semantics_version"])
                ),
                (
                    None
                    if row["legacy_manifest_sha256"] is None
                    else str(row["legacy_manifest_sha256"])
                ),
            )
        )
    ordered.sort(key=lambda item: (item[0], item[1], item[2]))
    composition_by_head: dict[
        str,
        tuple[SuccessorArtifactContributionV2, ...],
    ] = {}
    for (
        _project_id,
        _generation,
        transition_id,
        commit,
        indexed_head_id,
        stored_semantics,
        stored_legacy_manifest_sha256,
    ) in ordered:
        manifest = commit.manifest
        if upgrading_legacy_schema:
            semantics_version = (
                1
                if (
                    type(manifest) is AtomicSuccessorManifestV2
                    and manifest.runtime_context_source == "materialized_new"
                    and not manifest.artifacts
                )
                else 2
            )
            legacy_manifest_sha256 = commit.manifest_sha256 if semantics_version == 1 else None
        else:
            semantics_version = stored_semantics
            legacy_manifest_sha256 = stored_legacy_manifest_sha256
            if (
                semantics_version not in {1, 2}
                or (
                    semantics_version == 1
                    and (
                        legacy_manifest_sha256 is None
                        or type(manifest) is not AtomicSuccessorManifestV2
                    )
                )
                or (semantics_version == 2 and legacy_manifest_sha256 is not None)
            ):
                raise ScienceTaskStoreV2Error(
                    "v2 successor commit migration provenance is invalid"
                )
        successor_head = _load_v2_project_head(
            connection,
            manifest.successor_project_head_id,
        )
        predecessor_head = _load_v2_project_head(
            connection,
            manifest.predecessor_project_head_id,
        )
        if indexed_head_id is not None and indexed_head_id != manifest.successor_project_head_id:
            raise ScienceTaskStoreV2Error("v2 successor commit project-head index is inconsistent")
        predecessor_artifacts = composition_by_head.get(manifest.predecessor_project_head_id)
        if predecessor_artifacts is None:
            if (
                predecessor_head.generation != 0
                or predecessor_head.evolution_revision.artifact_count != 0
            ):
                raise ScienceTaskStoreV2Error(
                    "v2 successor migration lacks predecessor composition"
                )
            predecessor_artifacts = ()
        if len(predecessor_artifacts) != predecessor_head.evolution_revision.artifact_count:
            raise ScienceTaskStoreV2Error("v2 predecessor artifact composition is incomplete")
        artifacts = manifest.artifacts
        if not artifacts and manifest.method_artifact_ids and semantics_version != 1:
            raise ScienceTaskStoreV2Error("canonical v2 successor lost typed artifact provenance")
        if not artifacts and manifest.method_artifact_ids and semantics_version == 1:
            if type(manifest) is AtomicEvolutionAbandonManifestV2:
                artifacts = tuple(
                    item.model_copy(update={"origin": "inherited"})
                    for item in predecessor_artifacts
                )
                if tuple(item.artifact_id for item in artifacts) != manifest.method_artifact_ids:
                    raise ScienceTaskStoreV2Error("legacy evolution abandon artifacts changed")
            else:
                plan_payload = _load_guarded_v2_blob(
                    connection,
                    table="successor_transitions",
                    key_column="successor_transition_id",
                    key=transition_id,
                    blob_column="plan_json",
                    label="legacy v2 successor plan",
                )
                if plan_payload is None:
                    raise ScienceTaskStoreV2Error("legacy v2 successor has no immutable plan")
                plan = _v2_model_from_bytes(
                    ScienceSuccessorPlanV2,
                    plan_payload,
                )
                enabled = {item.target_id: item for item in plan.enabled_methods}
                predecessor_by_target = {item.target_id: item for item in predecessor_artifacts}
                full_targets = tuple(sorted(set(predecessor_by_target).union(enabled)))
                if len(manifest.method_artifact_ids) == len(full_targets):
                    target_ids = full_targets
                elif len(manifest.method_artifact_ids) == len(enabled):
                    target_ids = tuple(sorted(enabled))
                else:
                    raise ScienceTaskStoreV2Error("legacy v2 successor artifacts cannot be typed")
                migrated_artifacts: list[SuccessorArtifactContributionV2] = []
                for target_id, artifact_id in zip(
                    target_ids,
                    manifest.method_artifact_ids,
                    strict=True,
                ):
                    method = enabled.get(target_id)
                    if method is not None:
                        migrated_artifacts.append(
                            SuccessorArtifactContributionV2(
                                target_id=target_id,
                                artifact_id=artifact_id,
                                artifact_type=(method.output_artifact_type),
                                owner_successor_transition_id=(transition_id),
                                origin="produced",
                            )
                        )
                        continue
                    inherited = predecessor_by_target.get(target_id)
                    if inherited is None or inherited.artifact_id != artifact_id:
                        raise ScienceTaskStoreV2Error("legacy inherited artifact identity changed")
                    migrated_artifacts.append(inherited.model_copy(update={"origin": "inherited"}))
                artifacts = tuple(migrated_artifacts)
        if (
            tuple(item.artifact_id for item in artifacts) != manifest.method_artifact_ids
            or len(artifacts) != successor_head.evolution_revision.artifact_count
        ):
            raise ScienceTaskStoreV2Error("v2 successor artifact migration is incomplete")
        migrated_manifest = type(manifest).model_validate(
            manifest.model_copy(update={"artifacts": artifacts}).model_dump(mode="python")
        )
        if semantics_version == 1:
            legacy_manifest = AtomicSuccessorManifestV2.model_validate(
                migrated_manifest.model_copy(
                    update={"artifacts": (), "task_artifacts": ()}
                ).model_dump(mode="python")
            )
            if legacy_manifest_sha256 != atomic_successor_manifest_sha256(legacy_manifest):
                raise ScienceTaskStoreV2Error("legacy v2 successor provenance digest differs")
        migrated_commit = AtomicSuccessorCommitV2(
            manifest_sha256=atomic_successor_manifest_sha256(migrated_manifest),
            manifest=migrated_manifest,
        )
        if migrated_commit.manifest_sha256 != commit.manifest_sha256:
            attempt_row = connection.execute(
                "SELECT transition_attempt_id FROM "
                "successor_transition_attempts "
                "WHERE successor_transition_id = ? "
                "ORDER BY ordinal DESC LIMIT 1",
                (transition_id,),
            ).fetchone()
            if attempt_row is None:
                raise ScienceTaskStoreV2Error("migrated v2 successor has no transition attempt")
            attempt_id = str(attempt_row["transition_attempt_id"])
            attempt_payload = _load_guarded_v2_blob(
                connection,
                table="successor_transition_attempts",
                key_column="transition_attempt_id",
                key=attempt_id,
                blob_column="attempt_json",
                label="legacy v2 successor attempt",
            )
            if attempt_payload is None:
                raise ScienceTaskStoreV2Error("migrated v2 successor attempt disappeared")
            attempt = _v2_model_from_bytes(
                ScienceSuccessorTransitionAttemptV2,
                attempt_payload,
            )
            if type(migrated_manifest) is AtomicSuccessorManifestV2:
                if (
                    attempt.state != "committed"
                    or attempt.commit_manifest_sha256 != commit.manifest_sha256
                ):
                    raise ScienceTaskStoreV2Error("legacy v2 successor attempt receipt differs")
            elif attempt.commit_manifest_sha256 is not None:
                raise ScienceTaskStoreV2Error("legacy v2 abandon attempt exposes a commit receipt")
        connection.execute(
            "UPDATE successor_commits SET "
            "successor_project_head_id = ?, "
            "composition_semantics_version = ?, "
            "legacy_manifest_sha256 = ?, manifest_sha256 = ?, "
            "commit_json = ? WHERE successor_transition_id = ?",
            (
                migrated_manifest.successor_project_head_id,
                semantics_version,
                legacy_manifest_sha256,
                migrated_commit.manifest_sha256,
                _v2_model_bytes(migrated_commit),
                transition_id,
            ),
        )
        composition_by_head[migrated_manifest.successor_project_head_id] = (
            migrated_manifest.artifacts
        )
    connection.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS "
        "successor_commits_project_head_idx "
        "ON successor_commits(successor_project_head_id)"
    )
    if int(
        connection.execute(
            "SELECT COUNT(*) FROM successor_commits "
            "WHERE successor_project_head_id IS NULL "
            "OR composition_semantics_version IS NULL "
            "OR (composition_semantics_version = 1 "
            "AND legacy_manifest_sha256 IS NULL) "
            "OR (composition_semantics_version = 2 "
            "AND legacy_manifest_sha256 IS NOT NULL)"
        ).fetchone()[0]
    ):
        raise ScienceTaskStoreV2Error("v2 successor commit project-head index is incomplete")


def _load_v2_project_authority(
    connection: sqlite3.Connection,
    project_id: str,
) -> ScienceProjectAdmissionAuthorityV2:
    row = connection.execute(
        "SELECT authority_json FROM project_admission_authorities WHERE project_id = ?",
        (project_id,),
    ).fetchone()
    if row is None:
        raise ScienceTaskNotFoundV2("v2 project admission authority was not found")
    return _v2_authority_from_bytes(bytes(row["authority_json"]))


def _store_v2_project_head(
    connection: sqlite3.Connection,
    head: m2.ProjectHeadRefV2,
) -> None:
    head = _validate_v2_model(m2.ProjectHeadRefV2, head)
    payload = _v2_model_bytes(head)
    existing = connection.execute(
        "SELECT project_id, generation, predecessor_project_head_id, "
        "manifest_sha256, head_json FROM project_heads WHERE project_head_id = ?",
        (head.project_head_id,),
    ).fetchone()
    if existing is not None:
        if (
            existing["project_id"] != head.project_id
            or int(existing["generation"]) != head.generation
            or existing["predecessor_project_head_id"] != head.predecessor_project_head_id
            or existing["manifest_sha256"] != head.manifest_sha256
            or bytes(existing["head_json"]) != payload
        ):
            raise ScienceTaskStoreV2Error(
                "v2 project-head identity was reused with different content"
            )
        return
    if int(connection.execute("SELECT COUNT(*) FROM project_heads").fetchone()[0]) >= (
        _MAX_V2_PROJECT_HEADS
    ):
        raise ScienceTaskConflictV2("v2 project-head capacity is exhausted")
    same_generation = connection.execute(
        "SELECT 1 FROM project_heads WHERE project_id = ? AND generation = ?",
        (head.project_id, head.generation),
    ).fetchone()
    if same_generation is not None:
        raise ScienceTaskConflictV2("v2 project already has a different head at this generation")
    if head.generation == 0:
        if head.predecessor_project_head_id is not None:
            raise ScienceTaskStoreV2Error("v2 genesis project head has a predecessor")
    else:
        if head.predecessor_project_head_id is None:
            raise ScienceTaskStoreV2Error("v2 successor project head lacks a predecessor")
        predecessor = _load_v2_project_head(
            connection,
            head.predecessor_project_head_id,
        )
        if (
            predecessor.project_id != head.project_id
            or head.generation != predecessor.generation + 1
        ):
            raise ScienceTaskStoreV2Error("v2 project-head predecessor is not adjacent")
    connection.execute(
        "INSERT INTO project_heads(project_head_id, project_id, generation, "
        "predecessor_project_head_id, manifest_sha256, head_json) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (
            head.project_head_id,
            head.project_id,
            head.generation,
            head.predecessor_project_head_id,
            head.manifest_sha256,
            payload,
        ),
    )


def _load_v2_project_head(
    connection: sqlite3.Connection,
    project_head_id: str,
) -> m2.ProjectHeadRefV2:
    row = connection.execute(
        "SELECT project_id, generation, predecessor_project_head_id, "
        "manifest_sha256, head_json FROM project_heads WHERE project_head_id = ?",
        (project_head_id,),
    ).fetchone()
    if row is None:
        raise ScienceTaskStoreV2Error("persisted v2 project head is missing")
    head = _v2_model_from_bytes(m2.ProjectHeadRefV2, bytes(row["head_json"]))
    if (
        head.project_head_id != project_head_id
        or head.project_id != row["project_id"]
        or head.generation != int(row["generation"])
        or head.predecessor_project_head_id != row["predecessor_project_head_id"]
        or head.manifest_sha256 != row["manifest_sha256"]
    ):
        raise ScienceTaskStoreV2Error("persisted v2 project-head row is inconsistent")
    return head


def _validate_v2_project_head_chain(heads: Sequence[m2.ProjectHeadRefV2]) -> None:
    for index, head in enumerate(heads):
        if head.generation != index:
            raise ScienceTaskStoreV2Error("v2 project-head generations are not contiguous")
        if index == 0:
            if head.predecessor_project_head_id is not None:
                raise ScienceTaskStoreV2Error("v2 project-head history does not begin at genesis")
        elif head.predecessor_project_head_id != heads[index - 1].project_head_id:
            raise ScienceTaskStoreV2Error("v2 project-head chain is not adjacent")


def _replace_v2_successor_transition(
    current: m2.SuccessorTransitionV2,
    **changes: object,
) -> m2.SuccessorTransitionV2:
    data = current.model_dump(mode="python")
    data.update(changes)
    return m2.SuccessorTransitionV2.model_validate(data)


def _load_v2_successor_transition(
    connection: sqlite3.Connection,
    successor_transition_id: str,
) -> m2.SuccessorTransitionV2:
    row = connection.execute(
        "SELECT project_id, task_id, plan_sha256, plan_json, transition_json "
        "FROM successor_transitions WHERE successor_transition_id = ?",
        (successor_transition_id,),
    ).fetchone()
    if row is None:
        raise ScienceTaskNotFoundV2("v2 successor transition was not found")
    plan = _v2_model_from_bytes(ScienceSuccessorPlanV2, bytes(row["plan_json"]))
    transition = _v2_model_from_bytes(
        m2.SuccessorTransitionV2,
        bytes(row["transition_json"]),
    )
    reference = transition.transition
    if (
        reference.successor_transition_id != successor_transition_id
        or reference.project_id != row["project_id"]
        or plan.project_id != row["project_id"]
        or plan.task_id != row["task_id"]
        or reference.task_admission is None
        or reference.task_admission.task_id != row["task_id"]
        or reference.plan_sha256 != row["plan_sha256"]
        or science_successor_plan_sha256(plan) != row["plan_sha256"]
    ):
        raise ScienceTaskStoreV2Error("persisted v2 successor transition row is inconsistent")
    task = _load_v2_task_closure(connection, str(row["task_id"]))
    if task.successor_transition != reference or task.authoritative_attempt_id != (
        None if reference.accepted_attempt is None else reference.accepted_attempt.attempt_id
    ):
        raise ScienceTaskStoreV2Error(
            "persisted v2 successor transition ownership is inconsistent"
        )
    commit_row = connection.execute(
        "SELECT composition_semantics_version, "
        "legacy_manifest_sha256, manifest_sha256, commit_json "
        "FROM successor_commits "
        "WHERE successor_transition_id = ?",
        (successor_transition_id,),
    ).fetchone()
    if transition.state in {"committed", "cancelled"}:
        if commit_row is None or reference.successor_project_head is None:
            raise ScienceTaskStoreV2Error("published v2 successor transition is incomplete")
        commit = _v2_model_from_bytes(
            AtomicSuccessorCommitV2,
            bytes(commit_row["commit_json"]),
        )
        if commit.manifest_sha256 != commit_row["manifest_sha256"]:
            raise ScienceTaskStoreV2Error("persisted v2 successor commit digest is inconsistent")
        valid_receipt_kind = (
            transition.state == "committed" and type(commit.manifest) is AtomicSuccessorManifestV2
        ) or (
            transition.state == "cancelled"
            and type(commit.manifest) is AtomicEvolutionAbandonManifestV2
        )
        if not valid_receipt_kind:
            raise ScienceTaskStoreV2Error(
                "v2 successor terminal state and publication receipt differ"
            )
        _validate_v2_successor_commit_closure(
            connection=connection,
            task=task,
            transition=transition,
            successor=reference.successor_project_head,
            commit=commit,
            publication_timestamp=transition.updated_at,
        )
    elif commit_row is not None or reference.successor_project_head is not None:
        raise ScienceTaskStoreV2Error("noncommitted v2 successor transition exposes a successor")
    attempts = _load_v2_successor_transition_attempts(
        connection,
        successor_transition_id,
    )
    latest = attempts[-1]
    if any(attempt.state != "failed" for attempt in attempts[:-1]):
        raise ScienceTaskStoreV2Error("v2 successor transition has a non-final terminal attempt")
    if transition.state == "failed":
        attempt_matches = latest.state == "failed" and latest.error == transition.error
    elif transition.state == "committed":
        attempt_matches = latest.state == "committed"
    elif transition.state == "cancelled":
        attempt_matches = latest.state == "failed"
    else:
        attempt_matches = latest.state == "running"
    if not attempt_matches:
        raise ScienceTaskStoreV2Error("v2 successor transition and latest attempt state differ")
    dataset_event = _load_v2_dataset_event_for_task_unchecked(
        connection,
        str(row["task_id"]),
    )
    for transition_attempt in attempts:
        if transition_attempt.dataset_id is None:
            continue
        if (
            dataset_event is None
            or transition_attempt.dataset_id != dataset_event.dataset_id
            or transition_attempt.dataset_sha256 != dataset_event.dataset_sha256
        ):
            raise ScienceTaskStoreV2Error("v2 successor attempt dataset receipt is inconsistent")
    if dataset_event is not None and (
        latest.dataset_id != dataset_event.dataset_id
        or latest.dataset_sha256 != dataset_event.dataset_sha256
    ):
        raise ScienceTaskStoreV2Error("latest v2 successor attempt lacks sealed dataset authority")
    if transition.state == "committed":
        assert commit_row is not None
        semantics_version = _v2_successor_commit_semantics(
            connection,
            commit.manifest,
        )
        expected_attempt_receipt = (
            str(commit_row["legacy_manifest_sha256"])
            if semantics_version == 1
            else str(commit_row["manifest_sha256"])
        )
        if latest.commit_manifest_sha256 != expected_attempt_receipt:
            raise ScienceTaskStoreV2Error(
                "committed v2 successor legacy provenance receipt is inconsistent"
            )
    elif latest.commit_manifest_sha256 is not None:
        raise ScienceTaskStoreV2Error("noncommitted v2 successor attempt exposes a commit receipt")
    return transition


def _load_v2_successor_transition_attempts(
    connection: sqlite3.Connection,
    successor_transition_id: str,
) -> list[ScienceSuccessorTransitionAttemptV2]:
    rows = connection.execute(
        "SELECT transition_attempt_id, ordinal, retry_request_id, attempt_json "
        "FROM successor_transition_attempts "
        "WHERE successor_transition_id = ? ORDER BY ordinal LIMIT ?",
        (
            successor_transition_id,
            _MAX_V2_SUCCESSOR_ATTEMPTS_PER_TRANSITION + 1,
        ),
    ).fetchall()
    if not rows:
        raise ScienceTaskStoreV2Error("v2 successor transition has no execution attempt")
    if len(rows) > _MAX_V2_SUCCESSOR_ATTEMPTS_PER_TRANSITION:
        raise ScienceTaskStoreV2Error(
            "v2 successor transition attempt inventory exceeds its bound"
        )
    attempts = [
        _v2_model_from_bytes(
            ScienceSuccessorTransitionAttemptV2,
            bytes(row["attempt_json"]),
        )
        for row in rows
    ]
    for expected_ordinal, (row, attempt) in enumerate(
        zip(rows, attempts, strict=True),
        start=1,
    ):
        if (
            attempt.successor_transition_id != successor_transition_id
            or attempt.transition_attempt_id != row["transition_attempt_id"]
            or attempt.ordinal != expected_ordinal
            or int(row["ordinal"]) != expected_ordinal
            or attempt.retry_request_id != row["retry_request_id"]
        ):
            raise ScienceTaskStoreV2Error(
                "persisted v2 successor transition attempt is inconsistent"
            )
    if attempts[0].retry_request_id != "initial":
        raise ScienceTaskStoreV2Error(
            "v2 successor transition lacks its initial execution attempt"
        )
    if len({attempt.retry_request_id for attempt in attempts}) != len(attempts):
        raise ScienceTaskStoreV2Error("v2 successor transition reuses a retry request identity")
    return attempts


def _require_v2_successor_transition_attempt(
    connection: sqlite3.Connection,
    *,
    successor_transition_id: str,
    expected_transition_attempt_id: str,
    expected_state: str,
) -> ScienceSuccessorTransitionAttemptV2:
    attempts = _load_v2_successor_transition_attempts(
        connection,
        successor_transition_id,
    )
    latest = attempts[-1]
    if (
        latest.transition_attempt_id != expected_transition_attempt_id
        or latest.state != expected_state
    ):
        raise ScienceTaskPreconditionFailedV2("v2 successor transition execution attempt changed")
    return latest


def _replace_v2_successor_transition_attempt(
    attempt: ScienceSuccessorTransitionAttemptV2,
    **changes: object,
) -> ScienceSuccessorTransitionAttemptV2:
    data = attempt.model_dump(mode="python")
    data.update(changes)
    return ScienceSuccessorTransitionAttemptV2.model_validate(data)


def _store_v2_successor_transition_attempt(
    connection: sqlite3.Connection,
    attempt: ScienceSuccessorTransitionAttemptV2,
) -> None:
    cursor = connection.execute(
        "UPDATE successor_transition_attempts SET attempt_json = ? "
        "WHERE transition_attempt_id = ? AND successor_transition_id = ? "
        "AND ordinal = ? AND retry_request_id = ?",
        (
            _v2_model_bytes(attempt),
            attempt.transition_attempt_id,
            attempt.successor_transition_id,
            attempt.ordinal,
            attempt.retry_request_id,
        ),
    )
    if cursor.rowcount != 1:
        raise ScienceTaskStoreV2Error("v2 successor transition attempt identity changed")


def _load_v2_successor_commit(
    connection: sqlite3.Connection,
    successor_transition_id: str,
) -> AtomicSuccessorCommitV2:
    row = connection.execute(
        "SELECT manifest_sha256, successor_project_head_id "
        "FROM successor_commits "
        "WHERE successor_transition_id = ?",
        (successor_transition_id,),
    ).fetchone()
    if row is None:
        raise ScienceTaskStoreV2Error("published v2 successor transition has no commit receipt")
    payload = _load_guarded_v2_blob(
        connection,
        table="successor_commits",
        key_column="successor_transition_id",
        key=successor_transition_id,
        blob_column="commit_json",
        label="persisted v2 successor commit",
    )
    if payload is None:
        raise ScienceTaskStoreV2Error("published v2 successor transition lost its commit receipt")
    commit = _v2_model_from_bytes(
        AtomicSuccessorCommitV2,
        payload,
    )
    if (
        row["manifest_sha256"] != commit.manifest_sha256
        or row["successor_project_head_id"] != commit.manifest.successor_project_head_id
    ):
        raise ScienceTaskStoreV2Error("persisted v2 successor commit index is inconsistent")
    return commit


def _load_v2_successor_cleanup_receipt(
    connection: sqlite3.Connection,
    successor_transition_id: str,
) -> ScienceSuccessorCleanupReceiptV2 | None:
    row = connection.execute(
        "SELECT receipt_sha256 FROM successor_cleanup_receipts WHERE successor_transition_id = ?",
        (successor_transition_id,),
    ).fetchone()
    if row is None:
        return None
    payload = _load_guarded_v2_blob(
        connection,
        table="successor_cleanup_receipts",
        key_column="successor_transition_id",
        key=successor_transition_id,
        blob_column="receipt_json",
        label="persisted v2 successor cleanup receipt",
    )
    if payload is None:
        raise ScienceTaskStoreV2Error("v2 successor cleanup receipt disappeared")
    receipt = _v2_model_from_bytes(
        ScienceSuccessorCleanupReceiptV2,
        payload,
    )
    if (
        receipt.successor_transition_id != successor_transition_id
        or hashlib.sha256(payload).hexdigest() != row["receipt_sha256"]
    ):
        raise ScienceTaskStoreV2Error("persisted v2 successor cleanup receipt is inconsistent")
    return receipt


def _load_v2_task_close_receipt(
    connection: sqlite3.Connection,
    task_id: str,
) -> _TaskCloseReceiptV2 | None:
    row = connection.execute(
        "SELECT task_id, close_request_id, expected_etag, "
        "task_admission_id, admission_sha256 "
        "FROM task_close_receipts WHERE task_id = ?",
        (task_id,),
    ).fetchone()
    if row is None:
        return None
    try:
        expected_etag = row["expected_etag"]
        if expected_etag is not None and (
            not isinstance(expected_etag, str)
            or re.fullmatch(
                r'"[0-9a-f]{64}"',
                expected_etag,
                flags=re.ASCII,
            )
            is None
        ):
            raise ValueError("invalid expected ETag")
        admission_sha256 = row["admission_sha256"]
        if (
            not isinstance(admission_sha256, str)
            or re.fullmatch(
                r"[0-9a-f]{64}",
                admission_sha256,
                flags=re.ASCII,
            )
            is None
        ):
            raise ValueError("invalid admission digest")
        receipt = _TaskCloseReceiptV2(
            task_id=_v2_resource_id(
                row["task_id"],
                label="Task close receipt task",
            ),
            close_request_id=_v2_resource_id(
                row["close_request_id"],
                label="Task close request",
            ),
            expected_etag=expected_etag,
            task_admission_id=_v2_resource_id(
                row["task_admission_id"],
                label="Task close admission",
            ),
            admission_sha256=admission_sha256,
        )
    except (TypeError, ValueError) as exc:
        raise ScienceTaskStoreV2Error("persisted v2 Task close receipt is invalid") from exc
    if receipt.task_id != task_id:
        raise ScienceTaskStoreV2Error("persisted v2 Task close receipt is inconsistent")
    return receipt


def _load_v2_successor_abandon_receipt(
    connection: sqlite3.Connection,
    successor_transition_id: str,
) -> _SuccessorAbandonReceiptV2 | None:
    row = connection.execute(
        "SELECT successor_transition_id, abandon_request_id, "
        "expected_project_head_id, transition_attempt_id "
        "FROM successor_abandon_receipts "
        "WHERE successor_transition_id = ?",
        (successor_transition_id,),
    ).fetchone()
    if row is None:
        return None
    try:
        receipt = _SuccessorAbandonReceiptV2(
            successor_transition_id=_v2_resource_id(
                row["successor_transition_id"],
                label="successor abandon transition",
            ),
            abandon_request_id=_v2_resource_id(
                row["abandon_request_id"],
                label="successor abandon request",
            ),
            expected_project_head_id=_v2_resource_id(
                row["expected_project_head_id"],
                label="successor abandon project head",
            ),
            transition_attempt_id=_v2_resource_id(
                row["transition_attempt_id"],
                label="successor abandon attempt",
            ),
        )
    except (TypeError, ValueError) as exc:
        raise ScienceTaskStoreV2Error("persisted v2 successor abandon receipt is invalid") from exc
    if receipt.successor_transition_id != successor_transition_id:
        raise ScienceTaskStoreV2Error("persisted v2 successor abandon receipt is inconsistent")
    return receipt


def _load_v2_legacy_task_close_exemption(
    connection: sqlite3.Connection,
    task_id: str,
) -> _LegacyTaskCloseExemptionV2 | None:
    row = connection.execute(
        "SELECT task_id, task_admission_id, admission_sha256 "
        "FROM legacy_task_close_exemptions WHERE task_id = ?",
        (task_id,),
    ).fetchone()
    if row is None:
        return None
    try:
        admission_sha256 = row["admission_sha256"]
        if (
            not isinstance(admission_sha256, str)
            or re.fullmatch(
                r"[0-9a-f]{64}",
                admission_sha256,
                flags=re.ASCII,
            )
            is None
        ):
            raise ValueError("invalid admission digest")
        exemption = _LegacyTaskCloseExemptionV2(
            task_id=_v2_resource_id(
                row["task_id"],
                label="legacy Task close exemption",
            ),
            task_admission_id=_v2_resource_id(
                row["task_admission_id"],
                label="legacy Task close admission",
            ),
            admission_sha256=admission_sha256,
        )
    except (TypeError, ValueError) as exc:
        raise ScienceTaskStoreV2Error(
            "persisted legacy v2 Task close exemption is invalid"
        ) from exc
    if exemption.task_id != task_id:
        raise ScienceTaskStoreV2Error("persisted legacy v2 Task close exemption is inconsistent")
    return exemption


def _load_v2_legacy_successor_abandon_exemption(
    connection: sqlite3.Connection,
    successor_transition_id: str,
) -> _LegacySuccessorAbandonExemptionV2 | None:
    row = connection.execute(
        "SELECT successor_transition_id, "
        "expected_project_head_id, transition_attempt_id "
        "FROM legacy_successor_abandon_exemptions "
        "WHERE successor_transition_id = ?",
        (successor_transition_id,),
    ).fetchone()
    if row is None:
        return None
    try:
        exemption = _LegacySuccessorAbandonExemptionV2(
            successor_transition_id=_v2_resource_id(
                row["successor_transition_id"],
                label="legacy successor abandon transition",
            ),
            expected_project_head_id=_v2_resource_id(
                row["expected_project_head_id"],
                label="legacy successor abandon project head",
            ),
            transition_attempt_id=_v2_resource_id(
                row["transition_attempt_id"],
                label="legacy successor abandon attempt",
            ),
        )
    except (TypeError, ValueError) as exc:
        raise ScienceTaskStoreV2Error(
            "persisted legacy v2 successor abandon exemption is invalid"
        ) from exc
    if exemption.successor_transition_id != successor_transition_id:
        raise ScienceTaskStoreV2Error(
            "persisted legacy v2 successor abandon exemption is inconsistent"
        )
    return exemption


def _load_v2_successor_commit_for_project_head(
    connection: sqlite3.Connection,
    project_head_id: str,
) -> AtomicSuccessorCommitV2 | None:
    head = _load_v2_project_head(connection, project_head_id)
    row = connection.execute(
        "SELECT successor_transition_id FROM successor_commits "
        "WHERE successor_project_head_id = ?",
        (project_head_id,),
    ).fetchone()
    if row is None:
        if head.generation != 0:
            raise ScienceTaskStoreV2Error(
                "non-genesis v2 project head has no atomic successor receipt"
            )
        return None
    commit = _load_v2_successor_commit(
        connection,
        str(row["successor_transition_id"]),
    )
    if (
        commit.manifest.project_id != head.project_id
        or commit.manifest.successor_project_head_id != project_head_id
        or commit.manifest.successor_generation != head.generation
        or commit.manifest.successor_manifest_sha256 != head.manifest_sha256
    ):
        raise ScienceTaskStoreV2Error("v2 project head and atomic successor receipt differ")
    return commit


def _v2_successor_commit_semantics(
    connection: sqlite3.Connection,
    manifest: AtomicSuccessorManifestV2 | AtomicEvolutionAbandonManifestV2,
) -> int:
    row = connection.execute(
        "SELECT composition_semantics_version, "
        "legacy_manifest_sha256 FROM successor_commits "
        "WHERE successor_transition_id = ?",
        (manifest.successor_transition_id,),
    ).fetchone()
    if row is None:
        return 2
    semantics_version = int(row["composition_semantics_version"])
    legacy_sha256 = row["legacy_manifest_sha256"]
    if semantics_version == 2:
        if legacy_sha256 is not None:
            raise ScienceTaskStoreV2Error("canonical v2 successor exposes legacy provenance")
        return 2
    if (
        semantics_version != 1
        or legacy_sha256 is None
        or type(manifest) is not AtomicSuccessorManifestV2
    ):
        raise ScienceTaskStoreV2Error("legacy v2 successor provenance is invalid")
    legacy_manifest = AtomicSuccessorManifestV2.model_validate(
        manifest.model_copy(
            update={"artifacts": (), "task_artifacts": ()}
        ).model_dump(mode="python")
    )
    if str(legacy_sha256) != atomic_successor_manifest_sha256(legacy_manifest):
        raise ScienceTaskStoreV2Error("legacy v2 successor provenance digest is inconsistent")
    return 1


def _v2_typed_artifact_authority(
    manifest: AtomicSuccessorManifestV2 | AtomicEvolutionAbandonManifestV2,
    *,
    expected_count: int,
    label: str,
) -> tuple[tuple[str, str, str, str], ...]:
    artifacts = manifest.artifacts
    artifact_ids = tuple(item.artifact_id for item in artifacts)
    target_ids = tuple(item.target_id for item in artifacts)
    if (
        len(manifest.method_artifact_ids) != expected_count
        or len(artifacts) != expected_count
        or artifact_ids != manifest.method_artifact_ids
        or target_ids != tuple(sorted(target_ids))
        or len(target_ids) != len(set(target_ids))
        or len(artifact_ids) != len(set(artifact_ids))
    ):
        raise ScienceTaskPreconditionFailedV2(f"{label} typed artifact composition is incomplete")
    return tuple(
        (
            item.target_id,
            item.artifact_id,
            item.artifact_type,
            item.owner_successor_transition_id,
        )
        for item in artifacts
    )


def _v2_predecessor_artifact_authority(
    connection: sqlite3.Connection,
    predecessor: m2.ProjectHeadRefV2,
) -> tuple[
    AtomicSuccessorCommitV2 | None,
    tuple[tuple[str, str, str, str], ...],
]:
    predecessor_commit = _load_v2_successor_commit_for_project_head(
        connection,
        predecessor.project_head_id,
    )
    if predecessor_commit is None:
        if predecessor.generation != 0 or predecessor.evolution_revision.artifact_count != 0:
            raise ScienceTaskPreconditionFailedV2(
                "successor predecessor lacks exact genesis artifact authority"
            )
        return None, ()
    return (
        predecessor_commit,
        _v2_typed_artifact_authority(
            predecessor_commit.manifest,
            expected_count=(predecessor.evolution_revision.artifact_count),
            label="predecessor",
        ),
    )


def _v2_expected_inherited_runtime(
    predecessor_commit: AtomicSuccessorCommitV2 | None,
) -> tuple[str, str | None, str | None, str | None, str | None]:
    if predecessor_commit is None:
        return ("empty_inherited", None, None, None, None)
    predecessor_manifest = predecessor_commit.manifest
    if (
        type(predecessor_manifest) is AtomicSuccessorManifestV2
        and predecessor_manifest.runtime_context_source == "materialized_new"
    ):
        return (
            "materialized_inherited",
            predecessor_manifest.successor_transition_id,
            predecessor_manifest.predecessor_project_head_id,
            predecessor_manifest.materialized_context_id,
            predecessor_manifest.materialized_context_manifest_sha256,
        )
    return (
        predecessor_manifest.runtime_context_source,
        predecessor_manifest.materialized_source_successor_transition_id,
        predecessor_manifest.materialized_source_predecessor_project_head_id,
        predecessor_manifest.materialized_context_id,
        predecessor_manifest.materialized_context_manifest_sha256,
    )


def _validate_v2_inherited_runtime_authority(
    connection: sqlite3.Connection,
    *,
    predecessor: m2.ProjectHeadRefV2,
    expected_runtime: tuple[
        str,
        str | None,
        str | None,
        str | None,
        str | None,
    ],
    expected_artifacts: tuple[tuple[str, str, str, str], ...],
) -> None:
    (
        runtime_source,
        source_transition_id,
        source_predecessor_id,
        context_id,
        context_sha256,
    ) = expected_runtime
    if runtime_source == "empty_inherited":
        if (
            any(
                value is not None
                for value in (
                    source_transition_id,
                    source_predecessor_id,
                    context_id,
                    context_sha256,
                )
            )
            or expected_artifacts
            or predecessor.evolution_revision.artifact_count != 0
        ):
            raise ScienceTaskPreconditionFailedV2(
                "empty inherited runtime authority is inconsistent"
            )
        return
    if (
        runtime_source != "materialized_inherited"
        or source_transition_id is None
        or source_predecessor_id is None
        or context_id is None
        or context_sha256 is None
    ):
        raise ScienceTaskPreconditionFailedV2("inherited runtime authority is incomplete")
    source_commit = _load_v2_successor_commit(
        connection,
        source_transition_id,
    )
    source_manifest = source_commit.manifest
    if type(source_manifest) is not AtomicSuccessorManifestV2:
        raise ScienceTaskPreconditionFailedV2(
            "inherited runtime source is not an evolved successor"
        )
    source_artifacts = _v2_typed_artifact_authority(
        source_manifest,
        expected_count=(predecessor.evolution_revision.artifact_count),
        label="inherited runtime source",
    )
    if (
        source_manifest.runtime_context_source != "materialized_new"
        or source_manifest.successor_transition_id != source_transition_id
        or source_manifest.predecessor_project_head_id != source_predecessor_id
        or source_manifest.materialized_context_id != context_id
        or source_manifest.materialized_context_manifest_sha256 != context_sha256
        or source_manifest.evolution_revision_id
        != predecessor.evolution_revision.evolution_revision_id
        or source_manifest.evolution_revision_manifest_sha256
        != predecessor.evolution_revision.manifest_sha256
        or source_manifest.runtime_context_snapshot_id
        != predecessor.runtime_context_snapshot.runtime_context_snapshot_id
        or source_manifest.runtime_context_manifest_sha256
        != predecessor.runtime_context_snapshot.manifest_sha256
        or source_manifest.registry_sha256 != predecessor.registry_sha256
        or source_artifacts != expected_artifacts
    ):
        raise ScienceTaskPreconditionFailedV2(
            "inherited runtime authority differs from its exact source receipt"
        )


def _validate_v2_successor_commit_closure(
    *,
    connection: sqlite3.Connection,
    task: m2.TaskV2,
    transition: m2.SuccessorTransitionV2,
    successor: m2.ProjectHeadRefV2,
    commit: AtomicSuccessorCommitV2,
    publication_timestamp: str | None = None,
    require_task_artifacts: bool = False,
) -> None:
    reference = transition.transition
    admission = reference.task_admission
    attempt = reference.accepted_attempt
    if admission is None or attempt is None:
        raise ScienceTaskStoreV2Error("v2 successor commit lacks Task ownership")
    manifest = commit.manifest
    predecessor = reference.predecessor_project_head
    if (
        manifest.project_id != task.project_id
        or manifest.successor_transition_id != reference.successor_transition_id
        or manifest.task_id != task.task_id
        or manifest.task_admission_id != admission.task_admission_id
        or manifest.admission_sha256 != admission.admission_sha256
        or manifest.accepted_attempt_id != attempt.attempt_id
        or manifest.predecessor_project_head_id != predecessor.project_head_id
        or manifest.predecessor_generation != predecessor.generation
        or manifest.predecessor_manifest_sha256 != predecessor.manifest_sha256
        or manifest.successor_project_head_id != successor.project_head_id
        or manifest.successor_generation != successor.generation
        or manifest.successor_manifest_sha256 != successor.manifest_sha256
        or manifest.workspace_snapshot_id != successor.workspace_snapshot.workspace_snapshot_id
        or manifest.workspace_manifest_sha256 != successor.workspace_snapshot.manifest_sha256
        or manifest.evolution_revision_id != successor.evolution_revision.evolution_revision_id
        or manifest.evolution_revision_manifest_sha256
        != successor.evolution_revision.manifest_sha256
        or manifest.runtime_context_snapshot_id
        != successor.runtime_context_snapshot.runtime_context_snapshot_id
        or manifest.runtime_context_manifest_sha256
        != successor.runtime_context_snapshot.manifest_sha256
        or manifest.effective_execution_snapshot_id
        != successor.effective_execution_snapshot.effective_execution_snapshot_id
        or manifest.effective_execution_snapshot_sha256
        != successor.effective_execution_snapshot.snapshot_sha256
        or manifest.registry_sha256 != successor.registry_sha256
        or manifest.normalized_evolution_intent_sha256
        != admission.normalized_evolution_intent_sha256
        or successor.predecessor_project_head_id != predecessor.project_head_id
        or successor.generation != reference.expected_successor_generation
        or successor.effective_execution_snapshot != predecessor.effective_execution_snapshot
    ):
        raise ScienceTaskPreconditionFailedV2(
            "atomic v2 successor receipt does not match its authoritative closure"
        )
    plan_row = connection.execute(
        "SELECT plan_json FROM successor_transitions WHERE successor_transition_id = ?",
        (reference.successor_transition_id,),
    ).fetchone()
    if plan_row is None:
        raise ScienceTaskPreconditionFailedV2("atomic v2 successor has no immutable plan")
    plan = _v2_model_from_bytes(
        ScienceSuccessorPlanV2,
        bytes(plan_row["plan_json"]),
    )
    if (
        plan.project_id != task.project_id
        or plan.task_id != task.task_id
        or plan.task_admission_id != admission.task_admission_id
        or plan.admission_sha256 != admission.admission_sha256
        or plan.accepted_attempt_id != attempt.attempt_id
        or plan.predecessor_project_head_id != predecessor.project_head_id
        or plan.normalized_evolution_intent_sha256 != admission.normalized_evolution_intent_sha256
    ):
        raise ScienceTaskPreconditionFailedV2(
            "atomic v2 successor immutable plan has different authority"
        )
    (
        predecessor_commit,
        predecessor_artifacts,
    ) = _v2_predecessor_artifact_authority(
        connection,
        predecessor,
    )
    semantics_version = _v2_successor_commit_semantics(
        connection,
        manifest,
    )
    if require_task_artifacts and not manifest.task_artifacts:
        raise ScienceTaskPreconditionFailedV2(
            "canonical v2 successor lacks Task artifact publications"
        )
    if publication_timestamp is not None and any(
        _v2_timestamp(item.created_at) != publication_timestamp
        for item in manifest.task_artifacts
    ):
        raise ScienceTaskPreconditionFailedV2(
            "canonical v2 Task artifact publication timestamp changed"
        )
    if type(manifest) is AtomicSuccessorManifestV2:
        if transition.state == "cancelled":
            raise ScienceTaskPreconditionFailedV2(
                "evolved v2 successor receipt cannot abandon evolution"
            )
        dataset_event = _load_v2_dataset_event_for_task_unchecked(
            connection,
            task.task_id,
        )
        if (
            dataset_event is None
            or dataset_event.attempt_id != attempt.attempt_id
            or manifest.dataset_id != dataset_event.dataset_id
            or manifest.dataset_manifest_sha256 != dataset_event.dataset_sha256
        ):
            raise ScienceTaskPreconditionFailedV2(
                "atomic v2 successor receipt differs from sealed dataset authority"
            )
        observed_artifacts = _v2_typed_artifact_authority(
            manifest,
            expected_count=(successor.evolution_revision.artifact_count),
            label="atomic successor",
        )
        if semantics_version == 1:
            expected_produced = tuple(
                (
                    item.target_id,
                    item.output_artifact_type,
                )
                for item in plan.enabled_methods
            )
            observed_produced = tuple(
                (item.target_id, item.artifact_type) for item in manifest.artifacts
            )
            if (
                manifest.runtime_context_source != "materialized_new"
                or successor.registry_sha256 != predecessor.registry_sha256
                or successor.runtime_context_snapshot.runtime_contract_sha256
                != predecessor.runtime_context_snapshot.runtime_contract_sha256
                or successor.evolution_revision.evolution_revision_id
                == predecessor.evolution_revision.evolution_revision_id
                or successor.evolution_revision.manifest_sha256
                == predecessor.evolution_revision.manifest_sha256
                or successor.runtime_context_snapshot.runtime_context_snapshot_id
                == predecessor.runtime_context_snapshot.runtime_context_snapshot_id
                or successor.runtime_context_snapshot.manifest_sha256
                == predecessor.runtime_context_snapshot.manifest_sha256
                or observed_produced != expected_produced
                or any(
                    item.origin != "produced"
                    or item.owner_successor_transition_id != manifest.successor_transition_id
                    for item in manifest.artifacts
                )
            ):
                raise ScienceTaskPreconditionFailedV2(
                    "legacy v2 successor differs from its preserved output authority"
                )
            return
        if plan.enabled_methods:
            enabled_targets = frozenset(item.target_id for item in plan.enabled_methods)
            expected_produced = tuple(
                (
                    item.target_id,
                    item.output_artifact_type,
                )
                for item in plan.enabled_methods
            )
            observed_produced = tuple(
                (item.target_id, item.artifact_type)
                for item in manifest.artifacts
                if item.origin == "produced"
            )
            expected_inherited = tuple(
                item for item in predecessor_artifacts if item[0] not in enabled_targets
            )
            observed_inherited = tuple(
                (
                    item.target_id,
                    item.artifact_id,
                    item.artifact_type,
                    item.owner_successor_transition_id,
                )
                for item in manifest.artifacts
                if item.origin == "inherited"
            )
            if (
                manifest.runtime_context_source != "materialized_new"
                or successor.registry_sha256 != predecessor.registry_sha256
                or successor.runtime_context_snapshot.runtime_contract_sha256
                != predecessor.runtime_context_snapshot.runtime_contract_sha256
                or successor.evolution_revision.evolution_revision_id
                == predecessor.evolution_revision.evolution_revision_id
                or successor.evolution_revision.manifest_sha256
                == predecessor.evolution_revision.manifest_sha256
                or successor.runtime_context_snapshot.runtime_context_snapshot_id
                == predecessor.runtime_context_snapshot.runtime_context_snapshot_id
                or successor.runtime_context_snapshot.manifest_sha256
                == predecessor.runtime_context_snapshot.manifest_sha256
                or observed_produced != expected_produced
                or observed_inherited != expected_inherited
                or len(observed_artifacts) != len(expected_inherited) + len(expected_produced)
            ):
                raise ScienceTaskPreconditionFailedV2(
                    "evolved v2 successor composition differs from its immutable plan"
                )
            return
        expected_runtime = _v2_expected_inherited_runtime(predecessor_commit)
        observed_runtime = (
            manifest.runtime_context_source,
            manifest.materialized_source_successor_transition_id,
            manifest.materialized_source_predecessor_project_head_id,
            manifest.materialized_context_id,
            manifest.materialized_context_manifest_sha256,
        )
        if (
            successor.evolution_revision != predecessor.evolution_revision
            or successor.runtime_context_snapshot != predecessor.runtime_context_snapshot
            or successor.registry_sha256 != predecessor.registry_sha256
            or observed_runtime != expected_runtime
            or observed_artifacts != predecessor_artifacts
            or any(item.origin != "inherited" for item in manifest.artifacts)
        ):
            raise ScienceTaskPreconditionFailedV2(
                "no-evolution successor changed inherited evolution authority"
            )
        _validate_v2_inherited_runtime_authority(
            connection,
            predecessor=predecessor,
            expected_runtime=expected_runtime,
            expected_artifacts=predecessor_artifacts,
        )
        return
    if type(manifest) is not AtomicEvolutionAbandonManifestV2:
        raise ScienceTaskPreconditionFailedV2(
            "atomic v2 successor receipt has an unsupported type"
        )
    observed_artifacts = _v2_typed_artifact_authority(
        manifest,
        expected_count=(predecessor.evolution_revision.artifact_count),
        label="evolution abandon",
    )
    expected_runtime = _v2_expected_inherited_runtime(predecessor_commit)
    observed_runtime = (
        manifest.runtime_context_source,
        manifest.materialized_source_successor_transition_id,
        manifest.materialized_source_predecessor_project_head_id,
        manifest.materialized_context_id,
        manifest.materialized_context_manifest_sha256,
    )
    if (
        successor.evolution_revision != predecessor.evolution_revision
        or successor.runtime_context_snapshot != predecessor.runtime_context_snapshot
        or successor.registry_sha256 != predecessor.registry_sha256
        or observed_runtime != expected_runtime
        or observed_artifacts != predecessor_artifacts
        or any(item.origin != "inherited" for item in manifest.artifacts)
    ):
        raise ScienceTaskPreconditionFailedV2(
            "evolution abandon changed exact predecessor evolution authority"
        )
    _validate_v2_inherited_runtime_authority(
        connection,
        predecessor=predecessor,
        expected_runtime=expected_runtime,
        expected_artifacts=predecessor_artifacts,
    )


def _validate_v2_project_authority(
    authority: ScienceProjectAdmissionAuthorityV2,
) -> ScienceProjectAdmissionAuthorityV2:
    if type(authority) is not ScienceProjectAdmissionAuthorityV2:
        raise TypeError("v2 project admission authority has the wrong type")
    return ScienceProjectAdmissionAuthorityV2(
        project_id=authority.project_id,
        active_project_head=m2.ProjectHeadRefV2.model_validate(
            authority.active_project_head.model_dump(mode="python")
        ),
        project_config_sha256=authority.project_config_sha256,
        workspace_snapshot=m2.WorkspaceSnapshotRefV2.model_validate(
            authority.workspace_snapshot.model_dump(mode="python")
        ),
        normalized_evolution_intent_sha256=(authority.normalized_evolution_intent_sha256),
        blockers=authority.blockers,
    )


def _v2_authority_bytes(authority: ScienceProjectAdmissionAuthorityV2) -> bytes:
    return _v2_json_bytes(
        {
            "active_project_head": authority.active_project_head.model_dump(mode="json"),
            "blockers": [blocker.value for blocker in authority.blockers],
            "normalized_evolution_intent_sha256": (authority.normalized_evolution_intent_sha256),
            "project_config_sha256": authority.project_config_sha256,
            "project_id": authority.project_id,
            "workspace_snapshot": authority.workspace_snapshot.model_dump(mode="json"),
        }
    )


def _v2_authority_etag(authority: ScienceProjectAdmissionAuthorityV2) -> str:
    return f'"{hashlib.sha256(_v2_authority_bytes(authority)).hexdigest()}"'


def _v2_authority_from_bytes(payload: bytes | str) -> ScienceProjectAdmissionAuthorityV2:
    raw = payload.encode("utf-8") if isinstance(payload, str) else bytes(payload)
    if len(raw) > _MAX_DOCUMENT_BYTES:
        raise ScienceTaskStoreV2Error(
            "persisted v2 project admission authority exceeds its byte bound"
        )
    try:
        value = json.loads(raw)
        if not isinstance(value, dict) or set(value) != {
            "active_project_head",
            "blockers",
            "normalized_evolution_intent_sha256",
            "project_config_sha256",
            "project_id",
            "workspace_snapshot",
        }:
            raise ValueError("authority is not a closed object")
        blockers = value["blockers"]
        if not isinstance(blockers, list):
            raise ValueError("authority blockers are invalid")
        authority = ScienceProjectAdmissionAuthorityV2(
            project_id=value["project_id"],
            active_project_head=m2.ProjectHeadRefV2.model_validate(value["active_project_head"]),
            project_config_sha256=value["project_config_sha256"],
            workspace_snapshot=m2.WorkspaceSnapshotRefV2.model_validate(
                value["workspace_snapshot"]
            ),
            normalized_evolution_intent_sha256=value["normalized_evolution_intent_sha256"],
            blockers=tuple(ScienceProjectReadinessBlockerV2(item) for item in blockers),
        )
    except Exception as exc:
        raise ScienceTaskStoreV2Error(
            "persisted v2 project admission authority is invalid"
        ) from exc
    if _v2_authority_bytes(authority) != raw:
        raise ScienceTaskStoreV2Error("persisted v2 project admission authority is not canonical")
    return authority


def _validate_v2_model(model_type: type[_T], model: _T) -> _T:
    if type(model) is not model_type:
        raise TypeError(f"v2 {model_type.__name__} has the wrong type")
    return model_type.model_validate(model.model_dump(mode="python"))


def _v2_model_bytes(model: BaseModel) -> bytes:
    return _v2_json_bytes(model.model_dump(mode="json"))


def _v2_model_from_bytes(model_type: type[_T], payload: bytes | str) -> _T:
    raw = payload.encode("utf-8") if isinstance(payload, str) else bytes(payload)
    if len(raw) > _MAX_DOCUMENT_BYTES:
        raise ScienceTaskStoreV2Error("persisted v2 document exceeds its byte bound")
    try:
        model = model_type.model_validate_json(raw)
    except Exception as exc:
        raise ScienceTaskStoreV2Error("persisted v2 document is invalid") from exc
    if _v2_model_bytes(model) != raw:
        raise ScienceTaskStoreV2Error("persisted v2 document is not canonical")
    return model


_V2_EVENT_MODELS: dict[str, type[BaseModel]] = {
    "task_admitted": m2.TaskAdmittedEventV2,
    "attempt_appended": m2.AttemptAppendedEventV2,
    "dataset_sealed": m2.DatasetSealedEventV2,
    "transition_changed": m2.TransitionChangedEventV2,
    "evolution_revision_committed": m2.EvolutionRevisionCommittedEventV2,
    "runtime_context_committed": m2.RuntimeContextCommittedEventV2,
    "project_head_activated": m2.ProjectHeadActivatedEventV2,
}


def _append_v2_event(
    connection: sqlite3.Connection,
    *,
    model_type: type[BaseModel],
    event_type: str,
    project_id: str,
    task_id: str | None,
    occurred_at: str,
    payload: Mapping[str, object],
) -> m2.EventEnvelopeV2:
    if _V2_EVENT_MODELS.get(event_type) is not model_type:
        raise ScienceTaskStoreV2Error("v2 event model does not match its type")
    next_sequence = int(
        connection.execute("SELECT COALESCE(MAX(sequence), 0) + 1 FROM events").fetchone()[0]
    )
    if next_sequence > m2.MAX_JAVASCRIPT_SAFE_INTEGER:
        raise ScienceTaskConflictV2("v2 event sequence capacity is exhausted")
    event_count = int(connection.execute("SELECT COUNT(*) FROM events").fetchone()[0])
    if event_count >= _MAX_V2_EVENT_ROWS:
        raise ScienceTaskConflictV2("v2 event journal capacity is exhausted")
    event_payload = {
        "schema_version": "2",
        "sequence": next_sequence,
        "occurred_at": occurred_at,
        "project_id": project_id,
        "event_type": event_type,
        **dict(payload),
    }
    try:
        provisional = model_type.model_validate(
            {
                "event_id": f"event-{'0' * 64}",
                **event_payload,
            }
        )
        event_id = _v2_event_id(provisional)
        event = model_type.model_validate(
            {
                "event_id": event_id,
                **event_payload,
            }
        )
    except (TypeError, ValueError) as exc:
        raise ScienceTaskStoreV2Error("v2 event payload is invalid") from exc
    connection.execute(
        "INSERT INTO events(sequence, event_id, event_type, project_id, task_id, "
        "event_json) VALUES (?, ?, ?, ?, ?, ?)",
        (
            next_sequence,
            event_id,
            event_type,
            project_id,
            task_id,
            _v2_model_bytes(event),
        ),
    )
    return event  # type: ignore[return-value]


def _load_v2_event_bytes(payload: bytes | str) -> m2.EventEnvelopeV2:
    raw_bytes = payload.encode("utf-8") if isinstance(payload, str) else bytes(payload)
    try:
        raw = json.loads(raw_bytes.decode("utf-8", errors="strict"))
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise ScienceTaskStoreV2Error("persisted v2 event is invalid") from exc
    if not isinstance(raw, dict):
        raise ScienceTaskStoreV2Error("persisted v2 event is invalid")
    event_type = raw.get("event_type")
    model_type = _V2_EVENT_MODELS.get(event_type) if isinstance(event_type, str) else None
    if model_type is None:
        raise ScienceTaskStoreV2Error("persisted v2 event type is invalid")
    event = _v2_model_from_bytes(model_type, raw_bytes)
    if event.event_id != _v2_event_id(event):
        raise ScienceTaskStoreV2Error("persisted v2 event ID is inconsistent")
    return event  # type: ignore[return-value]


def _load_v2_dataset_event_for_task(
    connection: sqlite3.Connection,
    task_id: str,
) -> m2.DatasetSealedEventV2 | None:
    event = _load_v2_dataset_event_for_task_unchecked(
        connection,
        task_id,
    )
    if event is None:
        return None
    _validate_v2_event_authority(
        connection,
        event,
        task_id,
    )
    return event


def _load_v2_dataset_event_for_task_unchecked(
    connection: sqlite3.Connection,
    task_id: str,
) -> m2.DatasetSealedEventV2 | None:
    rows = connection.execute(
        "SELECT task_id, event_json FROM events "
        "WHERE task_id = ? AND event_type = 'dataset_sealed' "
        "ORDER BY sequence LIMIT 2",
        (task_id,),
    ).fetchall()
    if len(rows) > 1:
        raise ScienceTaskStoreV2Error("v2 Task has multiple sealed dataset events")
    if not rows:
        return None
    if rows[0]["task_id"] != task_id:
        raise ScienceTaskStoreV2Error("v2 Task sealed dataset event ownership is inconsistent")
    event = _load_v2_event_bytes(bytes(rows[0]["event_json"]))
    if not isinstance(event, m2.DatasetSealedEventV2):
        raise ScienceTaskStoreV2Error("v2 Task sealed dataset event has the wrong type")
    return event


def _load_and_validate_v2_event(
    connection: sqlite3.Connection,
    row: sqlite3.Row,
) -> m2.EventEnvelopeV2:
    event = _load_v2_event_bytes(bytes(row["event_json"]))
    task_id = row["task_id"]
    _validate_v2_event_authority(
        connection,
        event,
        None if task_id is None else str(task_id),
    )
    return event


def _validate_v2_event_authority(
    connection: sqlite3.Connection,
    event: m2.EventEnvelopeV2,
    task_id: str | None,
) -> None:
    if task_id is None:
        raise ScienceTaskStoreV2Error("persisted v2 event has no Task authority")
    task = _load_v2_task_closure(connection, task_id)
    if task.project_id != event.project_id:
        raise ScienceTaskStoreV2Error("persisted v2 event belongs to another project")
    if isinstance(event, m2.TaskAdmittedEventV2):
        valid = (
            event.admission == task.admission and event.occurred_at == task.admission.admitted_at
        )
    elif isinstance(event, m2.AttemptAppendedEventV2):
        valid = event.attempt in task.attempts and event.occurred_at == event.attempt.created_at
    elif isinstance(event, m2.DatasetSealedEventV2):
        valid = (
            event.task_id == task.task_id
            and event.task_admission_id == task.admission.task_admission_id
            and event.attempt_id == task.authoritative_attempt_id
            and task.successor_transition is not None
            and task.successor_transition.accepted_attempt is not None
            and event.attempt_id == task.successor_transition.accepted_attempt.attempt_id
        )
        if valid and task.successor_transition is not None:
            commit_row = connection.execute(
                "SELECT commit_json FROM successor_commits WHERE successor_transition_id = ?",
                (task.successor_transition.successor_transition_id,),
            ).fetchone()
            if commit_row is not None:
                commit = _v2_model_from_bytes(
                    AtomicSuccessorCommitV2,
                    bytes(commit_row["commit_json"]),
                )
                if type(commit.manifest) is AtomicSuccessorManifestV2:
                    valid = (
                        commit.manifest.dataset_id == event.dataset_id
                        and commit.manifest.dataset_manifest_sha256 == event.dataset_sha256
                    )
    elif isinstance(event, m2.TransitionChangedEventV2):
        current = _load_v2_successor_transition(
            connection,
            event.transition.successor_transition_id,
        )
        historical_reference = current.transition.model_copy(
            update={"successor_project_head": None}
        )
        valid = (
            current.transition.task_admission is not None
            and current.transition.task_admission.task_id == task.task_id
            and event.transition in (current.transition, historical_reference)
        )
    elif isinstance(event, m2.EvolutionRevisionCommittedEventV2):
        transition = _load_v2_successor_transition(
            connection,
            event.successor_transition_id,
        )
        successor = transition.transition.successor_project_head
        valid = successor is not None and event.evolution_revision == successor.evolution_revision
    elif isinstance(event, m2.RuntimeContextCommittedEventV2):
        transition = _load_v2_successor_transition(
            connection,
            event.successor_transition_id,
        )
        successor = transition.transition.successor_project_head
        valid = (
            successor is not None
            and event.runtime_context_snapshot == successor.runtime_context_snapshot
        )
    elif isinstance(event, m2.ProjectHeadActivatedEventV2):
        transition = _load_v2_successor_transition(
            connection,
            event.successor_transition_id,
        )
        valid = event.project_head == transition.transition.successor_project_head
    else:  # pragma: no cover - closed event union guards this branch
        valid = False
    if not valid:
        raise ScienceTaskStoreV2Error("persisted v2 event does not match authoritative Task state")


def _load_and_validate_v2_task_event_history(
    connection: sqlite3.Connection,
    task_id: str,
) -> list[m2.EventEnvelopeV2]:
    task = _load_v2_task_closure(connection, task_id)
    rows = connection.execute(
        "SELECT task_id, event_json FROM events WHERE task_id = ? ORDER BY sequence LIMIT ?",
        (task_id, _MAX_V2_EVENTS_PER_TASK + 1),
    ).fetchall()
    if len(rows) > _MAX_V2_EVENTS_PER_TASK:
        raise ScienceTaskStoreV2Error("v2 task timeline exceeds its bound")
    events = [_load_and_validate_v2_event(connection, row) for row in rows]

    def invalid() -> None:
        raise ScienceTaskStoreV2Error(
            "persisted v2 event history does not match the Task lifecycle"
        )

    expected_initial = 1 + len(task.attempts)
    if len(events) < expected_initial:
        invalid()
    admitted = events[0]
    if not isinstance(admitted, m2.TaskAdmittedEventV2):
        invalid()
    for offset, attempt in enumerate(task.attempts, start=1):
        event = events[offset]
        if not isinstance(event, m2.AttemptAppendedEventV2) or event.attempt != attempt:
            invalid()

    remaining = events[expected_initial:]
    if task.successor_transition is None:
        if remaining:
            invalid()
        return events

    transition = _load_v2_successor_transition(
        connection,
        task.successor_transition.successor_transition_id,
    )
    abandon_committed = (
        transition.state == "cancelled"
        and transition.transition.successor_project_head is not None
    )
    if not remaining or not isinstance(remaining[0], m2.TransitionChangedEventV2):
        invalid()
    pending = remaining[0]
    if (
        pending.state != "pending"
        or pending.progress_completed != 0
        or pending.progress_total != 6
    ):
        invalid()

    phases = (
        "pending",
        "sealing_dataset",
        "running_methods",
        "validating",
        "materializing",
        "committing",
        "committed",
    )
    phase_index = 0
    dataset_seen = False
    commit_stage = 0
    failed_seen = False
    cancelled_seen = False
    for event in remaining[1:]:
        if cancelled_seen:
            invalid()
        if isinstance(event, m2.TransitionChangedEventV2):
            if event.state == "failed":
                if (
                    failed_seen
                    or phase_index == 6
                    or event.progress_completed != phase_index
                    or event.progress_total != 6
                ):
                    invalid()
                failed_seen = True
                continue
            if failed_seen:
                if (
                    event.state == "cancelled"
                    and event.progress_completed == phase_index
                    and event.progress_total == 6
                    and abandon_committed
                    and commit_stage == 4
                ):
                    failed_seen = False
                    cancelled_seen = True
                    continue
                if (
                    event.state == "pending"
                    and not dataset_seen
                    and event.progress_completed == 0
                    and event.progress_total == 6
                ):
                    failed_seen = False
                    phase_index = 0
                    continue
                if (
                    event.state == "running_methods"
                    and dataset_seen
                    and event.progress_completed == 2
                    and event.progress_total == 6
                ):
                    failed_seen = False
                    phase_index = 2
                    continue
                invalid()
            if phase_index + 1 >= len(phases) or event.state != phases[phase_index + 1]:
                invalid()
            if event.state == "running_methods" and not dataset_seen:
                invalid()
            if event.state == "committed" and commit_stage != 3:
                invalid()
            phase_index += 1
            if event.progress_completed != phase_index or event.progress_total != 6:
                invalid()
            continue
        if isinstance(event, m2.DatasetSealedEventV2):
            if phase_index != 1 or dataset_seen or commit_stage != 0:
                invalid()
            dataset_seen = True
            continue
        if isinstance(event, m2.EvolutionRevisionCommittedEventV2):
            if phase_index != 5 or commit_stage != 0:
                invalid()
            commit_stage = 1
            continue
        if isinstance(event, m2.RuntimeContextCommittedEventV2):
            if phase_index != 5 or commit_stage != 1:
                invalid()
            commit_stage = 2
            continue
        if isinstance(event, m2.ProjectHeadActivatedEventV2):
            if failed_seen and abandon_committed and phase_index < 6 and commit_stage == 0:
                commit_stage = 4
                continue
            if phase_index != 5 or commit_stage != 2:
                invalid()
            commit_stage = 3
            continue
        invalid()

    if failed_seen:
        if transition.progress_completed != phase_index or commit_stage != 0:
            invalid()
        if transition.state != "failed":
            invalid()
    elif cancelled_seen:
        if (
            transition.state != "cancelled"
            or transition.progress_completed != phase_index
            or commit_stage != 4
        ):
            invalid()
    elif (
        transition.state != phases[phase_index]
        or transition.progress_completed != phase_index
        or transition.progress_total != 6
    ):
        invalid()
    if phase_index >= 2 and not dataset_seen:
        invalid()
    if cancelled_seen:
        if commit_stage != 4:
            invalid()
    elif phase_index == 6:
        if commit_stage != 3:
            invalid()
    elif commit_stage != 0:
        invalid()
    return events


def _v2_attempt_log_message(
    ordinal: int,
    record: ScienceAttemptExecutionRecordV2,
) -> str:
    state = {
        "preparing": "is preparing the managed runtime",
        "running": "is running",
        "cancelling": "is stopping after a cancellation request",
        "captured": "completed execution and sealed its result",
        "failed": f"failed ({record.error_code or 'unknown_error'})",
        "cancelled": "was cancelled",
    }[record.state]
    return f"Attempt {ordinal} {state}."


def _v2_task_state_log_message(state: m2.TaskStateV2) -> str:
    return {
        "admitted": "Task is admitted.",
        "preparing": "Task is preparing its managed execution.",
        "running": "Task is running.",
        "cancelling": "Task cancellation is in progress.",
        "completed": "Task completed and activated its successor Project Head.",
        "failed": "Task failed; the predecessor Project Head remains active.",
        "cancelled": "Task was cancelled; the predecessor Project Head remains active.",
        "closed": "Task was closed without an accepted result.",
        "waiting_for_successor": (
            "Task execution completed; Core is preparing the successor Project Head."
        ),
    }[state]


def _v2_task_event_log_message(event: m2.EventEnvelopeV2) -> str | None:
    if isinstance(event, (m2.TaskAdmittedEventV2, m2.AttemptAppendedEventV2)):
        return None
    if isinstance(event, m2.DatasetSealedEventV2):
        return "Captured transcript dataset sealed."
    if isinstance(event, m2.TransitionChangedEventV2):
        state = event.state.replace("_", " ")
        return (
            f"Successor transition {state} "
            f"({event.progress_completed}/{event.progress_total})."
        )
    if isinstance(event, m2.EvolutionRevisionCommittedEventV2):
        return "Evolution Revision committed."
    if isinstance(event, m2.RuntimeContextCommittedEventV2):
        return "Runtime Context Snapshot committed."
    if isinstance(event, m2.ProjectHeadActivatedEventV2):
        return "Successor Project Head activated."
    raise ScienceTaskStoreV2Error("v2 Task log projection received an unknown event")


def _v2_transcript_log_message(message: Mapping[str, object]) -> str | None:
    if not isinstance(message, Mapping):
        raise ScienceTaskStoreV2Error("captured transcript message is invalid")
    role = message.get("role")
    content = message.get("content")
    if isinstance(content, str):
        rendered = content
    else:
        try:
            rendered = json.dumps(
                content if content is not None else dict(message),
                ensure_ascii=False,
                allow_nan=False,
                sort_keys=True,
                separators=(",", ":"),
            )
        except (TypeError, ValueError) as exc:
            raise ScienceTaskStoreV2Error(
                "captured transcript message cannot be rendered"
            ) from exc
    rendered = rendered.strip()
    if not rendered:
        return None
    prefix = f"{role}: " if isinstance(role, str) and role else ""
    encoded = (prefix + rendered).encode("utf-8")[:16_384]
    value = encoded.decode("utf-8", errors="ignore")
    return value or None


def _v2_event_id(event: BaseModel) -> str:
    payload = event.model_dump(mode="json", exclude={"event_id"})
    return f"event-{hashlib.sha256(_v2_json_bytes(payload)).hexdigest()}"


def _v2_json_bytes(value: object) -> bytes:
    try:
        payload = json.dumps(
            value,
            ensure_ascii=True,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise ValueError("v2 document is not canonical JSON data") from exc
    if len(payload) > _MAX_DOCUMENT_BYTES:
        raise ValueError("v2 document exceeds its byte bound")
    return payload


def _v2_idempotency_key(value: str) -> str:
    if (
        not isinstance(value, str)
        or not value
        or len(value.encode("utf-8")) > 256
        or any(ord(character) < 0x20 or ord(character) == 0x7F for character in value)
    ):
        raise ValueError("v2 idempotency key is invalid")
    return value


def _v2_resource_id(value: str, *, label: str) -> str:
    if not isinstance(value, str) or _V2_ID_RE.fullmatch(value) is None:
        raise ValueError(f"v2 {label} ID is invalid")
    return value


def _validate_v2_submission_authority(
    request: m2.TaskSubmitRequestV2,
    authority: ScienceProjectAdmissionAuthorityV2,
) -> None:
    if (
        request.project_id != authority.project_id
        or request.expected_project_admission_etag != authority.project_etag
        or request.expected_project_head_id != authority.active_project_head.project_head_id
        or request.expected_project_head_manifest_sha256
        != authority.active_project_head.manifest_sha256
        or request.expected_project_config_sha256 != authority.project_config_sha256
    ):
        raise ScienceTaskStaleSubmissionV2(
            "v2 Task submission no longer matches project admission authority"
        )


def _new_v2_task(
    *,
    request: m2.TaskSubmitRequestV2,
    authority: ScienceProjectAdmissionAuthorityV2,
    now: datetime,
) -> m2.TaskV2:
    timestamp = _v2_timestamp(now)
    task_id = f"task-{secrets.token_hex(16)}"
    task_admission_id = f"task-admission-{secrets.token_hex(16)}"
    provisional_admission = m2.TaskAdmissionRefV2.model_construct(
        schema_version="2",
        task_admission_id=task_admission_id,
        task_id=task_id,
        project_id=request.project_id,
        predecessor_project_head=authority.active_project_head,
        workspace_snapshot=authority.workspace_snapshot,
        project_config_sha256=authority.project_config_sha256,
        task_envelope_sha256=_task_envelope_sha256(
            project_id=authority.project_id,
            predecessor_project_head=authority.active_project_head,
            workspace_snapshot=authority.workspace_snapshot,
            project_config_sha256=authority.project_config_sha256,
        ),
        normalized_evolution_intent_sha256=(authority.normalized_evolution_intent_sha256),
        registry_sha256=authority.active_project_head.registry_sha256,
        admission_sha256="0" * 64,
        admitted_at=timestamp,
    )
    admission = m2.TaskAdmissionRefV2.model_validate(
        {
            **provisional_admission.model_dump(mode="python"),
            "admission_sha256": m2.task_admission_sha256_for(provisional_admission),
        }
    )
    first_attempt = m2.AttemptRefV2(
        attempt_id=f"attempt-{secrets.token_hex(16)}",
        ordinal=1,
        task_id=task_id,
        task_admission_id=task_admission_id,
        admission_sha256=admission.admission_sha256,
        project_id=request.project_id,
        predecessor_project_head_id=authority.active_project_head.project_head_id,
        created_at=timestamp,
    )
    return _replace_v2_task(
        m2.TaskV2.model_construct(
            schema_version="2",
            task_id=task_id,
            project_id=request.project_id,
            admission=admission,
            attempts=[first_attempt],
            authoritative_attempt_id=None,
            successor_transition=None,
            state="admitted",
            created_at=timestamp,
            updated_at=timestamp,
            etag=f'"{"0" * 64}"',
        )
    )


def _task_envelope_sha256(
    *,
    project_id: str,
    predecessor_project_head: m2.ProjectHeadRefV2,
    workspace_snapshot: m2.WorkspaceSnapshotRefV2,
    project_config_sha256: str,
) -> str:
    """Derive the closed task-envelope identity from Daemon-owned authority."""

    return hashlib.sha256(
        _v2_json_bytes(
            {
                "project_config_sha256": project_config_sha256,
                "project_id": project_id,
                "schema_version": "2",
                "workspace_snapshot": workspace_snapshot.model_dump(mode="json"),
                "predecessor_project_head": predecessor_project_head.model_dump(mode="json"),
            }
        )
    ).hexdigest()


def _v2_timestamp(value: datetime) -> str:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise ValueError("v2 clock must return a timezone-aware datetime")
    normalized = value.astimezone(timezone.utc)
    return normalized.isoformat(timespec="microseconds").replace("+00:00", "Z")


def _v2_successor_timestamp(
    current: m2.SuccessorTransitionV2,
    value: datetime,
) -> str:
    candidate = _v2_timestamp(value)
    if candidate > current.updated_at:
        return candidate
    previous = datetime.fromisoformat(current.updated_at.replace("Z", "+00:00"))
    return _v2_timestamp(previous + timedelta(microseconds=1))


def _replace_v2_task(task: m2.TaskV2, **changes: object) -> m2.TaskV2:
    data: dict[str, object] = {
        "schema_version": task.schema_version,
        "task_id": task.task_id,
        "project_id": task.project_id,
        "admission": task.admission,
        "attempts": list(task.attempts),
        "authoritative_attempt_id": task.authoritative_attempt_id,
        "successor_transition": task.successor_transition,
        "state": task.state,
        "created_at": task.created_at,
        "updated_at": task.updated_at,
    }
    data.update(changes)
    provisional = m2.TaskV2.model_construct(
        **data,
        etag=f'"{"0" * 64}"',
    )
    etag_payload = _v2_json_bytes(provisional.model_dump(mode="json", exclude={"etag"}))
    data["etag"] = f'"{hashlib.sha256(etag_payload).hexdigest()}"'
    return m2.TaskV2.model_validate(data)


def _load_v2_task_closure(
    connection: sqlite3.Connection,
    task_id: str,
) -> m2.TaskV2:
    row = connection.execute(
        "SELECT project_id, task_admission_id, admission_sha256, idempotency_key, "
        "request_sha256, request_json, task_json, closed FROM tasks WHERE task_id = ?",
        (task_id,),
    ).fetchone()
    if row is None:
        raise ScienceTaskNotFoundV2("v2 Task was not found")
    request_json = bytes(row["request_json"])
    request = _v2_model_from_bytes(m2.TaskSubmitRequestV2, request_json)
    task = _v2_model_from_bytes(m2.TaskV2, bytes(row["task_json"]))
    if (
        task.task_id != task_id
        or task.project_id != row["project_id"]
        or request.project_id != task.project_id
        or row["task_admission_id"] != task.admission.task_admission_id
        or row["admission_sha256"] != task.admission.admission_sha256
        or hashlib.sha256(request_json).hexdigest() != row["request_sha256"]
        or not isinstance(row["idempotency_key"], str)
        or _v2_idempotency_key(str(row["idempotency_key"])) != row["idempotency_key"]
        or bool(row["closed"]) != (task.state in {"closed", "completed"})
    ):
        raise ScienceTaskStoreV2Error("persisted v2 Task row is inconsistent")
    expected = _replace_v2_task(task)
    if expected.etag != task.etag:
        raise ScienceTaskStoreV2Error("persisted v2 Task ETag is inconsistent")

    admission_row = connection.execute(
        "SELECT task_id, admission_sha256, admission_json FROM task_admissions "
        "WHERE task_admission_id = ?",
        (task.admission.task_admission_id,),
    ).fetchone()
    if admission_row is None:
        raise ScienceTaskStoreV2Error("persisted v2 Task admission is missing")
    admission = _v2_model_from_bytes(
        m2.TaskAdmissionRefV2,
        bytes(admission_row["admission_json"]),
    )
    admitted_authority = ScienceProjectAdmissionAuthorityV2(
        project_id=admission.project_id,
        active_project_head=admission.predecessor_project_head,
        project_config_sha256=admission.project_config_sha256,
        workspace_snapshot=admission.workspace_snapshot,
        normalized_evolution_intent_sha256=(admission.normalized_evolution_intent_sha256),
    )
    if (
        admission != task.admission
        or admission_row["task_id"] != task.task_id
        or admission_row["admission_sha256"] != admission.admission_sha256
        or m2.task_admission_sha256_for(admission) != admission.admission_sha256
        or request.expected_project_admission_etag != admitted_authority.project_etag
        or request.expected_project_head_id != admission.predecessor_project_head.project_head_id
        or request.expected_project_head_manifest_sha256
        != admission.predecessor_project_head.manifest_sha256
        or request.expected_project_config_sha256 != admission.project_config_sha256
        or admission.task_envelope_sha256
        != _task_envelope_sha256(
            project_id=admission.project_id,
            predecessor_project_head=admission.predecessor_project_head,
            workspace_snapshot=admission.workspace_snapshot,
            project_config_sha256=admission.project_config_sha256,
        )
        or admission.registry_sha256 != admission.predecessor_project_head.registry_sha256
    ):
        raise ScienceTaskStoreV2Error("persisted v2 Task admission closure is inconsistent")

    attempt_rows = connection.execute(
        "SELECT attempt_id, ordinal, attempt_json FROM attempts WHERE task_id = ? "
        "ORDER BY ordinal LIMIT 101",
        (task.task_id,),
    ).fetchall()
    if len(attempt_rows) > 100:
        raise ScienceTaskStoreV2Error("persisted v2 Attempt inventory exceeds its bound")
    attempts = [
        _v2_model_from_bytes(m2.AttemptRefV2, bytes(item["attempt_json"])) for item in attempt_rows
    ]
    if attempts != task.attempts or any(
        item["attempt_id"] != attempt.attempt_id or item["ordinal"] != attempt.ordinal
        for item, attempt in zip(attempt_rows, attempts, strict=True)
    ):
        raise ScienceTaskStoreV2Error("persisted v2 Attempt closure is inconsistent")
    transition_rows = connection.execute(
        "SELECT transition_json FROM successor_transitions WHERE task_id = ? LIMIT 2",
        (task.task_id,),
    ).fetchall()
    if task.successor_transition is None:
        if transition_rows:
            raise ScienceTaskStoreV2Error("persisted v2 Task omits its successor transition")
    else:
        if len(transition_rows) != 1:
            raise ScienceTaskStoreV2Error(
                "persisted v2 Task successor transition is missing or duplicated"
            )
        transition = _v2_model_from_bytes(
            m2.SuccessorTransitionV2,
            bytes(transition_rows[0]["transition_json"]),
        )
        nonterminal = {
            "pending",
            "sealing_dataset",
            "running_methods",
            "validating",
            "materializing",
            "committing",
        }
        if (
            transition.transition != task.successor_transition
            or (task.state == "waiting_for_successor" and transition.state not in nonterminal)
            or (task.state == "failed" and transition.state != "failed")
            or (task.state == "completed" and transition.state not in {"committed", "cancelled"})
            or (
                task.state == "completed"
                and transition.state == "cancelled"
                and transition.transition.successor_project_head is None
            )
            or task.state == "closed"
            or task.state not in {"waiting_for_successor", "failed", "completed", "closed"}
        ):
            raise ScienceTaskStoreV2Error("persisted v2 Task successor state is inconsistent")
    return task


def _load_v2_attempt(
    connection: sqlite3.Connection,
    *,
    task_id: str,
    attempt_id: str,
) -> m2.AttemptRefV2:
    row = connection.execute(
        "SELECT ordinal, attempt_json FROM attempts WHERE task_id = ? AND attempt_id = ?",
        (task_id, attempt_id),
    ).fetchone()
    if row is None:
        raise ScienceAttemptNotFoundV2("v2 Attempt was not found")
    attempt = _v2_model_from_bytes(m2.AttemptRefV2, bytes(row["attempt_json"]))
    if (
        attempt.task_id != task_id
        or attempt.attempt_id != attempt_id
        or attempt.ordinal != row["ordinal"]
    ):
        raise ScienceTaskStoreV2Error("persisted v2 Attempt row is inconsistent")
    return attempt


def _replace_v2_execution_record(
    record: ScienceAttemptExecutionRecordV2,
    **changes: object,
) -> ScienceAttemptExecutionRecordV2:
    data = record.model_dump(mode="python")
    data.update(changes)
    return ScienceAttemptExecutionRecordV2.model_validate(data)


def _load_v2_attempt_execution_optional(
    connection: sqlite3.Connection,
    attempt_id: str,
) -> ScienceAttemptExecutionRecordV2 | None:
    row = connection.execute(
        "SELECT task_id, state, receipt_sha256, session_result_sha256, "
        "length(CAST(session_result_json AS BLOB)) AS session_result_byte_size, "
        "execution_json "
        "FROM attempt_executions WHERE attempt_id = ?",
        (attempt_id,),
    ).fetchone()
    if row is None:
        return None
    record = _v2_model_from_bytes(
        ScienceAttemptExecutionRecordV2,
        bytes(row["execution_json"]),
    )
    expected_receipt_sha256 = None if record.receipt is None else record.receipt.receipt_sha256
    if (
        record.attempt_id != attempt_id
        or record.task_id != row["task_id"]
        or record.state != row["state"]
        or row["receipt_sha256"] != expected_receipt_sha256
        or (
            record.receipt is not None
            and science_attempt_execution_receipt_sha256(record.receipt)
            != record.receipt.receipt_sha256
        )
    ):
        raise ScienceTaskStoreV2Error("persisted v2 Attempt execution row is inconsistent")
    has_result = row["session_result_sha256"] is not None
    has_result_bytes = row["session_result_byte_size"] is not None
    if has_result != has_result_bytes or (record.state == "captured") != has_result:
        raise ScienceTaskStoreV2Error("persisted v2 Attempt result authority is inconsistent")
    if has_result:
        captured_result = _load_v2_captured_session_result(connection, attempt_id)
        if (
            record.receipt is None
            or record.evidence is None
            or science_session_result_sha256(captured_result)
            != record.receipt.session_result_sha256
            or record.evidence.session_result_sha256 != record.receipt.session_result_sha256
        ):
            raise ScienceTaskStoreV2Error(
                "persisted v2 Attempt result differs from its terminal receipt"
            )
    attempt = _load_v2_attempt(
        connection,
        task_id=record.task_id,
        attempt_id=record.attempt_id,
    )
    if attempt.attempt_id != record.attempt_id:
        raise ScienceTaskStoreV2Error("persisted v2 Attempt execution lost its Attempt")
    return record


def _load_v2_captured_session_result(
    connection: sqlite3.Connection,
    attempt_id: str,
) -> SessionResult:
    row = connection.execute(
        "SELECT session_result_sha256, "
        "length(CAST(session_result_json AS BLOB)) AS byte_size, "
        "CASE WHEN length(CAST(session_result_json AS BLOB)) <= ? "
        "THEN session_result_json ELSE NULL END AS guarded_json "
        "FROM attempt_executions WHERE attempt_id = ?",
        (MAX_CAPTURED_SESSION_RESULT_BYTES, attempt_id),
    ).fetchone()
    if row is None or row["session_result_sha256"] is None or row["byte_size"] is None:
        raise ScienceTaskStoreV2Error("captured v2 Attempt result is missing")
    byte_size = int(row["byte_size"])
    if byte_size < 1 or byte_size > MAX_CAPTURED_SESSION_RESULT_BYTES:
        raise ScienceTaskStoreV2Error("captured v2 Attempt result exceeds its byte budget")
    guarded = row["guarded_json"]
    if guarded is None:
        raise ScienceTaskStoreV2Error("captured v2 Attempt result was not read safely")
    payload = bytes(guarded)
    if (
        len(payload) != byte_size
        or hashlib.sha256(payload).hexdigest() != row["session_result_sha256"]
    ):
        raise ScienceTaskStoreV2Error("captured v2 Attempt result digest is inconsistent")
    try:
        result = SessionResult.model_validate_json(payload)
        result = canonical_subscription_session_result(result)
    except (TypeError, ValueError) as exc:
        raise ScienceTaskStoreV2Error("captured v2 Attempt result is invalid") from exc
    if science_session_result_bytes(result) != payload:
        raise ScienceTaskStoreV2Error("captured v2 Attempt result is not canonical")
    return result


def _load_v2_attempt_execution(
    connection: sqlite3.Connection,
    attempt_id: str,
) -> ScienceAttemptExecutionRecordV2:
    record = _load_v2_attempt_execution_optional(connection, attempt_id)
    if record is None:
        raise ScienceAttemptNotFoundV2("v2 Attempt execution was not found")
    return record


def _update_v2_attempt_execution(
    connection: sqlite3.Connection,
    record: ScienceAttemptExecutionRecordV2,
) -> None:
    receipt_sha256 = None if record.receipt is None else record.receipt.receipt_sha256
    updated = connection.execute(
        "UPDATE attempt_executions SET state = ?, receipt_sha256 = ?, "
        "execution_json = ?, resource_version = resource_version + 1 "
        "WHERE attempt_id = ? AND task_id = ?",
        (
            record.state,
            receipt_sha256,
            _v2_model_bytes(record),
            record.attempt_id,
            record.task_id,
        ),
    ).rowcount
    if updated != 1:
        raise ScienceTaskStoreV2Error("v2 Attempt execution update lost ownership")


def _validate_v2_execution_ownership(
    task: m2.TaskV2,
    record: ScienceAttemptExecutionRecordV2,
) -> None:
    if record.task_id != task.task_id or all(
        item.attempt_id != record.attempt_id for item in task.attempts
    ):
        raise ScienceTaskStoreV2Error("v2 Attempt execution does not belong to the immutable Task")
    if record.state == "captured":
        if task.authoritative_attempt_id != record.attempt_id or task.state not in {
            "waiting_for_successor",
            "completed",
            "failed",
        }:
            raise ScienceTaskStoreV2Error(
                "captured v2 Attempt does not own the authoritative Task result"
            )
    elif task.authoritative_attempt_id == record.attempt_id:
        raise ScienceTaskStoreV2Error("non-captured v2 Attempt unexpectedly owns the Task result")


def _validate_v2_terminal_execution_closure(
    *,
    connection: sqlite3.Connection,
    task: m2.TaskV2,
    attempt_id: str,
    receipt: ScienceAttemptExecutionReceiptV2,
    evidence: ScienceAttemptExecutionEvidenceV2,
    successor_plan: ScienceSuccessorPlanV2,
    terminal_result: SessionResult,
) -> None:
    admission = task.admission
    predecessor = admission.predecessor_project_head
    attempt = next(
        (item for item in task.attempts if item.attempt_id == attempt_id),
        None,
    )
    if attempt is None:
        raise ScienceTaskPreconditionFailedV2(
            "terminal execution Attempt does not belong to the immutable Task"
        )
    rollout_rows = connection.execute(
        "SELECT attempt_id, task_id, rollout_task_id, session_id, "
        "generation_digest, registry_digest, framework_lock_digest, payload_sha256 "
        "FROM attempt_run_admissions WHERE attempt_id = ? AND operation = ? LIMIT 2",
        (attempt_id, RunAdmissionOperation.ROLLOUT_TASK_SUBMIT.value),
    ).fetchall()
    if len(rollout_rows) != 1:
        raise ScienceTaskPreconditionFailedV2(
            "terminal execution has no exact pre-registered rollout authority"
        )
    rollout = rollout_rows[0]
    workspace_result = terminal_result.workspace_result
    if (
        rollout["attempt_id"] != attempt_id
        or rollout["task_id"] != task.task_id
        or rollout["rollout_task_id"] != receipt.rollout_task_id
        or rollout["session_id"] != ""
        or rollout["generation_digest"] != receipt.service_generation_sha256
        or rollout["registry_digest"] != receipt.registry_sha256
        or rollout["framework_lock_digest"] != receipt.framework_lock_sha256
        or rollout["payload_sha256"] != receipt.rollout_payload_sha256
        or receipt.task_id != task.task_id
        or receipt.attempt_id != attempt.attempt_id
        or receipt.task_admission_id != admission.task_admission_id
        or receipt.admission_sha256 != admission.admission_sha256
        or receipt.project_id != task.project_id
        or receipt.predecessor_project_head_id != predecessor.project_head_id
        or receipt.predecessor_project_head_manifest_sha256 != predecessor.manifest_sha256
        or receipt.workspace_snapshot_id != admission.workspace_snapshot.workspace_snapshot_id
        or receipt.workspace_manifest_sha256 != admission.workspace_snapshot.manifest_sha256
        or receipt.evolution_revision_id != predecessor.evolution_revision.evolution_revision_id
        or receipt.evolution_revision_manifest_sha256
        != predecessor.evolution_revision.manifest_sha256
        or receipt.runtime_context_snapshot_id
        != predecessor.runtime_context_snapshot.runtime_context_snapshot_id
        or receipt.runtime_context_manifest_sha256
        != predecessor.runtime_context_snapshot.manifest_sha256
        or receipt.effective_execution_snapshot_id
        != predecessor.effective_execution_snapshot.effective_execution_snapshot_id
        or receipt.effective_execution_snapshot_sha256
        != predecessor.effective_execution_snapshot.snapshot_sha256
        or receipt.registry_sha256 != admission.registry_sha256
        or receipt.capture_mode != predecessor.effective_execution_snapshot.capture_mode
        or receipt.token_level_metrics_available
        != predecessor.effective_execution_snapshot.token_level_metrics_available
        or evidence.task_id != task.task_id
        or evidence.attempt_id != attempt.attempt_id
        or evidence.rollout_task_id != receipt.rollout_task_id
        or evidence.session_id != receipt.session_id
        or evidence.session_result_sha256 != receipt.session_result_sha256
        or evidence.workspace_handoff_id != receipt.workspace_handoff_id
        or evidence.workspace_result_manifest_sha256 != receipt.workspace_result_manifest_sha256
        or evidence.capture_mode != receipt.capture_mode
        or evidence.token_level_metrics_available != receipt.token_level_metrics_available
        or evidence.session_status != receipt.terminal_status
        or terminal_result.task_id != receipt.rollout_task_id
        or terminal_result.session_id != receipt.session_id
        or science_session_result_sha256(terminal_result) != receipt.session_result_sha256
        or workspace_result is None
        or workspace_result.handoff_id != receipt.workspace_handoff_id
        or workspace_result.result_manifest_sha256 != receipt.workspace_result_manifest_sha256
        or workspace_result.attempt_id != attempt.attempt_id
        or workspace_result.task_admission_id != admission.task_admission_id
        or workspace_result.admission_sha256 != admission.admission_sha256
        or workspace_result.project_id != task.project_id
        or workspace_result.input_workspace_snapshot_id
        != admission.workspace_snapshot.workspace_snapshot_id
        or workspace_result.input_workspace_manifest_sha256
        != admission.workspace_snapshot.manifest_sha256
        or workspace_result.service_generation_sha256 != receipt.service_generation_sha256
        or workspace_result.registry_sha256 != receipt.registry_sha256
        or workspace_result.framework_lock_sha256 != receipt.framework_lock_sha256
        or terminal_result.metadata.get("policy_version") != evidence.policy_version
        or evidence.transcript_record_count != len(terminal_result.trajectory.traces)
        or evidence.workspace_archive_sha256 != workspace_result.output_archive.content_sha256
        or evidence.workspace_archive_byte_size != workspace_result.output_archive.byte_size
        or evidence.workspace_entry_count != workspace_result.output_archive.entry_count
        or evidence.workspace_extracted_byte_size
        != workspace_result.output_archive.extracted_byte_size
        or successor_plan.project_id != task.project_id
        or successor_plan.task_id != task.task_id
        or successor_plan.task_admission_id != admission.task_admission_id
        or successor_plan.admission_sha256 != admission.admission_sha256
        or successor_plan.accepted_attempt_id != attempt.attempt_id
        or successor_plan.predecessor_project_head_id != predecessor.project_head_id
        or successor_plan.normalized_evolution_intent_sha256
        != admission.normalized_evolution_intent_sha256
    ):
        raise ScienceTaskPreconditionFailedV2(
            "terminal execution evidence does not match immutable v2 admission"
        )


def _v2_run_admission_row(
    connection: sqlite3.Connection,
    check: GenerationBoundRunAdmissionCheck,
) -> sqlite3.Row | None:
    assert check.task_id is not None
    return connection.execute(
        "SELECT attempt_id, task_id, generation_digest, registry_digest, "
        "framework_lock_digest, payload_sha256 FROM attempt_run_admissions "
        "WHERE operation = ? AND rollout_task_id = ? AND session_id = ?",
        (check.operation.value, check.task_id, check.session_id or ""),
    ).fetchone()


def _v2_run_admission_matches(
    row: sqlite3.Row,
    check: GenerationBoundRunAdmissionCheck,
) -> bool:
    return all(
        row[column] == value
        for column, value in (
            ("generation_digest", check.generation_digest),
            ("registry_digest", check.registry_digest),
            ("framework_lock_digest", check.framework_lock_digest),
            ("payload_sha256", check.payload_sha256),
        )
    )


def _insert_v2_run_admission(
    connection: sqlite3.Connection,
    *,
    task_id: str,
    attempt_id: str,
    check: GenerationBoundRunAdmissionCheck,
) -> None:
    if check.task_id is None:
        raise ValueError("v2 run admission requires a rollout Task identity")
    per_attempt = int(
        connection.execute(
            "SELECT COUNT(*) FROM attempt_run_admissions WHERE attempt_id = ?",
            (attempt_id,),
        ).fetchone()[0]
    )
    total = int(connection.execute("SELECT COUNT(*) FROM attempt_run_admissions").fetchone()[0])
    if per_attempt >= _MAX_V2_RUN_ADMISSIONS_PER_ATTEMPT or total >= _MAX_V2_RUN_ADMISSIONS:
        raise ScienceTaskConflictV2("v2 run admission capacity is exhausted")
    try:
        connection.execute(
            "INSERT INTO attempt_run_admissions(attempt_id, task_id, operation, "
            "rollout_task_id, session_id, generation_digest, registry_digest, "
            "framework_lock_digest, payload_sha256) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                attempt_id,
                task_id,
                check.operation.value,
                check.task_id,
                check.session_id or "",
                check.generation_digest,
                check.registry_digest,
                check.framework_lock_digest,
                check.payload_sha256,
            ),
        )
    except sqlite3.IntegrityError as exc:
        raise ScienceTaskConflictV2("v2 run admission conflicts with existing authority") from exc


def _verify_v2_run_admission_inventory(connection: sqlite3.Connection) -> None:
    rows = connection.execute(
        "SELECT attempt_id, task_id, operation, rollout_task_id, session_id, "
        "generation_digest, registry_digest, framework_lock_digest, payload_sha256 "
        "FROM attempt_run_admissions ORDER BY operation, rollout_task_id, session_id "
        "LIMIT ?",
        (_MAX_V2_RUN_ADMISSIONS + 1,),
    ).fetchall()
    if len(rows) > _MAX_V2_RUN_ADMISSIONS:
        raise ScienceTaskStoreV2Error("v2 run admission inventory exceeds its bound")
    parents: dict[str, sqlite3.Row] = {}
    counts: dict[str, int] = {}
    for row in rows:
        try:
            operation = RunAdmissionOperation(str(row["operation"]))
        except ValueError as exc:
            raise ScienceTaskStoreV2Error(
                "persisted v2 run admission operation is invalid"
            ) from exc
        attempt_id = str(row["attempt_id"])
        task_id = str(row["task_id"])
        rollout_task_id = str(row["rollout_task_id"])
        session_id = str(row["session_id"])
        record = _load_v2_attempt_execution(connection, attempt_id)
        task = _load_v2_task_closure(connection, task_id)
        _validate_v2_execution_ownership(task, record)
        if record.task_id != task_id:
            raise ScienceTaskStoreV2Error("persisted v2 run admission crosses Task ownership")
        counts[attempt_id] = counts.get(attempt_id, 0) + 1
        if counts[attempt_id] > _MAX_V2_RUN_ADMISSIONS_PER_ATTEMPT:
            raise ScienceTaskStoreV2Error(
                "v2 per-Attempt run admission inventory exceeds its bound"
            )
        for column in (
            "generation_digest",
            "registry_digest",
            "framework_lock_digest",
            "payload_sha256",
        ):
            if re.fullmatch(r"[0-9a-f]{64}", str(row[column]), flags=re.ASCII) is None:
                raise ScienceTaskStoreV2Error("persisted v2 run admission digest is invalid")
        if row["registry_digest"] != task.admission.registry_sha256:
            raise ScienceTaskStoreV2Error(
                "persisted v2 run admission registry differs from admission"
            )
        if operation is RunAdmissionOperation.ROLLOUT_TASK_SUBMIT:
            if session_id or rollout_task_id in parents:
                raise ScienceTaskStoreV2Error(
                    "persisted v2 rollout admission identity is ambiguous"
                )
            parents[rollout_task_id] = row
        elif not session_id:
            raise ScienceTaskStoreV2Error(
                "persisted v2 gateway admission lacks a session identity"
            )

    for row in rows:
        operation = RunAdmissionOperation(str(row["operation"]))
        if operation is RunAdmissionOperation.ROLLOUT_TASK_SUBMIT:
            continue
        parent = parents.get(str(row["rollout_task_id"]))
        if parent is None or any(
            row[column] != parent[column]
            for column in (
                "attempt_id",
                "task_id",
                "generation_digest",
                "registry_digest",
                "framework_lock_digest",
            )
        ):
            raise ScienceTaskStoreV2Error(
                "persisted v2 gateway admission has no exact rollout parent"
            )


def page_items(
    items: Sequence[_T],
    *,
    limit: int,
    after: str | None,
    query: str,
) -> tuple[list[_T], str | None, bool]:
    offset = _decode_cursor(after, query) if after is not None else 0
    selected = list(items[offset : offset + limit + 1])
    has_more = len(selected) > limit
    selected = selected[:limit]
    next_cursor = _encode_cursor(query, offset + limit) if has_more else None
    return selected, next_cursor, has_more


def _encode_cursor(query: str, offset: int) -> str:
    payload = _json_bytes({"offset": offset, "query": hashlib.sha256(query.encode()).hexdigest()})
    return base64.urlsafe_b64encode(payload).decode("ascii").rstrip("=")


def _decode_cursor(value: str, query: str) -> int:
    if not isinstance(value, str) or not 1 <= len(value) <= 512:
        raise ValueError("science run cursor is invalid")
    try:
        padded = value + "=" * (-len(value) % 4)
        payload = json.loads(base64.urlsafe_b64decode(padded.encode("ascii")))
    except (ValueError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError("science run cursor is invalid") from exc
    expected = hashlib.sha256(query.encode()).hexdigest()
    if (
        not isinstance(payload, dict)
        or set(payload) != {"offset", "query"}
        or not isinstance(payload["offset"], int)
        or isinstance(payload["offset"], bool)
        or not 0 <= payload["offset"] <= _MAX_RUNS
        or payload["query"] != expected
    ):
        raise ValueError("science run cursor is invalid")
    return payload["offset"]


def _artifact_model(payload: bytes | str) -> m.ArtifactSummaryV1:
    try:
        value = json.loads(payload)
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise ScienceRunStoreError("persisted artifact summary is invalid") from exc
    artifact_type = value.get("artifact_type") if isinstance(value, dict) else None
    model_type = {
        str(m.ArtifactType.TEXT_MEMORY): m.TextMemoryArtifactSummaryV1,
        str(m.ArtifactType.SKILL_BUNDLE): m.SkillBundleArtifactSummaryV1,
        str(m.ArtifactType.AGENT_SYSTEM): m.AgentSystemArtifactSummaryV1,
        str(m.ArtifactType.PARAMETRIC_MEMORY): m.ParametricMemoryArtifactSummaryV1,
    }.get(str(artifact_type))
    if model_type is None:
        raise ScienceRunStoreError("persisted artifact type is invalid")
    return _model(model_type, payload)


def _existing_create_run(
    connection: sqlite3.Connection,
    *,
    project_id: str,
    idempotency_key: str,
    request_digest: str,
    request_json: bytes,
) -> m.RunV1 | None:
    existing = connection.execute(
        "SELECT request_digest, request_json, run_json FROM runs "
        "WHERE project_id = ? AND idempotency_key = ?",
        (project_id, idempotency_key),
    ).fetchone()
    if existing is None:
        return None
    if (
        existing["request_digest"] != request_digest
        or bytes(existing["request_json"]) != request_json
    ):
        raise ScienceRunIdempotencyConflict("run idempotency key was reused")
    return _model(m.RunV1, existing["run_json"])


def _pending_create_run(
    connection: sqlite3.Connection,
    *,
    project_id: str,
    idempotency_key: str,
    request_digest: str,
    request_json: bytes,
) -> str | None:
    pending = connection.execute(
        "SELECT run_id, request_digest, request_json FROM pending_run_creates "
        "WHERE project_id = ? AND idempotency_key = ?",
        (project_id, idempotency_key),
    ).fetchone()
    if pending is None:
        return None
    if (
        pending["request_digest"] != request_digest
        or bytes(pending["request_json"]) != request_json
    ):
        raise ScienceRunIdempotencyConflict("run idempotency key was reused")
    run_id = pending["run_id"]
    if not isinstance(run_id, str) or not run_id.startswith("run-") or len(run_id) != 36:
        raise ScienceRunStoreError("pending science run identity is invalid")
    return run_id


def _project_in_flight_owner(
    connection: sqlite3.Connection,
    project_id: str,
    *,
    allowed_run_id: str | None = None,
) -> ProjectInFlightOwner | None:
    pending = connection.execute(
        "SELECT run_id FROM pending_run_creates WHERE project_id = ? LIMIT 2",
        (project_id,),
    ).fetchall()
    owners = [
        ProjectInFlightOwner(
            project_id=project_id,
            run_id=str(row["run_id"]),
            source="pending_create",
        )
        for row in pending
        if row["run_id"] != allowed_run_id
    ]
    rows = connection.execute(
        "SELECT run_id, run_json FROM runs "
        "WHERE project_id = ? AND deleted = 0 ORDER BY run_id LIMIT ?",
        (project_id, _MAX_RUNS + 1),
    ).fetchall()
    if len(rows) > _MAX_RUNS:
        raise ScienceRunStoreError("science run project inventory exceeds its bound")
    for row in rows:
        run = _model(m.RunV1, row["run_json"])
        if run.id != row["run_id"] or run.project_id != project_id:
            raise ScienceRunStoreError("science run project authority is invalid")
        if run.id != allowed_run_id and _run_retains_project_authority(run):
            owners.append(
                ProjectInFlightOwner(
                    project_id=project_id,
                    run_id=run.id,
                    source="run",
                )
            )
    if len(owners) > 1:
        raise ScienceRunStoreError("project has multiple science run authorities")
    return None if not owners else owners[0]


def _run_retains_project_authority(run: m.RunV1) -> bool:
    if run.status in _IN_FLIGHT_STATUSES:
        return True
    return bool(
        run.status is m.RunStatus.FAILED
        and run.current_error is not None
        and run.current_error.retryable
        and run.current_attempt_id is not None
        and run.admitted_at is not None
        and run.pinned_revision is not None
        and run.attempt_count < 100
    )


def _require_live_run(connection: sqlite3.Connection, run_id: str) -> m.RunV1:
    row = connection.execute(
        "SELECT run_json, deleted FROM runs WHERE run_id = ?", (run_id,)
    ).fetchone()
    if row is None or bool(row["deleted"]):
        raise ScienceRunNotFound("science run was not found")
    return _model(m.RunV1, row["run_json"])


def _model(model_type: type[_T], payload: bytes | str) -> _T:
    try:
        value = model_type.model_validate_json(payload)
    except Exception as exc:
        raise ScienceRunStoreError("persisted science run document is invalid") from exc
    raw = payload.encode("utf-8") if isinstance(payload, str) else bytes(payload)
    if _model_bytes(value) != raw:
        raise ScienceRunStoreError("persisted science run document is not canonical")
    return value


def _model_bytes(model: BaseModel) -> bytes:
    return _json_bytes(model.model_dump(mode="json"))


def _json_bytes(value: object) -> bytes:
    payload = json.dumps(
        value,
        ensure_ascii=True,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    if len(payload) > _MAX_DOCUMENT_BYTES:
        raise ScienceRunStoreError("science run document exceeds its byte bound")
    return payload


def _context_bytes(value: Mapping[str, Sequence[str]]) -> bytes:
    normalized: dict[str, list[str]] = {}
    total = 0
    if len(value) > 128:
        raise ValueError("revision context has too many artifact types")
    for artifact_type, artifact_ids in sorted(value.items()):
        if not isinstance(artifact_type, str) or not 1 <= len(artifact_type) <= 128:
            raise ValueError("revision context artifact type is invalid")
        normalized_ids = list(artifact_ids)
        if (
            len(normalized_ids) > 256
            or len(set(normalized_ids)) != len(normalized_ids)
            or any(
                not isinstance(item, str) or not 1 <= len(item) <= 256 for item in normalized_ids
            )
        ):
            raise ValueError("revision context artifact IDs are invalid")
        total += len(normalized_ids)
        if total > 1024:
            raise ValueError("revision context has too many artifacts")
        normalized[artifact_type] = normalized_ids
    return _json_bytes(normalized)


def _context_value(payload: bytes | str) -> dict[str, list[str]]:
    try:
        value = json.loads(payload)
        canonical = _context_bytes(value)
    except Exception as exc:
        raise ScienceRunStoreError("persisted revision context is invalid") from exc
    raw = payload.encode("utf-8") if isinstance(payload, str) else bytes(payload)
    if canonical != raw:
        raise ScienceRunStoreError("persisted revision context is not canonical")
    return {key: list(items) for key, items in value.items()}


def _object_value(payload: bytes | str, *, label: str) -> dict[str, object]:
    try:
        value = json.loads(payload)
        canonical = _json_bytes(value)
    except Exception as exc:
        raise ScienceRunStoreError(f"persisted {label} is invalid") from exc
    raw = payload.encode("utf-8") if isinstance(payload, str) else bytes(payload)
    if not isinstance(value, dict) or canonical != raw:
        raise ScienceRunStoreError(f"persisted {label} is not a canonical object")
    return value


__all__ = [
    "ProjectInFlightCoordinator",
    "ProjectInFlightOwner",
    "ScienceProjectInFlight",
    "ScienceProjectAdmissionAuthorityV2",
    "ScienceProjectReadinessBlockerV2",
    "ScienceAttemptNotFoundV2",
    "ScienceRunConflict",
    "ScienceRunCreateAdmission",
    "ScienceRunIdempotencyConflict",
    "ScienceRunNotFound",
    "ScienceRunPreconditionFailed",
    "ScienceRunStore",
    "ScienceRunStoreError",
    "ScienceTaskConflictV2",
    "ScienceTaskETagChangedV2",
    "ScienceTaskIdempotencyConflictV2",
    "ScienceTaskNotFoundV2",
    "ScienceTaskNotReadyV2",
    "ScienceTaskPreconditionFailedV2",
    "ScienceTaskProjectInFlightV2",
    "ScienceTaskStaleSubmissionV2",
    "ScienceTaskStoreV2",
    "ScienceTaskStoreV2Error",
    "ScienceTaskTerminalV2",
    "page_items",
]
