"""Exact fingerprints for EvolutionStore bootstrap and persisted schemas.

The allowlist is intentionally versioned and exact. To add a historical schema,
initialize an empty database with that revision's real ``EvolutionStore``, copy
its complete DDL (including post-``SCHEMA`` indexes) into a new immutable entry,
and add an independent positive fixture plus near-match negatives. Never widen an
existing entry or derive one from a required-column subset.

Partial entries exist only for historical migration fixtures. Callers must not
let a partial match claim pre-existing managed recovery state.
"""

from __future__ import annotations

from dataclasses import dataclass
import re
import sqlite3
from typing import Literal


LegacySchemaKind = Literal["none", "partial", "complete"]
StoreIdentitySchemaStatus = Literal["absent", "exact", "invalid"]
StoreSchemaClassificationKind = Literal[
    "empty",
    "legacy",
    "current",
    "identity_only",
    "legacy_identity_crash_window",
    "current_identity",
    "invalid",
]


@dataclass(frozen=True, slots=True)
class LegacySchemaMatch:
    """The classification and immutable identity of an allowlisted schema."""

    kind: LegacySchemaKind
    version: str | None

    @property
    def can_claim_managed_recovery_state(self) -> bool:
        return self.kind == "complete"


@dataclass(frozen=True, slots=True)
class StoreSchemaClassification:
    """Exact identity and underlying-schema classification for store startup."""

    kind: StoreSchemaClassificationKind
    identity: StoreIdentitySchemaStatus
    underlying: LegacySchemaMatch


@dataclass(frozen=True, slots=True)
class _ColumnFingerprint:
    position: int
    name: str
    declared_type: str
    not_null: int
    default_sql: tuple[str, ...] | None
    primary_key_position: int
    hidden: int


@dataclass(frozen=True, slots=True)
class _ForeignKeyFingerprint:
    identifier: int
    sequence: int
    referenced_table: str
    from_column: str
    to_column: str | None
    on_update: str
    on_delete: str
    match: str


@dataclass(frozen=True, slots=True)
class _IndexedTermFingerprint:
    sequence: int
    column_id: int
    column_name: str | None
    descending: int
    collation: str | None
    key: int


@dataclass(frozen=True, slots=True)
class _IndexFingerprint:
    name: str
    unique: int
    origin: str
    partial: int
    terms: tuple[_IndexedTermFingerprint, ...]
    sql: tuple[str, ...] | None


@dataclass(frozen=True, slots=True)
class _TableFingerprint:
    name: str
    table_kind: str
    without_rowid: int
    strict: int
    sql: tuple[str, ...] | None
    columns: tuple[_ColumnFingerprint, ...]
    foreign_keys: tuple[_ForeignKeyFingerprint, ...]
    indexes: tuple[_IndexFingerprint, ...]


@dataclass(frozen=True, slots=True)
class _SchemaObjectFingerprint:
    object_type: str
    name: str
    table_name: str
    sql: tuple[str, ...] | None


@dataclass(frozen=True, slots=True)
class _SchemaFingerprint:
    objects: tuple[_SchemaObjectFingerprint, ...]
    tables: tuple[_TableFingerprint, ...]


@dataclass(frozen=True, slots=True)
class _KnownSchema:
    match: LegacySchemaMatch
    ddl: str


_SQL_TOKEN_RE = re.compile(
    r"""
    '(?:''|[^'])*'
    | "(?:""|[^"])*"
    | `(?:``|[^`])*`
    | \[(?:\]\]|[^\]])*\]
    | [A-Za-z_][A-Za-z_0-9$]*
    | (?:\d+(?:\.\d*)?|\.\d+)(?:[Ee][+-]?\d+)?
    | <=|>=|<>|!=|==|<<|>>|->>|->|\|\|
    | [^\s]
    """,
    re.VERBOSE,
)


def _normalize_sql(value: str | None) -> tuple[str, ...] | None:
    if value is None:
        return None
    tokens = _SQL_TOKEN_RE.findall(value)
    return tuple(
        token if token[:1] in {"'", '"', "`", "["} else token.upper()
        for token in tokens
        if token != ";"
    )


def _normalize_declared_type(value: object) -> str:
    return " ".join(str(value).upper().split())


def _quote_identifier(value: str) -> str:
    return '"' + value.replace('"', '""') + '"'


