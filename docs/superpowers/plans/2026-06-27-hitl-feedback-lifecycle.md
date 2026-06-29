# HITL Feedback Lifecycle Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Implement the HITL feedback lifecycle from issue #19 and `docs/superpowers/specs/2026-06-27-hitl-feedback-lifecycle-design.md`.

**Architecture:** Add first-class backend review lifecycle models and persistence, then wire OpenEvo promotion gates to optionally create backend review requests and resume pending reviews from submitted feedback. Keep existing local file/TUI human gates working. Export validated human feedback as sanitized evolution feedback and record applications when methods consume it. Start with a deterministic query-decision record so future policies can be trained.

**Tech Stack:** Python, FastAPI, Pydantic, SQLite, pytest, existing `polar_evolution` store/server/client patterns, existing `openevo.experiment` runner/promotion patterns.

---

## File Structure

- `src/polar_evolution/models.py`: add request/response models and enum-like string fields for review requests, review feedback, feedback applications, and query decisions.
- `src/polar_evolution/store.py`: add SQLite tables and store methods for HITL lifecycle objects. Keep JSON payloads in DB fields for MVP, mirroring existing job/context JSON columns.
- `src/polar_evolution/server.py`: add `/v1/reviews`, `/v1/reviews/{id}`, `/v1/reviews/{id}/claim`, `/v1/reviews/{id}/feedback`, `/v1/reviews/{id}/adjudicate`, `/v1/reviews/{id}/resolve`, `/v1/query-decisions`, and `/v1/feedback-applications` routes.
- `src/polar_evolution/client.py`: add async methods for external clients.
- `src/openevo/experiment/clients.py`: add sync protocol/client methods needed by runner.
- `src/openevo/experiment/promotion.py`: expose review packet hashing and conversion of human decisions into typed feedback payloads without breaking current file/TUI behavior.
- `src/openevo/experiment/runner.py`: create backend review requests for human gates when the evolution client supports them, keep local file/TUI fallback, and support resume from submitted backend feedback.
- `src/polar_evolution/methods.py`: ingest sanitized human feedback from dataset records or job config into agent-system reflector prompts and record feedback application metadata in artifact manifests.
- `docs/architecture/evolution-api-and-method-integration.md`: document API and method contract changes.
- `docs/architecture/evolution-backend.md`: document backend review lifecycle.
- Tests:
  - `tests/evolution/test_hitl_reviews.py`
  - `tests/evolution/test_server.py`
  - `tests/openevo/test_experiment_runner.py`
  - `tests/evolution/test_worker_methods.py`

## Task 1: Backend Review Lifecycle Storage and API

**Files:**
- Modify: `src/polar_evolution/models.py`
- Modify: `src/polar_evolution/store.py`
- Modify: `src/polar_evolution/server.py`
- Modify: `src/polar_evolution/client.py`
- Create: `tests/evolution/test_hitl_reviews.py`
- Modify: `tests/evolution/test_server.py`

- [ ] **Step 1: Write failing store/API tests**

Add `tests/evolution/test_hitl_reviews.py` with tests covering:

```python
from __future__ import annotations

import sqlite3

from fastapi.testclient import TestClient

from polar_evolution.models import (
    FeedbackApplicationCreateRequest,
    HumanFeedbackCreateRequest,
    HumanQueryDecisionCreateRequest,
    ReviewRequestCreateRequest,
)
from polar_evolution.server import create_app
from polar_evolution.store import EvolutionStore


def test_store_initializes_hitl_review_tables(tmp_path):
    store = EvolutionStore(db_path=tmp_path / "evolution.db", artifact_root=tmp_path / "artifacts")
    store.initialize()

    with sqlite3.connect(tmp_path / "evolution.db") as conn:
        tables = {
            row[0]
            for row in conn.execute("select name from sqlite_master where type = 'table'")
        }

    assert {
        "review_packets",
        "review_requests",
        "human_feedback",
        "feedback_applications",
        "human_query_decisions",
    }.issubset(tables)


def test_create_review_request_persists_packet_and_hashes(tmp_path):
    store = EvolutionStore(db_path=tmp_path / "evolution.db", artifact_root=tmp_path / "artifacts")
    store.initialize()

    response = store.create_review_request(
        ReviewRequestCreateRequest(
            review_type="promotion",
            artifact_ids=["art_a"],
            candidate_ids=["candidate-a"],
            job_id="job_1",
            task_id="task-a",
            round_index=2,
            method="agent_system_gepa_reflector",
            artifact_type="agent_system",
            packet={
                "trusted_metadata": {"task_id": "task-a"},
                "untrusted_artifact_excerpts": [{"path": "AGENTS.md", "text": "untrusted"}],
                "promotion_support": {"trajectory_findings": ["missed timeout"]},
                "questions": ["Approve?"],
            },
            artifact_hashes={"art_a": "sha256:artifact"},
            query_decision_id="hqd_1",
        )
    )

    assert response.review_id.startswith("rev_")
    assert response.packet_id.startswith("rpacket_")
    assert response.status == "queued"
    assert response.packet_hash.startswith("sha256:")
    assert response.artifact_hashes == {"art_a": "sha256:artifact"}

    fetched = store.get_review_request(response.review_id)
    assert fetched.review_id == response.review_id
    assert fetched.packet["trusted_metadata"]["task_id"] == "task-a"


def test_review_feedback_validation_normalizes_and_keeps_raw_payload(tmp_path):
    store = EvolutionStore(db_path=tmp_path / "evolution.db", artifact_root=tmp_path / "artifacts")
    store.initialize()
    review = store.create_review_request(
        ReviewRequestCreateRequest(
            review_type="promotion",
            artifact_ids=["art_a"],
            job_id="job_1",
            task_id="task-a",
            round_index=0,
            method="text_memory_reflector",
            artifact_type="text_memory",
            packet={"trusted_metadata": {}, "untrusted_artifact_excerpts": [], "questions": []},
            artifact_hashes={"art_a": "sha256:artifact"},
        )
    )

    feedback = store.submit_human_feedback(
        review.review_id,
        HumanFeedbackCreateRequest(
            reviewer_id="alice",
            reviewer_role="maintainer",
            decision="reject",
            score=0.25,
            confidence=0.9,
            rationale="Too broad.",
            observed_issues=["Encourages unbounded search."],
            suggested_changes=["Add bounded inventory."],
            risks=["May overfit."],
            validation_checks=["Run timeout-heavy tasks."],
            raw_payload={"approved": False, "rationale": "Too broad."},
        ),
    )

    assert feedback.feedback_id.startswith("hfb_")
    assert feedback.review_id == review.review_id
    assert feedback.status == "available_for_evolution"
    assert feedback.normalized_payload["observed_issues"] == ["Encourages unbounded search."]
    assert feedback.raw_payload == {"approved": False, "rationale": "Too broad."}


def test_feedback_application_and_query_decision_are_persisted(tmp_path):
    store = EvolutionStore(db_path=tmp_path / "evolution.db", artifact_root=tmp_path / "artifacts")
    store.initialize()

    query = store.create_human_query_decision(
        HumanQueryDecisionCreateRequest(
            artifact_ids=["art_a"],
            candidate_ids=["candidate-a"],
            task_id="task-a",
            round_index=0,
            method="agent_system_gepa_reflector",
            decision="ask_human",
            reason_codes=["candidate_tie", "high_risk"],
            estimated_value_of_information=0.7,
            estimated_human_cost=3.0,
            budget_context={"remaining_reviews": 5},
        )
    )

    review = store.create_review_request(
        ReviewRequestCreateRequest(
            review_type="promotion",
            artifact_ids=["art_a"],
            job_id="job_1",
            task_id="task-a",
            round_index=0,
            method="agent_system_gepa_reflector",
            artifact_type="agent_system",
            packet={"trusted_metadata": {}, "untrusted_artifact_excerpts": [], "questions": []},
            artifact_hashes={"art_a": "sha256:artifact"},
            query_decision_id=query.query_decision_id,
        )
    )
    feedback = store.submit_human_feedback(
        review.review_id,
        HumanFeedbackCreateRequest(
            reviewer_id="alice",
            decision="approve",
            confidence=0.8,
            raw_payload={"approved": True},
        ),
    )
    application = store.create_feedback_application(
        FeedbackApplicationCreateRequest(
            feedback_id=feedback.feedback_id,
            target_type="prompt_seed",
            target_id="job_next",
            consumed_by_method="agent_system_gepa_reflector",
            consumed_in_job_id="job_next",
            effect_summary="Added bounded inventory constraint.",
        )
    )

    assert query.query_decision_id.startswith("hqd_")
    assert application.application_id.startswith("hfa_")
    assert store.list_feedback_applications(feedback_id=feedback.feedback_id)[0].target_id == "job_next"


def test_review_routes_create_claim_feedback_and_resolve(tmp_path):
    app = create_app(db_path=tmp_path / "evolution.db", artifact_root=tmp_path / "artifacts")

    with TestClient(app) as client:
        created = client.post(
            "/v1/reviews",
            json={
                "review_type": "promotion",
                "artifact_ids": ["art_a"],
                "job_id": "job_1",
                "task_id": "task-a",
                "round_index": 0,
                "method": "text_memory_reflector",
                "artifact_type": "text_memory",
                "packet": {
                    "trusted_metadata": {},
                    "untrusted_artifact_excerpts": [],
                    "questions": [],
                },
                "artifact_hashes": {"art_a": "sha256:artifact"},
            },
        )
        review_id = created.json()["review_id"]
        claimed = client.post(
            f"/v1/reviews/{review_id}/claim",
            json={"reviewer_id": "alice", "reviewer_role": "maintainer"},
        )
        feedback = client.post(
            f"/v1/reviews/{review_id}/feedback",
            json={"reviewer_id": "alice", "decision": "approve", "raw_payload": {"approved": True}},
        )
        resolved = client.post(f"/v1/reviews/{review_id}/resolve")

    assert created.status_code == 200
    assert claimed.status_code == 200
    assert claimed.json()["status"] == "in_review"
    assert feedback.status_code == 200
    assert feedback.json()["status"] == "available_for_evolution"
    assert resolved.status_code == 200
    assert resolved.json()["status"] == "resolved"
```

