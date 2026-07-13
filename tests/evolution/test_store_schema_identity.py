from __future__ import annotations

from contextlib import contextmanager
import sqlite3
from typing import Iterator

import pytest

from openevo.evolution.store_schema_identity import (
    classify_store_schema,
    identify_legacy_store_schema,
    inspect_store_identity_schema,
)
from openevo.evolution.store import EvolutionStore


# This is the schema produced by EvolutionStore.initialize() on
# stable@de0481385cef87058efd884aab40f7fb95bf9e41. Keep this fixture independent
# from the production allowlist so a changed allowlist cannot make the positive
# test pass by construction.
_STABLE_SCHEMA = """
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

_JOBS_ONLY_SCHEMA = """
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

_REVIEW_REQUESTS_ONLY_SCHEMA = """
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

_STORE_IDENTITY_SCHEMA = """
CREATE TABLE store_identity (
    singleton INTEGER PRIMARY KEY CHECK(singleton = 1),
    store_id TEXT NOT NULL UNIQUE,
    artifact_root TEXT NOT NULL,
    binding_state TEXT NOT NULL CHECK(binding_state IN ('pending', 'bound'))
);
"""

_CONTEXT_MATERIALIZATIONS_SCHEMA = """
CREATE TABLE context_materializations (
    context_id TEXT PRIMARY KEY,
    created_at TEXT NOT NULL,
    registry_digest TEXT NOT NULL,
    request_digest TEXT NOT NULL,
    manifest_json TEXT NOT NULL,
    FOREIGN KEY(context_id) REFERENCES contexts(context_id) ON DELETE CASCADE
);
"""

# Independent historical fixtures. These reproduce stable first-parent commits
# 8462ec039b530bf13005e0919e5c5b319950f9fd,
# d85df7461b7c66bf29acf982be1db0fb30a6f03b, and
# de0481385cef87058efd884aab40f7fb95bf9e41 without importing production DDL.
_FRESH_D85_SCHEMA = _STABLE_SCHEMA.replace(
    "    manifest_json TEXT NOT NULL,\n",
    "",
)

_PRE_D85_SCHEMA = """
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

_FRESH_D85_JOBS_SCHEMA = """CREATE TABLE jobs (
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
);"""

_EVOLUTION_PLANS_SCHEMA = """CREATE TABLE evolution_plans (
    plan_id TEXT PRIMARY KEY,
    schema_version TEXT NOT NULL,
    registry_snapshot_digest TEXT NOT NULL,
    plan_digest TEXT NOT NULL,
    plan_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);"""

_D85_MIGRATION_DDL = """
CREATE TABLE evolution_plans (
    plan_id TEXT PRIMARY KEY,
    schema_version TEXT NOT NULL,
    registry_snapshot_digest TEXT NOT NULL,
    plan_digest TEXT NOT NULL,
    plan_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);
ALTER TABLE artifacts ADD COLUMN staging_job_id TEXT;
CREATE INDEX idx_artifacts_staging_job
ON artifacts(staging_job_id) WHERE staging_job_id IS NOT NULL;
ALTER TABLE jobs ADD COLUMN lease_duration_seconds INTEGER;
ALTER TABLE jobs ADD COLUMN plan_id TEXT;
ALTER TABLE jobs ADD COLUMN target_id TEXT;
ALTER TABLE jobs ADD COLUMN method_identity_digest TEXT;
ALTER TABLE jobs ADD COLUMN execution_envelope_json TEXT;
ALTER TABLE jobs ADD COLUMN execution_envelope_digest TEXT;
ALTER TABLE jobs ADD COLUMN declared_output_artifact_types_json TEXT;
CREATE UNIQUE INDEX idx_jobs_plan_target_unique
ON jobs(plan_id, target_id) WHERE plan_id IS NOT NULL;
"""

_DE048_MIGRATION_DDL = "ALTER TABLE artifacts ADD COLUMN manifest_json TEXT"

_FRESH_REVIEW_REQUESTS_SCHEMA = """CREATE TABLE review_requests (
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
);"""

_CURRENT_WITHOUT_JOBS_SCHEMA = _STABLE_SCHEMA.replace(
    _FRESH_D85_JOBS_SCHEMA + "\n",
    "",
).replace(
    "CREATE UNIQUE INDEX idx_jobs_plan_target_unique\n"
    "ON jobs(plan_id, target_id) WHERE plan_id IS NOT NULL;\n",
    "",
)