def _table_metadata(connection: sqlite3.Connection) -> dict[str, tuple[str, int, int]]:
    metadata: dict[str, tuple[str, int, int]] = {}
    for row in connection.execute("PRAGMA main.table_list").fetchall():
        schema_name, table_name, table_kind, _column_count, without_rowid, strict = row
        if str(schema_name) != "main" or str(table_name).lower().startswith("sqlite_"):
            continue
        metadata[str(table_name)] = (
            str(table_kind),
            int(without_rowid),
            int(strict),
        )
    return metadata


def _column_fingerprints(
    connection: sqlite3.Connection,
    table_name: str,
) -> tuple[_ColumnFingerprint, ...]:
    quoted_table = _quote_identifier(table_name)
    return tuple(
        _ColumnFingerprint(
            position=int(row[0]),
            name=str(row[1]),
            declared_type=_normalize_declared_type(row[2]),
            not_null=int(row[3]),
            default_sql=_normalize_sql(None if row[4] is None else str(row[4])),
            primary_key_position=int(row[5]),
            hidden=int(row[6]),
        )
        for row in connection.execute(f"PRAGMA main.table_xinfo({quoted_table})").fetchall()
    )


def _foreign_key_fingerprints(
    connection: sqlite3.Connection,
    table_name: str,
) -> tuple[_ForeignKeyFingerprint, ...]:
    quoted_table = _quote_identifier(table_name)
    fingerprints = [
        _ForeignKeyFingerprint(
            identifier=int(row[0]),
            sequence=int(row[1]),
            referenced_table=str(row[2]),
            from_column=str(row[3]),
            to_column=None if row[4] is None else str(row[4]),
            on_update=str(row[5]),
            on_delete=str(row[6]),
            match=str(row[7]),
        )
        for row in connection.execute(f"PRAGMA main.foreign_key_list({quoted_table})").fetchall()
    ]
    return tuple(sorted(fingerprints, key=lambda item: (item.identifier, item.sequence)))


def _index_fingerprints(
    connection: sqlite3.Connection,
    table_name: str,
) -> tuple[_IndexFingerprint, ...]:
    quoted_table = _quote_identifier(table_name)
    fingerprints: list[_IndexFingerprint] = []
    for row in connection.execute(f"PRAGMA main.index_list({quoted_table})").fetchall():
        index_name = str(row[1])
        quoted_index = _quote_identifier(index_name)
        terms = tuple(
            _IndexedTermFingerprint(
                sequence=int(term[0]),
                column_id=int(term[1]),
                column_name=None if term[2] is None else str(term[2]),
                descending=int(term[3]),
                collation=None if term[4] is None else str(term[4]),
                key=int(term[5]),
            )
            for term in connection.execute(f"PRAGMA main.index_xinfo({quoted_index})").fetchall()
        )
        sql_row = connection.execute(
            "SELECT sql FROM main.sqlite_schema WHERE type = 'index' AND name = ?",
            (index_name,),
        ).fetchone()
        fingerprints.append(
            _IndexFingerprint(
                name=index_name,
                unique=int(row[2]),
                origin=str(row[3]),
                partial=int(row[4]),
                terms=terms,
                sql=_normalize_sql(None if sql_row is None else sql_row[0]),
            )
        )
    return tuple(sorted(fingerprints, key=lambda item: item.name))


def _schema_object_fingerprints(
    connection: sqlite3.Connection,
    *,
    excluded_table_names: frozenset[str] = frozenset(),
) -> tuple[_SchemaObjectFingerprint, ...]:
    fingerprints = [
        _SchemaObjectFingerprint(
            object_type=str(row[0]),
            name=str(row[1]),
            table_name=str(row[2]),
            sql=_normalize_sql(None if row[3] is None else str(row[3])),
        )
        for row in connection.execute(
            "SELECT type, name, tbl_name, sql FROM main.sqlite_schema"
        ).fetchall()
        if not str(row[1]).lower().startswith("sqlite_")
        and not (str(row[0]) == "table" and str(row[1]) in excluded_table_names)
    ]
    return tuple(
        sorted(
            fingerprints,
            key=lambda item: (item.object_type, item.name, item.table_name),
        )
    )