- [ ] **Step 2: Run the failing tests**

Run:

```bash
pytest tests/evolution/test_hitl_reviews.py -q
```

Expected: FAIL because models and store/server methods do not exist yet.

- [ ] **Step 3: Add Pydantic models**

In `src/polar_evolution/models.py`, add:

```python
class ReviewRequestCreateRequest(BaseModel):
    review_type: str = Field(min_length=1)
    artifact_ids: list[str] = Field(default_factory=list)
    candidate_ids: list[str] = Field(default_factory=list)
    job_id: str | None = None
    task_id: str | None = None
    round_index: int | None = None
    method: str | None = None
    artifact_type: str | None = None
    packet: dict[str, Any] = Field(default_factory=dict)
    artifact_hashes: dict[str, str] = Field(default_factory=dict)
    query_decision_id: str | None = None
    priority: int = 100


class ReviewRequestResponse(BaseModel):
    review_id: str
    review_type: str
    status: str
    artifact_ids: list[str] = Field(default_factory=list)
    candidate_ids: list[str] = Field(default_factory=list)
    job_id: str | None = None
    task_id: str | None = None
    round_index: int | None = None
    method: str | None = None
    artifact_type: str | None = None
    packet_id: str
    packet_hash: str
    packet: dict[str, Any] = Field(default_factory=dict)
    artifact_hashes: dict[str, str] = Field(default_factory=dict)
    query_decision_id: str | None = None
    assigned_to: str | None = None
    reviewer_role: str | None = None
    created_at: str
    updated_at: str


class ReviewClaimRequest(BaseModel):
    reviewer_id: str = Field(min_length=1)
    reviewer_role: str | None = None


class HumanFeedbackCreateRequest(BaseModel):
    reviewer_id: str = Field(min_length=1)
    reviewer_role: str | None = None
    decision: str = Field(min_length=1)
    score: float | None = Field(default=None, ge=0.0, le=1.0)
    confidence: float | None = Field(default=None, ge=0.0, le=1.0)
    rationale: str | None = None
    observed_issues: list[str] = Field(default_factory=list)
    suggested_changes: list[str] = Field(default_factory=list)
    risks: list[str] = Field(default_factory=list)
    validation_checks: list[str] = Field(default_factory=list)
    labels: list[str] = Field(default_factory=list)
    raw_payload: dict[str, Any] = Field(default_factory=dict)


class HumanFeedbackResponse(BaseModel):
    feedback_id: str
    review_id: str
    reviewer_id: str
    reviewer_role: str | None = None
    status: str
    decision: str
    score: float | None = None
    confidence: float | None = None
    rationale: str
    raw_payload: dict[str, Any] = Field(default_factory=dict)
    normalized_payload: dict[str, Any] = Field(default_factory=dict)
    created_at: str


class ReviewAdjudicationRequest(BaseModel):
    status: str = "adjudicated"
    rationale: str | None = None


class FeedbackApplicationCreateRequest(BaseModel):
    feedback_id: str = Field(min_length=1)
    target_type: str = Field(min_length=1)
    target_id: str = Field(min_length=1)
    consumed_by_method: str = Field(min_length=1)
    consumed_in_job_id: str | None = None
    effect_summary: str = Field(min_length=1)


class FeedbackApplicationResponse(BaseModel):
    application_id: str
    feedback_id: str
    target_type: str
    target_id: str
    consumed_by_method: str
    consumed_in_job_id: str | None = None
    effect_summary: str
    created_at: str


class HumanQueryDecisionCreateRequest(BaseModel):
    artifact_ids: list[str] = Field(default_factory=list)
    candidate_ids: list[str] = Field(default_factory=list)
    task_id: str | None = None
    round_index: int | None = None
    method: str | None = None
    decision: str = Field(min_length=1)
    reason_codes: list[str] = Field(default_factory=list)
    estimated_value_of_information: float | None = Field(default=None, ge=0.0)
    estimated_human_cost: float | None = Field(default=None, ge=0.0)
    budget_context: dict[str, Any] = Field(default_factory=dict)


class HumanQueryDecisionResponse(BaseModel):
    query_decision_id: str
    artifact_ids: list[str] = Field(default_factory=list)
    candidate_ids: list[str] = Field(default_factory=list)
    task_id: str | None = None
    round_index: int | None = None
    method: str | None = None
    decision: str
    reason_codes: list[str] = Field(default_factory=list)
    estimated_value_of_information: float | None = None
    estimated_human_cost: float | None = None
    budget_context: dict[str, Any] = Field(default_factory=dict)
    actual_latency_seconds: float | None = None
    feedback_changed_promotion: bool | None = None
    feedback_changed_next_candidate: bool | None = None
    downstream_delta: float | None = None
    review_id: str | None = None
    created_at: str
```

