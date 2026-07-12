from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from openevo import experiments

ExperimentConfig = experiments.ExperimentConfig
compile_experiment = experiments.compile_experiment
load_experiment_config = experiments.load_experiment_config


def _config(**overrides: object) -> ExperimentConfig:
    payload = {
        "version": 1,
        "experiment": {"name": "biology-components"},
        "agent": {"preset": "codex", "model": "gpt-5.1-codex-mini"},
        "runtime": {"image": "runtime:latest"},
        "tasks": [
            {
                "id": "component-extraction-train",
                "instruction": "Extract biological components into final_components.json.",
                "workspace": "/root/codex54minitest/five_article_agentic_workflow_subset",
            }
        ],
    }
    payload.update(overrides)
    return ExperimentConfig.model_validate(payload)


def test_policy_versions_are_deterministic_per_task_and_round() -> None:
    compiled = compile_experiment(_config(), rounds_override=2)
    task = compiled.tasks[0]

    assert task.policy_version_for_round(0) == (
        "openevo:biology-components:component-extraction-train:round-0"
    )
    assert task.policy_version_for_round(1) == (
        "openevo:biology-components:component-extraction-train:round-1"
    )
    assert task.rollout_payload_for_round(1, context_artifact_ids=[])["metadata"][
        "policy_version"
    ] == "openevo:biology-components:component-extraction-train:round-1"


def test_policy_versions_include_run_id_when_compiled_for_live_run() -> None:
    compiled = compile_experiment(_config(), rounds_override=2, run_id="runabc")
    task = compiled.tasks[0]

    assert task.policy_version_for_round(0) == (
        "openevo:biology-components:component-extraction-train:run-runabc:round-0"
    )
    assert task.dataset_payload_for_round(0)["query"]["policy_version"] == (
        "openevo:biology-components:component-extraction-train:run-runabc:round-0"
    )
    assert task.rollout_payload_for_round(0, context_artifact_ids=[])["metadata"][
        "policy_version"
    ] == "openevo:biology-components:component-extraction-train:run-runabc:round-0"


def test_live_rollout_payload_scopes_submitted_task_id_by_run_and_round() -> None:
    compiled = compile_experiment(_config(), rounds_override=2, run_id="runabc")
    task = compiled.tasks[0]

    payload = task.rollout_payload_for_round(1, context_artifact_ids=[])

    assert payload["task_id"] == "component-extraction-train--run-runabc--round-1"
    assert payload["metadata"]["task_id"] == "component-extraction-train"
    assert payload["metadata"]["policy_version"] == (
        "openevo:biology-components:component-extraction-train:run-runabc:round-1"
    )


def test_dataset_query_uses_exact_policy_version_without_latest_fallback() -> None:
    compiled = compile_experiment(_config())
    task = compiled.tasks[0]

    payload = task.dataset_payload_for_round(0)

    assert payload["query"]["policy_version"] == (
        "openevo:biology-components:component-extraction-train:round-0"
    )
    assert payload["query"]["event_types"] == ["openevo.session_completed"]
    assert payload["query"]["status"] == ["COMPLETED"]
    assert "latest" not in payload["query"]
    assert "task_tags" not in payload["query"]


def test_rollout_payload_uploads_workspace_with_explicit_runtime_image() -> None:
    compiled = compile_experiment(_config())
    payload = compiled.tasks[0].rollout_payload_for_round(0, context_artifact_ids=[])

    assert payload["runtime"]["image"] == "runtime:latest"
    assert payload["runtime"]["prepare"] == [
        {
            "type": "upload_dir",
            "source": "/root/codex54minitest/five_article_agentic_workflow_subset",
            "target": "/openevo/session/workspace",
        }
    ]


def test_rollout_payload_omits_runtime_for_default_runtime_task_without_workspace() -> None:
    config = _config(
        runtime={},
        tasks=[{"id": "task-a", "instruction": "Do A."}],
    )
    compiled = compile_experiment(config)

    payload = compiled.tasks[0].rollout_payload_for_round(0, context_artifact_ids=[])

    assert "runtime" not in payload


