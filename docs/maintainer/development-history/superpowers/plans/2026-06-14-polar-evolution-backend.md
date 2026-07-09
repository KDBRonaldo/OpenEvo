# Polar Evolution Backend Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a local Evolution Backend that receives Polar rollout events, manages datasets/jobs/artifacts, resolves evolution context, and integrates with Polar gateway runtime injection.

**Architecture:** Add a separate `polar_evolution` Python package inside this repo with FastAPI routes, SQLite storage, local artifact files, and an external worker job protocol. Polar consumes the backend through an optional topology config block; gateway resolves context before agent execution and exports session events after post-run result construction.

**Tech Stack:** Python 3.13, FastAPI, Pydantic v2, SQLite via stdlib `sqlite3`, local filesystem artifacts, pytest, httpx.

---

## Scope Check

This plan implements the v1 described in `docs/superpowers/specs/2026-06-14-polar-evolution-backend-design.md`.

Covered in this plan:

- Evolution Backend package and CLI.
- SQLite metadata and filesystem artifact root.
- Event ingest API.
- Artifact registry.
- Dataset build API.
- Job create/claim/heartbeat/complete/fail API.
- Context resolver API.
- Polar topology config.
- Gateway context injection into runtime files and env vars.
- Best-effort gateway event export.
- Mock worker lifecycle tests.

Not covered in this plan:

- Real LoRA training.
- vLLM/SGLang adapter hot loading.
- Postgres/object-store storage.
- Distributed auth.

## File Structure

Create:

- `src/polar_evolution/__init__.py`  
  Package marker and version export.

- `src/polar_evolution/ids.py`  
  Stable id generation helpers, such as `evt_*`, `art_*`, `ds_*`, `job_*`, `ctx_*`.

- `src/polar_evolution/time.py`  
  UTC timestamp helpers for persisted rows and API responses.

- `src/polar_evolution/models.py`  
  Pydantic request/response models and enums for events, artifacts, datasets, jobs, workers, and contexts.

- `src/polar_evolution/store.py`  
  SQLite schema initialization, transactions, event ingest, artifact registration, dataset persistence, job leases, and context persistence.

- `src/polar_evolution/files.py`  
  Artifact root path management, safe path checks, JSON writing, event payload paths, dataset manifests, and context snapshots.

- `src/polar_evolution/context.py`  
  Deterministic context resolution over registered artifacts.

- `src/polar_evolution/server.py`  
  FastAPI app factory and `/v1/*` routes.

- `src/polar_evolution/cli.py`  
  CLI entry point for `polar-evolution serve`.

- `src/polar_evolution/client.py`  
  Small async client used by Polar gateway to resolve context and export events.

- `src/polar_evolution/worker.py`  
  Minimal worker client helpers for claim/heartbeat/complete/fail.

- `tests/evolution/test_models.py`  
  Model validation tests.

- `tests/evolution/test_store_events.py`  
  Event ingest and idempotency tests.

- `tests/evolution/test_artifacts_context.py`  
  Artifact registration and context resolver tests.

- `tests/evolution/test_datasets_jobs.py`  
  Dataset build and external job protocol tests.

- `tests/evolution/test_server.py`  
  FastAPI route tests.

- `tests/gateway/test_evolution_integration.py`  
  Gateway-side context injection and event export tests with a fake backend/client.

Modify:

- `pyproject.toml`  
  Include `polar_evolution*` packages and add `polar-evolution` script.

- `src/polar/config/topology.py`  
  Add optional immutable `EvolutionConfig`.

- `src/polar/config/__init__.py`  
  Export the evolution config models used by gateway integration.

- `src/polar/gateway/node.py`  
  Resolve context before harness execution; export event after normalized result.

- `src/polar/gateway/server.py`  
  Pass evolution config/client into `GatewayNodeManager`.

## Development Notes

- The repo currently may have unrelated working tree changes. Each task below stages only files listed under that task.
- All backend APIs use `/v1`.
- All persistence paths are rooted under a configured artifact root, default `.polar_evolution`.
- Use `trust_env=False` for Evolution client calls from Polar so local proxy variables do not affect local control-plane traffic.
- Every task uses TDD: write test, verify failure, implement, verify pass, commit only that task.

---

### Task 1: Package Skeleton, CLI, and Health Route

**Files:**
- Modify: `pyproject.toml`
- Create: `src/polar_evolution/__init__.py`
- Create: `src/polar_evolution/time.py`
- Create: `src/polar_evolution/ids.py`
- Create: `src/polar_evolution/server.py`
- Create: `src/polar_evolution/cli.py`
- Test: `tests/evolution/test_server.py`

- [ ] **Step 1: Write the failing health test**

Create `tests/evolution/test_server.py`:

```python
from __future__ import annotations

from fastapi.testclient import TestClient

from polar_evolution.server import create_app


def test_health_reports_artifact_root(tmp_path):
    app = create_app(db_path=tmp_path / "evolution.db", artifact_root=tmp_path / "artifacts")
    with TestClient(app) as client:
        response = client.get("/v1/health")

    assert response.status_code == 200
    assert response.json() == {
        "status": "ok",
        "db": "ok",
        "artifact_root": str(tmp_path / "artifacts"),
    }
```

- [ ] **Step 2: Run the health test and verify it fails**

Run:

```bash
uv run pytest tests/evolution/test_server.py::test_health_reports_artifact_root -q
```

Expected: FAIL with `ModuleNotFoundError: No module named 'polar_evolution'`.

- [ ] **Step 3: Add package metadata and console script**

Modify `pyproject.toml`:

```toml
[project.scripts]
polar = "polar.cli:main"
polar-evolution = "polar_evolution.cli:main"
```

Modify the package finder include list:

```toml
[tool.setuptools.packages.find]
where = ["src"]
include = ["polar*", "slime_bridge*", "polar_evolution*"]
```

- [ ] **Step 4: Add minimal package files**

Create `src/polar_evolution/__init__.py`:

```python
"""Evolution Backend control plane for Polar skill and memory evolution."""

__version__ = "0.1.0"
```

Create `src/polar_evolution/time.py`:

```python
from __future__ import annotations

from datetime import UTC, datetime


def utc_now_iso() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")
```

Create `src/polar_evolution/ids.py`:

```python
from __future__ import annotations

from uuid import uuid4


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid4().hex[:16]}"
```

Create `src/polar_evolution/server.py`:

```python
from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import FastAPI


def create_app(*, db_path: str | Path, artifact_root: str | Path) -> FastAPI:
    root = Path(artifact_root)
    root.mkdir(parents=True, exist_ok=True)
    app = FastAPI(title="Polar Evolution Backend", version="0.1.0")
    app.state.db_path = Path(db_path)
    app.state.artifact_root = root

    @app.get("/v1/health")
    async def health() -> dict[str, Any]:
        return {
            "status": "ok",
            "db": "ok",
            "artifact_root": str(root),
        }

    return app
```

Create `src/polar_evolution/cli.py`:

```python
from __future__ import annotations

import argparse
from pathlib import Path

from polar_evolution.server import create_app


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="polar-evolution")
    subparsers = parser.add_subparsers(dest="command", required=True)
    serve = subparsers.add_parser("serve", help="Start the Evolution Backend.")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8200)
    serve.add_argument("--db", default=".polar_evolution/evolution.db")
    serve.add_argument("--artifact-root", default=".polar_evolution")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "serve":
        import uvicorn

        app = create_app(db_path=Path(args.db), artifact_root=Path(args.artifact_root))
        uvicorn.run(app, host=args.host, port=args.port)
        return 0
    raise ValueError(f"Unknown command: {args.command}")
```

- [ ] **Step 5: Run the health test and verify it passes**

Run:

```bash
uv run pytest tests/evolution/test_server.py::test_health_reports_artifact_root -q
```

Expected: `1 passed`.

- [ ] **Step 6: Run CLI smoke test**

Run:

```bash
uv run polar-evolution --help
```

Expected output contains `serve`.

- [ ] **Step 7: Commit Task 1**

```bash
git add pyproject.toml src/polar_evolution/__init__.py src/polar_evolution/time.py src/polar_evolution/ids.py src/polar_evolution/server.py src/polar_evolution/cli.py tests/evolution/test_server.py
git commit -m "feat: add evolution backend skeleton"
```

---

### Task 2: Pydantic Models

**Files:**
- Create: `src/polar_evolution/models.py`
- Test: `tests/evolution/test_models.py`

- [ ] **Step 1: Write failing model tests**

Create `tests/evolution/test_models.py`:

```python
from __future__ import annotations

from pydantic import ValidationError

from polar_evolution.models import (
    ArtifactType,
    ContextResolveRequest,
    EventIngestRequest,
    JobState,
    WorkerClaimRequest,
)


def test_event_ingest_requires_source_identity():
    request = EventIngestRequest(
        source="polar",
        event_type="polar.session_completed",
        source_event_id="session:abc",
        task_id="task_1",
        session_id="abc",
        payload={"session_result": {"status": "COMPLETED"}},
    )

    assert request.source == "polar"
    assert request.payload["session_result"]["status"] == "COMPLETED"


def test_event_ingest_rejects_empty_event_type():
    try:
        EventIngestRequest(
            source="polar",
            event_type="",
            source_event_id="session:abc",
            payload={},
        )
    except ValidationError as exc:
        assert "String should have at least 1 character" in str(exc)
    else:
        raise AssertionError("empty event_type should fail validation")


def test_context_resolve_request_defaults_limits():
    request = ContextResolveRequest(
        task_id="task_1",
        instruction="solve",
        agent={"harness": "codex"},
        base_model="Qwen/Qwen3.6-27B",
    )

    assert request.limits.max_memory_chars == 12000
    assert request.limits.max_skill_bundles == 4
    assert request.limits.max_adapters == 2


def test_worker_claim_request_and_enums():
    request = WorkerClaimRequest(
        worker_id="worker_1",
        capabilities=["parametric_memory_train"],
    )

    assert request.lease_seconds == 600
    assert ArtifactType.PARAMETRIC_MEMORY == "parametric_memory"
    assert JobState.PENDING == "pending"
```

