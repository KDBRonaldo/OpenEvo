from __future__ import annotations

import json
from pathlib import Path

from polar_evolution.terminal_bench_per_task import (
    ArtifactMaterializer,
    EvolutionArtifact,
    build_harbor_command,
    discover_agent_system_artifact_path,
    summarize_transition,
)


def test_agent_system_materializer_sets_harbor_kwargs(tmp_path: Path):
    artifact_path = tmp_path / "AGENTS.md"
    artifact_path.write_text("Inspect files first.\n", encoding="utf-8")
    materializer = ArtifactMaterializer()

    kwargs = materializer.materialize(
        EvolutionArtifact(
            artifact_type="agent_system",
            artifact_id="art-agent",
            path=artifact_path,
            task_id="fix-git",
            round=1,
            method="agent_system_reflector",
            source_dataset_artifact_ids=["dataset-r0"],
        )
    )

    assert kwargs == {"agent_system_path": str(artifact_path)}


def test_skill_and_memory_materializers_are_explicitly_skipped(tmp_path: Path):
    materializer = ArtifactMaterializer()
    skill = EvolutionArtifact(
        artifact_type="skill_bundle",
        artifact_id="art-skill",
        path=tmp_path / "skills",
        task_id="fix-git",
        round=1,
        method="skill_bundle",
        source_dataset_artifact_ids=[],
    )
    memory = EvolutionArtifact(
        artifact_type="memory",
        artifact_id="art-memory",
        path=tmp_path / "memory.md",
        task_id="fix-git",
        round=1,
        method="text_memory",
        source_dataset_artifact_ids=[],
    )

    assert materializer.materialize(skill) == {}
    assert materializer.materialize(memory) == {}
    assert materializer.skipped == [
        {
            "artifact_id": "art-skill",
            "artifact_type": "skill_bundle",
            "reason": "skill_bundle materialization is not implemented for Harbor Codex runs",
        },
        {
            "artifact_id": "art-memory",
            "artifact_type": "memory",
            "reason": "memory materialization is not implemented for Harbor Codex runs",
        },
    ]


def test_build_harbor_command_includes_agent_system_path_and_subscription_env():
    command = build_harbor_command(
        job_name="tb21-evolved-fix-git-r1",
        task_root=Path("/root/datasets/terminal-bench-2-1/tasks"),
        task_id="fix-git",
        model="gpt-5.5",
        env_json={"NO_PROXY": "localhost"},
        agent_kwargs={"agent_system_path": "/tmp/AGENTS.md"},
        verifier_env={"UV_NO_INDEX": "1"},
        n_concurrent=1,
    )

    assert command[:2] == ["harbor", "run"]
    assert "--include-task-name" in command
    assert "fix-git" in command
    assert "--ak" in command
    assert "mode=codex_subscription" in command
    assert "agent_system_path=/tmp/AGENTS.md" in command
    assert "env_json={\"NO_PROXY\":\"localhost\"}" in command
    assert "--verifier-env" in command
    assert "UV_NO_INDEX=1" in command


def test_discover_agent_system_artifact_path_reads_worker_manifest(tmp_path: Path):
    content = tmp_path / "workers" / "job-1" / "agent_system_reflector" / "agents.md"
    content.parent.mkdir(parents=True)
    content.write_text("rules\n", encoding="utf-8")
    job_payload = {
        "job": {
            "input_artifact_ids": ["dataset-r0"],
        }
    }
    completed_artifacts = [
        {
            "artifact_id": "art-agent",
            "type": "agent_system",
            "uri": content.resolve().as_uri(),
            "manifest": {"method": "agent_system_reflector"},
        }
    ]

    artifact = discover_agent_system_artifact_path(
        completed_artifacts,
        task_id="fix-git",
        round_number=1,
        job_payload=job_payload,
    )

    assert artifact.artifact_type == "agent_system"
    assert artifact.artifact_id == "art-agent"
    assert artifact.path == content
    assert artifact.source_dataset_artifact_ids == ["dataset-r0"]


def test_summarize_transition_classifies_pass_fail_changes():
    assert summarize_transition(0.0, 1.0) == "fail_to_pass"
    assert summarize_transition(1.0, 0.0) == "pass_to_fail"
    assert summarize_transition(1.0, 1.0) == "pass_to_pass"
    assert summarize_transition(0.0, 0.0) == "fail_to_fail"
