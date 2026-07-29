from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from openevo.evolution import methods as methods_module
from openevo.evolution.framework import (
    EvolutionExecutionProfile,
    EvolutionTargetSelection,
)
from openevo.evolution.framework.builtins import (
    ImplementationDistributionIdentity,
    build_builtin_registry,
)
from openevo.evolution.framework.contracts import canonical_digest, canonical_json
from openevo.evolution.framework.execution import (
    MethodExecutionContext,
    MethodExecutionServices,
    ResolvedMethodInputBinding,
    build_execution_envelope,
    worker_input_artifact_digest,
)
from openevo.evolution.models import (
    ArtifactType,
    DatasetCreateRequest,
    EventIngestRequest,
    WorkerClaimInputArtifact,
    WorkerClaimRequest,
    WorkerClaimedJob,
    WorkerCompleteRequest,
    WorkerFailRequest,
    WorkerHeartbeatRequest,
)
from openevo.evolution.parametric.contracts import (
    SD_LORA_STATE_MANIFEST,
    SD_LORA_STATE_WEIGHTS,
    SdLoraStateComponent,
    SdLoraStateManifest,
    SdLoraStateModule,
    SdLoraTrainingRequest,
    SdLoraTrainingResult,
)
from openevo.evolution.parametric.sd_lora import parametric_memory_sd_lora
from openevo.evolution.planned_jobs import (
    PlanBoundJobCreateRequest,
    PlannedInputBinding,
)
from openevo.evolution.store import EvolutionStore
from openevo.evolution.worker import run_once


_MODEL_REVISION = "0123456789abcdef0123456789abcdef01234567"


def _private_write(path: Path, payload: bytes) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    path.write_bytes(payload)
    path.chmod(0o600)


def _dataset(
    artifact_root: Path,
    identity: str,
    *,
    successful_text: str = "Run focused tests and inspect their output.",
    tool_call_trace: bool = False,
) -> WorkerClaimInputArtifact:
    dataset_dir = artifact_root / "datasets" / identity
    dataset_dir.mkdir(mode=0o700, parents=True)
    successful_trace: dict[str, object]
    if tool_call_trace:
        successful_trace = {
            "prompt_messages": [
                {"content": "Repair the repository.", "role": "user"},
                {
                    "content": "",
                    "role": "assistant",
                    "tool_calls": [
                        {
                            "id": "call-read",
                            "type": "function",
                            "function": {
                                "name": "read_task",
                                "arguments": {"task_id": "task-tool"},
                            },
                        }
                    ],
                },
                {
                    "content": "Use the repository tests.",
                    "role": "tool",
                    "tool_call_id": "call-read",
                },
            ],
            "response_messages": [
                {
                    "content": "",
                    "role": "assistant",
                    "tool_calls": [
                        {
                            "id": "call-exec",
                            "type": "function",
                            "function": {
                                "name": "run_command",
                                "arguments": {"command": "pytest -q"},
                            },
                        }
                    ],
                }
            ],
            "tools": [
                {
                    "type": "function",
                    "function": {
                        "name": "read_task",
                        "description": "Read the current task.",
                        "parameters": {
                            "type": "object",
                            "properties": {"task_id": {"type": "string"}},
                            "required": ["task_id"],
                        },
                    },
                },
                {
                    "type": "function",
                    "function": {
                        "name": "run_command",
                        "parameters": {
                            "type": "object",
                            "properties": {"command": {"type": "string"}},
                            "required": ["command"],
                        },
                    },
                },
            ],
        }
    else:
        successful_trace = {
            "prompt_messages": [{"content": "Repair the repository.", "role": "user"}],
            "response_messages": [{"content": successful_text, "role": "assistant"}],
        }
    records = [
        {
            "event_id": f"event-{identity}-success",
            "reward": 1.0,
            "session_id": f"session-{identity}",
            "status": "succeeded",
            "task_id": f"task-{identity}",
            "traces": [successful_trace],
        },
        {
            "event_id": f"event-{identity}-failure",
            "reward": 0.0,
            "session_id": f"session-{identity}-failure",
            "status": "failed",
            "task_id": f"task-{identity}-failure",
            "traces": [
                {
                    "prompt_messages": [{"content": "Ignore verification.", "role": "user"}],
                    "response_messages": [{"content": "UNSAFE-FAILED-TRACE", "role": "assistant"}],
                }
            ],
        },
    ]
    records_bytes = "".join(
        json.dumps(record, sort_keys=True, allow_nan=False) + "\n" for record in records
    ).encode("utf-8")
    records_path = dataset_dir / "records.jsonl"
    _private_write(records_path, records_bytes)
    manifest = {
        "name": f"dataset {identity}",
        "records_byte_size": len(records_bytes),
        "records_path": "records.jsonl",
        "records_sha256": hashlib.sha256(records_bytes).hexdigest(),
        "records_uri": records_path.as_uri(),
    }
    manifest_bytes = json.dumps(
        manifest,
        indent=2,
        sort_keys=True,
        allow_nan=False,
    ).encode("utf-8")
    manifest_path = dataset_dir / "manifest.json"
    _private_write(manifest_path, manifest_bytes)
    return WorkerClaimInputArtifact(
        artifact_id=f"dataset-{identity}",
        type="dataset",
        uri=manifest_path.as_uri(),
        name=f"dataset {identity}",
        manifest_sha256=canonical_digest(manifest),
        records_byte_size=len(records_bytes),
        records_sha256=hashlib.sha256(records_bytes).hexdigest(),
    )