- [ ] **Step 4: Add SQLite schema**

In `src/polar_evolution/store.py` extend `SCHEMA` with:

```sql
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
```

- [ ] **Step 5: Implement store methods**

Add helpers in `src/polar_evolution/store.py`:

- `_canonical_json_hash(payload: dict[str, Any]) -> str`
- `_normalize_feedback_payload(request: HumanFeedbackCreateRequest) -> dict[str, Any]`
- row conversion helpers for each new response model.

Add methods:

- `create_review_request`
- `get_review_request`
- `list_review_requests`
- `claim_review_request`
- `submit_human_feedback`
- `adjudicate_review_request`
- `resolve_review_request`
- `create_feedback_application`
- `list_feedback_applications`
- `create_human_query_decision`
- `get_human_query_decision`

Use `new_id("rev")`, `new_id("rpacket")`, `new_id("hfb")`, `new_id("hfa")`, and `new_id("hqd")`.

- [ ] **Step 6: Add server routes**

In `src/polar_evolution/server.py`, import the new models and add routes:

- `POST /v1/reviews`
- `GET /v1/reviews`
- `GET /v1/reviews/{review_id}`
- `POST /v1/reviews/{review_id}/claim`
- `POST /v1/reviews/{review_id}/feedback`
- `POST /v1/reviews/{review_id}/adjudicate`
- `POST /v1/reviews/{review_id}/resolve`
- `POST /v1/query-decisions`
- `GET /v1/query-decisions/{query_decision_id}`
- `POST /v1/feedback-applications`
- `GET /v1/feedback-applications`

