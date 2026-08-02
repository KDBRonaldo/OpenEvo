from __future__ import annotations

import hashlib
import json
import math
import os
import re
import stat
import subprocess
import tempfile
from collections.abc import Callable
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse

import httpx

from openevo.evolution.agent_system import (
    DEFAULT_AGENT_SYSTEM_TARGET_PATH,
    normalize_agent_system_target_path,
)
from openevo.evolution.framework import canonical_digest
from openevo.evolution.harness_service import (
    core_gateway_base_url_from_environment,
    core_gateway_chat_completion,
)
from openevo.evolution.models import (
    ArtifactRegisterRequest,
    ArtifactType,
    WorkerClaimInputArtifact,
    WorkerClaimedJob,
)

EvolutionMethod = Callable[[WorkerClaimedJob, Path], list[ArtifactRegisterRequest]]

_REFLECTOR_PROVIDER_OPENAI_CHAT = "openai_chat"
_REFLECTOR_PROVIDER_CODEX_CLI = "codex_cli"
_DEFAULT_REFLECTOR_TIMEOUT_SECONDS = 30.0
_DEFAULT_CODEX_CLI_REFLECTOR_TIMEOUT_SECONDS = 300.0
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
_PROMOTION_SUPPORT_FIELDS = (
    "trajectory_findings",
    "proposed_changes",
    "expected_benefits",
    "risks",
    "validation_checks",
)
_HUMAN_FEEDBACK_LIST_FIELDS = (
    "observed_issues",
    "suggested_changes",
    "risks",
    "validation_checks",
    "labels",
)
_TEXT_MEMORY_EXPEL_REQUIRED_SECTIONS = (
    "Do",
    "Avoid",
    "Validate",
    "When Applicable",
    "Retired Or Superseded",
)
_TEXT_MEMORY_EXPEL_EMPTY_SECTION_ITEMS = {
    "Do": "No additional reusable action was supported by the selected trajectories.",
    "Avoid": "No recurring failure mode was supported by the selected trajectories.",
    "Validate": (
        "Before reusing this memory, verify its guidance against the current task and "
        "available checks."
    ),
    "When Applicable": (
        "Apply this memory only when the current task matches the reflected trajectory "
        "conditions."
    ),
    "Retired Or Superseded": (
        "No prior memory was identified as contradicted or superseded."
    ),
}
_HUMAN_FEEDBACK_AVAILABLE_STATUS = "available_for_evolution"
_HUMAN_FEEDBACK_SUMMARY_LIMIT = 8
_HUMAN_FEEDBACK_VALUES_PER_FIELD_LIMIT = 4
_CREDENTIAL_URI_RE = re.compile(
    r"\b[A-Za-z][A-Za-z0-9+.-]*://\S*@\S+",
    re.IGNORECASE,
)
_URI_QUERY_FRAGMENT_RE = re.compile(
    r"\b([A-Za-z][A-Za-z0-9+.-]*://[^\s?#]+)[?#]\S*",
    re.IGNORECASE,
)
_FILE_URI_RE = re.compile(r"\bfile://\S+", re.IGNORECASE)
_POSIX_ABSOLUTE_PATH_RE = re.compile(r"(?<![\w:/])/(?!/)(?:[^\s,;:/]+/)*[^\s,;]+")
_WINDOWS_UNC_PATH_RE = re.compile(
    r"\\\\[^\s\\/:*?\"<>|,;]+\\(?:[^\\/:*?\"<>|\r\n,;]+\\)*[^\s\\/:*?\"<>|,;]+"
)
_WINDOWS_ABSOLUTE_PATH_RE = re.compile(
    r"\b[A-Za-z]:[\\/](?:[^\\/:*?\"<>|\r\n,;]+[\\/])*[^\s\\/:*?\"<>|,;]+"
)
_SECRET_ASSIGNMENT_RE = re.compile(
    r"\b[A-Za-z0-9_]*(?:"
    r"api[_-]?key|access[_-]?key(?:[_-]?id)?|accesskeyid|token|password|secret|"
    r"authorization"
    r")[A-Za-z0-9_]*\s*[:=]\s*(?:bearer|basic)?\s*[^\s,;]+",
    re.IGNORECASE,
)
_BEARER_VALUE_RE = re.compile(
    r"\bBearer\s+[A-Za-z0-9._~+/=-]+",
    re.IGNORECASE,
)
_AWS_ACCESS_KEY_RE = re.compile(r"\b(?:AKIA|ASIA)[A-Z0-9]{12,}\b")
_EVOLUTION_FEEDBACK_UNSAFE_KEYS = {
    "human",
    "human_feedback",
    "raw_payload",
    "raw_payloads",
    "rationale",
    "status",
}
_EVOLUTION_FEEDBACK_UNSAFE_KEY_FRAGMENTS = (
    "api_key",
    "apikey",
    "access_key",
    "accesskey",
    "rationale",
    "token",
    "password",
    "secret",
    "authorization",
)
_EVOLUTION_FEEDBACK_LIKE_KEYS = {
    "confidence",
    "decision",
    "feedback_id",
    "labels",
    "observed_issues",
    "risks",
    "score",
    "suggested_changes",
    "validation_checks",
}
_MAX_DATASET_MANIFEST_BYTES = 4 * 1024 * 1024
_MAX_DATASET_RECORDS_BYTES = 128 * 1024 * 1024


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

    manifest, records = _read_dataset_artifact(dataset)

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
            compatibility=_dict_config(job.config.get("compatibility")),
            tags=_string_list(job.config.get("tags")),
            promoted=bool(job.config.get("promoted", False)),
        )
    ]


def text_memory_reflector(
    job: WorkerClaimedJob,
    artifact_root: Path,
) -> list[ArtifactRegisterRequest]:
    dataset = _first_input_artifact(job, ArtifactType.DATASET)
    if dataset is None:
        raise ValueError("text_memory_reflector requires an input dataset artifact")

    manifest, records = _read_dataset_artifact(dataset)
    reflected_records = _reflection_records(
        records,
        max_records=_int_config(job.config.get("max_records"), 20),
    )
    prior_memory_texts = _text_memory_reflector_base_texts(job)

    reflection_prompt = _render_text_memory_reflection_prompt(
        job=job,
        dataset=dataset,
        manifest=manifest,
        records=records,
        reflected_records=reflected_records,
        prior_memory_texts=prior_memory_texts,
    )
    reflection_prompt = _redact_generic_reflector_prompt(
        reflection_prompt,
        job=job,
        manifests=[manifest],
    )
    llm_config = _reflector_llm_config(job)
    memory_markdown = _generate_reflector_markdown(
        reflection_prompt,
        llm_config,
        system_message=(
            "You are a reflector for text memory. Read prior task trajectories and "
            "produce concise reusable Markdown memory. Return only memory.md content."
        ),
        codex_prompt=_codex_cli_text_memory_reflector_prompt(reflection_prompt),
        error_context="text_memory_reflector",
        temp_prefix="openevo-text-memory-reflector-",
    )
    memory_markdown, audit_report = _guard_generic_reflector_output(
        memory_markdown,
        job=job,
        manifests=[manifest],
    )

    output_dir = artifact_root / "workers" / job.job_id / "text_memory_reflector"
    output_dir.mkdir(parents=True, exist_ok=True)
    memory_path = output_dir / "memory.md"
    memory_path.write_text(_ensure_trailing_newline(memory_markdown), encoding="utf-8")

    success_count = sum(1 for record in reflected_records if record["kind"] == "success")
    failure_count = sum(1 for record in reflected_records if record["kind"] == "failure")
    lineage = {
        **_dict_config(job.config.get("lineage")),
        "method": "text_memory_reflector",
        "input_artifact_ids": _input_artifact_ids(job),
        "source_dataset_artifact_id": dataset.artifact_id,
    }
    return [
        ArtifactRegisterRequest(
            type=ArtifactType.TEXT_MEMORY,
            name=str(
                job.config.get("name")
                or f"{dataset.name or dataset.artifact_id} reflected memory"
            ),
            uri=memory_path.resolve().as_uri(),
            manifest={
                "content_path": "memory.md",
                "method": "text_memory_reflector",
                "source_dataset_artifact_id": dataset.artifact_id,
                "source_dataset_uri": dataset.uri,
                "record_count": len(records),
                "reflected_record_count": len(reflected_records),
                "success_count": success_count,
                "failure_count": failure_count,
                "reflector_provider": llm_config["provider"],
                "reflector_model": llm_config["model"],
                "reflection_audit": audit_report,
                "promotion_support": _reflector_promotion_support(
                    job,
                    method="text_memory_reflector",
                    artifact_kind="text_memory",
                    dataset_ids=[dataset.artifact_id],
                    record_count=len(records),
                    reflected_record_count=len(reflected_records),
                    success_count=success_count,
                    failure_count=failure_count,
                    proposed_changes=[
                        "Update reusable text memory from recurring observations in the "
                        "selected trajectories."
                    ],
                    expected_benefits=[
                        "Improve later rollouts by preserving task-relevant facts, "
                        "pitfalls, and validation reminders observed in prior runs."
                    ],
                    risks=[
                        "Memory may overfit the sampled trajectories or preserve a "
                        "task-specific shortcut if the review packet is not checked."
                    ],
                    validation_checks=[
                        "Verify that the memory is grounded in observed trajectories "
                        "and does not encode held-out answers or brittle file names."
                    ],
                ),
            },
            lineage=lineage,
            compatibility=_dict_config(job.config.get("compatibility")),
            scores=_scores_config(job.config.get("scores")),
            tags=_string_list(job.config.get("tags")),
            promoted=bool(job.config.get("promoted", False)),
        )
    ]


def text_memory_expel_reflector(
    job: WorkerClaimedJob,
    artifact_root: Path,
) -> list[ArtifactRegisterRequest]:
    dataset_artifacts = _input_artifacts(job, ArtifactType.DATASET)
    if not dataset_artifacts:
        raise ValueError("text_memory_expel_reflector requires an input dataset artifact")
    dataset = dataset_artifacts[0]

    manifests: list[dict[str, Any]] = []
    records: list[dict[str, Any]] = []
    for dataset_artifact in dataset_artifacts:
        manifest, dataset_records = _read_dataset_artifact(dataset_artifact)
        manifests.append(manifest)
        records.extend(dataset_records)
    source_dataset_artifact_ids = [artifact.artifact_id for artifact in dataset_artifacts]
    source_dataset_uris = [artifact.uri for artifact in dataset_artifacts]
    manifest = {
        "name": manifests[0].get("name") or dataset.name or "combined text memory dataset",
        "source_dataset_artifact_ids": source_dataset_artifact_ids,
        "source_dataset_uris": source_dataset_uris,
        "dataset_count": len(dataset_artifacts),
    }
    reflected_records = _reflection_records(
        records,
        max_records=_int_config(job.config.get("max_records"), 20),
    )
    prior_memory_texts = _text_memory_reflector_base_texts(job)

    reflection_prompt = _render_text_memory_expel_reflection_prompt(
        job=job,
        dataset=dataset,
        manifest=manifest,
        records=records,
        reflected_records=reflected_records,
        prior_memory_texts=prior_memory_texts,
    )
    reflection_prompt = _redact_generic_reflector_prompt(
        reflection_prompt,
        job=job,
        manifests=manifests,
    )
    llm_config = _reflector_llm_config(job)
    memory_markdown = _generate_reflector_markdown(
        reflection_prompt,
        llm_config,
        system_message=(
            "You are an ExpeL/Reflexion-style reflector for text memory. Read prior "
            "task trajectories and produce structured reusable Markdown memory. "
            "Return only memory.md content with the required sections."
        ),
        codex_prompt=_codex_cli_text_memory_reflector_prompt(reflection_prompt),
        error_context="text_memory_expel_reflector",
        temp_prefix="openevo-text-memory-expel-reflector-",
    )
    memory_markdown, audit_report = _guard_generic_reflector_output(
        memory_markdown,
        job=job,
        manifests=manifests,
    )
    memory_markdown, structure_completion = _complete_text_memory_expel_sections(
        memory_markdown
    )
    _require_text_memory_expel_sections(memory_markdown)

    output_dir = artifact_root / "workers" / job.job_id / "text_memory_expel_reflector"
    output_dir.mkdir(parents=True, exist_ok=True)
    memory_path = output_dir / "memory.md"
    memory_path.write_text(_ensure_trailing_newline(memory_markdown), encoding="utf-8")

    success_count = sum(1 for record in reflected_records if record["kind"] == "success")
    failure_count = sum(1 for record in reflected_records if record["kind"] == "failure")
    lineage = {
        **_dict_config(job.config.get("lineage")),
        "method": "text_memory_expel_reflector",
        "input_artifact_ids": _input_artifact_ids(job),
        "source_dataset_artifact_id": dataset.artifact_id,
        "source_dataset_artifact_ids": source_dataset_artifact_ids,
    }
    required_sections = list(_TEXT_MEMORY_EXPEL_REQUIRED_SECTIONS)
    return [
        ArtifactRegisterRequest(
            type=ArtifactType.TEXT_MEMORY,
            name=str(
                job.config.get("name")
                or f"{dataset.name or dataset.artifact_id} ExpeL reflected memory"
            ),
            uri=memory_path.resolve().as_uri(),
            manifest={
                "content_path": "memory.md",
                "method": "text_memory_expel_reflector",
                "source_dataset_artifact_id": dataset.artifact_id,
                "source_dataset_uri": dataset.uri,
                "source_dataset_artifact_ids": source_dataset_artifact_ids,
                "source_dataset_uris": source_dataset_uris,
                "record_count": len(records),
                "reflected_record_count": len(reflected_records),
                "success_count": success_count,
                "failure_count": failure_count,
                "prior_memory_count": len(prior_memory_texts),
                "required_sections": required_sections,
                "structure_completion": structure_completion,
                "reflector_provider": llm_config["provider"],
                "reflector_model": llm_config["model"],
                "reflection_audit": audit_report,
                "promotion_support": _reflector_promotion_support(
                    job,
                    method="text_memory_expel_reflector",
                    artifact_kind="text_memory",
                    dataset_ids=source_dataset_artifact_ids,
                    record_count=len(records),
                    reflected_record_count=len(reflected_records),
                    success_count=success_count,
                    failure_count=failure_count,
                    proposed_changes=[
                        "Update structured ExpeL-style text memory from recurring "
                        "successes, failures, validation habits, and superseded advice "
                        "in the selected trajectories."
                    ],
                    expected_benefits=[
                        "Improve later rollouts by separating actions to repeat, "
                        "failure modes to avoid, validation checks, conditional "
                        "guidance, and retired memory."
                    ],
                    risks=[
                        "Memory may overfit the sampled trajectories or preserve stale "
                        "advice if the retired section is not reviewed."
                    ],
                    validation_checks=[
                        "Verify that each required section is present, grounded in "
                        "observed trajectories, and free of protected held-out literals."
                    ],
                ),
            },
            lineage=lineage,
            compatibility=_dict_config(job.config.get("compatibility")),
            scores=_scores_config(job.config.get("scores")),
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
            compatibility=_dict_config(job.config.get("compatibility")),
            tags=_string_list(job.config.get("tags")),
            promoted=bool(job.config.get("promoted", False)),
        )
    ]