class _Harness:
    def infer(self, request):  # pragma: no cover - SD-LoRA must not call inference.
        raise AssertionError(f"unexpected harness inference: {request!r}")


class _ExecutableRegistry:
    def __init__(self, snapshot, method_handles) -> None:
        self.snapshot = snapshot
        self.method_handles = method_handles


class _StoreWorkerClient:
    def __init__(self, store: EvolutionStore) -> None:
        self.store = store
        self.failed: list[dict[str, object]] = []

    def claim(
        self,
        worker_id: str,
        capabilities: list[str],
        *,
        lease_seconds: int | None = None,
        method_capabilities: list[str] | None = None,
        method_identity_capabilities: dict[str, str] | None = None,
    ):
        response = self.store.claim_job(
            WorkerClaimRequest(
                worker_id=worker_id,
                capabilities=capabilities,
                lease_seconds=lease_seconds or 600,
                method_capabilities=method_capabilities,
                method_identity_capabilities=method_identity_capabilities,
            )
        )
        return None if response.job is None else response.job.model_dump(mode="json")

    def heartbeat(
        self,
        job_id: str,
        lease_id: str,
        *,
        progress: float | None = None,
        message: str | None = None,
    ):
        return self.store.heartbeat_job(
            job_id,
            WorkerHeartbeatRequest(
                lease_id=lease_id,
                progress=progress,
                message=message,
            ),
        )

    def complete(
        self,
        job_id: str,
        lease_id: str,
        artifacts: list[dict],
        *,
        report: dict | None = None,
    ):
        return self.store.complete_job(
            job_id,
            WorkerCompleteRequest(
                lease_id=lease_id,
                artifacts=artifacts,
                report=report or {},
            ),
        )

    def fail(
        self,
        job_id: str,
        lease_id: str,
        error: str,
        *,
        retryable: bool = True,
    ):
        result = self.store.fail_job(
            job_id,
            WorkerFailRequest(
                lease_id=lease_id,
                error=error,
                retryable=retryable,
            ),
        )
        self.failed.append(result)
        return result


