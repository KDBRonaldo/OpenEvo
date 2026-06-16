from __future__ import annotations

import json
import re
from collections.abc import Callable
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse

from polar_evolution.agent_system import normalize_agent_system_target_path
from polar_evolution.models import (
    ArtifactRegisterRequest,
    ArtifactType,
    WorkerClaimInputArtifact,
    WorkerClaimedJob,
)

EvolutionMethod = Callable[[WorkerClaimedJob, Path], list[ArtifactRegisterRequest]]


class UnknownEvolutionMethodError(ValueError):
    """Raised when a worker claims a job whose method is not registered locally."""


def run_method(job: WorkerClaimedJob, *, artifact_root: Path) -> list[ArtifactRegisterRequest]:
    method = METHOD_REGISTRY.get(job.method)
    if method is None:
        raise UnknownEvolutionMethodError(f"Unknown evolution method: {job.method}")
    return method(job, artifact_root)


def text_memory(job: WorkerClaimedJob, artifact_root: Path) -> list[ArtifactRegisterRequest]:
    dataset = _first_input_artifact(job, ArtifactType.DATASET)
    if dataset is None:
        raise ValueError("text_memory requires an input dataset artifact")

    manifest_path = _file_uri_to_path(dataset.uri)
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    records = _read_dataset_records(manifest_path, manifest)

    output_dir = artifact_root / "workers" / job.job_id / "text_memory"
    output_dir.mkdir(parents=True, exist_ok=True)
    memory_path = output_dir / "memory.md"
    memory_path.write_text(
        _render_memory_markdown(job, dataset, manifest, records), encoding="utf-8"
    )

    return [
        ArtifactRegisterRequest(
            type=ArtifactType.TEXT_MEMORY,
            name=str(job.config.get("name") or f"{dataset.name or dataset.artifact_id} memory"),
            uri=memory_path.resolve().as_uri(),
            manifest={
                "content_path": "memory.md",
                "source_dataset_artifact_id": dataset.artifact_id,
                "source_dataset_uri": dataset.uri,
                "record_count": len(records),
            },
            lineage={"input_artifact_ids": [dataset.artifact_id]},
            tags=_string_list(job.config.get("tags")),
            promoted=bool(job.config.get("promoted", False)),
        )
    ]


def skill_bundle(job: WorkerClaimedJob, artifact_root: Path) -> list[ArtifactRegisterRequest]:
    name = str(job.config.get("name") or f"{job.job_id}-skill")
    output_dir = artifact_root / "workers" / job.job_id / "skill_bundle" / _slug(name)
    output_dir.mkdir(parents=True, exist_ok=True)

    markdown = job.config.get("skill_markdown") or job.config.get("content")
    if not markdown:
        markdown = (
            f"# {name}\n\n"
            "Use this reference skill as a lightweight starting point for future tasks.\n"
        )
    skill_path = output_dir / "SKILL.md"
    skill_path.write_text(_ensure_trailing_newline(str(markdown)), encoding="utf-8")

    return [
        ArtifactRegisterRequest(
            type=ArtifactType.SKILL_BUNDLE,
            name=name,
            uri=output_dir.resolve().as_uri(),
            manifest={"entrypoint": "SKILL.md", "files": ["SKILL.md"]},
            tags=_string_list(job.config.get("tags")),
            promoted=bool(job.config.get("promoted", False)),
        )
    ]


def agent_system(job: WorkerClaimedJob, artifact_root: Path) -> list[ArtifactRegisterRequest]:
    name = str(job.config.get("name") or f"{job.job_id}-agent-system")
    target_path = Path(normalize_agent_system_target_path(job.config.get("target_path")))
    output_dir = artifact_root / "workers" / job.job_id / "agent_system"
    output_path = output_dir / target_path
    output_path.parent.mkdir(parents=True, exist_ok=True)

    markdown = job.config.get("agent_system_markdown") or job.config.get("content")
    if not markdown:
        markdown = (
            "# Evolved Agent System\n\n"
            "Follow repository-local conventions and preserve task-specific learnings.\n"
        )
    output_path.write_text(_ensure_trailing_newline(str(markdown)), encoding="utf-8")

    return [
        ArtifactRegisterRequest(
            type=ArtifactType.AGENT_SYSTEM,
            name=name,
            uri=output_path.resolve().as_uri(),
            manifest={
                "content_path": target_path.as_posix(),
                "target_path": target_path.as_posix(),
            },
            lineage=_dict_config(job.config.get("lineage")),
            compatibility=_dict_config(job.config.get("compatibility")),
            scores=_scores_config(job.config.get("scores")),
            tags=_string_list(job.config.get("tags")),
            promoted=bool(job.config.get("promoted", False)),
        )
    ]