def skill_bundle_reflector(
    job: WorkerClaimedJob,
    artifact_root: Path,
) -> list[ArtifactRegisterRequest]:
    dataset = _first_input_artifact(job, ArtifactType.DATASET)
    if dataset is None:
        raise ValueError("skill_bundle_reflector requires an input dataset artifact")

    manifest, records = _read_dataset_artifact(dataset)
    reflected_records = _reflection_records(
        records,
        max_records=_int_config(job.config.get("max_records"), 20),
    )
    base_text, base_skill_artifact = _skill_bundle_reflector_base(job)

    reflection_prompt = _render_skill_bundle_reflection_prompt(
        job=job,
        dataset=dataset,
        manifest=manifest,
        records=records,
        reflected_records=reflected_records,
        base_text=base_text,
    )
    reflection_prompt = _redact_generic_reflector_prompt(
        reflection_prompt,
        job=job,
        manifests=[manifest],
    )
    llm_config = _reflector_llm_config(job)
    skill_markdown = _generate_reflector_markdown(
        reflection_prompt,
        llm_config,
        system_message=(
            "You are a reflector for a Codex skill bundle. Read prior task trajectories "
            "and produce the SKILL.md entrypoint. Return only SKILL.md content."
        ),
        codex_prompt=_codex_cli_skill_bundle_reflector_prompt(reflection_prompt),
        error_context="skill_bundle_reflector",
        temp_prefix="openevo-skill-bundle-reflector-",
    )
    skill_markdown, audit_report = _guard_generic_reflector_output(
        skill_markdown,
        job=job,
        manifests=[manifest],
    )

    name = str(
        job.config.get("name") or f"{dataset.name or dataset.artifact_id} reflected skill"
    )
    output_dir = artifact_root / "workers" / job.job_id / "skill_bundle_reflector" / _slug(name)
    output_dir.mkdir(parents=True, exist_ok=True)
    skill_path = output_dir / "SKILL.md"
    skill_path.write_text(_ensure_trailing_newline(skill_markdown), encoding="utf-8")

    success_count = sum(1 for record in reflected_records if record["kind"] == "success")
    failure_count = sum(1 for record in reflected_records if record["kind"] == "failure")
    lineage = {
        **_dict_config(job.config.get("lineage")),
        "method": "skill_bundle_reflector",
        "input_artifact_ids": _input_artifact_ids(job),
        "source_dataset_artifact_id": dataset.artifact_id,
    }
    manifest_payload = {
        "entrypoint": "SKILL.md",
        "files": ["SKILL.md"],
        "method": "skill_bundle_reflector",
        "source_dataset_artifact_id": dataset.artifact_id,
        "source_dataset_uri": dataset.uri,
        "record_count": len(records),
        "reflected_record_count": len(reflected_records),
        "success_count": success_count,
        "failure_count": failure_count,
        "reflector_provider": llm_config["provider"],
        "reflector_model": llm_config["model"],
        "reflection_audit": audit_report,
        "promotion_support": _reflector_promotion_support(
            job,
            method="skill_bundle_reflector",
            artifact_kind="skill_bundle",
            dataset_ids=[dataset.artifact_id],
            record_count=len(records),
            reflected_record_count=len(reflected_records),
            success_count=success_count,
            failure_count=failure_count,
            proposed_changes=[
                "Update the skill entrypoint with reusable workflow guidance distilled "
                "from the selected trajectories."
            ],
            expected_benefits=[
                "Improve repeat task execution by packaging learned checks and "
                "workflow steps as a loadable skill."
            ],
            risks=[
                "The skill can become too broad or too task-specific if it copies "
                "incidental trajectory details."
            ],
            validation_checks=[
                "Review SKILL.md for concrete triggers, actions, and validation "
                "checks that transfer beyond a single held-out answer."
            ],
        ),
    }
    if base_skill_artifact is not None:
        manifest_payload["base_skill_bundle_artifact_id"] = base_skill_artifact.artifact_id

    return [
        ArtifactRegisterRequest(
            type=ArtifactType.SKILL_BUNDLE,
            name=name,
            uri=output_dir.resolve().as_uri(),
            manifest=manifest_payload,
            lineage=lineage,
            compatibility=_dict_config(job.config.get("compatibility")),
            scores=_scores_config(job.config.get("scores")),
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

    manifest, records = _read_dataset_artifact(dataset)
    reflected_records = _reflection_records(
        records,
        max_records=_int_config(job.config.get("max_records"), 20),
    )

    name = str(job.config.get("name") or f"{dataset.name or dataset.artifact_id} reflector")
    target_path = Path(
        normalize_agent_system_target_path(
            job.config.get("target_path") or DEFAULT_AGENT_SYSTEM_TARGET_PATH
        )
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
    agent_system_markdown, audit_report = _generate_audited_agent_system_reflection(
        reflection_prompt,
        llm_config,
        job=job,
        manifests=[manifest],
    )
    output_path.write_text(_ensure_trailing_newline(agent_system_markdown), encoding="utf-8")

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
                "agent_system_audit": audit_report,
                "promotion_support": _reflector_promotion_support(
                    job,
                    method="agent_system_reflector",
                    artifact_kind="agent_system",
                    dataset_ids=[dataset.artifact_id],
                    record_count=len(records),
                    reflected_record_count=len(reflected_records),
                    success_count=success_count,
                    failure_count=failure_count,
                    proposed_changes=[
                        "Update the agent-system instructions with methodology "
                        "changes inferred from observed success and failure traces."
                    ],
                    expected_benefits=[
                        "Improve future rollouts by turning trajectory-level failures "
                        "into concrete operating rules and validation checks."
                    ],
                    risks=[
                        "Agent-system rules may overgeneralize from a small trajectory "
                        "sample or hard-code task-specific literals."
                    ],
                    validation_checks=[
                        "Check that each new rule has a trigger, action, and validation "
                        "step and avoids held-out answer leakage."
                    ],
                ),
            },
            lineage=lineage,
            compatibility=_dict_config(job.config.get("compatibility")),
            scores=_scores_config(job.config.get("scores")),
            tags=_string_list(job.config.get("tags")),
            promoted=bool(job.config.get("promoted", False)),
        )
    ]


def agent_system_history_reflector(
    job: WorkerClaimedJob,
    artifact_root: Path,
) -> list[ArtifactRegisterRequest]:
    dataset_artifacts = _input_artifacts(job, ArtifactType.DATASET)
    if not dataset_artifacts:
        raise ValueError(
            "agent_system_history_reflector requires at least one dataset artifact"
        )

    max_records_per_round = _int_config(job.config.get("max_records_per_round"), 8)
    rounds = _history_reflection_rounds(
        dataset_artifacts,
        max_records_per_round=max_records_per_round,
    )

    name = str(
        job.config.get("name")
        or f"{dataset_artifacts[-1].name or dataset_artifacts[-1].artifact_id} history reflector"
    )
    target_path = Path(
        normalize_agent_system_target_path(
            job.config.get("target_path") or DEFAULT_AGENT_SYSTEM_TARGET_PATH
        )
    )
    output_dir = artifact_root / "workers" / job.job_id / "agent_system_history_reflector"
    output_path = output_dir / target_path
    output_path.parent.mkdir(parents=True, exist_ok=True)

    base_text = _agent_system_reflector_base_text(job)
    reflection_prompt = _render_agent_system_history_reflection_prompt(
        job=job,
        rounds=rounds,
        base_text=base_text,
    )
    llm_config = _reflector_llm_config(job)
    agent_system_markdown, audit_report = _generate_audited_agent_system_reflection(
        reflection_prompt,
        llm_config,
        job=job,
        manifests=[history_round["manifest"] for history_round in rounds],
    )
    output_path.write_text(_ensure_trailing_newline(agent_system_markdown), encoding="utf-8")

    reflected_records = [
        record for history_round in rounds for record in history_round["reflected_records"]
    ]
    records = [record for history_round in rounds for record in history_round["records"]]
    human_feedback = _human_feedback_from_records(records)
    shared_feedback_ids = _shared_evolution_feedback_ids_from_records(records)
    success_count = sum(1 for record in reflected_records if record["kind"] == "success")
    failure_count = sum(1 for record in reflected_records if record["kind"] == "failure")
    latest_round = rounds[-1]
    best_round = _best_history_round(rounds)
    dataset_ids = [history_round["artifact"].artifact_id for history_round in rounds]
    extra_findings = [
        f"Best prior round was {best_round['round']} with f1="
        f"{best_round['metrics'].get('f1')}; latest round was "
        f"{latest_round['round']} with f1={latest_round['metrics'].get('f1')}."
    ]
    if human_feedback:
        extra_findings.append(
            f"Included {len(human_feedback)} human feedback item(s) from prior reviews."
        )
    if shared_feedback_ids:
        extra_findings.append(_shared_evolution_feedback_inclusion_finding(shared_feedback_ids))
    lineage = {
        **_dict_config(job.config.get("lineage")),
        "method": "agent_system_history_reflector",
        "input_artifact_ids": _input_artifact_ids(job),
        "source_dataset_artifact_ids": dataset_ids,
    }
    return [
        ArtifactRegisterRequest(
            type=ArtifactType.AGENT_SYSTEM,
            name=name,
            uri=output_path.resolve().as_uri(),
            manifest={
                "content_path": target_path.as_posix(),
                "target_path": target_path.as_posix(),
                "source_dataset_artifact_ids": dataset_ids,
                "source_dataset_uris": [
                    history_round["artifact"].uri for history_round in rounds
                ],
                "round_count": len(rounds),
                "record_count": sum(len(history_round["records"]) for history_round in rounds),
                "reflected_record_count": len(reflected_records),
                "success_count": success_count,
                "failure_count": failure_count,
                "method": "agent_system_history_reflector",
                "latest_round": latest_round["round"],
                "latest_f1": latest_round["metrics"].get("f1"),
                "best_round": best_round["round"],
                "best_f1": best_round["metrics"].get("f1"),
                "human_feedback_ids": _human_feedback_ids(human_feedback),
                "human_feedback_count": len(human_feedback),
                "shared_evolution_feedback_ids": shared_feedback_ids,
                "shared_evolution_feedback_count": len(shared_feedback_ids),
                "reflector_provider": llm_config["provider"],
                "reflector_model": llm_config["model"],
                "agent_system_audit": audit_report,
                "promotion_support": _reflector_promotion_support(
                    job,
                    method="agent_system_history_reflector",
                    artifact_kind="agent_system",
                    dataset_ids=dataset_ids,
                    record_count=sum(len(history_round["records"]) for history_round in rounds),
                    reflected_record_count=len(reflected_records),
                    success_count=success_count,
                    failure_count=failure_count,
                    extra_findings=extra_findings,
                    proposed_changes=[
                        "Revise the agent-system instructions using all available "
                        "round histories, preserving improving behaviors and avoiding "
                        "regressions observed in later rounds."
                    ],
                    expected_benefits=[
                        "Improve stability by comparing multiple rounds instead of "
                        "reflecting only the most recent trajectory batch."
                    ],
                    risks=[
                        "The update may collapse coverage or overfit to the best round "
                        "unless regression risks are explicitly checked."
                    ],
                    validation_checks=[
                        "Compare against the best prior round and reject changes that "
                        "lack concrete regression-prevention checks."
                    ],
                ),
            },
            lineage=lineage,
            compatibility=_dict_config(job.config.get("compatibility")),
            scores=_scores_config(job.config.get("scores")),
            tags=_string_list(job.config.get("tags")),
            promoted=bool(job.config.get("promoted", False)),
        )
    ]


def agent_system_pareto_reflector(
    job: WorkerClaimedJob,
    artifact_root: Path,
) -> list[ArtifactRegisterRequest]:
    dataset_artifacts = _input_artifacts(job, ArtifactType.DATASET)
    if not dataset_artifacts:
        raise ValueError(
            "agent_system_pareto_reflector requires at least one dataset artifact"
        )

    max_records_per_round = _int_config(job.config.get("max_records_per_round"), 8)
    rounds = _history_reflection_rounds(
        dataset_artifacts,
        max_records_per_round=max_records_per_round,
    )
    strategies = _pareto_candidate_strategies(job)
    llm_config = _reflector_llm_config(job)
    base_text = _agent_system_reflector_base_text(job)

    name = str(
        job.config.get("name")
        or f"{dataset_artifacts[-1].name or dataset_artifacts[-1].artifact_id} pareto reflector"
    )
    target_path = Path(
        normalize_agent_system_target_path(
            job.config.get("target_path") or DEFAULT_AGENT_SYSTEM_TARGET_PATH
        )
    )
    output_dir = artifact_root / "workers" / job.job_id / "agent_system_pareto_reflector"
    candidates_dir = output_dir / "candidates"
    candidates_dir.mkdir(parents=True, exist_ok=True)

    manifests = [history_round["manifest"] for history_round in rounds]
    candidates: list[dict[str, Any]] = []
    for index, strategy in enumerate(strategies, start=1):
        prompt = _render_agent_system_pareto_candidate_prompt(
            job=job,
            rounds=rounds,
            base_text=base_text,
            strategy=strategy,
            index=index,
            total=len(strategies),
        )
        markdown, audit_report = _generate_audited_agent_system_reflection(
            prompt,
            llm_config,
            job=job,
            manifests=manifests,
        )
        candidate_path = candidates_dir / f"{index:02d}-{_slug(strategy)}.md"
        candidate_path.write_text(_ensure_trailing_newline(markdown), encoding="utf-8")
        evaluation = _pareto_candidate_evaluation(job, strategy, index=index)
        static_score = _agent_system_static_guardrail_score(markdown)
        gate_failures = _pareto_candidate_gate_failures(
            job=job,
            rounds=rounds,
            candidate_evaluation=evaluation,
            static_score=static_score,
        )
        candidates.append(
            {
                "index": index,
                "strategy": strategy,
                "markdown_path": candidate_path,
                "audit": audit_report,
                "evaluation": evaluation,
                "static_score": static_score,
                "gate_failures": gate_failures,
                "gate_passed": not gate_failures,
                "selection_score": _pareto_candidate_selection_score(
                    evaluation=evaluation,
                    static_score=static_score,
                    gate_failures=gate_failures,
                ),
            }
        )

    selected = _select_pareto_candidate(candidates)
    output_path = output_dir / target_path
    output_path.parent.mkdir(parents=True, exist_ok=True)
    selected_markdown = Path(selected["markdown_path"]).read_text(encoding="utf-8")
    output_path.write_text(_ensure_trailing_newline(selected_markdown), encoding="utf-8")

    best_round = _best_history_round(rounds)
    latest_round = rounds[-1]
    dataset_ids = [history_round["artifact"].artifact_id for history_round in rounds]
    records = [record for history_round in rounds for record in history_round["records"]]
    reflected_record_count = sum(
        len(history_round["reflected_records"])
        for history_round in rounds
    )
    human_feedback = _human_feedback_from_records(records)
    shared_feedback_ids = _shared_evolution_feedback_ids_from_records(records)
    extra_findings = [
        f"Selected candidate strategy {selected['strategy']} from "
        f"{len(candidates)} candidates.",
    ]
    selected_summary = _pareto_candidate_manifest_summary(selected)
    archive = {
        "method": "agent_system_pareto_reflector",
        "job_id": job.job_id,
        "round_count": len(rounds),
        "best_round": best_round["round"],
        "best_metrics": best_round["metrics"],
        "latest_round": latest_round["round"],
        "latest_metrics": latest_round["metrics"],
        "selected_candidate": selected_summary,
        "human_feedback_ids": _human_feedback_ids(human_feedback),
        "human_feedback_count": len(human_feedback),
        "shared_evolution_feedback_ids": shared_feedback_ids,
        "shared_evolution_feedback_count": len(shared_feedback_ids),
        "candidates": [
            _pareto_candidate_report(candidate, output_dir=output_dir)
            for candidate in candidates
        ],
        "promotion_gate": _pareto_promotion_gate_report(job, selected),
    }
    archive_path = output_dir / "candidate_archive.json"
    archive_path.write_text(
        json.dumps(archive, indent=2, sort_keys=True, allow_nan=False) + "\n",
        encoding="utf-8",
    )

    gate_report = _pareto_promotion_gate_report(job, selected)
    extra_findings.append(
        f"Promotion gate passed={gate_report['passed']} failures="
        f"{gate_report['failures']}."
    )
    if human_feedback:
        extra_findings.append(
            f"Included {len(human_feedback)} human feedback item(s) from prior reviews."
        )
    if shared_feedback_ids:
        extra_findings.append(_shared_evolution_feedback_inclusion_finding(shared_feedback_ids))
    lineage = {
        **_dict_config(job.config.get("lineage")),
        "method": "agent_system_pareto_reflector",
        "input_artifact_ids": _input_artifact_ids(job),
        "source_dataset_artifact_ids": dataset_ids,
    }
    return [
        ArtifactRegisterRequest(
            type=ArtifactType.AGENT_SYSTEM,
            name=name,
            uri=output_path.resolve().as_uri(),
            manifest={
                "content_path": target_path.as_posix(),
                "target_path": target_path.as_posix(),
                "source_dataset_artifact_ids": dataset_ids,
                "source_dataset_uris": [
                    history_round["artifact"].uri for history_round in rounds
                ],
                "round_count": len(rounds),
                "candidate_count": len(candidates),
                "method": "agent_system_pareto_reflector",
                "best_round": best_round["round"],
                "best_f1": best_round["metrics"].get("f1"),
                "latest_round": latest_round["round"],
                "latest_f1": latest_round["metrics"].get("f1"),
                "selected_candidate": selected_summary,
                "promotion_gate": gate_report,
                "archive_path": "candidate_archive.json",
                "human_feedback_ids": _human_feedback_ids(human_feedback),
                "human_feedback_count": len(human_feedback),
                "shared_evolution_feedback_ids": shared_feedback_ids,
                "shared_evolution_feedback_count": len(shared_feedback_ids),
                "reflector_provider": llm_config["provider"],
                "reflector_model": llm_config["model"],
                "promotion_support": _reflector_promotion_support(
                    job,
                    method="agent_system_pareto_reflector",
                    artifact_kind="agent_system",
                    dataset_ids=dataset_ids,
                    record_count=sum(len(history_round["records"]) for history_round in rounds),
                    reflected_record_count=reflected_record_count,
                    extra_findings=extra_findings,
                    proposed_changes=[
                        "Promote the selected Pareto candidate after comparing "
                        "candidate quality, static guardrails, and regression risk."
                    ],
                    expected_benefits=[
                        "Reduce single-candidate reflector regressions by requiring "
                        "candidate comparison before promotion."
                    ],
                    risks=[
                        "Candidate ranking can still overfit noisy external metrics or "
                        "underweight recall/precision tradeoffs."
                    ],
                    validation_checks=[
                        "Inspect the candidate archive, selected strategy, gate "
                        "failures, and external metrics before approving promotion."
                    ],
                ),
            },
            lineage=lineage,
            compatibility=_dict_config(job.config.get("compatibility")),
            scores=_pareto_selected_scores(selected, job=job),
            tags=_string_list(job.config.get("tags")),
            promoted=bool(job.config.get("promoted", False)) and gate_report["passed"],
        ),
        ArtifactRegisterRequest(
            type=ArtifactType.REPORT,
            name=f"{name} candidate archive",
            uri=archive_path.resolve().as_uri(),
            manifest={
                "content_path": "candidate_archive.json",
                "method": "agent_system_pareto_reflector",
                "candidate_count": len(candidates),
                "selected_candidate": selected_summary,
                "human_feedback_ids": _human_feedback_ids(human_feedback),
                "human_feedback_count": len(human_feedback),
                "shared_evolution_feedback_ids": shared_feedback_ids,
                "shared_evolution_feedback_count": len(shared_feedback_ids),
            },
            lineage=lineage,
            compatibility=_dict_config(job.config.get("compatibility")),
            tags=_string_list(job.config.get("tags")),
            promoted=False,
        ),
    ]


