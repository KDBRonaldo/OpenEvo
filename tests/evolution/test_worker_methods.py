from __future__ import annotations

import json
import subprocess
import threading
from pathlib import Path
from typing import Any

import httpx
import pytest

from openevo.evolution import methods as methods_module
from openevo.evolution import worker as worker_module
from openevo.evolution.cli import _parse_capabilities
from openevo.evolution.methods import METHOD_REGISTRY, _audit_agent_system_markdown, run_method
from openevo.evolution.models import (
    ArtifactType,
    ContextResolveRequest,
    DatasetCreateRequest,
    EventIngestRequest,
    WorkerClaimInputArtifact,
    WorkerClaimedJob,
)
from openevo.evolution.store import EvolutionStore
from openevo.evolution.worker import run_once


def _dataset_artifact(tmp_path: Path) -> dict[str, Any]:
    dataset_dir = tmp_path / "dataset"
    dataset_dir.mkdir()
    records_path = dataset_dir / "records.jsonl"
    records_path.write_text(
        "\n".join(
            [
                json.dumps(
                    {
                        "event_id": "evt_1",
                        "task_id": "task_alpha",
                        "session_id": "session_1",
                        "status": "COMPLETED",
                        "reward": 1.0,
                        "payload": {"summary": "solved calculator task"},
                    }
                ),
                json.dumps(
                    {
                        "event_id": "evt_2",
                        "task_id": "task_beta",
                        "session_id": "session_2",
                        "status": "ERROR",
                        "reward": 0.1,
                    }
                ),
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    manifest_path = dataset_dir / "manifest.json"
    manifest_path.write_text(
        json.dumps(
            {
                "dataset_id": "ds_test",
                "name": "test dataset",
                "records_path": "records.jsonl",
                "records_uri": records_path.as_uri(),
                "event_count": 2,
            }
        ),
        encoding="utf-8",
    )
    return {
        "artifact_id": "art_dataset",
        "type": "dataset",
        "uri": manifest_path.as_uri(),
        "name": "test dataset",
    }


def _reflector_dataset_artifact(tmp_path: Path) -> dict[str, Any]:
    dataset_dir = tmp_path / "reflector-dataset"
    dataset_dir.mkdir()
    records_path = dataset_dir / "records.jsonl"
    records_path.write_text(
        "\n".join(
            [
                json.dumps(
                    {
                        "event_id": "evt_success",
                        "task_id": "task_parser_success",
                        "session_id": "session_success",
                        "status": "COMPLETED",
                        "reward": 1.0,
                        "traces": [
                            {
                                "prompt_messages": [
                                    {"role": "user", "content": "Fix the parser precedence bug."}
                                ],
                                "response_messages": [
                                    {
                                        "role": "assistant",
                                        "content": (
                                            "Added a regression test first, then fixed parser "
                                            "precedence."
                                        ),
                                    }
                                ],
                                "metadata": {
                                    "capture_mode": "transcript",
                                    "token_level_metrics_available": False,
                                },
                            }
                        ],
                    }
                ),
                json.dumps(
                    {
                        "event_id": "evt_failure",
                        "task_id": "task_packaging_failure",
                        "session_id": "session_failure",
                        "status": "ERROR",
                        "reward": 0.0,
                        "traces": [
                            {
                                "prompt_messages": [
                                    {"role": "user", "content": "Fix the package import bug."}
                                ],
                                "response_messages": [
                                    {
                                        "role": "assistant",
                                        "content": (
                                            "Changed files without running the focused test."
                                        ),
                                    }
                                ],
                                "metadata": {
                                    "capture_mode": "transcript",
                                    "token_level_metrics_available": False,
                                    "transcript": "pytest failed after an unverified edit",
                                },
                            }
                        ],
                    }
                ),
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    manifest_path = dataset_dir / "manifest.json"
    manifest_path.write_text(
        json.dumps(
            {
                "dataset_id": "ds_reflector",
                "name": "reflector dataset",
                "records_path": "records.jsonl",
                "records_uri": records_path.as_uri(),
                "event_count": 2,
                "trace_count": 2,
            }
        ),
        encoding="utf-8",
    )
    return {
        "artifact_id": "art_dataset_reflector",
        "type": "dataset",
        "uri": manifest_path.as_uri(),
        "name": "reflector dataset",
    }


def _parametric_dataset_artifact(
    tmp_path: Path,
    records: list[dict[str, Any]],
    *,
    artifact_id: str = "art_dataset_parametric",
) -> dict[str, Any]:
    dataset_dir = tmp_path / artifact_id
    dataset_dir.mkdir()
    records_path = dataset_dir / "records.jsonl"
    records_path.write_text(
        "".join(json.dumps(record) + "\n" for record in records),
        encoding="utf-8",
    )
    manifest_path = dataset_dir / "manifest.json"
    manifest_path.write_text(
        json.dumps(
            {
                "dataset_id": f"ds_{artifact_id}",
                "name": f"{artifact_id} dataset",
                "records_path": "records.jsonl",
                "records_uri": records_path.as_uri(),
                "event_count": len(records),
            }
        ),
        encoding="utf-8",
    )
    return {
        "artifact_id": artifact_id,
        "type": "dataset",
        "uri": manifest_path.as_uri(),
        "name": f"{artifact_id} dataset",
    }


def _expel_memory_dataset_artifact(tmp_path: Path) -> dict[str, Any]:
    dataset_dir = tmp_path / "expel-memory-dataset"
    dataset_dir.mkdir()
    records_path = dataset_dir / "records.jsonl"
    records_path.write_text(
        "\n".join(
            [
                json.dumps(
                    {
                        "event_id": "evt_success",
                        "task_id": "tb_pass_task",
                        "session_id": "session_success",
                        "status": "COMPLETED",
                        "reward": 1.0,
                        "traces": [
                            {
                                "prompt_messages": [
                                    {"role": "user", "content": "Repair the CLI test failure."}
                                ],
                                "response_messages": [
                                    {
                                        "role": "assistant",
                                        "content": (
                                            "Inspected failing tests first, changed the parser, "
                                            "then reran the exact focused test."
                                        ),
                                    }
                                ],
                                "metadata": {
                                    "capture_mode": "transcript",
                                    "token_level_metrics_available": False,
                                },
                            }
                        ],
                    }
                ),
                json.dumps(
                    {
                        "event_id": "evt_failure",
                        "task_id": "tb_fail_task",
                        "session_id": "session_failure",
                        "status": "COMPLETED",
                        "reward": 0.0,
                        "traces": [
                            {
                                "prompt_messages": [
                                    {"role": "user", "content": "Fix the package test."}
                                ],
                                "response_messages": [
                                    {
                                        "role": "assistant",
                                        "content": (
                                            "Edited multiple files and gave a final answer "
                                            "without rerunning pytest."
                                        ),
                                    }
                                ],
                                "metadata": {
                                    "capture_mode": "transcript",
                                    "token_level_metrics_available": False,
                                    "transcript": "verifier failed after missing validation",
                                },
                            }
                        ],
                    }
                ),
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    manifest_path = dataset_dir / "manifest.json"
    manifest_path.write_text(
        json.dumps(
            {
                "dataset_id": "ds_expel",
                "name": "expel memory dataset",
                "records_path": "records.jsonl",
                "records_uri": records_path.as_uri(),
                "event_count": 2,
                "trace_count": 2,
            }
        ),
        encoding="utf-8",
    )
    return {
        "artifact_id": "art_dataset_expel",
        "type": "dataset",
        "uri": manifest_path.as_uri(),
        "name": "expel memory dataset",
    }


def _expel_history_dataset_artifact(tmp_path: Path) -> dict[str, Any]:
    dataset_dir = tmp_path / "expel-history-dataset"
    dataset_dir.mkdir()
    records_path = dataset_dir / "records.jsonl"
    records_path.write_text(
        json.dumps(
            {
                "event_id": "evt_history",
                "task_id": "tb_history_task",
                "session_id": "session_history",
                "status": "COMPLETED",
                "reward": 1.0,
                "traces": [
                    {
                        "prompt_messages": [
                            {"role": "user", "content": "Recover the hidden G-code text."}
                        ],
                        "response_messages": [
                            {
                                "role": "assistant",
                                "content": (
                                    "Inspected extrusion geometry instead of relying on slicer "
                                    "comments."
                                ),
                            }
                        ],
                        "metadata": {
                            "capture_mode": "transcript",
                            "token_level_metrics_available": False,
                        },
                    }
                ],
            }
        )
        + "\n",
        encoding="utf-8",
    )
    manifest_path = dataset_dir / "manifest.json"
    manifest_path.write_text(
        json.dumps(
            {
                "dataset_id": "ds_expel_history",
                "name": "expel history dataset",
                "records_path": "records.jsonl",
                "records_uri": records_path.as_uri(),
                "event_count": 1,
                "trace_count": 1,
            }
        ),
        encoding="utf-8",
    )
    return {
        "artifact_id": "art_dataset_expel_history",
        "type": "dataset",
        "uri": manifest_path.as_uri(),
        "name": "expel history dataset",
    }


def _prior_text_memory_artifact(tmp_path: Path) -> dict[str, Any]:
    memory_path = tmp_path / "prior-memory.md"
    memory_path.write_text(
        "# Terminal Bench Textual Memory\n\n"
        "## Do\n"
        "- Run a broad test suite before inspecting the focused failure.\n",
        encoding="utf-8",
    )
    return {
        "artifact_id": "art_prior_memory",
        "type": "text_memory",
        "uri": memory_path.as_uri(),
        "name": "prior memory",
    }


def _golden_feedback_dataset_artifact(tmp_path: Path) -> dict[str, Any]:
    dataset_dir = tmp_path / "golden-feedback-dataset"
    dataset_dir.mkdir()
    records_path = dataset_dir / "records.jsonl"
    records_path.write_text(
        json.dumps(
            {
                "event_id": "evt_golden_feedback",
                "task_id": "task_biology",
                "session_id": "session_golden_feedback",
                "status": "COMPLETED",
                "reward": 1.0,
                "payload": {
                    "session_result": {
                        "metadata": {
                            "evolution_feedback": {
                                "golden_standard": (
                                    "## Shared Golden Standard Evaluation (Sanitized)\n\n"
                                    "- Aggregate fit: precision=0.250, recall=0.900, f1=0.391.\n"
                                    "- Primary gap: over-inclusion. Generalize the component-boundary "
                                    "method without copying held-out literals."
                                )
                            }
                        }
                    }
                },
            }
        )
        + "\n",
        encoding="utf-8",
    )
    manifest_path = dataset_dir / "manifest.json"
    manifest_path.write_text(
        json.dumps(
            {
                "dataset_id": "ds_golden_feedback",
                "name": "golden feedback dataset",
                "records_path": "records.jsonl",
                "records_uri": records_path.as_uri(),
                "event_count": 1,
            }
        ),
        encoding="utf-8",
    )
    return {
        "artifact_id": "art_dataset_golden_feedback",
        "type": "dataset",
        "uri": manifest_path.as_uri(),
        "name": "golden feedback dataset",
    }


def _history_round_dataset_artifact(
    tmp_path: Path,
    *,
    round_number: int,
    precision: float,
    recall: float,
    f1: float,
    record: dict[str, Any],
) -> dict[str, Any]:
    dataset_dir = tmp_path / f"history-round-{round_number}"
    dataset_dir.mkdir()
    records_path = dataset_dir / "records.jsonl"
    records_path.write_text(json.dumps(record) + "\n", encoding="utf-8")
    manifest_path = dataset_dir / "manifest.json"
    manifest_path.write_text(
        json.dumps(
            {
                "dataset_id": f"ds_history_round_{round_number}",
                "name": f"history round {round_number}",
                "round": round_number,
                "agent_system_artifact_id": f"agent_system_round_{round_number}",
                "records_path": "records.jsonl",
                "records_uri": records_path.as_uri(),
                "event_count": 1,
                "metrics": {
                    "precision": precision,
                    "recall": recall,
                    "f1": f1,
                    "true_positive": int(f1 * 1000),
                    "false_positive": int((1 - precision) * 1000),
                    "false_negative": int((1 - recall) * 1000),
                    "duplicate_predictions": round_number,
                },
            }
        ),
        encoding="utf-8",
    )
    return {
        "artifact_id": f"art_history_round_{round_number}",
        "type": "dataset",
        "uri": manifest_path.as_uri(),
        "name": f"history round {round_number}",
    }


def _job(
    method: str,
    tmp_path: Path,
    *,
    config: dict[str, Any] | None = None,
    input_artifacts: list[dict[str, Any]] | None = None,
) -> WorkerClaimedJob:
    return WorkerClaimedJob(
        job_id=f"job_{method}",
        lease_id=f"lease_{method}",
        job_type="reference",
        method=method,
        input_artifacts=input_artifacts or [],
        config=config or {"promoted": True},
    )


def _patch_reflector_llm(
    monkeypatch: pytest.MonkeyPatch,
    content: str = (
        "# Evolved Agent System\n\n"
        "- Before finalizing, verify the reflected rules against the task constraints."
    ),
) -> dict[str, Any]:
    return _patch_reflector_llm_sequence(monkeypatch, [content])


def _patch_reflector_llm_sequence(
    monkeypatch: pytest.MonkeyPatch,
    contents: list[str],
) -> dict[str, Any]:
    captured: dict[str, Any] = {}
    captured["requests"] = []
    responses = list(contents)

    class FakeClient:
        def __init__(self, **kwargs: Any) -> None:
            captured["client_kwargs"] = kwargs

        def __enter__(self) -> "FakeClient":
            return self

        def __exit__(self, *args: Any) -> None:
            return None

        def post(
            self,
            url: str,
            *,
            headers: dict[str, str],
            json: dict[str, Any],
        ) -> httpx.Response:
            if responses:
                response_content = responses.pop(0)
            else:
                response_content = contents[-1]
            captured["url"] = url
            captured["headers"] = headers
            captured["json"] = json
            captured["requests"].append({"url": url, "headers": headers, "json": json})
            return httpx.Response(
                200,
                json={"choices": [{"message": {"content": response_content}}]},
                request=httpx.Request("POST", url),
            )

    monkeypatch.setattr(httpx, "Client", FakeClient)
    return captured


def test_text_memory_method_writes_markdown_from_dataset(tmp_path):
    job = _job(
        "text_memory",
        tmp_path,
        input_artifacts=[_dataset_artifact(tmp_path)],
        config={
            "compatibility": {"task_tags": ["openevo_project:project-1"]},
            "promoted": True,
        },
    )

    artifacts = run_method(job, artifact_root=tmp_path / "artifacts")

    assert len(artifacts) == 1
    artifact = artifacts[0]
    assert artifact.type == ArtifactType.TEXT_MEMORY
    assert artifact.promoted is True
    assert artifact.uri.endswith("/memory.md")
    memory_path = Path(artifact.uri.removeprefix("file://"))
    memory = memory_path.read_text(encoding="utf-8")
    assert "task_alpha" in memory
    assert "session_1" in memory
    assert "reward=1.0" in memory
    assert "solved calculator task" in memory
    assert artifact.manifest["source_dataset_artifact_id"] == "art_dataset"
    assert artifact.manifest["record_count"] == 2
    assert artifact.compatibility == {"task_tags": ["openevo_project:project-1"]}


def test_text_memory_method_rejects_non_dataset_input_artifact(tmp_path):
    report_path = tmp_path / "report.json"
    report_path.write_text(
        json.dumps({"records_path": "records.jsonl"}),
        encoding="utf-8",
    )
    job = _job(
        "text_memory",
        tmp_path,
        input_artifacts=[
            {
                "artifact_id": "art_report",
                "type": "report",
                "uri": report_path.as_uri(),
                "name": "report",
            }
        ],
    )

    try:
        run_method(job, artifact_root=tmp_path / "artifacts")
    except ValueError as exc:
        assert "requires an input dataset artifact" in str(exc)
    else:
        raise AssertionError("expected ValueError")


def test_skill_bundle_method_writes_skill_markdown(tmp_path):
    job = _job(
        "skill_bundle",
        tmp_path,
        config={
            "name": "calculator-helper",
            "skill_markdown": "# Calculator Helper\n\nUse exact arithmetic.",
            "compatibility": {"task_tags": ["openevo_project:project-1"]},
        },
    )

    artifacts = run_method(job, artifact_root=tmp_path / "artifacts")

    artifact = artifacts[0]
    assert artifact.type == ArtifactType.SKILL_BUNDLE
    assert artifact.name == "calculator-helper"
    bundle_path = Path(artifact.uri.removeprefix("file://"))
    assert bundle_path.is_dir()
    assert (bundle_path / "SKILL.md").read_text(encoding="utf-8") == (
        "# Calculator Helper\n\nUse exact arithmetic.\n"
    )
    assert artifact.manifest["entrypoint"] == "SKILL.md"
    assert artifact.compatibility == {"task_tags": ["openevo_project:project-1"]}


def test_text_memory_reflector_writes_llm_memory_from_dataset(tmp_path, monkeypatch):
    captured = _patch_reflector_llm(
        monkeypatch,
        content=(
            "# Reusable Task Memory\n\n"
            "- When fixing parser behavior, add a regression test before editing.\n"
            "- Before finalizing, run the focused verification that covers the changed path.\n"
        ),
    )
    job = _job(
        "text_memory_reflector",
        tmp_path,
        input_artifacts=[_reflector_dataset_artifact(tmp_path)],
        config={
            "name": "reflected-memory",
            "reflector_llm": {
                "model": "reflector-model",
                "base_url": "http://reflector.test/v1",
                "api_key": "test-key",
            },
            "tags": ["swe"],
            "promoted": True,
        },
    )

    artifacts = run_method(job, artifact_root=tmp_path / "artifacts")

    artifact = artifacts[0]
    assert artifact.type == ArtifactType.TEXT_MEMORY
    assert artifact.name == "reflected-memory"
    assert artifact.promoted is True
    assert artifact.tags == ["swe"]
    assert artifact.manifest["content_path"] == "memory.md"
    assert artifact.manifest["method"] == "text_memory_reflector"
    assert artifact.manifest["source_dataset_artifact_id"] == "art_dataset_reflector"
    assert artifact.manifest["record_count"] == 2
    assert artifact.manifest["reflected_record_count"] == 2
    assert artifact.manifest["reflector_provider"] == "openai_chat"
    assert artifact.manifest["reflector_model"] == "reflector-model"
    assert artifact.lineage["method"] == "text_memory_reflector"
    assert artifact.lineage["input_artifact_ids"] == ["art_dataset_reflector"]

    memory_path = Path(artifact.uri.removeprefix("file://"))
    assert memory_path.name == "memory.md"
    assert memory_path.read_text(encoding="utf-8") == (
        "# Reusable Task Memory\n\n"
        "- When fixing parser behavior, add a regression test before editing.\n"
        "- Before finalizing, run the focused verification that covers the changed path.\n"
    )

    prompt = captured["json"]["messages"][1]["content"]
    assert "text memory" in captured["json"]["messages"][0]["content"]
    assert "reusable task memory" in prompt
    assert "recurring failure modes" in prompt
    assert "validation habits" in prompt
    assert "Fix the parser precedence bug." in prompt
    assert "pytest failed after an unverified edit" in prompt


def test_text_memory_expel_reflector_writes_structured_memory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured = _patch_reflector_llm(
        monkeypatch,
        "# Terminal Bench Textual Memory\n\n"
        "## Do\n"
        "- Run the exact failing test before broad cleanup because the successful trace did that.\n\n"
        "## Avoid\n"
        "- Do not give a final answer without rerunning pytest because the failed trace skipped validation.\n\n"
        "## Validate\n"
        "- Rerun the focused verifier command before final response.\n\n"
        "## When Applicable\n"
        "- Applies when a task has a failing test or verifier output.\n\n"
        "## Retired Or Superseded\n"
        "- Retire broad-first testing when a focused failing test is available.\n",
    )
    job = _job(
        "text_memory_expel_reflector",
        tmp_path,
        input_artifacts=[
            _expel_memory_dataset_artifact(tmp_path),
            _expel_history_dataset_artifact(tmp_path),
            _prior_text_memory_artifact(tmp_path),
        ],
        config={
            "name": "expel-memory",
            "reflector_llm": {
                "model": "reflector-model",
                "base_url": "http://reflector.test/v1",
                "api_key": "test-key",
            },
            "compatibility": {"task_tags": ["terminal-bench"]},
            "scores": {"quality": 0.4},
            "tags": ["terminal-bench"],
            "promoted": True,
        },
    )

    [artifact] = run_method(job, artifact_root=tmp_path / "artifacts")

    assert artifact.type == ArtifactType.TEXT_MEMORY
    assert artifact.name == "expel-memory"
    assert artifact.promoted is True
    assert artifact.compatibility == {"task_tags": ["terminal-bench"]}
    assert artifact.scores == {"quality": 0.4}
    assert artifact.tags == ["terminal-bench"]
    assert artifact.manifest["content_path"] == "memory.md"
    assert artifact.manifest["method"] == "text_memory_expel_reflector"
    assert artifact.manifest["source_dataset_artifact_id"] == "art_dataset_expel"
    assert artifact.manifest["source_dataset_artifact_ids"] == [
        "art_dataset_expel",
        "art_dataset_expel_history",
    ]
    assert artifact.manifest["record_count"] == 3
    assert artifact.manifest["reflected_record_count"] == 3
    assert artifact.manifest["success_count"] == 2
    assert artifact.manifest["failure_count"] == 1
    assert artifact.manifest["prior_memory_count"] == 1
    assert artifact.manifest["required_sections"] == [
        "Do",
        "Avoid",
        "Validate",
        "When Applicable",
        "Retired Or Superseded",
    ]
    assert artifact.lineage["method"] == "text_memory_expel_reflector"
    assert artifact.lineage["input_artifact_ids"] == [
        "art_dataset_expel",
        "art_dataset_expel_history",
        "art_prior_memory",
    ]
    assert artifact.lineage["source_dataset_artifact_ids"] == [
        "art_dataset_expel",
        "art_dataset_expel_history",
    ]

    memory_path = Path(artifact.uri.removeprefix("file://"))
    memory = memory_path.read_text(encoding="utf-8")
    assert memory.endswith("\n")
    for section in artifact.manifest["required_sections"]:
        assert f"## {section}" in memory
    assert "focused verifier" in memory

    prompt = captured["json"]["messages"][1]["content"]
    assert "ExpeL" in prompt
    assert "## Existing Text Memory" in prompt
    assert "Run a broad test suite" in prompt
    assert "tb_pass_task" in prompt
    assert "art_dataset_expel_history" in prompt
    assert "tb_history_task" in prompt
    assert "extrusion geometry" in prompt
    assert "verifier failed after missing validation" in prompt
    assert "## Do" in prompt
    assert "## Avoid" in prompt


def test_text_memory_expel_reflector_rejects_missing_required_sections(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_reflector_llm(
        monkeypatch,
        "# Terminal Bench Textual Memory\n\n## Do\n- Rerun the exact test.\n",
    )
    job = _job(
        "text_memory_expel_reflector",
        tmp_path,
        input_artifacts=[_expel_memory_dataset_artifact(tmp_path)],
        config={
            "reflector_llm": {
                "model": "reflector-model",
                "base_url": "http://reflector.test/v1",
                "api_key": "test-key",
            },
        },
    )

    with pytest.raises(ValueError, match="missing required memory sections"):
        run_method(job, artifact_root=tmp_path / "artifacts")


@pytest.mark.parametrize(
    ("method", "error"),
    [
        ("text_memory_expel_reflector", "requires an input dataset artifact"),
        ("skill_bundle_reflector", "requires an input dataset artifact"),
        ("agent_system_gepa_reflector", "requires at least one dataset artifact"),
    ],
)
def test_canonical_reflector_methods_require_dataset(
    tmp_path: Path,
    method: str,
    error: str,
) -> None:
    with pytest.raises(ValueError, match=error):
        run_method(_job(method, tmp_path), artifact_root=tmp_path / "artifacts")


@pytest.mark.parametrize(
    ("method", "response"),
    [
        (
            "text_memory_expel_reflector",
            "# Memory\n\n"
            "## Do\n- Inspect the failure.\n\n"
            "## Avoid\n- Avoid unverified edits.\n\n"
            "## Validate\n- Run focused tests.\n\n"
            "## When Applicable\n- Apply to test failures.\n\n"
            "## Retired Or Superseded\n- Retire stale advice.\n",
        ),
        (
            "skill_bundle_reflector",
            "---\nname: focused-reflection\n"
            "description: Use when reflecting on task failures.\n---\n\n"
            "# Focused Reflection\n\nRun the focused test before finalizing.\n",
        ),
    ],
)
def test_canonical_text_reflectors_default_to_twenty_records(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    method: str,
    response: str,
) -> None:
    captured = _patch_reflector_llm(monkeypatch, response)
    records = [
        {
            "event_id": "evt_skipped",
            "task_id": "task_skipped",
            "session_id": "session_skipped",
            "status": "COMPLETED",
            "reward": 1.0,
            "traces": [],
        },
        *[
            {
                "event_id": f"evt_{index}",
                "task_id": f"task_{index}",
                "session_id": f"session_{index}",
                "status": "COMPLETED",
                "reward": 1.0,
                "traces": [
                    {
                        "prompt_messages": [
                            {
                                "role": "user",
                                "content": f"PROMPT_{index:02d}_MARKER",
                            }
                        ],
                        "response_messages": [{"role": "assistant", "content": f"Result {index}"}],
                    }
                ],
            }
            for index in range(21)
        ],
    ]
    job = _job(
        method,
        tmp_path,
        input_artifacts=[_parametric_dataset_artifact(tmp_path, records)],
        config={
            "reflector_llm": {
                "model": "reflector-model",
                "base_url": "http://reflector.test/v1",
                "api_key": "test-key",
            }
        },
    )

    [artifact] = run_method(job, artifact_root=tmp_path / "artifacts")

    prompt = captured["json"]["messages"][1]["content"]
    assert artifact.manifest["record_count"] == 22
    assert artifact.manifest["reflected_record_count"] == 20
    assert "PROMPT_00_MARKER" in prompt
    assert "PROMPT_19_MARKER" in prompt
    assert "PROMPT_20_MARKER" not in prompt
    assert "task_skipped" not in prompt


def test_text_memory_reflector_includes_prior_text_memory_in_prompt(
    tmp_path,
    monkeypatch,
):
    captured = _patch_reflector_llm(
        monkeypatch,
        content="# Updated Memory\n\n- Preserve prior validation habits.\n",
    )
    memory_round1 = tmp_path / "memory-round1.md"
    memory_round1.write_text(
        "# Round 1 Memory\n\n- Inventory files before extraction.\n",
        encoding="utf-8",
    )
    memory_round2 = tmp_path / "memory-round2.md"
    memory_round2.write_text(
        "# Round 2 Memory\n\n- Verify output counts against source coverage.\n",
        encoding="utf-8",
    )
    job = _job(
        "text_memory_reflector",
        tmp_path,
        input_artifacts=[
            _reflector_dataset_artifact(tmp_path),
            {
                "artifact_id": "art_memory_round_1",
                "type": "text_memory",
                "uri": memory_round1.as_uri(),
                "name": "round 1 memory",
            },
            {
                "artifact_id": "art_memory_round_2",
                "type": "text_memory",
                "uri": memory_round2.as_uri(),
                "name": "round 2 memory",
            },
        ],
        config={
            "name": "reflected-memory",
            "reflector_llm": {
                "model": "reflector-model",
                "base_url": "http://reflector.test/v1",
                "api_key": "test-key",
            },
        },
    )

    artifact = run_method(job, artifact_root=tmp_path / "artifacts")[0]

    prompt = captured["json"]["messages"][1]["content"]
    assert "## Existing Text Memory" in prompt
    assert "# Round 1 Memory" in prompt
    assert "Inventory files before extraction" in prompt
    assert "# Round 2 Memory" in prompt
    assert "Verify output counts against source coverage" in prompt
    assert artifact.lineage["input_artifact_ids"] == [
        "art_dataset_reflector",
        "art_memory_round_1",
        "art_memory_round_2",
    ]


def test_text_memory_reflector_redacts_forbidden_output_literals(
    tmp_path,
    monkeypatch,
):
    _patch_reflector_llm(
        monkeypatch,
        content=(
            "# Leaky Memory\n\n- Secret Heldout Paper requires values from golden_source.xlsx.\n"
        ),
    )
    job = _job(
        "text_memory_reflector",
        tmp_path,
        input_artifacts=[_reflector_dataset_artifact(tmp_path)],
        config={
            "reflector_llm": {
                "model": "reflector-model",
                "base_url": "http://reflector.test/v1",
                "api_key": "test-key",
            },
            "agent_system_audit": {
                "forbidden_literals": {
                    "article_titles": ["Secret Heldout Paper"],
                    "source_files": ["golden_source.xlsx"],
                }
            },
            "promoted": True,
        },
    )

    artifact = run_method(job, artifact_root=tmp_path / "artifacts")[0]

    memory = Path(artifact.uri.removeprefix("file://")).read_text(encoding="utf-8")
    assert "Secret Heldout Paper" not in memory
    assert "golden_source.xlsx" not in memory
    assert "[REDACTED_ARTICLE_TITLES_" in memory
    assert "[REDACTED_SOURCE_FILES_" in memory
    assert artifact.manifest["reflection_audit"]["finding_count"] >= 1
    assert artifact.manifest["reflection_audit"]["redaction_count"] == 1


def test_skill_bundle_reflector_writes_llm_skill_from_dataset_and_base(
    tmp_path,
    monkeypatch,
):
    captured = _patch_reflector_llm(
        monkeypatch,
        content=(
            "---\n"
            "name: verification-habits\n"
            "description: Use when editing parser or packaging code.\n"
            "---\n\n"
            "# Verification Habits\n\n"
            "Run the focused regression before finalizing parser or packaging changes.\n"
        ),
    )
    base_dir = tmp_path / "base-skill"
    base_dir.mkdir()
    (base_dir / "SKILL.md").write_text(
        "# Base Skill\n\nKeep regression tests close to the changed behavior.\n",
        encoding="utf-8",
    )
    job = _job(
        "skill_bundle_reflector",
        tmp_path,
        input_artifacts=[
            _reflector_dataset_artifact(tmp_path),
            {
                "artifact_id": "art_previous_skill",
                "type": "skill_bundle",
                "uri": base_dir.as_uri(),
                "name": "previous skill",
            },
        ],
        config={
            "name": "verification-habits",
            "reflector_llm": {
                "model": "reflector-model",
                "base_url": "http://reflector.test/v1",
                "api_key": "test-key",
            },
        },
    )

    artifacts = run_method(job, artifact_root=tmp_path / "artifacts")

    artifact = artifacts[0]
    assert artifact.type == ArtifactType.SKILL_BUNDLE
    assert artifact.name == "verification-habits"
    assert artifact.manifest["entrypoint"] == "SKILL.md"
    assert artifact.manifest["files"] == ["SKILL.md"]
    assert artifact.manifest["method"] == "skill_bundle_reflector"
    assert artifact.manifest["source_dataset_artifact_id"] == "art_dataset_reflector"
    assert artifact.manifest["record_count"] == 2
    assert artifact.manifest["reflected_record_count"] == 2
    assert artifact.manifest["reflector_provider"] == "openai_chat"
    assert artifact.manifest["reflector_model"] == "reflector-model"
    assert artifact.lineage["method"] == "skill_bundle_reflector"
    assert artifact.lineage["input_artifact_ids"] == [
        "art_dataset_reflector",
        "art_previous_skill",
    ]

    bundle_path = Path(artifact.uri.removeprefix("file://"))
    assert bundle_path.is_dir()
    assert (bundle_path / "SKILL.md").read_text(encoding="utf-8") == (
        "---\n"
        "name: verification-habits\n"
        "description: Use when editing parser or packaging code.\n"
        "---\n\n"
        "# Verification Habits\n\n"
        "Run the focused regression before finalizing parser or packaging changes.\n"
    )

    prompt = captured["json"]["messages"][1]["content"]
    assert "Codex skill bundle" in captured["json"]["messages"][0]["content"]
    assert "Codex skill bundle" in prompt
    assert "SKILL.md" in prompt
    assert "# Base Skill" in prompt
    assert "Fix the package import bug." in prompt


def test_skill_bundle_reflector_uses_latest_prior_skill_as_base(
    tmp_path,
    monkeypatch,
):
    captured = _patch_reflector_llm(
        monkeypatch,
        content=("---\nname: latest-skill\ndescription: Use latest skill base.\n---\n"),
    )
    old_dir = tmp_path / "old-skill"
    old_dir.mkdir()
    (old_dir / "SKILL.md").write_text(
        "# Old Skill\n\nDo not preserve stale round-one behavior.\n",
        encoding="utf-8",
    )
    latest_dir = tmp_path / "latest-skill"
    latest_dir.mkdir()
    (latest_dir / "SKILL.md").write_text(
        "# Latest Skill\n\nPreserve the round-two validation checklist.\n",
        encoding="utf-8",
    )
    job = _job(
        "skill_bundle_reflector",
        tmp_path,
        input_artifacts=[
            _reflector_dataset_artifact(tmp_path),
            {
                "artifact_id": "art_old_skill",
                "type": "skill_bundle",
                "uri": old_dir.as_uri(),
                "name": "old skill",
            },
            {
                "artifact_id": "art_latest_skill",
                "type": "skill_bundle",
                "uri": latest_dir.as_uri(),
                "name": "latest skill",
            },
        ],
        config={
            "name": "latest-skill",
            "reflector_llm": {
                "model": "reflector-model",
                "base_url": "http://reflector.test/v1",
                "api_key": "test-key",
            },
        },
    )

    artifact = run_method(job, artifact_root=tmp_path / "artifacts")[0]

    prompt = captured["json"]["messages"][1]["content"]
    assert "# Latest Skill" in prompt
    assert "round-two validation checklist" in prompt
    assert "# Old Skill" not in prompt
    assert "stale round-one behavior" not in prompt
    assert artifact.lineage["input_artifact_ids"] == [
        "art_dataset_reflector",
        "art_old_skill",
        "art_latest_skill",
    ]
    assert artifact.manifest["base_skill_bundle_artifact_id"] == "art_latest_skill"


def test_skill_bundle_reflector_redacts_forbidden_output_literals(
    tmp_path,
    monkeypatch,
):
    _patch_reflector_llm(
        monkeypatch,
        content=(
            "---\n"
            "name: leaky-skill\n"
            "description: Use for Secret Heldout Paper.\n"
            "---\n\n"
            "# Leaky Skill\n\n"
            "Inspect golden_source.xlsx before finalizing.\n"
        ),
    )
    job = _job(
        "skill_bundle_reflector",
        tmp_path,
        input_artifacts=[_reflector_dataset_artifact(tmp_path)],
        config={
            "name": "leaky-skill",
            "reflector_llm": {
                "model": "reflector-model",
                "base_url": "http://reflector.test/v1",
                "api_key": "test-key",
            },
            "agent_system_audit": {
                "forbidden_literals": {
                    "article_titles": ["Secret Heldout Paper"],
                    "source_files": ["golden_source.xlsx"],
                }
            },
            "promoted": True,
        },
    )

    artifact = run_method(job, artifact_root=tmp_path / "artifacts")[0]

    skill = (Path(artifact.uri.removeprefix("file://")) / "SKILL.md").read_text(encoding="utf-8")
    assert "Secret Heldout Paper" not in skill
    assert "golden_source.xlsx" not in skill
    assert "[REDACTED_ARTICLE_TITLES_" in skill
    assert "[REDACTED_SOURCE_FILES_" in skill
    assert artifact.manifest["reflection_audit"]["finding_count"] >= 1
    assert artifact.manifest["reflection_audit"]["redaction_count"] == 1


def test_reflector_methods_are_registered():
    assert METHOD_REGISTRY["text_memory_reflector"]
    assert METHOD_REGISTRY["skill_bundle_reflector"]


def test_agent_system_method_writes_harness_instruction_file(tmp_path):
    job = _job(
        "agent_system",
        tmp_path,
        config={
            "name": "codex-agent-system",
            "agent_system_markdown": "Prefer repository-local conventions.",
            "target_path": "AGENTS.md",
            "promoted": True,
        },
    )

    artifacts = run_method(job, artifact_root=tmp_path / "artifacts")

    artifact = artifacts[0]
    assert artifact.type == ArtifactType.AGENT_SYSTEM
    assert artifact.name == "codex-agent-system"
    instruction_path = Path(artifact.uri.removeprefix("file://"))
    assert instruction_path.name == "AGENTS.md"
    assert instruction_path.read_text(encoding="utf-8") == (
        "Prefer repository-local conventions.\n"
    )
    assert artifact.manifest["target_path"] == "AGENTS.md"
    assert artifact.manifest["content_path"] == "AGENTS.md"
    assert artifact.promoted is True


def test_agent_system_method_preserves_selection_metadata(tmp_path):
    job = _job(
        "agent_system",
        tmp_path,
        config={
            "agent_system_markdown": "Prefer Codex repository instructions.",
            "compatibility": {"agent_harness": ["codex"], "task_tags": ["calculator"]},
            "scores": {"quality": 0.7},
            "lineage": {"source": "unit-test"},
            "promoted": True,
        },
    )
    artifact_request = run_method(job, artifact_root=tmp_path / "artifacts")[0]

    assert artifact_request.compatibility == {
        "agent_harness": ["codex"],
        "task_tags": ["calculator"],
    }
    assert artifact_request.scores == {"quality": 0.7}
    assert artifact_request.lineage == {"source": "unit-test"}

    store = EvolutionStore(db_path=tmp_path / "evolution.db", artifact_root=tmp_path / "artifacts")
    store.initialize()
    artifact = store.register_artifact(artifact_request)

    other_context = store.resolve_context(
        ContextResolveRequest(
            task_id="task_other",
            instruction="fix parser",
            agent={"harness": "other"},
            metadata={"task_tags": ["calculator"]},
        )
    )
    codex_context = store.resolve_context(
        ContextResolveRequest(
            task_id="task_codex",
            instruction="fix parser",
            agent={"harness": "codex"},
            metadata={"task_tags": ["calculator"]},
        )
    )

    assert artifact.artifact_id not in other_context.selection["artifact_ids"]
    assert artifact.artifact_id in codex_context.agent_system["artifact_ids"]


def test_agent_system_reflector_writes_agents_md_from_dataset_trajectories(
    tmp_path,
    monkeypatch,
):
    _patch_reflector_llm(
        monkeypatch,
        content=(
            "# Evolved Agent System\n\n"
            "## Reflections From Prior Trajectories\n\n"
            "- prompt: Fix the parser precedence bug.\n"
            "- observed: Added a regression test first.\n"
            "- prompt: Fix the package import bug.\n"
            "- failure_signal: pytest failed after an unverified edit\n"
            "- Before finalizing parser or packaging changes, verify the reflected rule "
            "against the focused regression test.\n"
        ),
    )
    job = _job(
        "agent_system_reflector",
        tmp_path,
        input_artifacts=[_reflector_dataset_artifact(tmp_path)],
        config={
            "name": "codex-reflector",
            "target_path": "agents.md",
            "reflector_llm": {
                "model": "reflector-model",
                "base_url": "http://reflector.test/v1",
                "api_key": "test-key",
            },
            "compatibility": {"agent_harness": ["codex"]},
            "scores": {"quality": 0.42},
            "tags": ["swe"],
            "promoted": True,
        },
    )

    artifacts = run_method(job, artifact_root=tmp_path / "artifacts")

    artifact = artifacts[0]
    assert artifact.type == ArtifactType.AGENT_SYSTEM
    assert artifact.name == "codex-reflector"
    assert artifact.manifest["target_path"] == "agents.md"
    assert artifact.manifest["content_path"] == "agents.md"
    assert artifact.manifest["source_dataset_artifact_id"] == "art_dataset_reflector"
    assert artifact.manifest["record_count"] == 2
    assert artifact.manifest["reflected_record_count"] == 2
    assert artifact.manifest["success_count"] == 1
    assert artifact.manifest["failure_count"] == 1
    assert artifact.compatibility == {"agent_harness": ["codex"]}
    assert artifact.scores == {"quality": 0.42}
    assert artifact.tags == ["swe"]
    assert artifact.promoted is True
    assert artifact.lineage["method"] == "agent_system_reflector"
    assert artifact.lineage["input_artifact_ids"] == ["art_dataset_reflector"]

    instruction_path = Path(artifact.uri.removeprefix("file://"))
    assert instruction_path.name == "agents.md"
    text = instruction_path.read_text(encoding="utf-8")
    assert "# Evolved Agent System" in text
    assert "## Reflections From Prior Trajectories" in text
    assert "Fix the parser precedence bug." in text
    assert "Added a regression test first" in text
    assert "Fix the package import bug." in text
    assert "pytest failed after an unverified edit" in text

    store = EvolutionStore(db_path=tmp_path / "evolution.db", artifact_root=tmp_path / "artifacts")
    store.initialize()
    registered = store.register_artifact(artifact)
    context = store.resolve_context(
        ContextResolveRequest(
            task_id="task_reflected",
            instruction="fix import",
            agent={"harness": "codex"},
        )
    )
    assert registered.artifact_id in context.agent_system["artifact_ids"]
    assert context.agent_system["target_path"] == "agents.md"
    assert "pytest failed after an unverified edit" in context.agent_system["rendered_text"]


def test_agent_system_reflector_uses_configured_llm_response(tmp_path, monkeypatch):
    captured = _patch_reflector_llm(
        monkeypatch,
        content=(
            "# LLM Evolved Agent System\n\nAlways run focused verification before broad cleanup."
        ),
    )
    job = _job(
        "agent_system_reflector",
        tmp_path,
        input_artifacts=[_reflector_dataset_artifact(tmp_path)],
        config={
            "reflector_llm": {
                "model": "reflector-model",
                "base_url": "http://reflector.test/v1",
                "api_key": "secret",
                "temperature": 0.1,
                "max_tokens": 1234,
                "timeout_seconds": 7,
            }
        },
    )

    artifact = run_method(job, artifact_root=tmp_path / "artifacts")[0]

    text = Path(artifact.uri.removeprefix("file://")).read_text(encoding="utf-8")
    assert text == (
        "# LLM Evolved Agent System\n\nAlways run focused verification before broad cleanup.\n"
    )
    assert captured["url"] == "http://reflector.test/v1/chat/completions"
    assert captured["headers"]["Authorization"] == "Bearer secret"
    assert captured["json"]["model"] == "reflector-model"
    assert captured["json"]["temperature"] == 0.1
    assert captured["json"]["max_tokens"] == 1234
    assert captured["client_kwargs"]["timeout"] == 7.0
    messages = captured["json"]["messages"]
    assert messages[0]["role"] == "system"
    assert messages[1]["role"] == "user"
    assert "Fix the parser precedence bug." in messages[1]["content"]
    assert "pytest failed after an unverified edit" in messages[1]["content"]


def test_agent_system_reflector_includes_shared_evolution_feedback(tmp_path, monkeypatch):
    captured = _patch_reflector_llm(monkeypatch)
    job = _job(
        "agent_system_reflector",
        tmp_path,
        input_artifacts=[_golden_feedback_dataset_artifact(tmp_path)],
        config={
            "reflector_llm": {
                "model": "reflector-model",
                "base_url": "http://reflector.test/v1",
                "api_key": "secret",
            }
        },
    )

    run_method(job, artifact_root=tmp_path / "artifacts")

    prompt = captured["json"]["messages"][1]["content"]
    assert "Shared Evolution Feedback" in prompt
    assert "Shared Golden Standard Evaluation (Sanitized)" in prompt
    assert "over-inclusion" in prompt
    assert "held-out literals" in prompt
    assert "AAAACCCCGGGGTTTTAAAACCCC" not in prompt


def test_agent_system_reflector_can_use_codex_cli_subscription_provider(tmp_path, monkeypatch):
    captured: dict[str, Any] = {}

    def fake_run(
        args: list[str],
        *,
        check: bool,
        capture_output: bool,
        env: dict[str, str],
        input: str,
        text: bool,
        timeout: float,
    ) -> subprocess.CompletedProcess[str]:
        captured["args"] = args
        captured["check"] = check
        captured["capture_output"] = capture_output
        captured["env"] = env
        captured["input"] = input
        captured["text"] = text
        captured["timeout"] = timeout
        output_path = Path(args[args.index("--output-last-message") + 1])
        output_path.write_text(
            "# Codex CLI Evolved Agent System\n\n"
            "Use transcript evidence before changing the agent system.\n",
            encoding="utf-8",
        )
        return subprocess.CompletedProcess(args, 0, stdout="{}", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    job = _job(
        "agent_system_reflector",
        tmp_path,
        input_artifacts=[_reflector_dataset_artifact(tmp_path)],
        config={
            "reflector_llm": {
                "provider": "codex_cli",
                "model": "gpt-5.4",
                "codex_home": "/tmp/codex-home",
                "timeout_seconds": 9,
            }
        },
    )

    artifact = run_method(job, artifact_root=tmp_path / "artifacts")[0]

    text = Path(artifact.uri.removeprefix("file://")).read_text(encoding="utf-8")
    assert text == (
        "# Codex CLI Evolved Agent System\n\n"
        "Use transcript evidence before changing the agent system.\n"
    )
    assert captured["args"][:2] == ["codex", "exec"]
    assert "--json" in captured["args"]
    assert "--dangerously-bypass-approvals-and-sandbox" not in captured["args"]
    assert "--ignore-user-config" in captured["args"]
    assert "--ephemeral" in captured["args"]
    assert "--ask-for-approval" not in captured["args"]
    assert "--sandbox" in captured["args"]
    assert captured["args"][captured["args"].index("--sandbox") + 1] == "read-only"
    assert "--disable" in captured["args"]
    disabled_features = {
        captured["args"][index + 1]
        for index, arg in enumerate(captured["args"])
        if arg == "--disable"
    }
    assert "shell_tool" in disabled_features
    assert "--output-last-message" in captured["args"]
    assert "--model" in captured["args"]
    assert captured["args"][captured["args"].index("--model") + 1] == "gpt-5.4"
    assert captured["args"][-1] == "-"
    assert captured["input"].startswith("Return only the Markdown agent-system instruction file.")
    assert captured["input"] not in captured["args"]
    assert captured["env"]["CODEX_HOME"] == "/tmp/codex-home"
    assert "OPENAI_API_KEY" not in captured["env"]
    assert "OPENAI_BASE_URL" not in captured["env"]
    assert captured["timeout"] == 9.0
    assert artifact.manifest["reflector_provider"] == "codex_cli"
    assert artifact.manifest["reflector_model"] == "gpt-5.4"


def test_reflector_llm_uses_provider_specific_default_timeout(tmp_path: Path):
    codex_job = _job(
        "agent_system_reflector",
        tmp_path,
        config={
            "reflector_llm": {
                "provider": "codex_cli",
                "model": "gpt-5.4",
            }
        },
    )
    openai_job = _job(
        "agent_system_reflector",
        tmp_path,
        config={
            "reflector_llm": {
                "provider": "openai_chat",
                "model": "gpt-5.4",
            }
        },
    )

    assert methods_module._reflector_llm_config(codex_job)["timeout_seconds"] == 300.0
    assert methods_module._reflector_llm_config(openai_job)["timeout_seconds"] == 30.0


def test_agent_system_reflector_preserves_existing_agent_system_as_base(tmp_path, monkeypatch):
    _patch_reflector_llm(
        monkeypatch,
        content=(
            "# Base Agent System\n\n"
            "Keep changes minimal.\n\n"
            "- Before changing an existing base instruction, verify it still applies to "
            "the current task constraints.\n\n"
            "## Reflections From Prior Trajectories"
        ),
    )
    base_path = tmp_path / "base-agents.md"
    base_path.write_text("# Base Agent System\n\nKeep changes minimal.\n", encoding="utf-8")
    job = _job(
        "agent_system_reflector",
        tmp_path,
        input_artifacts=[
            _reflector_dataset_artifact(tmp_path),
            {
                "artifact_id": "art_previous_agent_system",
                "type": "agent_system",
                "uri": base_path.as_uri(),
                "name": "previous agent system",
            },
        ],
        config={
            "name": "reflected-with-base",
            "reflector_llm": {
                "model": "reflector-model",
                "base_url": "http://reflector.test/v1",
                "api_key": "test-key",
            },
        },
    )

    artifact = run_method(job, artifact_root=tmp_path / "artifacts")[0]

    assert artifact.manifest["target_path"] == "AGENTS.md"
    assert artifact.promoted is False
    assert artifact.lineage["input_artifact_ids"] == [
        "art_dataset_reflector",
        "art_previous_agent_system",
    ]
    text = Path(artifact.uri.removeprefix("file://")).read_text(encoding="utf-8")
    assert text.startswith("# Base Agent System\n\nKeep changes minimal.")
    assert "## Reflections From Prior Trajectories" in text


def test_agent_system_reflector_rejects_missing_dataset(tmp_path):
    job = _job("agent_system_reflector", tmp_path)

    try:
        run_method(job, artifact_root=tmp_path / "artifacts")
    except ValueError as exc:
        assert "agent_system_reflector requires an input dataset artifact" in str(exc)
    else:
        raise AssertionError("expected ValueError")


def test_agent_system_reflector_repairs_forbidden_literal_leakage(
    tmp_path,
    monkeypatch,
):
    captured = _patch_reflector_llm_sequence(
        monkeypatch,
        [
            (
                "# Leaky Agent System\n\n"
                "- For Secret Heldout Paper, inspect golden_source.xlsx before extraction."
            ),
            (
                "# Repaired Agent System\n\n"
                "- For multi-file extraction tasks, before extraction recursively inventory "
                "the allowed input root, group files by package and extension, inspect "
                "structured evidence such as tables or workbooks when present, and before "
                "finalizing verify that every package has inspected evidence or an explicit "
                "allowed-evidence exclusion reason."
            ),
        ],
    )
    job = _job(
        "agent_system_reflector",
        tmp_path,
        input_artifacts=[_reflector_dataset_artifact(tmp_path)],
        config={
            "reflector_llm": {
                "model": "reflector-model",
                "base_url": "http://reflector.test/v1",
                "api_key": "secret",
            },
            "agent_system_audit": {
                "forbidden_literals": {
                    "article_titles": ["Secret Heldout Paper"],
                    "source_files": ["golden_source.xlsx"],
                }
            },
        },
    )

    artifact = run_method(job, artifact_root=tmp_path / "artifacts")[0]

    text = Path(artifact.uri.removeprefix("file://")).read_text(encoding="utf-8")
    assert "Secret Heldout Paper" not in text
    assert "golden_source.xlsx" not in text
    assert "recursively inventory the allowed input root" in text
    assert len(captured["requests"]) == 2
    repair_prompt = captured["requests"][1]["json"]["messages"][1]["content"]
    assert "agent-system audit found issues" in repair_prompt
    assert "forbidden literal" in repair_prompt
    assert artifact.manifest["agent_system_audit"]["repair_count"] == 1
    assert artifact.manifest["agent_system_audit"]["finding_count"] == 0


def test_agent_system_reflector_repairs_wrapped_literals_and_numeric_rows(
    tmp_path,
    monkeypatch,
):
    captured = _patch_reflector_llm_sequence(
        monkeypatch,
        [
            (
                "# Leaky Agent System\n\n"
                "- Before finalizing, inspect row 12 and copy SecretWrappedLiteral."
            ),
            (
                "# Repaired Agent System\n\n"
                "- For structured extraction tasks, before finalizing recursively inventory "
                "the allowed input root, inspect tables or workbooks for eligible records, "
                "and verify every emitted record has allowed evidence without naming "
                "protected rows or literals."
            ),
        ],
    )
    job = _job(
        "agent_system_reflector",
        tmp_path,
        input_artifacts=[_reflector_dataset_artifact(tmp_path)],
        config={
            "reflector_llm": {
                "model": "reflector-model",
                "base_url": "http://reflector.test/v1",
                "api_key": "secret",
            },
            "agent_system_audit": {
                "forbidden_literals": {
                    "terminal_bench": ["SecretWrappedLiteral"],
                    "source_rows": [12],
                }
            },
        },
    )

    artifact = run_method(job, artifact_root=tmp_path / "artifacts")[0]

    text = Path(artifact.uri.removeprefix("file://")).read_text(encoding="utf-8")
    assert "SecretWrappedLiteral" not in text
    assert "row 12" not in text
    assert len(captured["requests"]) == 2
    repair_prompt = captured["requests"][1]["json"]["messages"][1]["content"]
    assert "[REDACTED_LITERAL_" in repair_prompt
    assert "[REDACTED_SOURCE_ROWS_" in repair_prompt
    assert artifact.manifest["agent_system_audit"]["repair_count"] == 1


def test_agent_system_history_reflector_repairs_slogan_coverage_rules(
    tmp_path,
    monkeypatch,
):
    captured = _patch_reflector_llm_sequence(
        monkeypatch,
        [
            (
                "# History-Aware Agent System\n\n"
                "- When sources are package-like or split across files, make a source "
                "checklist before finalizing and review every allowed bundle once; "
                "validate by confirming no eligible source bundle was skipped.\n"
                "- Balance precision and recall."
            ),
            (
                "# History-Aware Agent System\n\n"
                "- For package-style extraction tasks, before extraction recursively "
                "inventory every file under the allowed input root, group files by package "
                "and extension, inspect structured evidence formats such as markdown "
                "tables, spreadsheets, CSV, TSV, XLS, or XLSX when present, and before "
                "finalizing verify each package has inspected source evidence or an "
                "explicit exclusion reason.\n"
                "- Before writing final records, run a precision pass that removes "
                "unsupported candidates and then verify every included item has allowed "
                "evidence and satisfies the requested component boundary."
            ),
        ],
    )
    round1 = _history_round_dataset_artifact(
        tmp_path,
        round_number=1,
        precision=0.26,
        recall=0.68,
        f1=0.38,
        record={
            "event_id": "evt_round1",
            "task_id": "task_biology_round1",
            "session_id": "session_round1",
            "status": "COMPLETED",
            "reward": 0.38,
            "payload": {"summary": "Round 1 found broad source coverage."},
        },
    )
    round2 = _history_round_dataset_artifact(
        tmp_path,
        round_number=2,
        precision=0.33,
        recall=1.0,
        f1=0.49,
        record={
            "event_id": "evt_round2",
            "task_id": "task_biology_round2",
            "session_id": "session_round2",
            "status": "COMPLETED",
            "reward": 0.49,
            "payload": {"summary": "Round 2 improved after checking tables."},
        },
    )
    job = _job(
        "agent_system_history_reflector",
        tmp_path,
        input_artifacts=[round1, round2],
        config={
            "reflector_llm": {
                "model": "reflector-model",
                "base_url": "http://reflector.test/v1",
                "api_key": "secret",
            }
        },
    )

    artifact = run_method(job, artifact_root=tmp_path / "artifacts")[0]

    text = Path(artifact.uri.removeprefix("file://")).read_text(encoding="utf-8")
    assert "Perform a coverage pass across every allowed source bundle" not in text
    assert "recursively inventory every file under the allowed input root" in text
    assert "XLSX" in text
    assert "verify each package has inspected source evidence" in text
    assert len(captured["requests"]) == 2
    repair_prompt = captured["requests"][1]["json"]["messages"][1]["content"]
    assert "coverage rules must include recursive file-level source discovery" in repair_prompt
    assert artifact.manifest["agent_system_audit"]["repair_count"] == 1
    assert artifact.manifest["agent_system_audit"]["finding_count"] == 0


def test_agent_system_audit_rejects_abstract_source_checklist_without_file_inventory():
    text = (
        "# Agent System\n\n"
        "## Coverage and Precision Checks\n"
        "- When sources are package-like or split across files, make a source checklist "
        "before finalizing and review every allowed bundle once; validate by confirming "
        "no eligible source bundle was skipped.\n"
        "- When a source has multiple candidate component classes or sections, perform "
        "a structured pass over each relevant section instead of stopping at the first "
        "match; validate by confirming no allowed component class from that source was "
        "silently omitted.\n"
    )

    findings = _audit_agent_system_markdown(text, forbidden_literals=[])

    assert any(finding["code"] == "source_coverage_not_actionable" for finding in findings)


def test_agent_system_audit_requires_recursive_file_level_source_inventory():
    text = (
        "# Agent System\n\n"
        "- When sources come in multiple files or bundles, enumerate every allowed "
        "source package before extraction, review each package for eligible records, "
        "and verify no allowed package was skipped.\n"
        "- When a source contains multiple tables or supplementary structures, inspect "
        "each relevant structured section that could hold eligible records, extract "
        "from all qualifying sections, and verify coverage against the package inventory.\n"
    )

    findings = _audit_agent_system_markdown(text, forbidden_literals=[])

    assert any(finding["code"] == "source_coverage_not_actionable" for finding in findings)


def test_agent_system_audit_does_not_treat_test_coverage_as_source_coverage():
    text = (
        "# Agent System\n\n"
        "- When changing executable behavior, run regression coverage for touched "
        "paths and verify the relevant tests pass before finalizing.\n"
    )

    findings = _audit_agent_system_markdown(text, forbidden_literals=[])

    assert not any(finding["code"] == "source_coverage_not_actionable" for finding in findings)


def test_agent_system_history_reflector_uses_round_history_and_deltas(
    tmp_path,
    monkeypatch,
):
    captured = _patch_reflector_llm(
        monkeypatch,
        content=(
            "# History-Aware Agent System\n\n"
            "Preserve stable rules from improving rounds and investigate regressions."
        ),
    )
    base_path = tmp_path / "round2-agents.md"
    base_path.write_text(
        "# Round 2 Agent System\n\nPreserve canonical article ids and reduce over-extraction.\n",
        encoding="utf-8",
    )
    round1 = _history_round_dataset_artifact(
        tmp_path,
        round_number=1,
        precision=0.26,
        recall=0.68,
        f1=0.38,
        record={
            "event_id": "evt_round1",
            "task_id": "task_biology_round1",
            "session_id": "session_round1",
            "status": "COMPLETED",
            "reward": 0.38,
            "payload": {
                "session_result": {
                    "metadata": {
                        "evolution_feedback": {
                            "golden_standard": "Round 1 recovered broad coverage."
                        }
                    }
                }
            },
        },
    )
    round2 = _history_round_dataset_artifact(
        tmp_path,
        round_number=2,
        precision=0.33,
        recall=1.0,
        f1=0.49,
        record={
            "event_id": "evt_round2",
            "task_id": "task_biology_round2",
            "session_id": "session_round2",
            "status": "COMPLETED",
            "reward": 0.49,
            "payload": {
                "summary": "Round 2 found almost all gold records after article-id remap."
            },
        },
    )
    round3 = _history_round_dataset_artifact(
        tmp_path,
        round_number=3,
        precision=0.22,
        recall=0.68,
        f1=0.33,
        record={
            "event_id": "evt_round3",
            "task_id": "task_biology_round3",
            "session_id": "session_round3",
            "status": "COMPLETED",
            "reward": 0.33,
            "payload": {
                "session_result": {
                    "metadata": {
                        "evolution_feedback": {
                            "golden_standard": (
                                "Regression: one component-heavy article dropped to zero true "
                                "positives while another over-generated."
                            )
                        }
                    }
                }
            },
        },
    )
    job = _job(
        "agent_system_history_reflector",
        tmp_path,
        input_artifacts=[
            round1,
            round2,
            round3,
            {
                "artifact_id": "agent_system_round_2",
                "type": "agent_system",
                "uri": base_path.as_uri(),
                "name": "round 2 agent system",
            },
        ],
        config={
            "name": "history-aware-reflector",
            "target_path": "AGENTS.md",
            "max_records_per_round": 2,
            "reflector_llm": {
                "model": "reflector-model",
                "base_url": "http://reflector.test/v1",
                "api_key": "secret",
            },
        },
    )

    artifact = run_method(job, artifact_root=tmp_path / "artifacts")[0]

    prompt = captured["json"]["messages"][1]["content"]
    assert "# Round 2 Agent System" in prompt
    assert "## Multi-Round Evolution History" in prompt
    assert "Round 1" in prompt
    assert "f1=0.380" in prompt
    assert "Round 2" in prompt
    assert "delta_f1=+0.110" in prompt
    assert "Round 3" in prompt
    assert "delta_f1=-0.160" in prompt
    assert "regression" in prompt.lower()
    assert "Round 1 recovered broad coverage." in prompt
    assert "one component-heavy article dropped to zero true positives" in prompt
    assert "preserve stable improvements" in prompt.lower()
    assert "canonical article/package identifiers" in prompt
    assert artifact.type == ArtifactType.AGENT_SYSTEM
    assert artifact.name == "history-aware-reflector"
    assert artifact.manifest["method"] == "agent_system_history_reflector"
    assert artifact.manifest["round_count"] == 3
    assert artifact.manifest["source_dataset_artifact_ids"] == [
        "art_history_round_1",
        "art_history_round_2",
        "art_history_round_3",
    ]
    assert artifact.manifest["best_round"] == 2
    assert artifact.manifest["latest_round"] == 3
    assert artifact.manifest["best_f1"] == 0.49
    assert artifact.manifest["latest_f1"] == 0.33
    assert artifact.lineage["method"] == "agent_system_history_reflector"
    assert artifact.lineage["input_artifact_ids"] == [
        "art_history_round_1",
        "art_history_round_2",
        "art_history_round_3",
        "agent_system_round_2",
    ]


def test_agent_system_history_reflector_consumes_human_feedback(
    tmp_path,
    monkeypatch,
):
    captured = _patch_reflector_llm(
        monkeypatch,
        content=(
            "# Human Feedback Aware Agent System\n\n"
            "Add bounded source inventory checks from prior human review."
        ),
    )
    round1 = _history_round_dataset_artifact(
        tmp_path,
        round_number=1,
        precision=0.2,
        recall=0.6,
        f1=0.3,
        record={
            "event_id": "evt_human_review",
            "task_id": "task_human_review",
            "session_id": "session_human_review",
            "status": "COMPLETED",
            "reward": 0.3,
            "evolution_feedback": {
                "feedback_id": "hfb_direct_record_feedback",
                "status": "available_for_evolution",
                "decision": "evaluator-summary",
                "observed_issues": ["Direct evaluator feedback should stay generic."],
            },
            "human": [
                {
                    "feedback_id": "hfb_record_human",
                    "status": "available_for_evolution",
                    "decision": "revise",
                    "score": 1.5,
                    "observed_issues": ["Record-level human alias survives."],
                    "raw_payload": {"secret": "record-human-secret"},
                }
            ],
            "human_feedback": [
                {
                    "feedback_id": "hfb_bounded_search",
                    "status": "available_for_evolution",
                    "decision": "revise",
                    "suggested_changes": ["Record-level merge suggestion."],
                    "labels": ["record-merge"],
                }
            ],
            "payload": {
                "session_result": {
                    "metadata": {
                        "evolution_feedback": {
                            "golden_standard": "Non-human aggregate remains visible.",
                            "review_summary": "Safe shared evaluator note survives.",
                            "raw_payload": {
                                "secret": "shared-raw-secret",
                                "path": "/secret.txt",
                            },
                            "rationale": (
                                "Shared rationale leaks /secret.txt and "
                                "Authorization: Bearer shared-bearer"
                            ),
                            "debug": {
                                "authorization": "Authorization: Bearer shared-bearer",
                                "AWS_ACCESS_KEY_ID": "AKIA_SHARED_ACCESS",
                                "access_key_id": "AKIA_SHARED_COLON",
                                "signed_url": (
                                    "https://example.com/download?X-Amz-Signature=shared-sig#frag"
                                ),
                                "file_path": "/secret.txt",
                                "workspace_path": "/workspace/prod/key.pem",
                                "app_path": "/app/secret.txt",
                                "openevo_path": "/openevo/session/evolution/memory.md",
                                "windows_program_path": r"C:\Program Files\secret.txt",
                                "windows_user_path": r"C:\Users\Alice Smith\secret.txt",
                                "unc_path": r"\\server\share\secret.txt",
                                "safe_route": (
                                    "Keep route /api/v1/feedback, /healthz, and /v1/reviews."
                                ),
                                "safe_detail": "Nested safe shared detail survives.",
                            },
                            "review_feedback": {
                                "status": "submitted",
                                "observed_issues": [
                                    "Submitted shared review feedback must not render."
                                ],
                            },
                            "statusless_review_feedback": {
                                "observed_issues": [
                                    "Statusless shared review feedback must not render."
                                ],
                            },
                            "stateful_summary": {
                                "status": "submitted",
                                "summary": "Premature reviewer summary must not render.",
                            },
                            "available_stateful_summary": {
                                "status": "available_for_evolution",
                                "summary": "Available evaluator summary survives.",
                            },
                            "reviewer_rationale": "Reviewer rationale must not render.",
                            "adjudication_rationale": ("Adjudication rationale must not render."),
                            "human": [
                                {
                                    "feedback_id": "hfb_bounded_search",
                                    "status": "available_for_evolution",
                                    "decision": "revise",
                                    "confidence": 0.9,
                                    "score": 0.67,
                                    "observed_issues": [
                                        "Still encourages unbounded repository search.",
                                        "Do not log Authorization: Bearer sk-prompt-bearer",
                                        "Do not log Bearer standalone-prompt-bearer",
                                        "Do not log AWS_ACCESS_KEY_ID=AKIA_PROMPT_ACCESS",
                                        "Do not log AKIASTANDALONEPROMPT",
                                        "Do not log access_key_id: AKIA_PROMPT_COLON",
                                        {
                                            "text": "Nested typed issue.",
                                            "rationale": "Nested method rationale must not leak.",
                                            "raw_payload": {"secret": "nested-method-secret"},
                                        },
                                    ],
                                    "suggested_changes": [
                                        "Add a bounded source inventory step.",
                                        (
                                            "Avoid signed URL "
                                            "https://example.com/download"
                                            "?X-Amz-Signature=signed-prompt"
                                            "&AWSAccessKeyId=prompt-access"
                                            "#signed-fragment"
                                        ),
                                        "Avoid short URL https://example.com/download?sig=prompt-sig",
                                        (
                                            "Avoid object "
                                            "s3://bucket/key?X-Amz-Signature=s3-prompt"
                                            "#s3-fragment"
                                        ),
                                        "Avoid custom openevo+artifact://host/path?secret=query-secret#frag",
                                        (
                                            "Avoid credentialed URL "
                                            "https://reviewer:prompt_token@example.com/path"
                                            "?secret=prompt_query_secret#frag"
                                        ),
                                        (
                                            "Avoid credentialed URL with at sign "
                                            "https://user:p@ss@example.com/path"
                                        ),
                                        "Avoid postgres://alice:prompt_pg_secret@example.com/db",
                                        ["nested method change must not leak"],
                                    ],
                                    "risks": [
                                        "May overfit to one review packet.",
                                        "Do not log password=prompt_password",
                                        "Do not log api_key: sk-prompt-colon",
                                        "Do not log token: tok-prompt-colon",
                                        "Do not log secret: prompt-secret-colon",
                                        "Do not log OPENAI_API_KEY=sk-prompt-env",
                                        "Do not clone ssh://bob:prompt_ssh_secret@example.com/repo",
                                    ],
                                    "validation_checks": [
                                        "Run timeout-heavy tasks.",
                                        "Do not log AWS_SECRET_ACCESS_KEY=prompt-aws-secret",
                                        "Do not inspect file:///tmp/prompt-secret.txt",
                                        "Do not inspect /secret.txt",
                                        "Do not inspect /tmp/openevo-secret.txt",
                                        "Do not inspect /etc/passwd",
                                        "Do not inspect /mnt/data/secret.txt",
                                        "Do not inspect /scratch/alice/.aws/credentials",
                                        "Do not inspect /Users/alice/key.pem",
                                        r"Do not open C:\Users\alice\secret.txt",
                                        "Do not open C:/Users/Alice/secret.txt",
                                    ],
                                    "labels": ["bounded-search"],
                                    "raw_payload": {"approved": False},
                                    "rationale": "Do not leak raw reviewer prose.",
                                },
                                {
                                    "feedback_id": "hfb_missing_status",
                                    "decision": "revise",
                                    "observed_issues": ["missing-status prompt issue"],
                                },
                                {
                                    "feedback_id": "hfb_non_string_status",
                                    "status": ["available_for_evolution"],
                                    "decision": "revise",
                                    "observed_issues": ["non-string-status prompt issue"],
                                },
                                {
                                    "feedback_id": "hfb_submitted",
                                    "status": "submitted",
                                    "decision": "revise",
                                    "observed_issues": ["submitted prompt issue"],
                                },
                                {
                                    "feedback_id": "hfb_validated",
                                    "status": "validated",
                                    "decision": "revise",
                                    "observed_issues": ["validated prompt issue"],
                                },
                                {
                                    "feedback_id": "hfb_consumed",
                                    "status": "consumed",
                                    "decision": "revise",
                                    "observed_issues": ["consumed prompt issue"],
                                },
                            ],
                        }
                    }
                }
            },
        },
    )
    job = _job(
        "agent_system_history_reflector",
        tmp_path,
        input_artifacts=[round1],
        config={
            "name": "history-human-feedback",
            "reflector_llm": {
                "model": "reflector-model",
                "base_url": "http://reflector.test/v1",
                "api_key": "secret",
            },
            "promotion_support": {
                "trajectory_findings": ["Configured reviewer finding stays visible."]
            },
        },
    )

    artifact = run_method(job, artifact_root=tmp_path / "artifacts")[0]

    prompt = captured["json"]["messages"][1]["content"]
    assert "Human feedback signals" in prompt
    assert "hfb_bounded_search" in prompt
    assert "hfb_record_human" in prompt
    assert "status=available_for_evolution" in prompt
    assert "decision=revise" in prompt
    assert "score=0.670" in prompt
    assert "score=1.500" not in prompt
    assert "Still encourages unbounded repository search." in prompt
    assert prompt.count("Still encourages unbounded repository search.") == 1
    assert "Record-level human alias survives." in prompt
    assert "Record-level merge suggestion." in prompt
    assert prompt.count("Direct evaluator feedback should stay generic.") == 1
    assert "Feedback Id: hfb_direct_record_feedback" in prompt
    assert "Add a bounded source inventory step." in prompt
    assert "prompt_token" not in prompt
    assert "prompt_query_secret" not in prompt
    assert "signed-prompt" not in prompt
    assert "prompt-access" not in prompt
    assert "prompt-sig" not in prompt
    assert "s3-prompt" not in prompt
    assert "s3-fragment" not in prompt
    assert "query-secret" not in prompt
    assert "signed-fragment" not in prompt
    assert "X-Amz-Signature" not in prompt
    assert "AWSAccessKeyId" not in prompt
    assert "sig=prompt-sig" not in prompt
    assert "p@ss" not in prompt
    assert "https://user:p@ss@example.com/path" not in prompt
    assert "https://ss@example.com/path" not in prompt
    assert "alice" not in prompt
    assert "prompt_pg_secret" not in prompt
    assert "postgres://alice:prompt_pg_secret@example.com/db" not in prompt
    assert "bob" not in prompt
    assert "prompt_ssh_secret" not in prompt
    assert "ssh://bob:prompt_ssh_secret@example.com/repo" not in prompt
    assert "prompt_password" not in prompt
    assert "sk-prompt-colon" not in prompt
    assert "tok-prompt-colon" not in prompt
    assert "prompt-secret-colon" not in prompt
    assert "sk-prompt-env" not in prompt
    assert "sk-prompt-bearer" not in prompt
    assert "standalone-prompt-bearer" not in prompt
    assert "AKIA_PROMPT_ACCESS" not in prompt
    assert "AKIASTANDALONEPROMPT" not in prompt
    assert "AKIA_PROMPT_COLON" not in prompt
    assert "prompt-aws-secret" not in prompt
    assert "file://" not in prompt
    assert "/tmp/prompt-secret.txt" not in prompt
    assert "/tmp/openevo-secret.txt" not in prompt
    assert "/etc/passwd" not in prompt
    assert "/mnt/data/secret.txt" not in prompt
    assert "/scratch/alice/.aws/credentials" not in prompt
    assert "/Users/alice/key.pem" not in prompt
    assert "/secret.txt" not in prompt
    assert "/workspace/prod/key.pem" not in prompt
    assert "/app/secret.txt" not in prompt
    assert "/openevo/session/evolution/memory.md" not in prompt
    assert r"C:\Users\alice\secret.txt" not in prompt
    assert "Program Files" not in prompt
    assert "Alice Smith" not in prompt
    assert "server" not in prompt
    assert "C:/Users/Alice/secret.txt" not in prompt
    assert "/api/v1/feedback" not in prompt
    assert "/healthz" not in prompt
    assert "/v1/reviews" not in prompt
    assert "Non-human aggregate remains visible." in prompt
    assert "Safe shared evaluator note survives." in prompt
    assert "Nested safe shared detail survives." in prompt
    assert "raw_payload" not in prompt
    assert "shared-raw-secret" not in prompt
    assert "Shared rationale leaks" not in prompt
    assert "shared-bearer" not in prompt
    assert "AKIA_SHARED_ACCESS" not in prompt
    assert "AKIA_SHARED_COLON" not in prompt
    assert "shared-sig" not in prompt
    assert "Submitted shared review feedback must not render." not in prompt
    assert "Statusless shared review feedback must not render." not in prompt
    assert "Premature reviewer summary must not render." not in prompt
    assert "Status: submitted" not in prompt
    assert "Available evaluator summary survives." in prompt
    assert "Reviewer rationale must not render." not in prompt
    assert "Adjudication rationale must not render." not in prompt
    assert "record-human-secret" not in prompt
    assert "Do not leak raw reviewer prose." not in prompt
    assert "Nested method rationale must not leak." not in prompt
    assert "nested-method-secret" not in prompt
    assert "nested method change must not leak" not in prompt
    assert "missing-status prompt issue" not in prompt
    assert "non-string-status prompt issue" not in prompt
    assert "status=submitted" not in prompt
    assert "submitted prompt issue" not in prompt
    assert "status=validated" not in prompt
    assert "validated prompt issue" not in prompt
    assert "status=consumed" not in prompt
    assert "consumed prompt issue" not in prompt
    assert artifact.manifest["human_feedback_ids"] == [
        "hfb_record_human",
        "hfb_bounded_search",
    ]
    assert artifact.manifest["human_feedback_count"] == 2
    assert artifact.manifest["shared_evolution_feedback_ids"] == ["hfb_direct_record_feedback"]
    assert artifact.manifest["shared_evolution_feedback_count"] == 1
    assert (
        "Configured reviewer finding stays visible."
        in artifact.manifest["promotion_support"]["trajectory_findings"]
    )
    assert (
        "Included 2 human feedback item(s) from prior reviews."
        in artifact.manifest["promotion_support"]["trajectory_findings"]
    )
    assert (
        "Included 1 shared evolution feedback item(s) from prior evaluator signals."
        in artifact.manifest["promotion_support"]["trajectory_findings"]
    )


def test_agent_system_history_reflector_consumes_store_sanitized_human_feedback(
    tmp_path,
    monkeypatch,
):
    captured = _patch_reflector_llm(
        monkeypatch,
        content=(
            "# Store Sanitized Feedback Agent System\n\n"
            "- When repository scanning risks timeout, before extraction build a bounded "
            "source inventory and validate that the final answer cites only inventoried "
            "inputs."
        ),
    )
    store = EvolutionStore(db_path=tmp_path / "evolution.db", artifact_root=tmp_path / "store")
    store.initialize()
    store.ingest_event(
        EventIngestRequest(
            source="openevo",
            event_type="openevo.session_completed",
            source_event_id="session:store-human-feedback",
            task_id="task_store_human_feedback",
            session_id="session_store_human_feedback",
            status="COMPLETED",
            reward=0.6,
            payload={
                "session_result": {
                    "trajectory": {"traces": [{"reward": 0.6}]},
                    "metadata": {
                        "evolution_feedback": {
                            "human": [
                                {
                                    "feedback_id": "hfb_store_available",
                                    "status": "available_for_evolution",
                                    "normalized_payload": {
                                        "decision": "revise",
                                        "confidence": 0.8,
                                        "observed_issues": [
                                            "Reviewer saw unbounded source scanning."
                                        ],
                                        "suggested_changes": [
                                            "Add an inventory cap before extraction."
                                        ],
                                    },
                                    "raw_payload": {"approved": False},
                                }
                            ]
                        }
                    },
                }
            },
        )
    )
    dataset = store.create_dataset(
        DatasetCreateRequest(
            name="store-human-feedback",
            purpose="agent_system_evolution",
            query={
                "event_types": ["openevo.session_completed"],
                "status": ["COMPLETED"],
            },
        )
    )
    with store.connect() as conn:
        dataset_row = conn.execute(
            "SELECT * FROM datasets WHERE dataset_id = ?",
            (dataset.dataset_id,),
        ).fetchone()
        artifact_row = conn.execute(
            "SELECT * FROM artifacts WHERE artifact_id = ?",
            (dataset.artifact_id,),
        ).fetchone()
    dataset_artifact = {
        "artifact_id": dataset.artifact_id,
        "type": "dataset",
        "uri": artifact_row["uri"],
        "name": dataset_row["name"],
    }
    job = _job(
        "agent_system_history_reflector",
        tmp_path,
        input_artifacts=[dataset_artifact],
        config={
            "name": "store-sanitized-history-feedback",
            "reflector_llm": {
                "model": "reflector-model",
                "base_url": "http://reflector.test/v1",
                "api_key": "secret",
            },
        },
    )

    artifact = run_method(job, artifact_root=tmp_path / "artifacts")[0]

    prompt = captured["json"]["messages"][1]["content"]
    assert "Human feedback signals" in prompt
    assert "hfb_store_available" in prompt
    assert "Reviewer saw unbounded source scanning." in prompt
    assert "Add an inventory cap before extraction." in prompt
    assert "raw_payload" not in prompt
    assert artifact.manifest["human_feedback_ids"] == ["hfb_store_available"]
    assert artifact.manifest["human_feedback_count"] == 1


def test_agent_system_history_reflector_uses_latest_prior_agent_system_base(
    tmp_path,
    monkeypatch,
):
    captured = _patch_reflector_llm(monkeypatch)
    old_path = tmp_path / "round1-agents.md"
    old_path.write_text(
        "# Old Agent System\n\nDo not preserve stale round-one instructions.\n",
        encoding="utf-8",
    )
    latest_path = tmp_path / "round2-agents.md"
    latest_path.write_text(
        "# Latest Agent System\n\nPreserve round-two provenance validation.\n",
        encoding="utf-8",
    )
    round1 = _history_round_dataset_artifact(
        tmp_path,
        round_number=1,
        precision=0.25,
        recall=0.60,
        f1=0.35,
        record={
            "event_id": "evt_round1",
            "task_id": "task_round1",
            "session_id": "session_round1",
            "status": "COMPLETED",
            "reward": 0.35,
        },
    )
    round2 = _history_round_dataset_artifact(
        tmp_path,
        round_number=2,
        precision=0.40,
        recall=0.80,
        f1=0.53,
        record={
            "event_id": "evt_round2",
            "task_id": "task_round2",
            "session_id": "session_round2",
            "status": "COMPLETED",
            "reward": 0.53,
        },
    )
    job = _job(
        "agent_system_history_reflector",
        tmp_path,
        input_artifacts=[
            round1,
            round2,
            {
                "artifact_id": "agent_system_round_1",
                "type": "agent_system",
                "uri": old_path.as_uri(),
                "name": "round 1 agent system",
            },
            {
                "artifact_id": "agent_system_round_2",
                "type": "agent_system",
                "uri": latest_path.as_uri(),
                "name": "round 2 agent system",
            },
        ],
        config={
            "reflector_llm": {
                "model": "reflector-model",
                "base_url": "http://reflector.test/v1",
                "api_key": "secret",
            },
        },
    )

    run_method(job, artifact_root=tmp_path / "artifacts")

    prompt = captured["json"]["messages"][1]["content"]
    assert "# Latest Agent System" in prompt
    assert "round-two provenance validation" in prompt
    assert "# Old Agent System" not in prompt
    assert "stale round-one instructions" not in prompt


def test_agent_system_history_reflector_rejects_missing_dataset(tmp_path):
    job = _job("agent_system_history_reflector", tmp_path)

    try:
        run_method(job, artifact_root=tmp_path / "artifacts")
    except ValueError as exc:
        assert "agent_system_history_reflector requires at least one dataset artifact" in str(exc)
    else:
        raise AssertionError("expected ValueError")


def test_agent_system_history_reflector_infers_round_and_metrics_from_records(
    tmp_path,
    monkeypatch,
):
    captured = _patch_reflector_llm(monkeypatch)
    dataset_dir = tmp_path / "history-record-metadata"
    dataset_dir.mkdir()
    records_path = dataset_dir / "records.jsonl"
    records_path.write_text(
        json.dumps(
            {
                "event_id": "evt_round_metadata",
                "task_id": "task_biology_round_metadata",
                "session_id": "session_round_metadata",
                "status": "COMPLETED",
                "reward": 1.0,
                "payload": {
                    "session_result": {
                        "metadata": {
                            "round": 2,
                            "evolution_feedback": {
                                "golden_standard": (
                                    "## Shared Golden Standard Evaluation (Sanitized)\n\n"
                                    "- Aggregate fit: precision=0.123, recall=0.456, "
                                    "f1=0.234, prediction/reference ratio=1.23x, "
                                    "duplicate rate=0.010."
                                )
                            },
                        }
                    }
                },
            }
        )
        + "\n",
        encoding="utf-8",
    )
    manifest_path = dataset_dir / "manifest.json"
    manifest_path.write_text(
        json.dumps(
            {
                "dataset_id": "ds_history_record_metadata",
                "name": "history record metadata",
                "records_path": "records.jsonl",
                "records_uri": records_path.as_uri(),
                "event_count": 1,
            }
        ),
        encoding="utf-8",
    )
    job = _job(
        "agent_system_history_reflector",
        tmp_path,
        input_artifacts=[
            {
                "artifact_id": "art_history_record_metadata",
                "type": "dataset",
                "uri": manifest_path.as_uri(),
                "name": "history record metadata",
            }
        ],
        config={
            "reflector_llm": {
                "model": "reflector-model",
                "base_url": "http://reflector.test/v1",
                "api_key": "secret",
            }
        },
    )

    artifact = run_method(job, artifact_root=tmp_path / "artifacts")[0]

    prompt = captured["json"]["messages"][1]["content"]
    assert "### Round 2" in prompt
    assert "precision=0.123" in prompt
    assert "recall=0.456" in prompt
    assert "f1=0.234" in prompt
    assert artifact.manifest["latest_round"] == 2
    assert artifact.manifest["latest_f1"] == 0.234


def test_agent_system_pareto_reflector_selects_candidate_with_external_gate(
    tmp_path,
    monkeypatch,
):
    captured = _patch_reflector_llm_sequence(
        monkeypatch,
        [
            (
                "# Precision Candidate\n\n"
                "- Before finalizing an extraction task, run a precision check that "
                "rejects unsupported rows and verify each kept record has source evidence."
            ),
            (
                "# Recall Candidate\n\n"
                "- Before finalizing a package-like extraction task, recursively inventory "
                "every file under the allowed input root, inspect tables, spreadsheets, "
                "CSV, TSV, XLS, and XLSX sources, and verify every package has a coverage "
                "decision."
            ),
            (
                "# Provenance Candidate\n\n"
                "- When task inputs are grouped by package or article, before extraction "
                "build a canonical source-id map from each top-level input directory to "
                "itself, use only those ids in final records, and before finalizing verify "
                "every record's source id is in that map.\n"
                "- Before extracting from package-like sources, recursively inventory "
                "every file under the allowed input root, inspect structured evidence "
                "formats such as tables, spreadsheets, CSV, TSV, XLS, and XLSX, and "
                "verify every package has inspected evidence or an exclusion reason.\n"
                "- Before accepting table-derived records, compare precision and recall "
                "risks, remove unsupported candidates, and verify output volume is within "
                "the task's expected source coverage rather than a raw DNA-cell dump."
            ),
        ],
    )
    round1 = _history_round_dataset_artifact(
        tmp_path,
        round_number=1,
        precision=0.26,
        recall=0.68,
        f1=0.38,
        record={
            "event_id": "evt_round1",
            "task_id": "task_biology_round1",
            "session_id": "session_round1",
            "status": "COMPLETED",
            "reward": 0.38,
            "payload": {"summary": "Round 1 had incomplete article coverage."},
        },
    )
    round2 = _history_round_dataset_artifact(
        tmp_path,
        round_number=2,
        precision=0.33,
        recall=0.998,
        f1=0.494,
        record={
            "event_id": "evt_round2",
            "task_id": "task_biology_round2",
            "session_id": "session_round2",
            "status": "COMPLETED",
            "reward": 0.494,
            "payload": {
                "summary": "Round 2 improved after preserving article ids.",
                "session_result": {
                    "metadata": {
                        "evolution_feedback": {
                            "human": [
                                {
                                    "feedback_id": "hfb_pareto_bounded",
                                    "status": "available_for_evolution",
                                    "decision": "revise",
                                    "observed_issues": ["Pareto reviewer saw article-id drift."],
                                    "suggested_changes": [
                                        "Preserve canonical source ids in each candidate."
                                    ],
                                }
                            ]
                        }
                    }
                },
            },
        },
    )
    round3 = _history_round_dataset_artifact(
        tmp_path,
        round_number=3,
        precision=0.22,
        recall=0.679,
        f1=0.335,
        record={
            "event_id": "evt_round3",
            "task_id": "task_biology_round3",
            "session_id": "session_round3",
            "status": "COMPLETED",
            "reward": 0.335,
            "payload": {"summary": "Round 3 regressed with coverage collapse."},
        },
    )
    job = _job(
        "agent_system_pareto_reflector",
        tmp_path,
        input_artifacts=[round1, round2, round3],
        config={
            "name": "pareto-reflector",
            "candidate_strategies": [
                "precision_guarded",
                "recall_recovery",
                "provenance_guarded",
            ],
            "candidate_evaluations": {
                "precision_guarded": {
                    "precision": 0.36,
                    "recall": 0.82,
                    "f1": 0.50,
                    "prediction_to_reference_ratio": 2.0,
                },
                "recall_recovery": {
                    "precision": 0.09,
                    "recall": 1.0,
                    "f1": 0.60,
                    "prediction_to_reference_ratio": 20.0,
                },
                "provenance_guarded": {
                    "precision": 0.40,
                    "recall": 0.88,
                    "f1": 0.55,
                    "prediction_to_reference_ratio": 2.5,
                },
            },
            "promotion_gate": {
                "max_prediction_to_reference_ratio": 5.0,
                "max_f1_regression": 0.0,
            },
            "reflector_llm": {
                "model": "reflector-model",
                "base_url": "http://reflector.test/v1",
                "api_key": "secret",
            },
            "promotion_support": {
                "trajectory_findings": ["Configured Pareto finding stays visible."]
            },
            "promoted": True,
        },
    )

    artifacts = run_method(job, artifact_root=tmp_path / "artifacts")

    agent_system_artifact = next(
        artifact for artifact in artifacts if artifact.type == ArtifactType.AGENT_SYSTEM
    )
    report_artifact = next(
        artifact for artifact in artifacts if artifact.type == ArtifactType.REPORT
    )
    text = Path(agent_system_artifact.uri.removeprefix("file://")).read_text(encoding="utf-8")
    assert text.startswith("# Provenance Candidate")
    assert agent_system_artifact.promoted is True
    assert agent_system_artifact.manifest["method"] == "agent_system_pareto_reflector"
    assert agent_system_artifact.manifest["candidate_count"] == 3
    assert agent_system_artifact.manifest["selected_candidate"]["strategy"] == "provenance_guarded"
    assert agent_system_artifact.manifest["promotion_gate"]["passed"] is True
    assert agent_system_artifact.manifest["best_round"] == 2
    assert agent_system_artifact.manifest["latest_round"] == 3
    assert agent_system_artifact.manifest["human_feedback_ids"] == ["hfb_pareto_bounded"]
    assert agent_system_artifact.manifest["human_feedback_count"] == 1
    assert (
        "Configured Pareto finding stays visible."
        in agent_system_artifact.manifest["promotion_support"]["trajectory_findings"]
    )
    assert (
        "Included 1 human feedback item(s) from prior reviews."
        in agent_system_artifact.manifest["promotion_support"]["trajectory_findings"]
    )
    assert agent_system_artifact.lineage["method"] == "agent_system_pareto_reflector"
    assert agent_system_artifact.lineage["input_artifact_ids"] == [
        "art_history_round_1",
        "art_history_round_2",
        "art_history_round_3",
    ]

    prompts = [request["json"]["messages"][1]["content"] for request in captured["requests"]]
    assert len(prompts) == 3
    assert "all historical trajectories" in prompts[0]
    assert "Candidate strategy: precision_guarded" in prompts[0]
    assert "Candidate strategy: recall_recovery" in prompts[1]
    assert "Candidate strategy: provenance_guarded" in prompts[2]
    assert "Promotion gate" in prompts[2]
    assert "coverage collapse" in prompts[2]
    assert all("Human feedback signals" in prompt for prompt in prompts)
    assert all("hfb_pareto_bounded" in prompt for prompt in prompts)
    assert all("Pareto reviewer saw article-id drift." in prompt for prompt in prompts)

    report = json.loads(Path(report_artifact.uri.removeprefix("file://")).read_text())
    assert report["selected_candidate"]["strategy"] == "provenance_guarded"
    assert report_artifact.manifest["human_feedback_ids"] == ["hfb_pareto_bounded"]
    assert report_artifact.manifest["human_feedback_count"] == 1
    assert report["human_feedback_ids"] == ["hfb_pareto_bounded"]
    assert report["human_feedback_count"] == 1
    rejected = {candidate["strategy"]: candidate for candidate in report["candidates"]}
    assert "prediction_to_reference_ratio" in rejected["recall_recovery"]["gate_failures"]


def test_agent_system_pareto_reflector_requires_history_dataset(tmp_path):
    job = _job("agent_system_pareto_reflector", tmp_path)

    try:
        run_method(job, artifact_root=tmp_path / "artifacts")
    except ValueError as exc:
        assert "agent_system_pareto_reflector requires at least one dataset artifact" in str(exc)
    else:
        raise AssertionError("expected ValueError")


@pytest.mark.parametrize(
    ("candidate_count", "mutation_strategies", "expected_strategies"),
    [
        (None, None, ["failure_targeted", "verification_gate", "preservation_gate"]),
        (0, None, ["failure_targeted"]),
        (
            6,
            None,
            [
                "failure_targeted",
                "verification_gate",
                "preservation_gate",
                "anti_regression",
                "edge_case_corpus",
            ],
        ),
        (
            1,
            ["bounded_inventory", "verifier_recovery"],
            ["bounded_inventory", "verifier_recovery"],
        ),
    ],
)
def test_agent_system_gepa_reflector_resolves_mutation_strategies(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    candidate_count: int | None,
    mutation_strategies: list[str] | None,
    expected_strategies: list[str],
) -> None:
    _patch_reflector_llm(
        monkeypatch,
        "# Candidate\n\n"
        "- When changing behavior, run a focused test and validate the result before finalizing.",
    )
    dataset = _history_round_dataset_artifact(
        tmp_path,
        round_number=1,
        precision=0.5,
        recall=0.5,
        f1=0.5,
        record={
            "event_id": "evt_gepa_defaults",
            "task_id": "task_gepa_defaults",
            "session_id": "session_gepa_defaults",
            "status": "COMPLETED",
            "reward": 0.5,
            "traces": [
                {
                    "prompt_messages": [{"role": "user", "content": "Fix the failure."}],
                    "response_messages": [
                        {"role": "assistant", "content": "Ran a focused check."}
                    ],
                }
            ],
        },
    )
    config: dict[str, Any] = {
        "reflector_llm": {
            "model": "reflector-model",
            "base_url": "http://reflector.test/v1",
            "api_key": "test-key",
        }
    }
    if candidate_count is not None:
        config["candidate_count"] = candidate_count
    if mutation_strategies is not None:
        config["mutation_strategies"] = mutation_strategies

    artifacts = run_method(
        _job(
            "agent_system_gepa_reflector",
            tmp_path,
            input_artifacts=[dataset],
            config=config,
        ),
        artifact_root=tmp_path / "artifacts",
    )

    candidates = [artifact for artifact in artifacts if artifact.type == ArtifactType.AGENT_SYSTEM]
    assert [artifact.manifest["candidate_strategy"] for artifact in candidates] == (
        expected_strategies
    )
    assert all(
        artifact.manifest["candidate_count"] == len(expected_strategies) for artifact in candidates
    )


def test_agent_system_gepa_reflector_generates_mutation_pool_from_verifier_feedback(
    tmp_path,
    monkeypatch,
):
    captured = _patch_reflector_llm_sequence(
        monkeypatch,
        [
            (
                "# Preservation Candidate\n\n"
                "- When modifying HTML sanitizers, before finalizing run a clean-HTML "
                "preservation corpus covering forms, semantic tags, media tags, empty "
                "elements, and entity-heavy text; validate that the sanitized output "
                "does not reorder benign attributes or rewrite entities for clean files."
            ),
            (
                "# XSS Corpus Candidate\n\n"
                "- When removing executable HTML, before finalizing run a malformed-XSS "
                "corpus covering namespaced scripts, encoded javascript URLs, data URLs, "
                "meta refresh, CSS URL payloads, and malformed event-handler tags; "
                "validate that no executable surface remains."
            ),
        ],
    )
    failure_round = _history_round_dataset_artifact(
        tmp_path,
        round_number=1,
        precision=0.0,
        recall=0.0,
        f1=0.0,
        record={
            "event_id": "evt_filter_failure",
            "task_id": "filter-js-from-html",
            "session_id": "filter-js-from-html__failure",
            "status": "COMPLETED",
            "reward": 0.0,
            "traces": [
                {
                    "prompt_messages": [
                        {"role": "user", "content": "Create an HTML JavaScript filter."}
                    ],
                    "response_messages": [
                        {
                            "role": "assistant",
                            "content": "Used narrow text transformations and smoke tests.",
                        }
                    ],
                    "metadata": {
                        "verifier": {
                            "summary": {"tests": 2, "passed": 0, "failed": 2},
                            "failed_tests": [
                                {
                                    "name": "test_clean_html_unchanged",
                                    "message": (
                                        "clean HTML files were reformatted, benign "
                                        "attributes were reordered, and entities changed"
                                    ),
                                },
                                {
                                    "name": "test_filter_blocks_xss",
                                    "message": (
                                        "malformed XSS vectors such as iframe onload and "
                                        "encoded javascript URLs remained executable"
                                    ),
                                },
                            ],
                        }
                    },
                }
            ],
            "payload": {
                "session_result": {
                    "metadata": {
                        "terminal_bench": {
                            "trial_name": "filter-js-from-html__failure",
                        },
                    }
                }
            },
        },
    )
    job = _job(
        "agent_system_gepa_reflector",
        tmp_path,
        input_artifacts=[failure_round],
        config={
            "name": "gepa-reflector",
            "candidate_count": 2,
            "mutation_strategies": ["preservation_gate", "xss_corpus"],
            "reflector_llm": {
                "model": "reflector-model",
                "base_url": "http://reflector.test/v1",
                "api_key": "secret",
            },
            "promoted": True,
        },
    )

    artifacts = run_method(job, artifact_root=tmp_path / "artifacts")

    agent_system_artifacts = [
        artifact for artifact in artifacts if artifact.type == ArtifactType.AGENT_SYSTEM
    ]
    report_artifact = next(
        artifact for artifact in artifacts if artifact.type == ArtifactType.REPORT
    )
    assert len(agent_system_artifacts) == 2
    assert [artifact.manifest["candidate_strategy"] for artifact in agent_system_artifacts] == [
        "preservation_gate",
        "xss_corpus",
    ]
    assert agent_system_artifacts[0].manifest["method"] == "agent_system_gepa_reflector"
    assert agent_system_artifacts[0].manifest["candidate_count"] == 2
    assert agent_system_artifacts[0].lineage["method"] == "agent_system_gepa_reflector"
    assert all(artifact.promoted is False for artifact in agent_system_artifacts)
    assert report_artifact.promoted is False
    for artifact in agent_system_artifacts:
        support = artifact.manifest["promotion_support"]
        assert support["trajectory_findings"]
        assert support["proposed_changes"]
        assert support["expected_benefits"]
        assert support["risks"]
        assert support["validation_checks"]

    candidate_texts = [
        Path(artifact.uri.removeprefix("file://")).read_text(encoding="utf-8")
        for artifact in agent_system_artifacts
    ]
    assert candidate_texts[0].startswith("# Preservation Candidate")
    assert candidate_texts[1].startswith("# XSS Corpus Candidate")

    prompts = [request["json"]["messages"][1]["content"] for request in captured["requests"]]
    assert len(prompts) == 2
    assert "GEPA Candidate Mutation" in prompts[0]
    assert "Mutation strategy: preservation_gate" in prompts[0]
    assert "clean HTML files were reformatted" in prompts[0]
    assert "Mutation strategy: xss_corpus" in prompts[1]
    assert "encoded javascript URLs" in prompts[1]

    report = json.loads(Path(report_artifact.uri.removeprefix("file://")).read_text())
    assert report["method"] == "agent_system_gepa_reflector"
    assert report["candidate_count"] == 2
    assert "selected_candidate" not in report
    assert [candidate["strategy"] for candidate in report["candidates"]] == [
        "preservation_gate",
        "xss_corpus",
    ]


def test_agent_system_gepa_history_sort_and_best_round_tie_are_stable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_reflector_llm_sequence(
        monkeypatch,
        ["# Stable Candidate\n\n- Preserve verified behavior across rounds."],
    )
    specifications = (
        ("round-3", 3, 0.2),
        ("round-1", 1, 0.8),
        ("round-2-a", 2, 0.8),
        ("round-2-b", 2, 0.8),
    )
    artifacts: list[dict[str, Any]] = []
    for label, round_number, f1 in specifications:
        root = tmp_path / label
        root.mkdir()
        artifact = _history_round_dataset_artifact(
            root,
            round_number=round_number,
            precision=f1,
            recall=f1,
            f1=f1,
            record={
                "event_id": f"evt_{label}",
                "task_id": "task_gepa_history",
                "session_id": f"session_{label}",
                "status": "COMPLETED",
                "reward": f1,
                "traces": [
                    {
                        "prompt_messages": [{"role": "user", "content": label}],
                        "response_messages": [
                            {"role": "assistant", "content": f"observed {label}"}
                        ],
                    }
                ],
            },
        )
        artifact["artifact_id"] = f"artifact_{label}"
        artifacts.append(artifact)

    round_three_manifest = Path(artifacts[0]["uri"].removeprefix("file://"))
    payload = json.loads(round_three_manifest.read_text(encoding="utf-8"))
    payload["metrics"]["f1"] = None
    round_three_manifest.write_text(json.dumps(payload), encoding="utf-8")

    rounds = methods_module._history_reflection_rounds(
        [WorkerClaimInputArtifact(**item) for item in artifacts],
        max_records_per_round=8,
    )
    assert [item["artifact"].artifact_id for item in rounds] == [
        "artifact_round-1",
        "artifact_round-2-a",
        "artifact_round-2-b",
        "artifact_round-3",
    ]
    assert methods_module._best_history_round(rounds)["artifact"].artifact_id == (
        "artifact_round-1"
    )

    produced = run_method(
        _job(
            "agent_system_gepa_reflector",
            tmp_path,
            input_artifacts=artifacts,
            config={
                "mutation_strategies": ["stable_history"],
                "reflector_llm": {
                    "model": "reflector-model",
                    "base_url": "http://reflector.test/v1",
                    "api_key": "test-key",
                },
            },
        ),
        artifact_root=tmp_path / "artifacts",
    )
    candidate = next(
        artifact for artifact in produced if artifact.type == ArtifactType.AGENT_SYSTEM
    )

    assert candidate.manifest["source_dataset_artifact_ids"] == [
        "artifact_round-1",
        "artifact_round-2-a",
        "artifact_round-2-b",
        "artifact_round-3",
    ]
    assert candidate.manifest["best_round"] == 1
    assert candidate.manifest["latest_round"] == 3


def test_agent_system_gepa_reflector_consumes_human_feedback(
    tmp_path,
    monkeypatch,
):
    captured = _patch_reflector_llm_sequence(
        monkeypatch,
        ["# Bounded Search Candidate\n\n- Add bounded inventory checks."],
    )
    failure_round = _history_round_dataset_artifact(
        tmp_path,
        round_number=1,
        precision=0.0,
        recall=0.0,
        f1=0.0,
        record={
            "event_id": "evt_gepa_human_feedback",
            "task_id": "task_gepa_human_feedback",
            "session_id": "session_gepa_human_feedback",
            "status": "COMPLETED",
            "reward": 0.0,
            "payload": {
                "session_result": {
                    "metadata": {
                        "evolution_feedback": {
                            "human": [
                                {
                                    "feedback_id": "hfb_gepa_bounded",
                                    "status": "available_for_evolution",
                                    "decision": "revise",
                                    "confidence": 0.75,
                                    "observed_issues": [
                                        "Candidate still scans the full repository."
                                    ],
                                    "suggested_changes": [
                                        "Prefer a bounded source inventory mutation."
                                    ],
                                    "risks": ["Could miss hidden inputs."],
                                    "validation_checks": ["Run budget-stress tasks."],
                                    "labels": ["gepa", "bounded-search"],
                                    "raw_payload": {"approved": False},
                                }
                            ]
                        }
                    }
                }
            },
        },
    )
    job = _job(
        "agent_system_gepa_reflector",
        tmp_path,
        input_artifacts=[failure_round],
        config={
            "name": "gepa-human-feedback",
            "candidate_count": 1,
            "mutation_strategies": ["bounded_inventory"],
            "reflector_llm": {
                "model": "reflector-model",
                "base_url": "http://reflector.test/v1",
                "api_key": "secret",
            },
        },
    )

    artifacts = run_method(job, artifact_root=tmp_path / "artifacts")

    agent_system_artifact = next(
        artifact for artifact in artifacts if artifact.type == ArtifactType.AGENT_SYSTEM
    )
    prompt = captured["requests"][0]["json"]["messages"][1]["content"]
    assert "Human feedback signals" in prompt
    assert "hfb_gepa_bounded" in prompt
    assert "Candidate still scans the full repository." in prompt
    assert "Prefer a bounded source inventory mutation." in prompt
    assert "raw_payload" not in prompt
    assert agent_system_artifact.manifest["human_feedback_ids"] == ["hfb_gepa_bounded"]
    assert agent_system_artifact.manifest["human_feedback_count"] == 1
    assert (
        "Included 1 human feedback item(s) from prior reviews."
        in agent_system_artifact.manifest["promotion_support"]["trajectory_findings"]
    )



def test_parametric_memory_register_returns_adapter_artifact(tmp_path):
    adapter_dir = tmp_path / "adapter"
    adapter_dir.mkdir()
    job = _job(
        "parametric_memory_register",
        tmp_path,
        config={
            "adapter_uri": adapter_dir.as_uri(),
            "base_model": "Qwen/Qwen3.6-27B",
        },
    )

    artifacts = run_method(job, artifact_root=tmp_path / "artifacts")

    artifact = artifacts[0]
    assert artifact.type == ArtifactType.PARAMETRIC_MEMORY
    assert artifact.uri == adapter_dir.as_uri()
    assert artifact.manifest["base_model"] == "Qwen/Qwen3.6-27B"
    assert artifact.manifest["adapter_format"] == "lora"
    assert artifact.manifest["adapter_id"] == "adapter"
    assert artifact.compatibility == {"base_model": ["Qwen/Qwen3.6-27B"]}


def test_parametric_memory_register_preserves_configured_adapter_id(tmp_path):
    adapter_dir = tmp_path / "adapter"
    adapter_dir.mkdir()
    job = _job(
        "parametric_memory_register",
        tmp_path,
        config={
            "adapter_uri": adapter_dir.as_uri(),
            "base_model": "Qwen/Qwen3.6-27B",
            "adapter_id": "parser-memory",
        },
    )

    artifacts = run_method(job, artifact_root=tmp_path / "artifacts")

    assert artifacts[0].name == "parser-memory"
    assert artifacts[0].manifest["adapter_id"] == "parser-memory"


def test_parametric_memory_register_preserves_routing_metadata(tmp_path):
    adapter_dir = tmp_path / "adapter"
    adapter_dir.mkdir()
    job = _job(
        "parametric_memory_register",
        tmp_path,
        config={
            "adapter_uri": adapter_dir.as_uri(),
            "base_model": "Qwen/Qwen3.6-27B",
            "compatibility": {
                "base_model": ["Qwen/Qwen3.6-27B"],
                "task_tags": ["terminal-bench"],
            },
            "lineage": {"input_artifact_ids": ["dataset_1"]},
            "scores": {"quality": 0.82, "heldout_reward_delta": 0.1},
        },
    )

    artifacts = run_method(job, artifact_root=tmp_path / "artifacts")

    artifact = artifacts[0]
    assert artifact.compatibility == {
        "base_model": ["Qwen/Qwen3.6-27B"],
        "task_tags": ["terminal-bench"],
    }
    assert artifact.lineage == {"input_artifact_ids": ["dataset_1"]}
    assert artifact.scores == {"quality": 0.82, "heldout_reward_delta": 0.1}


def test_parametric_memory_rejects_mismatched_base_model_compatibility(tmp_path):
    adapter_dir = tmp_path / "adapter"
    adapter_dir.mkdir()
    job = _job(
        "parametric_memory_register",
        tmp_path,
        config={
            "adapter_uri": adapter_dir.as_uri(),
            "base_model": "Qwen/Qwen3.6-27B",
            "compatibility": {"base_model": ["other/model"]},
        },
    )

    with pytest.raises(ValueError, match="compatibility.base_model"):
        run_method(job, artifact_root=tmp_path / "artifacts")


def test_parse_capabilities_defaults_to_reference_job_types():
    assert _parse_capabilities([]) == [
        "text_memory",
        "text_memory_reflector",
        "text_memory_expel_reflector",
        "skill_bundle",
        "skill_bundle_reflector",
        "agent_system",
        "agent_system_reflector",
        "agent_system_history_reflector",
        "agent_system_pareto_reflector",
        "agent_system_gepa_reflector",
        "parametric_memory_register",
    ]


def test_parse_capabilities_uses_verified_context_method_defaults():
    assert _parse_capabilities(
        [],
        defaults=("text_memory", "parametric_memory_sd_lora"),
    ) == ["text_memory", "parametric_memory_sd_lora"]


class FakeClient:
    def __init__(self, job: dict[str, Any] | None) -> None:
        self.job = job
        self.claims: list[dict[str, Any]] = []
        self.heartbeats: list[dict[str, Any]] = []
        self.completed: list[dict[str, Any]] = []
        self.failed: list[dict[str, Any]] = []

    def claim(
        self,
        worker_id: str,
        capabilities: list[str],
        *,
        lease_seconds: int | None = None,
        method_capabilities: list[str] | None = None,
        method_identity_capabilities: dict[str, str] | None = None,
    ) -> dict[str, Any] | None:
        self.claims.append(
            {
                "worker_id": worker_id,
                "capabilities": capabilities,
                "lease_seconds": lease_seconds,
                "method_capabilities": method_capabilities,
                "method_identity_capabilities": method_identity_capabilities,
            }
        )
        return self.job

    def heartbeat(
        self,
        job_id: str,
        lease_id: str,
        *,
        progress: float | None = None,
        message: str | None = None,
    ) -> dict[str, Any]:
        self.heartbeats.append(
            {"job_id": job_id, "lease_id": lease_id, "progress": progress, "message": message}
        )
        return {}

    def complete(
        self,
        job_id: str,
        lease_id: str,
        artifacts: list[dict[str, Any]],
        *,
        report: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        self.completed.append(
            {"job_id": job_id, "lease_id": lease_id, "artifacts": artifacts, "report": report}
        )
        return {}

    def fail(
        self,
        job_id: str,
        lease_id: str,
        error: str,
        *,
        retryable: bool = True,
    ) -> dict[str, Any]:
        self.failed.append(
            {"job_id": job_id, "lease_id": lease_id, "error": error, "retryable": retryable}
        )
        return {}


def test_run_once_claims_and_completes_job(tmp_path):
    job = _job("skill_bundle", tmp_path, config={"name": "demo"}).model_dump(mode="json")
    client = FakeClient(job)

    result = run_once(
        client,
        worker_id="worker-1",
        capabilities=["skill_bundle"],
        artifact_root=tmp_path / "artifacts",
    )

    assert result is True
    assert client.heartbeats[0]["progress"] == 0.0
    assert client.completed[0]["job_id"] == "job_skill_bundle"
    assert client.completed[0]["artifacts"][0]["type"] == "skill_bundle"
    assert client.failed == []


@pytest.mark.parametrize(
    ("lease_seconds", "expected_effective_lease_seconds"),
    [(None, 600), (30, 30)],
)
def test_run_once_heartbeats_during_blocking_method_before_half_lease(
    tmp_path,
    monkeypatch,
    lease_seconds,
    expected_effective_lease_seconds,
):
    job = _job("skill_bundle", tmp_path, config={"name": "demo"}).model_dump(mode="json")
    client = FakeClient(job)
    release_method = threading.Event()
    seen_effective_leases: list[int] = []

    def heartbeat_interval(effective_lease_seconds: int) -> float:
        seen_effective_leases.append(effective_lease_seconds)
        return 0.01

    def blocking_method(job, *, artifact_root):
        assert release_method.wait(timeout=1.0)
        return []

    original_heartbeat = client.heartbeat

    def heartbeat(*args, **kwargs):
        result = original_heartbeat(*args, **kwargs)
        if len(client.heartbeats) >= 3:
            release_method.set()
        return result

    client.heartbeat = heartbeat
    monkeypatch.setattr(worker_module, "_heartbeat_interval_seconds", heartbeat_interval)
    monkeypatch.setattr(worker_module, "run_method", blocking_method)

    result = run_once(
        client,
        worker_id="worker-1",
        capabilities=["skill_bundle"],
        artifact_root=tmp_path / "artifacts",
        lease_seconds=lease_seconds,
    )

    assert result is True
    assert seen_effective_leases == [expected_effective_lease_seconds]
    assert client.claims[0]["lease_seconds"] == lease_seconds
    assert len(client.heartbeats) >= 4
    assert client.completed != []
    assert client.failed == []
    assert not any(
        thread.name.startswith("openevo-heartbeat-") for thread in threading.enumerate()
    )


@pytest.mark.parametrize("lease_seconds", [None, 30])
def test_heartbeat_interval_is_strictly_before_half_lease(lease_seconds):
    effective_lease_seconds = 600 if lease_seconds is None else lease_seconds

    interval = worker_module._heartbeat_interval_seconds(effective_lease_seconds)

    assert 0 < interval < effective_lease_seconds / 2


def test_run_once_stops_heartbeat_thread_when_method_fails(tmp_path, monkeypatch):
    job = _job("skill_bundle", tmp_path, config={"name": "demo"}).model_dump(mode="json")
    client = FakeClient(job)
    method_started = threading.Event()

    def failing_method(job, *, artifact_root):
        method_started.set()
        raise RuntimeError("method failed")

    monkeypatch.setattr(worker_module, "run_method", failing_method)

    result = run_once(
        client,
        worker_id="worker-1",
        capabilities=["skill_bundle"],
        artifact_root=tmp_path / "artifacts",
        lease_seconds=1,
    )

    assert result is True
    assert method_started.is_set()
    assert client.completed == []
    assert len(client.failed) == 1
    assert client.failed[0]["error"] == "method failed"
    assert client.failed[0]["retryable"] is False
    assert not any(
        thread.name.startswith("openevo-heartbeat-") for thread in threading.enumerate()
    )


def test_run_once_heartbeat_failure_prevents_complete_and_fails_once(tmp_path, monkeypatch):
    job = _job("skill_bundle", tmp_path, config={"name": "demo"}).model_dump(mode="json")
    client = FakeClient(job)
    release_method = threading.Event()
    heartbeat_calls = 0

    def blocking_method(job, *, artifact_root):
        assert release_method.wait(timeout=1.0)
        return []

    def heartbeat(job_id, lease_id, *, progress=None, message=None):
        nonlocal heartbeat_calls
        heartbeat_calls += 1
        if heartbeat_calls == 2:
            release_method.set()
            raise RuntimeError("heartbeat failed")
        return {}

    client.heartbeat = heartbeat
    monkeypatch.setattr(worker_module, "run_method", blocking_method)
    monkeypatch.setattr(worker_module, "_heartbeat_interval_seconds", lambda _: 0.01)

    result = run_once(
        client,
        worker_id="worker-1",
        capabilities=["skill_bundle"],
        artifact_root=tmp_path / "artifacts",
        lease_seconds=1,
    )

    assert result is True
    assert heartbeat_calls == 2
    assert client.completed == []
    assert len(client.failed) == 1
    assert client.failed[0]["error"] == "heartbeat failed"
    assert client.failed[0]["retryable"] is False
    assert not any(
        thread.name.startswith("openevo-heartbeat-") for thread in threading.enumerate()
    )


def test_run_once_fails_unknown_method(tmp_path):
    job = _job("unknown_method", tmp_path).model_dump(mode="json")
    client = FakeClient(job)

    result = run_once(
        client,
        worker_id="worker-1",
        capabilities=["unknown_method"],
        artifact_root=tmp_path / "artifacts",
    )

    assert result is True
    assert client.completed == []
    assert client.failed[0]["job_id"] == "job_unknown_method"
    assert "Unknown evolution method" in client.failed[0]["error"]
    assert client.failed[0]["retryable"] is True


def test_run_once_fails_invalid_claim_payload_with_job_identity(tmp_path):
    client = FakeClient(
        {
            "job_id": "job_invalid",
            "lease_id": "lease_invalid",
            "job_type": "reference",
        }
    )

    result = run_once(
        client,
        worker_id="worker-1",
        capabilities=["reference"],
        artifact_root=tmp_path / "artifacts",
    )

    assert result is True
    assert client.completed == []
    assert client.failed[0]["job_id"] == "job_invalid"
    assert client.failed[0]["lease_id"] == "lease_invalid"


def test_run_once_preserves_completion_error_when_redundant_fail_is_rejected(
    tmp_path,
) -> None:
    job = _job("skill_bundle", tmp_path, config={"name": "demo"}).model_dump(mode="json")
    client = FakeClient(job)

    def reject_complete(*args, **kwargs):
        del args, kwargs
        raise RuntimeError("completion rejected")

    def reject_fail(*args, **kwargs):
        del args, kwargs
        raise RuntimeError("lease already closed")

    client.complete = reject_complete
    client.fail = reject_fail

    with pytest.raises(RuntimeError, match="completion rejected") as raised:
        run_once(
            client,
            worker_id="worker-1",
            capabilities=["skill_bundle"],
            artifact_root=tmp_path / "artifacts",
        )

    assert any("lease already closed" in note for note in raised.value.__notes__)
