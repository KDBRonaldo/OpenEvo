from __future__ import annotations

import json
from pathlib import Path

import pytest

from openevo.evolution.framework import (
    CORE_CONFIG_RESERVED_KEYS,
    HarnessInferenceRequest,
    HarnessInferenceResponse,
    MethodExecutionContext,
    MethodExecutionServices,
    MethodInputBinding,
    ResolvedMethodInputBinding,
    build_execution_envelope,
    invoke_legacy_method,
    resolve_method_inputs,
    worker_input_artifact_digest,
)
from openevo.evolution.models import (
    ArtifactRegisterRequest,
    WorkerClaimInputArtifact,
    WorkerClaimedJob,
)


class _Harness:
    def infer(self, request: HarnessInferenceRequest) -> HarnessInferenceResponse:
        return HarnessInferenceResponse(
            request_id=request.request_id,
            text="response",
            capture_mode="transcript",
        )


def _artifact(artifact_id: str, artifact_type: str) -> WorkerClaimInputArtifact:
    return WorkerClaimInputArtifact(
        artifact_id=artifact_id,
        type=artifact_type,
        uri=f"file:///artifacts/{artifact_id}",
    )


def test_method_inputs_preserve_binding_and_candidate_order() -> None:
    bindings = (
        MethodInputBinding(
            binding_id="current",
            source="current_dataset",
            artifact_type="dataset",
            min_count=1,
            max_count=1,
        ),
        MethodInputBinding(
            binding_id="history",
            source="history_datasets",
            artifact_type="dataset",
            max_count=4,
        ),
    )
    current = _artifact("dataset-current", "dataset")
    history_2 = _artifact("dataset-r2", "dataset")
    history_1 = _artifact("dataset-r1", "dataset")

    resolved = resolve_method_inputs(
        bindings,
        {
            "history": (history_2, history_1),
            "current": (current,),
        },
    )

    assert [artifact.artifact_id for artifact in resolved.input_artifacts] == [
        "dataset-current",
        "dataset-r2",
        "dataset-r1",
    ]
    assert resolved.bindings == (
        ResolvedMethodInputBinding(
            binding_id="current",
            artifact_ids=("dataset-current",),
            artifact_digests=(worker_input_artifact_digest(current),),
        ),
        ResolvedMethodInputBinding(
            binding_id="history",
            artifact_ids=("dataset-r2", "dataset-r1"),
            artifact_digests=(
                worker_input_artifact_digest(history_2),
                worker_input_artifact_digest(history_1),
            ),
        ),
    )

    duplicates = resolve_method_inputs(
        (
            MethodInputBinding(
                binding_id="history",
                source="history_datasets",
                artifact_type="dataset",
                max_count=3,
            ),
        ),
        {"history": (history_1, history_1)},
    )
    assert [item.artifact_id for item in duplicates.input_artifacts] == [
        "dataset-r1",
        "dataset-r1",
    ]


def test_method_input_resolution_fails_closed() -> None:
    binding = MethodInputBinding(
        binding_id="current",
        source="current_dataset",
        artifact_type="dataset",
        min_count=1,
        max_count=1,
    )
    with pytest.raises(ValueError, match="unknown input binding"):
        resolve_method_inputs((binding,), {"other": ()})
    with pytest.raises(ValueError, match="requires at least"):
        resolve_method_inputs((binding,), {"current": ()})
    with pytest.raises(ValueError, match="allows at most"):
        resolve_method_inputs(
            (binding,),
            {"current": (_artifact("one", "dataset"), _artifact("two", "dataset"))},
        )
    with pytest.raises(ValueError, match="artifact type"):
        resolve_method_inputs((binding,), {"current": (_artifact("one", "report"),)})


