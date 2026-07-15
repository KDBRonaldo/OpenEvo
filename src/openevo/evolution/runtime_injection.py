"""Closed authority for deterministic evolution runtime injection."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import re
from typing import Any, Mapping, Sequence

from openevo.evolution.agent_system import normalize_agent_system_target_path
from openevo.evolution.framework.handlers import PayloadManifestEntry, payload_tree_digest
from openevo.runtime.base import (
    RUNTIME_READBACK_MAX_BYTES,
    RUNTIME_READBACK_MAX_FILES,
)


_ARTIFACT_TYPES = {
    "agent_system",
    "parametric_memory",
    "skill_bundle",
    "text_memory",
}
_SAFE_SKILL_NAME_RE = re.compile(r"[^A-Za-z0-9_.-]+")
_SHA256_RE = re.compile(r"[0-9a-f]{64}")
_MAX_ARTIFACTS = 256
_MAX_RUNTIME_FILES = RUNTIME_READBACK_MAX_FILES
_MAX_RUNTIME_BYTES = RUNTIME_READBACK_MAX_BYTES


@dataclass(frozen=True, slots=True)
class RuntimeInjectionPlan:
    effective_instruction: str
    canonical_files: Mapping[str, bytes]
    agent_system_targets: Mapping[str, bytes]
    skill_directories: Mapping[str, str]
    authority: dict[str, object]


def build_runtime_injection_plan(
    *,
    context: Mapping[str, Any],
    revision_id: str,
    instruction: str,
    expected_artifact_ids: Sequence[str],
) -> RuntimeInjectionPlan:
    """Build the exact expected runtime bytes without reading artifact host paths."""

    context_id = _bounded_identity(context.get("context_id"), label="context")
    revision = _bounded_identity(revision_id, label="revision")
    expected_ids = _ordered_identities(expected_artifact_ids, label="expected artifact")
    if not expected_ids:
        raise ValueError("runtime injection authority requires at least one artifact")

    selection = context.get("selection")
    if not isinstance(selection, Mapping) or set(selection) != {
        "artifact_ids",
        "artifacts",
        "reasons",
    }:
        raise ValueError("runtime injection selection is invalid")
    selected_ids = _ordered_identities(
        selection.get("artifact_ids"),
        label="selected artifact",
    )
    if selected_ids != expected_ids:
        raise ValueError("runtime injection selection differs from revision order")
    reasons = selection.get("reasons")
    if not isinstance(reasons, list) or not all(isinstance(item, str) for item in reasons):
        raise ValueError("runtime injection selection reasons are invalid")

    raw_artifacts = selection.get("artifacts")
    if not isinstance(raw_artifacts, list) or len(raw_artifacts) != len(selected_ids):
        raise ValueError("runtime injection artifact inventory is incomplete")
    artifacts: list[dict[str, Any]] = []
    for expected_id, raw in zip(selected_ids, raw_artifacts, strict=True):
        if not isinstance(raw, Mapping) or set(raw) != {
            "artifact_id",
            "artifact_type",
            "content_sha256",
            "payload_entries",
        }:
            raise ValueError("runtime injection artifact inventory is invalid")
        artifact_id = _bounded_identity(raw.get("artifact_id"), label="artifact")
        artifact_type = raw.get("artifact_type")
        content_sha256 = raw.get("content_sha256")
        raw_entries = raw.get("payload_entries")
        if (
            artifact_id != expected_id
            or artifact_type not in _ARTIFACT_TYPES
            or not isinstance(content_sha256, str)
            or _SHA256_RE.fullmatch(content_sha256) is None
            or not isinstance(raw_entries, list)
            or not raw_entries
        ):
            raise ValueError("runtime injection artifact inventory is invalid")
        entries = tuple(PayloadManifestEntry.model_validate(item) for item in raw_entries)
        if payload_tree_digest(entries) != content_sha256:
            raise ValueError("runtime injection artifact content digest is invalid")
        artifacts.append(
            {
                "artifact_id": artifact_id,
                "artifact_type": str(artifact_type),
                "content_sha256": content_sha256,
                "payload_entries": entries,
            }
        )

    _memory_items, memory_text = _ordered_text_section(
        context,
        section_name="memory",
        item_name="items",
        expected_ids=tuple(
            item["artifact_id"] for item in artifacts if item["artifact_type"] == "text_memory"
        ),
    )
    agent_items, agent_text = _ordered_text_section(
        context,
        section_name="agent_system",
        item_name="targets",
        expected_ids=tuple(
            item["artifact_id"] for item in artifacts if item["artifact_type"] == "agent_system"
        ),
        require_target=True,
    )
    skill_directories = _skill_authority(context, artifacts)
    adapters = _adapter_document(context, artifacts)
    context_document = _runtime_context_document(context)
    effective_instruction = instruction_with_evolution_context(instruction, context)

    canonical_files: dict[str, bytes] = {
        "context.json": _pretty_json_bytes(context_document),
        "instruction.txt": effective_instruction.encode("utf-8"),
        "memory.md": memory_text.encode("utf-8"),
        "adapters.json": _pretty_json_bytes(adapters),
    }
    if agent_text:
        canonical_files["agent_system.md"] = agent_text.encode("utf-8")

    target_texts: dict[str, list[str]] = {}
    for item in agent_items:
        target_path = str(item["target_path"])
        target_texts.setdefault(target_path, []).append(str(item["rendered_text"]))
    agent_system_targets = {
        path: "\n\n".join(parts).encode("utf-8") for path, parts in target_texts.items()
    }

    expected_files: list[dict[str, object]] = []
    for path, payload in canonical_files.items():
        expected_files.append(_file_entry(f"evolution/{path}", payload))
    for artifact in artifacts:
        if artifact["artifact_type"] != "skill_bundle":
            continue
        directory = skill_directories[str(artifact["artifact_id"])]
        for entry in artifact["payload_entries"]:
            expected_files.append(
                {
                    "relative_path": f"evolution/skills/{directory}/{entry.relative_path}",
                    "size_bytes": entry.size_bytes,
                    "sha256": entry.sha256,
                }
            )
    for path, payload in agent_system_targets.items():
        expected_files.append(_file_entry(f"agent_system_targets/{path}", payload))
    expected_files.sort(key=lambda item: str(item["relative_path"]))
    _validate_runtime_file_inventory(expected_files)

    files_by_path = {str(item["relative_path"]): item for item in expected_files}
    receipt_artifacts: list[dict[str, object]] = []
    for artifact in artifacts:
        artifact_id = str(artifact["artifact_id"])
        artifact_type = str(artifact["artifact_type"])
        if artifact_type == "text_memory":
            runtime_paths = ["evolution/memory.md"]
        elif artifact_type == "agent_system":
            agent_item = next(
                value for value in agent_items if value["artifact_id"] == artifact_id
            )
            runtime_paths = [
                "evolution/agent_system.md",
                f"agent_system_targets/{agent_item['target_path']}",
            ]
        elif artifact_type == "skill_bundle":
            prefix = f"evolution/skills/{skill_directories[artifact_id]}/"
            runtime_paths = [
                str(value["relative_path"])
                for value in expected_files
                if str(value["relative_path"]).startswith(prefix)
            ]
        else:
            runtime_paths = ["evolution/adapters.json"]
        runtime_paths = list(dict.fromkeys(runtime_paths))
        runtime_entries = [files_by_path[path] for path in runtime_paths]
        receipt_artifacts.append(
            {
                "artifact_id": artifact_id,
                "artifact_type": artifact_type,
                "content_sha256": artifact["content_sha256"],
                "runtime_paths": runtime_paths,
                "runtime_tree_sha256": _canonical_sha256({"files": runtime_entries}),
            }
        )

    instruction_entry = files_by_path["evolution/instruction.txt"]
    authority: dict[str, object] = {
        "schema_version": "3",
        "context_id": context_id,
        "revision_id": revision,
        "instruction_sha256": instruction_entry["sha256"],
        "runtime_tree_sha256": _canonical_sha256({"files": expected_files}),
        "files": expected_files,
        "artifacts": receipt_artifacts,
    }
    return RuntimeInjectionPlan(
        effective_instruction=effective_instruction,
        canonical_files=canonical_files,
        agent_system_targets=agent_system_targets,
        skill_directories=skill_directories,
        authority=authority,
    )


def receipt_from_runtime_readback(
    authority: Mapping[str, object],
    files: Sequence[Mapping[str, object]],
) -> dict[str, object]:
    """Build a receipt from runtime-read bytes and reject any authority drift."""

    actual_files = [
        {
            "relative_path": item.get("relative_path"),
            "size_bytes": item.get("size_bytes"),
            "sha256": item.get("sha256"),
        }
        for item in files
    ]
    actual_files.sort(key=lambda item: str(item["relative_path"]))
    _validate_runtime_file_inventory(actual_files)
    expected_files = authority.get("files")
    if actual_files != expected_files:
        raise ValueError("runtime injection readback differs from expected files")
    receipt = json.loads(json.dumps(authority, ensure_ascii=True, allow_nan=False))
    receipt["files"] = actual_files
    receipt["runtime_tree_sha256"] = _canonical_sha256({"files": actual_files})
    files_by_path = {str(item["relative_path"]): item for item in actual_files}
    artifacts = receipt.get("artifacts")
    if not isinstance(artifacts, list):
        raise ValueError("runtime injection authority artifacts are invalid")
    for artifact in artifacts:
        if not isinstance(artifact, dict):
            raise ValueError("runtime injection authority artifact is invalid")
        paths = artifact.get("runtime_paths")
        if not isinstance(paths, list) or not all(path in files_by_path for path in paths):
            raise ValueError("runtime injection authority paths are invalid")
        artifact["runtime_tree_sha256"] = _canonical_sha256(
            {"files": [files_by_path[path] for path in paths]}
        )
    if receipt != authority:
        raise ValueError("runtime injection readback digest differs from authority")
    return receipt


def instruction_with_evolution_context(instruction: str, context: Mapping[str, Any]) -> str:
    agent_system = str((context.get("agent_system") or {}).get("rendered_text") or "").strip()
    memory = str((context.get("memory") or {}).get("rendered_text") or "").strip()
    if not agent_system and not memory:
        return instruction
    parts: list[str] = []
    if agent_system:
        parts.append(
            f"Use the following evolved agent system instructions for this task:\n{agent_system}"
        )
    if memory:
        parts.append(f"Use the following long-term memory for this task:\n{memory}")
    parts.append(f"Task:\n{instruction}")
    return "\n\n".join(parts)


def _ordered_text_section(
    context: Mapping[str, Any],
    *,
    section_name: str,
    item_name: str,
    expected_ids: tuple[str, ...],
    require_target: bool = False,
) -> tuple[list[dict[str, str]], str]:
    section = context.get(section_name)
    if not isinstance(section, Mapping):
        raise ValueError(f"runtime injection {section_name} section is invalid")
    artifact_ids = _ordered_identities(
        section.get("artifact_ids"),
        label=f"{section_name} artifact",
    )
    values = section.get(item_name)
    rendered = section.get("rendered_text")
    if (
        artifact_ids != expected_ids
        or not isinstance(values, list)
        or not isinstance(rendered, str)
    ):
        raise ValueError(f"runtime injection {section_name} section is incomplete")
    ordered: list[dict[str, str]] = []
    for expected_id, value in zip(expected_ids, values, strict=True):
        if not isinstance(value, Mapping):
            raise ValueError(f"runtime injection {section_name} item is invalid")
        artifact_id = _bounded_identity(value.get("artifact_id"), label=section_name)
        text = value.get("rendered_text")
        if artifact_id != expected_id or not isinstance(text, str) or not text:
            raise ValueError(f"runtime injection {section_name} item is invalid")
        item = {"artifact_id": artifact_id, "rendered_text": text}
        if require_target:
            item["target_path"] = normalize_agent_system_target_path(value.get("target_path"))
        ordered.append(item)
    if "\n\n".join(item["rendered_text"] for item in ordered) != rendered:
        raise ValueError(f"runtime injection {section_name} rendering is inconsistent")
    return ordered, rendered


def _skill_authority(
    context: Mapping[str, Any],
    artifacts: Sequence[Mapping[str, Any]],
) -> dict[str, str]:
    skills = context.get("skills")
    if not isinstance(skills, list):
        raise ValueError("runtime injection skills are invalid")
    expected = [item for item in artifacts if item["artifact_type"] == "skill_bundle"]
    if len(skills) != len(expected):
        raise ValueError("runtime injection skills are incomplete")
    directories: dict[str, str] = {}
    seen_directories: set[str] = set()
    for artifact, skill in zip(expected, skills, strict=True):
        if not isinstance(skill, Mapping):
            raise ValueError("runtime injection skill is invalid")
        artifact_id = _bounded_identity(skill.get("artifact_id"), label="skill")
        if artifact_id != artifact["artifact_id"]:
            raise ValueError("runtime injection skill order differs from revision")
        if not any(entry.relative_path == "SKILL.md" for entry in artifact["payload_entries"]):
            raise ValueError("runtime injection skill has no root SKILL.md")
        directory = _safe_skill_directory(artifact_id)
        if directory in seen_directories:
            raise ValueError("runtime injection skill directories collide")
        seen_directories.add(directory)
        directories[artifact_id] = directory
    return directories


def _adapter_document(
    context: Mapping[str, Any],
    artifacts: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    raw = context.get("adapter_merge_spec") or {}
    if not isinstance(raw, Mapping):
        raise ValueError("runtime injection adapter spec is invalid")
    document = json.loads(json.dumps(raw, ensure_ascii=True, allow_nan=False))
    adapters = document.get("adapters")
    if not isinstance(adapters, list):
        raise ValueError("runtime injection adapters are invalid")
    expected_ids = [
        str(item["artifact_id"])
        for item in artifacts
        if item["artifact_type"] == "parametric_memory"
    ]
    actual_ids: list[str] = []
    for adapter in adapters:
        if not isinstance(adapter, dict):
            raise ValueError("runtime injection adapter is invalid")
        actual_ids.append(_bounded_identity(adapter.get("artifact_id"), label="adapter"))
        adapter.pop("uri", None)
    if actual_ids != expected_ids:
        raise ValueError("runtime injection adapters differ from revision order")
    return document


def _runtime_context_document(context: Mapping[str, Any]) -> dict[str, Any]:
    document = json.loads(json.dumps(context, ensure_ascii=True, allow_nan=False))
    for skill in document.get("skills", []):
        if isinstance(skill, dict):
            skill.pop("uri", None)
    adapter_spec = document.get("adapter_merge_spec")
    if isinstance(adapter_spec, dict):
        for adapter in adapter_spec.get("adapters", []):
            if isinstance(adapter, dict):
                adapter.pop("uri", None)
    return document


def _ordered_identities(value: object, *, label: str) -> tuple[str, ...]:
    if not isinstance(value, list | tuple) or len(value) > _MAX_ARTIFACTS:
        raise ValueError(f"{label} identities are invalid")
    values = tuple(_bounded_identity(item, label=label) for item in value)
    if len(values) != len(set(values)):
        raise ValueError(f"{label} identities are duplicated")
    return values


def _bounded_identity(value: object, *, label: str) -> str:
    if not isinstance(value, str) or not value or len(value.encode("utf-8")) > 256:
        raise ValueError(f"runtime injection {label} identity is invalid")
    return value


def _safe_skill_directory(artifact_id: str) -> str:
    normalized = _SAFE_SKILL_NAME_RE.sub("-", artifact_id).strip(".-")
    return normalized or "skill"


def _file_entry(relative_path: str, payload: bytes) -> dict[str, object]:
    return {
        "relative_path": relative_path,
        "size_bytes": len(payload),
        "sha256": hashlib.sha256(payload).hexdigest(),
    }


def _validate_runtime_file_inventory(files: Sequence[Mapping[str, object]]) -> None:
    if not files or len(files) > _MAX_RUNTIME_FILES:
        raise ValueError("runtime injection file inventory is invalid")
    paths: list[str] = []
    total_bytes = 0
    for item in files:
        if set(item) != {"relative_path", "size_bytes", "sha256"}:
            raise ValueError("runtime injection file entry is invalid")
        path = item.get("relative_path")
        size = item.get("size_bytes")
        digest = item.get("sha256")
        if (
            not isinstance(path, str)
            or not path
            or path.startswith("/")
            or "\\" in path
            or any(part in {"", ".", ".."} for part in path.split("/"))
            or not isinstance(size, int)
            or isinstance(size, bool)
            or size < 0
            or not isinstance(digest, str)
            or _SHA256_RE.fullmatch(digest) is None
        ):
            raise ValueError("runtime injection file entry is invalid")
        paths.append(path)
        total_bytes += size
    if (
        len(paths) != len(set(paths))
        or list(paths) != sorted(paths)
        or total_bytes > _MAX_RUNTIME_BYTES
    ):
        raise ValueError("runtime injection file inventory is not canonical")


def _pretty_json_bytes(value: object) -> bytes:
    return json.dumps(value, indent=2, sort_keys=True, ensure_ascii=True).encode("utf-8")


def _canonical_sha256(value: object) -> str:
    payload = json.dumps(
        value,
        ensure_ascii=True,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()