def _schema_fingerprint(
    connection: sqlite3.Connection,
    *,
    excluded_table_names: frozenset[str] = frozenset(),
) -> _SchemaFingerprint:
    metadata = _table_metadata(connection)
    table_rows = connection.execute(
        "SELECT name, sql FROM main.sqlite_schema WHERE type = 'table' ORDER BY name"
    ).fetchall()
    tables: list[_TableFingerprint] = []
    for row in table_rows:
        table_name = str(row[0])
        if table_name.lower().startswith("sqlite_") or table_name in excluded_table_names:
            continue
        table_kind, without_rowid, strict = metadata[table_name]
        tables.append(
            _TableFingerprint(
                name=table_name,
                table_kind=table_kind,
                without_rowid=without_rowid,
                strict=strict,
                sql=_normalize_sql(None if row[1] is None else str(row[1])),
                columns=_column_fingerprints(connection, table_name),
                foreign_keys=_foreign_key_fingerprints(connection, table_name),
                indexes=_index_fingerprints(connection, table_name),
            )
        )
    return _SchemaFingerprint(
        objects=_schema_object_fingerprints(
            connection,
            excluded_table_names=excluded_table_names,
        ),
        tables=tuple(tables),
    )


# Complete schema produced by EvolutionStore.initialize() at
# stable@8462ec039b530bf13005e0919e5c5b319950f9fd, the first-parent predecessor
# of d85df7461b7c66bf29acf982be1db0fb30a6f03b. The final index is created by
# _ensure_schema rather than the module-level SCHEMA script.
_STABLE_8462EC039B53_PRE_D85DF746_DDL = """
CREATE TABLE events (
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
CREATE TABLE datasets (
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
CREATE TABLE dataset_events (
    dataset_id TEXT NOT NULL,
    event_id TEXT NOT NULL,
    PRIMARY KEY(dataset_id, event_id)
);
CREATE TABLE jobs (
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
CREATE TABLE artifacts (
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
CREATE TABLE artifact_lineage (
    parent_artifact_id TEXT NOT NULL,
    child_artifact_id TEXT NOT NULL,
    relation TEXT NOT NULL,
    PRIMARY KEY(parent_artifact_id, child_artifact_id, relation)
);
CREATE TABLE contexts (
    context_id TEXT PRIMARY KEY,
    created_at TEXT NOT NULL,
    request_json TEXT NOT NULL,
    response_json TEXT NOT NULL,
    selected_artifact_ids_json TEXT NOT NULL
);
CREATE TABLE review_packets (
    packet_id TEXT PRIMARY KEY,
    packet_hash TEXT NOT NULL UNIQUE,
    packet_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE review_requests (
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
CREATE TABLE human_feedback (
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
CREATE TABLE feedback_applications (
    application_id TEXT PRIMARY KEY,
    feedback_id TEXT NOT NULL,
    target_type TEXT NOT NULL,
    target_id TEXT NOT NULL,
    consumed_by_method TEXT NOT NULL,
    consumed_in_job_id TEXT,
    effect_summary TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE human_query_decisions (
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
CREATE UNIQUE INDEX idx_review_requests_query_decision_id_unique
ON review_requests(query_decision_id)
WHERE query_decision_id IS NOT NULL;
CREATE UNIQUE INDEX idx_feedback_applications_natural_key_unique
ON feedback_applications(
    feedback_id,
    target_type,
    target_id,
    consumed_by_method,
    COALESCE(consumed_in_job_id, '')
);
"""


