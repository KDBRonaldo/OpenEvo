from __future__ import annotations

import hashlib
import json
import sqlite3

from fastapi.testclient import TestClient
from pydantic import ValidationError
import pytest

from polar_evolution.models import (
    FeedbackApplicationCreateRequest,
    FeedbackApplicationResponse,
    FeedbackApplicationTargetType,
    HumanFeedbackCreateRequest,
    HumanFeedbackDecision,
    HumanFeedbackResponse,
    HumanFeedbackStatus,
    HumanQueryDecision,
    HumanQueryDecisionCreateRequest,
    HumanQueryDecisionResponse,
    ReviewPacket,
    ReviewAdjudicationRequest,
    ReviewClaimRequest,
    ReviewRequestCreateRequest,
    ReviewRequestResponse,
    ReviewStatus,
    ReviewType,
)
from polar_evolution.server import create_app
from polar_evolution.store import EvolutionStore


def _create_submitted_review(
    store: EvolutionStore,
    *,
    artifact_id: str = "art_a",
    reviewer_id: str = "alice",
):
    review = store.create_review_request(
        ReviewRequestCreateRequest(
            review_type="promotion",
            artifact_ids=[artifact_id],
            packet={"questions": ["Approve?"]},
        )
    )
    store.claim_review_request(review.review_id, ReviewClaimRequest(reviewer_id=reviewer_id))
    store.submit_human_feedback(
        review.review_id,
        HumanFeedbackCreateRequest(reviewer_id=reviewer_id, decision="approve"),
    )
    return store.get_review_request(review.review_id)


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
    query = store.create_human_query_decision(
        HumanQueryDecisionCreateRequest(decision="ask_human")
    )

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
            query_decision_id=query.query_decision_id,
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
    assert fetched.query_decision_id == query.query_decision_id
    assert store.get_human_query_decision(query.query_decision_id).review_id == response.review_id


def test_review_packet_normalizes_fields_and_preserves_extra_data(tmp_path):
    store = EvolutionStore(db_path=tmp_path / "evolution.db", artifact_root=tmp_path / "artifacts")
    store.initialize()
    packet = ReviewPacket(
        trusted_metadata={"task_id": "task-a"},
        questions=["Approve?"],
        custom_support={"source": "reviewer-note"},
    )

    response = store.create_review_request(
        ReviewRequestCreateRequest(
            review_type="promotion",
            artifact_ids=["art_a"],
            packet=packet,
        )
    )
    duplicate = store.create_review_request(
        ReviewRequestCreateRequest(
            review_type="promotion",
            artifact_ids=["art_b"],
            packet={
                "questions": ["Approve?"],
                "trusted_metadata": {"task_id": "task-a"},
                "custom_support": {"source": "reviewer-note"},
            },
        )
    )
    packet_response = store.get_review_packet(response.packet_id)
    listed_packets = store.list_review_packets()

    assert response.packet["trusted_metadata"] == {"task_id": "task-a"}
    assert response.packet["untrusted_artifact_excerpts"] == []
    assert response.packet["promotion_support"] == {}
    assert response.packet["questions"] == ["Approve?"]
    assert response.packet["custom_support"] == {"source": "reviewer-note"}
    assert duplicate.packet_id == response.packet_id
    assert duplicate.packet_hash == response.packet_hash
    assert packet_response.packet_id == response.packet_id
    assert packet_response.packet_hash == response.packet_hash
    assert packet_response.packet["custom_support"] == {"source": "reviewer-note"}
    assert [packet.packet_id for packet in listed_packets] == [response.packet_id]


def test_create_review_request_sanitizes_packet_before_hashing_and_storage(tmp_path):
    store = EvolutionStore(db_path=tmp_path / "evolution.db", artifact_root=tmp_path / "artifacts")
    store.initialize()

    response = store.create_review_request(
        ReviewRequestCreateRequest(
            review_type="promotion",
            artifact_ids=["art_a"],
            packet={
                "artifact": {
                    "artifact_id": "art_a",
                    "uri": "file:///home/alice/private/memory.md",
                    "manifest": {
                        "content_uri": "file:///tmp/artifacts/memory.md",
                        "local_path": "/home/alice/private/memory.md",
                        "api_key": "sk-live-secret",
                    },
                },
                "artifact_content": {
                    "source_uri": "file:///tmp/artifacts/memory.md",
                    "excerpts": [
                        {
                            "path": "/home/alice/private/memory.md",
                            "text": (
                                "fetch https://user:pass@example.test/report"
                                "?token=secret-token#frag with Authorization: Bearer abc123 "
                                "and AWS_ACCESS_KEY_ID=AKIAIOSFODNN7EXAMPLE"
                            ),
                        }
                    ],
                },
                "trusted_metadata": {
                    "token": "raw-token",
                    "windows_path": r"C:\Users\Alice\secret.txt",
                },
                "extra_packet_context": {
                    "source_path": "/Users/alice/.aws/credentials",
                    "source_uri": "memory.md?signature=relative-secret#frag",
                    "https://user:pass@example.test/key?token=key-secret#frag": (
                        "keyed context"
                    ),
                    "/home/alice/private-key.md": "keyed local path",
                    "scratch_path": "/scratch/alice/private-key.md",
                    "nested": {"password": "correct-horse"},
                },
                "questions": ["Approve /home/alice/private/memory.md?"],
            },
        )
    )

    fetched = store.get_review_request(response.review_id)
    packet_response = store.get_review_packet(response.packet_id)
    listed_packets = store.list_review_packets()
    serialized_packet = json.dumps(fetched.packet.model_dump(mode="python"), sort_keys=True)

    for raw_secret in (
        "file:///home/alice/private/memory.md",
        "file:///tmp/artifacts/memory.md",
        "/home/alice/private/memory.md",
        "/Users/alice/.aws/credentials",
        r"C:\Users\Alice\secret.txt",
        "user:pass@example.test",
        "secret-token",
        "abc123",
        "AKIAIOSFODNN7EXAMPLE",
        "sk-live-secret",
        "raw-token",
        "correct-horse",
        "relative-secret",
        "key-secret",
        "/home/alice/private-key.md",
        "/scratch/alice/private-key.md",
        "signature=",
        "#frag",
    ):
        assert raw_secret not in serialized_packet
    assert "[LOCAL_ARTIFACT_URI]" in serialized_packet
    assert "[LOCAL_ARTIFACT_PATH]" in serialized_packet
    assert "[REDACTED]" in serialized_packet
    assert "https://example.test/report?<redacted>" in serialized_packet
    assert "https://example.test/key?<redacted>" in serialized_packet
    assert "memory.md?<redacted>" in serialized_packet
    assert packet_response.packet == fetched.packet
    assert listed_packets[0].packet == fetched.packet

    sanitized_packet = fetched.packet.model_dump(mode="json")
    expected_hash = (
        "sha256:"
        + hashlib.sha256(
            json.dumps(sanitized_packet, sort_keys=True, allow_nan=False).encode("utf-8")
        ).hexdigest()
    )
    assert response.packet_hash == expected_hash

    with store.connect() as conn:
        row = conn.execute(
            "SELECT packet_json FROM review_packets WHERE packet_id = ?",
            (response.packet_id,),
        ).fetchone()

    assert row is not None
    assert json.loads(row["packet_json"]) == sanitized_packet


def test_create_review_request_sanitizes_target_metadata(tmp_path):
    store = EvolutionStore(db_path=tmp_path / "evolution.db", artifact_root=tmp_path / "artifacts")
    store.initialize()

    response = store.create_review_request(
        ReviewRequestCreateRequest(
            review_type="promotion",
            artifact_ids=[
                "artifact-tokenizer-memory",
                "token:raw-review-token",
                "/home/alice/private/artifact-memory.md",
                "https://user:pass@example.test/artifacts/art_a?token=secret-token#frag",
            ],
            candidate_ids=[
                "candidate-tokenizer-v2",
                r"C:\Users\Alice\candidate-secret.md",
                "memory.md?signature=relative-secret#frag",
            ],
            artifact_hashes={
                "token:raw-hash-token": "secret://method/raw-hash-secret?x=y",
                "/home/alice/private/artifact-memory.md": "sha256:" + "a" * 64,
                "https://user:pass@example.test/artifacts/art_a?token=secret-token#frag": (
                    "sha256:" + "b" * 64
                ),
            },
            packet={"questions": ["Approve?"]},
        )
    )

    fetched = store.get_review_request(response.review_id)
    listed = store.list_review_requests()[0]
    serialized = json.dumps(
        {
            "response": response.model_dump(mode="python"),
            "fetched": fetched.model_dump(mode="python"),
            "listed": listed.model_dump(mode="python"),
        },
        sort_keys=True,
    )

    for raw_secret in (
        "/home/alice/private/artifact-memory.md",
        r"C:\Users\Alice\candidate-secret.md",
        "user:pass@example.test",
        "raw-review-token",
        "raw-hash-token",
        "raw-hash-secret",
        "secret-token",
        "relative-secret",
        "signature=",
        "#frag",
    ):
        assert raw_secret not in serialized
    assert "artifact-tokenizer-memory" in response.artifact_ids
    assert "candidate-tokenizer-v2" in response.candidate_ids
    assert "[LOCAL_ARTIFACT_PATH]" in serialized
    assert "[REDACTED]" in serialized
    assert "https://example.test/artifacts/art_a?<redacted>" in serialized
    assert "memory.md?<redacted>" in serialized