def test_rollout_metadata_uses_sanitized_agent_summary() -> None:
    config = _config(
        agent={
            "preset": "codex",
            "model": "gpt-5.1-codex-mini",
            "settings": {
                "auth_mode": "proxy",
                "api_key": "secret-setting-token",
            },
            "env": {"OPENAI_API_KEY": "secret-env-token"},
        }
    )
    compiled = compile_experiment(config)

    payload = compiled.tasks[0].rollout_payload_for_round(0, context_artifact_ids=[])

    assert payload["agent"]["settings"]["api_key"] == "secret-setting-token"
    assert payload["agent"]["env"]["OPENAI_API_KEY"] == "secret-env-token"
    assert payload["metadata"]["agent"] == {
        "harness": "codex",
        "model_name": "gpt-5.1-codex-mini",
    }
    assert "secret-setting-token" not in str(payload["metadata"])
    assert "secret-env-token" not in str(payload["metadata"])


def test_agent_native_memory_policy_is_preserved_in_rollout_settings() -> None:
    compiled = compile_experiment(
        _config(
            agent={
                "preset": "codex",
                "model": "gpt-5.1-codex-mini",
                "settings": {"native_memory_policy": "clear"},
            }
        )
    )

    payload = compiled.tasks[0].rollout_payload_for_round(0, context_artifact_ids=[])

    assert payload["agent"]["settings"]["native_memory_policy"] == "clear"


def test_agent_native_memory_policy_rejects_unknown_value() -> None:
    try:
        _config(
            agent={
                "preset": "codex",
                "model": "gpt-5.1-codex-mini",
                "settings": {"native_memory_policy": "wipe"},
            }
        )
    except ValueError as exc:
        assert "native_memory_policy" in str(exc)
    else:
        raise AssertionError("expected ValueError")


def test_agent_native_memory_policy_rejects_explicit_null() -> None:
    try:
        _config(
            agent={
                "preset": "codex",
                "model": "gpt-5.1-codex-mini",
                "settings": {"native_memory_policy": None},
            }
        )
    except ValueError as exc:
        assert "native_memory_policy" in str(exc)
    else:
        raise AssertionError("expected ValueError")


def test_relative_workspace_resolves_from_config_file(tmp_path: Path) -> None:
    config_dir = tmp_path / "configs"
    config_dir.mkdir()
    workspace = config_dir / "workspace"
    workspace.mkdir()
    config_path = config_dir / "experiment.yaml"
    payload = {
        "version": 1,
        "experiment": {"name": "relative-workspace"},
        "agent": {"preset": "codex", "model": "gpt-5.1-codex-mini"},
        "runtime": {"image": "runtime:latest"},
        "tasks": [
            {
                "id": "task-a",
                "instruction": "Do A.",
                "workspace": "workspace",
            }
        ],
    }
    config_path.write_text(yaml.safe_dump(payload), encoding="utf-8")
    config = load_experiment_config(config_path)
    compiled = compile_experiment(config)

    payload = compiled.tasks[0].rollout_payload_for_round(0, context_artifact_ids=[])

    assert payload["runtime"]["prepare"][0]["source"] == str(workspace.resolve())


def test_evolution_methods_default_to_text_memory_skill_bundle_agent_system() -> None:
    compiled = compile_experiment(_config())

    assert [
        spec.artifact_type
        for spec in compiled.evolution_methods_for_round(
            0,
            prior_dataset_artifact_ids=[],
        )
    ] == [
        "text_memory",
        "skill_bundle",
        "agent_system",
    ]