def test_execution_envelope_separates_config_and_legacy_adapter_is_exact(
    tmp_path: Path,
) -> None:
    artifacts = [_artifact("current", "dataset"), _artifact("history", "dataset")]
    envelope = build_execution_envelope(
        plan_id="plan-1",
        plan_digest="a" * 64,
        registry_snapshot_digest="b" * 64,
        target_id="agent_system",
        method_id="agent_system_reflector",
        method_identity_digest="c" * 64,
        user_config={"reflector_llm": {"model": "gpt-5.5"}},
        core_config={"round_index": 2, "task_id": "task-1"},
        input_bindings=(
            ResolvedMethodInputBinding(
                binding_id="current",
                artifact_ids=("current",),
                artifact_digests=(worker_input_artifact_digest(artifacts[0]),),
            ),
            ResolvedMethodInputBinding(
                binding_id="history",
                artifact_ids=("history",),
                artifact_digests=(worker_input_artifact_digest(artifacts[1]),),
            ),
        ),
        output_artifact_types=("agent_system",),
    )
    original = WorkerClaimedJob(
        job_id="job-1",
        lease_id="lease-1",
        job_type="agent_system_reflector",
        method="agent_system_reflector",
        input_artifacts=artifacts,
        config={"must_not_leak": True},
    )
    context = MethodExecutionContext(
        job=original,
        artifact_root=tmp_path,
        envelope=envelope,
        services=MethodExecutionServices(harness=_Harness()),
    )
    observed: dict[str, object] = {}

    def legacy_method(
        job: WorkerClaimedJob,
        artifact_root: Path,
    ) -> list[ArtifactRegisterRequest]:
        observed["job"] = job
        observed["artifact_root"] = artifact_root
        return []

    assert invoke_legacy_method(legacy_method, context) == []
    legacy_job = observed["job"]
    assert isinstance(legacy_job, WorkerClaimedJob)
    assert legacy_job.config == {
        "reflector_llm": {"model": "gpt-5.5"},
        "round_index": 2,
        "task_id": "task-1",
    }
    assert [item.artifact_id for item in legacy_job.input_artifacts] == [
        "current",
        "history",
    ]
    assert observed["artifact_root"] == tmp_path
    assert original.config == {"must_not_leak": True}

    context.job.input_artifacts.reverse()
    with pytest.raises(ValueError, match="input artifact order"):
        invoke_legacy_method(legacy_method, context)


def test_execution_envelope_rejects_core_user_shadowing() -> None:
    with pytest.raises(ValueError, match="shadow Core-owned"):
        build_execution_envelope(
            plan_id="plan-1",
            plan_digest="a" * 64,
            registry_snapshot_digest="b" * 64,
            target_id="memory",
            method_id="reflect",
            method_identity_digest="c" * 64,
            user_config={"round_index": 9},
            core_config={"round_index": 1},
            input_bindings=(),
            output_artifact_types=("memory",),
        )

    with pytest.raises(ValueError, match="reserved for Core"):
        build_execution_envelope(
            plan_id="plan-1",
            plan_digest="a" * 64,
            registry_snapshot_digest="b" * 64,
            target_id="memory",
            method_id="reflect",
            method_identity_digest="c" * 64,
            user_config={"task_id": "forged"},
            core_config={},
            input_bindings=(),
            output_artifact_types=("memory",),
        )

    with pytest.raises(ValueError, match="only contain Core-owned"):
        build_execution_envelope(
            plan_id="plan-1",
            plan_digest="a" * 64,
            registry_snapshot_digest="b" * 64,
            target_id="memory",
            method_id="reflect",
            method_identity_digest="c" * 64,
            user_config={},
            core_config={"algorithm_setting": True},
            input_bindings=(),
            output_artifact_types=("memory",),
        )