# Complete schema produced by EvolutionStore.initialize() at
# stable@de0481385cef87058efd884aab40f7fb95bf9e41. The final three indexes are
# created by _ensure_schema rather than the module-level SCHEMA script.
_STABLE_DE0481385CEF_DDL = """
CREATE TABLE events (
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
CREATE TABLE datasets (
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
CREATE TABLE dataset_events (
    dataset_id TEXT NOT NULL,
    event_id TEXT NOT NULL,
    PRIMARY KEY(dataset_id, event_id)
);
CREATE TABLE evolution_plans (
    plan_id TEXT PRIMARY KEY,
    schema_version TEXT NOT NULL,
    registry_snapshot_digest TEXT NOT NULL,
    plan_digest TEXT NOT NULL,
    plan_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE jobs (
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
CREATE TABLE artifacts (
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
CREATE TABLE artifact_lineage (
    parent_artifact_id TEXT NOT NULL,
    child_artifact_id TEXT NOT NULL,
    relation TEXT NOT NULL,
    PRIMARY KEY(parent_artifact_id, child_artifact_id, relation)
);
CREATE TABLE contexts (
    context_id TEXT PRIMARY KEY,
    created_at TEXT NOT NULL,
    request_json TEXT NOT NULL,
    response_json TEXT NOT NULL,
    selected_artifact_ids_json TEXT NOT NULL
);
CREATE TABLE review_packets (
    packet_id TEXT PRIMARY KEY,
    packet_hash TEXT NOT NULL UNIQUE,
    packet_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE review_requests (
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
CREATE TABLE human_feedback (
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
CREATE TABLE feedback_applications (
    application_id TEXT PRIMARY KEY,
    feedback_id TEXT NOT NULL,
    target_type TEXT NOT NULL,
    target_id TEXT NOT NULL,
    consumed_by_method TEXT NOT NULL,
    consumed_in_job_id TEXT,
    effect_summary TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE human_query_decisions (
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
CREATE UNIQUE INDEX idx_review_requests_query_decision_id_unique
ON review_requests(query_decision_id)
WHERE query_decision_id IS NOT NULL;
CREATE INDEX idx_artifacts_staging_job
ON artifacts(staging_job_id) WHERE staging_job_id IS NOT NULL;
CREATE UNIQUE INDEX idx_jobs_plan_target_unique
ON jobs(plan_id, target_id) WHERE plan_id IS NOT NULL;
CREATE UNIQUE INDEX idx_feedback_applications_natural_key_unique
ON feedback_applications(
    feedback_id,
    target_type,
    target_id,
    consumed_by_method,
    COALESCE(consumed_in_job_id, '')
);
"""


def _replace_exact_ddl(source: str, old: str, new: str) -> str:
    if source.count(old) != 1:
        raise RuntimeError("historical schema DDL replacement is not exact")
    return source.replace(old, new)


_FRESH_DE048_ARTIFACT_COLUMNS = """    manifest_path TEXT NOT NULL,
    manifest_json TEXT NOT NULL,
    lineage_json TEXT NOT NULL,
    compatibility_json TEXT NOT NULL,
    scores_json TEXT NOT NULL,
    tags_json TEXT NOT NULL,
    promoted INTEGER NOT NULL,
    staging_job_id TEXT
"""

_MIGRATED_DE048_ARTIFACT_COLUMNS = """    manifest_path TEXT NOT NULL,
    lineage_json TEXT NOT NULL,
    compatibility_json TEXT NOT NULL,
    scores_json TEXT NOT NULL,
    tags_json TEXT NOT NULL,
    promoted INTEGER NOT NULL,
    staging_job_id TEXT,
    manifest_json TEXT
"""

_PRE_DE048_ARTIFACT_COLUMNS = """    manifest_path TEXT NOT NULL,
    lineage_json TEXT NOT NULL,
    compatibility_json TEXT NOT NULL,
    scores_json TEXT NOT NULL,
    tags_json TEXT NOT NULL,
    promoted INTEGER NOT NULL,
    staging_job_id TEXT
"""

_FRESH_D85_JOBS_COLUMNS = """    lease_expires_at TEXT,
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
"""

_MIGRATED_D85_JOBS_COLUMNS = """    lease_expires_at TEXT,
    input_artifact_ids_json TEXT NOT NULL,
    config_json TEXT NOT NULL,
    error TEXT,
    attempt_count INTEGER NOT NULL,
    lease_duration_seconds INTEGER,
    plan_id TEXT,
    target_id TEXT,
    method_identity_digest TEXT,
    execution_envelope_json TEXT,
    execution_envelope_digest TEXT,
    declared_output_artifact_types_json TEXT
"""

_FRESH_REVIEW_REQUESTS_COLUMNS = """    assigned_to TEXT,
    reviewer_role TEXT,
    adjudication_rationale TEXT,
    priority INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
"""

_MIGRATED_REVIEW_REQUESTS_COLUMNS = """    assigned_to TEXT,
    reviewer_role TEXT,
    priority INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    adjudication_rationale TEXT
"""

