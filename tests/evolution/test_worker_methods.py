from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from polar_evolution.cli import _parse_capabilities
from polar_evolution.methods import run_method
from polar_evolution.models import ArtifactType, ContextResolveRequest, WorkerClaimedJob
from polar_evolution.store import EvolutionStore
from polar_evolution.worker import run_once


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


def test_text_memory_method_writes_markdown_from_dataset(tmp_path):
    job = _job(
        "text_memory",
        tmp_path,
        input_artifacts=[_dataset_artifact(tmp_path)],
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

    store = EvolutionStore(db_path=tmp_path / "evolution.db", artifact_root=tmp_path / "store")
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


def test_parse_capabilities_defaults_to_reference_job_types():
    assert _parse_capabilities([]) == [
        "text_memory",
        "skill_bundle",
        "agent_system",
        "parametric_memory_register",
    ]


class FakeClient:
    def __init__(self, job: dict[str, Any] | None) -> None:
        self.job = job
        self.heartbeats: list[dict[str, Any]] = []
        self.completed: list[dict[str, Any]] = []
        self.failed: list[dict[str, Any]] = []

    def claim(
        self,
        worker_id: str,
        capabilities: list[str],
        *,
        lease_seconds: int | None = None,
    ) -> dict[str, Any] | None:
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