Return 404 for unknown review/query IDs and 422 for invalid lifecycle transitions.

- [ ] **Step 7: Add async client methods**

In `src/polar_evolution/client.py`, add async methods matching the routes.

- [ ] **Step 8: Verify backend tests pass**

Run:

```bash
pytest tests/evolution/test_hitl_reviews.py tests/evolution/test_server.py -q
```

Expected: PASS.

- [ ] **Step 9: Commit**

```bash
git add src/polar_evolution/models.py src/polar_evolution/store.py src/polar_evolution/server.py src/polar_evolution/client.py tests/evolution/test_hitl_reviews.py tests/evolution/test_server.py
git commit -m "feat: add HITL review lifecycle backend"
```

## Task 2: OpenEvo Promotion Integration and Async Resume

**Files:**
- Modify: `src/openevo/experiment/clients.py`
- Modify: `src/openevo/experiment/promotion.py`
- Modify: `src/openevo/experiment/runner.py`
- Modify: `tests/openevo/test_experiment_runner.py`

- [ ] **Step 1: Write failing runner tests**

Add tests to `tests/openevo/test_experiment_runner.py`:

```python
def test_human_promotion_gate_creates_backend_review_request_when_supported(tmp_path: Path) -> None:
    review_requests: list[dict[str, Any]] = []

    class ReviewAwareEvolutionClient(FakeEvolutionClient):
        def create_review_request(self, payload: dict[str, Any]) -> dict[str, Any]:
            review_requests.append(payload)
            return {
                "review_id": "rev_backend",
                "status": "queued",
                "packet_id": "rpacket_backend",
                "packet_hash": "sha256:packet",
                **payload,
            }

    evolution = ReviewAwareEvolutionClient(
        artifacts={
            "artifact-text-memory": {
                "artifact_id": "artifact-text-memory",
                "type": "text_memory",
                "name": "candidate memory",
                "uri": "file:///tmp/memory.md",
                "manifest": {
                    "promotion_support": {
                        "trajectory_findings": ["The run timed out after broad scanning."],
                        "proposed_changes": ["Add a bounded source inventory pass."],
                        "expected_benefits": ["Avoid unbounded scans."],
                        "risks": ["Could miss hidden files if bounds are too tight."],
                        "validation_checks": ["Confirm runtime and output completeness."],
                    }
                },
                "compatibility": {},
                "scores": {},
                "tags": [],
                "promoted": False,
            }
        }
    )

    result = run_experiment(
        _config(
            artifacts={
                "text_memory": {"enabled": True},
                "skill_bundle": {"enabled": False},
                "agent_system": {"enabled": False},
            },
            evolution={
                "promotion_gate": {
                    "mode": "human",
                    "decision_timeout_seconds": 0.0,
                }
            },
        ),
        output_dir=tmp_path / "run",
        rollout_client=FakeRolloutClient(),
        evolution_client=evolution,
        worker_runner=FakeWorkerRunner(),
        poll_interval_seconds=0.0,
        max_poll_attempts=1,
    )

    job = result["tasks"][0]["rounds"][0]["jobs"][0]
    assert result["status"] == "pending_review"
    assert review_requests
    assert review_requests[0]["review_type"] == "promotion"
    assert review_requests[0]["artifact_ids"] == ["artifact-text-memory"]
    assert review_requests[0]["packet"]["promotion_support"]["trajectory_findings"]
    assert job["promotion_reviews"][0]["review_id"] == "rev_backend"
    assert job["promotion_reviews"][0]["packet_hash"] == "sha256:packet"


def test_human_promotion_gate_resumes_from_backend_feedback(tmp_path: Path) -> None:
    class ReviewResumeEvolutionClient(FakeEvolutionClient):
        def list_review_requests(self, **_filters: Any) -> list[dict[str, Any]]:
            return [
                {
                    "review_id": "rev_backend",
                    "status": "resolved",
                    "artifact_ids": ["artifact-text-memory"],
                    "packet": {"artifact_type": "text_memory"},
                }
            ]

        def list_human_feedback(self, *, review_id: str) -> list[dict[str, Any]]:
            assert review_id == "rev_backend"
            return [
                {
                    "feedback_id": "hfb_1",
                    "review_id": review_id,
                    "status": "available_for_evolution",
                    "decision": "approve",
                    "score": 1.0,
                    "confidence": 0.9,
                    "rationale": "Looks good.",
                    "normalized_payload": {"suggested_changes": ["Keep bounded inventory."]},
                }
            ]

    evolution = ReviewResumeEvolutionClient(
        artifacts={
            "artifact-text-memory": {
                "artifact_id": "artifact-text-memory",
                "type": "text_memory",
                "name": "candidate memory",
                "uri": "file:///tmp/memory.md",
                "manifest": {
                    "promotion_support": {
                        "trajectory_findings": ["The run timed out after broad scanning."],
                        "proposed_changes": ["Add a bounded source inventory pass."],
                        "expected_benefits": ["Avoid unbounded scans."],
                        "risks": ["Could miss hidden files if bounds are too tight."],
                        "validation_checks": ["Confirm runtime and output completeness."],
                    }
                },
                "compatibility": {},
                "scores": {},
                "tags": [],
                "promoted": False,
            }
        }
    )

    result = openevo_promotion.resume_promotion_from_review_feedback(
        gate_config={"mode": "human", "artifact_types": ["text_memory"]},
        artifact_type="text_memory",
        artifacts=[evolution.artifacts["artifact-text-memory"]],
        review_requests=evolution.list_review_requests(),
        feedback_by_review={
            "rev_backend": evolution.list_human_feedback(review_id="rev_backend")
        },
    )

    assert result["status"] == "approved"
    assert result["approved_artifact_ids"] == ["artifact-text-memory"]
    assert result["reviews"][0]["feedback_id"] == "hfb_1"
```