- [ ] **Step 2: Run model tests and verify failure**

Run:

```bash
uv run pytest tests/evolution/test_models.py -q
```

Expected: FAIL with `ModuleNotFoundError: No module named 'polar_evolution.models'`.

- [ ] **Step 3: Implement models**

Create `src/polar_evolution/models.py`:

```python
from __future__ import annotations

from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class ArtifactType(StrEnum):
    TEXT_MEMORY = "text_memory"
    SKILL_BUNDLE = "skill_bundle"
    PARAMETRIC_MEMORY = "parametric_memory"
    DATASET = "dataset"
    REPORT = "report"
    CONTEXT_SNAPSHOT = "context_snapshot"


class ArtifactState(StrEnum):
    ACTIVE = "active"
    EXPERIMENTAL = "experimental"
    DEPRECATED = "deprecated"
    BROKEN = "broken"


class JobState(StrEnum):
    PENDING = "pending"
    CLAIMED = "claimed"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"
    EXPIRED = "expired"


class EventIngestRequest(BaseModel):
    source: str = Field(min_length=1)
    event_type: str = Field(min_length=1)
    source_event_id: str = Field(min_length=1)
    created_at: str | None = None
    task_id: str | None = None
    session_id: str | None = None
    policy_version: str | None = None
    rollout_step: int | None = None
    agent: dict[str, Any] = Field(default_factory=dict)
    base_model: str | None = None
    reward: float | None = None
    status: str | None = None
    payload: dict[str, Any] = Field(default_factory=dict)


class EventIngestResponse(BaseModel):
    event_id: str
    ingested: bool
    duplicate: bool


class ArtifactRegisterRequest(BaseModel):
    type: ArtifactType
    name: str = Field(min_length=1)
    uri: str = Field(min_length=1)
    manifest: dict[str, Any] = Field(default_factory=dict)
    lineage: dict[str, Any] = Field(default_factory=dict)
    compatibility: dict[str, Any] = Field(default_factory=dict)
    scores: dict[str, float] = Field(default_factory=dict)
    tags: list[str] = Field(default_factory=list)
    promoted: bool = False


class ArtifactResponse(BaseModel):
    artifact_id: str
    type: ArtifactType
    name: str
    version: int
    state: ArtifactState
    uri: str
    manifest: dict[str, Any] = Field(default_factory=dict)
    compatibility: dict[str, Any] = Field(default_factory=dict)
    scores: dict[str, float] = Field(default_factory=dict)
    tags: list[str] = Field(default_factory=list)
    promoted: bool = False


class DatasetQuery(BaseModel):
    event_types: list[str] = Field(default_factory=list)
    status: list[str] = Field(default_factory=list)
    reward_min: float | None = None
    policy_version: str | None = None
    task_tags: list[str] = Field(default_factory=list)


class DatasetLimits(BaseModel):
    max_events: int = Field(default=10000, ge=1)
    max_traces: int = Field(default=50000, ge=1)


class DatasetCreateRequest(BaseModel):
    name: str = Field(min_length=1)
    purpose: str = Field(min_length=1)
    query: DatasetQuery = Field(default_factory=DatasetQuery)
    limits: DatasetLimits = Field(default_factory=DatasetLimits)


class DatasetCreateResponse(BaseModel):
    dataset_id: str
    artifact_id: str
    event_count: int
    trace_count: int


class JobCreateRequest(BaseModel):
    method: str = Field(min_length=1)
    job_type: str = Field(min_length=1)
    input_artifact_ids: list[str] = Field(default_factory=list)
    config: dict[str, Any] = Field(default_factory=dict)
    priority: int = 100


class JobCreateResponse(BaseModel):
    job_id: str
    state: JobState


class WorkerClaimRequest(BaseModel):
    worker_id: str = Field(min_length=1)
    capabilities: list[str] = Field(default_factory=list)
    lease_seconds: int = Field(default=600, ge=1)


class WorkerClaimResponse(BaseModel):
    job: dict[str, Any] | None = None


class WorkerHeartbeatRequest(BaseModel):
    lease_id: str = Field(min_length=1)
    progress: float | None = Field(default=None, ge=0.0, le=1.0)
    message: str | None = None


class WorkerCompleteRequest(BaseModel):
    lease_id: str = Field(min_length=1)
    artifacts: list[ArtifactRegisterRequest] = Field(default_factory=list)
    report: dict[str, Any] = Field(default_factory=dict)


class WorkerFailRequest(BaseModel):
    lease_id: str = Field(min_length=1)
    error: str = Field(min_length=1)
    retryable: bool = True


class ContextLimits(BaseModel):
    max_memory_chars: int = Field(default=12000, ge=0)
    max_skill_bundles: int = Field(default=4, ge=0)
    max_adapters: int = Field(default=2, ge=0)


class ContextResolveRequest(BaseModel):
    task_id: str
    instruction: str
    agent: dict[str, Any] = Field(default_factory=dict)
    base_model: str | None = None
    policy_version: str | None = None
    rollout_step: int | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)
    limits: ContextLimits = Field(default_factory=ContextLimits)


class AdapterMergeSpec(BaseModel):
    base_model: str | None = None
    merge_mode: str = "reference_only"
    adapters: list[dict[str, Any]] = Field(default_factory=list)


class ContextResolveResponse(BaseModel):
    context_id: str
    memory: dict[str, Any] = Field(default_factory=dict)
    skills: list[dict[str, Any]] = Field(default_factory=list)
    adapter_merge_spec: AdapterMergeSpec = Field(default_factory=AdapterMergeSpec)
    selection: dict[str, Any] = Field(default_factory=dict)

    model_config = ConfigDict(use_enum_values=True)
```

- [ ] **Step 4: Run model tests and verify pass**

Run:

```bash
uv run pytest tests/evolution/test_models.py -q
```

Expected: `4 passed`.

- [ ] **Step 5: Commit Task 2**

```bash
git add src/polar_evolution/models.py tests/evolution/test_models.py
git commit -m "feat: add evolution backend models"
```

---

### Task 3: Filesystem Helpers and SQLite Store Initialization

**Files:**
- Create: `src/polar_evolution/files.py`
- Create: `src/polar_evolution/store.py`
- Test: `tests/evolution/test_store_events.py`

- [ ] **Step 1: Write failing store initialization test**

Create `tests/evolution/test_store_events.py`:

```python
from __future__ import annotations

import sqlite3

from polar_evolution.store import EvolutionStore


def test_store_initializes_schema(tmp_path):
    db_path = tmp_path / "evolution.db"
    store = EvolutionStore(db_path=db_path, artifact_root=tmp_path / "artifacts")
    store.initialize()

    with sqlite3.connect(db_path) as conn:
        tables = {
            row[0]
            for row in conn.execute(
                "select name from sqlite_master where type = 'table'"
            ).fetchall()
        }

    assert {
        "events",
        "datasets",
        "dataset_events",
        "jobs",
        "artifacts",
        "artifact_lineage",
        "contexts",
    }.issubset(tables)
```

- [ ] **Step 2: Run store test and verify failure**

Run:

```bash
uv run pytest tests/evolution/test_store_events.py::test_store_initializes_schema -q
```

Expected: FAIL with `ModuleNotFoundError: No module named 'polar_evolution.store'`.

- [ ] **Step 3: Implement filesystem helpers**

Create `src/polar_evolution/files.py`:

```python
from __future__ import annotations

import json
from pathlib import Path
from typing import Any


class ArtifactFileStore:
    def __init__(self, root: str | Path) -> None:
        self.root = Path(root).resolve()

    def initialize(self) -> None:
        for relative in (
            "events",
            "datasets",
            "artifacts/text_memory",
            "artifacts/skills",
            "artifacts/parametric_memory",
            "artifacts/reports",
            "contexts",
        ):
            (self.root / relative).mkdir(parents=True, exist_ok=True)

    def safe_path(self, *parts: str) -> Path:
        path = (self.root / Path(*parts)).resolve()
        if self.root != path and self.root not in path.parents:
            raise ValueError(f"path escapes artifact root: {path}")
        return path

    def write_json(self, path: Path, payload: dict[str, Any]) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
        return path

    def event_payload_path(self, event_id: str) -> Path:
        return self.safe_path("events", f"{event_id}.json")

    def dataset_manifest_path(self, dataset_id: str) -> Path:
        return self.safe_path("datasets", dataset_id, "manifest.json")

    def artifact_manifest_path(self, artifact_type: str, artifact_id: str) -> Path:
        return self.safe_path("artifacts", artifact_type, artifact_id, "manifest.json")

    def context_snapshot_path(self, context_id: str) -> Path:
        return self.safe_path("contexts", f"{context_id}.json")
```

- [ ] **Step 4: Implement SQLite schema initialization**

Create `src/polar_evolution/store.py`:

```python
from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
import sqlite3
from typing import Iterator

from polar_evolution.files import ArtifactFileStore


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

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
        finally:
            conn.close()
```

- [ ] **Step 5: Run store initialization test**

Run:

```bash
uv run pytest tests/evolution/test_store_events.py::test_store_initializes_schema -q
```

Expected: `1 passed`.

- [ ] **Step 6: Commit Task 3**