# d85df7461b7c66bf29acf982be1db0fb30a6f03b and
# de0481385cef87058efd884aab40f7fb95bf9e41 are on stable's first-parent chain.
# SQLite ALTER TABLE appends columns, so migrated layouts differ from a fresh
# database created at de048138.
_STABLE_DE0481385CEF_FROM_D85DF746_DDL = _replace_exact_ddl(
    _STABLE_DE0481385CEF_DDL,
    _FRESH_DE048_ARTIFACT_COLUMNS,
    _MIGRATED_DE048_ARTIFACT_COLUMNS,
)
_STABLE_DE0481385CEF_FROM_PRE_D85DF746_DDL = _replace_exact_ddl(
    _STABLE_DE0481385CEF_FROM_D85DF746_DDL,
    _FRESH_D85_JOBS_COLUMNS,
    _MIGRATED_D85_JOBS_COLUMNS,
)
_STABLE_PRE_DE048_DDL = _replace_exact_ddl(
    _STABLE_DE0481385CEF_DDL,
    _FRESH_DE048_ARTIFACT_COLUMNS,
    _PRE_DE048_ARTIFACT_COLUMNS,
)
_STABLE_PRE_DE048_FROM_PRE_D85DF746_DDL = _replace_exact_ddl(
    _STABLE_PRE_DE048_DDL,
    _FRESH_D85_JOBS_COLUMNS,
    _MIGRATED_D85_JOBS_COLUMNS,
)

_CONTEXT_MATERIALIZATIONS_DDL = """
CREATE TABLE context_materializations (
    context_id TEXT PRIMARY KEY,
    created_at TEXT NOT NULL,
    registry_digest TEXT NOT NULL,
    request_digest TEXT NOT NULL,
    manifest_json TEXT NOT NULL,
    FOREIGN KEY(context_id) REFERENCES contexts(context_id) ON DELETE CASCADE
);
"""

_REVISION_LEDGER_DDL = """
CREATE TABLE execution_snapshots (
    execution_snapshot_id TEXT PRIMARY KEY,
    created_at TEXT NOT NULL,
    snapshot_digest TEXT NOT NULL UNIQUE,
    producer_id TEXT NOT NULL,
    snapshot_json TEXT NOT NULL
);
CREATE TABLE revisions (
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
CREATE TABLE revision_streams (
    stream_id TEXT PRIMARY KEY,
    active_revision_id TEXT NOT NULL UNIQUE,
    active_generation INTEGER NOT NULL CHECK(
        typeof(active_generation) = 'integer' AND active_generation >= 0
    ),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    FOREIGN KEY(active_revision_id) REFERENCES revisions(revision_id)
);
CREATE TABLE task_admissions (
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
CREATE INDEX idx_task_admissions_active_revision
ON task_admissions(pinned_revision_id) WHERE status = 'admitted';
"""

_PLAN_BOUND_JOB_RETRY_REQUESTS_DDL = """
CREATE TABLE plan_bound_job_retry_requests (
    job_id TEXT NOT NULL,
    retry_request_id TEXT NOT NULL,
    plan_id TEXT NOT NULL,
    target_id TEXT NOT NULL,
    claim_attempt_before INTEGER NOT NULL CHECK (claim_attempt_before >= 0),
    created_at TEXT NOT NULL,
    PRIMARY KEY(job_id, retry_request_id),
    FOREIGN KEY(job_id) REFERENCES jobs(job_id) ON DELETE RESTRICT
);
"""

_PLAN_BOUND_JOB_TRANSITION_BINDINGS_DDL = """
CREATE TABLE plan_bound_job_transition_bindings (
    job_id TEXT PRIMARY KEY,
    successor_transition_id TEXT NOT NULL,
    predecessor_successor_transition_id TEXT,
    created_at TEXT NOT NULL,
    FOREIGN KEY(job_id) REFERENCES jobs(job_id) ON DELETE RESTRICT
);
CREATE INDEX idx_plan_bound_job_transition_successor
ON plan_bound_job_transition_bindings(successor_transition_id, job_id);
"""

_SUCCESSOR_TRANSITION_DISCARDS_DDL = """
CREATE TABLE successor_transition_discards (
    successor_transition_id TEXT PRIMARY KEY,
    receipt_sha256 TEXT NOT NULL CHECK(length(receipt_sha256) = 64),
    receipt_json TEXT NOT NULL,
    discarded_at TEXT NOT NULL
) STRICT;
"""

_DATASET_CREATE_REQUESTS_V1_DDL = """
CREATE TABLE dataset_create_requests (
    idempotency_key TEXT PRIMARY KEY,
    request_sha256 TEXT NOT NULL CHECK (length(request_sha256) = 64),
    request_json TEXT NOT NULL,
    dataset_id TEXT NOT NULL UNIQUE,
    response_json TEXT,
    created_at TEXT NOT NULL
);
"""

_DATASET_CREATE_REQUESTS_DDL = """
CREATE TABLE dataset_create_requests (
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
"""