def test_create_review_request_can_atomically_create_query_decision(tmp_path):
    store = EvolutionStore(db_path=tmp_path / "evolution.db", artifact_root=tmp_path / "artifacts")
    store.initialize()

    response = store.create_review_request(
        ReviewRequestCreateRequest(
            review_type="promotion",
            artifact_ids=["art_a"],
            candidate_ids=["candidate-a"],
            task_id="task-a",
            round_index=1,
            method="agent_system_gepa_reflector",
            packet={"questions": ["Approve?"]},
            query_decision={
                "artifact_ids": ["art_a"],
                "candidate_ids": ["candidate-a"],
                "task_id": "task-a",
                "round_index": 1,
                "method": "agent_system_gepa_reflector",
                "decision": "ask_human",
                "reason_codes": ["promotion_gate_targeted", "human_gate"],
                "estimated_value_of_information": None,
                "estimated_human_cost": None,
                "budget_context": {},
            },
        )
    )

    assert response.query_decision_id is not None
    query = store.get_human_query_decision(response.query_decision_id)
    assert query.review_id == response.review_id
    assert query.artifact_ids == ["art_a"]
    assert query.candidate_ids == ["candidate-a"]
    assert query.decision == HumanQueryDecision.ASK_HUMAN


def test_create_review_request_rejects_inline_and_existing_query_decision(tmp_path):
    store = EvolutionStore(db_path=tmp_path / "evolution.db", artifact_root=tmp_path / "artifacts")
    store.initialize()
    query = store.create_human_query_decision(
        HumanQueryDecisionCreateRequest(decision="ask_human")
    )

    try:
        store.create_review_request(
            ReviewRequestCreateRequest(
                review_type="promotion",
                artifact_ids=["art_a"],
                packet={"questions": ["Approve?"]},
                query_decision_id=query.query_decision_id,
                query_decision={"decision": "ask_human"},
            )
        )
    except ValueError as exc:
        assert "cannot include both query_decision and query_decision_id" in str(exc)
    else:
        raise AssertionError("expected mixed query decision forms to be rejected")

    assert store.get_human_query_decision(query.query_decision_id).review_id is None


def test_create_review_request_requires_artifact_or_candidate_target(tmp_path):
    store = EvolutionStore(db_path=tmp_path / "evolution.db", artifact_root=tmp_path / "artifacts")
    store.initialize()

    with pytest.raises(ValidationError, match="artifact_ids or candidate_ids"):
        ReviewRequestCreateRequest(
            review_type="promotion",
            packet={"questions": ["Approve?"]},
        )

    app = create_app(db_path=tmp_path / "api.db", artifact_root=tmp_path / "api-artifacts")
    with TestClient(app) as client:
        response = client.post(
            "/v1/reviews",
            json={
                "review_type": "promotion",
                "packet": {"questions": ["Approve?"]},
            },
        )

    assert response.status_code == 422
    assert "artifact_ids or candidate_ids" in response.text


