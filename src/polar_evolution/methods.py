from __future__ import annotations

import json
import os
import re
import subprocess
import tempfile
from collections.abc import Callable
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse

import httpx

from polar_evolution.agent_system import normalize_agent_system_target_path
from polar_evolution.models import (
    ArtifactRegisterRequest,
    ArtifactType,
    WorkerClaimInputArtifact,
    WorkerClaimedJob,
)

EvolutionMethod = Callable[[WorkerClaimedJob, Path], list[ArtifactRegisterRequest]]

_REFLECTOR_PROVIDER_OPENAI_CHAT = "openai_chat"
_REFLECTOR_PROVIDER_CODEX_CLI = "codex_cli"
_REFLECTOR_PROXY_ENV_VARS = (
    "OPENAI_BASE_URL",
    "OPENAI_API_KEY",
    "ANTHROPIC_BASE_URL",
    "ANTHROPIC_API_KEY",
    "GOOGLE_API_URL",
    "GOOGLE_API_KEY",
    "GEMINI_API_KEY",
    "GOOGLE_GEMINI_BASE_URL",
)


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


def agent_system_reflector(
    job: WorkerClaimedJob,
    artifact_root: Path,
) -> list[ArtifactRegisterRequest]:
    dataset = _first_input_artifact(job, ArtifactType.DATASET)
    if dataset is None:
        raise ValueError("agent_system_reflector requires an input dataset artifact")

    manifest_path = _file_uri_to_path(dataset.uri)
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    records = _read_dataset_records(manifest_path, manifest)
    reflected_records = _reflection_records(
        records,
        max_records=_int_config(job.config.get("max_records"), 20),
    )

    name = str(job.config.get("name") or f"{dataset.name or dataset.artifact_id} reflector")
    target_path = Path(
        normalize_agent_system_target_path(job.config.get("target_path") or "agents.md")
    )
    output_dir = artifact_root / "workers" / job.job_id / "agent_system_reflector"
    output_path = output_dir / target_path
    output_path.parent.mkdir(parents=True, exist_ok=True)

    base_text = _agent_system_reflector_base_text(job)
    reflection_prompt = _render_agent_system_reflection_prompt(
        job=job,
        dataset=dataset,
        manifest=manifest,
        records=records,
        reflected_records=reflected_records,
        base_text=base_text,
    )
    llm_config = _reflector_llm_config(job)
    output_path.write_text(
        _ensure_trailing_newline(_generate_agent_system_reflection(reflection_prompt, llm_config)),
        encoding="utf-8",
    )

    success_count = sum(1 for record in reflected_records if record["kind"] == "success")
    failure_count = sum(1 for record in reflected_records if record["kind"] == "failure")
    lineage = {
        **_dict_config(job.config.get("lineage")),
        "method": "agent_system_reflector",
        "input_artifact_ids": _input_artifact_ids(job),
        "source_dataset_artifact_id": dataset.artifact_id,
    }
    return [
        ArtifactRegisterRequest(
            type=ArtifactType.AGENT_SYSTEM,
            name=name,
            uri=output_path.resolve().as_uri(),
            manifest={
                "content_path": target_path.as_posix(),
                "target_path": target_path.as_posix(),
                "source_dataset_artifact_id": dataset.artifact_id,
                "source_dataset_uri": dataset.uri,
                "record_count": len(records),
                "reflected_record_count": len(reflected_records),
                "success_count": success_count,
                "failure_count": failure_count,
                "method": "agent_system_reflector",
                "reflector_provider": llm_config["provider"],
                "reflector_model": llm_config["model"],
            },
            lineage=lineage,
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
    "agent_system_reflector": agent_system_reflector,
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


def _render_agent_system_reflection_prompt(
    *,
    job: WorkerClaimedJob,
    dataset: WorkerClaimInputArtifact,
    manifest: dict[str, Any],
    records: list[dict[str, Any]],
    reflected_records: list[dict[str, str]],
    base_text: str,
) -> str:
    success_records = [record for record in reflected_records if record["kind"] == "success"]
    failure_records = [record for record in reflected_records if record["kind"] == "failure"]
    other_records = [record for record in reflected_records if record["kind"] == "observation"]
    feedback_records = [record for record in reflected_records if record.get("evolution_feedback")]

    lines: list[str] = []
    if base_text.strip():
        lines.extend([base_text.strip(), ""])
    else:
        lines.extend(
            [
                "# Evolved Agent System",
                "",
                "Follow repository-local conventions and preserve task-specific learnings.",
                "",
            ]
        )

    lines.extend(
        [
            "## Reflections From Prior Trajectories",
            "",
            f"- job_id: {job.job_id}",
            f"- dataset_artifact_id: {dataset.artifact_id}",
            f"- dataset_name: {manifest.get('name') or dataset.name or 'unknown'}",
            f"- record_count: {len(records)}",
            f"- reflected_record_count: {len(reflected_records)}",
            "",
        ]
    )

    _append_reflection_section(lines, "Successful Patterns", success_records, "observed")
    _append_reflection_section(lines, "Failures To Avoid", failure_records, "failure_signal")
    _append_reflection_section(lines, "Additional Observations", other_records, "observed")
    _append_shared_evolution_feedback_section(lines, feedback_records)
    lines.extend(
        [
            "## Operating Rules",
            "",
            "- Start from repository-local instructions and task-specific constraints.",
            "- Turn repeated failure signals into explicit checks before editing.",
            "- Prefer focused verification tied to the changed behavior before broad cleanup.",
            "",
        ]
    )
    return "\n".join(lines)


def _generate_agent_system_reflection(prompt: str, llm_config: dict[str, Any]) -> str:
    provider = llm_config["provider"]
    if provider == _REFLECTOR_PROVIDER_CODEX_CLI:
        return _generate_agent_system_reflection_with_codex_cli(prompt, llm_config)
    if provider == _REFLECTOR_PROVIDER_OPENAI_CHAT:
        return _generate_agent_system_reflection_with_openai_chat(prompt, llm_config)
    raise ValueError(f"Unsupported agent_system_reflector LLM provider: {provider}")


def _generate_agent_system_reflection_with_openai_chat(
    prompt: str,
    llm_config: dict[str, Any],
) -> str:
    payload: dict[str, Any] = {
        "model": llm_config["model"],
        "messages": [
            {
                "role": "system",
                "content": (
                    "You are a reflector for an agent system. Read prior task trajectories, "
                    "preserve useful existing instructions, and produce a concise Markdown "
                    "agent-system instruction file. Return only the Markdown file content."
                ),
            },
            {"role": "user", "content": prompt},
        ],
        "temperature": llm_config["temperature"],
    }
    max_tokens = llm_config.get("max_tokens")
    if max_tokens is not None:
        payload["max_tokens"] = max_tokens

    headers = {"content-type": "application/json"}
    api_key = llm_config.get("api_key")
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"

    with httpx.Client(timeout=llm_config["timeout_seconds"], trust_env=False) as client:
        response = client.post(
            f"{llm_config['base_url']}/chat/completions",
            headers=headers,
            json=payload,
        )
        response.raise_for_status()
    content = _chat_completion_content(response.json())
    if not content.strip():
        raise ValueError("agent_system_reflector LLM returned empty content")
    return content.strip()


def _generate_agent_system_reflection_with_codex_cli(
    prompt: str,
    llm_config: dict[str, Any],
) -> str:
    with tempfile.TemporaryDirectory(prefix="polar-agent-system-reflector-") as tmp:
        tmpdir = Path(tmp)
        output_path = tmpdir / "last-message.md"
        args = [
            str(llm_config["codex_bin"]),
            "exec",
            "--json",
            "--ignore-user-config",
            "--ephemeral",
            "--ask-for-approval",
            "never",
            "--sandbox",
            "read-only",
            "--disable",
            "shell_tool",
            "--skip-git-repo-check",
            "--cd",
            str(tmpdir),
            "--output-last-message",
            str(output_path),
            "--model",
            str(llm_config["model"]),
            "-",
        ]
        prompt_input = _codex_cli_reflector_prompt(prompt)
        try:
            subprocess.run(
                args,
                check=True,
                capture_output=True,
                env=_codex_cli_reflector_env(llm_config),
                input=prompt_input,
                text=True,
                timeout=llm_config["timeout_seconds"],
            )
        except subprocess.CalledProcessError as exc:
            stderr = (exc.stderr or "").strip()
            stdout = (exc.stdout or "").strip()
            detail = stderr or stdout or f"exit code {exc.returncode}"
            raise ValueError(f"agent_system_reflector codex_cli failed: {detail}") from exc
        except subprocess.TimeoutExpired as exc:
            raise ValueError("agent_system_reflector codex_cli timed out") from exc

        content = output_path.read_text(encoding="utf-8") if output_path.exists() else ""
    if not content.strip():
        raise ValueError("agent_system_reflector codex_cli returned empty content")
    return content.strip()


def _codex_cli_reflector_prompt(prompt: str) -> str:
    return (
        "Return only the Markdown agent-system instruction file. "
        "Do not include explanations, code fences, or surrounding commentary.\n\n"
        "You are a reflector for an agent system. Read prior task trajectories, "
        "preserve useful existing instructions, and produce a concise Markdown "
        "agent-system instruction file.\n\n"
        f"{prompt}"
    )


def _codex_cli_reflector_env(llm_config: dict[str, Any]) -> dict[str, str]:
    env = dict(os.environ)
    for key in _REFLECTOR_PROXY_ENV_VARS:
        env.pop(key, None)
    codex_home = llm_config.get("codex_home")
    if isinstance(codex_home, str) and codex_home.strip():
        env["CODEX_HOME"] = codex_home.strip()
    return env


def _chat_completion_content(payload: Any) -> str:
    if not isinstance(payload, dict):
        return ""
    choices = payload.get("choices")
    if not isinstance(choices, list) or not choices:
        return ""
    first = choices[0]
    if not isinstance(first, dict):
        return ""
    message = first.get("message")
    if isinstance(message, dict):
        content = message.get("content")
        if isinstance(content, str):
            return content
    text = first.get("text")
    if isinstance(text, str):
        return text
    return ""


def _append_reflection_section(
    lines: list[str],
    title: str,
    records: list[dict[str, str]],
    detail_key: str,
) -> None:
    if not records:
        return
    lines.extend([f"### {title}", ""])
    for record in records:
        lines.append(
            "- "
            f"task={record['task_id']} session={record['session_id']} "
            f"status={record['status']} reward={record['reward']}"
        )
        if record["prompt"]:
            lines.append(f"  - prompt: {record['prompt']}")
        detail = record[detail_key]
        if detail:
            lines.append(f"  - {detail_key}: {detail}")
    lines.append("")


def _append_shared_evolution_feedback_section(
    lines: list[str],
    records: list[dict[str, str]],
) -> None:
    if not records:
        return
    lines.extend(["## Shared Evolution Feedback", ""])
    for record in records:
        lines.append(
            "- "
            f"task={record['task_id']} session={record['session_id']} "
            f"status={record['status']} reward={record['reward']}"
        )
        lines.append(f"  - feedback: {record['evolution_feedback']}")
    lines.append("")


def _reflection_records(
    records: list[dict[str, Any]],
    *,
    max_records: int,
) -> list[dict[str, str]]:
    reflected: list[dict[str, str]] = []
    for record in records:
        if len(reflected) >= max_records:
            break
        summary = _reflection_record(record)
        if summary is not None:
            reflected.append(summary)
    return reflected


def _reflection_record(record: dict[str, Any]) -> dict[str, str] | None:
    traces = record.get("traces")
    evolution_feedback = _record_evolution_feedback(record)
    if (not isinstance(traces, list) or not traces) and not evolution_feedback:
        return None

    prompt = ""
    observed = ""
    failure_signal = ""
    for trace in traces if isinstance(traces, list) else []:
        if not isinstance(trace, dict):
            continue
        prompt = prompt or _messages_text(trace.get("prompt_messages"))
        observed = observed or _messages_text(trace.get("response_messages"))
        metadata = trace.get("metadata")
        if isinstance(metadata, dict):
            transcript = metadata.get("transcript")
            if isinstance(transcript, str) and transcript.strip():
                failure_signal = failure_signal or transcript.strip()

    payload_summary = _record_summary(record)
    if not observed and payload_summary:
        observed = payload_summary
    if not failure_signal:
        failure_signal = observed or payload_summary

    reward = _record_reward(record)
    kind = _reflection_kind(record.get("status"), reward)
    return {
        "kind": kind,
        "task_id": _string_field(record.get("task_id"), "unknown_task"),
        "session_id": _string_field(record.get("session_id"), "unknown_session"),
        "status": _string_field(record.get("status"), "unknown_status"),
        "reward": "" if reward is None else str(reward),
        "prompt": _snippet(prompt),
        "observed": _snippet(observed),
        "failure_signal": _snippet(failure_signal),
        "evolution_feedback": _snippet(evolution_feedback, limit=2000),
    }


def _reflection_kind(status: Any, reward: float | None) -> str:
    normalized_status = str(status or "").upper()
    if normalized_status in {"ERROR", "FAILED", "FAILURE", "TIMEOUT"}:
        return "failure"
    if reward is not None and reward < 0.5:
        return "failure"
    if normalized_status in {"COMPLETED", "SUCCESS", "SUCCEEDED"}:
        return "success"
    if reward is not None and reward >= 0.8:
        return "success"
    return "observation"


def _messages_text(value: Any) -> str:
    if not isinstance(value, list):
        return ""
    parts: list[str] = []
    for message in value:
        if not isinstance(message, dict):
            continue
        content = _content_text(message.get("content"))
        if content:
            parts.append(content)
    return "\n".join(parts)


def _content_text(value: Any) -> str:
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, list):
        return "\n".join(part for item in value if (part := _content_text(item)))
    if isinstance(value, dict):
        for key in ("text", "content", "output_text", "summary"):
            text = _content_text(value.get(key))
            if text:
                return text
    return ""


def _agent_system_reflector_base_text(job: WorkerClaimedJob) -> str:
    for key in ("base_agent_system_markdown", "agent_system_markdown", "content"):
        value = job.config.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    for artifact in job.input_artifacts:
        if (
            artifact.type == ArtifactType.AGENT_SYSTEM
            or artifact.type == ArtifactType.AGENT_SYSTEM.value
        ):
            try:
                return _file_uri_to_path(artifact.uri).read_text(encoding="utf-8").strip()
            except (OSError, ValueError, UnicodeDecodeError):
                continue
    return ""


def _reflector_llm_config(job: WorkerClaimedJob) -> dict[str, Any]:
    nested = job.config.get("reflector_llm")
    raw_config = nested if isinstance(nested, dict) else {}
    model = _config_string(raw_config, "model") or _config_string(job.config, "reflector_model")
    if not model:
        raise ValueError("agent_system_reflector requires reflector_llm.model")
    provider = (
        _config_string(raw_config, "provider")
        or _config_string(job.config, "reflector_provider")
        or _REFLECTOR_PROVIDER_OPENAI_CHAT
    )

    api_key_env = (
        _config_string(raw_config, "api_key_env")
        or _config_string(job.config, "reflector_api_key_env")
        or "OPENAI_API_KEY"
    )
    api_key = (
        _config_string(raw_config, "api_key")
        or _config_string(job.config, "reflector_api_key")
        or os.environ.get(api_key_env, "")
    )
    base_url = (
        _config_string(raw_config, "base_url")
        or _config_string(job.config, "reflector_base_url")
        or os.environ.get("OPENAI_BASE_URL", "")
        or "https://api.openai.com/v1"
    ).rstrip("/")
    temperature = _float_config(
        raw_config.get("temperature", job.config.get("reflector_temperature")),
        0.2,
    )
    timeout_seconds = _float_config(
        raw_config.get("timeout_seconds", job.config.get("reflector_timeout_seconds")),
        30.0,
    )
    max_tokens = raw_config.get("max_tokens", job.config.get("reflector_max_tokens"))
    codex_home = _config_string(raw_config, "codex_home") or _config_string(
        job.config,
        "reflector_codex_home",
    )
    codex_bin = (
        _config_string(raw_config, "codex_bin")
        or _config_string(job.config, "reflector_codex_bin")
        or "codex"
    )
    return {
        "provider": provider,
        "model": model,
        "base_url": base_url,
        "api_key": api_key,
        "temperature": temperature,
        "timeout_seconds": timeout_seconds,
        "max_tokens": _optional_int(max_tokens),
        "codex_home": codex_home,
        "codex_bin": codex_bin,
    }


def _record_reward(record: dict[str, Any]) -> float | None:
    reward = record.get("reward")
    if isinstance(reward, int | float):
        return float(reward)
    return None


def _record_evolution_feedback(record: dict[str, Any]) -> str:
    for value in _candidate_evolution_feedback_values(record):
        rendered = _render_evolution_feedback_value(value)
        if rendered:
            return rendered
    return ""


def _candidate_evolution_feedback_values(record: dict[str, Any]) -> list[Any]:
    values: list[Any] = [record.get("evolution_feedback")]
    payload = record.get("payload")
    if isinstance(payload, dict):
        values.append(payload.get("evolution_feedback"))
        session_result = payload.get("session_result")
        if isinstance(session_result, dict):
            values.append(session_result.get("evolution_feedback"))
            metadata = session_result.get("metadata")
            if isinstance(metadata, dict):
                values.append(metadata.get("evolution_feedback"))
    return values


def _render_evolution_feedback_value(value: Any) -> str:
    if isinstance(value, str) and value.strip():
        return value.strip()
    if isinstance(value, dict):
        parts: list[str] = []
        for key in sorted(value):
            rendered = _render_evolution_feedback_value(value[key])
            if rendered:
                title = str(key).replace("_", " ").title()
                parts.append(f"{title}: {rendered}")
        return "\n".join(parts)
    if isinstance(value, list):
        parts = [_render_evolution_feedback_value(item) for item in value]
        return "\n".join(part for part in parts if part)
    return ""


def _string_field(value: Any, default: str) -> str:
    if isinstance(value, str) and value.strip():
        return value.strip()
    return default


def _config_string(config: dict[str, Any], key: str) -> str:
    value = config.get(key)
    if isinstance(value, str) and value.strip():
        return value.strip()
    return ""


def _snippet(value: str, *, limit: int = 240) -> str:
    text = " ".join(value.split())
    if len(text) <= limit:
        return text
    return text[: limit - 3].rstrip() + "..."


def _input_artifact_ids(job: WorkerClaimedJob) -> list[str]:
    return [artifact.artifact_id for artifact in job.input_artifacts]


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


def _int_config(value: Any, default: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return max(parsed, 0)


def _optional_int(value: Any) -> int | None:
    if value is None:
        return None
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def _float_config(value: Any, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default