def agent_system_gepa_reflector(
    job: WorkerClaimedJob,
    artifact_root: Path,
) -> list[ArtifactRegisterRequest]:
    dataset_artifacts = _input_artifacts(job, ArtifactType.DATASET)
    if not dataset_artifacts:
        raise ValueError(
            "agent_system_gepa_reflector requires at least one dataset artifact"
        )

    max_records_per_round = _int_config(job.config.get("max_records_per_round"), 8)
    rounds = _history_reflection_rounds(
        dataset_artifacts,
        max_records_per_round=max_records_per_round,
    )
    strategies = _gepa_mutation_strategies(job)
    llm_config = _reflector_llm_config(job)
    base_text = _agent_system_reflector_base_text(job)
    target_path = Path(
        normalize_agent_system_target_path(
            job.config.get("target_path") or DEFAULT_AGENT_SYSTEM_TARGET_PATH
        )
    )
    name = str(
        job.config.get("name")
        or f"{dataset_artifacts[-1].name or dataset_artifacts[-1].artifact_id} GEPA reflector"
    )
    output_dir = artifact_root / "workers" / job.job_id / "agent_system_gepa_reflector"
    candidates_dir = output_dir / "candidates"
    candidates_dir.mkdir(parents=True, exist_ok=True)

    manifests = [history_round["manifest"] for history_round in rounds]
    best_round = _best_history_round(rounds)
    latest_round = rounds[-1]
    dataset_ids = [history_round["artifact"].artifact_id for history_round in rounds]
    reflected_records = [
        record for history_round in rounds for record in history_round["reflected_records"]
    ]
    records = [record for history_round in rounds for record in history_round["records"]]
    human_feedback = _human_feedback_from_records(records)
    shared_feedback_ids = _shared_evolution_feedback_ids_from_records(records)
    success_count = sum(1 for record in reflected_records if record["kind"] == "success")
    failure_count = sum(1 for record in reflected_records if record["kind"] == "failure")
    record_count = sum(len(history_round["records"]) for history_round in rounds)
    lineage = {
        **_dict_config(job.config.get("lineage")),
        "method": "agent_system_gepa_reflector",
        "input_artifact_ids": _input_artifact_ids(job),
        "source_dataset_artifact_ids": dataset_ids,
    }

    artifacts: list[ArtifactRegisterRequest] = []
    archive_candidates: list[dict[str, Any]] = []
    for index, strategy in enumerate(strategies, start=1):
        prompt = _render_agent_system_gepa_candidate_prompt(
            job=job,
            rounds=rounds,
            base_text=base_text,
            strategy=strategy,
            index=index,
            total=len(strategies),
        )
        markdown, audit_report = _generate_audited_agent_system_reflection(
            prompt,
            llm_config,
            job=job,
            manifests=manifests,
        )
        candidate_dir = candidates_dir / f"{index:02d}-{_slug(strategy)}"
        candidate_path = candidate_dir / target_path
        candidate_path.parent.mkdir(parents=True, exist_ok=True)
        candidate_path.write_text(_ensure_trailing_newline(markdown), encoding="utf-8")
        static_score = _agent_system_static_guardrail_score(markdown)
        extra_findings = [
            f"Generated GEPA candidate {index} of {len(strategies)} using "
            f"mutation strategy {strategy}.",
            f"Static guardrail score for this candidate is {static_score}.",
            f"Best prior round was {best_round['round']} with f1="
            f"{best_round['metrics'].get('f1')}; latest round was "
            f"{latest_round['round']} with f1={latest_round['metrics'].get('f1')}.",
        ]
        if human_feedback:
            extra_findings.append(
                f"Included {len(human_feedback)} human feedback item(s) from prior reviews."
            )
        if shared_feedback_ids:
            extra_findings.append(_shared_evolution_feedback_inclusion_finding(shared_feedback_ids))
        manifest = {
            "content_path": target_path.as_posix(),
            "target_path": target_path.as_posix(),
            "source_dataset_artifact_ids": dataset_ids,
            "source_dataset_uris": [
                history_round["artifact"].uri for history_round in rounds
            ],
            "round_count": len(rounds),
            "candidate_count": len(strategies),
            "candidate_index": index,
            "candidate_strategy": strategy,
            "method": "agent_system_gepa_reflector",
            "best_round": best_round["round"],
            "best_f1": best_round["metrics"].get("f1"),
            "latest_round": latest_round["round"],
            "latest_f1": latest_round["metrics"].get("f1"),
            "static_guardrail_score": static_score,
            "human_feedback_ids": _human_feedback_ids(human_feedback),
            "human_feedback_count": len(human_feedback),
            "shared_evolution_feedback_ids": shared_feedback_ids,
            "shared_evolution_feedback_count": len(shared_feedback_ids),
            "agent_system_audit": audit_report,
            "reflector_provider": llm_config["provider"],
            "reflector_model": llm_config["model"],
            "promotion_support": _reflector_promotion_support(
                job,
                method="agent_system_gepa_reflector",
                artifact_kind="agent_system",
                dataset_ids=dataset_ids,
                record_count=record_count,
                reflected_record_count=len(reflected_records),
                success_count=success_count,
                failure_count=failure_count,
                extra_findings=extra_findings,
                proposed_changes=[
                    f"Promote the GEPA candidate generated by the {strategy} mutation "
                    "strategy if its instructions address the observed verifier "
                    "failures without hard-coding task outputs."
                ],
                expected_benefits=[
                    "Improve targeted failure recovery by reviewing multiple mutation "
                    "strategies as separate candidate artifacts."
                ],
                risks=[
                    "Candidate mutations may overfit one verifier failure mode or "
                    "regress already-passing behaviors if promoted without review."
                ],
                validation_checks=[
                    "Inspect the candidate text, verifier failure evidence, audit "
                    "report, and any follow-up evaluator score before promotion."
                ],
            ),
        }
        artifacts.append(
            ArtifactRegisterRequest(
                type=ArtifactType.AGENT_SYSTEM,
                name=f"{name} candidate {index}: {strategy}",
                uri=candidate_path.resolve().as_uri(),
                manifest=manifest,
                lineage=lineage,
                compatibility=_dict_config(job.config.get("compatibility")),
                scores={
                    **_scores_config(job.config.get("scores")),
                    "static_guardrail_score": float(static_score),
                },
                tags=_string_list(job.config.get("tags")),
                promoted=False,
            )
        )
        archive_candidates.append(
            {
                "index": index,
                "strategy": strategy,
                "markdown_path": candidate_path.relative_to(output_dir).as_posix(),
                "static_score": static_score,
                "audit": audit_report,
            }
        )

    archive = {
        "method": "agent_system_gepa_reflector",
        "job_id": job.job_id,
        "candidate_count": len(strategies),
        "round_count": len(rounds),
        "best_round": best_round["round"],
        "best_metrics": best_round["metrics"],
        "latest_round": latest_round["round"],
        "latest_metrics": latest_round["metrics"],
        "candidates": archive_candidates,
    }
    archive_path = output_dir / "gepa_candidate_archive.json"
    archive_path.write_text(
        json.dumps(archive, indent=2, sort_keys=True, allow_nan=False) + "\n",
        encoding="utf-8",
    )
    artifacts.append(
        ArtifactRegisterRequest(
            type=ArtifactType.REPORT,
            name=f"{name} GEPA candidate archive",
            uri=archive_path.resolve().as_uri(),
            manifest={
                "content_path": "gepa_candidate_archive.json",
                "method": "agent_system_gepa_reflector",
                "candidate_count": len(strategies),
                "human_feedback_ids": _human_feedback_ids(human_feedback),
                "human_feedback_count": len(human_feedback),
                "shared_evolution_feedback_ids": shared_feedback_ids,
                "shared_evolution_feedback_count": len(shared_feedback_ids),
            },
            lineage=lineage,
            compatibility=_dict_config(job.config.get("compatibility")),
            tags=_string_list(job.config.get("tags")),
            promoted=False,
        )
    )
    return artifacts


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
    compatibility = _parametric_memory_compatibility(job, base_model)

    return [
        ArtifactRegisterRequest(
            type=ArtifactType.PARAMETRIC_MEMORY,
            name=str(job.config.get("name") or adapter_id),
            uri=adapter_uri,
            manifest=manifest,
            lineage=_dict_config(job.config.get("lineage")),
            compatibility=compatibility,
            scores=_scores_config(job.config.get("scores")),
            tags=_string_list(job.config.get("tags")),
            promoted=bool(job.config.get("promoted", False)),
        )
    ]



_TRANSCRIPT_AND_SELF_DEPLOYED_MODES = (
    "codex_subscription_transcript",
    "self-deployed",
)
_SELF_DEPLOYED_MODE = ("self-deployed",)
_DATASET_INPUT = ("dataset",)
_ADAPTER_INPUT = ("adapter",)
_OBJECT_CONFIG_SCHEMA: dict[str, Any] = {"type": "object", "additionalProperties": True}