_CURRENT_WITHOUT_REVIEW_REQUESTS_SCHEMA = _STABLE_SCHEMA.replace(
    _FRESH_REVIEW_REQUESTS_SCHEMA + "\n",
    "",
)

_JOBS_V1_TO_CURRENT_DDL = """
ALTER TABLE jobs ADD COLUMN lease_duration_seconds INTEGER;
ALTER TABLE jobs ADD COLUMN plan_id TEXT;
ALTER TABLE jobs ADD COLUMN target_id TEXT;
ALTER TABLE jobs ADD COLUMN method_identity_digest TEXT;
ALTER TABLE jobs ADD COLUMN execution_envelope_json TEXT;
ALTER TABLE jobs ADD COLUMN execution_envelope_digest TEXT;
ALTER TABLE jobs ADD COLUMN declared_output_artifact_types_json TEXT;
CREATE UNIQUE INDEX idx_jobs_plan_target_unique
ON jobs(plan_id, target_id) WHERE plan_id IS NOT NULL;
"""


@contextmanager
def _connection(schema: str) -> Iterator[sqlite3.Connection]:
    connection = sqlite3.connect(":memory:")
    try:
        connection.executescript(schema)
        yield connection
    finally:
        connection.close()


@pytest.fixture
def fresh_de048_connection() -> Iterator[sqlite3.Connection]:
    with _connection(_STABLE_SCHEMA) as connection:
        yield connection


@pytest.fixture
def pre_d85_connection() -> Iterator[sqlite3.Connection]:
    with _connection(_PRE_D85_SCHEMA) as connection:
        yield connection


@pytest.fixture
def d85_then_de048_connection() -> Iterator[sqlite3.Connection]:
    with _connection(_FRESH_D85_SCHEMA) as connection:
        connection.execute(_DE048_MIGRATION_DDL)
        yield connection


@pytest.fixture
def pre_d85_then_d85_then_de048_connection() -> Iterator[sqlite3.Connection]:
    with _connection(_PRE_D85_SCHEMA) as connection:
        connection.executescript(_D85_MIGRATION_DDL)
        connection.execute(_DE048_MIGRATION_DDL)
        yield connection


def _identity(schema: str):
    with _connection(schema) as connection:
        return identify_legacy_store_schema(connection)


def test_identifies_exact_stable_schema_as_complete() -> None:
    match = _identity(_STABLE_SCHEMA)

    assert match.kind == "complete"
    assert match.version == "stable-de0481385cef"
    assert match.can_claim_managed_recovery_state


def test_identifies_pre_de048_stable_schema_as_complete() -> None:
    match = _identity(_FRESH_D85_SCHEMA)

    assert match.kind == "complete"
    assert match.version == "stable-pre-de0481385cef"
    assert match.can_claim_managed_recovery_state


def test_identifies_original_pre_d85_complete_schema_without_migration(
    pre_d85_connection: sqlite3.Connection,
) -> None:
    match = identify_legacy_store_schema(pre_d85_connection)

    assert match.kind == "complete"
    assert match.version == "stable-8462ec039b53-pre-d85df746"
    assert match.can_claim_managed_recovery_state


def test_classifies_original_pre_d85_complete_schema_with_identity_absent(
    pre_d85_connection: sqlite3.Connection,
) -> None:
    classification = classify_store_schema(pre_d85_connection)

    assert classification.kind == "legacy"
    assert classification.identity == "absent"
    assert classification.underlying.kind == "complete"
    assert classification.underlying.version == "stable-8462ec039b53-pre-d85df746"


def test_classifies_original_pre_d85_identity_crash_window(
    pre_d85_connection: sqlite3.Connection,
) -> None:
    pre_d85_connection.executescript(_STORE_IDENTITY_SCHEMA)

    classification = classify_store_schema(pre_d85_connection)

    assert classification.kind == "legacy_identity_crash_window"
    assert classification.identity == "exact"
    assert classification.underlying.kind == "complete"
    assert classification.underlying.version == "stable-8462ec039b53-pre-d85df746"