class _FakeTrainer:
    def __init__(self) -> None:
        self.requests: list[SdLoraTrainingRequest] = []
        self.training_records: list[list[dict[str, object]]] = []

    def train_sd_lora(self, request: SdLoraTrainingRequest) -> SdLoraTrainingResult:
        request = SdLoraTrainingRequest.model_validate(request)
        self.requests.append(request)
        work_dir = Path(request.work_dir)
        training_text = (work_dir / request.training_data_path).read_text(encoding="utf-8")
        assert '"messages"' in training_text
        assert "UNSAFE-FAILED-TRACE" not in training_text
        self.training_records.append(
            [json.loads(line) for line in training_text.splitlines() if line]
        )
        prior_count = 0
        if request.prior_adapter_path is not None:
            prior_dir = work_dir / request.prior_adapter_path
            assert prior_dir.is_dir()
            assert (prior_dir / SD_LORA_STATE_WEIGHTS).read_bytes() == b"state-weights"
            prior_state = json.loads(
                (prior_dir / SD_LORA_STATE_MANIFEST).read_text(encoding="utf-8")
            )
            prior_count = int(prior_state["component_count"])

        component_count = prior_count + 1
        output = work_dir / request.output_adapter_path
        output.mkdir(mode=0o700)
        _private_write(output / "adapter_config.json", b"{}\n")
        _private_write(output / "adapter_model.safetensors", b"adapter-weights")
        state_weights = b"state-weights"
        _private_write(output / SD_LORA_STATE_WEIGHTS, state_weights)
        state = SdLoraStateManifest(
            adapter_id=request.adapter_id,
            base_model=request.config.base_model,
            model_revision=request.config.model_revision,
            task_index=component_count - 1,
            component_count=component_count,
            effective_rank=component_count * request.config.rank,
            target_module_suffixes=request.config.target_modules,
            modules=(
                SdLoraStateModule(
                    name="model.layers.0.self_attn.q_proj",
                    in_features=2,
                    out_features=2,
                ),
            ),
            components=tuple(
                SdLoraStateComponent(
                    task_index=index,
                    rank=request.config.rank,
                    coefficient=0.8,
                )
                for index in range(component_count)
            ),
            state_weights_size_bytes=len(state_weights),
            state_weights_sha256=hashlib.sha256(state_weights).hexdigest(),
            training_record_count=request.training_record_count,
            steps_completed=2,
            training_loss=0.25,
            source_dataset_artifact_ids=request.source_dataset_artifact_ids,
            prior_parametric_memory_artifact_id=(request.prior_parametric_memory_artifact_id),
        )
        _private_write(
            output / SD_LORA_STATE_MANIFEST,
            (canonical_json(state.model_dump(mode="json")) + "\n").encode("utf-8"),
        )
        return SdLoraTrainingResult(
            request_id=request.request_id,
            adapter_path=request.output_adapter_path,
            state_manifest_path=(f"{request.output_adapter_path}/{SD_LORA_STATE_MANIFEST}"),
            state_weights_path=(f"{request.output_adapter_path}/{SD_LORA_STATE_WEIGHTS}"),
            training_record_count=request.training_record_count,
            steps_completed=2,
            training_loss=0.25,
            task_index=component_count - 1,
            component_count=component_count,
            effective_rank=component_count * request.config.rank,
            target_module_names=("model.layers.0.self_attn.q_proj",),
            coefficients=tuple(0.8 for _ in range(component_count)),
        )


class _TamperedStateTrainer(_FakeTrainer):
    def train_sd_lora(self, request: SdLoraTrainingRequest) -> SdLoraTrainingResult:
        result = super().train_sd_lora(request)
        state_weights = Path(request.work_dir) / result.state_weights_path
        state_weights.write_bytes(state_weights.read_bytes() + b"tampered")
        state_weights.chmod(0o600)
        return result


def _context(
    artifact_root: Path,
    trainer: _FakeTrainer | None,
    dataset: WorkerClaimInputArtifact,
    *,
    prior: WorkerClaimInputArtifact | None = None,
    generation: int = 0,
) -> MethodExecutionContext:
    artifacts = [dataset, *([prior] if prior is not None else [])]
    bindings = (
        ResolvedMethodInputBinding(
            binding_id="current_dataset",
            artifact_ids=(dataset.artifact_id,),
            artifact_digests=(worker_input_artifact_digest(dataset),),
        ),
        ResolvedMethodInputBinding(
            binding_id="prior_target_artifacts",
            artifact_ids=((prior.artifact_id,) if prior is not None else ()),
            artifact_digests=((worker_input_artifact_digest(prior),) if prior else ()),
        ),
    )
    user_config = {
        "base_model": "Qwen/Qwen3-0.6B",
        "max_records": 8,
        "model_revision": _MODEL_REVISION,
        "rank": 4,
    }
    core_config = {
        "compatibility": {"agent_harnesses": ["codex"]},
        "lineage": {"experiment_id": "experiment-sd-lora"},
        "scores": {"heldout_reward_delta": 0.1},
        "tags": ["research"],
    }
    envelope = build_execution_envelope(
        plan_id=f"plan-sd-lora-{generation}",
        plan_digest="a" * 64,
        registry_snapshot_digest="b" * 64,
        target_id="parametric_memory",
        method_id="parametric_memory_sd_lora",
        method_identity_digest="c" * 64,
        user_config=user_config,
        core_config=core_config,
        input_bindings=bindings,
        output_artifact_types=("parametric_memory",),
    )
    return MethodExecutionContext(
        job=WorkerClaimedJob(
            job_id=f"job-sd-lora-{generation}",
            lease_id=f"lease-sd-lora-{generation}",
            job_type="parametric_memory",
            method="parametric_memory_sd_lora",
            input_artifacts=artifacts,
            config={**user_config, **core_config},
        ),
        artifact_root=artifact_root,
        envelope=envelope,
        services=MethodExecutionServices(
            harness=_Harness(),
            parametric_trainer=trainer,
        ),
    )