def test_evolution_methods_include_parametric_memory_when_enabled() -> None:
    compiled = compile_experiment(
        _config(
            evolution={
                "targets": {
                    "text_memory": {
                        "enabled": True,
                        "method": "text_memory_reflector",
                    },
                    "parametric_memory": {
                        "enabled": True,
                        "method": "parametric_memory_register",
                        "config": {
                            "adapter_uri": "file:///adapters/parser-memory",
                            "base_model": "gpt-5.1-codex-mini",
                            "adapter_id": "parser-memory",
                        },
                    },
                    "skill_bundle": {
                        "enabled": True,
                        "method": "skill_bundle_reflector",
                    },
                    "agent_system": {"enabled": True, "method": "auto"},
                },
            }
        )
    )

    specs = compiled.evolution_methods_for_round(0, prior_dataset_artifact_ids=[])

    assert [spec.artifact_type for spec in specs] == [
        "text_memory",
        "parametric_memory",
        "skill_bundle",
        "agent_system",
    ]
    assert specs[1].method == "parametric_memory_register"
    assert specs[1].config["adapter_uri"] == "file:///adapters/parser-memory"
    assert specs[1].config["base_model"] == "gpt-5.1-codex-mini"
    assert specs[1].config["adapter_id"] == "parser-memory"
    assert "reflector_llm" not in specs[1].config


def test_parametric_memory_config_drops_user_reflector_llm() -> None:
    compiled = compile_experiment(
        _config(
            evolution={
                "targets": {
                    "text_memory": {
                        "enabled": True,
                        "method": "text_memory_reflector",
                    },
                    "parametric_memory": {
                        "enabled": True,
                        "method": "parametric_memory_register",
                        "config": {
                            "adapter_uri": "file:///adapters/parser-memory",
                            "reflector_llm": {"provider": "bad", "model": "bad"},
                        },
                    },
                    "skill_bundle": {
                        "enabled": True,
                        "method": "skill_bundle_reflector",
                    },
                    "agent_system": {"enabled": True, "method": "auto"},
                },
            }
        )
    )

    specs = compiled.evolution_methods_for_round(0, prior_dataset_artifact_ids=[])

    assert specs[1].artifact_type == "parametric_memory"
    assert specs[1].config["adapter_uri"] == "file:///adapters/parser-memory"
    assert "reflector_llm" not in specs[1].config


def test_agent_system_auto_resolves_from_prior_dataset_snapshot_not_round() -> None:
    compiled = compile_experiment(_config(), rounds_override=2)

    without_history = compiled.evolution_methods_for_round(
        1,
        prior_dataset_artifact_ids=[],
    )[-1]
    with_history = compiled.evolution_methods_for_round(
        0,
        prior_dataset_artifact_ids=["dataset_artifact_0"],
    )[-1]

    assert without_history.method == "agent_system_reflector"
    assert without_history.requested_method == "auto"
    assert without_history.prior_dataset_artifact_ids == ()
    assert with_history.method == "agent_system_history_reflector"
    assert with_history.requested_method == "auto"
    assert with_history.prior_dataset_artifact_ids == ("dataset_artifact_0",)


def test_prior_dataset_snapshot_rejects_bare_string_and_non_string_ids() -> None:
    compiled = compile_experiment(_config())

    with pytest.raises(TypeError, match="must be a sequence of strings"):
        compiled.evolution_methods_for_round(
            0,
            prior_dataset_artifact_ids="dataset_artifact_0",
        )
    with pytest.raises(TypeError, match="must contain only strings"):
        compiled.evolution_methods_for_round(
            0,
            prior_dataset_artifact_ids=["dataset_artifact_0", 1],
        )