def test_current_store_directly_upgrades_original_pre_d85_database(tmp_path) -> None:
    db_path = tmp_path / "pre-d85.db"
    artifact_root = tmp_path / "managed"
    with sqlite3.connect(db_path) as connection:
        connection.executescript(_PRE_D85_SCHEMA)

    store = EvolutionStore(db_path=db_path, artifact_root=artifact_root)
    store.initialize()

    with sqlite3.connect(db_path) as connection:
        classification = classify_store_schema(connection)
        job_columns = {
            str(row[1]) for row in connection.execute("PRAGMA table_info(jobs)").fetchall()
        }
        artifact_columns = {
            str(row[1]) for row in connection.execute("PRAGMA table_info(artifacts)").fetchall()
        }
    assert classification.kind == "current_identity"
    assert {
        "lease_duration_seconds",
        "plan_id",
        "target_id",
        "method_identity_digest",
        "execution_envelope_json",
        "execution_envelope_digest",
        "declared_output_artifact_types_json",
    }.issubset(job_columns)
    assert {"staging_job_id", "manifest_json"}.issubset(artifact_columns)


def test_identifies_fresh_d85_migrated_by_de048_as_complete(
    d85_then_de048_connection: sqlite3.Connection,
) -> None:
    match = identify_legacy_store_schema(d85_then_de048_connection)

    assert match.kind == "complete"
    assert match.version == "stable-de0481385cef-from-d85df746"
    assert match.can_claim_managed_recovery_state


def test_identifies_pre_d85_database_migrated_by_d85_and_de048_as_complete(
    pre_d85_then_d85_then_de048_connection: sqlite3.Connection,
) -> None:
    match = identify_legacy_store_schema(pre_d85_then_d85_then_de048_connection)

    assert match.kind == "complete"
    assert match.version == "stable-de0481385cef-from-pre-d85df746"
    assert match.can_claim_managed_recovery_state


def test_stable_history_fixtures_have_distinct_exact_layouts(
    fresh_de048_connection: sqlite3.Connection,
    d85_then_de048_connection: sqlite3.Connection,
    pre_d85_then_d85_then_de048_connection: sqlite3.Connection,
) -> None:
    fresh_artifact = fresh_de048_connection.execute("PRAGMA table_xinfo(artifacts)").fetchall()
    d85_artifact = d85_then_de048_connection.execute("PRAGMA table_xinfo(artifacts)").fetchall()
    fresh_jobs = fresh_de048_connection.execute("PRAGMA table_xinfo(jobs)").fetchall()
    pre_d85_jobs = pre_d85_then_d85_then_de048_connection.execute(
        "PRAGMA table_xinfo(jobs)"
    ).fetchall()

    assert [(row[1], row[3]) for row in fresh_artifact][-7:-5] == [
        ("manifest_json", 1),
        ("lineage_json", 1),
    ]
    assert [(row[1], row[3]) for row in d85_artifact][-2:] == [
        ("staging_job_id", 0),
        ("manifest_json", 0),
    ]
    assert [row[1] for row in fresh_jobs][10:13] == [
        "lease_duration_seconds",
        "input_artifact_ids_json",
        "config_json",
    ]
    assert [row[1] for row in pre_d85_jobs][10:16] == [
        "input_artifact_ids_json",
        "config_json",
        "error",
        "attempt_count",
        "lease_duration_seconds",
        "plan_id",
    ]


@pytest.mark.parametrize(
    ("schema", "version"),
    [
        (_JOBS_ONLY_SCHEMA, "legacy-jobs-v1"),
        (_REVIEW_REQUESTS_ONLY_SCHEMA, "legacy-review-requests-v1"),
    ],
)
def test_identifies_exact_historical_partial_schema(
    schema: str,
    version: str,
) -> None:
    match = _identity(schema)

    assert match.kind == "partial"
    assert match.version == version
    assert not match.can_claim_managed_recovery_state


def test_empty_and_unknown_schemas_return_none() -> None:
    empty = _identity("")
    unknown = _identity("CREATE TABLE unrelated (value TEXT)")

    assert (empty.kind, empty.version) == ("none", None)
    assert (unknown.kind, unknown.version) == ("none", None)
    assert not empty.can_claim_managed_recovery_state


def test_inspects_absent_and_exact_store_identity_schema() -> None:
    with _connection("") as connection:
        assert inspect_store_identity_schema(connection) == "absent"
    with _connection(_STORE_IDENTITY_SCHEMA) as connection:
        assert inspect_store_identity_schema(connection) == "exact"
        indexes = connection.execute("PRAGMA index_list(store_identity)").fetchall()
        assert [(row[1], row[2], row[3]) for row in indexes] == [
            ("sqlite_autoindex_store_identity_1", 1, "u")
        ]