METHOD_METADATA: dict[str, dict[str, Any]] = {
    "text_memory": {
        "method_id": "text_memory",
        "display_name": "Text Memory",
        "description": "Build text memory directly from dataset records.",
        "artifact_type": "text_memory",
        "visibility": "dev_kit",
        "visible_in_desktop": False,
        "input_requirements": _DATASET_INPUT,
        "supported_execution_modes": _SELF_DEPLOYED_MODE,
        "default_config": {},
        "config_schema": _OBJECT_CONFIG_SCHEMA,
        "stability_level": "stable",
    },
    "text_memory_reflector": {
        "method_id": "text_memory_reflector",
        "display_name": "Text Memory Reflector",
        "description": "Reflect over task trajectories to synthesize reusable text memory.",
        "artifact_type": "text_memory",
        "visibility": "ordinary_user",
        "visible_in_desktop": True,
        "input_requirements": _DATASET_INPUT,
        "supported_execution_modes": _TRANSCRIPT_AND_SELF_DEPLOYED_MODES,
        "default_config": {},
        "config_schema": _OBJECT_CONFIG_SCHEMA,
        "stability_level": "stable",
    },
    "text_memory_expel_reflector": {
        "method_id": "text_memory_expel_reflector",
        "display_name": "Text Memory ExpeL Reflector",
        "description": "Produce structured ExpeL-style text memory from success and failure traces.",
        "artifact_type": "text_memory",
        "visibility": "dev_kit",
        "visible_in_desktop": False,
        "input_requirements": _DATASET_INPUT,
        "supported_execution_modes": _TRANSCRIPT_AND_SELF_DEPLOYED_MODES,
        "default_config": {},
        "config_schema": _OBJECT_CONFIG_SCHEMA,
        "stability_level": "experimental",
    },
    "skill_bundle": {
        "method_id": "skill_bundle",
        "display_name": "Skill Bundle",
        "description": "Register a configured skill bundle artifact.",
        "artifact_type": "skill_bundle",
        "visibility": "dev_kit",
        "visible_in_desktop": False,
        "input_requirements": (),
        "supported_execution_modes": _SELF_DEPLOYED_MODE,
        "default_config": {},
        "config_schema": _OBJECT_CONFIG_SCHEMA,
        "stability_level": "stable",
    },
    "skill_bundle_reflector": {
        "method_id": "skill_bundle_reflector",
        "display_name": "Skill Bundle Reflector",
        "description": "Reflect over trajectories to synthesize a harness-loadable skill bundle.",
        "artifact_type": "skill_bundle",
        "visibility": "ordinary_user",
        "visible_in_desktop": True,
        "input_requirements": _DATASET_INPUT,
        "supported_execution_modes": _TRANSCRIPT_AND_SELF_DEPLOYED_MODES,
        "default_config": {},
        "config_schema": _OBJECT_CONFIG_SCHEMA,
        "stability_level": "stable",
    },
    "agent_system": {
        "method_id": "agent_system",
        "display_name": "Agent System",
        "description": "Register a configured agent-system instruction artifact.",
        "artifact_type": "agent_system",
        "visibility": "dev_kit",
        "visible_in_desktop": False,
        "input_requirements": (),
        "supported_execution_modes": _SELF_DEPLOYED_MODE,
        "default_config": {"target_path": DEFAULT_AGENT_SYSTEM_TARGET_PATH},
        "config_schema": _OBJECT_CONFIG_SCHEMA,
        "stability_level": "stable",
    },
    "agent_system_reflector": {
        "method_id": "agent_system_reflector",
        "display_name": "Agent System Reflector",
        "description": "Reflect over trajectories to synthesize improved agent instructions.",
        "artifact_type": "agent_system",
        "visibility": "ordinary_user",
        "visible_in_desktop": True,
        "input_requirements": _DATASET_INPUT,
        "supported_execution_modes": _TRANSCRIPT_AND_SELF_DEPLOYED_MODES,
        "default_config": {"target_path": DEFAULT_AGENT_SYSTEM_TARGET_PATH},
        "config_schema": _OBJECT_CONFIG_SCHEMA,
        "stability_level": "stable",
    },
    "agent_system_history_reflector": {
        "method_id": "agent_system_history_reflector",
        "display_name": "Agent System History Reflector",
        "description": "Use prior evolution rounds to synthesize the next agent-system artifact.",
        "artifact_type": "agent_system",
        "visibility": "dev_kit",
        "visible_in_desktop": False,
        "input_requirements": _DATASET_INPUT,
        "supported_execution_modes": _TRANSCRIPT_AND_SELF_DEPLOYED_MODES,
        "default_config": {"target_path": DEFAULT_AGENT_SYSTEM_TARGET_PATH},
        "config_schema": _OBJECT_CONFIG_SCHEMA,
        "stability_level": "experimental",
    },
    "agent_system_pareto_reflector": {
        "method_id": "agent_system_pareto_reflector",
        "display_name": "Agent System Pareto Reflector",
        "description": "Generate and select agent-system candidates with Pareto-style scoring.",
        "artifact_type": "agent_system",
        "visibility": "dev_kit",
        "visible_in_desktop": False,
        "input_requirements": _DATASET_INPUT,
        "supported_execution_modes": _TRANSCRIPT_AND_SELF_DEPLOYED_MODES,
        "default_config": {"target_path": DEFAULT_AGENT_SYSTEM_TARGET_PATH},
        "config_schema": _OBJECT_CONFIG_SCHEMA,
        "stability_level": "experimental",
    },
    "agent_system_gepa_reflector": {
        "method_id": "agent_system_gepa_reflector",
        "display_name": "Agent System GEPA Reflector",
        "description": "Generate agent-system prompt mutations using GEPA-style strategies.",
        "artifact_type": "agent_system",
        "visibility": "dev_kit",
        "visible_in_desktop": False,
        "input_requirements": _DATASET_INPUT,
        "supported_execution_modes": _TRANSCRIPT_AND_SELF_DEPLOYED_MODES,
        "default_config": {"target_path": DEFAULT_AGENT_SYSTEM_TARGET_PATH},
        "config_schema": _OBJECT_CONFIG_SCHEMA,
        "stability_level": "experimental",
    },
    "parametric_memory_register": {
        "method_id": "parametric_memory_register",
        "display_name": "Parametric Memory Register",
        "description": "Register an existing adapter as a parametric-memory artifact.",
        "artifact_type": "parametric_memory",
        "visibility": "dev_kit",
        "visible_in_desktop": False,
        "input_requirements": _ADAPTER_INPUT,
        "supported_execution_modes": _SELF_DEPLOYED_MODE,
        "default_config": {},
        "config_schema": _OBJECT_CONFIG_SCHEMA,
        "stability_level": "experimental",
    },
}

METHOD_REGISTRY: dict[str, EvolutionMethod] = {
    "text_memory": text_memory,
    "text_memory_reflector": text_memory_reflector,
    "text_memory_expel_reflector": text_memory_expel_reflector,
    "skill_bundle": skill_bundle,
    "skill_bundle_reflector": skill_bundle_reflector,
    "agent_system": agent_system,
    "agent_system_reflector": agent_system_reflector,
    "agent_system_history_reflector": agent_system_history_reflector,
    "agent_system_pareto_reflector": agent_system_pareto_reflector,
    "agent_system_gepa_reflector": agent_system_gepa_reflector,
    "parametric_memory_register": parametric_memory_register,
}


def _input_artifacts(
    job: WorkerClaimedJob,
    artifact_type: ArtifactType,
) -> list[WorkerClaimInputArtifact]:
    return [
        artifact
        for artifact in job.input_artifacts
        if artifact.type == artifact_type or artifact.type == artifact_type.value
    ]


def _first_input_artifact(
    job: WorkerClaimedJob,
    artifact_type: ArtifactType,
) -> WorkerClaimInputArtifact | None:
    for artifact in job.input_artifacts:
        if artifact.type == artifact_type or artifact.type == artifact_type.value:
            return artifact
    return None


def _read_dataset_artifact(
    dataset: WorkerClaimInputArtifact,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    manifest_path = _file_uri_to_path(dataset.uri)
    strict_snapshot = dataset.manifest_sha256 is not None
    manifest_bytes = _read_bounded_dataset_file(
        manifest_path,
        max_bytes=_MAX_DATASET_MANIFEST_BYTES,
        label="dataset manifest",
        strict_mode=strict_snapshot,
    )
    try:
        manifest_text = manifest_bytes.decode("utf-8")
        manifest = json.loads(manifest_text)
    except (UnicodeDecodeError, ValueError) as exc:
        raise ValueError("dataset manifest is not valid UTF-8 JSON") from exc
    if not isinstance(manifest, dict):
        raise ValueError("dataset manifest must be a JSON object")

    if strict_snapshot:
        if manifest_path.name != "manifest.json":
            raise ValueError("dataset manifest path is not canonical")
        canonical_manifest_bytes = json.dumps(
            manifest,
            indent=2,
            sort_keys=True,
            allow_nan=False,
        ).encode("utf-8")
        if manifest_bytes != canonical_manifest_bytes:
            raise ValueError("dataset manifest bytes are not canonical")
        if canonical_digest(manifest) != dataset.manifest_sha256:
            raise ValueError("dataset manifest digest differs from the worker snapshot")
        records_path = manifest_path.with_name("records.jsonl")
        manifest_records_byte_size = manifest.get("records_byte_size")
        manifest_records_sha256 = manifest.get("records_sha256")
        records_byte_size = (
            dataset.records_byte_size
            if manifest_records_byte_size is None
            else manifest_records_byte_size
        )
        records_sha256 = (
            dataset.records_sha256
            if manifest_records_sha256 is None
            else manifest_records_sha256
        )
        if (
            manifest.get("records_path") != records_path.name
            or manifest.get("records_uri") != records_path.as_uri()
            or type(records_byte_size) is not int
            or not 0 <= records_byte_size <= _MAX_DATASET_RECORDS_BYTES
            or not isinstance(records_sha256, str)
            or re.fullmatch(r"[0-9a-f]{64}", records_sha256) is None
            or (
                dataset.records_byte_size is not None
                and dataset.records_byte_size != records_byte_size
            )
            or (
                dataset.records_sha256 is not None
                and dataset.records_sha256 != records_sha256
            )
        ):
            raise ValueError("dataset records authority is invalid")
    else:
        records_path = _dataset_records_path(manifest_path, manifest)
        records_byte_size = None
        records_sha256 = None

    records_bytes = _read_bounded_dataset_file(
        records_path,
        max_bytes=_MAX_DATASET_RECORDS_BYTES,
        label="dataset records",
        strict_mode=strict_snapshot,
    )
    if strict_snapshot and (
        len(records_bytes) != records_byte_size
        or hashlib.sha256(records_bytes).hexdigest() != records_sha256
    ):
        raise ValueError("dataset records differ from the worker snapshot")

    records: list[dict[str, Any]] = []
    try:
        records_text = records_bytes.decode("utf-8")
        for line in records_text.splitlines():
            if not line.strip():
                continue
            record = json.loads(line)
            if not isinstance(record, dict):
                raise ValueError("dataset record must be a JSON object")
            records.append(record)
    except (UnicodeDecodeError, ValueError) as exc:
        raise ValueError("dataset records are not valid UTF-8 JSONL") from exc
    if strict_snapshot:
        canonical_records_bytes = "".join(
            json.dumps(
                record,
                sort_keys=True,
                allow_nan=False,
            )
            + "\n"
            for record in records
        ).encode("utf-8")
        if records_bytes != canonical_records_bytes:
            raise ValueError("dataset records bytes are not canonical")
    return manifest, records


def _dataset_records_path(manifest_path: Path, manifest: dict[str, Any]) -> Path:
    records_uri = manifest.get("records_uri")
    if isinstance(records_uri, str) and records_uri.startswith("file://"):
        return _file_uri_to_path(records_uri)
    records_path_value = manifest.get("records_path") or "records.jsonl"
    records_path = Path(str(records_path_value))
    if records_path.is_absolute() or records_path.name != str(records_path_value):
        raise ValueError("dataset records path is unsafe")
    return manifest_path.parent / records_path


def _read_bounded_dataset_file(
    path: Path,
    *,
    max_bytes: int,
    label: str,
    strict_mode: bool,
) -> bytes:
    path = Path(os.path.abspath(path))
    directory_flags = os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_DIRECTORY
    directory_descriptor = os.open(path.parent, directory_flags)
    try:
        directory_before = os.fstat(directory_descriptor)
        pathname_before = os.stat(path.parent, follow_symlinks=False)
        if (
            not stat.S_ISDIR(directory_before.st_mode)
            or (directory_before.st_dev, directory_before.st_ino)
            != (pathname_before.st_dev, pathname_before.st_ino)
        ):
            raise ValueError(f"{label} directory binding is invalid")
        descriptor = os.open(
            path.name,
            os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW,
            dir_fd=directory_descriptor,
        )
        try:
            before = os.fstat(descriptor)
            if (
                not stat.S_ISREG(before.st_mode)
                or before.st_uid != os.geteuid()
                or before.st_nlink != 1
                or before.st_size < 0
                or before.st_size > max_bytes
                or (
                    strict_mode
                    and stat.S_IMODE(before.st_mode) != 0o600
                )
            ):
                raise ValueError(f"{label} is not a bounded owned regular file")
            remaining = int(before.st_size)
            chunks: list[bytes] = []
            while remaining:
                chunk = os.read(descriptor, min(1024 * 1024, remaining))
                if not chunk:
                    raise ValueError(f"{label} ended before its declared size")
                chunks.append(chunk)
                remaining -= len(chunk)
            if os.read(descriptor, 1):
                raise ValueError(f"{label} grew while being read")
            after = os.fstat(descriptor)
            if (
                after.st_dev,
                after.st_ino,
                after.st_mode,
                after.st_uid,
                after.st_nlink,
                after.st_size,
            ) != (
                before.st_dev,
                before.st_ino,
                before.st_mode,
                before.st_uid,
                before.st_nlink,
                before.st_size,
            ):
                raise ValueError(f"{label} identity changed while being read")
        finally:
            os.close(descriptor)
        directory_after = os.fstat(directory_descriptor)
        pathname_after = os.stat(path.parent, follow_symlinks=False)
        if (
            directory_after.st_dev,
            directory_after.st_ino,
            directory_after.st_mode,
            directory_after.st_uid,
        ) != (
            directory_before.st_dev,
            directory_before.st_ino,
            directory_before.st_mode,
            directory_before.st_uid,
        ) or (
            pathname_after.st_dev,
            pathname_after.st_ino,
        ) != (
            directory_before.st_dev,
            directory_before.st_ino,
        ):
            raise ValueError(f"{label} directory binding changed while being read")
        return b"".join(chunks)
    except OSError as exc:
        raise ValueError(f"{label} cannot be read safely") from exc
    finally:
        os.close(directory_descriptor)



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


def _render_text_memory_reflection_prompt(
    *,
    job: WorkerClaimedJob,
    dataset: WorkerClaimInputArtifact,
    manifest: dict[str, Any],
    records: list[dict[str, Any]],
    reflected_records: list[dict[str, str]],
    prior_memory_texts: list[str],
) -> str:
    success_records = [record for record in reflected_records if record["kind"] == "success"]
    failure_records = [record for record in reflected_records if record["kind"] == "failure"]
    other_records = [record for record in reflected_records if record["kind"] == "observation"]
    feedback_records = [record for record in reflected_records if record.get("evolution_feedback")]
    source_dataset_ids = manifest.get("source_dataset_artifact_ids")
    if (
        isinstance(source_dataset_ids, list)
        and source_dataset_ids
        and all(isinstance(item, str) for item in source_dataset_ids)
    ):
        dataset_id_lines = [f"- dataset_artifact_ids: {', '.join(source_dataset_ids)}"]
    else:
        dataset_id_lines = [f"- dataset_artifact_id: {dataset.artifact_id}"]

    lines = [
        "# Text Memory Reflection Context",
        "",
        "Write reusable task memory as Markdown. The memory should help future rollouts "
        "by capturing reusable task memory, recurring failure modes, and validation habits.",
        "",
        f"- job_id: {job.job_id}",
        *dataset_id_lines,
        f"- dataset_name: {manifest.get('name') or dataset.name or 'unknown'}",
        f"- record_count: {len(records)}",
        f"- reflected_record_count: {len(reflected_records)}",
        "",
    ]
    if prior_memory_texts:
        lines.extend(["## Existing Text Memory", ""])
        for index, memory_text in enumerate(prior_memory_texts, start=1):
            lines.extend([f"### Memory {index}", "", memory_text.strip(), ""])
    _append_reflection_section(lines, "Successful Patterns", success_records, "observed")
    _append_reflection_section(lines, "Failures To Remember", failure_records, "failure_signal")
    _append_reflection_section(lines, "Additional Observations", other_records, "observed")
    _append_shared_evolution_feedback_section(lines, feedback_records)
    lines.extend(
        [
            "## Output Contract",
            "",
            "- Return only the Markdown for memory.md.",
            "- Keep the memory concise and reusable across related task instances.",
            "- Convert concrete trajectory evidence into general checks, validation habits, "
            "and failure reminders.",
            "- Do not copy held-out answers, article titles, exact source rows, exact expected "
            "outputs, or verifier-private records.",
            "",
        ]
    )
    return "\n".join(lines)


def _render_text_memory_expel_reflection_prompt(
    *,
    job: WorkerClaimedJob,
    dataset: WorkerClaimInputArtifact,
    manifest: dict[str, Any],
    records: list[dict[str, Any]],
    reflected_records: list[dict[str, str]],
    prior_memory_texts: list[str],
) -> str:
    base_prompt = _render_text_memory_reflection_prompt(
        job=job,
        dataset=dataset,
        manifest=manifest,
        records=records,
        reflected_records=reflected_records,
        prior_memory_texts=prior_memory_texts,
    )
    required_section_lines = [
        f"- `## {section}`" for section in _TEXT_MEMORY_EXPEL_REQUIRED_SECTIONS
    ]
    return "\n".join(
        [
            base_prompt.rstrip(),
            "",
            "## ExpeL / Reflexion Memory Synthesis",
            "",
            "Use an ExpeL-style process: compare successful and failed trajectories, "
            "extract lessons that transfer across future tasks, and retire advice that "
            "is contradicted or superseded by newer evidence. Treat prior text memory as "
            "candidate memory, not as ground truth.",
            "",
            "## Output Contract",
            "",
            "- Return only memory.md content; do not wrap it in a code fence.",
            "- Write structured textual memory with these exact level-2 headings:",
            *required_section_lines,
            "- `## Do` contains reusable actions that helped successful trajectories.",
            "- `## Avoid` contains recurring failure modes and stale behaviors to prevent.",
            "- `## Validate` contains concrete verification checks before final response.",
            "- `## When Applicable` contains conditional triggers for applying the memory.",
            "- `## Retired Or Superseded` names prior memory or habits that should no "
            "longer be followed.",
            "- Ground every item in the supplied trajectories or existing memory.",
            "- Do not copy held-out answers, article titles, exact source rows, exact "
            "expected outputs, or verifier-private records.",
            "",
        ]
    )


def _render_skill_bundle_reflection_prompt(
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

    lines = [
        "# Skill Bundle Reflection Context",
        "",
        "Write a Codex skill bundle entrypoint. For this first implementation, return "
        "only the Markdown content for SKILL.md.",
        "",
        f"- job_id: {job.job_id}",
        f"- dataset_artifact_id: {dataset.artifact_id}",
        f"- dataset_name: {manifest.get('name') or dataset.name or 'unknown'}",
        f"- record_count: {len(records)}",
        f"- reflected_record_count: {len(reflected_records)}",
        "",
    ]
    if base_text.strip():
        lines.extend(["## Existing Skill Bundle", "", base_text.strip(), ""])
    _append_reflection_section(lines, "Successful Patterns", success_records, "observed")
    _append_reflection_section(lines, "Failures To Avoid", failure_records, "failure_signal")
    _append_reflection_section(lines, "Additional Observations", other_records, "observed")
    _append_shared_evolution_feedback_section(lines, feedback_records)
    lines.extend(
        [
            "## Output Contract",
            "",
            "- Return only SKILL.md content; do not wrap it in a code fence.",
            "- Include YAML frontmatter with name and description when useful.",
            "- Make the skill trigger-oriented: state when to use it, what to inspect, "
            "what helper checks to run, and what final validation proves completion.",
            "- Tools are part of the skill bundle. If a future version needs helper scripts, "
            "describe the helper behavior in SKILL.md rather than creating a separate "
            "tool artifact type.",
            "- Do not copy held-out answers, article titles, exact source rows, exact expected "
            "outputs, or verifier-private records.",
            "",
        ]
    )
    return "\n".join(lines)


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
            "## Agent-System Rule Quality Gate",
            "",
            "- Do not copy exact held-out literals, source filenames, source sheet names, row numbers, article titles, answer counts, sequences, or reference records.",
            "- Each methodology rule should be general across task instances but concrete enough to include a trigger, action, and validation check.",
            "- Replace slogans such as broad coverage reminders with executable checks, for example recursive file-level source inventory, general structured evidence formats such as tables/spreadsheets/CSV/TSV/XLS/XLSX/supplementary files, and per-source final validation when the task involves package-like inputs.",
            "",
        ]
    )
    return "\n".join(lines)


