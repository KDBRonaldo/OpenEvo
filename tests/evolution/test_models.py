from __future__ import annotations

from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

from openevo.evolution.models import (
    ArtifactType,
    ContextResolveRequest,
    EventIngestRequest,
    JobState,
    WorkerClaimRequest,
    WorkerClaimResponse,
)


def test_event_ingest_requires_source_identity():
    created_at = datetime(2026, 1, 2, 3, 4, 5, tzinfo=timezone.utc)
    request = EventIngestRequest(
        source="openevo",
        event_type="openevo.session_completed",
        source_event_id="session:abc",
        created_at=created_at,
        task_id="task_1",
        session_id="abc",
        payload={"session_result": {"status": "COMPLETED"}},
    )

    assert request.source == "openevo"
    assert request.created_at == created_at
    assert request.payload["session_result"]["status"] == "COMPLETED"
    assert request.model_dump(mode="json")["created_at"] == "2026-01-02T03:04:05Z"


def test_event_ingest_rejects_empty_event_type():
    with pytest.raises(ValidationError) as exc_info:
        EventIngestRequest(
            source="openevo",
            event_type="",
            source_event_id="session:abc",
            payload={},
        )

    error = exc_info.value.errors()[0]
    assert error["loc"] == ("event_type",)
    assert error["type"] == "string_too_short"


def test_event_ingest_rejects_invalid_created_at():
    with pytest.raises(ValidationError) as exc_info:
        EventIngestRequest(
            source="openevo",
            event_type="openevo.session_completed",
            source_event_id="session:abc",
            created_at="not-a-date",
            payload={},
        )

    error = exc_info.value.errors()[0]
    assert error["loc"] == ("created_at",)
    assert error["type"] == "datetime_from_date_parsing"


def test_context_resolve_request_defaults_limits():
    request = ContextResolveRequest(
        task_id="task_1",
        instruction="solve",
        agent={"harness": "codex"},
        base_model="Qwen/Qwen3.6-27B",
    )

    assert request.limits.max_memory_chars == 12000
    assert request.limits.max_agent_system_chars == 12000
    assert request.limits.max_skill_bundles == 4
    assert request.limits.max_adapters == 2


@pytest.mark.parametrize("field", ["task_id", "instruction"])
def test_context_resolve_request_rejects_empty_required_fields(field: str):
    data = {
        "task_id": "task_1",
        "instruction": "solve",
    }
    data[field] = ""

    with pytest.raises(ValidationError) as exc_info:
        ContextResolveRequest(**data)

    error = exc_info.value.errors()[0]
    assert error["loc"] == (field,)
    assert error["type"] == "string_too_short"


def test_worker_claim_request_and_enums():
    request = WorkerClaimRequest(
        worker_id="worker_1",
        capabilities=["parametric_memory_train"],
    )

    assert request.lease_seconds == 600
    assert ArtifactType.PARAMETRIC_MEMORY == "parametric_memory"
    assert ArtifactType.AGENT_SYSTEM == "agent_system"
    assert JobState.PENDING == "pending"


def test_worker_claim_request_binds_method_names_to_identity_digests():
    request = WorkerClaimRequest(
        worker_id="verified-worker",
        method_capabilities=["skill_bundle_reflector"],
        method_identity_capabilities={"skill_bundle_reflector": "a" * 64},
    )

    assert request.method_identity_capabilities == {
        "skill_bundle_reflector": "a" * 64
    }
    with pytest.raises(ValidationError, match="same methods"):
        WorkerClaimRequest(
            worker_id="mismatched-worker",
            method_capabilities=["skill_bundle_reflector"],
            method_identity_capabilities={"text_memory_reflector": "a" * 64},
        )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("capabilities", [f"capability-{index}" for index in range(257)]),
        ("method_capabilities", [f"method-{index}" for index in range(257)]),
        ("capabilities", ["duplicate", "duplicate"]),
        ("method_capabilities", ["duplicate", "duplicate"]),
    ],
)
def test_worker_claim_request_rejects_unbounded_or_duplicate_capabilities(
    field: str,
    value: list[str],
):
    with pytest.raises(ValidationError):
        WorkerClaimRequest(worker_id="worker", **{field: value})


def test_worker_claim_response_validates_typed_job_payload():
    response = WorkerClaimResponse(
        job={
            "job_id": "job_1",
            "lease_id": "lease_1",
            "job_type": "train",
            "method": "parametric_memory_train",
            "input_artifacts": [
                {
                    "artifact_id": "artifact_1",
                    "type": "parametric_memory",
                    "uri": "s3://bucket/model",
                },
                {
                    "artifact_id": "artifact_2",
                    "type": "custom_external_type",
                    "name": "external",
                    "uri": "s3://bucket/external",
                },
            ],
            "config": {"epochs": 1},
            "priority": 50,
            "state": "claimed",
        }
    )

    assert response.job is not None
    assert response.job.input_artifacts[0].type == ArtifactType.PARAMETRIC_MEMORY
    assert response.job.input_artifacts[1].type == "custom_external_type"
    assert response.model_dump(mode="json") == {
        "job": {
            "job_id": "job_1",
            "lease_id": "lease_1",
            "job_type": "train",
            "method": "parametric_memory_train",
            "input_artifacts": [
                {
                    "artifact_id": "artifact_1",
                    "type": "parametric_memory",
                    "uri": "s3://bucket/model",
                    "name": None,
                    "manifest_sha256": None,
                    "records_byte_size": None,
                    "records_sha256": None,
                },
                {
                    "artifact_id": "artifact_2",
                    "type": "custom_external_type",
                    "uri": "s3://bucket/external",
                    "name": "external",
                    "manifest_sha256": None,
                    "records_byte_size": None,
                    "records_sha256": None,
                },
            ],
            "config": {"epochs": 1},
            "priority": 50,
            "state": "claimed",
            "plan": None,
            "target_id": None,
            "registry_snapshot_digest": None,
            "method_identity_digest": None,
            "execution_envelope": None,
            "execution_envelope_digest": None,
        }
    }


def test_worker_claim_response_rejects_missing_artifact_uri():
    with pytest.raises(ValidationError) as exc_info:
        WorkerClaimResponse(
            job={
                "job_id": "job_1",
                "lease_id": "lease_1",
                "job_type": "train",
                "method": "parametric_memory_train",
                "input_artifacts": [
                    {
                        "artifact_id": "artifact_1",
                        "type": "parametric_memory",
                    }
                ],
                "config": {},
            }
        )

    error = exc_info.value.errors()[0]
    assert error["loc"] == ("job", "input_artifacts", 0, "uri")
    assert error["type"] == "missing"
