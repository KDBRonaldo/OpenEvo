from __future__ import annotations

from openevo.evolution.methods import METHOD_METADATA, METHOD_REGISTRY
from openevo.experiments import ExperimentConfig, compile_experiment


PROTECTED_METHOD_CONTRACTS = {
    "text_memory_expel_reflector": {
        "artifact_type": "text_memory",
        "input_requirements": ("dataset",),
        "supported_execution_modes": (
            "codex_subscription_transcript",
            "self-deployed",
        ),
        "default_config": {},
    },
    "skill_bundle_reflector": {
        "artifact_type": "skill_bundle",
        "input_requirements": ("dataset",),
        "supported_execution_modes": (
            "codex_subscription_transcript",
            "self-deployed",
        ),
        "default_config": {},
    },
    "agent_system_gepa_reflector": {
        "artifact_type": "agent_system",
        "input_requirements": ("dataset",),
        "supported_execution_modes": (
            "codex_subscription_transcript",
            "self-deployed",
        ),
        "default_config": {"target_path": "AGENTS.md"},
    },
}


def test_protected_methods_keep_algorithm_facing_registry_contract() -> None:
    for method_id, expected in PROTECTED_METHOD_CONTRACTS.items():
        assert METHOD_REGISTRY[method_id].__name__ == method_id
        metadata = METHOD_METADATA[method_id]
        assert metadata["method_id"] == method_id
        assert {key: metadata[key] for key in expected} == expected


def test_protected_methods_keep_generic_project_job_projection() -> None:
    config = ExperimentConfig.model_validate(
        {
            "version": 1,
            "experiment": {"name": "protected-method-projection"},
            "agent": {"preset": "codex", "model": "gpt-5.1-codex-mini"},
            "tasks": [{"id": "protected-task", "instruction": "Run task."}],
            "evolution": {
                "targets": {
                    "text_memory": {
                        "enabled": True,
                        "method": "text_memory_expel_reflector",
                        "config": {"max_records": 17},
                    },
                    "skill_bundle": {
                        "enabled": True,
                        "method": "skill_bundle_reflector",
                        "config": {"max_records": 13},
                    },
                    "agent_system": {
                        "enabled": True,
                        "method": "agent_system_gepa_reflector",
                        "config": {
                            "candidate_count": 2,
                            "target_path": "AGENTS.md",
                        },
                    },
                }
            },
        }
    )
    compiled = compile_experiment(config, run_id="preservation")

    jobs = compiled.evolution_job_payloads_for_round(
        0,
        dataset_artifact_id="dataset-current",
        context_artifact_ids={
            "dataset": ["dataset-history"],
            "text_memory": ["memory-history"],
            "skill_bundle": ["skill-history"],
            "agent_system": ["agent-system-history"],
        },
    )

    assert [job["method"] for job in jobs] == list(PROTECTED_METHOD_CONTRACTS)
    assert [job["input_artifact_ids"] for job in jobs] == [
        ["dataset-current", "memory-history"],
        ["dataset-current", "skill-history"],
        ["dataset-current", "dataset-history", "agent-system-history"],
    ]
    assert [job["config"]["max_records"] for job in jobs[:2]] == [17, 13]
    assert jobs[2]["config"]["candidate_count"] == 2
    assert jobs[2]["config"]["target_path"] == "AGENTS.md"
    assert all(
        job["config"]["reflector_llm"]
        == {"provider": "codex_cli", "model": "gpt-5.1-codex-mini"}
        for job in jobs
    )
    assert all(
        job["config"]["task_tags"]
        == ["openevo_run_task:preservation:protected-task"]
        for job in jobs
    )