def _history_reflection_rounds(
    dataset_artifacts: list[WorkerClaimInputArtifact],
    *,
    max_records_per_round: int,
) -> list[dict[str, Any]]:
    rounds: list[dict[str, Any]] = []
    for index, dataset in enumerate(dataset_artifacts, start=1):
        manifest, records = _read_dataset_artifact(dataset)
        reflected_records = _reflection_records(
            records,
            max_records=max_records_per_round,
        )
        rounds.append(
            {
                "artifact": dataset,
                "manifest": manifest,
                "records": records,
                "reflected_records": reflected_records,
                "round": _history_round_number(manifest, records=records, default=index),
                "metrics": _history_metrics(manifest, records=records),
            }
        )
    return sorted(rounds, key=lambda history_round: history_round["round"])


def _render_agent_system_history_reflection_prompt(
    *,
    job: WorkerClaimedJob,
    rounds: list[dict[str, Any]],
    base_text: str,
) -> str:
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

    best_round = _best_history_round(rounds)
    latest_round = rounds[-1]
    lines.extend(
        [
            "## Multi-Round Evolution History",
            "",
            f"- job_id: {job.job_id}",
            f"- round_count: {len(rounds)}",
            f"- best_round: Round {best_round['round']} f1={_format_metric(best_round['metrics'].get('f1'))}",
            f"- latest_round: Round {latest_round['round']} f1={_format_metric(latest_round['metrics'].get('f1'))}",
            "- reflector_goal: preserve stable improvements, recover regressions, and update methodology rather than memorizing held-out records.",
            "",
        ]
    )

    previous_metrics: dict[str, float | None] | None = None
    for history_round in rounds:
        metrics = history_round["metrics"]
        delta_f1 = _metric_delta(metrics.get("f1"), None if previous_metrics is None else previous_metrics.get("f1"))
        delta_recall = _metric_delta(
            metrics.get("recall"),
            None if previous_metrics is None else previous_metrics.get("recall"),
        )
        delta_precision = _metric_delta(
            metrics.get("precision"),
            None if previous_metrics is None else previous_metrics.get("precision"),
        )
        status = _round_delta_status(delta_f1)
        lines.extend(
            [
                f"### Round {history_round['round']}",
                "",
                f"- dataset_artifact_id: {history_round['artifact'].artifact_id}",
                f"- dataset_name: {history_round['manifest'].get('name') or history_round['artifact'].name or 'unknown'}",
                f"- agent_system_artifact_id: {history_round['manifest'].get('agent_system_artifact_id') or 'unknown'}",
                (
                    "- metrics: "
                    f"precision={_format_metric(metrics.get('precision'))} "
                    f"recall={_format_metric(metrics.get('recall'))} "
                    f"f1={_format_metric(metrics.get('f1'))} "
                    f"tp={_format_count(metrics.get('true_positive'))} "
                    f"fp={_format_count(metrics.get('false_positive'))} "
                    f"fn={_format_count(metrics.get('false_negative'))} "
                    f"duplicates={_format_count(metrics.get('duplicate_predictions'))}"
                ),
                (
                    "- delta_from_previous: "
                    f"delta_precision={_format_delta(delta_precision)} "
                    f"delta_recall={_format_delta(delta_recall)} "
                    f"delta_f1={_format_delta(delta_f1)} "
                    f"status={status}"
                ),
                f"- reflected_record_count: {len(history_round['reflected_records'])}",
                "",
            ]
        )
        _append_round_reflection_sections(lines, history_round)
        previous_metrics = metrics

    human_feedback_summary = _render_human_feedback_summary(
        _human_feedback_from_records(
            [record for history_round in rounds for record in history_round["records"]]
        )
    )
    if human_feedback_summary:
        lines.extend([human_feedback_summary, ""])

    lines.extend(
        [
            "## History-Aware Operating Rules",
            "",
            "- Preserve stable improvements from better rounds before adding new rules.",
            "- Compare each proposed rule against earlier rounds; do not keep a rule that explains a later regression unless it also has stronger counter-evidence.",
            "- Treat negative metric deltas as regression evidence and identify which methodology changed or disappeared.",
            "- Use shared evaluator feedback only as sanitized methodology guidance; do not copy exact held-out literals, article titles, row identifiers, filenames, or tables.",
            "- Prefer rules that improve component boundaries, canonical article/package identifiers, other task-provided source identifiers, deduplication, and final-output discipline across rounds.",
            "",
            "## Agent-System Rule Quality Gate",
            "",
            "- Do not copy exact held-out literals, source filenames, source sheet names, row numbers, article titles, answer counts, sequences, or reference records.",
            "- Each methodology rule should be general across task instances but concrete enough to include a trigger, action, and validation check.",
            "- Replace slogans such as broad coverage reminders with executable checks, for example recursive file-level source inventory, general structured evidence formats such as tables/spreadsheets/CSV/TSV/XLS/XLSX/supplementary files, and per-source final validation when the task involves package-like inputs.",
            "",
        ]
    )
    return "\n".join(lines)


def _append_round_reflection_sections(
    lines: list[str],
    history_round: dict[str, Any],
) -> None:
    reflected_records = history_round["reflected_records"]
    success_records = [record for record in reflected_records if record["kind"] == "success"]
    failure_records = [record for record in reflected_records if record["kind"] == "failure"]
    other_records = [record for record in reflected_records if record["kind"] == "observation"]
    feedback_records = [record for record in reflected_records if record.get("evolution_feedback")]

    round_label = f"Round {history_round['round']}"
    _append_reflection_section(lines, f"{round_label} Successful Patterns", success_records, "observed")
    _append_reflection_section(lines, f"{round_label} Failures To Avoid", failure_records, "failure_signal")
    _append_reflection_section(lines, f"{round_label} Additional Observations", other_records, "observed")
    _append_shared_evolution_feedback_section(lines, feedback_records)


def _history_round_number(
    manifest: dict[str, Any],
    *,
    records: list[dict[str, Any]] | None = None,
    default: int,
) -> int:
    for key in ("round", "round_number", "evolution_round", "iteration"):
        value = manifest.get(key)
        try:
            return int(value)
        except (TypeError, ValueError):
            continue
    for record in records or []:
        metadata = _record_session_metadata(record)
        for key in ("round", "round_number", "evolution_round", "iteration"):
            value = metadata.get(key)
            try:
                return int(value)
            except (TypeError, ValueError):
                continue
        value = record.get("rollout_step")
        try:
            return int(value)
        except (TypeError, ValueError):
            pass
        policy_version = record.get("policy_version")
        if isinstance(policy_version, str):
            match = re.search(r"(?:round|iteration)[-_ ]*(\d+)", policy_version, re.IGNORECASE)
            if match:
                return int(match.group(1))
    return default


def _history_metrics(
    manifest: dict[str, Any],
    *,
    records: list[dict[str, Any]] | None = None,
) -> dict[str, float | None]:
    metrics = manifest.get("metrics")
    if not isinstance(metrics, dict):
        metrics = manifest.get("summary")
    if not isinstance(metrics, dict):
        metrics = {}
    parsed = {
        "precision": _metric_float(metrics, "precision"),
        "recall": _metric_float(metrics, "recall"),
        "f1": _metric_float(metrics, "f1"),
        "true_positive": _metric_float(metrics, "true_positive", "tp"),
        "false_positive": _metric_float(metrics, "false_positive", "fp"),
        "false_negative": _metric_float(metrics, "false_negative", "fn"),
        "duplicate_predictions": _metric_float(metrics, "duplicate_predictions", "duplicates"),
    }
    if any(value is not None for value in parsed.values()):
        return parsed
    return _history_metrics_from_records(records or [])


def _record_session_metadata(record: dict[str, Any]) -> dict[str, Any]:
    payload = record.get("payload")
    if isinstance(payload, dict):
        session_result = payload.get("session_result")
        if isinstance(session_result, dict):
            metadata = session_result.get("metadata")
            if isinstance(metadata, dict):
                return metadata
    return {}


def _history_metrics_from_records(records: list[dict[str, Any]]) -> dict[str, float | None]:
    metrics: dict[str, float | None] = {
        "precision": None,
        "recall": None,
        "f1": None,
        "true_positive": None,
        "false_positive": None,
        "false_negative": None,
        "duplicate_predictions": None,
    }
    for record in records:
        feedback = _record_evolution_feedback(record)
        aggregate_line = _aggregate_fit_line(feedback)
        if not aggregate_line:
            continue
        for key in ("precision", "recall", "f1"):
            value = _metric_from_text(aggregate_line, key)
            if value is not None:
                metrics[key] = value
        if any(metrics[key] is not None for key in ("precision", "recall", "f1")):
            return metrics
    return metrics


def _aggregate_fit_line(feedback: str) -> str:
    match = re.search(r"Aggregate fit:[^\n\r]*", feedback)
    return match.group(0) if match else ""


def _metric_from_text(text: str, key: str) -> float | None:
    match = re.search(rf"\b{re.escape(key)}=([-+]?\d+(?:\.\d+)?)", text)
    if not match:
        return None
    try:
        return float(match.group(1))
    except ValueError:
        return None


def _metric_float(metrics: dict[str, Any], *keys: str) -> float | None:
    for key in keys:
        value = metrics.get(key)
        if isinstance(value, int | float):
            return float(value)
        if isinstance(value, str):
            try:
                return float(value)
            except ValueError:
                continue
    return None


def _best_history_round(rounds: list[dict[str, Any]]) -> dict[str, Any]:
    return max(
        rounds,
        key=lambda history_round: (
            history_round["metrics"].get("f1")
            if history_round["metrics"].get("f1") is not None
            else float("-inf")
        ),
    )


def _metric_delta(current: float | None, previous: float | None) -> float | None:
    if current is None or previous is None:
        return None
    return current - previous


def _round_delta_status(delta_f1: float | None) -> str:
    if delta_f1 is None:
        return "baseline"
    if delta_f1 < -0.01:
        return "regression"
    if delta_f1 > 0.01:
        return "improvement"
    return "stable"


def _format_metric(value: float | None) -> str:
    return "unknown" if value is None else f"{value:.3f}"


def _format_count(value: float | None) -> str:
    return "unknown" if value is None else str(int(value))


def _format_delta(value: float | None) -> str:
    return "n/a" if value is None else f"{value:+.3f}"


def _pareto_candidate_strategies(job: WorkerClaimedJob) -> list[str]:
    configured = job.config.get("candidate_strategies")
    if isinstance(configured, list):
        strategies = [str(item).strip() for item in configured if str(item).strip()]
        if strategies:
            return strategies
    count = _int_config(job.config.get("candidate_count"), 3)
    defaults = [
        "precision_guarded",
        "recall_recovery",
        "provenance_guarded",
        "anti_regression",
        "balanced_pareto",
    ]
    return defaults[: max(1, min(count, len(defaults)))]


def _render_agent_system_pareto_candidate_prompt(
    *,
    job: WorkerClaimedJob,
    rounds: list[dict[str, Any]],
    base_text: str,
    strategy: str,
    index: int,
    total: int,
) -> str:
    history_prompt = _render_agent_system_history_reflection_prompt(
        job=job,
        rounds=rounds,
        base_text=base_text,
    )
    return (
        f"{history_prompt}\n"
        "## Pareto Candidate Generation\n\n"
        "Use all historical trajectories, round-level metric deltas, and sanitized "
        "shared evaluator feedback to propose one candidate AGENTS.md update. "
        "This backend will generate multiple candidates, audit them, and let a "
        "promotion gate select the candidate; do not assume this candidate will "
        "be accepted automatically.\n\n"
        f"- Candidate index: {index} of {total}\n"
        f"- Candidate strategy: {strategy}\n"
        "- Promotion gate: prefer candidates that preserve the best prior round, "
        "avoid coverage collapse, avoid over-generation, preserve task-provided "
        "canonical source identifiers, and do not copy held-out literals or task "
        "answers.\n"
        "- Candidate requirements: write general methodology, not task answers; each "
        "rule should include a trigger, an action, and a validation check.\n\n"
        "Return only the candidate Markdown agent-system instruction file."
    )


def _gepa_mutation_strategies(job: WorkerClaimedJob) -> list[str]:
    configured = job.config.get("mutation_strategies")
    if isinstance(configured, list):
        strategies = [str(item).strip() for item in configured if str(item).strip()]
        if strategies:
            return strategies
    count = _int_config(job.config.get("candidate_count"), 3)
    defaults = [
        "failure_targeted",
        "verification_gate",
        "preservation_gate",
        "anti_regression",
        "edge_case_corpus",
    ]
    return defaults[: max(1, min(count, len(defaults)))]