- [ ] **Step 2: Run failing tests**

Run:

```bash
pytest tests/openevo/test_experiment_runner.py::test_human_promotion_gate_creates_backend_review_request_when_supported tests/openevo/test_experiment_runner.py::test_human_promotion_gate_resumes_from_backend_feedback -q
```

Expected: FAIL because protocol methods and resume helper do not exist.

- [ ] **Step 3: Extend OpenEvo client protocol and HTTP client**

In `src/openevo/experiment/clients.py`, add optional methods to `EvolutionClientProtocol` and implementations on `EvolutionHttpClient`:

- `create_review_request(payload)`
- `list_review_requests(**filters)`
- `submit_human_feedback(review_id, payload)`
- `list_human_feedback(review_id=...)`
- `create_feedback_application(payload)`
- `create_human_query_decision(payload)`

Use `hasattr` checks in runner so old fakes and older backends remain compatible.

- [ ] **Step 4: Add review packet hash helper and backend payload conversion**

In `src/openevo/experiment/promotion.py`, add:

- `review_packet_hash(packet: Mapping[str, Any]) -> str`
- `review_request_payload_from_packet(...) -> dict[str, Any]`
- `decision_from_backend_feedback(feedback: Mapping[str, Any]) -> dict[str, Any]`
- `resume_promotion_from_review_feedback(...) -> dict[str, Any]`

