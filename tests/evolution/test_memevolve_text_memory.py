from __future__ import annotations

import json
from pathlib import Path

import pytest

from openevo.evolution.framework.builtins import (
    ImplementationDistributionIdentity,
    build_builtin_registry,
)
from openevo.evolution.framework.execution import (
    HarnessInferenceRequest,
    HarnessInferenceResponse,
    MethodExecutionContext,
    MethodExecutionServices,
    ResolvedMethodInputBinding,
    build_execution_envelope,
    worker_input_artifact_digest,
)
from openevo.evolution.methods import METHOD_METADATA, METHOD_REGISTRY
from openevo.evolution.models import WorkerClaimInputArtifact, WorkerClaimedJob


class _Harness:
    def __init__(self, responses: list[str]) -> None:
        self._responses = iter(responses)
        self.requests: list[HarnessInferenceRequest] = []

    def infer(self, request: HarnessInferenceRequest) -> HarnessInferenceResponse:
        self.requests.append(request)
        return HarnessInferenceResponse(
            request_id=request.request_id,
            text=next(self._responses),
            capture_mode="transcript",
        )


def _dataset(tmp_path: Path) -> WorkerClaimInputArtifact:
    dataset_dir = tmp_path / "dataset"
    dataset_dir.mkdir()
    records_path = dataset_dir / "records.jsonl"
    records_path.write_text(
        json.dumps(
            {
                "task_id": "task-1",
                "session_id": "session-1",
                "status": "FAILED",
                "reward": 0.0,
                "traces": [
                    {
                        "prompt_messages": [
                            {"role": "user", "content": "Repair the package."}
                        ],
                        "response_messages": [
                            {
                                "role": "assistant",
                                "content": "Changed code without running focused tests.",
                            }
                        ],
                        "metadata": {
                            "transcript": "Verifier exposed SECRET-ROW-ANSWER.",
                        },
                    }
                ],
            },
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    manifest_path = dataset_dir / "manifest.json"
    manifest_path.write_text(
        json.dumps(
            {
                "name": "memory trajectories",
                "records_path": "records.jsonl",
                "records_uri": records_path.as_uri(),
                "forbidden_literals": ["SECRET-ROW-ANSWER"],
            }
        ),
        encoding="utf-8",
    )
    return WorkerClaimInputArtifact(
        artifact_id="dataset-1",
        type="dataset",
        uri=manifest_path.as_uri(),
        name="memory trajectories",
    )


def _prior_memory(tmp_path: Path) -> WorkerClaimInputArtifact:
    path = tmp_path / "prior-memory.md"
    path.write_text("# Prior memory\n\n- Always inspect first.\n", encoding="utf-8")
    return WorkerClaimInputArtifact(
        artifact_id="memory-0",
        type="text_memory",
        uri=path.as_uri(),
        name="prior memory",
    )


def _context(
    tmp_path: Path,
    harness: _Harness,
    *,
    candidate_count: int = 2,
) -> MethodExecutionContext:
    artifacts = (_dataset(tmp_path), _prior_memory(tmp_path))
    bindings = (
        ResolvedMethodInputBinding(
            binding_id="dataset_inputs",
            artifact_ids=(artifacts[0].artifact_id,),
            artifact_digests=(worker_input_artifact_digest(artifacts[0]),),
        ),
        ResolvedMethodInputBinding(
            binding_id="prior_target_artifacts",
            artifact_ids=(artifacts[1].artifact_id,),
            artifact_digests=(worker_input_artifact_digest(artifacts[1]),),
        ),
    )
    user_config = {
        "candidate_count": candidate_count,
        "max_records": 10,
        "reflector_llm": {"model": "gpt-5.5", "provider": "codex_cli"},
    }
    core_config = {
        "compatibility": {"agent_harnesses": ["codex"]},
        "forbidden_literals": ["SECRET-ROW-ANSWER"],
        "scores": {"heldout_reward_delta": 0.1},
        "tags": ["research"],
    }
    envelope = build_execution_envelope(
        plan_id="plan-memevolve",
        plan_digest="a" * 64,
        registry_snapshot_digest="b" * 64,
        target_id="text_memory",
        method_id="text_memory_memevolve",
        method_identity_digest="c" * 64,
        user_config=user_config,
        core_config=core_config,
        input_bindings=bindings,
        output_artifact_types=("text_memory",),
    )
    return MethodExecutionContext(
        job=WorkerClaimedJob(
            job_id="job-memevolve",
            lease_id="lease-memevolve",
            job_type="text_memory",
            method="text_memory_memevolve",
            input_artifacts=list(artifacts),
            config={**user_config, **core_config},
        ),
        artifact_root=tmp_path / "artifacts",
        envelope=envelope,
        services=MethodExecutionServices(harness=harness),
    )


def test_memevolve_descriptor_is_context_only_and_not_legacy_registered() -> None:
    snapshot = build_builtin_registry(
        ImplementationDistributionIdentity(
            distribution="openevo",
            distribution_version="0.1.0",
            distribution_digest="d" * 64,
        )
    )

    descriptor = snapshot.methods["text_memory_memevolve"]
    assert descriptor.invocation_abi.value == "method_context_v1"
    assert descriptor.target_id == "text_memory"
    assert descriptor.output_artifact_types == ("text_memory",)
    assert descriptor.implementation_ref is not None
    assert descriptor.implementation_ref.entry_point == (
        "openevo.evolution.memevolve:text_memory_memevolve"
    )
    assert "text_memory_memevolve" not in METHOD_REGISTRY
    assert "text_memory_memevolve" not in METHOD_METADATA


def test_memevolve_runs_independent_analysis_generation_and_selection(
    tmp_path: Path,
) -> None:
    from openevo.evolution.memevolve import text_memory_memevolve

    candidate_one = "# Memory\n\n## Validate\n- Run focused tests first."
    candidate_two = (
        "# Memory\n\n## Do\n- Inspect failures first.\n\n"
        "## Validate\n- Never persist SECRET-ROW-ANSWER."
    )
    harness = _Harness(
        [
            "Analysis one: validation was missing.",
            candidate_one,
            "Analysis two: prior advice was too broad.",
            candidate_two,
            json.dumps({"winner_index": 2, "quality": 0.82}),
        ]
    )

    artifacts = text_memory_memevolve(_context(tmp_path, harness))

    assert len(harness.requests) == 5
    assert [request.request_id for request in harness.requests] == [
        "memevolve-analysis-1",
        "memevolve-generate-1",
        "memevolve-analysis-2",
        "memevolve-generate-2",
        "memevolve-select",
    ]
    assert all(request.harness_id == "codex" for request in harness.requests)
    assert all(request.model_name == "gpt-5.5" for request in harness.requests)
    assert all(request.temperature is None for request in harness.requests)
    assert all(request.max_output_tokens is None for request in harness.requests)
    assert "SECRET-ROW-ANSWER" not in "\n".join(
        request.prompt for request in harness.requests
    )

    assert len(artifacts) == 1
    artifact = artifacts[0]
    output = Path(artifact.uri.removeprefix("file://")).read_text(encoding="utf-8")
    assert "Inspect failures first" in output
    assert "SECRET-ROW-ANSWER" not in output
    assert "[REDACTED_LITERAL_1]" in output
    assert artifact.manifest["method"] == "text_memory_memevolve"
    assert artifact.manifest["adaptation_scope"] == "declarative_text_memory_v1"
    assert artifact.manifest["paper_equivalent"] is False
    assert artifact.manifest["candidate_count"] == 2
    assert artifact.manifest["selected_candidate_index"] == 2
    assert artifact.lineage["input_artifact_ids"] == ["dataset-1", "memory-0"]
    assert artifact.compatibility == {"agent_harnesses": ["codex"]}
    assert artifact.scores == {"heldout_reward_delta": 0.1, "quality": 0.82}
    assert artifact.tags == ["research"]


def test_memevolve_rejects_out_of_range_selection(tmp_path: Path) -> None:
    from openevo.evolution.memevolve import text_memory_memevolve

    harness = _Harness(
        [
            "Analysis one",
            "# Memory\n\n- Candidate one",
            "Analysis two",
            "# Memory\n\n- Candidate two",
            json.dumps({"winner_index": 3, "quality": 0.5}),
        ]
    )

    with pytest.raises(ValueError, match="winner index"):
        text_memory_memevolve(_context(tmp_path, harness))


def test_memevolve_rejects_executable_provider_candidate(tmp_path: Path) -> None:
    from openevo.evolution.memevolve import text_memory_memevolve

    harness = _Harness(
        [
            "Analysis one",
            "# Memory\n\ndef provide_memory(self, request):\n    return request",
        ]
    )

    with pytest.raises(ValueError, match="executable provider code"):
        text_memory_memevolve(_context(tmp_path, harness))
    assert len(harness.requests) == 2