def test_generic_target_config_is_projected_into_compiled_method_specs() -> None:
    compiled = compile_experiment(
        _config(
            evolution={
                "targets": {
                    "text_memory": {
                        "enabled": True,
                        "method": "text_memory_reflector",
                        "config": {"max_records": 11},
                    },
                    "parametric_memory": {
                        "enabled": True,
                        "method": "parametric_memory_register",
                        "config": {
                            "adapter_uri": "file:///adapters/parser-memory",
                            "base_model": "gpt-5.1-codex-mini",
                            "adapter_id": "parser-memory",
                        },
                    },
                    "skill_bundle": {
                        "enabled": True,
                        "method": "skill_bundle_reflector",
                        "config": {
                            "max_records": 7,
                            "base_skill_markdown": "Existing skill.",
                        },
                    },
                    "agent_system": {
                        "enabled": True,
                        "method": "agent_system_reflector",
                        "config": {
                            "max_records": 5,
                            "target_path": "CLAUDE.md",
                        },
                    },
                }
            }
        )
    )

    specs = compiled.evolution_methods_for_round(0, prior_dataset_artifact_ids=[])

    assert [spec.artifact_type for spec in specs] == [
        "text_memory",
        "parametric_memory",
        "skill_bundle",
        "agent_system",
    ]
    assert specs[0].config["max_records"] == 11
    assert specs[1].config["adapter_id"] == "parser-memory"
    assert specs[2].config["max_records"] == 7
    assert specs[2].config["base_skill_markdown"] == "Existing skill."
    assert specs[3].config["max_records"] == 5
    assert specs[3].config["target_path"] == "CLAUDE.md"
    assert specs[0].config["reflector_llm"] == {
        "provider": "codex_cli",
        "model": "gpt-5.1-codex-mini",
    }
    assert specs[2].config["reflector_llm"] == specs[0].config["reflector_llm"]
    assert specs[3].config["reflector_llm"] == specs[0].config["reflector_llm"]
    assert "reflector_llm" not in specs[1].config


def test_compiled_target_selections_do_not_alias_mutable_project_config() -> None:
    config = _config(
        evolution={
            "targets": {
                "text_memory": {
                    "enabled": True,
                    "method": "text_memory_reflector",
                    "config": {"nested": {"values": [1]}},
                }
            }
        }
    )
    compiled = compile_experiment(config)

    config.evolution.targets["text_memory"].config["nested"]["values"].append(2)

    spec = compiled.evolution_methods_for_round(
        0,
        prior_dataset_artifact_ids=[],
    )[0]
    assert spec.config["nested"] == {"values": [1]}


def test_compile_experiment_rejects_unknown_evolution_target() -> None:
    config = _config(
        evolution={
            "targets": {
                "text_memory": {
                    "enabled": True,
                    "method": "text_memory_reflector",
                },
                "future_memory": {
                    "enabled": True,
                    "method": "future_memory_reflector",
                },
            }
        }
    )

    with pytest.raises(ValueError, match="Unsupported evolution target: future_memory"):
        compile_experiment(config)


def test_compile_experiment_preserves_but_ignores_disabled_unknown_target() -> None:
    config = _config(
        evolution={
            "targets": {
                "text_memory": {
                    "enabled": True,
                    "method": "text_memory_reflector",
                },
                "future_memory": {
                    "enabled": False,
                    "method": "future_memory_reflector",
                    "config": {"draft": {"keep": True}},
                },
            }
        }
    )

    compiled = compile_experiment(config)

    assert config.evolution.targets["future_memory"].config == {
        "draft": {"keep": True}
    }
    assert [
        spec.artifact_type
        for spec in compiled.evolution_methods_for_round(
            0,
            prior_dataset_artifact_ids=[],
        )
    ] == ["text_memory"]


def test_compile_experiment_rejects_cross_target_method_before_job_creation() -> None:
    config = _config(
        agent={
            "preset": "codex",
            "model": "gpt-5.1-codex-mini",
            "auth": "subscription",
            "settings": {"capture_mode": "transcript"},
        },
        evolution={
            "targets": {
                "text_memory": {
                    "enabled": True,
                    "method": "parametric_memory_register",
                    "config": {
                        "adapter_uri": "s3://adapters/parser-memory",
                        "base_model": "model",
                    },
                }
            }
        },
    )

    compiled = compile_experiment(config)
    with pytest.raises(
        ValueError,
        match=(
            "method 'parametric_memory_register' belongs to target "
            "'parametric_memory', not 'text_memory'"
        ),
    ):
        compiled.evolution_methods_for_round(
            0,
            prior_dataset_artifact_ids=[],
        )