@pytest.mark.parametrize(
    "changed_identity_schema",
    [
        _STORE_IDENTITY_SCHEMA.replace(
            "    binding_state TEXT NOT NULL",
            "    unexpected TEXT,\n    binding_state TEXT NOT NULL",
        ),
        _STORE_IDENTITY_SCHEMA.replace(" CHECK(singleton = 1)", ""),
        _STORE_IDENTITY_SCHEMA.replace(
            " CHECK(binding_state IN ('pending', 'bound'))",
            "",
        ),
        _STORE_IDENTITY_SCHEMA.replace("store_id TEXT NOT NULL UNIQUE", "store_id TEXT NOT NULL"),
        """
        CREATE TABLE store_identity (
            singleton INTEGER,
            store_id TEXT,
            artifact_root TEXT,
            binding_state TEXT
        )
        """,
    ],
    ids=[
        "extra-column",
        "missing-singleton-check",
        "missing-state-check",
        "missing-unique",
        "forged-four-columns",
    ],
)
def test_rejects_near_match_store_identity_table(changed_identity_schema: str) -> None:
    with _connection(changed_identity_schema) as connection:
        assert inspect_store_identity_schema(connection) == "invalid"
        assert classify_store_schema(connection).kind == "invalid"


def test_rejects_extra_store_identity_index() -> None:
    with _connection(
        _STORE_IDENTITY_SCHEMA
        + "CREATE INDEX unexpected_identity_state ON store_identity(binding_state)"
    ) as connection:
        assert inspect_store_identity_schema(connection) == "invalid"
        assert classify_store_schema(connection).kind == "invalid"


def test_classifies_identity_only_bootstrap() -> None:
    with _connection(_STORE_IDENTITY_SCHEMA) as connection:
        classification = classify_store_schema(connection)

    assert classification.kind == "identity_only"
    assert classification.identity == "exact"
    assert (classification.underlying.kind, classification.underlying.version) == (
        "none",
        None,
    )


def test_classifies_allowlisted_legacy_identity_crash_window() -> None:
    with _connection(_STORE_IDENTITY_SCHEMA + _STABLE_SCHEMA) as connection:
        classification = classify_store_schema(connection)

    assert classification.kind == "legacy_identity_crash_window"
    assert classification.identity == "exact"
    assert classification.underlying.version == "stable-de0481385cef"
    assert classification.underlying.can_claim_managed_recovery_state


def test_classifies_partial_legacy_identity_crash_window() -> None:
    with _connection(_STORE_IDENTITY_SCHEMA + _JOBS_ONLY_SCHEMA) as connection:
        classification = classify_store_schema(connection)

    assert classification.kind == "legacy_identity_crash_window"
    assert classification.underlying.kind == "partial"
    assert classification.underlying.version == "legacy-jobs-v1"


def test_classifies_each_current_branch_complete_layout(
    fresh_de048_connection: sqlite3.Connection,
    d85_then_de048_connection: sqlite3.Connection,
    pre_d85_then_d85_then_de048_connection: sqlite3.Connection,
) -> None:
    connections = (
        fresh_de048_connection,
        d85_then_de048_connection,
        pre_d85_then_d85_then_de048_connection,
    )
    expected_versions = (
        "current-from-stable-de0481385cef",
        "current-from-stable-de0481385cef-from-d85df746",
        "current-from-stable-de0481385cef-from-pre-d85df746",
    )
    for connection, expected_version in zip(connections, expected_versions, strict=True):
        connection.executescript(_CONTEXT_MATERIALIZATIONS_SCHEMA)
        without_identity = classify_store_schema(connection)
        assert without_identity.kind == "current"
        assert without_identity.underlying.version == expected_version

        connection.executescript(_STORE_IDENTITY_SCHEMA)
        with_identity = classify_store_schema(connection)
        assert with_identity.kind == "current_identity"
        assert with_identity.identity == "exact"
        assert with_identity.underlying.version == expected_version


