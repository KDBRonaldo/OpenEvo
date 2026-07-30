"""Safe declarative MemEvolve adaptation for OpenEvo text memory."""

from __future__ import annotations

import json
import re
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from openevo.evolution.framework.contracts import CaptureMode
from openevo.evolution.framework.execution import (
    HarnessInferenceRequest,
    MethodExecutionContext,
)
from openevo.evolution.methods import (
    _dict_config,
    _ensure_trailing_newline,
    _guard_generic_reflector_output,
    _read_dataset_artifact,
    _read_input_artifact_text,
    _redact_generic_reflector_prompt,
    _reflection_records,
    _scores_config,
    _string_list,
)
from openevo.evolution.models import ArtifactRegisterRequest, ArtifactType


_METHOD_ID = "text_memory_memevolve"
_MAX_ANALYSIS_BYTES = 64 * 1024
_MAX_MEMORY_BYTES = 64 * 1024
_MAX_SELECTION_BYTES = 16 * 1024
_MAX_EVIDENCE_BYTES = 256 * 1024
_MAX_PRIOR_MEMORY_BYTES = 128 * 1024
_GENERATED_PROVIDER_MARKERS = (
    "basememoryprovider",
    "def provide_memory(",
    "def take_in_memory(",
    "provider_mapping",
)