```bash
git add src/polar_evolution/files.py src/polar_evolution/store.py tests/evolution/test_store_events.py
git commit -m "feat: initialize evolution storage"
```

---

### Task 4: Event Ingest Store and API

**Files:**
- Modify: `src/polar_evolution/store.py`
- Modify: `src/polar_evolution/server.py`
- Test: `tests/evolution/test_store_events.py`
- Test: `tests/evolution/test_server.py`

- [ ] **Step 1: Add failing event idempotency test**

Append to `tests/evolution/test_store_events.py`:

```python
from polar_evolution.models import EventIngestRequest


def test_ingest_event_is_idempotent(tmp_path):
    store = EvolutionStore(db_path=tmp_path / "evolution.db", artifact_root=tmp_path / "artifacts")
    store.initialize()
    request = EventIngestRequest(
        source="polar",
        event_type="polar.session_completed",
        source_event_id="session:abc",
        task_id="task_1",
        session_id="abc",
        agent={"harness": "codex", "model_name": "gpt-5.4"},
        base_model="Qwen/Qwen3.6-27B",
        reward=1.0,
        status="COMPLETED",
        payload={"session_result": {"session_id": "abc"}},
    )

    first = store.ingest_event(request)
    second = store.ingest_event(request)

    assert first.event_id == second.event_id
    assert first.ingested is True
    assert first.duplicate is False
    assert second.ingested is False
    assert second.duplicate is True
```

- [ ] **Step 2: Add failing event API test**

Append to `tests/evolution/test_server.py`:

```python
def test_post_event_ingests_once(tmp_path):
    app = create_app(db_path=tmp_path / "evolution.db", artifact_root=tmp_path / "artifacts")
    payload = {
        "source": "polar",
        "event_type": "polar.session_completed",
        "source_event_id": "session:abc",
        "task_id": "task_1",
        "session_id": "abc",
        "payload": {"session_result": {"session_id": "abc"}},
    }

    with TestClient(app) as client:
        first = client.post("/v1/events", json=payload).json()
        second = client.post("/v1/events", json=payload).json()

    assert first["ingested"] is True
    assert first["duplicate"] is False
    assert second["event_id"] == first["event_id"]
    assert second["ingested"] is False
    assert second["duplicate"] is True
```

- [ ] **Step 3: Run event tests and verify failure**

Run:

```bash
uv run pytest tests/evolution/test_store_events.py::test_ingest_event_is_idempotent tests/evolution/test_server.py::test_post_event_ingests_once -q
```

Expected: FAIL because `EvolutionStore.ingest_event` and `/v1/events` do not exist.

- [ ] **Step 4: Implement event ingest**

Add imports to `src/polar_evolution/store.py`:

```python
import json

from polar_evolution.ids import new_id
from polar_evolution.models import EventIngestRequest, EventIngestResponse
from polar_evolution.time import utc_now_iso
```

Add method to `EvolutionStore`:

```python
    def ingest_event(self, request: EventIngestRequest) -> EventIngestResponse:
        with self.connect() as conn:
            existing = conn.execute(
                """
                SELECT event_id FROM events
                WHERE source = ? AND event_type = ? AND source_event_id = ?
                """,
                (request.source, request.event_type, request.source_event_id),
            ).fetchone()
            if existing is not None:
                return EventIngestResponse(
                    event_id=str(existing["event_id"]),
                    ingested=False,
                    duplicate=True,
                )

            event_id = new_id("evt")
            created_at = request.created_at or utc_now_iso()
            ingested_at = utc_now_iso()
            payload_path = self.files.event_payload_path(event_id)
            self.files.write_json(payload_path, request.model_dump(mode="json"))
            agent_harness = request.agent.get("harness")
            agent_model = request.agent.get("model_name")
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
```

- [ ] **Step 5: Wire store into FastAPI app**

Modify `src/polar_evolution/server.py` imports:

```python
from polar_evolution.models import EventIngestRequest, EventIngestResponse
from polar_evolution.store import EvolutionStore
```

Modify `create_app` to initialize store:

```python
    store = EvolutionStore(db_path=db_path, artifact_root=root)
    store.initialize()
    app.state.store = store
```

Add route inside `create_app`:

```python
    @app.post("/v1/events", response_model=EventIngestResponse)
    async def ingest_event(request: EventIngestRequest) -> EventIngestResponse:
        return store.ingest_event(request)
```

- [ ] **Step 6: Run event tests and verify pass**

Run:

```bash
uv run pytest tests/evolution/test_store_events.py tests/evolution/test_server.py -q
```

Expected: all tests in both files pass.

- [ ] **Step 7: Commit Task 4**

```bash
git add src/polar_evolution/store.py src/polar_evolution/server.py tests/evolution/test_store_events.py tests/evolution/test_server.py
git commit -m "feat: ingest evolution events"
```

---

### Task 5: Artifact Registry

**Files:**
- Modify: `src/polar_evolution/store.py`
- Modify: `src/polar_evolution/server.py`
- Test: `tests/evolution/test_artifacts_context.py`

- [ ] **Step 1: Write failing artifact registration test**

Create `tests/evolution/test_artifacts_context.py`:

```python
from __future__ import annotations

from polar_evolution.models import ArtifactRegisterRequest, ArtifactType
from polar_evolution.store import EvolutionStore


def test_register_artifact_persists_manifest(tmp_path):
    store = EvolutionStore(db_path=tmp_path / "evolution.db", artifact_root=tmp_path / "artifacts")
    store.initialize()

    artifact = store.register_artifact(
        ArtifactRegisterRequest(
            type=ArtifactType.TEXT_MEMORY,
            name="calculator memory",
            uri="file:///tmp/memory.md",
            manifest={"content_path": "memory.md"},
            compatibility={"task_tags": ["calculator"]},
            scores={"quality": 0.9},
            tags=["calculator"],
            promoted=True,
        )
    )

    assert artifact.artifact_id.startswith("art_")
    assert artifact.type == ArtifactType.TEXT_MEMORY
    assert artifact.version == 1
    assert artifact.promoted is True
    assert artifact.compatibility["task_tags"] == ["calculator"]
```

- [ ] **Step 2: Write failing artifact API test**

Append to `tests/evolution/test_server.py`:

```python
def test_register_artifact_route(tmp_path):
    app = create_app(db_path=tmp_path / "evolution.db", artifact_root=tmp_path / "artifacts")

    with TestClient(app) as client:
        response = client.post(
            "/v1/artifacts",
            json={
                "type": "parametric_memory",
                "name": "pmem_calc",
                "uri": "file:///tmp/adapter",
                "manifest": {"base_model": "Qwen/Qwen3.6-27B", "adapter_format": "lora"},
                "compatibility": {"base_model": "Qwen/Qwen3.6-27B"},
                "scores": {"heldout_reward_delta": 0.08},
                "tags": ["calculator"],
                "promoted": True,
            },
        )

    assert response.status_code == 200
    body = response.json()
    assert body["type"] == "parametric_memory"
    assert body["name"] == "pmem_calc"
    assert body["manifest"]["adapter_format"] == "lora"
```

- [ ] **Step 3: Run artifact tests and verify failure**

Run:

```bash
uv run pytest tests/evolution/test_artifacts_context.py::test_register_artifact_persists_manifest tests/evolution/test_server.py::test_register_artifact_route -q
```

Expected: FAIL because `register_artifact` and `/v1/artifacts` do not exist.

- [ ] **Step 4: Implement artifact registration in store**

Add imports to `src/polar_evolution/store.py`:

```python
from polar_evolution.models import ArtifactRegisterRequest, ArtifactResponse, ArtifactState
```

Add method to `EvolutionStore`:

```python
    def register_artifact(self, request: ArtifactRegisterRequest) -> ArtifactResponse:
        artifact_id = new_id("art")
        created_at = utc_now_iso()
        manifest_path = self.files.artifact_manifest_path(str(request.type), artifact_id)
        manifest_payload = {
            "artifact_id": artifact_id,
            "type": str(request.type),
            "name": request.name,
            "uri": request.uri,
            "manifest": request.manifest,
            "lineage": request.lineage,
            "compatibility": request.compatibility,
            "scores": request.scores,
            "tags": request.tags,
            "promoted": request.promoted,
        }
        self.files.write_json(manifest_path, manifest_payload)
        with self.connect() as conn:
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
                    str(request.type),
                    request.name,
                    1,
                    str(ArtifactState.ACTIVE),
                    created_at,
                    request.uri,
                    str(manifest_path),
                    json.dumps(request.lineage, sort_keys=True),
                    json.dumps(request.compatibility, sort_keys=True),
                    json.dumps(request.scores, sort_keys=True),
                    json.dumps(request.tags, sort_keys=True),
                    1 if request.promoted else 0,
                ),
            )
            conn.commit()
        return ArtifactResponse(
            artifact_id=artifact_id,
            type=request.type,
            name=request.name,
            version=1,
            state=ArtifactState.ACTIVE,
            uri=request.uri,
            manifest=request.manifest,
            compatibility=request.compatibility,
            scores=request.scores,
            tags=request.tags,
            promoted=request.promoted,
        )
```

- [ ] **Step 5: Add artifact route**

Modify `src/polar_evolution/server.py` imports:

```python
from polar_evolution.models import ArtifactRegisterRequest, ArtifactResponse
```

Add route:

```python
    @app.post("/v1/artifacts", response_model=ArtifactResponse)
    async def register_artifact(request: ArtifactRegisterRequest) -> ArtifactResponse:
        return store.register_artifact(request)
```

- [ ] **Step 6: Run artifact tests and verify pass**

Run:

```bash
uv run pytest tests/evolution/test_artifacts_context.py tests/evolution/test_server.py -q
```

Expected: all tests pass.

- [ ] **Step 7: Commit Task 5**

```bash
git add src/polar_evolution/store.py src/polar_evolution/server.py tests/evolution/test_artifacts_context.py tests/evolution/test_server.py
git commit -m "feat: register evolution artifacts"
```

---

### Task 6: Dataset Builder

**Files:**
- Modify: `src/polar_evolution/store.py`
- Modify: `src/polar_evolution/server.py`
- Test: `tests/evolution/test_datasets_jobs.py`

- [ ] **Step 1: Write failing dataset creation test**

Create `tests/evolution/test_datasets_jobs.py`:

```python
from __future__ import annotations

from polar_evolution.models import DatasetCreateRequest, EventIngestRequest
from polar_evolution.store import EvolutionStore


def test_create_dataset_filters_events(tmp_path):
    store = EvolutionStore(db_path=tmp_path / "evolution.db", artifact_root=tmp_path / "artifacts")
    store.initialize()
    store.ingest_event(
        EventIngestRequest(
            source="polar",
            event_type="polar.session_completed",
            source_event_id="session:good",
            task_id="task_good",
            status="COMPLETED",
            reward=1.0,
            policy_version="policy_1",
            payload={"session_result": {"trajectory": {"traces": [{"reward": 1.0}]}}},
        )
    )
    store.ingest_event(
        EventIngestRequest(
            source="polar",
            event_type="polar.session_completed",
            source_event_id="session:bad",
            task_id="task_bad",
            status="ERROR",
            reward=0.0,
            policy_version="policy_1",
            payload={"session_result": {"trajectory": {"traces": []}}},
        )
    )

    response = store.create_dataset(
        DatasetCreateRequest(
            name="good_policy_1",
            purpose="skill_distillation",
            query={
                "event_types": ["polar.session_completed"],
                "status": ["COMPLETED"],
                "reward_min": 0.8,
                "policy_version": "policy_1",
            },
        )
    )

    assert response.dataset_id.startswith("ds_")
    assert response.artifact_id.startswith("art_")
    assert response.event_count == 1
    assert response.trace_count == 1
```

- [ ] **Step 2: Run dataset test and verify failure**

Run:

```bash
uv run pytest tests/evolution/test_datasets_jobs.py::test_create_dataset_filters_events -q
```

Expected: FAIL because `create_dataset` does not exist.

- [ ] **Step 3: Implement dataset creation**

Add imports to `src/polar_evolution/store.py`:

```python
from polar_evolution.models import DatasetCreateRequest, DatasetCreateResponse, ArtifactType
```

Add helper and method to `EvolutionStore`:

```python
    def _event_rows_for_dataset(self, conn: sqlite3.Connection, request: DatasetCreateRequest) -> list[sqlite3.Row]:
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
        where = " AND ".join(clauses) if clauses else "1 = 1"
        return conn.execute(
            f"SELECT * FROM events WHERE {where} ORDER BY ingested_at LIMIT ?",
            (*params, request.limits.max_events),
        ).fetchall()

    def create_dataset(self, request: DatasetCreateRequest) -> DatasetCreateResponse:
        dataset_id = new_id("ds")
        created_at = utc_now_iso()
        with self.connect() as conn:
            rows = self._event_rows_for_dataset(conn, request)
            trace_count = 0
            event_ids: list[str] = []
            for row in rows:
                event_ids.append(str(row["event_id"]))
                payload = json.loads(Path(str(row["payload_path"])).read_text(encoding="utf-8"))
                traces = (
                    payload.get("payload", {})
                    .get("session_result", {})
                    .get("trajectory", {})
                    .get("traces", [])
                )
                trace_count += len(traces)
            trace_count = min(trace_count, request.limits.max_traces)
            manifest_path = self.files.dataset_manifest_path(dataset_id)
            manifest = {
                "dataset_id": dataset_id,
                "name": request.name,
                "purpose": request.purpose,
                "query": request.query.model_dump(mode="json"),
                "event_ids": event_ids,
                "event_count": len(event_ids),
                "trace_count": trace_count,
            }
            self.files.write_json(manifest_path, manifest)
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
                    request.name,
                    request.purpose,
                    "active",
                    created_at,
                    request.query.model_dump_json(),
                    str(manifest_path),
                    len(event_ids),
                    trace_count,
                    artifact.artifact_id,
                ),
            )
            conn.executemany(
                "INSERT INTO dataset_events (dataset_id, event_id) VALUES (?, ?)",
                [(dataset_id, event_id) for event_id in event_ids],
            )
            conn.commit()
            return DatasetCreateResponse(
                dataset_id=dataset_id,
                artifact_id=artifact.artifact_id,
                event_count=len(event_ids),
                trace_count=trace_count,
            )
```

In this step, import `Path` at the top of `src/polar_evolution/store.py`.

- [ ] **Step 4: Add dataset API route**

Modify `src/polar_evolution/server.py` imports:

```python
from polar_evolution.models import DatasetCreateRequest, DatasetCreateResponse
```

Add route:

```python
    @app.post("/v1/datasets", response_model=DatasetCreateResponse)
    async def create_dataset(request: DatasetCreateRequest) -> DatasetCreateResponse:
        return store.create_dataset(request)
```

- [ ] **Step 5: Run dataset tests**

Run:

```bash
uv run pytest tests/evolution/test_datasets_jobs.py::test_create_dataset_filters_events -q
```

Expected: `1 passed`.

- [ ] **Step 6: Commit Task 6**

```bash
git add src/polar_evolution/store.py src/polar_evolution/server.py tests/evolution/test_datasets_jobs.py
git commit -m "feat: build evolution datasets"
```

---

### Task 7: External Job Protocol

**Files:**
- Modify: `src/polar_evolution/store.py`
- Modify: `src/polar_evolution/server.py`
- Create: `src/polar_evolution/worker.py`
- Test: `tests/evolution/test_datasets_jobs.py`

- [ ] **Step 1: Add failing job lifecycle test**

Append to `tests/evolution/test_datasets_jobs.py`:

```python
from polar_evolution.models import (
    ArtifactRegisterRequest,
    ArtifactType,
    JobCreateRequest,
    WorkerClaimRequest,
    WorkerCompleteRequest,
    WorkerFailRequest,
    WorkerHeartbeatRequest,
)


def test_job_claim_heartbeat_and_complete(tmp_path):
    store = EvolutionStore(db_path=tmp_path / "evolution.db", artifact_root=tmp_path / "artifacts")
    store.initialize()
    dataset = store.register_artifact(
        ArtifactRegisterRequest(
            type=ArtifactType.DATASET,
            name="dataset",
            uri="file:///tmp/dataset.json",
            promoted=True,
        )
    )
    job = store.create_job(
        JobCreateRequest(
            method="mock_lora",
            job_type="parametric_memory_train",
            input_artifact_ids=[dataset.artifact_id],
            config={"base_model": "Qwen/Qwen3.6-27B"},
        )
    )

    claim = store.claim_job(
        WorkerClaimRequest(
            worker_id="worker_1",
            capabilities=["parametric_memory_train"],
            lease_seconds=60,
        )
    )
    assert claim.job is not None
    assert claim.job["job_id"] == job.job_id
    lease_id = claim.job["lease_id"]

    store.heartbeat_job(job.job_id, WorkerHeartbeatRequest(lease_id=lease_id, progress=0.5))
    complete = store.complete_job(
        job.job_id,
        WorkerCompleteRequest(
            lease_id=lease_id,
            artifacts=[
                ArtifactRegisterRequest(
                    type=ArtifactType.PARAMETRIC_MEMORY,
                    name="pmem_calc",
                    uri="file:///tmp/adapter",
                    manifest={"base_model": "Qwen/Qwen3.6-27B", "adapter_format": "lora"},
                    compatibility={"base_model": "Qwen/Qwen3.6-27B"},
                    promoted=True,
                )
            ],
        ),
    )

    assert complete["state"] == "succeeded"
    assert complete["artifact_ids"][0].startswith("art_")


def test_job_failure_records_error(tmp_path):
    store = EvolutionStore(db_path=tmp_path / "evolution.db", artifact_root=tmp_path / "artifacts")
    store.initialize()
    job = store.create_job(JobCreateRequest(method="mock", job_type="text_memory_mining"))
    claim = store.claim_job(WorkerClaimRequest(worker_id="worker_1", capabilities=["text_memory_mining"]))
    assert claim.job is not None

    result = store.fail_job(
        job.job_id,
        WorkerFailRequest(
            lease_id=claim.job["lease_id"],
            error="worker command failed",
            retryable=False,
        ),
    )

    assert result["state"] == "failed"
    assert "worker command failed" in result["error"]
```

- [ ] **Step 2: Run job tests and verify failure**

Run:

```bash
uv run pytest tests/evolution/test_datasets_jobs.py::test_job_claim_heartbeat_and_complete tests/evolution/test_datasets_jobs.py::test_job_failure_records_error -q
```

Expected: FAIL because job store methods do not exist.

- [ ] **Step 3: Implement job store methods**

Add imports to `src/polar_evolution/store.py`:

```python
from datetime import UTC, datetime, timedelta

from polar_evolution.models import (
    JobCreateRequest,
    JobCreateResponse,
    JobState,
    WorkerClaimRequest,
    WorkerClaimResponse,
    WorkerCompleteRequest,
    WorkerFailRequest,
    WorkerHeartbeatRequest,
)
```

Add methods:

```python
    def create_job(self, request: JobCreateRequest) -> JobCreateResponse:
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
                    request.job_type,
                    request.method,
                    str(JobState.PENDING),
                    request.priority,
                    now,
                    now,
                    json.dumps(request.input_artifact_ids),
                    json.dumps(request.config, sort_keys=True),
                    0,
                ),
            )
            conn.commit()
        return JobCreateResponse(job_id=job_id, state=JobState.PENDING)

    def claim_job(self, request: WorkerClaimRequest) -> WorkerClaimResponse:
        now_dt = datetime.now(UTC)
        lease_expires = (now_dt + timedelta(seconds=request.lease_seconds)).replace(microsecond=0)
        with self.connect() as conn:
            row = conn.execute(
                """
                SELECT * FROM jobs
                WHERE state = ?
                ORDER BY priority DESC, created_at ASC
                LIMIT 1
                """,
                (str(JobState.PENDING),),
            ).fetchone()
            if row is None:
                return WorkerClaimResponse(job=None)
            if request.capabilities and str(row["job_type"]) not in request.capabilities:
                return WorkerClaimResponse(job=None)
            lease_id = new_id("lease")
            conn.execute(
                """
                UPDATE jobs
                SET state = ?, claimed_by = ?, lease_id = ?, lease_expires_at = ?,
                    updated_at = ?, attempt_count = attempt_count + 1
                WHERE job_id = ?
                """,
                (
                    str(JobState.CLAIMED),
                    request.worker_id,
                    lease_id,
                    lease_expires.isoformat().replace("+00:00", "Z"),
                    utc_now_iso(),
                    row["job_id"],
                ),
            )
            conn.commit()
            artifacts = []
            for artifact_id in json.loads(str(row["input_artifact_ids_json"])):
                artifact = conn.execute(
                    "SELECT * FROM artifacts WHERE artifact_id = ?",
                    (artifact_id,),
                ).fetchone()
                if artifact is not None:
                    artifacts.append(
                        {
                            "artifact_id": artifact["artifact_id"],
                            "type": artifact["type"],
                            "uri": artifact["uri"],
                        }
                    )
            return WorkerClaimResponse(
                job={
                    "job_id": row["job_id"],
                    "lease_id": lease_id,
                    "job_type": row["job_type"],
                    "method": row["method"],
                    "input_artifacts": artifacts,
                    "config": json.loads(str(row["config_json"])),
                }
            )

    def _assert_job_lease(self, conn: sqlite3.Connection, job_id: str, lease_id: str) -> sqlite3.Row:
        row = conn.execute("SELECT * FROM jobs WHERE job_id = ?", (job_id,)).fetchone()
        if row is None:
            raise ValueError(f"unknown job_id: {job_id}")
        if row["lease_id"] != lease_id:
            raise ValueError(f"lease mismatch for job_id: {job_id}")
        return row

    def heartbeat_job(self, job_id: str, request: WorkerHeartbeatRequest) -> dict[str, object]:
        with self.connect() as conn:
            self._assert_job_lease(conn, job_id, request.lease_id)
            conn.execute(
                "UPDATE jobs SET state = ?, updated_at = ? WHERE job_id = ?",
                (str(JobState.RUNNING), utc_now_iso(), job_id),
            )
            conn.commit()
        return {"job_id": job_id, "state": str(JobState.RUNNING)}

    def complete_job(self, job_id: str, request: WorkerCompleteRequest) -> dict[str, object]:
        artifact_ids: list[str] = []
        with self.connect() as conn:
            self._assert_job_lease(conn, job_id, request.lease_id)
        for artifact_request in request.artifacts:
            artifact_ids.append(self.register_artifact(artifact_request).artifact_id)
        with self.connect() as conn:
            conn.execute(
                "UPDATE jobs SET state = ?, updated_at = ?, error = NULL WHERE job_id = ?",
                (str(JobState.SUCCEEDED), utc_now_iso(), job_id),
            )
            conn.commit()
        return {"job_id": job_id, "state": str(JobState.SUCCEEDED), "artifact_ids": artifact_ids}

    def fail_job(self, job_id: str, request: WorkerFailRequest) -> dict[str, object]:
        with self.connect() as conn:
            self._assert_job_lease(conn, job_id, request.lease_id)
            conn.execute(
                "UPDATE jobs SET state = ?, updated_at = ?, error = ? WHERE job_id = ?",
                (str(JobState.FAILED), utc_now_iso(), request.error, job_id),
            )
            conn.commit()
        return {"job_id": job_id, "state": str(JobState.FAILED), "error": request.error}
```

- [ ] **Step 4: Add job API routes**

Modify `src/polar_evolution/server.py` imports:

```python
from polar_evolution.models import (
    JobCreateRequest,
    JobCreateResponse,
    WorkerClaimRequest,
    WorkerClaimResponse,
    WorkerCompleteRequest,
    WorkerFailRequest,
    WorkerHeartbeatRequest,
)
```

Add routes:

```python
    @app.post("/v1/jobs", response_model=JobCreateResponse)
    async def create_job(request: JobCreateRequest) -> JobCreateResponse:
        return store.create_job(request)

    @app.post("/v1/jobs/claim", response_model=WorkerClaimResponse)
    async def claim_job(request: WorkerClaimRequest) -> WorkerClaimResponse:
        return store.claim_job(request)

    @app.post("/v1/jobs/{job_id}/heartbeat")
    async def heartbeat_job(job_id: str, request: WorkerHeartbeatRequest) -> dict[str, object]:
        return store.heartbeat_job(job_id, request)

    @app.post("/v1/jobs/{job_id}/complete")
    async def complete_job(job_id: str, request: WorkerCompleteRequest) -> dict[str, object]:
        return store.complete_job(job_id, request)

    @app.post("/v1/jobs/{job_id}/fail")
    async def fail_job(job_id: str, request: WorkerFailRequest) -> dict[str, object]:
        return store.fail_job(job_id, request)
```

- [ ] **Step 5: Add worker client helper**

Create `src/polar_evolution/worker.py`:

```python
from __future__ import annotations

from typing import Any

import httpx


class EvolutionWorkerClient:
    def __init__(self, base_url: str) -> None:
        self.base_url = base_url.rstrip("/")
        self._client = httpx.Client(timeout=30.0, trust_env=False)

    def claim(self, worker_id: str, capabilities: list[str]) -> dict[str, Any] | None:
        response = self._client.post(
            f"{self.base_url}/v1/jobs/claim",
            json={"worker_id": worker_id, "capabilities": capabilities},
        )
        response.raise_for_status()
        return response.json().get("job")

    def close(self) -> None:
        self._client.close()
```

- [ ] **Step 6: Run job tests**

Run:

```bash
uv run pytest tests/evolution/test_datasets_jobs.py -q
```

Expected: all tests pass.

- [ ] **Step 7: Commit Task 7**

```bash
git add src/polar_evolution/store.py src/polar_evolution/server.py src/polar_evolution/worker.py tests/evolution/test_datasets_jobs.py
git commit -m "feat: add evolution job protocol"
```

---

### Task 8: Context Resolver

**Files:**
- Create: `src/polar_evolution/context.py`
- Modify: `src/polar_evolution/store.py`
- Modify: `src/polar_evolution/server.py`
- Test: `tests/evolution/test_artifacts_context.py`

- [ ] **Step 1: Add failing context resolver test**

Append to `tests/evolution/test_artifacts_context.py`:

```python
from polar_evolution.models import ContextResolveRequest


def test_context_resolver_selects_memory_skill_and_adapter(tmp_path):
    store = EvolutionStore(db_path=tmp_path / "evolution.db", artifact_root=tmp_path / "artifacts")
    store.initialize()
    memory_file = tmp_path / "memory.md"
    memory_file.write_text("Use recursive descent for parser tasks.", encoding="utf-8")
    memory = store.register_artifact(
        ArtifactRegisterRequest(
            type=ArtifactType.TEXT_MEMORY,
            name="parser memory",
            uri=memory_file.as_uri(),
            compatibility={"task_tags": ["calculator"], "agent_harness": ["codex"]},
            scores={"quality": 0.9},
            tags=["calculator"],
            promoted=True,
        )
    )
    skill = store.register_artifact(
        ArtifactRegisterRequest(
            type=ArtifactType.SKILL_BUNDLE,
            name="parser skill",
            uri="file:///tmp/skills/parser",
            compatibility={"task_tags": ["calculator"]},
            scores={"quality": 0.8},
            tags=["calculator"],
            promoted=True,
        )
    )
    adapter = store.register_artifact(
        ArtifactRegisterRequest(
            type=ArtifactType.PARAMETRIC_MEMORY,
            name="parser lora",
            uri="file:///tmp/adapters/parser",
            manifest={"adapter_format": "lora", "base_model": "Qwen/Qwen3.6-27B"},
            compatibility={"base_model": "Qwen/Qwen3.6-27B", "task_tags": ["calculator"]},
            scores={"heldout_reward_delta": 0.1},
            tags=["calculator"],
            promoted=True,
        )
    )

    context = store.resolve_context(
        ContextResolveRequest(
            task_id="task_1",
            instruction="fix calculator parser",
            agent={"harness": "codex"},
            base_model="Qwen/Qwen3.6-27B",
            metadata={"task_tags": ["calculator"]},
        )
    )

    assert context.context_id.startswith("ctx_")
    assert memory.artifact_id in context.memory["artifact_ids"]
    assert "recursive descent" in context.memory["rendered_text"]
    assert context.skills[0]["artifact_id"] == skill.artifact_id
    assert context.adapter_merge_spec.adapters[0]["artifact_id"] == adapter.artifact_id
```

- [ ] **Step 2: Run context test and verify failure**

Run:

```bash
uv run pytest tests/evolution/test_artifacts_context.py::test_context_resolver_selects_memory_skill_and_adapter -q
```

Expected: FAIL because `resolve_context` does not exist.

- [ ] **Step 3: Implement context resolver**

Create `src/polar_evolution/context.py`:

```python
from __future__ import annotations

import json
from pathlib import Path
from urllib.parse import urlparse

from polar_evolution.models import ArtifactType, ContextResolveRequest


def artifact_matches(request: ContextResolveRequest, row: dict[str, object]) -> bool:
    compatibility = json.loads(str(row.get("compatibility_json") or "{}"))
    task_tags = set(request.metadata.get("task_tags") or [])
    required_tags = set(compatibility.get("task_tags") or [])
    if required_tags and not task_tags.intersection(required_tags):
        return False
    base_model = compatibility.get("base_model")
    if base_model and request.base_model and base_model != request.base_model:
        return False
    harnesses = set(compatibility.get("agent_harness") or [])
    harness = request.agent.get("harness")
    if harnesses and harness not in harnesses:
        return False
    return True


def read_file_uri_text(uri: str) -> str:
    parsed = urlparse(uri)
    if parsed.scheme != "file":
        return ""
    path = Path(parsed.path)
    if not path.is_file():
        return ""
    return path.read_text(encoding="utf-8")


def artifact_score(row: dict[str, object]) -> float:
    scores = json.loads(str(row.get("scores_json") or "{}"))
    if "quality" in scores:
        return float(scores["quality"])
    if "heldout_reward_delta" in scores:
        return float(scores["heldout_reward_delta"])
    return 0.0


def sort_candidates(rows: list[dict[str, object]]) -> list[dict[str, object]]:
    return sorted(rows, key=lambda row: (artifact_score(row), str(row["created_at"])), reverse=True)


def artifact_type(row: dict[str, object]) -> ArtifactType:
    return ArtifactType(str(row["type"]))
```

- [ ] **Step 4: Add artifact listing and context persistence to store**

Add imports to `src/polar_evolution/store.py`:

```python
from polar_evolution.context import artifact_matches, artifact_type, read_file_uri_text, sort_candidates
from polar_evolution.models import AdapterMergeSpec, ContextResolveRequest, ContextResolveResponse
```

Add methods:

```python
    def _promoted_artifact_rows(self) -> list[dict[str, object]]:
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM artifacts WHERE promoted = 1 AND state IN (?, ?)",
                ("active", "experimental"),
            ).fetchall()
        return [dict(row) for row in rows]

    def resolve_context(self, request: ContextResolveRequest) -> ContextResolveResponse:
        context_id = new_id("ctx")
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
                remaining = request.limits.max_memory_chars - memory_chars
                if text and remaining > 0:
                    clipped = text[:remaining]
                    rendered_parts.append(clipped)
                    memory_chars += len(clipped)
                selected_memory.append({"artifact_id": artifact_id, "name": row["name"]})
                selected_ids.append(artifact_id)
            elif kind == ArtifactType.SKILL_BUNDLE and len(skills) < request.limits.max_skill_bundles:
                skills.append({"artifact_id": artifact_id, "name": row["name"], "uri": row["uri"]})
                selected_ids.append(artifact_id)
            elif kind == ArtifactType.PARAMETRIC_MEMORY and len(adapters) < request.limits.max_adapters:
                adapters.append(
                    {
                        "artifact_id": artifact_id,
                        "adapter_id": row["name"],
                        "uri": row["uri"],
                        "weight": 1.0,
                        "format": "lora",
                    }
                )
                selected_ids.append(artifact_id)

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
            selection={"artifact_ids": selected_ids, "reasons": ["matched promoted compatible artifacts"]},
        )
        snapshot_path = self.files.context_snapshot_path(context_id)
        self.files.write_json(
            snapshot_path,
            {
                "request": request.model_dump(mode="json"),
                "response": response.model_dump(mode="json"),
            },
        )
        with self.connect() as conn:
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
                    request.model_dump_json(),
                    response.model_dump_json(),
                    json.dumps(selected_ids),
                ),
            )
            conn.commit()
        return response
```

- [ ] **Step 5: Add context route**

Modify `src/polar_evolution/server.py` imports:

```python
from polar_evolution.models import ContextResolveRequest, ContextResolveResponse
```

Add route:

```python
    @app.post("/v1/contexts/resolve", response_model=ContextResolveResponse)
    async def resolve_context(request: ContextResolveRequest) -> ContextResolveResponse:
        return store.resolve_context(request)
```

- [ ] **Step 6: Run context tests**

Run:

```bash
uv run pytest tests/evolution/test_artifacts_context.py tests/evolution/test_server.py -q
```

Expected: all tests pass.

- [ ] **Step 7: Commit Task 8**

```bash
git add src/polar_evolution/context.py src/polar_evolution/store.py src/polar_evolution/server.py tests/evolution/test_artifacts_context.py tests/evolution/test_server.py
git commit -m "feat: resolve evolution context"
```

---

### Task 9: Polar Evolution Topology Config

**Files:**
- Modify: `src/polar/config/topology.py`
- Modify: `src/polar/config/__init__.py`
- Test: `tests/config/test_topology.py`

- [ ] **Step 1: Add failing topology config test**

Append to `tests/config/test_topology.py`:

```python
def test_topology_loads_optional_evolution_config(tmp_path):
    config_path = tmp_path / "topology.yaml"
    config_path.write_text(
        """
rollout:
  host: 127.0.0.1
  port: 8080
gateway:
  nodes:
    - id: node-a
      host: 127.0.0.1
      port: 8100
      model_served: Qwen/Qwen3.6-27B
      inference:
        engine: vllm
        base_url: http://127.0.0.1:8000
evolution:
  enabled: true
  backend_url: http://127.0.0.1:8200
  context:
    target_dir: /polar/session/evolution
    timeout_seconds: 3
    fail_open: true
  event_export:
    enabled: true
    timeout_seconds: 4
    fail_open: true
""".strip()
    )

    topology = TopologyConfig.load(config_path)

    assert topology.evolution is not None
    assert topology.evolution.enabled is True
    assert topology.evolution.backend_url == "http://127.0.0.1:8200"
    assert topology.evolution.context.target_dir == "/polar/session/evolution"
    assert topology.evolution.context.timeout_seconds == 3
    assert topology.evolution.event_export.timeout_seconds == 4
```

- [ ] **Step 2: Run topology test and verify failure**

Run:

```bash
uv run pytest tests/config/test_topology.py::test_topology_loads_optional_evolution_config -q
```

Expected: FAIL because `evolution` is an unknown key.

- [ ] **Step 3: Add evolution config models**

Modify `src/polar/config/topology.py` by adding:

```python
class EvolutionContextConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    target_dir: str = "/polar/session/evolution"
    timeout_seconds: float = Field(default=10.0, gt=0)
    fail_open: bool = True


class EvolutionEventExportConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    enabled: bool = True
    timeout_seconds: float = Field(default=10.0, gt=0)
    fail_open: bool = True


class EvolutionConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    enabled: bool = False
    backend_url: str = "http://127.0.0.1:8200"
    context: EvolutionContextConfig = Field(default_factory=EvolutionContextConfig)
    event_export: EvolutionEventExportConfig = Field(default_factory=EvolutionEventExportConfig)

    @field_validator("backend_url")
    @classmethod
    def _strip_backend_url(cls, value: str) -> str:
        return value.rstrip("/")
```

Add field to `TopologyConfig`:

```python
    evolution: EvolutionConfig | None = None
```

Update `__all__` if present:

```python
    "EvolutionConfig",
    "EvolutionContextConfig",
    "EvolutionEventExportConfig",
```

- [ ] **Step 4: Export evolution config models from package**

Modify `src/polar/config/__init__.py` import list:

```python
from polar.config.topology import (
    EvolutionConfig,
    EvolutionContextConfig,
    EvolutionEventExportConfig,
    GatewayConfig,
    GatewayNodeConfig,
    RolloutServiceConfig,
    TopologyConfig,
)
```

Modify `__all__`:

```python
__all__ = [
    "EvolutionConfig",
    "EvolutionContextConfig",
    "EvolutionEventExportConfig",
    "GatewayConfig",
    "GatewayNodeConfig",
    "RolloutServiceConfig",
    "TopologyConfig",
]
```

- [ ] **Step 5: Run topology tests**

Run:

```bash
uv run pytest tests/config/test_topology.py -q
```

Expected: all config tests pass.

- [ ] **Step 6: Commit Task 9**

```bash
git add src/polar/config/topology.py src/polar/config/__init__.py tests/config/test_topology.py
git commit -m "feat: add evolution topology config"
```

---

### Task 10: Evolution Client for Polar Gateway

**Files:**
- Create: `src/polar_evolution/client.py`
- Test: `tests/evolution/test_server.py`

- [ ] **Step 1: Add failing async client test**

Append to `tests/evolution/test_server.py`:

```python
import pytest

from polar_evolution.client import EvolutionClient


@pytest.mark.asyncio
async def test_evolution_client_resolve_context_with_mock_transport():
    async def handler(request):
        import httpx

        assert request.url.path == "/v1/contexts/resolve"
        return httpx.Response(
            200,
            json={
                "context_id": "ctx_test",
                "memory": {"artifact_ids": [], "rendered_text": ""},
                "skills": [],
                "adapter_merge_spec": {"base_model": "Qwen/Qwen3.6-27B", "merge_mode": "reference_only", "adapters": []},
                "selection": {},
            },
        )

    import httpx

    client = EvolutionClient(
        "http://evolution.test",
        transport=httpx.MockTransport(handler),
    )
    try:
        context = await client.resolve_context(
            {
                "task_id": "task_1",
                "instruction": "solve",
                "agent": {"harness": "codex"},
                "base_model": "Qwen/Qwen3.6-27B",
            }
        )
    finally:
        await client.close()

    assert context["context_id"] == "ctx_test"
```