def test_review_feedback_validation_normalizes_and_persists_raw_payload_without_exposing_it(
    tmp_path,
):
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
    store.claim_review_request(
        review.review_id,
        ReviewClaimRequest(reviewer_id="alice", reviewer_role="maintainer"),
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
    assert "raw_payload" not in feedback.model_dump()
    listed_feedback = store.list_human_feedback(review_id=review.review_id)
    assert len(listed_feedback) == 1
    assert "raw_payload" not in listed_feedback[0].model_dump()

    with store.connect() as conn:
        row = conn.execute(
            "SELECT raw_payload_json FROM human_feedback WHERE feedback_id = ?",
            (feedback.feedback_id,),
        ).fetchone()

    assert row is not None
    assert json.loads(row["raw_payload_json"]) == {
        "approved": False,
        "rationale": "Too broad.",
    }


def test_review_feedback_normalized_payload_is_sanitized_before_storage(tmp_path):
    store = EvolutionStore(db_path=tmp_path / "evolution.db", artifact_root=tmp_path / "artifacts")
    store.initialize()
    review = store.create_review_request(
        ReviewRequestCreateRequest(
            review_type="promotion",
            artifact_ids=["art_a"],
            packet={"questions": ["Approve?"]},
        )
    )
    store.claim_review_request(review.review_id, ReviewClaimRequest(reviewer_id="alice"))

    feedback = store.submit_human_feedback(
        review.review_id,
        HumanFeedbackCreateRequest(
            reviewer_id="alice",
            decision="approve",
            rationale=(
                "Read file:///tmp/private.md and /home/alice/private.md with "
                "Authorization: Bearer abc123"
            ),
            observed_issues=[
                "AWS_ACCESS_KEY_ID=AKIAIOSFODNN7EXAMPLE found in /Users/alice/.aws/credentials"
            ],
            suggested_changes=[
                "Fetch https://user:pass@example.test/path?token=secret-token#frag"
            ],
            labels=["token=secret-token", r"C:\Users\Alice\secret.txt"],
        ),
    )

    listed = store.list_human_feedback(review_id=review.review_id)
    assert listed[0].normalized_payload == feedback.normalized_payload
    serialized_payload = json.dumps(feedback.normalized_payload, sort_keys=True)
    for raw_secret in (
        "file:///tmp/private.md",
        "/home/alice/private.md",
        "/Users/alice/.aws/credentials",
        r"C:\Users\Alice\secret.txt",
        "abc123",
        "AKIAIOSFODNN7EXAMPLE",
        "user:pass@example.test",
        "secret-token",
    ):
        assert raw_secret not in serialized_payload
    assert "[LOCAL_ARTIFACT_URI]" in serialized_payload
    assert "[LOCAL_ARTIFACT_PATH]" in serialized_payload
    assert "[REDACTED]" in serialized_payload
    assert "https://example.test/path?<redacted>" in serialized_payload

    with store.connect() as conn:
        row = conn.execute(
            "SELECT normalized_payload_json, raw_payload_json FROM human_feedback WHERE feedback_id = ?",
            (feedback.feedback_id,),
        ).fetchone()

    assert row is not None
    assert json.loads(row["normalized_payload_json"]) == feedback.normalized_payload


def test_human_feedback_request_rejects_boolean_score_and_confidence() -> None:
    for field_name, value in (("score", True), ("confidence", False)):
        try:
            HumanFeedbackCreateRequest(
                reviewer_id="alice",
                decision="approve",
                **{field_name: value},
            )
        except ValidationError:
            pass
        else:
            raise AssertionError(f"expected boolean {field_name} to be rejected")

    request = HumanFeedbackCreateRequest(
        reviewer_id="alice",
        decision="approve",
        score=1,
        confidence=0.75,
    )

    assert request.score == 1.0
    assert request.confidence == 0.75


def test_store_cannot_persist_boolean_score_or_confidence_as_available_feedback(
    tmp_path,
) -> None:
    store = EvolutionStore(db_path=tmp_path / "evolution.db", artifact_root=tmp_path / "artifacts")
    store.initialize()
    review = store.create_review_request(
        ReviewRequestCreateRequest(
            review_type="promotion",
            artifact_ids=["art_a"],
            packet={"questions": ["Approve?"]},
        )
    )
    store.claim_review_request(review.review_id, ReviewClaimRequest(reviewer_id="alice"))

    try:
        request = HumanFeedbackCreateRequest(
            reviewer_id="alice",
            decision="approve",
            score=True,
        )
    except ValidationError:
        pass
    else:
        store.submit_human_feedback(review.review_id, request)
        raise AssertionError("expected boolean score to be rejected before persistence")

    assert store.list_human_feedback(review_id=review.review_id) == []


def test_create_review_request_rejects_unknown_query_decision_id(tmp_path):
    store = EvolutionStore(db_path=tmp_path / "evolution.db", artifact_root=tmp_path / "artifacts")
    store.initialize()

    try:
        store.create_review_request(
            ReviewRequestCreateRequest(
                review_type="promotion",
                artifact_ids=["art_a"],
                packet={"questions": ["Approve?"]},
                query_decision_id="hqd_missing",
            )
        )
    except ValueError as exc:
        assert str(exc) == "unknown query decision: hqd_missing"
    else:
        raise AssertionError("expected unknown query decision to be rejected")


def test_create_review_request_rejects_reused_query_decision_id(tmp_path):
    store = EvolutionStore(db_path=tmp_path / "evolution.db", artifact_root=tmp_path / "artifacts")
    store.initialize()
    query = store.create_human_query_decision(
        HumanQueryDecisionCreateRequest(decision="ask_human")
    )
    first = store.create_review_request(
        ReviewRequestCreateRequest(
            review_type="promotion",
            artifact_ids=["art_a"],
            packet={"questions": ["Approve?"]},
            query_decision_id=query.query_decision_id,
        )
    )

    try:
        store.create_review_request(
            ReviewRequestCreateRequest(
                review_type="promotion",
                artifact_ids=["art_b"],
                packet={"questions": ["Approve another?"]},
                query_decision_id=query.query_decision_id,
            )
        )
    except ValueError as exc:
        assert str(exc) == f"query decision already linked to review: {first.review_id}"
    else:
        raise AssertionError("expected reused query decision to be rejected")


def test_review_schema_upgrade_adds_unique_query_decision_index(tmp_path):
    store = EvolutionStore(db_path=tmp_path / "evolution.db", artifact_root=tmp_path / "artifacts")
    store.initialize()

    with sqlite3.connect(tmp_path / "evolution.db") as conn:
        index_rows = conn.execute(
            """
            SELECT name, sql
            FROM sqlite_master
            WHERE type = 'index'
              AND tbl_name = 'review_requests'
              AND name = 'idx_review_requests_query_decision_id_unique'
            """
        ).fetchall()

    assert len(index_rows) == 1
    assert "WHERE query_decision_id IS NOT NULL" in index_rows[0][1]


def test_claim_review_rejects_different_reviewer_overwrite(tmp_path):
    store = EvolutionStore(db_path=tmp_path / "evolution.db", artifact_root=tmp_path / "artifacts")
    store.initialize()
    review = store.create_review_request(
        ReviewRequestCreateRequest(
            review_type="promotion",
            artifact_ids=["art_a"],
            packet={"questions": ["Approve?"]},
        )
    )

    first_claim = store.claim_review_request(
        review.review_id,
        ReviewClaimRequest(reviewer_id="alice", reviewer_role="maintainer"),
    )
    retry_claim = store.claim_review_request(
        review.review_id,
        ReviewClaimRequest(reviewer_id="alice"),
    )
    try:
        store.claim_review_request(
            review.review_id,
            ReviewClaimRequest(reviewer_id="bob", reviewer_role="maintainer"),
        )
    except ValueError as exc:
        assert str(exc) == f"review already claimed by another reviewer: {review.review_id}"
    else:
        raise AssertionError("expected different reviewer claim to be rejected")

    assert first_claim.assigned_to == "alice"
    assert retry_claim.assigned_to == "alice"
    assert retry_claim.reviewer_role == "maintainer"


def test_submit_feedback_rejects_reviewer_that_does_not_own_claim(tmp_path):
    store = EvolutionStore(db_path=tmp_path / "evolution.db", artifact_root=tmp_path / "artifacts")
    store.initialize()
    bob_review = store.create_review_request(
        ReviewRequestCreateRequest(
            review_type="promotion",
            artifact_ids=["art_a"],
            packet={"questions": ["Approve?"]},
        )
    )
    alice_review = store.create_review_request(
        ReviewRequestCreateRequest(
            review_type="promotion",
            artifact_ids=["art_b"],
            packet={"questions": ["Approve?"]},
        )
    )
    store.claim_review_request(
        bob_review.review_id,
        ReviewClaimRequest(reviewer_id="alice", reviewer_role="maintainer"),
    )
    store.claim_review_request(
        alice_review.review_id,
        ReviewClaimRequest(reviewer_id="alice", reviewer_role="maintainer"),
    )

    try:
        store.submit_human_feedback(
            bob_review.review_id,
            HumanFeedbackCreateRequest(reviewer_id="bob", decision="approve"),
        )
    except ValueError as exc:
        assert str(exc) == f"review claimed by a different reviewer: {bob_review.review_id}"
    else:
        raise AssertionError("expected feedback by non-owner reviewer to be rejected")

    assert store.get_review_request(bob_review.review_id).status == "in_review"
    alice_feedback = store.submit_human_feedback(
        alice_review.review_id,
        HumanFeedbackCreateRequest(reviewer_id="alice", decision="approve"),
    )
    assert alice_feedback.review_id == alice_review.review_id
    assert store.get_review_request(alice_review.review_id).status == "submitted"


def test_submit_feedback_enforces_and_inherits_claimed_reviewer_role(tmp_path):
    store = EvolutionStore(db_path=tmp_path / "evolution.db", artifact_root=tmp_path / "artifacts")
    store.initialize()
    mismatch_review = store.create_review_request(
        ReviewRequestCreateRequest(
            review_type="promotion",
            artifact_ids=["art_a"],
            packet={"questions": ["Approve?"]},
        )
    )
    inherited_role_review = store.create_review_request(
        ReviewRequestCreateRequest(
            review_type="promotion",
            artifact_ids=["art_b"],
            packet={"questions": ["Approve?"]},
        )
    )
    store.claim_review_request(
        mismatch_review.review_id,
        ReviewClaimRequest(reviewer_id="alice", reviewer_role="maintainer"),
    )
    store.claim_review_request(
        inherited_role_review.review_id,
        ReviewClaimRequest(reviewer_id="alice", reviewer_role="maintainer"),
    )

    try:
        store.submit_human_feedback(
            mismatch_review.review_id,
            HumanFeedbackCreateRequest(
                reviewer_id="alice",
                reviewer_role="observer",
                decision="approve",
            ),
        )
    except ValueError as exc:
        assert str(exc) == (
            f"feedback reviewer role does not match claimed role: {mismatch_review.review_id}"
        )
    else:
        raise AssertionError("expected mismatched reviewer role to be rejected")

    feedback = store.submit_human_feedback(
        inherited_role_review.review_id,
        HumanFeedbackCreateRequest(reviewer_id="alice", decision="approve"),
    )

    assert store.get_review_request(mismatch_review.review_id).status == "in_review"
    assert feedback.reviewer_role == "maintainer"
    assert feedback.normalized_payload["reviewer_role"] == "maintainer"


def test_review_lifecycle_rejects_invalid_transitions_and_statuses(tmp_path):
    store = EvolutionStore(db_path=tmp_path / "evolution.db", artifact_root=tmp_path / "artifacts")
    store.initialize()
    queued = store.create_review_request(
        ReviewRequestCreateRequest(
            review_type="promotion",
            artifact_ids=["art_a"],
            packet={"questions": ["Approve?"]},
        )
    )

    for action in (
        lambda: store.resolve_review_request(queued.review_id),
        lambda: store.adjudicate_review_request(
            queued.review_id,
            ReviewAdjudicationRequest(status="adjudicated"),
        ),
        lambda: store.submit_human_feedback(
            queued.review_id,
            HumanFeedbackCreateRequest(reviewer_id="alice", decision="approve"),
        ),
    ):
        try:
            action()
        except ValueError:
            pass
        else:
            raise AssertionError("expected invalid queued transition to be rejected")

    claimed = store.claim_review_request(
        queued.review_id,
        ReviewClaimRequest(reviewer_id="alice", reviewer_role="maintainer"),
    )
    submitted_feedback = store.submit_human_feedback(
        claimed.review_id,
        HumanFeedbackCreateRequest(reviewer_id="alice", decision="approve"),
    )
    assert submitted_feedback.review_id == claimed.review_id

    try:
        ReviewAdjudicationRequest(status="unexpected")
    except ValidationError:
        pass
    else:
        raise AssertionError("expected unknown adjudication status to be rejected")

    adjudicated = store.adjudicate_review_request(
        claimed.review_id,
        ReviewAdjudicationRequest(status="adjudicated"),
    )
    assert adjudicated.status == "adjudicated"
    resolved = store.resolve_review_request(claimed.review_id)
    assert resolved.status == "resolved"

    try:
        store.mark_review_stale(claimed.review_id)
    except ValueError as exc:
        assert "cannot mark review" in str(exc)
    else:
        raise AssertionError("expected resolved review to reject stale transition")


def test_hitl_status_enums_validate_response_models():
    for status in ReviewStatus:
        response = ReviewRequestResponse(
            review_id=f"rev_{status.value}",
            review_type="promotion",
            status=status.value,
            packet_id="rpacket_1",
            packet_hash="sha256:packet",
            created_at="2026-06-27T00:00:00Z",
            updated_at="2026-06-27T00:00:00Z",
        )
        assert response.status == status

    for status in HumanFeedbackStatus:
        response = HumanFeedbackResponse(
            feedback_id=f"hfb_{status.value}",
            review_id="rev_1",
            reviewer_id="alice",
            status=status.value,
            decision="approve",
            created_at="2026-06-27T00:00:00Z",
        )
        assert response.status == status

    indexed_response = HumanFeedbackResponse(
        feedback_id="hfb_indexed",
        review_id="rev_1",
        reviewer_id="alice",
        status="indexed",
        decision="approve",
        created_at="2026-06-27T00:00:00Z",
    )
    assert indexed_response.status == HumanFeedbackStatus.INDEXED


def test_hitl_response_models_reject_invalid_enum_values():
    invalid_responses = (
        lambda: ReviewRequestResponse(
            review_id="rev_1",
            review_type="not_a_review_type",
            status=ReviewStatus.QUEUED,
            packet_id="rpacket_1",
            packet_hash="sha256:packet",
            created_at="2026-06-27T00:00:00Z",
            updated_at="2026-06-27T00:00:00Z",
        ),
        lambda: HumanFeedbackResponse(
            feedback_id="hfb_1",
            review_id="rev_1",
            reviewer_id="alice",
            status=HumanFeedbackStatus.AVAILABLE_FOR_EVOLUTION,
            decision="rubber_stamp",
            created_at="2026-06-27T00:00:00Z",
        ),
        lambda: FeedbackApplicationResponse(
            application_id="hfa_1",
            feedback_id="hfb_1",
            target_type="side_channel",
            target_id="target_1",
            consumed_by_method="reflector",
            effect_summary="Used feedback.",
            created_at="2026-06-27T00:00:00Z",
        ),
        lambda: HumanQueryDecisionResponse(
            query_decision_id="hqd_1",
            decision="maybe_human",
            created_at="2026-06-27T00:00:00Z",
        ),
    )

    for build_response in invalid_responses:
        try:
            build_response()
        except ValidationError:
            pass
        else:
            raise AssertionError("expected invalid response enum value to be rejected")


def test_hitl_response_models_accept_valid_enum_values():
    review = ReviewRequestResponse(
        review_id="rev_1",
        review_type="promotion",
        status=ReviewStatus.QUEUED,
        packet_id="rpacket_1",
        packet_hash="sha256:packet",
        created_at="2026-06-27T00:00:00Z",
        updated_at="2026-06-27T00:00:00Z",
    )
    feedback = HumanFeedbackResponse(
        feedback_id="hfb_1",
        review_id="rev_1",
        reviewer_id="alice",
        status=HumanFeedbackStatus.AVAILABLE_FOR_EVOLUTION,
        decision="approve",
        created_at="2026-06-27T00:00:00Z",
    )
    application = FeedbackApplicationResponse(
        application_id="hfa_1",
        feedback_id="hfb_1",
        target_type="prompt_seed",
        target_id="target_1",
        consumed_by_method="reflector",
        effect_summary="Used feedback.",
        created_at="2026-06-27T00:00:00Z",
    )
    query_decision = HumanQueryDecisionResponse(
        query_decision_id="hqd_1",
        decision="ask_human",
        created_at="2026-06-27T00:00:00Z",
    )

    assert review.review_type == ReviewType.PROMOTION
    assert feedback.decision == HumanFeedbackDecision.APPROVE
    assert application.target_type == FeedbackApplicationTargetType.PROMPT_SEED
    assert query_decision.decision == HumanQueryDecision.ASK_HUMAN


def test_review_adjudication_request_accepts_spec_statuses_and_rejects_invalid():
    for status in (
        "validated",
        "adjudicated",
        "needs_revision",
        "rejected_invalid",
        "conflict",
        "archived_only",
    ):
        assert ReviewAdjudicationRequest(status=status).status == ReviewStatus(status)

    try:
        ReviewAdjudicationRequest(status="unexpected")
    except ValidationError:
        pass
    else:
        raise AssertionError("expected invalid adjudication status to be rejected")


def test_review_adjudication_accepts_documented_status_transitions(tmp_path):
    store = EvolutionStore(db_path=tmp_path / "evolution.db", artifact_root=tmp_path / "artifacts")
    store.initialize()

    needs_revision = _create_submitted_review(store, artifact_id="art_needs_revision")
    rejected_invalid = _create_submitted_review(store, artifact_id="art_rejected_invalid")
    validated_to_conflict = _create_submitted_review(store, artifact_id="art_conflict")
    validated_to_adjudicated = _create_submitted_review(store, artifact_id="art_validated")
    archived_only = _create_submitted_review(store, artifact_id="art_archived")

    assert (
        store.adjudicate_review_request(
            needs_revision.review_id,
            ReviewAdjudicationRequest(status="needs_revision"),
        ).status
        == ReviewStatus.NEEDS_REVISION
    )
    assert (
        store.adjudicate_review_request(
            rejected_invalid.review_id,
            ReviewAdjudicationRequest(status="rejected_invalid"),
        ).status
        == ReviewStatus.REJECTED_INVALID
    )
    assert (
        store.adjudicate_review_request(
            validated_to_conflict.review_id,
            ReviewAdjudicationRequest(status="validated"),
        ).status
        == ReviewStatus.VALIDATED
    )
    assert (
        store.adjudicate_review_request(
            validated_to_conflict.review_id,
            ReviewAdjudicationRequest(status="conflict"),
        ).status
        == ReviewStatus.CONFLICT
    )
    assert (
        store.adjudicate_review_request(
            validated_to_adjudicated.review_id,
            ReviewAdjudicationRequest(status="validated"),
        ).status
        == ReviewStatus.VALIDATED
    )
    assert (
        store.adjudicate_review_request(
            validated_to_adjudicated.review_id,
            ReviewAdjudicationRequest(status="adjudicated"),
        ).status
        == ReviewStatus.ADJUDICATED
    )
    assert (
        store.adjudicate_review_request(
            archived_only.review_id,
            ReviewAdjudicationRequest(status="adjudicated"),
        ).status
        == ReviewStatus.ADJUDICATED
    )
    assert (
        store.adjudicate_review_request(
            archived_only.review_id,
            ReviewAdjudicationRequest(status="archived_only"),
        ).status
        == ReviewStatus.ARCHIVED_ONLY
    )


def test_resolve_rejects_queued_and_in_review_but_allows_reviewed_terminal_sources(tmp_path):
    store = EvolutionStore(db_path=tmp_path / "evolution.db", artifact_root=tmp_path / "artifacts")
    store.initialize()
    queued = store.create_review_request(
        ReviewRequestCreateRequest(
            review_type="promotion",
            artifact_ids=["art_queued"],
            packet={"questions": ["Approve?"]},
        )
    )
    in_review = store.create_review_request(
        ReviewRequestCreateRequest(
            review_type="promotion",
            artifact_ids=["art_in_review"],
            packet={"questions": ["Approve?"]},
        )
    )
    store.claim_review_request(in_review.review_id, ReviewClaimRequest(reviewer_id="alice"))

    for review_id in (queued.review_id, in_review.review_id):
        try:
            store.resolve_review_request(review_id)
        except ValueError:
            pass
        else:
            raise AssertionError("expected unresolved early review to reject resolve")

    for status in ("validated", "needs_revision", "rejected_invalid"):
        review = _create_submitted_review(store, artifact_id=f"art_{status}")
        store.adjudicate_review_request(
            review.review_id,
            ReviewAdjudicationRequest(status=status),
        )
        assert store.resolve_review_request(review.review_id).status == ReviewStatus.RESOLVED
    conflict = _create_submitted_review(store, artifact_id="art_resolve_conflict")
    store.adjudicate_review_request(
        conflict.review_id,
        ReviewAdjudicationRequest(status="validated"),
    )
    store.adjudicate_review_request(
        conflict.review_id,
        ReviewAdjudicationRequest(status="conflict"),
    )
    assert store.resolve_review_request(conflict.review_id).status == ReviewStatus.RESOLVED


def test_mark_review_stale_updates_non_terminal_reviews(tmp_path):
    store = EvolutionStore(db_path=tmp_path / "evolution.db", artifact_root=tmp_path / "artifacts")
    store.initialize()
    review = store.create_review_request(
        ReviewRequestCreateRequest(
            review_type="promotion",
            artifact_ids=["art_a"],
            packet={"questions": ["Approve?"]},
        )
    )

    stale = store.mark_review_stale(review.review_id)

    assert stale.status == "stale"
    assert stale.updated_at >= review.updated_at


def test_mark_review_stale_accepts_created_status(tmp_path):
    store = EvolutionStore(db_path=tmp_path / "evolution.db", artifact_root=tmp_path / "artifacts")
    store.initialize()
    review = store.create_review_request(
        ReviewRequestCreateRequest(
            review_type="promotion",
            artifact_ids=["art_a"],
            packet={"questions": ["Approve?"]},
        )
    )
    with store.connect() as conn:
        conn.execute(
            "UPDATE review_requests SET status = ? WHERE review_id = ?",
            (ReviewStatus.CREATED.value, review.review_id),
        )
        conn.commit()

    stale = store.mark_review_stale(review.review_id)

    assert stale.status == ReviewStatus.STALE


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
    store.claim_review_request(review.review_id, ReviewClaimRequest(reviewer_id="alice"))
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
    assert store.list_human_feedback(review_id=review.review_id)[0].status == "consumed"
    assert store.list_feedback_applications(feedback_id=feedback.feedback_id)[0].target_id == "job_next"
    store.mark_review_stale(review.review_id)
    assert store.list_human_feedback(review_id=review.review_id)[0].status == "archived_only"


def test_feedback_application_exact_retry_is_idempotent(tmp_path):
    store = EvolutionStore(db_path=tmp_path / "evolution.db", artifact_root=tmp_path / "artifacts")
    store.initialize()
    review = _create_submitted_review(store)
    feedback = store.list_human_feedback(review_id=review.review_id)[0]
    request = FeedbackApplicationCreateRequest(
        feedback_id=feedback.feedback_id,
        target_type="prompt_seed",
        target_id="job_next",
        consumed_by_method="agent_system_gepa_reflector",
        effect_summary="Added bounded inventory constraint.",
    )

    first = store.create_feedback_application(request)
    retry = store.create_feedback_application(request)
    applications = store.list_feedback_applications(feedback_id=feedback.feedback_id)

    assert retry.application_id == first.application_id
    assert [application.application_id for application in applications] == [
        first.application_id
    ]


def test_feedback_application_sanitizes_public_metadata_and_effect_summary(tmp_path):
    store = EvolutionStore(db_path=tmp_path / "evolution.db", artifact_root=tmp_path / "artifacts")
    store.initialize()
    review = _create_submitted_review(store)
    feedback = store.list_human_feedback(review_id=review.review_id)[0]
    request = FeedbackApplicationCreateRequest(
        feedback_id=feedback.feedback_id,
        target_type="prompt_seed",
        target_id="/home/alice/private/job_next",
        consumed_by_method="agent_system_gepa_reflector",
        consumed_in_job_id="memory.md?signature=relative-secret#frag",
        effect_summary=(
            "Used /home/alice/private/memory.md and "
            "https://user:pass@example.test/report?token=secret-token#frag "
            "with Authorization: Bearer abc123 bearer:effect-bearer "
            "basic:effect-basic"
        ),
    )

    first = store.create_feedback_application(request)
    retry = store.create_feedback_application(request)
    listed = store.list_feedback_applications(feedback_id=feedback.feedback_id)[0]
    serialized = json.dumps(
        {
            "first": first.model_dump(mode="python"),
            "retry": retry.model_dump(mode="python"),
            "listed": listed.model_dump(mode="python"),
        },
        sort_keys=True,
    )

    assert retry.application_id == first.application_id
    for raw_secret in (
        "/home/alice/private/job_next",
        "/home/alice/private/memory.md",
        "user:pass@example.test",
        "secret-token",
        "relative-secret",
        "Authorization: Bearer abc123",
        "effect-bearer",
        "effect-basic",
        "signature=",
        "#frag",
    ):
        assert raw_secret not in serialized
    assert first.target_id == "[LOCAL_ARTIFACT_PATH]"
    assert first.consumed_in_job_id == "memory.md?<redacted>"
    assert "[LOCAL_ARTIFACT_PATH]" in first.effect_summary
    assert "https://example.test/report?<redacted>" in first.effect_summary
    assert "[REDACTED]" in first.effect_summary

    benign = store.create_feedback_application(
        FeedbackApplicationCreateRequest(
            feedback_id=feedback.feedback_id,
            target_type="mutation_constraint",
            target_id="job-tokenizer-next",
            consumed_by_method="token_memory_reflector",
            effect_summary="Kept tokenizer-specific memory constraint.",
        )
    )
    sensitive = store.create_feedback_application(
        FeedbackApplicationCreateRequest(
            feedback_id=feedback.feedback_id,
            target_type="audit_note",
            target_id="token:raw-target-token",
            consumed_by_method="secret://method/raw-method-secret?x=y",
            effect_summary="Rejected secret://method/raw-effect-secret?x=y.",
        )
    )

    serialized_extra = json.dumps(
        {
            "benign": benign.model_dump(mode="python"),
            "sensitive": sensitive.model_dump(mode="python"),
        },
        sort_keys=True,
    )
    assert benign.target_id == "job-tokenizer-next"
    assert benign.consumed_by_method == "token_memory_reflector"
    for raw_secret in ("raw-target-token", "raw-method-secret", "raw-effect-secret"):
        assert raw_secret not in serialized_extra


def test_feedback_application_rejects_same_target_with_different_effect_summary(tmp_path):
    store = EvolutionStore(db_path=tmp_path / "evolution.db", artifact_root=tmp_path / "artifacts")
    store.initialize()
    review = _create_submitted_review(store)
    feedback = store.list_human_feedback(review_id=review.review_id)[0]
    store.create_feedback_application(
        FeedbackApplicationCreateRequest(
            feedback_id=feedback.feedback_id,
            target_type="prompt_seed",
            target_id="job_next",
            consumed_by_method="agent_system_gepa_reflector",
            effect_summary="Added bounded inventory constraint.",
        )
    )

    try:
        store.create_feedback_application(
            FeedbackApplicationCreateRequest(
                feedback_id=feedback.feedback_id,
                target_type="prompt_seed",
                target_id="job_next",
                consumed_by_method="agent_system_gepa_reflector",
                effect_summary="Added a different constraint.",
            )
        )
    except ValueError as exc:
        assert "feedback application already exists with a different effect summary" in str(exc)
    else:
        raise AssertionError("expected changed effect summary retry to be rejected")

    applications = store.list_feedback_applications(feedback_id=feedback.feedback_id)
    assert len(applications) == 1
    assert applications[0].effect_summary == "Added bounded inventory constraint."


def test_feedback_application_allows_distinct_natural_targets(tmp_path):
    store = EvolutionStore(db_path=tmp_path / "evolution.db", artifact_root=tmp_path / "artifacts")
    store.initialize()
    review = _create_submitted_review(store)
    feedback = store.list_human_feedback(review_id=review.review_id)[0]
    first = store.create_feedback_application(
        FeedbackApplicationCreateRequest(
            feedback_id=feedback.feedback_id,
            target_type="prompt_seed",
            target_id="job_next",
            consumed_by_method="agent_system_gepa_reflector",
            consumed_in_job_id="job_next",
            effect_summary="Added bounded inventory constraint.",
        )
    )
    different_target = store.create_feedback_application(
        FeedbackApplicationCreateRequest(
            feedback_id=feedback.feedback_id,
            target_type="prompt_seed",
            target_id="job_other",
            consumed_by_method="agent_system_gepa_reflector",
            consumed_in_job_id="job_next",
            effect_summary="Applied same feedback to another target.",
        )
    )
    different_job = store.create_feedback_application(
        FeedbackApplicationCreateRequest(
            feedback_id=feedback.feedback_id,
            target_type="prompt_seed",
            target_id="job_next",
            consumed_by_method="agent_system_gepa_reflector",
            consumed_in_job_id="job_other",
            effect_summary="Applied same feedback in another job.",
        )
    )

    applications = store.list_feedback_applications(feedback_id=feedback.feedback_id)

    assert len(applications) == 3
    assert {
        application.application_id for application in applications
    } == {
        first.application_id,
        different_target.application_id,
        different_job.application_id,
    }


def test_stale_review_archives_feedback_and_blocks_application(tmp_path):
    store = EvolutionStore(db_path=tmp_path / "evolution.db", artifact_root=tmp_path / "artifacts")
    store.initialize()
    review = store.create_review_request(
        ReviewRequestCreateRequest(
            review_type="promotion",
            artifact_ids=["art_a"],
            packet={"questions": ["Approve?"]},
        )
    )
    store.claim_review_request(review.review_id, ReviewClaimRequest(reviewer_id="alice"))
    feedback = store.submit_human_feedback(
        review.review_id,
        HumanFeedbackCreateRequest(reviewer_id="alice", decision="approve"),
    )

    stale = store.mark_review_stale(review.review_id)

    assert stale.status == "stale"
    archived_feedback = store.list_human_feedback(review_id=review.review_id)[0]
    assert archived_feedback.feedback_id == feedback.feedback_id
    assert archived_feedback.status == "archived_only"
    try:
        store.create_feedback_application(
            FeedbackApplicationCreateRequest(
                feedback_id=feedback.feedback_id,
                target_type="prompt_seed",
                target_id="job_next",
                consumed_by_method="agent_system_gepa_reflector",
                effect_summary="Used feedback.",
            )
        )
    except ValueError as exc:
        assert str(exc) == f"feedback is not available for evolution: {feedback.feedback_id}"
    else:
        raise AssertionError("expected stale feedback application to be rejected")


def test_adjudication_invalid_and_archived_statuses_update_available_feedback(tmp_path):
    store = EvolutionStore(db_path=tmp_path / "evolution.db", artifact_root=tmp_path / "artifacts")
    store.initialize()
    rejected_review = _create_submitted_review(store, artifact_id="art_rejected")
    archived_review = _create_submitted_review(store, artifact_id="art_archived")

    store.adjudicate_review_request(
        rejected_review.review_id,
        ReviewAdjudicationRequest(status="rejected_invalid"),
    )
    archived_feedback_before = store.list_human_feedback(review_id=archived_review.review_id)[0]
    store.create_feedback_application(
        FeedbackApplicationCreateRequest(
            feedback_id=archived_feedback_before.feedback_id,
            target_type="prompt_seed",
            target_id="job_next",
            consumed_by_method="agent_system_gepa_reflector",
            effect_summary="Used feedback before archival.",
        )
    )
    store.adjudicate_review_request(
        archived_review.review_id,
        ReviewAdjudicationRequest(status="adjudicated"),
    )
    store.adjudicate_review_request(
        archived_review.review_id,
        ReviewAdjudicationRequest(status="archived_only"),
    )

    rejected_feedback = store.list_human_feedback(review_id=rejected_review.review_id)[0]
    archived_feedback = store.list_human_feedback(review_id=archived_review.review_id)[0]
    assert rejected_feedback.status == HumanFeedbackStatus.REJECTED_INVALID
    assert rejected_feedback.status != HumanFeedbackStatus.AVAILABLE_FOR_EVOLUTION
    assert archived_feedback.status == HumanFeedbackStatus.CONSUMED

    available_archived_review = _create_submitted_review(
        store,
        artifact_id="art_available_archived",
    )
    store.adjudicate_review_request(
        available_archived_review.review_id,
        ReviewAdjudicationRequest(status="adjudicated"),
    )
    store.adjudicate_review_request(
        available_archived_review.review_id,
        ReviewAdjudicationRequest(status="archived_only"),
    )
    available_archived_feedback = store.list_human_feedback(
        review_id=available_archived_review.review_id
    )[0]
    assert available_archived_feedback.status == HumanFeedbackStatus.ARCHIVED_ONLY
    assert available_archived_feedback.status != HumanFeedbackStatus.AVAILABLE_FOR_EVOLUTION


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
        feedback_list = client.get(f"/v1/reviews/{review_id}/feedback")
        resolved = client.post(f"/v1/reviews/{review_id}/resolve")

    assert created.status_code == 200
    assert claimed.status_code == 200
    assert claimed.json()["status"] == "in_review"
    assert feedback.status_code == 200
    assert feedback.json()["status"] == "available_for_evolution"
    assert "raw_payload" not in feedback.json()
    assert feedback_list.status_code == 200
    assert "raw_payload" not in feedback_list.json()[0]
    assert resolved.status_code == 200
    assert resolved.json()["status"] == "resolved"


def test_review_packet_routes_get_list_and_404(tmp_path):
    app = create_app(db_path=tmp_path / "evolution.db", artifact_root=tmp_path / "artifacts")

    with TestClient(app) as client:
        created = client.post(
            "/v1/reviews",
            json={
                "review_type": "promotion",
                "artifact_ids": ["art_a"],
                "packet": {
                    "trusted_metadata": {"task_id": "task-a"},
                    "questions": ["Approve?"],
                    "extra_packet_context": {"rank": 1},
                },
            },
        )
        packet_id = created.json()["packet_id"]
        fetched = client.get(f"/v1/review-packets/{packet_id}")
        listed = client.get("/v1/review-packets")
        missing = client.get("/v1/review-packets/rpacket_missing")

    assert created.status_code == 200
    assert created.json()["packet"]["trusted_metadata"] == {"task_id": "task-a"}
    assert created.json()["packet"]["untrusted_artifact_excerpts"] == []
    assert created.json()["packet"]["promotion_support"] == {}
    assert created.json()["packet"]["extra_packet_context"] == {"rank": 1}
    assert fetched.status_code == 200
    assert fetched.json()["packet_id"] == packet_id
    assert fetched.json()["packet"] == created.json()["packet"]
    assert listed.status_code == 200
    assert [packet["packet_id"] for packet in listed.json()] == [packet_id]
    assert missing.status_code == 404


def test_review_packet_routes_return_sanitized_packets(tmp_path):
    app = create_app(db_path=tmp_path / "evolution.db", artifact_root=tmp_path / "artifacts")

    with TestClient(app) as client:
        created = client.post(
            "/v1/reviews",
            json={
                "review_type": "promotion",
                "artifact_ids": ["art_a"],
                "packet": {
                    "artifact": {
                        "artifact_id": "art_a",
                        "uri": "file:///home/alice/private/memory.md",
                        "manifest": {"content_uri": "file:///tmp/artifacts/memory.md"},
                    },
                    "artifact_content": {
                        "source_uri": "file:///tmp/artifacts/memory.md",
                        "excerpts": [
                            {
                                "path": "/home/alice/private/memory.md",
                                "text": (
                                    "See https://user:pass@example.test/path"
                                    "?token=secret-token#frag and Authorization: Bearer abc123"
                                ),
                            }
                        ],
                    },
                    "custom_extra": {"password": "correct-horse"},
                },
            },
        )
        packet_id = created.json()["packet_id"]
        fetched = client.get(f"/v1/review-packets/{packet_id}")
        listed = client.get("/v1/review-packets")

    assert created.status_code == 200
    assert fetched.status_code == 200
    assert listed.status_code == 200
    for payload in (created.json(), fetched.json(), listed.json()[0]):
        serialized = json.dumps(payload["packet"], sort_keys=True)
        for raw_secret in (
            "file:///home/alice/private/memory.md",
            "file:///tmp/artifacts/memory.md",
            "/home/alice/private/memory.md",
            "user:pass@example.test",
            "secret-token",
            "abc123",
            "correct-horse",
        ):
            assert raw_secret not in serialized
        assert "[LOCAL_ARTIFACT_URI]" in serialized
        assert "[LOCAL_ARTIFACT_PATH]" in serialized
        assert "[REDACTED]" in serialized
        assert "https://example.test/path?<redacted>" in serialized


def test_review_feedback_routes_return_sanitized_normalized_payload(tmp_path):
    app = create_app(db_path=tmp_path / "evolution.db", artifact_root=tmp_path / "artifacts")

    with TestClient(app) as client:
        created = client.post(
            "/v1/reviews",
            json={
                "review_type": "promotion",
                "artifact_ids": ["art_a"],
                "packet": {"questions": ["Approve?"]},
            },
        )
        review_id = created.json()["review_id"]
        client.post(f"/v1/reviews/{review_id}/claim", json={"reviewer_id": "alice"})
        feedback = client.post(
            f"/v1/reviews/{review_id}/feedback",
            json={
                "reviewer_id": "alice",
                "decision": "approve",
                "rationale": "Authorization: Bearer abc123 in file:///tmp/private.md",
                "observed_issues": [
                    "AWS_SECRET_ACCESS_KEY=wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY"
                ],
                "suggested_changes": [
                    "Review https://user:pass@example.test/path?token=secret-token#frag"
                ],
                "labels": ["/home/alice/private.md"],
            },
        )
        feedback_list = client.get(f"/v1/reviews/{review_id}/feedback")

    assert feedback.status_code == 200
    assert feedback_list.status_code == 200
    for payload in (feedback.json(), feedback_list.json()[0]):
        serialized = json.dumps(payload["normalized_payload"], sort_keys=True)
        for raw_secret in (
            "abc123",
            "file:///tmp/private.md",
            "wJalrXUtnFEMI",
            "user:pass@example.test",
            "secret-token",
            "/home/alice/private.md",
        ):
            assert raw_secret not in serialized
        assert "[LOCAL_ARTIFACT_URI]" in serialized
        assert "[LOCAL_ARTIFACT_PATH]" in serialized
        assert "[REDACTED]" in serialized
        assert "https://example.test/path?<redacted>" in serialized


def test_review_adjudication_rationale_persists_and_is_returned(tmp_path):
    app = create_app(db_path=tmp_path / "evolution.db", artifact_root=tmp_path / "artifacts")

    with TestClient(app) as client:
        created = client.post(
            "/v1/reviews",
            json={
                "review_type": "promotion",
                "artifact_ids": ["art_a"],
                "packet": {"questions": ["Approve?"]},
            },
        )
        review_id = created.json()["review_id"]
        client.post(f"/v1/reviews/{review_id}/claim", json={"reviewer_id": "alice"})
        client.post(
            f"/v1/reviews/{review_id}/feedback",
            json={"reviewer_id": "alice", "decision": "approve"},
        )
        adjudicated = client.post(
            f"/v1/reviews/{review_id}/adjudicate",
            json={
                "status": "adjudicated",
                "rationale": (
                    "Approved after checking https://user:pass@example.test/path"
                    "?token=adjudication-secret#frag and /home/alice/private.md "
                    "with Authorization: Bearer adjudication-bearer."
                ),
            },
        )
        fetched = client.get(f"/v1/reviews/{review_id}")
        listed = client.get("/v1/reviews")

    assert adjudicated.status_code == 200
    assert adjudicated.json()["status"] == "adjudicated"
    sanitized_rationale = adjudicated.json()["adjudication_rationale"]
    for raw_secret in (
        "user:pass@example.test",
        "adjudication-secret",
        "/home/alice/private.md",
        "adjudication-bearer",
        "#frag",
    ):
        assert raw_secret not in sanitized_rationale
        assert raw_secret not in fetched.text
        assert raw_secret not in listed.text
    assert "https://example.test/path?<redacted>" in sanitized_rationale
    assert "[LOCAL_ARTIFACT_PATH]" in sanitized_rationale
    assert "[REDACTED]" in sanitized_rationale
    assert fetched.status_code == 200
    assert fetched.json()["adjudication_rationale"] == sanitized_rationale
    assert listed.status_code == 200
    assert listed.json()[0]["adjudication_rationale"] == sanitized_rationale

    with app.state.store.connect() as conn:
        row = conn.execute(
            "SELECT adjudication_rationale FROM review_requests WHERE review_id = ?",
            (review_id,),
        ).fetchone()

    assert row is not None
    assert row["adjudication_rationale"] == sanitized_rationale


def test_review_routes_accept_documented_adjudication_status_transitions(tmp_path):
    app = create_app(db_path=tmp_path / "evolution.db", artifact_root=tmp_path / "artifacts")

    def create_submitted_review(client: TestClient, artifact_id: str) -> str:
        created = client.post(
            "/v1/reviews",
            json={
                "review_type": "promotion",
                "artifact_ids": [artifact_id],
                "packet": {"questions": ["Approve?"]},
            },
        )
        review_id = created.json()["review_id"]
        client.post(f"/v1/reviews/{review_id}/claim", json={"reviewer_id": "alice"})
        client.post(
            f"/v1/reviews/{review_id}/feedback",
            json={"reviewer_id": "alice", "decision": "approve"},
        )
        return review_id

    with TestClient(app) as client:
        needs_revision_id = create_submitted_review(client, "art_needs_revision")
        rejected_invalid_id = create_submitted_review(client, "art_rejected_invalid")
        conflict_id = create_submitted_review(client, "art_conflict")
        adjudicated_id = create_submitted_review(client, "art_adjudicated")
        archived_id = create_submitted_review(client, "art_archived")

        needs_revision = client.post(
            f"/v1/reviews/{needs_revision_id}/adjudicate",
            json={"status": "needs_revision"},
        )
        rejected_invalid = client.post(
            f"/v1/reviews/{rejected_invalid_id}/adjudicate",
            json={"status": "rejected_invalid"},
        )
        conflict_validated = client.post(
            f"/v1/reviews/{conflict_id}/adjudicate",
            json={"status": "validated"},
        )
        conflict = client.post(
            f"/v1/reviews/{conflict_id}/adjudicate",
            json={"status": "conflict"},
        )
        adjudicated_validated = client.post(
            f"/v1/reviews/{adjudicated_id}/adjudicate",
            json={"status": "validated"},
        )
        adjudicated = client.post(
            f"/v1/reviews/{adjudicated_id}/adjudicate",
            json={"status": "adjudicated"},
        )
        archived_adjudicated = client.post(
            f"/v1/reviews/{archived_id}/adjudicate",
            json={"status": "adjudicated"},
        )
        archived = client.post(
            f"/v1/reviews/{archived_id}/adjudicate",
            json={"status": "archived_only"},
        )

    assert needs_revision.status_code == 200
    assert needs_revision.json()["status"] == "needs_revision"
    assert rejected_invalid.status_code == 200
    assert rejected_invalid.json()["status"] == "rejected_invalid"
    assert conflict_validated.status_code == 200
    assert conflict_validated.json()["status"] == "validated"
    assert conflict.status_code == 200
    assert conflict.json()["status"] == "conflict"
    assert adjudicated_validated.status_code == 200
    assert adjudicated_validated.json()["status"] == "validated"
    assert adjudicated.status_code == 200
    assert adjudicated.json()["status"] == "adjudicated"
    assert archived_adjudicated.status_code == 200
    assert archived_adjudicated.json()["status"] == "adjudicated"
    assert archived.status_code == 200
    assert archived.json()["status"] == "archived_only"


def test_review_schema_upgrade_adds_adjudication_rationale_to_existing_db(tmp_path):
    db_path = tmp_path / "evolution.db"
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """
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
        )

    store = EvolutionStore(db_path=db_path, artifact_root=tmp_path / "artifacts")
    store.initialize()

    with sqlite3.connect(db_path) as conn:
        columns = {
            row[1]
            for row in conn.execute("PRAGMA table_info(review_requests)").fetchall()
        }

    assert "adjudication_rationale" in columns


def test_review_write_routes_return_404_for_unknown_review_ids(tmp_path):
    app = create_app(db_path=tmp_path / "evolution.db", artifact_root=tmp_path / "artifacts")

    with TestClient(app) as client:
        claim = client.post(
            "/v1/reviews/rev_missing/claim",
            json={"reviewer_id": "alice", "reviewer_role": "maintainer"},
        )
        feedback = client.post(
            "/v1/reviews/rev_missing/feedback",
            json={"reviewer_id": "alice", "decision": "approve", "raw_payload": {}},
        )
        adjudicate = client.post(
            "/v1/reviews/rev_missing/adjudicate",
            json={"status": "adjudicated"},
        )
        resolve = client.post("/v1/reviews/rev_missing/resolve")
        stale = client.post("/v1/reviews/rev_missing/mark-stale")

    assert claim.status_code == 404
    assert feedback.status_code == 404
    assert adjudicate.status_code == 404
    assert resolve.status_code == 404
    assert stale.status_code == 404


def test_query_decision_route_returns_404_for_unknown_id(tmp_path):
    app = create_app(db_path=tmp_path / "evolution.db", artifact_root=tmp_path / "artifacts")

    with TestClient(app) as client:
        response = client.get("/v1/query-decisions/hqd_missing")

    assert response.status_code == 404


def test_review_write_routes_keep_422_for_invalid_transitions(tmp_path):
    app = create_app(db_path=tmp_path / "evolution.db", artifact_root=tmp_path / "artifacts")

    with TestClient(app) as client:
        created = client.post(
            "/v1/reviews",
            json={
                "review_type": "promotion",
                "artifact_ids": ["art_a"],
                "packet": {"questions": ["Approve?"]},
            },
        )
        review_id = created.json()["review_id"]
        resolve_queued = client.post(f"/v1/reviews/{review_id}/resolve")
        adjudicate_unknown = client.post(
            f"/v1/reviews/{review_id}/adjudicate",
            json={"status": "unexpected"},
        )
        stale = client.post(f"/v1/reviews/{review_id}/mark-stale")
        invalid_claim = client.post(
            f"/v1/reviews/{review_id}/claim",
            json={"reviewer_id": "alice", "reviewer_role": "maintainer"},
        )

    assert created.status_code == 200
    assert resolve_queued.status_code == 422
    assert adjudicate_unknown.status_code == 422
    assert stale.status_code == 200
    assert stale.json()["status"] == "stale"
    assert invalid_claim.status_code == 422


def test_review_list_rejects_invalid_status_filter(tmp_path):
    app = create_app(db_path=tmp_path / "evolution.db", artifact_root=tmp_path / "artifacts")

    with TestClient(app) as client:
        created = client.post(
            "/v1/reviews",
            json={
                "review_type": "promotion",
                "artifact_ids": ["art_a"],
                "packet": {"questions": ["Approve?"]},
            },
        )
        valid_filter = client.get("/v1/reviews", params={"status": "queued"})
        invalid_filter = client.get("/v1/reviews", params={"status": "not_a_status"})

    assert created.status_code == 200
    assert valid_filter.status_code == 200
    assert [review["review_id"] for review in valid_filter.json()] == [
        created.json()["review_id"]
    ]
    assert invalid_filter.status_code == 422


def test_review_routes_reject_unknown_query_decision_link(tmp_path):
    app = create_app(db_path=tmp_path / "evolution.db", artifact_root=tmp_path / "artifacts")

    with TestClient(app) as client:
        response = client.post(
            "/v1/reviews",
            json={
                "review_type": "promotion",
                "artifact_ids": ["art_a"],
                "packet": {"questions": ["Approve?"]},
                "query_decision_id": "hqd_missing",
            },
        )

    assert response.status_code == 404


def test_review_routes_reject_claim_overwrite_by_different_reviewer(tmp_path):
    app = create_app(db_path=tmp_path / "evolution.db", artifact_root=tmp_path / "artifacts")

    with TestClient(app) as client:
        created = client.post(
            "/v1/reviews",
            json={
                "review_type": "promotion",
                "artifact_ids": ["art_a"],
                "packet": {"questions": ["Approve?"]},
            },
        )
        review_id = created.json()["review_id"]
        first_claim = client.post(
            f"/v1/reviews/{review_id}/claim",
            json={"reviewer_id": "alice", "reviewer_role": "maintainer"},
        )
        retry_claim = client.post(
            f"/v1/reviews/{review_id}/claim",
            json={"reviewer_id": "alice"},
        )
        overwrite_claim = client.post(
            f"/v1/reviews/{review_id}/claim",
            json={"reviewer_id": "bob", "reviewer_role": "maintainer"},
        )

    assert first_claim.status_code == 200
    assert retry_claim.status_code == 200
    assert retry_claim.json()["assigned_to"] == "alice"
    assert retry_claim.json()["reviewer_role"] == "maintainer"
    assert overwrite_claim.status_code == 422


def test_review_routes_reject_feedback_by_non_owner_reviewer(tmp_path):
    app = create_app(db_path=tmp_path / "evolution.db", artifact_root=tmp_path / "artifacts")

    with TestClient(app) as client:
        bob_review = client.post(
            "/v1/reviews",
            json={
                "review_type": "promotion",
                "artifact_ids": ["art_a"],
                "packet": {"questions": ["Approve?"]},
            },
        )
        bob_review_id = bob_review.json()["review_id"]
        alice_review = client.post(
            "/v1/reviews",
            json={
                "review_type": "promotion",
                "artifact_ids": ["art_b"],
                "packet": {"questions": ["Approve?"]},
            },
        )
        alice_review_id = alice_review.json()["review_id"]
        client.post(
            f"/v1/reviews/{bob_review_id}/claim",
            json={"reviewer_id": "alice", "reviewer_role": "maintainer"},
        )
        client.post(
            f"/v1/reviews/{alice_review_id}/claim",
            json={"reviewer_id": "alice", "reviewer_role": "maintainer"},
        )
        bob_feedback = client.post(
            f"/v1/reviews/{bob_review_id}/feedback",
            json={"reviewer_id": "bob", "decision": "approve"},
        )
        bob_review_after = client.get(f"/v1/reviews/{bob_review_id}")
        alice_feedback = client.post(
            f"/v1/reviews/{alice_review_id}/feedback",
            json={"reviewer_id": "alice", "decision": "approve"},
        )

    assert bob_feedback.status_code == 422
    assert bob_review_after.status_code == 200
    assert bob_review_after.json()["status"] == "in_review"
    assert alice_feedback.status_code == 200
    assert alice_feedback.json()["review_id"] == alice_review_id


def test_review_routes_enforce_and_inherit_claimed_reviewer_role(tmp_path):
    app = create_app(db_path=tmp_path / "evolution.db", artifact_root=tmp_path / "artifacts")

    with TestClient(app) as client:
        mismatch_review = client.post(
            "/v1/reviews",
            json={
                "review_type": "promotion",
                "artifact_ids": ["art_a"],
                "packet": {"questions": ["Approve?"]},
            },
        )
        mismatch_review_id = mismatch_review.json()["review_id"]
        inherited_role_review = client.post(
            "/v1/reviews",
            json={
                "review_type": "promotion",
                "artifact_ids": ["art_b"],
                "packet": {"questions": ["Approve?"]},
            },
        )
        inherited_role_review_id = inherited_role_review.json()["review_id"]
        client.post(
            f"/v1/reviews/{mismatch_review_id}/claim",
            json={"reviewer_id": "alice", "reviewer_role": "maintainer"},
        )
        client.post(
            f"/v1/reviews/{inherited_role_review_id}/claim",
            json={"reviewer_id": "alice", "reviewer_role": "maintainer"},
        )
        mismatch_feedback = client.post(
            f"/v1/reviews/{mismatch_review_id}/feedback",
            json={
                "reviewer_id": "alice",
                "reviewer_role": "observer",
                "decision": "approve",
            },
        )
        inherited_feedback = client.post(
            f"/v1/reviews/{inherited_role_review_id}/feedback",
            json={"reviewer_id": "alice", "decision": "approve"},
        )

    assert mismatch_feedback.status_code == 422
    assert inherited_feedback.status_code == 200
    assert inherited_feedback.json()["reviewer_role"] == "maintainer"
    assert inherited_feedback.json()["normalized_payload"]["reviewer_role"] == "maintainer"


def test_review_routes_block_application_for_stale_feedback(tmp_path):
    app = create_app(db_path=tmp_path / "evolution.db", artifact_root=tmp_path / "artifacts")

    with TestClient(app) as client:
        created = client.post(
            "/v1/reviews",
            json={
                "review_type": "promotion",
                "artifact_ids": ["art_a"],
                "packet": {"questions": ["Approve?"]},
            },
        )
        review_id = created.json()["review_id"]
        client.post(f"/v1/reviews/{review_id}/claim", json={"reviewer_id": "alice"})
        feedback = client.post(
            f"/v1/reviews/{review_id}/feedback",
            json={"reviewer_id": "alice", "decision": "approve"},
        )
        feedback_id = feedback.json()["feedback_id"]
        stale = client.post(f"/v1/reviews/{review_id}/mark-stale")
        feedback_after_stale = client.get(f"/v1/reviews/{review_id}/feedback")
        application = client.post(
            "/v1/feedback-applications",
            json={
                "feedback_id": feedback_id,
                "target_type": "prompt_seed",
                "target_id": "job_next",
                "consumed_by_method": "agent_system_gepa_reflector",
                "effect_summary": "Used feedback.",
            },
        )

    assert stale.status_code == 200
    assert feedback_after_stale.status_code == 200
    assert feedback_after_stale.json()[0]["status"] == "archived_only"
    assert application.status_code == 422


def test_review_routes_reject_duplicate_application_with_changed_effect_summary(tmp_path):
    app = create_app(db_path=tmp_path / "evolution.db", artifact_root=tmp_path / "artifacts")

    with TestClient(app) as client:
        created = client.post(
            "/v1/reviews",
            json={
                "review_type": "promotion",
                "artifact_ids": ["art_a"],
                "packet": {"questions": ["Approve?"]},
            },
        )
        review_id = created.json()["review_id"]
        client.post(f"/v1/reviews/{review_id}/claim", json={"reviewer_id": "alice"})
        feedback = client.post(
            f"/v1/reviews/{review_id}/feedback",
            json={"reviewer_id": "alice", "decision": "approve"},
        )
        feedback_id = feedback.json()["feedback_id"]
        first = client.post(
            "/v1/feedback-applications",
            json={
                "feedback_id": feedback_id,
                "target_type": "prompt_seed",
                "target_id": "job_next",
                "consumed_by_method": "agent_system_gepa_reflector",
                "effect_summary": "Used feedback.",
            },
        )
        changed_summary = client.post(
            "/v1/feedback-applications",
            json={
                "feedback_id": feedback_id,
                "target_type": "prompt_seed",
                "target_id": "job_next",
                "consumed_by_method": "agent_system_gepa_reflector",
                "effect_summary": "Used feedback differently.",
            },
        )

    assert first.status_code == 200
    assert changed_summary.status_code == 422


def test_review_routes_return_404_for_unknown_feedback_application_source(tmp_path):
    app = create_app(db_path=tmp_path / "evolution.db", artifact_root=tmp_path / "artifacts")

    with TestClient(app) as client:
        application = client.post(
            "/v1/feedback-applications",
            json={
                "feedback_id": "hfb_missing",
                "target_type": "prompt_seed",
                "target_id": "job_next",
                "consumed_by_method": "agent_system_gepa_reflector",
                "effect_summary": "Used feedback.",
            },
        )

    assert application.status_code == 404


def test_review_routes_reject_invalid_lifecycle_enum_values(tmp_path):
    app = create_app(db_path=tmp_path / "evolution.db", artifact_root=tmp_path / "artifacts")

    with TestClient(app) as client:
        invalid_review = client.post(
            "/v1/reviews",
            json={
                "review_type": "not_a_review_type",
                "artifact_ids": ["art_a"],
                "packet": {"questions": ["Approve?"]},
            },
        )
        valid_review = client.post(
            "/v1/reviews",
            json={
                "review_type": "promotion",
                "artifact_ids": ["art_a"],
                "packet": {"questions": ["Approve?"]},
            },
        )
        review_id = valid_review.json()["review_id"]
        client.post(f"/v1/reviews/{review_id}/claim", json={"reviewer_id": "alice"})
        invalid_feedback = client.post(
            f"/v1/reviews/{review_id}/feedback",
            json={"reviewer_id": "alice", "decision": "rubber_stamp"},
        )
        invalid_query_decision = client.post(
            "/v1/query-decisions",
            json={"decision": "maybe_human"},
        )
        invalid_feedback_application = client.post(
            "/v1/feedback-applications",
            json={
                "feedback_id": "hfb_missing",
                "target_type": "side_channel",
                "target_id": "target_1",
                "consumed_by_method": "reflector",
                "effect_summary": "Used feedback.",
            },
        )

    assert invalid_review.status_code == 422
    assert valid_review.status_code == 200
    assert invalid_feedback.status_code == 422
    assert invalid_query_decision.status_code == 422
    assert invalid_feedback_application.status_code == 422


def test_query_decision_accepts_all_spec_values_and_rejects_skip_human(tmp_path):
    app = create_app(db_path=tmp_path / "evolution.db", artifact_root=tmp_path / "artifacts")
    valid_decisions = (
        "ask_human",
        "ask_llm",
        "auto_promote",
        "auto_reject",
        "run_more_eval",
        "defer",
    )

    for decision in valid_decisions:
        assert HumanQueryDecisionCreateRequest(decision=decision).decision == decision

    try:
        HumanQueryDecisionCreateRequest(decision="skip_human")
    except ValidationError:
        pass
    else:
        raise AssertionError("expected skip_human query decision to be rejected")

    with TestClient(app) as client:
        valid_responses = [
            client.post("/v1/query-decisions", json={"decision": decision})
            for decision in valid_decisions
        ]
        invalid_response = client.post(
            "/v1/query-decisions",
            json={"decision": "skip_human"},
        )

    assert [response.status_code for response in valid_responses] == [200] * len(
        valid_decisions
    )
    assert [response.json()["decision"] for response in valid_responses] == list(
        valid_decisions
    )
    assert invalid_response.status_code == 422


def test_hitl_request_models_reject_invalid_enum_values():
    invalid_requests = (
        lambda: ReviewRequestCreateRequest(
            review_type="not_a_review_type",
            artifact_ids=["art_a"],
            packet={"questions": ["Approve?"]},
        ),
        lambda: HumanFeedbackCreateRequest(
            reviewer_id="alice",
            decision="rubber_stamp",
        ),
        lambda: HumanQueryDecisionCreateRequest(decision="maybe_human"),
        lambda: FeedbackApplicationCreateRequest(
            feedback_id="hfb_1",
            target_type="side_channel",
            target_id="target_1",
            consumed_by_method="reflector",
            effect_summary="Used feedback.",
        ),
    )

    for build_request in invalid_requests:
        try:
            build_request()
        except ValidationError:
            pass
        else:
            raise AssertionError("expected invalid enum-like value to be rejected")