def test_evolution_job_payloads_include_ordered_methods_and_reflector_llm() -> None:
    compiled = compile_experiment(_config(), rounds_override=2)

    jobs = compiled.evolution_job_payloads_for_round(
        1,
        dataset_artifact_id="dataset_artifact_1",
        context_artifact_ids={
            "dataset": ["dataset_artifact_0"],
            "text_memory": ["memory_1"],
            "skill_bundle": ["skill_1"],
            "agent_system": ["agent_system_1"],
        },
    )

    assert [job["method"] for job in jobs] == [
        "text_memory_reflector",
        "skill_bundle_reflector",
        "agent_system_history_reflector",
    ]
    assert [job["job_type"] for job in jobs] == [
        "text_memory_reflector",
        "skill_bundle_reflector",
        "agent_system_history_reflector",
    ]
    assert jobs[0]["input_artifact_ids"] == ["dataset_artifact_1", "memory_1"]
    assert jobs[1]["input_artifact_ids"] == ["dataset_artifact_1", "skill_1"]
    assert jobs[2]["input_artifact_ids"] == [
        "dataset_artifact_1",
        "dataset_artifact_0",
        "agent_system_1",
    ]
    assert jobs[2]["config"]["target_path"] == "AGENTS.md"
    assert jobs[2]["config"]["lineage"]["method_resolution"] == {
        "requested_method": "auto",
        "resolved_method": "agent_system_history_reflector",
        "prior_dataset_artifact_ids": ["dataset_artifact_0"],
    }
    assert all(job["config"]["promoted"] is True for job in jobs)
    assert all("base_model" not in job["config"]["compatibility"] for job in jobs)
    assert jobs[2]["config"]["reflector_llm"] == {
        "provider": "codex_cli",
        "model": "gpt-5.1-codex-mini",
    }


def test_flat_context_is_not_reinterpreted_as_prior_dataset_history() -> None:
    compiled = compile_experiment(_config())

    jobs = compiled.evolution_job_payloads_for_round(
        0,
        dataset_artifact_id="dataset_artifact_0",
        context_artifact_ids=["prior_agent_system"],
    )

    agent_system_job = jobs[-1]
    assert agent_system_job["method"] == "agent_system_reflector"
    assert agent_system_job["input_artifact_ids"] == [
        "dataset_artifact_0",
        "prior_agent_system",
    ]


def test_parametric_memory_job_uses_prior_parametric_context_only() -> None:
    compiled = compile_experiment(
        _config(
            evolution={
                "targets": {
                    "text_memory": {"enabled": False},
                    "skill_bundle": {"enabled": False},
                    "agent_system": {"enabled": False},
                    "parametric_memory": {
                        "enabled": True,
                        "method": "parametric_memory_register",
                        "config": {
                            "adapter_uri": "file:///tmp/qwen-memory-adapter",
                            "base_model": "Qwen/Qwen3.6-35B-A3B",
                        },
                    },
                },
            },
        ),
        rounds_override=2,
    )

    jobs = compiled.evolution_job_payloads_for_round(
        1,
        dataset_artifact_id="dataset_artifact_1",
        context_artifact_ids={
            "text_memory": ["memory_1"],
            "parametric_memory": ["adapter_1"],
            "agent_system": ["agent_system_1"],
        },
    )

    assert [job["method"] for job in jobs] == ["parametric_memory_register"]
    assert jobs[0]["input_artifact_ids"] == ["dataset_artifact_1", "adapter_1"]
    assert jobs[0]["config"]["compatibility"]["base_model"] == ["Qwen/Qwen3.6-35B-A3B"]


