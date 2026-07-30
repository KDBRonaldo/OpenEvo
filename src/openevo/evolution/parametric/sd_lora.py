"""SD-LoRA continual-learning method for causal-language-model adapters."""

from __future__ import annotations

import hashlib
import math
import os
import shutil
from pathlib import Path, PurePosixPath
from typing import Any

from openevo.evolution.artifact_payloads import ArtifactPayloadService
from openevo.evolution.framework.contracts import canonical_json, validate_relative_path
from openevo.evolution.framework.execution import MethodExecutionContext
from openevo.evolution.methods import _read_dataset_artifact
from openevo.evolution.models import (
    ArtifactRegisterRequest,
    ArtifactType,
    WorkerClaimInputArtifact,
)

from .contracts import (
    SD_LORA_STATE_MANIFEST,
    SD_LORA_STATE_WEIGHTS,
    SdLoraMethodConfig,
    SdLoraStateManifest,
    SdLoraTrainingRequest,
    SdLoraTrainingResult,
)
from .training_data import (
    MAX_TRAINING_FILE_BYTES,
    MAX_TRAINING_LINE_BYTES,
    normalize_chat_messages,
    normalize_tool_definitions,
)


_SUCCESS_STATUSES = frozenset(
    {
        "completed",
        "pass",
        "passed",
        "success",
        "succeeded",
    }
)
_MAX_STATE_MANIFEST_BYTES = 1024 * 1024
_REQUIRED_ADAPTER_FILES = frozenset(
    {
        "adapter_config.json",
        "adapter_model.safetensors",
        SD_LORA_STATE_MANIFEST,
        SD_LORA_STATE_WEIGHTS,
    }
)


def _binding_artifacts(
    context: MethodExecutionContext,
    binding_id: str,
    artifact_type: ArtifactType,
) -> tuple[WorkerClaimInputArtifact, ...]:
    binding = next(
        (item for item in context.envelope.input_bindings if item.binding_id == binding_id),
        None,
    )
    if binding is None:
        raise ValueError(f"SD-LoRA execution envelope is missing {binding_id!r}")
    by_id = {artifact.artifact_id: artifact for artifact in context.job.input_artifacts}
    try:
        artifacts = tuple(by_id[artifact_id] for artifact_id in binding.artifact_ids)
    except KeyError as exc:
        raise ValueError("SD-LoRA binding references an unavailable artifact") from exc
    if any(str(artifact.type) != artifact_type.value for artifact in artifacts):
        raise ValueError(f"SD-LoRA {binding_id!r} binding has the wrong artifact type")
    return artifacts


def _successful_record(record: dict[str, Any], minimum_reward: float) -> bool:
    reward = record.get("reward")
    if isinstance(reward, (int, float)) and not isinstance(reward, bool):
        numeric_reward = float(reward)
        return math.isfinite(numeric_reward) and numeric_reward >= minimum_reward
    status = record.get("status")
    return isinstance(status, str) and status.strip().lower() in _SUCCESS_STATUSES


def _training_examples(
    datasets: tuple[WorkerClaimInputArtifact, ...],
    *,
    maximum: int,
    minimum_reward: float,
) -> list[dict[str, Any]]:
    examples: list[dict[str, Any]] = []
    for dataset in datasets:
        if (
            dataset.manifest_sha256 is None
            or dataset.records_byte_size is None
            or dataset.records_sha256 is None
        ):
            raise ValueError("SD-LoRA requires exact claimed dataset file receipts")
        manifest, records = _read_dataset_artifact(dataset)
        for record in records:
            if not _successful_record(record, minimum_reward):
                continue
            traces = record.get("traces")
            if not isinstance(traces, list):
                continue
            for trace_index, trace in enumerate(traces):
                if not isinstance(trace, dict):
                    continue
                try:
                    prompt_messages = normalize_chat_messages(trace.get("prompt_messages"))
                    response_messages = normalize_chat_messages(trace.get("response_messages"))
                    tools = (
                        normalize_tool_definitions(trace["tools"])
                        if trace.get("tools") is not None
                        else None
                    )
                except ValueError:
                    continue
                if not prompt_messages or not any(
                    message["role"] == "assistant"
                    and (bool(message["content"]) or bool(message.get("tool_calls")))
                    for message in response_messages
                ):
                    continue
                example: dict[str, Any] = {
                    "messages": [*prompt_messages, *response_messages],
                    "metadata": {
                        "dataset_artifact_id": dataset.artifact_id,
                        "dataset_name": manifest.get("name") or dataset.name,
                        "event_id": record.get("event_id"),
                        "reward": record.get("reward"),
                        "session_id": record.get("session_id"),
                        "status": record.get("status"),
                        "task_id": record.get("task_id"),
                        "trace_index": trace_index,
                    },
                }
                if tools is not None:
                    example["tools"] = tools
                examples.append(example)
                if len(examples) >= maximum:
                    return examples
    return examples