- [ ] **Step 2: Run client test and verify failure**

Run:

```bash
uv run pytest tests/evolution/test_server.py::test_evolution_client_resolve_context_with_mock_transport -q
```

Expected: FAIL because `polar_evolution.client` does not exist.

- [ ] **Step 3: Implement EvolutionClient**

Create `src/polar_evolution/client.py`:

```python
from __future__ import annotations

from typing import Any

import httpx


class EvolutionClient:
    def __init__(
        self,
        base_url: str,
        *,
        timeout_seconds: float = 10.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self._client = httpx.AsyncClient(
            timeout=timeout_seconds,
            trust_env=False,
            transport=transport,
        )

    async def close(self) -> None:
        await self._client.aclose()

    async def resolve_context(self, payload: dict[str, Any]) -> dict[str, Any]:
        response = await self._client.post(
            f"{self.base_url}/v1/contexts/resolve",
            json=payload,
        )
        response.raise_for_status()
        return response.json()

    async def export_event(self, payload: dict[str, Any]) -> dict[str, Any]:
        response = await self._client.post(f"{self.base_url}/v1/events", json=payload)
        response.raise_for_status()
        return response.json()
```

- [ ] **Step 4: Run client test**

Run:

```bash
uv run pytest tests/evolution/test_server.py::test_evolution_client_resolve_context_with_mock_transport -q
```

Expected: `1 passed`.

- [ ] **Step 5: Commit Task 10**

```bash
git add src/polar_evolution/client.py tests/evolution/test_server.py
git commit -m "feat: add evolution client"
```

---

### Task 11: Gateway Runtime Context Injection

**Files:**
- Modify: `src/polar/gateway/node.py`
- Modify: `src/polar/gateway/server.py`
- Test: `tests/gateway/test_evolution_integration.py`

- [ ] **Step 1: Write failing helper-level injection test**

Create `tests/gateway/test_evolution_integration.py`:

```python
from __future__ import annotations

import json

import pytest

from polar.gateway.node import write_evolution_context_files


class FakeRuntime:
    def __init__(self):
        self.uploads: dict[str, str] = {}

    async def exec(self, command, **kwargs):
        return None

    async def upload_file(self, source, target):
        self.uploads[target] = source.read_text(encoding="utf-8")

    async def upload_dir(self, source, target):
        self.uploads[target] = str(source)


@pytest.mark.asyncio
async def test_write_evolution_context_files(tmp_path):
    runtime = FakeRuntime()
    context = {
        "context_id": "ctx_1",
        "memory": {"rendered_text": "Remember parser precedence."},
        "skills": [],
        "adapter_merge_spec": {"base_model": "Qwen/Qwen3.6-27B", "merge_mode": "reference_only", "adapters": []},
        "selection": {},
    }

    env = await write_evolution_context_files(
        runtime=runtime,
        context=context,
        host_dir=tmp_path,
        target_dir="/polar/session/evolution",
    )

    assert json.loads(runtime.uploads["/polar/session/evolution/context.json"])["context_id"] == "ctx_1"
    assert runtime.uploads["/polar/session/evolution/memory.md"] == "Remember parser precedence."
    assert json.loads(runtime.uploads["/polar/session/evolution/adapters.json"])["merge_mode"] == "reference_only"
    assert env["POLAR_EVOLUTION_CONTEXT"] == "/polar/session/evolution/context.json"
    assert env["POLAR_MEMORY_FILE"] == "/polar/session/evolution/memory.md"
```

- [ ] **Step 2: Run injection test and verify failure**

Run:

```bash
uv run pytest tests/gateway/test_evolution_integration.py::test_write_evolution_context_files -q
```

Expected: FAIL because `write_evolution_context_files` does not exist.

- [ ] **Step 3: Implement context file writer**

Add imports to `src/polar/gateway/node.py`:

```python
import json
```

Add module-level helper near the top of `src/polar/gateway/node.py`:

```python
async def write_evolution_context_files(
    *,
    runtime: BaseRuntime,
    context: dict,
    host_dir: Path,
    target_dir: str,
) -> dict[str, str]:
    evolution_dir = host_dir / "evolution"
    skills_dir = evolution_dir / "skills"
    skills_dir.mkdir(parents=True, exist_ok=True)
    context_path = evolution_dir / "context.json"
    memory_path = evolution_dir / "memory.md"
    adapters_path = evolution_dir / "adapters.json"

    context_path.write_text(json.dumps(context, indent=2, sort_keys=True), encoding="utf-8")
    memory_path.write_text(str((context.get("memory") or {}).get("rendered_text") or ""), encoding="utf-8")
    adapters_path.write_text(
        json.dumps(context.get("adapter_merge_spec") or {}, indent=2, sort_keys=True),
        encoding="utf-8",
    )

    await runtime.upload_file(context_path, f"{target_dir}/context.json")
    await runtime.upload_file(memory_path, f"{target_dir}/memory.md")
    await runtime.upload_file(adapters_path, f"{target_dir}/adapters.json")
    await runtime.upload_dir(skills_dir, f"{target_dir}/skills")

    return {
        "POLAR_EVOLUTION_CONTEXT": f"{target_dir}/context.json",
        "POLAR_MEMORY_FILE": f"{target_dir}/memory.md",
        "POLAR_SKILLS_DIR": f"{target_dir}/skills",
        "POLAR_ADAPTER_MERGE_SPEC": f"{target_dir}/adapters.json",
    }
```

- [ ] **Step 4: Run helper test**

Run:

```bash
uv run pytest tests/gateway/test_evolution_integration.py::test_write_evolution_context_files -q
```

Expected: `1 passed`.

- [ ] **Step 5: Wire EvolutionClient into GatewayNodeManager constructor**

Modify imports:

```python
from polar.config import EvolutionConfig, GatewayNodeConfig, TopologyConfig
from polar_evolution.client import EvolutionClient
```

Modify `GatewayNodeManager.__init__` signature:

```python
        model_served: str | None = None,
        evolution: EvolutionConfig | None = None,
        evolution_client: EvolutionClient | None = None,
```

Inside `__init__`:

```python
        self.model_served = model_served
        self.evolution = evolution
        self.evolution_client = evolution_client
```

Modify `close`:

```python
        if self.evolution_client is not None:
            await self.evolution_client.close()
```

- [ ] **Step 6: Pass config/client from gateway server**

In `src/polar/gateway/server.py`, import:

```python
from polar_evolution.client import EvolutionClient
```

When creating `GatewayNodeManager`, add:

```python
        model_served=node.model_served,
        evolution=topology.evolution,
        evolution_client=(
            EvolutionClient(
                topology.evolution.backend_url,
                timeout_seconds=topology.evolution.context.timeout_seconds,
            )
            if topology.evolution is not None and topology.evolution.enabled
            else None
        ),
```

- [ ] **Step 7: Resolve and inject context before harness setup**

In `GatewayNodeManager._handle_run`, before harness setup, add a call to a new private method:

```python
            evolution_env = await self._resolve_and_inject_evolution_context(
                managed,
                harness,
            )
            if evolution_env:
                harness.env.update(evolution_env)
```

Add private method:

```python
    async def _resolve_and_inject_evolution_context(
        self,
        managed: ManagedSession,
        harness: BaseHarness,
    ) -> dict[str, str]:
        request = managed.request
        if self.evolution is None or not self.evolution.enabled or self.evolution_client is None:
            return {}
        if managed.runtime is None:
            return {}
        payload = {
            "task_id": request.task_id,
            "instruction": request.instruction,
            "agent": request.agent.model_dump(mode="json"),
            "base_model": self.model_served,
            "policy_version": request.metadata.get("policy_version"),
            "rollout_step": request.metadata.get("rollout_step"),
            "metadata": dict(request.metadata),
        }
        try:
            context = await self.evolution_client.resolve_context(payload)
            env = await write_evolution_context_files(
                runtime=managed.runtime,
                context=context,
                host_dir=managed.session_dir,
                target_dir=self.evolution.context.target_dir,
            )
            request.metadata.setdefault("evolution", {})
            request.metadata["evolution"] = {
                **dict(request.metadata.get("evolution") or {}),
                "context_id": context.get("context_id"),
                "context_injected": True,
            }
            return env
        except Exception as exc:
            if not self.evolution.context.fail_open:
                raise
            request.metadata.setdefault("evolution", {})
            request.metadata["evolution"] = {
                **dict(request.metadata.get("evolution") or {}),
                "context_injected": False,
                "error": str(exc),
            }
            logger.warning("Evolution context resolution failed for session %s: %s", request.session_id, exc)
            return {}
```

- [ ] **Step 8: Run gateway evolution helper test and existing gateway tests**

Run:

```bash
uv run pytest tests/gateway/test_evolution_integration.py tests/gateway/test_detection.py -q
```

Expected: all selected tests pass.

- [ ] **Step 9: Commit Task 11**

```bash
git add src/polar/gateway/node.py src/polar/gateway/server.py tests/gateway/test_evolution_integration.py
git commit -m "feat: inject evolution context into gateway runtimes"
```

---

### Task 12: Gateway Event Export

**Files:**
- Modify: `src/polar/gateway/node.py`
- Test: `tests/gateway/test_evolution_integration.py`

- [ ] **Step 1: Add failing event payload test**

Append to `tests/gateway/test_evolution_integration.py`:

```python
from polar.rollout.models import SessionResult, SessionStatus, SessionTiming
from polar.trajectory.models import Trajectory
from polar.gateway.node import build_evolution_session_event


def test_build_evolution_session_event():
    result = SessionResult(
        session_id="ses_1",
        task_id="task_1",
        status=SessionStatus.COMPLETED,
        trajectory=Trajectory(
            status="COMPLETED",
            traces=[],
            metadata={"model_used": "Qwen/Qwen3.6-27B"},
        ),
        timing=SessionTiming(),
        node_id="node-a",
        metadata={"policy_version": "policy_1", "rollout_step": 4},
    )

    event = build_evolution_session_event(result)

    assert event["source"] == "polar"
    assert event["event_type"] == "polar.session_completed"
    assert event["source_event_id"] == "session:ses_1"
    assert event["policy_version"] == "policy_1"
    assert event["rollout_step"] == 4
    assert event["payload"]["session_result"]["session_id"] == "ses_1"
```

- [ ] **Step 2: Run event payload test and verify failure**

Run:

```bash
uv run pytest tests/gateway/test_evolution_integration.py::test_build_evolution_session_event -q
```

Expected: FAIL because `build_evolution_session_event` does not exist.

- [ ] **Step 3: Implement event builder**

Add helper to `src/polar/gateway/node.py`:

```python
def build_evolution_session_event(result: SessionResult) -> dict:
    metadata = dict(result.metadata or {})
    trajectory_metadata = dict(result.trajectory.metadata or {})
    return {
        "source": "polar",
        "event_type": "polar.session_completed",
        "source_event_id": f"session:{result.session_id}",
        "task_id": result.task_id,
        "session_id": result.session_id,
        "policy_version": metadata.get("policy_version") or trajectory_metadata.get("policy_version"),
        "rollout_step": metadata.get("rollout_step") or trajectory_metadata.get("rollout_step"),
        "agent": metadata.get("agent") or {},
        "base_model": trajectory_metadata.get("model_used"),
        "reward": _mean_trace_reward(result.trajectory.traces),
        "status": str(result.status),
        "payload": {"session_result": result.model_dump(mode="json")},
    }


def _mean_trace_reward(traces) -> float | None:
    rewards = [trace.reward for trace in traces if trace.reward is not None]
    if not rewards:
        return None
    return float(sum(rewards) / len(rewards))
```

- [ ] **Step 4: Add best-effort export after normalized result**

In `_handle_postrun`, after `self.session_registry.set_result(request.session_id, normalized)` and before `self.storage.delete_session`, add:

```python
            await self._export_evolution_event(normalized)
```

Add private method:

```python
    async def _export_evolution_event(self, result: SessionResult) -> None:
        if (
            self.evolution is None
            or not self.evolution.enabled
            or not self.evolution.event_export.enabled
            or self.evolution_client is None
        ):
            return
        try:
            await self.evolution_client.export_event(build_evolution_session_event(result))
        except Exception as exc:
            if not self.evolution.event_export.fail_open:
                raise
            logger.warning(
                "Evolution event export failed for session %s: %s",
                result.session_id,
                exc,
            )
```

- [ ] **Step 5: Run event export tests**

Run:

```bash
uv run pytest tests/gateway/test_evolution_integration.py -q
```

Expected: all gateway evolution integration tests pass.

- [ ] **Step 6: Commit Task 12**

```bash
git add src/polar/gateway/node.py tests/gateway/test_evolution_integration.py
git commit -m "feat: export gateway results to evolution backend"
```

---

### Task 13: End-to-End Backend Route Test

**Files:**
- Modify: `tests/evolution/test_server.py`

- [ ] **Step 1: Add full control-plane route test**

Append to `tests/evolution/test_server.py`:

```python
def test_backend_event_dataset_job_context_flow(tmp_path):
    app = create_app(db_path=tmp_path / "evolution.db", artifact_root=tmp_path / "artifacts")
    memory_file = tmp_path / "memory.md"
    memory_file.write_text("Use parser precedence.", encoding="utf-8")

    with TestClient(app) as client:
        event = client.post(
            "/v1/events",
            json={
                "source": "polar",
                "event_type": "polar.session_completed",
                "source_event_id": "session:abc",
                "task_id": "task_1",
                "session_id": "abc",
                "status": "COMPLETED",
                "reward": 1.0,
                "policy_version": "policy_1",
                "payload": {"session_result": {"trajectory": {"traces": [{"reward": 1.0}]}}},
            },
        )
        assert event.status_code == 200
        dataset = client.post(
            "/v1/datasets",
            json={
                "name": "policy_1_success",
                "purpose": "skill_distillation",
                "query": {
                    "event_types": ["polar.session_completed"],
                    "status": ["COMPLETED"],
                    "reward_min": 0.8,
                    "policy_version": "policy_1",
                },
            },
        ).json()
        job = client.post(
            "/v1/jobs",
            json={
                "method": "mock_memory",
                "job_type": "text_memory_mining",
                "input_artifact_ids": [dataset["artifact_id"]],
            },
        ).json()
        claim = client.post(
            "/v1/jobs/claim",
            json={"worker_id": "worker_1", "capabilities": ["text_memory_mining"]},
        ).json()["job"]
        complete = client.post(
            f"/v1/jobs/{job['job_id']}/complete",
            json={
                "lease_id": claim["lease_id"],
                "artifacts": [
                    {
                        "type": "text_memory",
                        "name": "parser memory",
                        "uri": memory_file.as_uri(),
                        "compatibility": {"task_tags": ["calculator"]},
                        "scores": {"quality": 0.9},
                        "tags": ["calculator"],
                        "promoted": True,
                    }
                ],
            },
        ).json()
        context = client.post(
            "/v1/contexts/resolve",
            json={
                "task_id": "task_2",
                "instruction": "fix calculator",
                "agent": {"harness": "codex"},
                "base_model": "Qwen/Qwen3.6-27B",
                "metadata": {"task_tags": ["calculator"]},
            },
        ).json()

    assert dataset["event_count"] == 1
    assert complete["state"] == "succeeded"
    assert "Use parser precedence" in context["memory"]["rendered_text"]
```

- [ ] **Step 2: Run full route test**

Run:

```bash
uv run pytest tests/evolution/test_server.py::test_backend_event_dataset_job_context_flow -q
```

Expected: `1 passed`.

- [ ] **Step 3: Run all evolution tests**

Run:

```bash
uv run pytest tests/evolution -q
```

Expected: all evolution tests pass.

- [ ] **Step 4: Commit Task 13**

```bash
git add tests/evolution/test_server.py
git commit -m "test: cover evolution backend flow"
```

---

### Task 14: Documentation and Example Config

**Files:**
- Create: `src/polar_evolution/README.md`
- Modify: `examples/calculator/topology.yaml`

- [ ] **Step 1: Write backend README**

Create `src/polar_evolution/README.md`:

```markdown
# Polar Evolution Backend

The Evolution Backend is an asynchronous control plane for skill and memory
evolution. It receives Polar session/task events, builds evolution datasets,
leases jobs to external workers, registers artifacts, and resolves context for
future Polar sessions.

Start locally:

```bash
uv run polar-evolution serve --host 127.0.0.1 --port 8200
```

Default state lives under `.polar_evolution/`.

Core APIs:

- `POST /v1/events`
- `POST /v1/datasets`
- `POST /v1/jobs`
- `POST /v1/jobs/claim`
- `POST /v1/jobs/{job_id}/heartbeat`
- `POST /v1/jobs/{job_id}/complete`
- `POST /v1/jobs/{job_id}/fail`
- `POST /v1/contexts/resolve`

The backend does not train LoRA adapters or serve model inference. Parametric
memory artifacts are registered and returned as adapter merge specs for trainer
or inference infrastructure to apply.
```

- [ ] **Step 2: Add commented evolution example to calculator topology**

Append to `examples/calculator/topology.yaml`:

```yaml

# Optional: enable the Evolution Backend control plane.
# Start it with:
#   uv run polar-evolution serve --host 127.0.0.1 --port 8200
# evolution:
#   enabled: true
#   backend_url: http://127.0.0.1:8200
#   context:
#     target_dir: /polar/session/evolution
#     timeout_seconds: 10
#     fail_open: true
#   event_export:
#     enabled: true
#     timeout_seconds: 10
#     fail_open: true
```

- [ ] **Step 3: Verify docs do not affect topology parsing**

Run:

```bash
uv run pytest tests/config/test_topology.py -q
```

Expected: all config tests pass.

- [ ] **Step 4: Commit Task 14**

```bash
git add src/polar_evolution/README.md examples/calculator/topology.yaml
git commit -m "docs: document evolution backend"
```

---

### Task 15: Final Verification

**Files:**
- Verify only.

- [ ] **Step 1: Run lint**

Run:

```bash
uv run ruff check .
```

Expected: `All checks passed!`

- [ ] **Step 2: Run Python test suite**

Run:

```bash
uv run pytest
```

Expected: all tests pass.

- [ ] **Step 3: Run frontend build to confirm existing dashboard remains valid**

Run:

```bash
cd web && npm run build
```

Expected: Vite build exits 0.

- [ ] **Step 4: Run CLI help checks**

Run:

```bash
uv run polar --help
uv run polar-evolution --help
```

Expected: first output contains `serve_gateway`; second output contains `serve`.

- [ ] **Step 5: Commit final verification note if a docs/status file was changed**

If no files changed during Task 15, do not create a commit. If a status document was updated by the engineer, commit only that document:

```bash
git add path/to/status-document.md
git commit -m "docs: record evolution backend verification"
```