def test_parametric_memory_job_derives_base_model_from_agent_model_when_absent() -> None:
    compiled = compile_experiment(
        _config(
            agent={"preset": "codex", "model": "Qwen/Qwen3-Coder-30B-A3B-Instruct"},
            evolution={
                "targets": {
                    "text_memory": {"enabled": False},
                    "skill_bundle": {"enabled": False},
                    "agent_system": {"enabled": False},
                    "parametric_memory": {
                        "enabled": True,
                        "method": "parametric_memory_register",
                        "config": {
                            "adapter_uri": "file:///tmp/qwen-memory-adapter",
                        },
                    },
                },
            },
        )
    )

    jobs = compiled.evolution_job_payloads_for_round(
        0,
        dataset_artifact_id="dataset_artifact_1",
        context_artifact_ids=[],
    )

    assert jobs[0]["config"]["base_model"] == "Qwen/Qwen3-Coder-30B-A3B-Instruct"
    assert jobs[0]["config"]["compatibility"]["base_model"] == [
        "Qwen/Qwen3-Coder-30B-A3B-Instruct"
    ]


def test_evolution_jobs_are_unpromoted_when_promotion_gate_is_enabled() -> None:
    compiled = compile_experiment(
        _config(
            evolution={
                "promotion_gate": {
                    "mode": "llm",
                    "min_score": 0.8,
                }
            }
        )
    )

    jobs = compiled.evolution_job_payloads_for_round(
        0,
        dataset_artifact_id="dataset_artifact_1",
        context_artifact_ids=[],
    )

    assert all(job["config"]["promoted"] is False for job in jobs)
    assert jobs[0]["config"]["promotion_gate"]["mode"] == "llm"
    assert jobs[0]["config"]["promotion_contract"] == {
        "required": True,
        "fields": [
            "trajectory_findings",
            "proposed_changes",
            "expected_benefits",
            "risks",
            "validation_checks",
        ],
    }


def test_promotion_gate_accepts_human_input_mode() -> None:
    compiled = compile_experiment(
        _config(
            evolution={
                "promotion_gate": {
                    "mode": "human",
                    "human_input": "tui",
                }
            }
        )
    )

    jobs = compiled.tasks[0].evolution_job_payloads_for_round(
        0,
        compiled.evolution_methods_for_round(
            0,
            prior_dataset_artifact_ids=[],
        ),
        dataset_artifact_id="dataset_artifact_1",
        context_artifact_ids=[],
    )

    assert compiled.promotion_gate["human_input"] == "tui"
    assert jobs[0]["config"]["promotion_gate"]["human_input"] == "tui"


def test_evolution_job_payloads_do_not_persist_promotion_llm_secrets() -> None:
    compiled = compile_experiment(
        _config(
            evolution={
                "promotion_gate": {
                    "mode": "llm",
                    "min_score": 0.8,
                    "llm": {
                        "provider": "openai_chat",
                        "model": "reviewer-model",
                        "api_key": "secret-reviewer-key",
                        "base_url": "http://reviewer.test/v1",
                    },
                }
            }
        )
    )

    jobs = compiled.evolution_job_payloads_for_round(
        0,
        dataset_artifact_id="dataset_artifact_1",
        context_artifact_ids=[],
    )

    assert "secret-reviewer-key" in str(compiled.promotion_gate)
    assert "secret-reviewer-key" not in str(jobs)
    assert "llm" not in jobs[0]["config"]["promotion_gate"]


def test_evolution_job_compatibility_uses_single_task_scoped_tag() -> None:
    config = _config(
        tasks=[
            {"id": "task-a", "instruction": "Do A.", "workspace": "/tmp/a"},
            {"id": "task-b", "instruction": "Do B.", "workspace": "/tmp/b"},
        ],
    )
    compiled = compile_experiment(config, run_id="runabc")

    jobs = compiled.evolution_job_payloads_for_round(
        0,
        dataset_artifact_id="dataset_artifact_1",
        task_id="task-a",
    )

    assert jobs[0]["config"]["compatibility"]["task_tags"] == [
        "openevo_run_task:runabc:task-a"
    ]
    assert "openevo:biology-components" not in jobs[0]["config"]["compatibility"][
        "task_tags"
    ]