def test_classifies_pre_de048_current_migration_window() -> None:
    schema = _FRESH_D85_SCHEMA + _CONTEXT_MATERIALIZATIONS_SCHEMA
    with _connection(schema) as connection:
        without_identity = classify_store_schema(connection)
        connection.executescript(_STORE_IDENTITY_SCHEMA)
        with_identity = classify_store_schema(connection)

    expected = "current-migration-window-from-stable-pre-de0481385cef"
    assert without_identity.kind == "current"
    assert without_identity.underlying.version == expected
    assert with_identity.kind == "current_identity"
    assert with_identity.underlying.version == expected


def test_classifies_current_layout_migrated_from_jobs_only_fixture() -> None:
    schema = (
        _JOBS_ONLY_SCHEMA + ";" + _CURRENT_WITHOUT_JOBS_SCHEMA + _CONTEXT_MATERIALIZATIONS_SCHEMA
    )
    with _connection(schema) as connection:
        connection.executescript(_JOBS_V1_TO_CURRENT_DDL)
        without_identity = classify_store_schema(connection)
        connection.executescript(_STORE_IDENTITY_SCHEMA)
        with_identity = classify_store_schema(connection)

    assert without_identity.kind == "current"
    assert without_identity.underlying.version == "current-from-legacy-jobs-v1"
    assert with_identity.kind == "current_identity"
    assert with_identity.underlying.version == "current-from-legacy-jobs-v1"


def test_classifies_current_layout_migrated_from_review_only_fixture() -> None:
    schema = (
        _REVIEW_REQUESTS_ONLY_SCHEMA
        + ";"
        + _CURRENT_WITHOUT_REVIEW_REQUESTS_SCHEMA
        + _CONTEXT_MATERIALIZATIONS_SCHEMA
    )
    with _connection(schema) as connection:
        connection.execute("ALTER TABLE review_requests ADD COLUMN adjudication_rationale TEXT")
        without_identity = classify_store_schema(connection)
        connection.executescript(_STORE_IDENTITY_SCHEMA)
        with_identity = classify_store_schema(connection)

    assert without_identity.kind == "current"
    assert without_identity.underlying.version == "current-from-legacy-review-requests-v1"
    assert with_identity.kind == "current_identity"
    assert with_identity.underlying.version == "current-from-legacy-review-requests-v1"


@pytest.mark.parametrize(
    "extra_object_ddl",
    [
        "CREATE TABLE unexpected_audit (id INTEGER)",
        "CREATE INDEX unexpected_jobs_state ON jobs(state)",
        "CREATE VIEW unexpected_jobs AS SELECT job_id FROM jobs",
        "CREATE TRIGGER unexpected_jobs_insert AFTER INSERT ON jobs BEGIN SELECT NEW.job_id; END",
    ],
    ids=["table", "index", "view", "trigger"],
)
def test_current_identity_rejects_every_extra_schema_object(
    extra_object_ddl: str,
) -> None:
    schema = (
        _STORE_IDENTITY_SCHEMA
        + _STABLE_SCHEMA
        + _CONTEXT_MATERIALIZATIONS_SCHEMA
        + extra_object_ddl
    )

    with _connection(schema) as connection:
        classification = classify_store_schema(connection)

    assert classification.kind == "invalid"
    assert classification.identity == "exact"


def test_rejects_near_named_identity_object() -> None:
    with _connection("CREATE TABLE store_identity_backup (value TEXT)") as connection:
        classification = classify_store_schema(connection)

    assert classification.kind == "invalid"
    assert classification.identity == "absent"


@pytest.mark.parametrize(
    "changed_column",
    [
        "status TEXT NOT NULL",
        "state INTEGER NOT NULL",
        "state TEXT",
        "state TEXT NOT NULL DEFAULT 'pending'",
        "state TEXT NOT NULL CHECK(state <> '')",
        "state TEXT GENERATED ALWAYS AS (method) VIRTUAL",
    ],
    ids=["name", "type", "not-null", "default", "constraint", "hidden"],
)
def test_jobs_partial_rejects_column_definition_drift(changed_column: str) -> None:
    changed = _JOBS_ONLY_SCHEMA.replace("state TEXT NOT NULL", changed_column)

    assert _identity(changed).kind == "none"


def test_jobs_partial_rejects_column_order_drift() -> None:
    changed = _JOBS_ONLY_SCHEMA.replace(
        "    job_type TEXT NOT NULL,\n    method TEXT NOT NULL,",
        "    method TEXT NOT NULL,\n    job_type TEXT NOT NULL,",
    )

    assert _identity(changed).kind == "none"