def _render_agent_system_gepa_candidate_prompt(
    *,
    job: WorkerClaimedJob,
    rounds: list[dict[str, Any]],
    base_text: str,
    strategy: str,
    index: int,
    total: int,
) -> str:
    history_prompt = _render_agent_system_history_reflection_prompt(
        job=job,
        rounds=rounds,
        base_text=base_text,
    )
    return (
        f"{history_prompt}\n"
        "## GEPA Candidate Mutation\n\n"
        "Generate one candidate AGENTS.md mutation for a GEPA-style optimizer. "
        "Use trajectory evidence, verifier failure text, and sanitized evaluator "
        "feedback to create concrete methodology rules. This candidate will be "
        "evaluated externally against the task verifier, so optimize for behavior "
        "that can be executed and checked rather than for sounding comprehensive.\n\n"
        f"- Candidate index: {index} of {total}\n"
        f"- Mutation strategy: {strategy}\n"
        "- Reflection rule: each added instruction must name a trigger, an action, "
        "and a validation check.\n"
        "- Leakage rule: do not copy exact held-out literals, filenames, source row "
        "numbers, task answers, or verifier-specific hidden records.\n"
        "- Selection rule: prefer candidates that directly address observed verifier "
        "failures while preserving successful prior behavior.\n\n"
        "Return only the candidate Markdown agent-system instruction file."
    )


def _pareto_candidate_evaluation(
    job: WorkerClaimedJob,
    strategy: str,
    *,
    index: int,
) -> dict[str, float]:
    evaluations = job.config.get("candidate_evaluations")
    value: Any = None
    if isinstance(evaluations, dict):
        value = evaluations.get(strategy)
        if value is None:
            value = evaluations.get(str(index))
    elif isinstance(evaluations, list) and 0 <= index - 1 < len(evaluations):
        value = evaluations[index - 1]
    if not isinstance(value, dict):
        return {}

    parsed: dict[str, float] = {}
    for key in (
        "precision",
        "recall",
        "f1",
        "true_positive",
        "false_positive",
        "false_negative",
        "duplicate_predictions",
        "prediction_to_reference_ratio",
        "predicted_to_gold_ratio",
        "output_ratio",
    ):
        number = _float_config(value.get(key), None)
        if number is not None:
            parsed[key] = number
    return parsed


def _pareto_candidate_gate_failures(
    *,
    job: WorkerClaimedJob,
    rounds: list[dict[str, Any]],
    candidate_evaluation: dict[str, float],
    static_score: float,
) -> list[str]:
    gate = _dict_config(job.config.get("promotion_gate"))
    failures: list[str] = []
    if gate.get("requires_external_evaluation") and not candidate_evaluation:
        failures.append("missing_external_evaluation")

    max_ratio = _float_config(gate.get("max_prediction_to_reference_ratio"), None)
    ratio = _candidate_prediction_ratio(candidate_evaluation)
    if max_ratio is not None and ratio is not None and ratio > max_ratio:
        failures.append("prediction_to_reference_ratio")

    best_f1 = _best_history_round(rounds)["metrics"].get("f1")
    candidate_f1 = candidate_evaluation.get("f1")
    max_regression = _float_config(gate.get("max_f1_regression"), 0.01)
    if (
        best_f1 is not None
        and candidate_f1 is not None
        and max_regression is not None
        and candidate_f1 < best_f1 - max_regression
    ):
        failures.append("f1_regression")

    for metric_name in ("precision", "recall", "f1"):
        minimum = _float_config(gate.get(f"min_{metric_name}"), None)
        value = candidate_evaluation.get(metric_name)
        if minimum is not None and value is not None and value < minimum:
            failures.append(f"{metric_name}_below_minimum")

    min_static_score = _float_config(gate.get("min_static_score"), None)
    if min_static_score is not None and static_score < min_static_score:
        failures.append("static_guardrail_score")

    return list(dict.fromkeys(failures))


def _candidate_prediction_ratio(candidate_evaluation: dict[str, float]) -> float | None:
    for key in (
        "prediction_to_reference_ratio",
        "predicted_to_gold_ratio",
        "output_ratio",
    ):
        value = candidate_evaluation.get(key)
        if value is not None:
            return value
    return None


def _agent_system_static_guardrail_score(text: str) -> float:
    normalized = text.lower()
    score = 0.0
    actionable_count = sum(
        1 for line in _agent_system_rule_lines(text) if _is_actionable_rule(line)
    )
    score += min(actionable_count, 3)
    guardrail_groups = [
        {"canonical", "source-id", "source id", "article_id", "article id"},
        {"provenance", "evidence", "source"},
        {"recursive", "inventory", "every file", "input root"},
        {"precision", "false positive", "over-generation", "unsupported"},
        {"recall", "coverage", "false negative", "collapse"},
        {"verify", "validate", "audit", "check"},
        {"leakage", "held-out", "literal", "answer"},
        {"spreadsheet", "workbook", "csv", "tsv", "xls", "xlsx", "table"},
    ]
    for group in guardrail_groups:
        if any(term in normalized for term in group):
            score += 1
    return score


def _pareto_candidate_selection_score(
    *,
    evaluation: dict[str, float],
    static_score: float,
    gate_failures: list[str],
) -> float:
    if evaluation:
        score = (
            evaluation.get("f1", 0.0) * 1000
            + evaluation.get("precision", 0.0) * 100
            + evaluation.get("recall", 0.0) * 10
        )
    else:
        score = static_score
    if gate_failures:
        score -= 100000 + len(gate_failures) * 1000
    return score


def _select_pareto_candidate(candidates: list[dict[str, Any]]) -> dict[str, Any]:
    if not candidates:
        raise ValueError("agent_system_pareto_reflector generated no candidates")
    return max(candidates, key=lambda candidate: candidate["selection_score"])


def _pareto_candidate_manifest_summary(candidate: dict[str, Any]) -> dict[str, Any]:
    return {
        "index": candidate["index"],
        "strategy": candidate["strategy"],
        "evaluation": candidate["evaluation"],
        "static_score": candidate["static_score"],
        "gate_passed": candidate["gate_passed"],
        "gate_failures": candidate["gate_failures"],
        "selection_score": candidate["selection_score"],
    }


def _pareto_candidate_report(
    candidate: dict[str, Any],
    *,
    output_dir: Path,
) -> dict[str, Any]:
    summary = _pareto_candidate_manifest_summary(candidate)
    summary["markdown_path"] = Path(candidate["markdown_path"]).relative_to(output_dir).as_posix()
    summary["audit"] = candidate["audit"]
    return summary


def _pareto_promotion_gate_report(
    job: WorkerClaimedJob,
    selected: dict[str, Any],
) -> dict[str, Any]:
    gate = _dict_config(job.config.get("promotion_gate"))
    return {
        "passed": selected["gate_passed"],
        "failures": selected["gate_failures"],
        "selected_strategy": selected["strategy"],
        "requires_external_evaluation": bool(gate.get("requires_external_evaluation", False)),
        "max_prediction_to_reference_ratio": _float_config(
            gate.get("max_prediction_to_reference_ratio"),
            None,
        ),
        "max_f1_regression": _float_config(gate.get("max_f1_regression"), 0.01),
    }


def _pareto_selected_scores(
    selected: dict[str, Any],
    *,
    job: WorkerClaimedJob,
) -> dict[str, float]:
    scores = _scores_config(job.config.get("scores"))
    for key in ("precision", "recall", "f1"):
        value = selected["evaluation"].get(key)
        if value is not None:
            scores[f"candidate_{key}"] = float(value)
    scores["static_guardrail_score"] = float(selected["static_score"])
    return scores


def _reflector_promotion_support(
    job: WorkerClaimedJob,
    *,
    method: str,
    artifact_kind: str,
    dataset_ids: list[str],
    record_count: int,
    reflected_record_count: int | None = None,
    success_count: int | None = None,
    failure_count: int | None = None,
    extra_findings: list[str] | None = None,
    proposed_changes: list[str],
    expected_benefits: list[str],
    risks: list[str],
    validation_checks: list[str],
) -> dict[str, list[str]]:
    findings = [
        (
            f"{method} reviewed {record_count} records from "
            f"{len(dataset_ids)} dataset artifact(s): {', '.join(dataset_ids)}."
        )
    ]
    if reflected_record_count is not None:
        findings.append(f"Selected {reflected_record_count} records for reflection.")
    if success_count is not None or failure_count is not None:
        findings.append(
            f"Selected trajectory mix: {success_count or 0} success records and "
            f"{failure_count or 0} failure records."
        )
    if extra_findings:
        findings.extend(extra_findings)

    support = {
        "trajectory_findings": findings,
        "proposed_changes": proposed_changes,
        "expected_benefits": expected_benefits,
        "risks": risks,
        "validation_checks": validation_checks,
    }
    configured = _dict_config(job.config.get("promotion_support"))
    for field in _PROMOTION_SUPPORT_FIELDS:
        value = configured.get(field)
        if _promotion_support_field_present(value):
            support[field] = _promotion_support_values(value)
    if extra_findings:
        for finding in extra_findings:
            if (
                _is_feedback_inclusion_finding(finding)
                and finding not in support["trajectory_findings"]
            ):
                support["trajectory_findings"].append(finding)
    support.setdefault("proposed_changes", []).append(
        f"Artifact kind under review: {artifact_kind}."
    )
    return support


def _is_human_feedback_inclusion_finding(value: str) -> bool:
    return (
        value.startswith("Included ")
        and " human feedback item(s) from prior reviews." in value
    )


def _is_shared_evolution_feedback_inclusion_finding(value: str) -> bool:
    return (
        value.startswith("Included ")
        and " shared evolution feedback item(s) from prior evaluator signals." in value
    )


def _is_feedback_inclusion_finding(value: str) -> bool:
    return _is_human_feedback_inclusion_finding(
        value
    ) or _is_shared_evolution_feedback_inclusion_finding(value)


def _shared_evolution_feedback_inclusion_finding(feedback_ids: list[str]) -> str:
    return (
        f"Included {len(feedback_ids)} shared evolution feedback item(s) "
        "from prior evaluator signals."
    )


def _promotion_support_field_present(value: object) -> bool:
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, list | tuple):
        return any(_promotion_support_field_present(item) for item in value)
    return value is not None


def _promotion_support_values(value: object) -> list[str]:
    if isinstance(value, str):
        return [value.strip()] if value.strip() else []
    if isinstance(value, list | tuple):
        return [
            str(item).strip()
            for item in value
            if str(item).strip()
        ]
    return [str(value)]


def _generate_agent_system_reflection(prompt: str, llm_config: dict[str, Any]) -> str:
    return _generate_reflector_markdown(
        prompt,
        llm_config,
        system_message=(
            "You are a reflector for an agent system. Read prior task trajectories, "
            "preserve useful existing instructions, and produce a concise Markdown "
            "agent-system instruction file. Return only the Markdown file content."
        ),
        codex_prompt=_codex_cli_reflector_prompt(prompt),
        error_context="agent_system_reflector",
        temp_prefix="openevo-agent-system-reflector-",
    )


def _generate_reflector_markdown(
    prompt: str,
    llm_config: dict[str, Any],
    *,
    system_message: str,
    codex_prompt: str,
    error_context: str,
    temp_prefix: str,
) -> str:
    provider = llm_config["provider"]
    if provider == _REFLECTOR_PROVIDER_CODEX_CLI:
        proxy_base_url = core_gateway_base_url_from_environment()
        if proxy_base_url is not None:
            return core_gateway_chat_completion(
                proxy_base_url=proxy_base_url,
                model=str(llm_config["model"]),
                system_instruction=system_message,
                prompt=prompt,
                timeout_seconds=float(llm_config["timeout_seconds"]),
                temperature=float(llm_config["temperature"]),
                max_tokens=llm_config.get("max_tokens"),
            )
        return _generate_agent_system_reflection_with_codex_cli(
            llm_config,
            prompt_input=codex_prompt,
            error_context=error_context,
            temp_prefix=temp_prefix,
        )
    if provider == _REFLECTOR_PROVIDER_OPENAI_CHAT:
        return _generate_agent_system_reflection_with_openai_chat(
            prompt,
            llm_config,
            system_message=system_message,
            error_context=error_context,
        )
    raise ValueError(f"Unsupported {error_context} LLM provider: {provider}")


def _generate_audited_agent_system_reflection(
    prompt: str,
    llm_config: dict[str, Any],
    *,
    job: WorkerClaimedJob,
    manifests: list[dict[str, Any]],
) -> tuple[str, dict[str, Any]]:
    audit_config = _agent_system_audit_config(job)
    if audit_config.get("enabled") is False:
        return _generate_agent_system_reflection(prompt, llm_config), {
            "enabled": False,
            "repair_count": 0,
            "finding_count": 0,
        }

    max_repairs = _int_config(audit_config.get("max_repair_attempts"), 2)
    forbidden_literals = _agent_system_forbidden_literals(job, manifests)
    content = _generate_agent_system_reflection(prompt, llm_config)
    findings = _audit_agent_system_markdown(
        content,
        forbidden_literals=forbidden_literals,
    )
    repair_count = 0
    while findings and repair_count < max_repairs:
        repair_prompt = _render_agent_system_audit_repair_prompt(
            original_prompt=prompt,
            candidate_markdown=content,
            findings=findings,
            forbidden_literals=forbidden_literals,
        )
        content = _generate_agent_system_reflection(repair_prompt, llm_config)
        repair_count += 1
        findings = _audit_agent_system_markdown(
            content,
            forbidden_literals=forbidden_literals,
        )

    if findings:
        finding_summary = "; ".join(finding["message"] for finding in findings[:5])
        raise ValueError(f"agent_system_reflector output failed audit: {finding_summary}")

    return content, {
        "enabled": True,
        "repair_count": repair_count,
        "finding_count": 0,
    }