def test_parametric_memory_job_compatibility_preserves_base_model_and_task_scope() -> None:
    compiled = compile_experiment(
        _config(
            evolution={
                "targets": {
                    "text_memory": {"enabled": False},
                    "parametric_memory": {
                        "enabled": True,
                        "method": "parametric_memory_register",
                        "config": {
                            "adapter_uri": "file:///adapters/parser-memory",
                            "base_model": "gpt-5.1-codex-mini",
                            "adapter_id": "parser-memory",
                            "compatibility": {
                                "base_model": ["wrong-model"],
                                "task_tags": ["wrong-task"],
                                "agent_harness": ["wrong-harness"],
                                "capability": ["component-extraction"],
                            },
                        },
                    },
                    "skill_bundle": {"enabled": False},
                    "agent_system": {"enabled": False},
                },
            }
        )
    )

    jobs = compiled.evolution_job_payloads_for_round(
        0,
        dataset_artifact_id="dataset_artifact_1",
    )

    assert len(jobs) == 1
    assert jobs[0]["method"] == "parametric_memory_register"
    compatibility = jobs[0]["config"]["compatibility"]
    assert compatibility["base_model"] == ["gpt-5.1-codex-mini"]
    assert compatibility["task_tags"] == [
        "openevo_task:biology-components:component-extraction-train"
    ]
    assert compatibility["agent_harness"] == ["codex"]
    assert compatibility["capability"] == ["component-extraction"]


def test_history_agent_system_jobs_include_prior_dataset_artifacts() -> None:
    compiled = compile_experiment(_config(), rounds_override=2)

    jobs = compiled.evolution_job_payloads_for_round(
        1,
        dataset_artifact_id="dataset_artifact_1",
        context_artifact_ids={
            "dataset": ["dataset_artifact_0"],
            "agent_system": ["agent_system_0"],
        },
    )

    assert jobs[2]["method"] == "agent_system_history_reflector"
    assert jobs[2]["input_artifact_ids"] == [
        "dataset_artifact_1",
        "dataset_artifact_0",
        "agent_system_0",
    ]


def test_rollout_context_excludes_internal_dataset_history() -> None:
    compiled = compile_experiment(_config(), rounds_override=2)

    payload = compiled.tasks[0].rollout_payload_for_round(
        1,
        context_artifact_ids={
            "dataset": ["dataset_artifact_0"],
            "text_memory": ["memory_0"],
            "parametric_memory": ["adapter_0"],
        },
    )

    assert payload["metadata"]["evolution"]["context_artifact_ids"] == [
        "memory_0",
        "adapter_0",
    ]


def test_rollout_payload_emits_explicit_empty_context_selection() -> None:
    compiled = compile_experiment(_config(), rounds_override=2)

    payload = compiled.tasks[0].rollout_payload_for_round(0, context_artifact_ids=[])

    assert payload["metadata"]["evolution"]["context_artifact_ids"] == []


def test_subscription_agents_default_reflector_provider_to_codex_cli() -> None:
    compiled = compile_experiment(
        _config(
            agent={
                "preset": "codex",
                "model": "gpt-5.1-codex-mini",
                "auth": "subscription",
                "settings": {"capture_mode": "transcript"},
            }
        )
    )

    jobs = compiled.evolution_job_payloads_for_round(
        0,
        dataset_artifact_id="dataset_artifact_1",
        context_artifact_ids=[],
    )

    assert jobs[0]["config"]["reflector_llm"]["provider"] == "codex_cli"


def test_subscription_agents_respect_explicit_reflector_provider() -> None:
    compiled = compile_experiment(
        _config(
            agent={
                "preset": "codex",
                "model": "gpt-5.1-codex-mini",
                "auth": "subscription",
                "provider": "openai_chat",
                "settings": {"capture_mode": "transcript"},
            }
        )
    )

    jobs = compiled.evolution_job_payloads_for_round(
        0,
        dataset_artifact_id="dataset_artifact_1",
        context_artifact_ids=[],
    )

    assert jobs[0]["config"]["reflector_llm"]["provider"] == "openai_chat"