def parametric_memory_register(
    job: WorkerClaimedJob,
    artifact_root: Path,
) -> list[ArtifactRegisterRequest]:
    del artifact_root
    adapter_uri = job.config.get("adapter_uri") or job.config.get("uri")
    if not adapter_uri:
        raise ValueError("parametric_memory_register requires adapter_uri or uri")
    if not isinstance(adapter_uri, str):
        raise ValueError("parametric_memory_register adapter_uri must be a string")

    if adapter_uri.startswith("file://"):
        adapter_path = _file_uri_to_path(adapter_uri)
        if not adapter_path.exists():
            raise ValueError(f"Adapter URI does not exist: {adapter_uri}")

    base_model = _find_base_model(job)
    if not base_model:
        raise ValueError("parametric_memory_register requires base_model")

    config_manifest = job.config.get("manifest")
    manifest_adapter_format = None
    if isinstance(config_manifest, dict):
        manifest_adapter_format = config_manifest.get("adapter_format")
    adapter_format = str(job.config.get("adapter_format") or manifest_adapter_format or "lora")
    adapter_id = _adapter_id(job, adapter_uri)
    manifest = {
        "adapter_id": adapter_id,
        "base_model": base_model,
        "adapter_format": adapter_format,
    }
    if isinstance(config_manifest, dict):
        manifest.update(config_manifest)
        manifest["adapter_id"] = adapter_id
        manifest["base_model"] = base_model
        manifest["adapter_format"] = adapter_format

    return [
        ArtifactRegisterRequest(
            type=ArtifactType.PARAMETRIC_MEMORY,
            name=str(job.config.get("name") or adapter_id),
            uri=adapter_uri,
            manifest=manifest,
            tags=_string_list(job.config.get("tags")),
            promoted=bool(job.config.get("promoted", False)),
        )
    ]


METHOD_REGISTRY: dict[str, EvolutionMethod] = {
    "text_memory": text_memory,
    "skill_bundle": skill_bundle,
    "agent_system": agent_system,
    "parametric_memory_register": parametric_memory_register,
}


def _first_input_artifact(
    job: WorkerClaimedJob,
    artifact_type: ArtifactType,
) -> WorkerClaimInputArtifact | None:
    for artifact in job.input_artifacts:
        if artifact.type == artifact_type or artifact.type == artifact_type.value:
            return artifact
    return None


def _read_dataset_records(manifest_path: Path, manifest: dict[str, Any]) -> list[dict[str, Any]]:
    records_uri = manifest.get("records_uri")
    if isinstance(records_uri, str) and records_uri.startswith("file://"):
        records_path = _file_uri_to_path(records_uri)
    else:
        records_path_value = manifest.get("records_path") or "records.jsonl"
        records_path = manifest_path.parent / str(records_path_value)

    records: list[dict[str, Any]] = []
    for line in records_path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            record = json.loads(line)
            if isinstance(record, dict):
                records.append(record)
    return records


def _render_memory_markdown(
    job: WorkerClaimedJob,
    dataset: WorkerClaimInputArtifact,
    manifest: dict[str, Any],
    records: list[dict[str, Any]],
) -> str:
    lines = [
        f"# Memory from {dataset.name or dataset.artifact_id}",
        "",
        f"- job_id: {job.job_id}",
        f"- dataset_artifact_id: {dataset.artifact_id}",
        f"- dataset_name: {manifest.get('name') or dataset.name or 'unknown'}",
        f"- record_count: {len(records)}",
        "",
        "## Records",
    ]
    for record in records[: int(job.config.get("max_records", 20))]:
        task_id = record.get("task_id") or "unknown_task"
        session_id = record.get("session_id") or "unknown_session"
        status = record.get("status") or "unknown_status"
        reward = record.get("reward")
        summary = _record_summary(record)
        lines.append(f"- task={task_id} session={session_id} status={status} reward={reward}")
        if summary:
            lines.append(f"  - summary: {summary}")
    lines.append("")
    return "\n".join(lines)


def _record_summary(record: dict[str, Any]) -> str:
    payload = record.get("payload")
    if isinstance(payload, dict):
        for key in ("summary", "result", "message"):
            value = payload.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
        session_result = payload.get("session_result")
        if isinstance(session_result, dict):
            for key in ("summary", "status", "message"):
                value = session_result.get(key)
                if isinstance(value, str) and value.strip():
                    return value.strip()
    return ""


def _find_base_model(job: WorkerClaimedJob) -> str | None:
    value = job.config.get("base_model")
    if isinstance(value, str) and value.strip():
        return value
    manifest = job.config.get("manifest")
    if isinstance(manifest, dict):
        value = manifest.get("base_model")
        if isinstance(value, str) and value.strip():
            return value
    context = job.config.get("context")
    if isinstance(context, dict):
        value = context.get("base_model")
        if isinstance(value, str) and value.strip():
            return value
    return None


def _adapter_id(job: WorkerClaimedJob, adapter_uri: str) -> str:
    value = job.config.get("adapter_id")
    if isinstance(value, str) and value.strip():
        return _slug(value)
    manifest = job.config.get("manifest")
    if isinstance(manifest, dict):
        value = manifest.get("adapter_id")
        if isinstance(value, str) and value.strip():
            return _slug(value)
    parsed = urlparse(adapter_uri)
    candidate = Path(unquote(parsed.path)).name if parsed.scheme == "file" else adapter_uri
    return _slug(candidate) or "adapter"


def _file_uri_to_path(uri: str) -> Path:
    parsed = urlparse(uri)
    if parsed.scheme != "file":
        raise ValueError(f"Expected file:// URI, got: {uri}")
    return Path(unquote(parsed.path)).resolve()


def _slug(value: str) -> str:
    slug = re.sub(r"[^A-Za-z0-9_.-]+", "-", value.strip()).strip("-")
    return slug or "skill"


def _ensure_trailing_newline(value: str) -> str:
    return value if value.endswith("\n") else value + "\n"


def _dict_config(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return dict(value)
    return {}


def _scores_config(value: Any) -> dict[str, float]:
    if not isinstance(value, dict):
        return {}
    return {str(key): float(score) for key, score in value.items()}


def _string_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        return [str(item) for item in value]
    return []