def _write_private_jsonl(path: Path, records: list[dict[str, Any]]) -> None:
    fd = os.open(
        path,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | os.O_NOFOLLOW,
        0o600,
    )
    try:
        total_bytes = 0
        for record in records:
            line = (canonical_json(record) + "\n").encode("utf-8")
            if len(line) > MAX_TRAINING_LINE_BYTES:
                raise ValueError("SD-LoRA training example exceeds the line budget")
            total_bytes += len(line)
            if total_bytes > MAX_TRAINING_FILE_BYTES:
                raise ValueError("SD-LoRA training dataset exceeds the file budget")
            pending = memoryview(line)
            while pending:
                written = os.write(fd, pending)
                if written <= 0:
                    raise OSError("SD-LoRA training dataset write made no progress")
                pending = pending[written:]
        os.fsync(fd)
    finally:
        os.close(fd)


def _copy_prior_adapter(
    context: MethodExecutionContext,
    artifact: WorkerClaimInputArtifact,
    destination: Path,
) -> None:
    with ArtifactPayloadService(context.artifact_root) as payloads:
        snapshot = payloads.issue_snapshot(
            artifact_id=artifact.artifact_id,
            artifact_type=ArtifactType.PARAMETRIC_MEMORY.value,
            name=artifact.name or artifact.artifact_id,
            uri=artifact.uri,
            manifest={},
            scores={},
            rank_index=0,
        )
        entry_paths = {entry.relative_path for entry in snapshot.payload_entries}
        missing = sorted(_REQUIRED_ADAPTER_FILES.difference(entry_paths))
        if missing:
            raise ValueError(
                "prior parametric memory is not an SD-LoRA state artifact; missing "
                + ", ".join(missing)
            )
        destination.mkdir(mode=0o700)
        for entry in snapshot.payload_entries:
            relative_path = validate_relative_path(entry.relative_path)
            target = destination.joinpath(*PurePosixPath(relative_path).parts)
            target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            os.chmod(target.parent, 0o700, follow_symlinks=False)
            fd = os.open(
                target,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | os.O_NOFOLLOW,
                0o600,
            )
            try:
                receipt = payloads.copy_verified_file(
                    snapshot.payload_handle,
                    relative_path,
                    fd,
                )
                if receipt.size_bytes != entry.size_bytes or receipt.sha256 != entry.sha256:
                    raise ValueError("prior SD-LoRA adapter copy receipt does not match")
                os.fsync(fd)
            finally:
                os.close(fd)
        payloads.verify_payload_content(snapshot.payload_handle)


def _discard_consumed_training_inputs(
    work_dir: Path,
    training_path: Path,
    prior_path: Path | None,
) -> None:
    directory_fd = os.open(
        work_dir,
        os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_DIRECTORY,
    )
    try:
        os.unlink(training_path.name, dir_fd=directory_fd)
        if prior_path is not None:
            if not shutil.rmtree.avoids_symlink_attacks:
                raise RuntimeError("safe recursive cleanup is unavailable")
            shutil.rmtree(prior_path.name, dir_fd=directory_fd)
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)