Use the same `sha256:` canonical JSON style as backend.

- [ ] **Step 5: Wire backend review request creation in runner**

In `src/openevo/experiment/runner.py`, after `evaluate_promotion_gate` returns pending human reviews, detect `hasattr(evolution_client, "create_review_request")`. For every review with `status == "pending_review"` and a `review_path`, load the packet JSON and create a backend review request. Add returned `review_id`, `packet_id`, and `packet_hash` to the corresponding `promotion_reviews` entry.

Keep existing local file/TUI behavior unchanged.

- [ ] **Step 6: Verify OpenEvo focused tests pass**

Run:

```bash
pytest tests/openevo/test_experiment_runner.py -q
```

Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add src/openevo/experiment/clients.py src/openevo/experiment/promotion.py src/openevo/experiment/runner.py tests/openevo/test_experiment_runner.py
git commit -m "feat: connect OpenEvo promotion gates to HITL reviews"
```

## Task 3: Feedback Ingestion and Method Consumption

**Files:**
- Modify: `src/polar_evolution/store.py`
- Modify: `src/polar_evolution/methods.py`
- Modify: `tests/evolution/test_worker_methods.py`
- Modify: `tests/evolution/test_datasets_jobs.py`

- [ ] **Step 1: Write failing tests**

Add tests that prove:

- A dataset record includes sanitized `payload.session_result.metadata.evolution_feedback.human` when the source event payload has validated human feedback.
- `agent_system_history_reflector` and `agent_system_gepa_reflector` include human feedback summaries in reflection/candidate prompts.
- Output artifact manifests include consumed `human_feedback_ids` and a feedback application-ready summary.

Use existing worker method tests that monkeypatch reflector generation and inspect captured prompts.

- [ ] **Step 2: Run failing tests**

Run:

```bash
pytest tests/evolution/test_datasets_jobs.py tests/evolution/test_worker_methods.py -q
```

Expected: FAIL for new tests only.

- [ ] **Step 3: Add sanitizer/extractor helpers**

In `src/polar_evolution/methods.py`, add helpers:

- `_human_feedback_from_records(records: list[dict[str, Any]]) -> list[dict[str, Any]]`
- `_render_human_feedback_summary(feedback: list[dict[str, Any]]) -> str`
- `_human_feedback_ids(feedback: list[dict[str, Any]]) -> list[str]`

Only include normalized fields: `feedback_id`, `decision`, `confidence`, `observed_issues`, `suggested_changes`, `risks`, `validation_checks`, and `labels`.

- [ ] **Step 4: Add human feedback to agent-system prompts**

In `agent_system_history_reflector` and `agent_system_gepa_reflector`, include a bounded "Human feedback signals" section when sanitized feedback exists.

In artifact manifests, add:

```python
"human_feedback_ids": _human_feedback_ids(human_feedback),
"human_feedback_count": len(human_feedback),
```

Also add a promotion support finding such as:

```python
f"Consumed {len(human_feedback)} human feedback item(s) from prior reviews."
```

- [ ] **Step 5: Verify method tests pass**

Run:

```bash
pytest tests/evolution/test_worker_methods.py -q
```

Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add src/polar_evolution/store.py src/polar_evolution/methods.py tests/evolution/test_worker_methods.py tests/evolution/test_datasets_jobs.py
git commit -m "feat: feed human review signals into evolution methods"
```

## Task 4: Query Policy Records and Documentation

**Files:**
- Modify: `src/openevo/experiment/promotion.py`
- Modify: `src/openevo/experiment/runner.py`
- Modify: `tests/openevo/test_experiment_runner.py`
- Modify: `docs/architecture/evolution-api-and-method-integration.md`
- Modify: `docs/architecture/evolution-backend.md`
- Modify: `README.md`