def test_evaluator_and_audit_controls_are_core_owned(tmp_path: Path) -> None:
    controls = {
        "agent_system_audit": {"enabled": True},
        "candidate_evaluations": {"candidate-1": {"f1": 0.5}},
        "forbidden_literals": {"source_files": ["heldout.txt"]},
        "promotion_support": {"validation_checks": ["run heldout evaluation"]},
    }

    envelope = build_execution_envelope(
        plan_id="plan-controls",
        plan_digest="a" * 64,
        registry_snapshot_digest="b" * 64,
        target_id="agent_system",
        method_id="agent_system_gepa_reflector",
        method_identity_digest="c" * 64,
        user_config={},
        core_config=controls,
        input_bindings=(),
        output_artifact_types=("agent_system", "report"),
    )

    assert json.loads(envelope.core_config_json) == controls
    for key in controls:
        assert key in CORE_CONFIG_RESERVED_KEYS

    observed: dict[str, object] = {}

    def legacy_method(job: WorkerClaimedJob, artifact_root: Path):
        observed["config"] = job.config
        observed["artifact_root"] = artifact_root
        return []

    context = MethodExecutionContext(
        job=WorkerClaimedJob(
            job_id="job-controls",
            lease_id="lease-controls",
            job_type="agent_system_gepa_reflector",
            method="agent_system_gepa_reflector",
        ),
        artifact_root=tmp_path,
        envelope=envelope,
        services=MethodExecutionServices(harness=_Harness()),
    )
    assert invoke_legacy_method(legacy_method, context) == []
    assert observed == {"config": controls, "artifact_root": tmp_path}


def test_execution_envelope_binds_full_duplicate_artifact_snapshots(
    tmp_path: Path,
) -> None:
    first = _artifact("duplicate", "dataset")
    second = first.model_copy(update={"uri": "file:///artifacts/other-copy"})
    resolution = resolve_method_inputs(
        (
            MethodInputBinding(
                binding_id="datasets",
                source="history_datasets",
                artifact_type="dataset",
                max_count=2,
            ),
        ),
        {"datasets": (first, second)},
    )
    envelope = build_execution_envelope(
        plan_id="plan-duplicate",
        plan_digest="a" * 64,
        registry_snapshot_digest="b" * 64,
        target_id="memory",
        method_id="reflect",
        method_identity_digest="c" * 64,
        user_config={},
        core_config={},
        input_bindings=resolution.bindings,
        output_artifact_types=("memory",),
    )
    context = MethodExecutionContext(
        job=WorkerClaimedJob(
            job_id="job-duplicate",
            lease_id="lease-duplicate",
            job_type="reflect",
            method="reflect",
            input_artifacts=list(resolution.input_artifacts),
        ),
        artifact_root=tmp_path,
        envelope=envelope,
        services=MethodExecutionServices(harness=_Harness()),
    )
    context.job.input_artifacts.reverse()
    with pytest.raises(ValueError, match="artifact snapshots"):
        invoke_legacy_method(lambda job, root: [], context)


def test_harness_contract_exposes_no_endpoint_or_credentials() -> None:
    fields = set(HarnessInferenceRequest.model_fields)
    assert not fields.intersection(
        {"api_key", "api_key_ref", "base_url", "endpoint", "headers", "token"}
    )
    with pytest.raises(ValueError, match="Extra inputs"):
        HarnessInferenceRequest(
            request_id="request-1",
            harness_id="codex",
            prompt="Reflect.",
            metadata_json='{"api_key":"secret"}',
        )
    with pytest.raises(ValueError, match="less than or equal"):
        HarnessInferenceRequest(
            request_id="request-1",
            harness_id="codex",
            prompt="Reflect.",
            max_output_tokens=10**100,
        )
    with pytest.raises(ValueError, match="at most"):
        HarnessInferenceResponse(
            request_id="request-1",
            text="x" * 1_048_577,
            capture_mode="transcript",
        )
    with pytest.raises(ValueError, match="maximum UTF-8 bytes"):
        HarnessInferenceRequest(
            request_id="request-1",
            harness_id="codex",
            prompt="界" * 400_000,
        )
    with pytest.raises(ValueError, match="maximum UTF-8 bytes"):
        HarnessInferenceResponse(
            request_id="request-1",
            text="界" * 400_000,
            capture_mode="transcript",
        )