class _Selection(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    winner_index: int = Field(ge=1, le=5)
    quality: int | float = Field(ge=0.0, le=1.0)


def text_memory_memevolve(
    context: MethodExecutionContext,
) -> list[ArtifactRegisterRequest]:
    """Evolve one static Markdown memory without executing generated provider code."""

    context.validate_job_projection()
    job = context.job
    user_config = context.envelope.user_config()
    core_config = context.envelope.core_config()
    candidate_count = _bounded_int(
        user_config.get("candidate_count", 3),
        label="candidate_count",
        minimum=2,
        maximum=5,
    )
    max_records = _bounded_int(
        user_config.get("max_records", 20),
        label="max_records",
        minimum=1,
        maximum=100,
    )
    model_name, timeout_seconds = _harness_config(user_config)

    datasets = [
        artifact
        for artifact in job.input_artifacts
        if str(artifact.type) == ArtifactType.DATASET.value
    ]
    if not datasets:
        raise ValueError(f"{_METHOD_ID} requires an input dataset artifact")

    manifests: list[dict[str, Any]] = []
    reflected_records: list[dict[str, str]] = []
    consumed_dataset_ids: list[str] = []
    source_record_count = 0
    for dataset in datasets:
        if len(reflected_records) >= max_records:
            break
        manifest, records = _read_dataset_artifact(dataset)
        manifests.append(manifest)
        consumed_dataset_ids.append(dataset.artifact_id)
        source_record_count += len(records)
        reflected_records.extend(
            _reflection_records(
                records,
                max_records=max_records - len(reflected_records),
            )
        )
    if not manifests:
        raise ValueError(f"{_METHOD_ID} could not consume a dataset artifact")

    prior_artifacts = [
        artifact
        for artifact in job.input_artifacts
        if str(artifact.type) == ArtifactType.TEXT_MEMORY.value
    ]
    prior_memory = ""
    prior_memory_id: str | None = None
    for artifact in prior_artifacts:
        prior_memory = _read_input_artifact_text(artifact)
        if prior_memory:
            prior_memory_id = artifact.artifact_id
            break

    base_prompt, prompt_truncated = _base_prompt(
        reflected_records=reflected_records,
        prior_memory=prior_memory,
    )
    base_prompt = _redact_generic_reflector_prompt(
        base_prompt,
        job=job,
        manifests=manifests,
    )

    candidates: list[str] = []
    candidate_audits: list[dict[str, Any]] = []
    for candidate_index in range(1, candidate_count + 1):
        analysis = _infer_text(
            context,
            request_id=f"memevolve-analysis-{candidate_index}",
            model_name=model_name,
            timeout_seconds=timeout_seconds,
            system_instruction=(
                "Analyze task trajectories and prior static text memory. Identify which "
                "memory content, organization, retrieval cues, consolidation rules, and "
                "retirement rules would improve future attempts. This OpenEvo adaptation "
                "must remain a declarative Markdown document: do not propose executable "
                "provider code."
            ),
            prompt=(
                f"Develop independent MemEvolve analysis branch {candidate_index} of "
                f"{candidate_count}. Use a distinct defensible direction.\n\n{base_prompt}"
            ),
            max_bytes=_MAX_ANALYSIS_BYTES,
        )
        analysis, _ = _guard_generic_reflector_output(
            analysis,
            job=job,
            manifests=manifests,
        )
        candidate = _infer_text(
            context,
            request_id=f"memevolve-generate-{candidate_index}",
            model_name=model_name,
            timeout_seconds=timeout_seconds,
            system_instruction=(
                "Generate one concise, reusable declarative memory.md. Evolve the memory "
                "document's organization and retrieval cues, but return only Markdown. "
                "Never return Python, a provider class, imports, configuration code, or "
                "instructions to execute generated code."
            ),
            prompt=(
                f"# Independent analysis branch {candidate_index}\n\n{analysis}\n\n"
                f"# Evidence and prior memory\n\n{base_prompt}\n\n"
                "Return only the candidate memory.md. Generalize from evidence; do not "
                "copy held-out answers, verifier-private values, or exact expected output."
            ),
            max_bytes=_MAX_MEMORY_BYTES,
        )
        candidate = _validate_candidate(candidate)
        candidate, audit = _guard_generic_reflector_output(
            candidate,
            job=job,
            manifests=manifests,
        )
        candidates.append(candidate)
        candidate_audits.append(audit)

    selection_prompt = _selection_prompt(base_prompt, candidates)
    selection_prompt = _redact_generic_reflector_prompt(
        selection_prompt,
        job=job,
        manifests=manifests,
    )
    selection_text = _infer_text(
        context,
        request_id="memevolve-select",
        model_name=model_name,
        timeout_seconds=timeout_seconds,
        system_instruction=(
            "Select the declarative memory candidate best grounded in the supplied "
            "trajectory evidence. Prefer transferable, concise, non-contradictory, and "
            "actionable memory. Return strict JSON only."
        ),
        prompt=selection_prompt,
        max_bytes=_MAX_SELECTION_BYTES,
    )
    selection = _parse_selection(selection_text)
    if selection.winner_index > len(candidates):
        raise ValueError("MemEvolve winner index is outside generated candidates")
    selected_memory = candidates[selection.winner_index - 1]
    selected_audit = candidate_audits[selection.winner_index - 1]

    output_dir = context.artifact_root / "workers" / job.job_id / _METHOD_ID
    output_dir.mkdir(parents=True, exist_ok=True)
    memory_path = output_dir / "memory.md"
    memory_path.write_text(
        _ensure_trailing_newline(selected_memory),
        encoding="utf-8",
    )

    input_artifact_ids = [artifact.artifact_id for artifact in job.input_artifacts]
    dataset_ids = [artifact.artifact_id for artifact in datasets]
    prior_memory_ids = [artifact.artifact_id for artifact in prior_artifacts]
    lineage = {
        **_dict_config(core_config.get("lineage")),
        "method": _METHOD_ID,
        "input_artifact_ids": input_artifact_ids,
        "source_dataset_artifact_ids": dataset_ids,
        "consumed_dataset_artifact_ids": consumed_dataset_ids,
        "prior_text_memory_artifact_ids": prior_memory_ids,
    }
    scores = _scores_config(core_config.get("scores"))
    scores["quality"] = float(selection.quality)
    return [
        ArtifactRegisterRequest(
            type=ArtifactType.TEXT_MEMORY,
            name=str(
                core_config.get("name")
                or f"{datasets[0].name or datasets[0].artifact_id} MemEvolve memory"
            ),
            uri=memory_path.resolve().as_uri(),
            manifest={
                "content_path": "memory.md",
                "method": _METHOD_ID,
                "algorithm_family": "MemEvolve",
                "adaptation_scope": "declarative_text_memory_v1",
                "paper_equivalent": False,
                "provider_runtime": "static_markdown",
                "source_dataset_artifact_ids": dataset_ids,
                "consumed_dataset_artifact_ids": consumed_dataset_ids,
                "source_record_count": source_record_count,
                "reflected_record_count": len(reflected_records),
                "prior_memory_artifact_id": prior_memory_id,
                "candidate_count": candidate_count,
                "selected_candidate_index": selection.winner_index,
                "selection_mode": "codex_evidence_judge_v1",
                "reflector_provider": "codex_cli",
                "reflector_model": model_name,
                "prompt_truncated": prompt_truncated,
                "reflection_audit": selected_audit,
            },
            lineage=lineage,
            compatibility=_dict_config(core_config.get("compatibility")),
            scores=scores,
            tags=_string_list(core_config.get("tags")),
            promoted=bool(core_config.get("promoted", False)),
        )
    ]


def _harness_config(config: dict[str, Any]) -> tuple[str, float]:
    value = config.get("reflector_llm")
    if not isinstance(value, dict):
        raise ValueError(f"{_METHOD_ID} requires reflector_llm config")
    if value.get("provider") != "codex_cli":
        raise ValueError(f"{_METHOD_ID} requires the Core Codex harness")
    model = value.get("model")
    if not isinstance(model, str) or not model.strip():
        raise ValueError(f"{_METHOD_ID} requires reflector_llm.model")
    timeout = value.get("timeout_seconds", 300.0)
    if isinstance(timeout, bool) or not isinstance(timeout, int | float):
        raise ValueError("reflector_llm.timeout_seconds must be numeric")
    timeout_seconds = float(timeout)
    if not 0.0 < timeout_seconds <= 86_400.0:
        raise ValueError("reflector_llm.timeout_seconds is outside the supported range")
    return model.strip(), timeout_seconds


def _bounded_int(
    value: object,
    *,
    label: str,
    minimum: int,
    maximum: int,
) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{label} must be an integer")
    if not minimum <= value <= maximum:
        raise ValueError(f"{label} is outside the supported range")
    return value


def _base_prompt(
    *,
    reflected_records: list[dict[str, str]],
    prior_memory: str,
) -> tuple[str, bool]:
    evidence, evidence_truncated = _clip_utf8(
        json.dumps(reflected_records, ensure_ascii=False, indent=2, sort_keys=True),
        _MAX_EVIDENCE_BYTES,
    )
    prior, prior_truncated = _clip_utf8(prior_memory, _MAX_PRIOR_MEMORY_BYTES)
    prompt = "\n".join(
        (
            "# OpenEvo MemEvolve Declarative Context",
            "",
            "This is a safety-constrained adaptation. Evolve a static Markdown memory, "
            "not an executable BaseMemoryProvider. The resulting artifact is prepended "
            "to a future OpenEvo session.",
            "",
            "## Prior Memory",
            "",
            prior or "(none)",
            "",
            "## Trajectory Evidence",
            "",
            evidence or "[]",
            "",
            "## Constraints",
            "",
            "- Derive reusable behavior, checks, retrieval cues, and retirement rules.",
            "- Treat prior memory as revisable evidence, not ground truth.",
            "- Do not copy exact held-out answers or verifier-private data.",
            "- Do not generate or request executable provider code.",
        )
    )
    return prompt, evidence_truncated or prior_truncated


def _selection_prompt(base_prompt: str, candidates: list[str]) -> str:
    lines = [
        "Review the evidence and candidates below.",
        "Return exactly one JSON object with keys winner_index (one-based integer) and "
        "quality (number from 0 through 1). Do not include rationale or extra keys.",
        "",
        base_prompt,
    ]
    for index, candidate in enumerate(candidates, start=1):
        lines.extend(("", f"# Candidate {index}", "", candidate))
    return "\n".join(lines)


def _infer_text(
    context: MethodExecutionContext,
    *,
    request_id: str,
    model_name: str,
    timeout_seconds: float,
    system_instruction: str,
    prompt: str,
    max_bytes: int,
) -> str:
    request = HarnessInferenceRequest(
        request_id=request_id,
        harness_id="codex",
        system_instruction=system_instruction,
        prompt=prompt,
        model_name=model_name,
        timeout_seconds=timeout_seconds,
    )
    response = context.services.harness.infer(request)
    if response.request_id != request.request_id:
        raise ValueError("Core harness response request ID mismatch")
    if response.capture_mode is not CaptureMode.TRANSCRIPT:
        raise ValueError("MemEvolve requires transcript harness capture")
    text = response.text.strip()
    if not text:
        raise ValueError("Core harness returned empty MemEvolve output")
    if len(text.encode("utf-8")) > max_bytes:
        raise ValueError("Core harness MemEvolve output exceeds method budget")
    return text


def _validate_candidate(value: str) -> str:
    candidate = value.strip()
    if not re.search(r"^#{1,6}\s+\S", candidate, flags=re.MULTILINE):
        raise ValueError("MemEvolve candidate must contain a Markdown heading")
    if candidate.startswith("```") and candidate.endswith("```"):
        raise ValueError("MemEvolve candidate must not be wrapped in a code fence")
    lowered = candidate.lower()
    if any(marker in lowered for marker in _GENERATED_PROVIDER_MARKERS):
        raise ValueError("MemEvolve candidate contains executable provider code")
    return candidate


def _parse_selection(value: str) -> _Selection:
    encoded = value.strip()
    if encoded.startswith("```json") and encoded.endswith("```"):
        encoded = encoded[len("```json") : -len("```")].strip()
    try:
        payload = json.loads(encoded)
        return _Selection.model_validate(payload)
    except (json.JSONDecodeError, ValidationError) as exc:
        raise ValueError("MemEvolve selection is not strict valid JSON") from exc


def _clip_utf8(value: str, max_bytes: int) -> tuple[str, bool]:
    encoded = value.encode("utf-8")
    if len(encoded) <= max_bytes:
        return value, False
    suffix = "\n[TRUNCATED TO METHOD INPUT BUDGET]"
    budget = max_bytes - len(suffix.encode("utf-8"))
    clipped = encoded[:budget].decode("utf-8", errors="ignore")
    return clipped + suffix, True


__all__ = ["text_memory_memevolve"]