- [ ] **Step 1: Write failing query-decision tests**

Add a runner test proving human gate review creation records a query decision with reason codes:

```python
def test_human_gate_records_query_decision_before_backend_review(tmp_path: Path) -> None:
    query_decisions: list[dict[str, Any]] = []
    review_requests: list[dict[str, Any]] = []

    class QueryAwareEvolutionClient(FakeEvolutionClient):
        def create_human_query_decision(self, payload: dict[str, Any]) -> dict[str, Any]:
            query_decisions.append(payload)
            return {"query_decision_id": "hqd_1", **payload}

        def create_review_request(self, payload: dict[str, Any]) -> dict[str, Any]:
            review_requests.append(payload)
            return {
                "review_id": "rev_1",
                "status": "queued",
                "packet_id": "rpacket_1",
                "packet_hash": "sha256:packet",
                **payload,
            }
```

Assert reason codes include `promotion_gate_targeted` and `human_gate`.

- [ ] **Step 2: Run failing test**

Run the single new test and confirm it fails.

- [ ] **Step 3: Implement deterministic query-decision payload**

Add a helper in `promotion.py` or `runner.py` that creates:

```python
{
    "artifact_ids": [...],
    "candidate_ids": [...],
    "task_id": task_id,
    "round_index": round_index,
    "method": method,
    "decision": "ask_human",
    "reason_codes": ["promotion_gate_targeted", "human_gate"],
    "estimated_value_of_information": None,
    "estimated_human_cost": None,
    "budget_context": {},
}
```

Call `create_human_query_decision` before `create_review_request` when supported, and pass the returned `query_decision_id` into review request payloads.

- [ ] **Step 4: Update docs**

Update docs to explain:

- backend HITL lifecycle APIs
- async review behavior
- typed human feedback
- feedback applications
- query-decision records
- current limitations: no RLHF/reward model yet, deterministic query policy only

- [ ] **Step 5: Verify docs and tests**

Run:

```bash
pytest tests/openevo/test_experiment_runner.py tests/evolution/test_hitl_reviews.py -q
git diff --check
```

- [ ] **Step 6: Commit**

```bash
git add src/openevo/experiment/promotion.py src/openevo/experiment/runner.py tests/openevo/test_experiment_runner.py docs/architecture/evolution-api-and-method-integration.md docs/architecture/evolution-backend.md README.md
git commit -m "docs: document HITL lifecycle query policy"
```

## Task 5: Final Verification and Review

**Files:**
- No planned production edits.

- [ ] **Step 1: Run focused test suite**

```bash
pytest tests/evolution/test_hitl_reviews.py tests/evolution/test_server.py tests/evolution/test_datasets_jobs.py tests/evolution/test_worker_methods.py tests/openevo/test_experiment_runner.py -q
```

- [ ] **Step 2: Run patch hygiene**

```bash
git status --short
git diff --check
```

- [ ] **Step 3: Final subagent review**

Dispatch a final reviewer with issue #19, the design spec, and the full git diff from before the implementation to current HEAD. Fix Critical and Important findings, then re-review until no actionable findings remain.

- [ ] **Step 4: Prepare PR**

Push the branch and open a PR that references issue #19 with `Fixes #19`, includes docs and tests run, and notes any intentionally deferred scope.

## Self-Review

Spec coverage:

- Review lifecycle objects: Task 1.
- Typed feedback: Task 1.
- Feedback applications: Task 1 and Task 3.
- Async pending review and resume: Task 2.
- Feedback ingestion/method consumption: Task 3.
- Query policy records: Task 4.
- Docs/tests/review loop: Task 4 and Task 5.

Known scope control:

- This plan does not implement RLHF, PPO, DPO, or learned query policies.
- This plan does not replace local file/TUI review; it adds backend lifecycle support while preserving compatibility.
- The first query policy is deterministic and logged, matching the design MVP.