def _generate_agent_system_reflection_with_openai_chat(
    prompt: str,
    llm_config: dict[str, Any],
    *,
    system_message: str,
    error_context: str,
) -> str:
    payload: dict[str, Any] = {
        "model": llm_config["model"],
        "messages": [
            {
                "role": "system",
                "content": system_message,
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
        raise ValueError(f"{error_context} LLM returned empty content")
    return content.strip()


def _generate_agent_system_reflection_with_codex_cli(
    llm_config: dict[str, Any],
    *,
    prompt_input: str,
    error_context: str,
    temp_prefix: str,
) -> str:
    with tempfile.TemporaryDirectory(prefix=temp_prefix) as tmp:
        tmpdir = Path(tmp)
        output_path = tmpdir / "last-message.md"
        args = [
            str(llm_config["codex_bin"]),
            "exec",
            "--json",
            "--ignore-user-config",
            "--ephemeral",
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
            raise ValueError(f"{error_context} codex_cli failed: {detail}") from exc
        except subprocess.TimeoutExpired as exc:
            raise ValueError(f"{error_context} codex_cli timed out") from exc

        content = output_path.read_text(encoding="utf-8") if output_path.exists() else ""
    if not content.strip():
        raise ValueError(f"{error_context} codex_cli returned empty content")
    return content.strip()


def _codex_cli_reflector_prompt(prompt: str) -> str:
    return (
        "Return only the Markdown agent-system instruction file. "
        "Do not include explanations, code fences, or surrounding commentary.\n\n"
        "You are a reflector for an agent system. Read prior task trajectories, "
        "preserve useful existing instructions, and produce a concise Markdown "
        "agent-system instruction file. Every new methodology rule should be general "
        "enough to transfer across tasks and concrete enough to describe a trigger, "
        "an action, and a validation check.\n\n"
        f"{prompt}"
    )


def _codex_cli_text_memory_reflector_prompt(prompt: str) -> str:
    return (
        "Return only the Markdown memory.md file. "
        "Do not include explanations, code fences, or surrounding commentary.\n\n"
        "You are a reflector for text memory. Read prior task trajectories and produce "
        "concise reusable memory for future sessions. Focus on recurring failure modes, "
        "successful task habits, and validation checks that transfer across tasks. "
        "Do not copy exact held-out literals or task answers.\n\n"
        f"{prompt}"
    )


def _codex_cli_skill_bundle_reflector_prompt(prompt: str) -> str:
    return (
        "Return only SKILL.md content for a Codex skill bundle. "
        "Do not include explanations, code fences, or surrounding commentary.\n\n"
        "You are a reflector for a Codex skill bundle. Read prior task trajectories "
        "and produce a concise skill entrypoint with a clear trigger, workflow, and "
        "verification guidance. Do not create a separate tool artifact type and do "
        "not copy exact held-out literals or task answers.\n\n"
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


def _agent_system_audit_config(job: WorkerClaimedJob) -> dict[str, Any]:
    value = job.config.get("agent_system_audit")
    return value if isinstance(value, dict) else {}


def _agent_system_forbidden_literals(
    job: WorkerClaimedJob,
    manifests: list[dict[str, Any]],
) -> list[tuple[str, str]]:
    literals: list[tuple[str, str]] = []
    for source in (job.config, _agent_system_audit_config(job), *manifests):
        _collect_forbidden_literals(source, literals)
    return _unique_forbidden_literals(literals)


def _redact_generic_reflector_prompt(
    prompt: str,
    *,
    job: WorkerClaimedJob,
    manifests: list[dict[str, Any]],
) -> str:
    forbidden_literals = _agent_system_forbidden_literals(job, manifests)
    if not forbidden_literals:
        return prompt
    return _redact_forbidden_literals(prompt, forbidden_literals)


def _guard_generic_reflector_output(
    markdown: str,
    *,
    job: WorkerClaimedJob,
    manifests: list[dict[str, Any]],
) -> tuple[str, dict[str, Any]]:
    forbidden_literals = _agent_system_forbidden_literals(job, manifests)
    findings = _forbidden_literal_findings(markdown, forbidden_literals)
    if not findings:
        return (
            markdown,
            {
                "finding_count": 0,
                "redaction_count": 0,
                "remaining_finding_count": 0,
                "findings": [],
            },
        )

    redacted = _redact_forbidden_literals(markdown, forbidden_literals)
    remaining_findings = _forbidden_literal_findings(redacted, forbidden_literals)
    if remaining_findings:
        raise ValueError(
            "reflector output still contains protected literals after redaction"
        )
    return (
        redacted,
        {
            "finding_count": len(findings),
            "redaction_count": 1,
            "remaining_finding_count": 0,
            "findings": findings,
        },
    )


def _missing_text_memory_expel_sections(markdown: str) -> list[str]:
    return [
        section
        for section in _TEXT_MEMORY_EXPEL_REQUIRED_SECTIONS
        if not re.search(
            rf"^##\s+{re.escape(section)}\s*#*\s*$",
            markdown,
            flags=re.MULTILINE,
        )
    ]


def _complete_text_memory_expel_sections(
    markdown: str,
) -> tuple[str, dict[str, Any]]:
    missing = _missing_text_memory_expel_sections(markdown)
    if not missing:
        return markdown, {"added_sections": [], "applied": False}

    lines = [markdown.rstrip()]
    for section in missing:
        lines.extend(
            [
                "",
                f"## {section}",
                f"- {_TEXT_MEMORY_EXPEL_EMPTY_SECTION_ITEMS[section]}",
            ]
        )
    return "\n".join(lines), {"added_sections": missing, "applied": True}


def _require_text_memory_expel_sections(markdown: str) -> None:
    missing = _missing_text_memory_expel_sections(markdown)
    if missing:
        raise ValueError(
            "text_memory_expel_reflector missing required memory sections: "
            + ", ".join(missing)
        )


_FORBIDDEN_LITERAL_KEYS = {
    "article_id",
    "article_ids",
    "article_title",
    "article_titles",
    "source_file",
    "source_files",
    "source_sheet",
    "source_sheets",
    "source_row",
    "source_rows",
    "sequence",
    "sequences",
}


def _collect_forbidden_literals(
    value: Any,
    literals: list[tuple[str, str]],
    *,
    kind: str = "literal",
    protected_context: bool = False,
) -> None:
    if isinstance(value, str):
        text = value.strip()
        if text:
            literals.append((kind, text))
        return
    if isinstance(value, int | float) and not isinstance(value, bool):
        if protected_context or kind != "literal":
            literals.append((kind, str(value)))
        return
    if isinstance(value, list):
        for item in value:
            _collect_forbidden_literals(
                item,
                literals,
                kind=kind,
                protected_context=protected_context,
            )
        return
    if not isinstance(value, dict):
        return

    for key in ("forbidden_literals", "leakage_basis"):
        nested = value.get(key)
        if nested is not None:
            _collect_forbidden_literals(
                nested,
                literals,
                kind=kind,
                protected_context=True,
            )
    for key, nested in value.items():
        if key in {"forbidden_literals", "leakage_basis"}:
            continue
        normalized_key = str(key).strip().lower().replace("-", "_")
        if normalized_key in _FORBIDDEN_LITERAL_KEYS:
            _collect_forbidden_literals(
                nested,
                literals,
                kind=normalized_key,
                protected_context=True,
            )
        elif protected_context:
            _collect_forbidden_literals(
                nested,
                literals,
                kind=kind,
                protected_context=True,
            )


def _unique_forbidden_literals(literals: list[tuple[str, str]]) -> list[tuple[str, str]]:
    seen: set[tuple[str, str]] = set()
    unique: list[tuple[str, str]] = []
    for kind, literal in literals:
        text = literal.strip()
        if not text:
            continue
        key = (kind, text.lower())
        if key in seen:
            continue
        seen.add(key)
        unique.append((kind, text))
    return unique


def _audit_agent_system_markdown(
    text: str,
    *,
    forbidden_literals: list[tuple[str, str]],
) -> list[dict[str, str]]:
    findings: list[dict[str, str]] = []
    if not text.strip():
        return [{"code": "empty_output", "message": "agent-system output is empty"}]
    findings.extend(_forbidden_literal_findings(text, forbidden_literals))
    findings.extend(_actionability_findings(text))
    return findings


def _forbidden_literal_findings(
    text: str,
    forbidden_literals: list[tuple[str, str]],
) -> list[dict[str, str]]:
    findings: list[dict[str, str]] = []
    haystack = text.lower()
    sequence_haystack = _normalize_dna_literal(text)
    for kind, literal in forbidden_literals:
        if _literal_too_short(kind, literal):
            continue
        if _forbidden_literal_found(
            text=text,
            haystack=haystack,
            sequence_haystack=sequence_haystack,
            kind=kind,
            literal=literal,
        ):
            findings.append(
                {
                    "code": "forbidden_literal",
                    "message": f"forbidden literal copied from protected evaluation data ({kind})",
                }
            )
    return _unique_findings(findings)


def _literal_too_short(kind: str, literal: str) -> bool:
    if "sequence" in kind:
        return len(_normalize_dna_literal(literal)) < 20
    if "row" in kind:
        return not re.fullmatch(r"\d+", literal.strip())
    return len(literal.strip()) < 6


def _forbidden_literal_found(
    *,
    text: str,
    haystack: str,
    sequence_haystack: str,
    kind: str,
    literal: str,
) -> bool:
    if "sequence" in kind:
        sequence = _normalize_dna_literal(literal)
        return bool(sequence and sequence in sequence_haystack)
    if "row" in kind:
        row = literal.strip()
        return (
            re.search(
                rf"\b(?:source[_\s-]*row|row)\s*(?:[:=#]\s*)?{re.escape(row)}\b",
                text,
                flags=re.IGNORECASE,
            )
            is not None
        )
    return literal.lower() in haystack


def _normalize_dna_literal(value: str) -> str:
    return re.sub(r"\s+", "", value).upper()


def _actionability_findings(text: str) -> list[dict[str, str]]:
    findings: list[dict[str, str]] = []
    rule_lines = _agent_system_rule_lines(text)
    if not any(_is_actionable_rule(line) for line in rule_lines):
        findings.append(
            {
                "code": "missing_actionable_rule",
                "message": "agent-system rules must include at least one concrete trigger, action, and validation check",
            }
        )

    for line in rule_lines:
        if _is_slogan_rule(line):
            findings.append(
                {
                    "code": "slogan_rule",
                    "message": f"rule is too generic to execute: {_redact_for_finding(line)}",
                }
            )

    if _mentions_source_coverage(text) and not _has_actionable_source_coverage_rule(text):
        findings.append(
            {
                "code": "source_coverage_not_actionable",
                "message": (
                    "coverage rules must include recursive file-level source discovery, named "
                    "structured evidence formats, and a per-package or per-source validation check"
                ),
            }
        )
    return _unique_findings(findings)


def _agent_system_rule_lines(text: str) -> list[str]:
    lines: list[str] = []
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if re.fullmatch(r"[-*_]+", line):
            continue
        if line.startswith(("- ", "* ")):
            line = line[2:].strip()
        elif re.match(r"^\d+[.)]\s+", line):
            line = re.sub(r"^\d+[.)]\s+", "", line).strip()
        lines.append(line)
    return lines


def _is_actionable_rule(line: str) -> bool:
    normalized = line.lower()
    has_action = _contains_any(
        normalized,
        {
            "audit",
            "check",
            "compare",
            "confirm",
            "deduplicate",
            "enumerate",
            "group",
            "inspect",
            "inventory",
            "list",
            "normalize",
            "parse",
            "read",
            "reject",
            "remove",
            "run",
            "scan",
            "use",
            "preserve",
            "investigate",
            "validate",
            "verify",
            "walk",
        },
    )
    has_trigger_or_validation = _contains_any(
        normalized,
        {
            "after",
            "before",
            "when",
            "if",
            "unless",
            "until",
            "finalizing",
            "finishing",
            "validation",
            "verification",
            "regression",
            "regressions",
            "failure",
            "failures",
            "round",
            "rounds",
            "check",
            "audit",
            "confirm",
            "verify",
        },
    )
    return has_action and has_trigger_or_validation


def _is_slogan_rule(line: str) -> bool:
    normalized = line.lower()
    if _is_actionable_rule(line) and len(normalized.split()) >= 12:
        return False
    return bool(
        re.search(
            r"\b("
            r"perform a coverage pass|review all (?:allowed )?sources|review every allowed source|"
            r"balance precision and recall|improve coverage|be careful|avoid mistakes|"
            r"use reflected lessons|preserve useful learnings"
            r")\b",
            normalized,
        )
    )


def _mentions_source_coverage(text: str) -> bool:
    normalized = text.lower()
    source_terms = (
        r"source|sources|package|packages|bundle|bundles|component|components|"
        r"inventory|inventories"
    )
    patterns = [
        r"\bsource[-\s]*coverage\b",
        rf"\bcoverage\s+(?:pass|check|checks|rule|rules|audit|against)\b[^\n.]*\b(?:{source_terms})\b",
        rf"\b(?:{source_terms})\b[^\n.]*\bcoverage\b",
        r"\b(source bundle|source bundles|all allowed sources|every allowed source|"
        r"all sources|eligible component classes|source checklist|allowed bundle|"
        r"allowed bundles)\b",
    ]
    return any(re.search(pattern, normalized) for pattern in patterns)


def _has_actionable_source_coverage_rule(text: str) -> bool:
    normalized = text.lower()
    has_inventory = _has_file_level_inventory_rule(normalized)
    has_source_scope = _contains_any_term(
        normalized,
        {
            "allowed input",
            "input root",
            "source root",
            "package",
            "article",
            "directory",
            "bundle",
        },
    )
    has_structured_evidence = _contains_any_term(
        normalized,
        {
            "table",
            "tables",
            "spreadsheet",
            "spreadsheets",
            "workbook",
            "workbooks",
            "csv",
            "tsv",
            "xlsx",
            "xls",
            "supplement",
            "supplementary",
        },
    )
    has_validation = _contains_any(
        normalized,
        {"verify", "confirm", "audit", "check", "before finalizing", "before finishing"},
    )
    return has_inventory and has_source_scope and has_structured_evidence and has_validation


def _has_file_level_inventory_rule(text: str) -> bool:
    if _contains_any_term(text, {"recursive", "recursively", "walk"}):
        return True
    return (
        re.search(
            r"\b(?:inventory|list|enumerate|scan)\s+(?:all\s+|every\s+|allowed\s+|source\s+)?files?\b",
            text,
        )
        is not None
        or re.search(r"\b(?:all|every|allowed|source)\s+files?\s+(?:under|in|within)\b", text)
        is not None
        or re.search(r"\bevery\s+file\b", text) is not None
    )


def _contains_any_term(text: str, needles: set[str]) -> bool:
    return any(_contains_term(text, needle) for needle in needles)


def _contains_term(text: str, needle: str) -> bool:
    if re.search(r"[^a-z0-9_]", needle):
        return needle in text
    return re.search(rf"\b{re.escape(needle)}\b", text) is not None


def _contains_any(text: str, needles: set[str]) -> bool:
    return any(needle in text for needle in needles)


def _unique_findings(findings: list[dict[str, str]]) -> list[dict[str, str]]:
    seen: set[tuple[str, str]] = set()
    unique: list[dict[str, str]] = []
    for finding in findings:
        key = (finding["code"], finding["message"])
        if key in seen:
            continue
        seen.add(key)
        unique.append(finding)
    return unique


def _redact_for_finding(text: str) -> str:
    return _snippet(text, limit=160)


def _render_agent_system_audit_repair_prompt(
    *,
    original_prompt: str,
    candidate_markdown: str,
    findings: list[dict[str, str]],
    forbidden_literals: list[tuple[str, str]],
) -> str:
    redacted_candidate = _redact_forbidden_literals(candidate_markdown, forbidden_literals)
    lines = [
        "Return only the revised Markdown agent-system instruction file.",
        "",
        "The previous agent-system audit found issues. Revise the Markdown so it passes these requirements:",
    ]
    for finding in findings:
        lines.append(f"- {finding['message']}")
    lines.extend(
        [
            "",
            "Rules for the revision:",
            "- Keep methodology general across tasks; do not name protected titles, exact source files, sheet names, row numbers, answer counts, sequences, or reference records.",
            "- Replace generic slogans with rules that include a trigger, a concrete action, and a validation check.",
            "- For source-coverage rules, describe recursive file-level inventory under the allowed input root, general structured evidence formats such as tables/spreadsheets/CSV/TSV/XLS/XLSX/supplementary files, and validation without naming task-specific files.",
            "",
            "Original reflection context:",
            original_prompt,
            "",
            "Candidate Markdown with protected literals redacted:",
            redacted_candidate,
        ]
    )
    return "\n".join(lines)


def _redact_forbidden_literals(
    text: str,
    forbidden_literals: list[tuple[str, str]],
) -> str:
    redacted = text
    for index, (kind, literal) in enumerate(forbidden_literals, 1):
        if _literal_too_short(kind, literal):
            continue
        placeholder = f"[REDACTED_{kind.upper()}_{index}]"
        if "sequence" in kind:
            redacted = _redact_sequence_literal(redacted, literal, placeholder)
        elif "row" in kind:
            redacted = re.sub(
                rf"\b(?:source[_\s-]*row|row)\s*(?:[:=#]\s*)?{re.escape(literal.strip())}\b",
                placeholder,
                redacted,
                flags=re.IGNORECASE,
            )
        else:
            redacted = re.sub(re.escape(literal), placeholder, redacted, flags=re.IGNORECASE)
    return redacted


def _redact_sequence_literal(text: str, literal: str, placeholder: str) -> str:
    sequence = _normalize_dna_literal(literal)
    if not sequence:
        return text
    pattern = r"\s*".join(re.escape(base) for base in sequence)
    return re.sub(pattern, placeholder, text, flags=re.IGNORECASE)


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


def _human_feedback_from_records(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    feedback: list[dict[str, Any]] = []
    seen: set[str] = set()
    for record in records:
        for value in _candidate_human_feedback_values(record):
            for item in _human_feedback_from_value(value):
                feedback_id = item.get("feedback_id")
                if isinstance(feedback_id, str) and feedback_id:
                    existing = next(
                        (
                            candidate
                            for candidate in feedback
                            if candidate.get("feedback_id") == feedback_id
                        ),
                        None,
                    )
                    if existing is not None:
                        _merge_human_feedback_item(existing, item)
                        continue
                    seen.add(feedback_id)
                    feedback.append(item)
                    continue
                dedupe_key = json.dumps(item, sort_keys=True)
                if dedupe_key in seen:
                    continue
                seen.add(dedupe_key)
                feedback.append(item)
    return feedback


def _merge_human_feedback_item(
    target: dict[str, Any],
    source: dict[str, Any],
) -> None:
    target["status"] = _HUMAN_FEEDBACK_AVAILABLE_STATUS
    for field in ("feedback_id", "decision", "confidence", "score"):
        if field not in target and source.get(field) not in (None, "", []):
            target[field] = source[field]
    for field in _HUMAN_FEEDBACK_LIST_FIELDS:
        values = source.get(field)
        if not isinstance(values, list):
            continue
        target_values = target.setdefault(field, [])
        if not isinstance(target_values, list):
            continue
        for value in values:
            if isinstance(value, str) and value not in target_values:
                target_values.append(value)


def _candidate_human_feedback_values(record: dict[str, Any]) -> list[Any]:
    values: list[Any] = []
    _append_explicit_human_feedback_values(values, record)
    payload = record.get("payload")
    if isinstance(payload, dict):
        _append_explicit_human_feedback_values(values, payload)
        session_result = payload.get("session_result")
        if isinstance(session_result, dict):
            _append_explicit_human_feedback_values(values, session_result)
            metadata = session_result.get("metadata")
            if isinstance(metadata, dict):
                _append_explicit_human_feedback_values(values, metadata)
    return values


def _append_explicit_human_feedback_values(values: list[Any], mapping: dict[str, Any]) -> None:
    values.append(mapping.get("human"))
    values.append(mapping.get("human_feedback"))
    evolution_feedback = mapping.get("evolution_feedback")
    if isinstance(evolution_feedback, dict):
        values.append(evolution_feedback.get("human"))
        values.append(evolution_feedback.get("human_feedback"))


def _human_feedback_from_value(value: Any) -> list[dict[str, Any]]:
    if isinstance(value, list):
        feedback: list[dict[str, Any]] = []
        for item in value:
            feedback.extend(_human_feedback_from_value(item))
        return feedback
    if not isinstance(value, dict):
        return []

    feedback: list[dict[str, Any]] = []
    for key in ("human", "human_feedback"):
        if key in value:
            feedback.extend(_human_feedback_from_value(value[key]))
    item = _sanitize_human_feedback_item(value)
    if item:
        feedback.append(item)
    return feedback


def _sanitize_human_feedback_item(item: dict[str, Any]) -> dict[str, Any]:
    status = item.get("status")
    if status != _HUMAN_FEEDBACK_AVAILABLE_STATUS:
        return {}

    source = item.get("normalized_payload")
    if not isinstance(source, dict):
        source = item

    sanitized: dict[str, Any] = {"status": _HUMAN_FEEDBACK_AVAILABLE_STATUS}
    feedback_id = item.get("feedback_id") or source.get("feedback_id")
    if isinstance(feedback_id, str) and feedback_id.strip():
        sanitized["feedback_id"] = _snippet(_redact_human_feedback_text(feedback_id), limit=96)
    decision = source.get("decision")
    if isinstance(decision, str) and decision.strip():
        sanitized["decision"] = _snippet(_redact_human_feedback_text(decision), limit=64)
    confidence = source.get("confidence")
    if isinstance(confidence, int | float):
        sanitized["confidence"] = float(confidence)
    score = _bounded_human_feedback_score(source.get("score"))
    if score is None and source is not item:
        score = _bounded_human_feedback_score(item.get("score"))
    if score is not None:
        sanitized["score"] = score
    for field in _HUMAN_FEEDBACK_LIST_FIELDS:
        values = _human_feedback_string_list(source.get(field))
        if values:
            sanitized[field] = values
    return sanitized


def _human_feedback_string_list(value: Any) -> list[str]:
    if isinstance(value, str):
        text = _snippet(_redact_human_feedback_text(value), limit=240)
        return [text] if text else []
    if isinstance(value, list | tuple):
        values: list[str] = []
        for item in value[:_HUMAN_FEEDBACK_VALUES_PER_FIELD_LIMIT]:
            if not isinstance(item, str):
                continue
            text = _snippet(_redact_human_feedback_text(item), limit=240)
            if text:
                values.append(text)
        return values
    return []


def _bounded_human_feedback_score(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    score = float(value)
    if not math.isfinite(score) or score < 0.0 or score > 1.0:
        return None
    return score


def _redact_human_feedback_text(value: str) -> str:
    text = value.strip()
    if not text:
        return ""
    text = _CREDENTIAL_URI_RE.sub("[REDACTED_URI]", text)
    text = _URI_QUERY_FRAGMENT_RE.sub(lambda match: match.group(1), text)
    text = _FILE_URI_RE.sub("[REDACTED_FILE_URI]", text)
    text = _POSIX_ABSOLUTE_PATH_RE.sub(_redact_posix_path_match, text)
    text = _WINDOWS_UNC_PATH_RE.sub("[REDACTED_FILE_PATH]", text)
    text = _WINDOWS_ABSOLUTE_PATH_RE.sub("[REDACTED_FILE_PATH]", text)
    text = _SECRET_ASSIGNMENT_RE.sub("[REDACTED_SECRET]", text)
    text = _BEARER_VALUE_RE.sub("[REDACTED_SECRET]", text)
    text = _AWS_ACCESS_KEY_RE.sub("[REDACTED_SECRET]", text)
    return text.strip()


def _redact_posix_path_match(match: re.Match[str]) -> str:
    return "[REDACTED_FILE_PATH]"


def _render_human_feedback_summary(feedback: list[dict[str, Any]]) -> str:
    if not feedback:
        return ""

    lines = ["## Human feedback signals", ""]
    for item in feedback[:_HUMAN_FEEDBACK_SUMMARY_LIMIT]:
        parts: list[str] = []
        feedback_id = item.get("feedback_id")
        if isinstance(feedback_id, str) and feedback_id:
            parts.append(f"feedback_id={feedback_id}")
        status = item.get("status")
        if status == _HUMAN_FEEDBACK_AVAILABLE_STATUS:
            parts.append(f"status={status}")
        decision = item.get("decision")
        if isinstance(decision, str) and decision:
            parts.append(f"decision={decision}")
        confidence = item.get("confidence")
        if isinstance(confidence, int | float):
            parts.append(f"confidence={float(confidence):.3f}")
        score = _bounded_human_feedback_score(item.get("score"))
        if score is not None:
            parts.append(f"score={score:.3f}")
        labels = item.get("labels")
        if isinstance(labels, list) and labels:
            parts.append("labels=" + ", ".join(str(label) for label in labels))
        lines.append("- " + (" ".join(parts) if parts else "human feedback"))
        for field in (
            "observed_issues",
            "suggested_changes",
            "risks",
            "validation_checks",
        ):
            values = item.get(field)
            if isinstance(values, list) and values:
                lines.append(f"  - {field}: " + "; ".join(str(value) for value in values))
    if len(feedback) > _HUMAN_FEEDBACK_SUMMARY_LIMIT:
        remaining = len(feedback) - _HUMAN_FEEDBACK_SUMMARY_LIMIT
        lines.append(f"- {remaining} additional human feedback item(s) omitted.")
    return "\n".join(lines)


def _human_feedback_ids(feedback: list[dict[str, Any]]) -> list[str]:
    ids: list[str] = []
    for item in feedback:
        feedback_id = item.get("feedback_id")
        if isinstance(feedback_id, str) and feedback_id and feedback_id not in ids:
            ids.append(feedback_id)
    return ids


def _shared_evolution_feedback_ids_from_records(records: list[dict[str, Any]]) -> list[str]:
    ids: list[str] = []
    for record in records:
        for value in _candidate_evolution_feedback_values(record):
            for feedback_id in _shared_evolution_feedback_ids_from_value(value):
                if feedback_id not in ids:
                    ids.append(feedback_id)
    return ids


def _shared_evolution_feedback_ids_from_value(value: Any) -> list[str]:
    if isinstance(value, list):
        ids: list[str] = []
        for item in value:
            for feedback_id in _shared_evolution_feedback_ids_from_value(item):
                if feedback_id not in ids:
                    ids.append(feedback_id)
        return ids
    if not isinstance(value, dict):
        return []
    rendered = _render_evolution_feedback_value(value)
    if not rendered:
        return []

    ids: list[str] = []
    feedback_id = value.get("feedback_id")
    if isinstance(feedback_id, str) and feedback_id.strip():
        sanitized_id = _snippet(_redact_human_feedback_text(feedback_id), limit=96)
        if sanitized_id:
            ids.append(sanitized_id)
    for key, item in value.items():
        if not _shared_evolution_feedback_key_safe(key):
            continue
        for nested_id in _shared_evolution_feedback_ids_from_value(item):
            if nested_id not in ids:
                ids.append(nested_id)
    return ids


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
            verifier_feedback = _verifier_feedback_summary(metadata.get("verifier"))
            if verifier_feedback:
                failure_signal = (
                    f"{failure_signal}\n{verifier_feedback}".strip()
                    if failure_signal
                    else verifier_feedback
                )

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
        "failure_signal": _snippet(failure_signal, limit=2000),
        "evolution_feedback": _snippet(evolution_feedback, limit=2000),
    }


def _verifier_feedback_summary(verifier: Any) -> str:
    if not isinstance(verifier, dict):
        return ""
    lines: list[str] = []
    summary = verifier.get("summary")
    if isinstance(summary, dict) and summary:
        parts = [
            f"{key}={value}"
            for key, value in summary.items()
            if isinstance(key, str) and isinstance(value, int | float | str)
        ]
        if parts:
            lines.append("verifier_summary: " + " ".join(parts))
    failed_tests = verifier.get("failed_tests")
    if isinstance(failed_tests, list):
        for failed_test in failed_tests[:5]:
            if not isinstance(failed_test, dict):
                continue
            name = failed_test.get("name")
            message = failed_test.get("message")
            name_text = name.strip() if isinstance(name, str) else "unknown_failed_test"
            message_text = message.strip() if isinstance(message, str) else ""
            if message_text:
                lines.append(f"failed_test: {name_text}: {message_text}")
            else:
                lines.append(f"failed_test: {name_text}")
    return "\n".join(lines)


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
    for artifact in reversed(job.input_artifacts):
        if (
            artifact.type == ArtifactType.AGENT_SYSTEM
            or artifact.type == ArtifactType.AGENT_SYSTEM.value
        ):
            text = _read_input_artifact_text(artifact)
            if text:
                return text
    return ""


def _text_memory_reflector_base_texts(job: WorkerClaimedJob) -> list[str]:
    texts: list[str] = []
    for artifact in job.input_artifacts:
        if (
            artifact.type == ArtifactType.TEXT_MEMORY
            or artifact.type == ArtifactType.TEXT_MEMORY.value
        ):
            text = _read_input_artifact_text(artifact)
            if text:
                texts.append(text)
    return texts


def _skill_bundle_reflector_base(
    job: WorkerClaimedJob,
) -> tuple[str, WorkerClaimInputArtifact | None]:
    for key in ("base_skill_markdown", "skill_markdown", "content"):
        value = job.config.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip(), None
    for artifact in reversed(job.input_artifacts):
        if (
            artifact.type == ArtifactType.SKILL_BUNDLE
            or artifact.type == ArtifactType.SKILL_BUNDLE.value
        ):
            text = _read_input_artifact_text(artifact, directory_entrypoint="SKILL.md")
            if text:
                return text, artifact
    return "", None


def _read_input_artifact_text(
    artifact: WorkerClaimInputArtifact,
    *,
    directory_entrypoint: str | None = None,
) -> str:
    try:
        path = _file_uri_to_path(artifact.uri)
        if directory_entrypoint and path.is_dir():
            path = path / directory_entrypoint
        return path.read_text(encoding="utf-8").strip()
    except (OSError, ValueError, UnicodeDecodeError):
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
    default_timeout_seconds = (
        _DEFAULT_CODEX_CLI_REFLECTOR_TIMEOUT_SECONDS
        if provider == _REFLECTOR_PROVIDER_CODEX_CLI
        else _DEFAULT_REFLECTOR_TIMEOUT_SECONDS
    )
    timeout_seconds = _float_config(
        raw_config.get("timeout_seconds", job.config.get("reflector_timeout_seconds")),
        default_timeout_seconds,
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
    rendered_values: list[str] = []
    for value in _candidate_evolution_feedback_values(record):
        rendered = _render_evolution_feedback_value(value)
        if rendered:
            rendered_values.append(rendered)
    return "\n".join(rendered_values)


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
        return _snippet(_redact_human_feedback_text(value), limit=240)
    if isinstance(value, dict):
        if _shared_evolution_feedback_stateful_mapping(value):
            status = value.get("status")
            if status != _HUMAN_FEEDBACK_AVAILABLE_STATUS:
                return ""
        parts: list[str] = []
        for key in sorted(value):
            if not _shared_evolution_feedback_key_safe(key):
                continue
            rendered = _render_evolution_feedback_value(value[key])
            if rendered:
                title = str(key).replace("_", " ").title()
                parts.append(f"{title}: {rendered}")
        return "\n".join(parts)
    if isinstance(value, list):
        parts = [_render_evolution_feedback_value(item) for item in value]
        return "\n".join(part for part in parts if part)
    return ""


def _shared_evolution_feedback_key_safe(key: Any) -> bool:
    normalized = str(key).strip().lower().replace("-", "_")
    if normalized in _EVOLUTION_FEEDBACK_UNSAFE_KEYS:
        return False
    return not any(
        fragment in normalized for fragment in _EVOLUTION_FEEDBACK_UNSAFE_KEY_FRAGMENTS
    )


def _shared_evolution_feedback_stateful_mapping(value: dict[str, Any]) -> bool:
    if "status" in value:
        return True
    return any(
        str(key).strip().lower().replace("-", "_") in _EVOLUTION_FEEDBACK_LIKE_KEYS
        for key in value
    )


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


def _parametric_memory_compatibility(
    job: WorkerClaimedJob,
    base_model: str,
) -> dict[str, Any]:
    compatibility = _dict_config(job.config.get("compatibility"))
    configured_base_model = compatibility.get("base_model")
    if configured_base_model is None:
        compatibility["base_model"] = [base_model]
        return compatibility

    compatible_base_models = _string_list(configured_base_model)
    if base_model not in compatible_base_models:
        raise ValueError(
            "parametric_memory compatibility.base_model must include trained base_model "
            f"{base_model!r}"
        )
    compatibility["base_model"] = compatible_base_models
    return compatibility


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


def _float_config(value: Any, default: float | None) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default