def _discard_failed_work_dir(work_dir: Path) -> None:
    if not shutil.rmtree.avoids_symlink_attacks:
        raise RuntimeError("safe recursive cleanup is unavailable")
    parent_fd = os.open(
        work_dir.parent,
        os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_DIRECTORY,
    )
    try:
        try:
            shutil.rmtree(work_dir.name, dir_fd=parent_fd)
        except FileNotFoundError:
            pass
        os.fsync(parent_fd)
    finally:
        os.close(parent_fd)


def _private_work_dir(artifact_root: Path, job_id: str, lease_id: str) -> Path:
    root = Path(os.path.abspath(artifact_root))
    if not root.is_dir():
        raise ValueError("SD-LoRA artifact root does not exist")
    workers = root / "workers"
    workers.mkdir(mode=0o700, exist_ok=True)
    os.chmod(workers, 0o700, follow_symlinks=False)
    identity = hashlib.sha256(f"{job_id}\0{lease_id}".encode("utf-8")).hexdigest()[:32]
    work_dir = workers / f"sd-lora-{identity}"
    work_dir.mkdir(mode=0o700)
    return work_dir


def _validated_output_state(
    context: MethodExecutionContext,
    *,
    adapter_dir: Path,
    request: SdLoraTrainingRequest,
    result: SdLoraTrainingResult,
) -> SdLoraStateManifest:
    with ArtifactPayloadService(context.artifact_root) as payloads:
        snapshot = payloads.issue_snapshot(
            artifact_id=request.request_id,
            artifact_type=ArtifactType.PARAMETRIC_MEMORY.value,
            name=request.adapter_id,
            uri=adapter_dir.as_uri(),
            manifest={},
            scores={},
            rank_index=0,
        )
        entries = {entry.relative_path: entry for entry in snapshot.payload_entries}
        missing = sorted(_REQUIRED_ADAPTER_FILES.difference(entries))
        if missing:
            raise ValueError("SD-LoRA trainer output is incomplete; missing " + ", ".join(missing))
        state_entry = entries[SD_LORA_STATE_MANIFEST]
        if state_entry.size_bytes > _MAX_STATE_MANIFEST_BYTES:
            raise ValueError("SD-LoRA state manifest exceeds its byte budget")
        state_text = payloads.read_utf8_prefix(
            snapshot.payload_handle,
            SD_LORA_STATE_MANIFEST,
            max_chars=_MAX_STATE_MANIFEST_BYTES,
            max_bytes=_MAX_STATE_MANIFEST_BYTES,
        )
        try:
            state = SdLoraStateManifest.model_validate_json(state_text)
        except ValueError as exc:
            raise ValueError("SD-LoRA state manifest is invalid") from exc
        expected_state_bytes = (canonical_json(state.model_dump(mode="json")) + "\n").encode(
            "utf-8"
        )
        if state_text.encode("utf-8") != expected_state_bytes:
            raise ValueError("SD-LoRA state manifest bytes are not canonical")
        weights_entry = entries[SD_LORA_STATE_WEIGHTS]
        if (
            weights_entry.size_bytes != state.state_weights_size_bytes
            or weights_entry.sha256 != state.state_weights_sha256
        ):
            raise ValueError("SD-LoRA state weights do not match their manifest")
        payloads.verify_payload_content(snapshot.payload_handle)

    expected_values = {
        "adapter_id": request.adapter_id,
        "base_model": request.config.base_model,
        "model_revision": request.config.model_revision,
        "task_index": result.task_index,
        "component_count": result.component_count,
        "effective_rank": result.effective_rank,
        "target_module_suffixes": request.config.target_modules,
        "training_record_count": result.training_record_count,
        "steps_completed": result.steps_completed,
        "training_time_seconds": result.training_time_seconds,
        "gpu_peak_memory_bytes": result.gpu_peak_memory_bytes,
        "source_dataset_artifact_ids": request.source_dataset_artifact_ids,
        "prior_parametric_memory_artifact_id": (request.prior_parametric_memory_artifact_id),
    }
    if any(getattr(state, key) != value for key, value in expected_values.items()):
        raise ValueError("SD-LoRA state manifest does not match the trainer result")
    if tuple(module.name for module in state.modules) != result.target_module_names:
        raise ValueError("SD-LoRA state module inventory does not match the trainer result")
    if tuple(component.coefficient for component in state.components) != result.coefficients:
        raise ValueError("SD-LoRA state coefficients do not match the trainer result")
    if state.training_loss != result.training_loss:
        raise ValueError("SD-LoRA state loss does not match the trainer result")
    return state