def test_sd_lora_publishes_one_cumulative_adapter_across_generations(
    tmp_path: Path,
) -> None:
    artifact_root = tmp_path / "artifacts"
    artifact_root.mkdir(mode=0o700)
    trainer = _FakeTrainer()
    first = parametric_memory_sd_lora(
        _context(artifact_root, trainer, _dataset(artifact_root, "one"))
    )[0]

    assert first.manifest["routing_mode"] == "single_cumulative_adapter"
    assert first.manifest["component_count"] == 1
    assert first.manifest["paper_equivalent"] is False
    assert first.manifest["base_model"] == "Qwen/Qwen3-0.6B"
    assert first.compatibility["base_model"] == ["Qwen/Qwen3-0.6B"]
    assert first.scores["heldout_reward_delta"] == 0.1
    assert first.scores["quality"] == pytest.approx(0.8)
    assert trainer.requests[0].prior_adapter_path is None

    prior = WorkerClaimInputArtifact(
        artifact_id="parametric-memory-one",
        type="parametric_memory",
        uri=first.uri,
        name=first.name,
    )
    second = parametric_memory_sd_lora(
        _context(
            artifact_root,
            trainer,
            _dataset(
                artifact_root,
                "two",
                successful_text="Keep prior behavior while learning this task.",
            ),
            prior=prior,
            generation=1,
        )
    )[0]

    assert len(trainer.requests) == 2
    assert trainer.requests[1].prior_adapter_path == "prior_adapter"
    assert trainer.requests[1].prior_parametric_memory_artifact_id == prior.artifact_id
    assert second.manifest["component_count"] == 2
    assert second.manifest["continual_task_index"] == 1
    assert second.manifest["effective_rank"] == 8
    assert second.manifest["routing_mode"] == "single_cumulative_adapter"
    assert second.lineage["prior_parametric_memory_artifact_id"] == prior.artifact_id
    assert Path(second.uri.removeprefix("file://")).is_dir()
    for training_request in trainer.requests:
        work_dir = Path(training_request.work_dir)
        assert sorted(path.name for path in work_dir.iterdir()) == ["adapter"]


def test_sd_lora_requires_daemon_trainer_and_exact_dataset_receipts(
    tmp_path: Path,
) -> None:
    artifact_root = tmp_path / "artifacts"
    artifact_root.mkdir(mode=0o700)
    dataset = _dataset(artifact_root, "missing-service")
    with pytest.raises(ValueError, match="Daemon trainer service"):
        parametric_memory_sd_lora(_context(artifact_root, None, dataset))

    payload = dataset.model_dump(mode="python")
    payload.update(
        manifest_sha256=None,
        records_byte_size=None,
        records_sha256=None,
    )
    unverified = WorkerClaimInputArtifact.model_validate(payload)
    with pytest.raises(ValueError, match="exact claimed dataset file receipts"):
        parametric_memory_sd_lora(
            _context(artifact_root, _FakeTrainer(), unverified, generation=1)
        )


def test_sd_lora_preserves_generic_tool_calls_from_codex_style_trajectories(
    tmp_path: Path,
) -> None:
    artifact_root = tmp_path / "artifacts"
    artifact_root.mkdir(mode=0o700)
    trainer = _FakeTrainer()

    parametric_memory_sd_lora(
        _context(
            artifact_root,
            trainer,
            _dataset(artifact_root, "tool-call", tool_call_trace=True),
        )
    )

    request = trainer.requests[0]
    [record] = trainer.training_records[0]
    assert not (Path(request.work_dir) / request.training_data_path).exists()
    assert record["messages"][1]["tool_calls"][0]["function"] == {
        "arguments": {"task_id": "task-tool"},
        "name": "read_task",
    }
    assert record["messages"][2]["tool_call_id"] == "call-read"
    assert record["messages"][-1]["tool_calls"][0]["function"] == {
        "arguments": {"command": "pytest -q"},
        "name": "run_command",
    }
    assert [tool["function"]["name"] for tool in record["tools"]] == [
        "read_task",
        "run_command",
    ]


def test_sd_lora_rejects_trainer_state_weight_tampering(tmp_path: Path) -> None:
    artifact_root = tmp_path / "artifacts"
    artifact_root.mkdir(mode=0o700)

    trainer = _TamperedStateTrainer()
    with pytest.raises(ValueError, match="state weights do not match"):
        parametric_memory_sd_lora(
            _context(
                artifact_root,
                trainer,
                _dataset(artifact_root, "tampered"),
            )
        )
    request = trainer.requests[0]
    assert not (Path(request.work_dir) / request.training_data_path).exists()