_STORE_IDENTITY_DDL = """
CREATE TABLE store_identity (
    singleton INTEGER PRIMARY KEY CHECK(singleton = 1),
    store_id TEXT NOT NULL UNIQUE,
    artifact_root TEXT NOT NULL,
    binding_state TEXT NOT NULL CHECK(binding_state IN ('pending', 'bound'))
);
"""


# Exact jobs-only migration fixture retained by test_planned_jobs.py and
# test_store_events.py. It is not a complete EvolutionStore database.
_LEGACY_JOBS_V1_DDL = """
CREATE TABLE jobs (
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
)
"""


# Exact review_requests-only migration fixture retained by test_hitl_reviews.py.
# It is not a complete EvolutionStore database.
_LEGACY_REVIEW_REQUESTS_V1_DDL = """
CREATE TABLE review_requests (
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
    priority INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
)
"""


_KNOWN_SCHEMAS = (
    _KnownSchema(
        match=LegacySchemaMatch(
            kind="complete",
            version="stable-8462ec039b53-pre-d85df746",
        ),
        ddl=_STABLE_8462EC039B53_PRE_D85DF746_DDL,
    ),
    _KnownSchema(
        match=LegacySchemaMatch(kind="complete", version="stable-pre-de0481385cef"),
        ddl=_STABLE_PRE_DE048_DDL,
    ),
    _KnownSchema(
        match=LegacySchemaMatch(
            kind="complete",
            version="stable-pre-de0481385cef-from-pre-d85df746",
        ),
        ddl=_STABLE_PRE_DE048_FROM_PRE_D85DF746_DDL,
    ),
    _KnownSchema(
        match=LegacySchemaMatch(kind="complete", version="stable-de0481385cef"),
        ddl=_STABLE_DE0481385CEF_DDL,
    ),
    _KnownSchema(
        match=LegacySchemaMatch(
            kind="complete",
            version="stable-de0481385cef-from-d85df746",
        ),
        ddl=_STABLE_DE0481385CEF_FROM_D85DF746_DDL,
    ),
    _KnownSchema(
        match=LegacySchemaMatch(
            kind="complete",
            version="stable-de0481385cef-from-pre-d85df746",
        ),
        ddl=_STABLE_DE0481385CEF_FROM_PRE_D85DF746_DDL,
    ),
    _KnownSchema(
        match=LegacySchemaMatch(kind="partial", version="legacy-jobs-v1"),
        ddl=_LEGACY_JOBS_V1_DDL,
    ),
    _KnownSchema(
        match=LegacySchemaMatch(
            kind="partial",
            version="legacy-review-requests-v1",
        ),
        ddl=_LEGACY_REVIEW_REQUESTS_V1_DDL,
    ),
)

_PRE_REVISION_CURRENT_SCHEMAS = (
    _KnownSchema(
        match=LegacySchemaMatch(
            kind="complete",
            version="current-migration-window-from-stable-pre-de0481385cef",
        ),
        ddl=_STABLE_PRE_DE048_DDL + _CONTEXT_MATERIALIZATIONS_DDL,
    ),
    _KnownSchema(
        match=LegacySchemaMatch(
            kind="complete",
            version=("current-migration-window-from-stable-pre-de0481385cef-from-pre-d85df746"),
        ),
        ddl=(_STABLE_PRE_DE048_FROM_PRE_D85DF746_DDL + _CONTEXT_MATERIALIZATIONS_DDL),
    ),
    _KnownSchema(
        match=LegacySchemaMatch(
            kind="complete",
            version="current-from-stable-de0481385cef",
        ),
        ddl=_STABLE_DE0481385CEF_DDL + _CONTEXT_MATERIALIZATIONS_DDL,
    ),
    _KnownSchema(
        match=LegacySchemaMatch(
            kind="complete",
            version="current-from-stable-de0481385cef-from-d85df746",
        ),
        ddl=(_STABLE_DE0481385CEF_FROM_D85DF746_DDL + _CONTEXT_MATERIALIZATIONS_DDL),
    ),
    _KnownSchema(
        match=LegacySchemaMatch(
            kind="complete",
            version="current-from-stable-de0481385cef-from-pre-d85df746",
        ),
        ddl=(_STABLE_DE0481385CEF_FROM_PRE_D85DF746_DDL + _CONTEXT_MATERIALIZATIONS_DDL),
    ),
    _KnownSchema(
        match=LegacySchemaMatch(
            kind="complete",
            version="current-from-legacy-jobs-v1",
        ),
        ddl=_replace_exact_ddl(
            _STABLE_DE0481385CEF_DDL + _CONTEXT_MATERIALIZATIONS_DDL,
            _FRESH_D85_JOBS_COLUMNS,
            _MIGRATED_D85_JOBS_COLUMNS,
        ),
    ),
    _KnownSchema(
        match=LegacySchemaMatch(
            kind="complete",
            version="current-from-legacy-review-requests-v1",
        ),
        ddl=_replace_exact_ddl(
            _STABLE_DE0481385CEF_DDL + _CONTEXT_MATERIALIZATIONS_DDL,
            _FRESH_REVIEW_REQUESTS_COLUMNS,
            _MIGRATED_REVIEW_REQUESTS_COLUMNS,
        ),
    ),
)