def _train_and_validate_output(
    context: MethodExecutionContext,
    trainer: Any,
    request: SdLoraTrainingRequest,
) -> tuple[SdLoraTrainingResult, Path]:
    result = SdLoraTrainingResult.model_validate(
        trainer.train_sd_lora(
            request,
            cancellation=context.services.cancellation,
        )
    )
    if result.request_id != request.request_id:
        raise ValueError("SD-LoRA trainer returned a result for a different request")
    if result.training_record_count != request.training_record_count:
        raise ValueError("SD-LoRA trainer changed the claimed training record count")
    if result.adapter_path != request.output_adapter_path or (
        result.state_manifest_path != f"{request.output_adapter_path}/{SD_LORA_STATE_MANIFEST}"
        or result.state_weights_path != f"{request.output_adapter_path}/{SD_LORA_STATE_WEIGHTS}"
    ):
        raise ValueError("SD-LoRA trainer returned unexpected output paths")
    adapter_dir = Path(request.work_dir) / result.adapter_path
    _validated_output_state(
        context,
        adapter_dir=adapter_dir,
        request=request,
        result=result,
    )
    return result, adapter_dir


def _core_mapping(context: MethodExecutionContext, key: str) -> dict[str, Any]:
    value = context.envelope.core_config().get(key)
    return dict(value) if isinstance(value, dict) else {}


def _core_strings(context: MethodExecutionContext, key: str) -> list[str]:
    value = context.envelope.core_config().get(key)
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, str) and item.strip()]