def test_jobs_partial_rejects_primary_key_and_index_origin_drift() -> None:
    changed = _JOBS_ONLY_SCHEMA.replace(
        "job_id TEXT PRIMARY KEY",
        "job_id TEXT UNIQUE",
    )

    assert _identity(changed).kind == "none"


def test_jobs_partial_rejects_foreign_key_drift() -> None:
    changed = _JOBS_ONLY_SCHEMA.replace(
        "    attempt_count INTEGER NOT NULL\n)",
        "    attempt_count INTEGER NOT NULL,\n"
        "    FOREIGN KEY(claimed_by) REFERENCES jobs(job_id)\n)",
    )

    assert _identity(changed).kind == "none"


@pytest.mark.parametrize(
    "extra_object_ddl",
    [
        "CREATE TABLE unexpected_audit (id INTEGER)",
        "CREATE INDEX unexpected_jobs_state ON jobs(state)",
        "CREATE VIEW unexpected_jobs AS SELECT job_id FROM jobs",
        "CREATE TRIGGER unexpected_jobs_insert AFTER INSERT ON jobs BEGIN SELECT NEW.job_id; END",
    ],
    ids=["table", "index", "view", "trigger"],
)
def test_partial_rejects_extra_schema_object(extra_object_ddl: str) -> None:
    assert _identity(_JOBS_ONLY_SCHEMA + ";" + extra_object_ddl).kind == "none"


def test_complete_rejects_extra_column_and_extra_business_table() -> None:
    with _connection(_STABLE_SCHEMA) as connection:
        connection.execute("ALTER TABLE jobs ADD COLUMN unexpected TEXT")
        assert identify_legacy_store_schema(connection).kind == "none"

    assert _identity(_STABLE_SCHEMA + "CREATE TABLE audit_log (id INTEGER)").kind == "none"


@pytest.mark.parametrize(
    "mutation",
    [
        "ALTER TABLE jobs ADD COLUMN unexpected TEXT",
        "DROP INDEX idx_feedback_applications_natural_key_unique",
        "CREATE TABLE unexpected_audit (id INTEGER)",
    ],
    ids=["extra-column", "missing-index", "extra-table"],
)
def test_original_pre_d85_complete_rejects_near_match(
    pre_d85_connection: sqlite3.Connection,
    mutation: str,
) -> None:
    pre_d85_connection.execute(mutation)

    assert identify_legacy_store_schema(pre_d85_connection).kind == "none"
    classification = classify_store_schema(pre_d85_connection)
    assert classification.kind == "invalid"
    assert classification.identity == "absent"


@pytest.mark.parametrize(
    "replacement",
    [
        "CREATE UNIQUE INDEX idx_artifacts_staging_job "
        "ON artifacts(staging_job_id) WHERE staging_job_id IS NOT NULL",
        "CREATE INDEX idx_artifacts_staging_job ON artifacts(staging_job_id)",
        "CREATE INDEX idx_artifacts_staging_job ON artifacts(type)",
        "CREATE INDEX idx_artifacts_staging_job ON artifacts(lower(staging_job_id)) "
        "WHERE staging_job_id IS NOT NULL",
    ],
    ids=["unique", "partial", "indexed-column", "expression"],
)
def test_complete_rejects_index_drift(replacement: str) -> None:
    with _connection(_STABLE_SCHEMA) as connection:
        connection.execute("DROP INDEX idx_artifacts_staging_job")
        connection.execute(replacement)

        assert identify_legacy_store_schema(connection).kind == "none"


def test_complete_rejects_missing_index() -> None:
    with _connection(_STABLE_SCHEMA) as connection:
        connection.execute("DROP INDEX idx_jobs_plan_target_unique")

        assert identify_legacy_store_schema(connection).kind == "none"


def test_complete_rejects_extra_index() -> None:
    with _connection(_STABLE_SCHEMA) as connection:
        connection.execute("CREATE INDEX unexpected_jobs_method ON jobs(method)")

        assert identify_legacy_store_schema(connection).kind == "none"


def test_sql_formatting_is_not_schema_drift() -> None:
    reformatted = _JOBS_ONLY_SCHEMA.replace("CREATE TABLE jobs", "create table jobs").replace(
        " TEXT NOT NULL", "   text\nnot null"
    )

    assert _identity(reformatted).version == "legacy-jobs-v1"