def test_sd_lora_crosses_plan_store_worker_and_artifact_publication(
    tmp_path: Path,
) -> None:
    artifact_root = tmp_path / "artifacts"
    store = EvolutionStore(
        db_path=tmp_path / "evolution.db",
        artifact_root=artifact_root,
    )
    store.initialize()
    store.ingest_event(
        EventIngestRequest(
            source="openevo",
            event_type="openevo.session_completed",
            source_event_id="session:sd-lora-plan-bound",
            task_id="task-sd-lora-plan-bound",
            session_id="session-sd-lora-plan-bound",
            status="COMPLETED",
            payload={
                "session_result": {
                    "trajectory": {
                        "traces": [
                            {
                                "prompt_messages": [
                                    {"role": "user", "content": "Repair the task."}
                                ],
                                "response_messages": [
                                    {"role": "assistant", "content": "Run focused tests."}
                                ],
                            }
                        ]
                    }
                }
            },
        )
    )
    dataset = store.create_dataset(
        DatasetCreateRequest(
            idempotency_key="sd-lora-plan-bound-dataset",
            name="SD-LoRA plan-bound dataset",
            purpose="parametric_memory_training",
            query={
                "event_types": ["openevo.session_completed"],
                "status": ["COMPLETED"],
            },
            limits={"max_events": 1, "max_traces": 1},
        )
    )
    snapshot = build_builtin_registry(
        ImplementationDistributionIdentity(
            distribution="openevo-test",
            distribution_version="1.0.0",
            distribution_digest="d" * 64,
        )
    )
    handles = dict(methods_module.METHOD_REGISTRY)
    handles["parametric_memory_sd_lora"] = parametric_memory_sd_lora
    registry = _ExecutableRegistry(snapshot, handles)
    plan = snapshot.compile_plan(
        plan_id="plan-sd-lora-plan-bound",
        selections=(
            EvolutionTargetSelection(
                target_id="parametric_memory",
                enabled=True,
                method_id="parametric_memory_sd_lora",
                config={
                    "base_model": "Qwen/Qwen3-0.6B",
                    "model_revision": _MODEL_REVISION,
                    "rank": 4,
                    "max_records": 8,
                },
            ),
        ),
        profile=EvolutionExecutionProfile(
            execution_mode="self_deployed",
            capture_mode="transcript",
            harness_id="codex",
            runtime_capabilities=(
                "adapter_serving",
                "gpu",
                "sd_lora_continual_trainer",
            ),
        ),
    )
    request = PlanBoundJobCreateRequest(
        plan=plan,
        target_id="parametric_memory",
        job_type="openevo:test:sd-lora-plan-bound",
        input_bindings=(
            PlannedInputBinding(
                binding_id="current_dataset",
                artifact_ids=(dataset.artifact_id,),
            ),
            PlannedInputBinding(
                binding_id="prior_target_artifacts",
                artifact_ids=(),
            ),
        ),
        core_config={
            "name": "sd-lora-plan-bound",
            "promoted": True,
            "lineage": {"input_artifact_ids": [dataset.artifact_id]},
            "compatibility": {"agent_harnesses": ["codex"]},
        },
    )
    created = store.create_plan_bound_job(request, snapshot=snapshot)
    trainer = _FakeTrainer()
    client = _StoreWorkerClient(store)

    assert run_once(
        client,
        worker_id="sd-lora-plan-bound-worker",
        capabilities=[request.job_type],
        artifact_root=artifact_root,
        executable_registry=registry,
        method_services=MethodExecutionServices(
            harness=_Harness(),
            parametric_trainer=trainer,
        ),
    )

    assert client.failed == []
    assert len(trainer.requests) == 1
    with store.connect() as connection:
        job_row = connection.execute(
            "SELECT state FROM jobs WHERE job_id = ?",
            (created.job_id,),
        ).fetchone()
        artifact_row = connection.execute(
            "SELECT state, staging_job_id, uri, manifest_json, lineage_json "
            "FROM artifacts WHERE type = ? ORDER BY created_at DESC LIMIT 1",
            (ArtifactType.PARAMETRIC_MEMORY.value,),
        ).fetchone()
    assert job_row["state"] == "succeeded"
    assert artifact_row["state"] == "active"
    assert artifact_row["staging_job_id"] is None
    manifest = json.loads(artifact_row["manifest_json"])
    assert manifest["routing_mode"] == "single_cumulative_adapter"
    assert manifest["component_count"] == 1
    execution = json.loads(artifact_row["lineage_json"])["openevo_execution"]
    assert execution["job_id"] == created.job_id
    assert execution["method_id"] == "parametric_memory_sd_lora"
    output_dir = Path(artifact_row["uri"].removeprefix("file://"))
    assert (output_dir / "adapter_model.safetensors").is_file()
    assert (output_dir / SD_LORA_STATE_MANIFEST).is_file()