def test_subscription_agent_payload_defaults_auth_mode() -> None:
    compiled = compile_experiment(
        _config(
            agent={
                "preset": "codex",
                "model": "gpt-5.1-codex-mini",
                "auth": "subscription",
                "settings": {"capture_mode": "transcript"},
            }
        )
    )

    payload = compiled.tasks[0].rollout_payload_for_round(0, context_artifact_ids=[])

    assert payload["agent"]["settings"]["auth_mode"] == "subscription"
    assert payload["agent"]["settings"]["capture_mode"] == "transcript"


def test_codex_cli_agent_provider_defaults_reflector_provider_to_codex_cli() -> None:
    compiled = compile_experiment(
        _config(
            agent={
                "preset": "codex",
                "model": "gpt-5.1-codex-mini",
                "provider": "codex_cli",
            }
        )
    )

    jobs = compiled.evolution_job_payloads_for_round(
        0,
        dataset_artifact_id="dataset_artifact_1",
        context_artifact_ids=[],
    )

    assert jobs[0]["config"]["reflector_llm"]["provider"] == "codex_cli"


def test_proxy_codex_cli_agent_uses_codex_cli_reflector_in_job_config() -> None:
    compiled = compile_experiment(
        _config(
            agent={
                "preset": "codex",
                "model": "Qwen/Qwen3-Coder-30B-A3B-Instruct",
                "auth": "proxy",
                "provider": "codex_cli",
                "settings": {"auth_mode": "proxy"},
            }
        )
    )

    jobs = compiled.evolution_job_payloads_for_round(
        0,
        dataset_artifact_id="dataset_artifact_1",
        context_artifact_ids=[],
    )

    assert jobs[0]["config"]["reflector_llm"] == {
        "provider": "codex_cli",
        "model": "Qwen/Qwen3-Coder-30B-A3B-Instruct",
    }
    assert jobs[0]["config"]["reflector_llm"]["provider"] != "openai_chat"


def test_proxy_agent_respects_explicit_openai_chat_reflector_provider() -> None:
    compiled = compile_experiment(
        _config(
            agent={
                "preset": "codex",
                "model": "gpt-5.1-codex-mini",
                "auth": "proxy",
                "provider": "openai_chat",
            }
        )
    )

    jobs = compiled.evolution_job_payloads_for_round(
        0,
        dataset_artifact_id="dataset_artifact_1",
        context_artifact_ids=[],
    )

    assert jobs[0]["config"]["reflector_llm"]["provider"] == "openai_chat"


def test_task_filter_and_round_override_are_applied() -> None:
    config = _config(
        tasks=[
            {"id": "task-a", "instruction": "Do A.", "workspace": "/tmp/a"},
            {"id": "task-b", "instruction": "Do B.", "workspace": "/tmp/b"},
        ],
        evolution={"rounds": 3},
    )

    compiled = compile_experiment(config, task_ids=["task-b"], rounds_override=1)

    assert compiled.round_count == 1
    assert [task.task_id for task in compiled.tasks] == ["task-b"]


def test_empty_task_filter_is_rejected() -> None:
    config = _config()

    try:
        compile_experiment(config, task_ids=[])
    except ValueError as exc:
        assert "task_ids must select at least one task" in str(exc)
    else:
        raise AssertionError("expected ValueError")


def test_workspace_upload_precedes_runtime_prepare_actions() -> None:
    config = _config(
        runtime={
            "image": "runtime:latest",
            "prepare": [
                {
                    "type": "exec",
                    "command": "python -m pip install -r requirements.txt",
                    "cwd": "/openevo/session/workspace",
                }
            ],
        }
    )
    compiled = compile_experiment(config)

    payload = compiled.tasks[0].rollout_payload_for_round(0, context_artifact_ids=[])

    assert payload["runtime"]["prepare"] == [
        {
            "type": "upload_dir",
            "source": "/root/codex54minitest/five_article_agentic_workflow_subset",
            "target": "/openevo/session/workspace",
        },
        {
            "type": "exec",
            "command": "python -m pip install -r requirements.txt",
            "cwd": "/openevo/session/workspace",
            "env": None,
            "source": None,
            "target": None,
        },
    ]