def parametric_memory_sd_lora(
    context: MethodExecutionContext,
) -> list[ArtifactRegisterRequest]:
    """Train one SD-LoRA component and publish one cumulative adapter."""

    trainer = context.services.parametric_trainer
    if trainer is None:
        raise ValueError("parametric_memory_sd_lora requires the Daemon trainer service")
    config = SdLoraMethodConfig.model_validate(context.envelope.user_config())
    datasets = _binding_artifacts(context, "current_dataset", ArtifactType.DATASET)
    if len(datasets) != 1:
        raise ValueError("parametric_memory_sd_lora requires exactly one current dataset")
    prior_artifacts = _binding_artifacts(
        context,
        "prior_target_artifacts",
        ArtifactType.PARAMETRIC_MEMORY,
    )
    if len(prior_artifacts) > 1:
        raise ValueError("parametric_memory_sd_lora accepts one cumulative prior adapter")

    examples = _training_examples(
        datasets,
        maximum=config.max_records,
        minimum_reward=config.minimum_reward,
    )
    if not examples:
        raise ValueError("parametric_memory_sd_lora found no successful training traces")

    work_dir = _private_work_dir(
        context.artifact_root,
        context.job.job_id,
        context.job.lease_id,
    )
    training_path = work_dir / "training.jsonl"
    prior_path: Path | None = None
    try:
        _write_private_jsonl(training_path, examples)
        prior_artifact = prior_artifacts[0] if prior_artifacts else None
        if prior_artifact is not None:
            prior_path = work_dir / "prior_adapter"
            _copy_prior_adapter(context, prior_artifact, prior_path)
    except Exception:
        _discard_failed_work_dir(work_dir)
        raise

    identity = hashlib.sha256(
        (
            context.job.job_id
            + "\0"
            + context.envelope.method_identity_digest
            + "\0"
            + datasets[0].artifact_id
        ).encode("utf-8")
    ).hexdigest()[:24]
    adapter_id = f"sd-lora-{identity}"
    request = SdLoraTrainingRequest(
        request_id=f"sd-lora-{identity}",
        work_dir=str(work_dir),
        training_data_path=training_path.relative_to(work_dir).as_posix(),
        prior_adapter_path=(
            prior_path.relative_to(work_dir).as_posix() if prior_path is not None else None
        ),
        output_adapter_path="adapter",
        adapter_id=adapter_id,
        source_dataset_artifact_ids=tuple(artifact.artifact_id for artifact in datasets),
        prior_parametric_memory_artifact_id=(
            prior_artifact.artifact_id if prior_artifact is not None else None
        ),
        training_record_count=len(examples),
        config=config,
    )
    try:
        try:
            result, adapter_dir = _train_and_validate_output(context, trainer, request)
        finally:
            _discard_consumed_training_inputs(work_dir, training_path, prior_path)
    except Exception:
        _discard_failed_work_dir(work_dir)
        raise

    compatibility = _core_mapping(context, "compatibility")
    configured_models = compatibility.get("base_model")
    if configured_models is not None:
        if not isinstance(configured_models, list) or config.base_model not in configured_models:
            raise ValueError("parametric memory compatibility must include its base model")
    compatibility["base_model"] = [config.base_model]
    scores = {
        key: float(value)
        for key, value in _core_mapping(context, "scores").items()
        if isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(float(value))
    }
    scores["training_loss"] = result.training_loss
    scores.setdefault("quality", 1.0 / (1.0 + max(0.0, result.training_loss)))
    tags = list(dict.fromkeys([*_core_strings(context, "tags"), "continual-learning", "sd-lora"]))
    lineage = {
        **_core_mapping(context, "lineage"),
        "method": "parametric_memory_sd_lora",
        "input_artifact_ids": [artifact.artifact_id for artifact in context.job.input_artifacts],
        "source_dataset_artifact_ids": [artifact.artifact_id for artifact in datasets],
        "prior_parametric_memory_artifact_id": (
            prior_artifact.artifact_id if prior_artifact is not None else None
        ),
    }
    manifest = {
        "method": "parametric_memory_sd_lora",
        "algorithm_family": "SD-LoRA",
        "adaptation_scope": "causal_lm_continual_sft_v1",
        "paper_equivalent": False,
        "upstream_repository": "https://github.com/WuYichen-97/SD-Lora-CL",
        "upstream_revision": "8bacded6eb44786db071f66fb90a87dd660d94ea",
        "adapter_id": adapter_id,
        "adapter_format": "lora",
        "serialization_format": "peft_safetensors",
        "base_model": config.base_model,
        "model_revision": config.model_revision,
        "routing_mode": "single_cumulative_adapter",
        "continual_task_index": result.task_index,
        "component_count": result.component_count,
        "component_rank": config.rank,
        "effective_rank": result.effective_rank,
        "coefficients": list(result.coefficients),
        "target_module_names": list(result.target_module_names),
        "training_record_count": len(examples),
        "steps_completed": result.steps_completed,
        "training_loss": result.training_loss,
        "training_time_seconds": result.training_time_seconds,
        "gpu_peak_memory_bytes": result.gpu_peak_memory_bytes,
        "quality_metric": "inverse_training_loss_proxy_not_heldout",
        "source_dataset_artifact_ids": [artifact.artifact_id for artifact in datasets],
        "prior_parametric_memory_artifact_id": (
            prior_artifact.artifact_id if prior_artifact is not None else None
        ),
        "state_manifest_path": SD_LORA_STATE_MANIFEST,
        "state_weights_path": SD_LORA_STATE_WEIGHTS,
        "training_config": config.model_dump(mode="json"),
    }
    core_config = context.envelope.core_config()
    try:
        artifact = ArtifactRegisterRequest(
            type=ArtifactType.PARAMETRIC_MEMORY,
            name=str(core_config.get("name") or adapter_id),
            uri=adapter_dir.resolve().as_uri(),
            manifest=manifest,
            lineage=lineage,
            compatibility=compatibility,
            scores=scores,
            tags=tags,
            promoted=bool(core_config.get("promoted", False)),
        )
    except Exception:
        _discard_failed_work_dir(work_dir)
        raise
    return [artifact]


__all__ = ["parametric_memory_sd_lora"]
