from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from openevo.daemon.evolution_orchestrator import (
    EvolutionOrchestrator,
    selected_document_evolution,
)


class _DatasetStore:
    def __init__(self) -> None:
        self.datasets: list[dict[str, Any]] = []

    def record_dataset_artifact(self, **kwargs: Any) -> None:
        self.datasets.append(kwargs)


def test_selected_document_evolution_keeps_only_closed_enabled_selections() -> None:
    assert selected_document_evolution(
        {
            "evolution": {
                "targets": {
                    "text_memory": {
                        "enabled": True,
                        "method": "text_memory_reflector",
                        "config": {"temperature": 0},
                    },
                    "skill_bundle": {
                        "enabled": False,
                        "method": "skill_bundle_reflector",
                        "config": {},
                    },
                    "bad target": {
                        "enabled": True,
                        "method": "anything",
                        "config": {},
                    },
                }
            }
        }
    ) == [
        {
            "target_id": "text_memory",
            "method": "text_memory_reflector",
            "config": {"temperature": 0},
        }
    ]


def test_capture_session_dataset_is_durable_transcript_evidence(
    tmp_path: Path,
) -> None:
    store = _DatasetStore()
    orchestrator = EvolutionOrchestrator(
        state_root=tmp_path,
        codex_binary="codex",
        model="test-model",
        timeout_seconds=30,
    )

    dataset = orchestrator.capture_session_dataset(
        session_id="session-1",
        request={
            "project_id": "project-1",
            "project_name": "Project",
            "task_title": "Question",
            "instruction": "What is OpenEvo?",
        },
        result={"response": "A self-hosted evolution system."},
        store=store,
    )

    manifest_path = Path(dataset["uri"].removeprefix("file://"))
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    record = json.loads(
        (manifest_path.parent / manifest["records_path"]).read_text(encoding="utf-8").strip()
    )
    assert manifest["capture_mode"] == "transcript"
    assert manifest["token_level_metrics_available"] is False
    assert record["traces"][0]["prompt_messages"][0]["content"] == "What is OpenEvo?"
    assert record["traces"][0]["response_messages"][0]["content"] == (
        "A self-hosted evolution system."
    )
    assert store.datasets[0]["artifact_id"] == "dataset-session-1"
    assert len(store.datasets[0]["manifest_sha256"]) == 64


def test_explicit_evolution_run_pins_requested_session_order(tmp_path: Path) -> None:
    captured: dict[str, Any] = {}

    class RecordingOrchestrator(EvolutionOrchestrator):
        def _run_selections(self, **kwargs: Any) -> dict[str, Any]:
            captured.update(kwargs)
            return {"artifacts": [], "errors": []}

    class Store:
        def project(self, project_id: str) -> dict[str, Any]:
            assert project_id == "project-1"
            return {"project_id": project_id, "display_name": "Project"}

    orchestrator = RecordingOrchestrator(
        state_root=tmp_path,
        codex_binary="codex",
        model="test-model",
        timeout_seconds=30,
    )
    result = orchestrator.evolve_run(
        run={
            "run_id": "run-1",
            "project_id": "project-1",
            "source_session_ids": ["session-1", "session-2", "session-3"],
            "selections": [
                {
                    "target_id": "text_memory",
                    "method": "text_memory_reflector",
                    "config": {},
                }
            ],
        },
        store=Store(),
    )

    assert result == {"artifacts": [], "errors": []}
    assert captured["current_session_id"] == "session-3"
    assert captured["prior_dataset_ids"] == [
        "dataset-session-1",
        "dataset-session-2",
    ]
    assert captured["promote_outputs"] is False


def test_artifact_document_reading_ignores_binary_and_preserves_relative_paths(
    tmp_path: Path,
) -> None:
    artifact_root = tmp_path / "artifact"
    (artifact_root / "nested").mkdir(parents=True)
    (artifact_root / "memory.md").write_text("# Memory", encoding="utf-8")
    (artifact_root / "nested" / "notes.txt").write_text("Notes", encoding="utf-8")
    (artifact_root / "binary.bin").write_bytes(b"\xff\xfe")

    documents = EvolutionOrchestrator._read_documents(
        artifact_root.resolve().as_uri(),
        "file_bundle",
    )

    assert [(item["path"], item["content"]) for item in documents] == [
        ("memory.md", "# Memory"),
        ("nested/notes.txt", "Notes"),
    ]


def test_project_managed_model_is_injected_into_reflector_execution(tmp_path: Path) -> None:
    prepared: list[str] = []

    def prepare(project_id: str) -> dict[str, str]:
        prepared.append(project_id)
        return {
            "model": "OpenEvo/Fixture-0.1B",
            "base_url": "http://127.0.0.1:18432/v1",
        }

    orchestrator = EvolutionOrchestrator(
        state_root=tmp_path,
        codex_binary="codex",
        model="subscription-model",
        timeout_seconds=30,
        inference_preparer=prepare,
    )
    orchestrator._load_catalog()
    method = orchestrator._registry.methods["text_memory_reflector"]

    config = orchestrator._method_config(method, {}, project_id="project-model")

    assert prepared == ["project-model"]
    assert config["reflector_llm"] == {
        "provider": "openai_chat",
        "model": "OpenEvo/Fixture-0.1B",
        "base_url": "http://127.0.0.1:18432/v1",
        "timeout_seconds": 30,
    }