_REVISION_CURRENT_SCHEMAS = _PRE_REVISION_CURRENT_SCHEMAS + tuple(
    _KnownSchema(
        match=LegacySchemaMatch(
            kind="complete",
            version=f"revision-ledger-{known.match.version}",
        ),
        ddl=known.ddl + _REVISION_LEDGER_DDL,
    )
    for known in _PRE_REVISION_CURRENT_SCHEMAS
)

_PLAN_BOUND_RETRY_CURRENT_SCHEMAS = tuple(
    _KnownSchema(
        match=LegacySchemaMatch(
            kind="complete",
            version=f"{known.match.version}-plan-bound-retry",
        ),
        ddl=known.ddl + _PLAN_BOUND_JOB_RETRY_REQUESTS_DDL,
    )
    for known in _REVISION_CURRENT_SCHEMAS
)

_DATASET_CREATE_V1_CURRENT_SCHEMAS = tuple(
    _KnownSchema(
        match=LegacySchemaMatch(
            kind="complete",
            version=f"{known.match.version}-dataset-create-requests-v1",
        ),
        ddl=known.ddl + _DATASET_CREATE_REQUESTS_V1_DDL,
    )
    for known in _PLAN_BOUND_RETRY_CURRENT_SCHEMAS
)

_DATASET_CREATE_CURRENT_SCHEMAS = tuple(
    _KnownSchema(
        match=LegacySchemaMatch(
            kind="complete",
            version=f"{known.match.version}-dataset-create-requests",
        ),
        ddl=known.ddl + _DATASET_CREATE_REQUESTS_DDL,
    )
    for known in _PLAN_BOUND_RETRY_CURRENT_SCHEMAS
)

_PLAN_BOUND_TRANSITION_CURRENT_SCHEMAS = tuple(
    _KnownSchema(
        match=LegacySchemaMatch(
            kind="complete",
            version=f"{known.match.version}-transition-bindings",
        ),
        ddl=known.ddl + _PLAN_BOUND_JOB_TRANSITION_BINDINGS_DDL,
    )
    for known in (
        _PLAN_BOUND_RETRY_CURRENT_SCHEMAS
        + _DATASET_CREATE_V1_CURRENT_SCHEMAS
        + _DATASET_CREATE_CURRENT_SCHEMAS
    )
)

_SUCCESSOR_TRANSITION_DISCARD_CURRENT_SCHEMAS = tuple(
    _KnownSchema(
        match=LegacySchemaMatch(
            kind="complete",
            version=(
                f"{known.match.version}-successor-transition-discards"
            ),
        ),
        ddl=known.ddl + _SUCCESSOR_TRANSITION_DISCARDS_DDL,
    )
    for known in _PLAN_BOUND_TRANSITION_CURRENT_SCHEMAS
)

_CURRENT_SCHEMAS = (
    _REVISION_CURRENT_SCHEMAS
    + _PLAN_BOUND_RETRY_CURRENT_SCHEMAS
    + _DATASET_CREATE_V1_CURRENT_SCHEMAS
    + _DATASET_CREATE_CURRENT_SCHEMAS
    + _PLAN_BOUND_TRANSITION_CURRENT_SCHEMAS
    + _SUCCESSOR_TRANSITION_DISCARD_CURRENT_SCHEMAS
)


def _fingerprint_ddl(ddl: str) -> _SchemaFingerprint:
    connection = sqlite3.connect(":memory:")
    try:
        connection.executescript(ddl)
        return _schema_fingerprint(connection)
    finally:
        connection.close()


_KNOWN_FINGERPRINTS = tuple((known.match, _fingerprint_ddl(known.ddl)) for known in _KNOWN_SCHEMAS)
_CURRENT_FINGERPRINTS = tuple(
    (known.match, _fingerprint_ddl(known.ddl)) for known in _CURRENT_SCHEMAS
)
_EMPTY_FINGERPRINT = _fingerprint_ddl("")
_STORE_IDENTITY_TABLE_FINGERPRINT = _fingerprint_ddl(_STORE_IDENTITY_DDL).tables[0]


def _match_fingerprint(
    candidate: _SchemaFingerprint,
    known: tuple[tuple[LegacySchemaMatch, _SchemaFingerprint], ...],
) -> LegacySchemaMatch | None:
    for match, known_fingerprint in known:
        if candidate == known_fingerprint:
            return match
    return None


def inspect_store_identity_schema(
    connection: sqlite3.Connection,
) -> StoreIdentitySchemaStatus:
    """Inspect the exact ``store_identity`` table and its owned indexes."""

    objects = connection.execute(
        "SELECT type FROM main.sqlite_schema WHERE name = 'store_identity'"
    ).fetchall()
    if not objects:
        return "absent"
    if len(objects) != 1 or str(objects[0][0]) != "table":
        return "invalid"
    metadata = _table_metadata(connection)
    if "store_identity" not in metadata:
        return "invalid"
    row = connection.execute(
        "SELECT sql FROM main.sqlite_schema WHERE type = 'table' AND name = 'store_identity'"
    ).fetchone()
    if row is None:
        return "invalid"
    table_kind, without_rowid, strict = metadata["store_identity"]
    candidate = _TableFingerprint(
        name="store_identity",
        table_kind=table_kind,
        without_rowid=without_rowid,
        strict=strict,
        sql=_normalize_sql(None if row[0] is None else str(row[0])),
        columns=_column_fingerprints(connection, "store_identity"),
        foreign_keys=_foreign_key_fingerprints(connection, "store_identity"),
        indexes=_index_fingerprints(connection, "store_identity"),
    )
    return "exact" if candidate == _STORE_IDENTITY_TABLE_FINGERPRINT else "invalid"


def classify_store_schema(connection: sqlite3.Connection) -> StoreSchemaClassification:
    """Classify exact identity/bootstrap/current/crash-window store schemas."""

    identity = inspect_store_identity_schema(connection)
    if identity == "invalid":
        return StoreSchemaClassification(
            kind="invalid",
            identity=identity,
            underlying=LegacySchemaMatch(kind="none", version=None),
        )
    excluded = frozenset({"store_identity"}) if identity == "exact" else frozenset()
    candidate = _schema_fingerprint(connection, excluded_table_names=excluded)
    if candidate == _EMPTY_FINGERPRINT:
        kind: StoreSchemaClassificationKind = "identity_only" if identity == "exact" else "empty"
        return StoreSchemaClassification(
            kind=kind,
            identity=identity,
            underlying=LegacySchemaMatch(kind="none", version=None),
        )
    legacy = _match_fingerprint(candidate, _KNOWN_FINGERPRINTS)
    if legacy is not None:
        kind = "legacy_identity_crash_window" if identity == "exact" else "legacy"
        return StoreSchemaClassification(
            kind=kind,
            identity=identity,
            underlying=legacy,
        )
    current = _match_fingerprint(candidate, _CURRENT_FINGERPRINTS)
    if current is not None:
        kind = "current_identity" if identity == "exact" else "current"
        return StoreSchemaClassification(
            kind=kind,
            identity=identity,
            underlying=current,
        )
    return StoreSchemaClassification(
        kind="invalid",
        identity=identity,
        underlying=LegacySchemaMatch(kind="none", version=None),
    )


def identify_legacy_store_schema(connection: sqlite3.Connection) -> LegacySchemaMatch:
    """Return the exact allowlisted identity of ``connection``'s main schema."""

    candidate = _schema_fingerprint(connection)
    return _match_fingerprint(candidate, _KNOWN_FINGERPRINTS) or LegacySchemaMatch(
        kind="none",
        version=None,
    )


__all__ = [
    "LegacySchemaKind",
    "LegacySchemaMatch",
    "StoreIdentitySchemaStatus",
    "StoreSchemaClassification",
    "StoreSchemaClassificationKind",
    "classify_store_schema",
    "identify_legacy_store_schema",
    "inspect_store_identity_schema",
]
