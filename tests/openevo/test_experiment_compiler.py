from __future__ import annotations

from pathlib import Path
import subprocess
import sys

import pytest
import yaml

from openevo import experiments
from openevo.evolution.framework import (
    EvolutionExecutionProfile,
    EvolutionFrameworkRegistry,
    EvolutionMethodDescriptor,
    EvolutionTargetDescriptor,
    MethodInputBinding,
    ProjectConfigInjection,
    TargetHandlerDescriptor,
)
from openevo.evolution.framework.builtins import (
    ImplementationDistributionIdentity,
    _handler_descriptors,
    _method_descriptors,
    _target_descriptors,
    build_builtin_registry,
)
from openevo.experiments.compiler import (
    _compile_core_experiment,
    _issue_core_project_scope_authority,
)
from openevo.runtime.managed import MANAGED_RUNTIME_RELEASES

ExperimentConfig = experiments.ExperimentConfig
_compile_experiment = experiments.compile_experiment
load_experiment_config = experiments.load_experiment_config

_REGISTRY_SNAPSHOT = build_builtin_registry(
    ImplementationDistributionIdentity(
        distribution="openevo",
        distribution_version="0.1.0",
        distribution_digest="a" * 64,
    )
)
_EXECUTION_PROFILE = EvolutionExecutionProfile(
    execution_mode="self_deployed",
    capture_mode="transcript",
    harness_id="codex",
    runtime_capabilities=(
        "adapter_serving",
        "constrained_trainer_contract",
        "trainer",
    ),
)


def _registry_with_external_target(*, inject_config: bool = True):
    identity = ImplementationDistributionIdentity(
        distribution="openevo",
        distribution_version="0.1.0",
        distribution_digest="b" * 64,
    )
    registry = EvolutionFrameworkRegistry()
    for descriptor in (
        *_target_descriptors(identity),
        *_handler_descriptors(identity),
        *_method_descriptors(identity),
        EvolutionTargetDescriptor(
            id="quality_notes",
            display_name="Quality notes",
            description="External quality notes target.",
            artifact_type="research_note",
            handler_id="quality_notes_handler",
            renderer_kind="markdown",
            default_method_id="quality_notes_external",
            implementation_ref=identity.ref("external:quality_notes_target"),
        ),
        TargetHandlerDescriptor(
            id="quality_notes_handler",
            target_id="quality_notes",
            artifact_types=("research_note",),
            renderer_kind="markdown",
            allowed_uri_schemes=("file",),
            allowed_media_types=("text/markdown",),
            allowed_destination_scopes=("target_data",),
            allowed_contribution_kinds=("staged_payload",),
            implementation_ref=identity.ref("external:quality_notes_handler"),
        ),
        EvolutionMethodDescriptor(
            id="quality_notes_external",
            display_name="Quality notes external",
            description="External method with source-sensitive bindings.",
            target_id="quality_notes",
            invocation_abi="method_context_v1",
            execution_modes=("self_deployed",),
            capture_modes=("transcript",),
            supported_harness_ids=("codex",),
            input_bindings=(
                MethodInputBinding(
                    binding_id="current",
                    source="current_dataset",
                    artifact_type="dataset",
                    min_count=1,
                    max_count=1,
                ),
                MethodInputBinding(
                    binding_id="history",
                    source="history_datasets",
                    artifact_type="dataset",
                ),
                MethodInputBinding(
                    binding_id="prior_notes_primary",
                    source="current_target_artifacts",
                    artifact_type="research_note",
                ),
                MethodInputBinding(
                    binding_id="prior_notes_secondary",
                    source="current_target_artifacts",
                    artifact_type="research_note",
                ),
                MethodInputBinding(
                    binding_id="explicit_evidence",
                    source="explicit_inputs",
                    artifact_type="evidence",
                ),
            ),
            output_artifact_types=("research_note",),
            config_schema={
                "type": "object",
                "properties": {
                    "prompt": {"type": "string"},
                    "reflector_llm": {
                        "type": "object",
                        "properties": {
                            "model": {"type": "string"},
                            "provider": {"type": "string"},
                        },
                        "required": ["model", "provider"],
                        "additionalProperties": False,
                    },
                    "base_model": {"type": "string"},
                },
                "additionalProperties": False,
            },
            project_config_injections=(
                (
                    ProjectConfigInjection(
                        field_name="base_model",
                        source="agent_model",
                    ),
                    ProjectConfigInjection(
                        field_name="reflector_llm",
                        source="reflector_llm",
                    ),
                )
                if inject_config
                else ()
            ),
            implementation_ref=identity.ref("external:quality_notes_method"),
        ),
    ):
        registry.register(descriptor)
    return registry.freeze()


def compile_experiment(
    config: ExperimentConfig,
    *args: object,
    **kwargs: object,
):
    return _compile_experiment(
        config,
        *args,
        registry_snapshot=_REGISTRY_SNAPSHOT,
        execution_profile=_EXECUTION_PROFILE,
        **kwargs,
    )


def compile_core_experiment(
    config: ExperimentConfig,
    *args: object,
    **kwargs: object,
):
    return _compile_core_experiment(
        config,
        *args,
        registry_snapshot=_REGISTRY_SNAPSHOT,
        execution_profile=_EXECUTION_PROFILE,
        **kwargs,
    )


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
    if payload["agent"].get("auth") in {"subscription", "chatgpt_subscription"}:
        payload["runtime"].update(
            {
                "profile": "managed_science",
                "image": MANAGED_RUNTIME_RELEASES[
                    "managed_science"
                ].immutable_reference,
                "container_user": "host",
            }
        )
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


@pytest.mark.parametrize(
    ("enable_memevolve", "enable_sd_lora", "expected_methods"),
    [
        (True, False, ("text_memory_memevolve",)),
        (False, True, ("parametric_memory_sd_lora",)),
        (
            True,
            True,
            ("text_memory_memevolve", "parametric_memory_sd_lora"),
        ),
    ],
)
def test_context_memory_methods_compile_independently(
    enable_memevolve: bool,
    enable_sd_lora: bool,
    expected_methods: tuple[str, ...],
) -> None:
    targets: dict[str, dict[str, object]] = {
        "text_memory": {"enabled": False},
        "parametric_memory": {"enabled": False},
        "skill_bundle": {"enabled": False},
        "agent_system": {"enabled": False},
    }
    if enable_memevolve:
        targets["text_memory"] = {
            "enabled": True,
            "method": "text_memory_memevolve",
            "config": {"candidate_count": 2},
        }
    if enable_sd_lora:
        targets["parametric_memory"] = {
            "enabled": True,
            "method": "parametric_memory_sd_lora",
            "config": {"model_revision": "a" * 40},
        }

    compiled = _compile_experiment(
        _config(evolution={"targets": targets}),
        registry_snapshot=_REGISTRY_SNAPSHOT,
        execution_profile=EvolutionExecutionProfile(
            execution_mode="self_deployed",
            capture_mode="transcript",
            harness_id="codex",
            runtime_capabilities=(
                "adapter_serving",
                "gpu",
                "sd_lora_continual_trainer",
            ),
        ),
    )

    specs = compiled.evolution_methods_for_round(
        0,
        prior_dataset_artifact_ids=[],
    )
    assert tuple(spec.method for spec in specs) == expected_methods
    by_method = {spec.method: spec for spec in specs}
    if enable_memevolve:
        assert by_method["text_memory_memevolve"].config["reflector_llm"] == {
            "provider": "codex_cli",
            "model": "gpt-5.1-codex-mini",
        }
    if enable_sd_lora:
        assert by_method["parametric_memory_sd_lora"].config["base_model"] == (
            "gpt-5.1-codex-mini"
        )
        assert by_method["parametric_memory_sd_lora"].config["model_revision"] == (
            "a" * 40
        )


def test_parametric_memory_config_rejects_undeclared_reflector_llm() -> None:
    with pytest.raises(ValueError, match="config.reflector_llm: unknown property"):
        compile_experiment(
            _config(
                evolution={
                    "targets": {
                        "text_memory": {"enabled": False},
                        "parametric_memory": {
                            "enabled": True,
                            "method": "parametric_memory_register",
                            "config": {
                                "adapter_uri": "file:///adapters/parser-memory",
                                "reflector_llm": {
                                    "provider": "bad",
                                    "model": "bad",
                                },
                            },
                        },
                        "skill_bundle": {"enabled": False},
                        "agent_system": {"enabled": False},
                    },
                }
            )
        )


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


def test_external_target_compiles_after_builtins_with_descriptor_artifact_type() -> None:
    compiled = _compile_experiment(
        _config(
            evolution={
                "targets": {
                    "quality_notes": {
                        "enabled": True,
                        "method": "quality_notes_external",
                        "config": {
                            "prompt": "Find unsupported claims.",
                            "base_model": "stale/model",
                            "reflector_llm": {
                                "provider": "stale",
                                "model": "stale/model",
                            },
                        },
                    },
                    "agent_system": {
                        "enabled": True,
                        "method": "agent_system_reflector",
                    },
                    "skill_bundle": {
                        "enabled": True,
                        "method": "skill_bundle_reflector",
                    },
                    "text_memory": {
                        "enabled": True,
                        "method": "text_memory_reflector",
                    },
                    "parametric_memory": {
                        "enabled": True,
                        "method": "parametric_memory_register",
                        "config": {
                            "adapter_uri": "file:///adapter",
                            "base_model": "model",
                        },
                    },
                }
            }
        ),
        rounds_override=2,
        registry_snapshot=_registry_with_external_target(),
        execution_profile=_EXECUTION_PROFILE,
    )

    specs = compiled.evolution_methods_for_round(
        0,
        prior_dataset_artifact_ids=["dataset-old"],
    )

    assert [spec.target_id for spec in specs] == [
        "text_memory",
        "parametric_memory",
        "skill_bundle",
        "agent_system",
        "quality_notes",
    ]
    assert specs[-1].artifact_type == "research_note"
    assert specs[-1].config == {
        "prompt": "Find unsupported claims.",
        "base_model": "gpt-5.1-codex-mini",
        "reflector_llm": {
            "provider": "codex_cli",
            "model": "gpt-5.1-codex-mini",
        },
    }


def test_external_method_without_injection_keeps_user_owned_same_name_fields() -> None:
    compiled = _compile_experiment(
        _config(
            evolution={
                "targets": {
                    "quality_notes": {
                        "enabled": True,
                        "method": "quality_notes_external",
                        "config": {
                            "prompt": "Find unsupported claims.",
                            "base_model": "user/model",
                            "reflector_llm": {
                                "provider": "user_provider",
                                "model": "user/model",
                            },
                        },
                    }
                }
            }
        ),
        registry_snapshot=_registry_with_external_target(inject_config=False),
        execution_profile=_EXECUTION_PROFILE,
    )

    spec = compiled.evolution_methods_for_round(
        0,
        prior_dataset_artifact_ids=[],
    )[0]

    assert spec.config["base_model"] == "user/model"
    assert spec.config["reflector_llm"] == {
        "provider": "user_provider",
        "model": "user/model",
    }


def test_external_source_bindings_preserve_descriptor_order_and_duplicates() -> None:
    compiled = _compile_experiment(
        _config(
            evolution={
                "targets": {
                    "quality_notes": {
                        "enabled": True,
                        "method": "quality_notes_external",
                        "config": {"prompt": "Find unsupported claims."},
                    }
                }
            }
        ),
        rounds_override=2,
        registry_snapshot=_registry_with_external_target(),
        execution_profile=_EXECUTION_PROFILE,
    )

    jobs = compiled.evolution_job_payloads_for_round(
        1,
        dataset_artifact_id="dataset-current",
        prior_dataset_artifact_ids=["dataset-old-a", "dataset-old-b"],
        context_artifact_ids={
            "dataset": ["dataset-old-a", "dataset-old-b"],
            "research_note": ["note-a", "note-b"],
            "evidence": ["evidence-a"],
        },
    )

    assert jobs[0]["input_bindings"] == [
        {"binding_id": "current", "artifact_ids": ["dataset-current"]},
        {
            "binding_id": "history",
            "artifact_ids": ["dataset-old-a", "dataset-old-b"],
        },
        {
            "binding_id": "prior_notes_primary",
            "artifact_ids": ["note-a", "note-b"],
        },
        {
            "binding_id": "prior_notes_secondary",
            "artifact_ids": ["note-a", "note-b"],
        },
        {"binding_id": "explicit_evidence", "artifact_ids": ["evidence-a"]},
    ]
    assert jobs[0]["input_artifact_ids"] == [
        "dataset-current",
        "dataset-old-a",
        "dataset-old-b",
        "note-a",
        "note-b",
        "note-a",
        "note-b",
        "evidence-a",
    ]


def test_expel_explicit_dataset_binding_matches_stable_current_only_projection() -> None:
    compiled = compile_experiment(
        _config(
            evolution={
                "targets": {
                    "text_memory": {
                        "enabled": True,
                        "method": "text_memory_expel_reflector",
                    }
                }
            }
        ),
        rounds_override=2,
    )

    jobs = compiled.evolution_job_payloads_for_round(
        1,
        dataset_artifact_id="dataset-current",
        prior_dataset_artifact_ids=["dataset-old-a", "dataset-old-b"],
        context_artifact_ids={"dataset": ["dataset-old-a", "dataset-old-b"]},
    )

    assert jobs[0]["input_bindings"][0] == {
        "binding_id": "dataset_inputs",
        "artifact_ids": ["dataset-current"],
    }


def test_compiled_target_selections_do_not_alias_mutable_project_config() -> None:
    config = _config(
        evolution={
            "targets": {
                "text_memory": {
                    "enabled": True,
                    "method": "text_memory_reflector",
                    "config": {"max_records": 1},
                }
            }
        }
    )
    compiled = compile_experiment(config)

    with pytest.raises(TypeError):
        config.evolution.targets["text_memory"].config["max_records"] = 2

    spec = compiled.evolution_methods_for_round(
        0,
        prior_dataset_artifact_ids=[],
    )[0]
    assert spec.config["max_records"] == 1


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

    with pytest.raises(ValueError, match="unknown target 'future_memory'"):
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

    with pytest.raises(
        ValueError,
        match="does not belong to target 'text_memory'",
    ):
        compile_experiment(config)


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
    assert all("promoted" not in job["config"] for job in jobs)
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
            agent={"preset": "codex", "model": "Qwen/Qwen3.6-35B-A3B"},
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
                            "base_model": "stale/wrong-model",
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


def test_core_project_compatibility_scope_is_stable_across_live_runs() -> None:
    config = _config(
        tasks=[
            {
                "id": "task-a",
                "instruction": "Do A.",
                "workspace": "/tmp/a",
                "metadata": {"openevo": {"project_id": "project-forged"}},
            }
        ],
    )
    unscoped = compile_experiment(config, run_id="run-unscoped")
    first = compile_core_experiment(
        config,
        run_id="run-first",
        core_project_scope=_issue_core_project_scope_authority(
            project_id="project-stable",
            run_id="run-first",
        ),
    )
    second = compile_core_experiment(
        config,
        run_id="run-second",
        core_project_scope=_issue_core_project_scope_authority(
            project_id="project-stable",
            run_id="run-second",
        ),
    )

    jobs = first.evolution_job_payloads_for_round(
        0,
        dataset_artifact_id="dataset_artifact_1",
        task_id="task-a",
    )
    unscoped_jobs = unscoped.evolution_job_payloads_for_round(
        0,
        dataset_artifact_id="dataset_artifact_1",
        task_id="task-a",
    )
    next_rollout = second.tasks[0].rollout_payload_for_round(
        0,
        context_artifact_ids=["artifact-first"],
    )

    artifact_tags = jobs[0]["config"]["compatibility"]["task_tags"]
    assert artifact_tags == [
        "openevo_run_task:run-first:task-a",
        "openevo_project:project-stable",
    ]
    assert next_rollout["metadata"]["task_tags"] == [
        "openevo_run_task:run-second:task-a",
        "openevo_project:project-stable",
    ]
    assert set(artifact_tags).intersection(next_rollout["metadata"]["task_tags"]) == {
        "openevo_project:project-stable"
    }
    assert unscoped_jobs[0]["config"]["compatibility"]["task_tags"] == [
        "openevo_run_task:run-unscoped:task-a"
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
    with pytest.raises(ValueError, match="allowed enum value"):
        compile_experiment(
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
    with pytest.raises(ValueError, match="allowed enum value"):
        compile_experiment(
            _config(
                agent={
                    "preset": "codex",
                    "model": "gpt-5.1-codex-mini",
                    "auth": "proxy",
                    "provider": "openai_chat",
                }
            )
        )


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


def test_compile_requires_explicit_registry_snapshot_and_execution_profile() -> None:
    with pytest.raises(TypeError, match="registry_snapshot"):
        _compile_experiment(_config(), execution_profile=_EXECUTION_PROFILE)
    with pytest.raises(TypeError, match="execution_profile"):
        _compile_experiment(_config(), registry_snapshot=_REGISTRY_SNAPSHOT)


def test_round_plan_has_stable_identity_and_all_specs_reference_it() -> None:
    compiled = compile_experiment(_config(), rounds_override=2, run_id="stable-run")

    plan = compiled.evolution_plan_for_round(
        1,
        task_id="component-extraction-train",
        prior_dataset_artifact_ids=["dataset-0"],
    )
    repeated = compiled.evolution_plan_for_round(
        1,
        task_id="component-extraction-train",
        prior_dataset_artifact_ids=["dataset-0"],
    )
    specs = compiled.evolution_methods_for_round(
        1,
        prior_dataset_artifact_ids=["dataset-0"],
    )

    assert repeated == plan
    assert plan.plan_id.startswith("plan-")
    assert len(plan.plan_id) <= 128
    assert all(spec.plan_id == plan.plan_id for spec in specs)
    assert all(spec.plan is specs[0].plan for spec in specs)
    assert all(
        spec.registry_snapshot_digest == plan.registry_snapshot_digest
        for spec in specs
    )
    assert {
        selection.target_id: selection.method_id for selection in plan.selections
    }["agent_system"] == "agent_system_history_reflector"


def test_plan_identity_distinguishes_task_round_and_ordered_prior_datasets() -> None:
    compiled = compile_experiment(
        _config(
            tasks=[
                {"id": "task-a", "instruction": "Do A."},
                {"id": "task-b", "instruction": "Do B."},
            ]
        ),
        rounds_override=2,
        run_id="identity-run",
    )

    def plan_id(task_id: str, round_index: int, prior: list[str]) -> str:
        return compiled.evolution_plan_for_round(
            round_index,
            task_id=task_id,
            prior_dataset_artifact_ids=prior,
        ).plan_id

    baseline = plan_id("task-a", 0, ["dataset-a", "dataset-b"])
    assert baseline != plan_id("task-b", 0, ["dataset-a", "dataset-b"])
    assert baseline != plan_id("task-a", 1, ["dataset-a", "dataset-b"])
    assert baseline != plan_id("task-a", 0, ["dataset-b", "dataset-a"])


def test_job_projection_carries_complete_plan_and_resolved_identities() -> None:
    compiled = compile_experiment(_config(), run_id="plan-job")

    jobs = compiled.evolution_job_payloads_for_round(
        0,
        dataset_artifact_id="dataset-current",
    )
    plan_ids = {job["plan"]["plan_id"] for job in jobs}

    assert len(plan_ids) == 1
    assert all(job["target_id"] == job["plan_selection"]["target_id"] for job in jobs)
    assert all(job["method"] == job["plan_selection"]["method_id"] for job in jobs)
    assert all(job["plan"]["registry_snapshot_digest"] for job in jobs)
    assert all(job["plan_selection"]["handler_id"].endswith("_handler") for job in jobs)
    assert all(len(job["plan_selection"]["method_identity_digest"]) == 64 for job in jobs)


def test_compiled_and_task_level_job_projection_are_equivalent() -> None:
    compiled = compile_experiment(_config(), run_id="projection-run")
    context = {
        "dataset": ["dataset-history"],
        "text_memory": ["memory-history"],
        "skill_bundle": ["skill-history"],
        "agent_system": ["agent-system-history"],
    }
    specs = compiled.evolution_methods_for_round(
        0,
        prior_dataset_artifact_ids=context["dataset"],
    )

    direct = compiled.tasks[0].evolution_job_payloads_for_round(
        0,
        specs,
        dataset_artifact_id="dataset-current",
        context_artifact_ids=context,
    )
    projected = compiled.evolution_job_payloads_for_round(
        0,
        dataset_artifact_id="dataset-current",
        context_artifact_ids=context,
    )

    assert projected == direct


def test_registry_rejects_unknown_config_and_profile_mismatch_at_plan_time() -> None:
    with pytest.raises(ValueError, match="unknown"):
        compile_experiment(
            _config(
                evolution={
                    "targets": {
                        "text_memory": {
                            "enabled": True,
                            "method": "text_memory_reflector",
                            "config": {"unknown": True},
                        }
                    }
                }
            )
        )

    subscription_profile = EvolutionExecutionProfile(
        execution_mode="subscription",
        capture_mode="transcript",
        harness_id="codex",
    )
    parametric = _config(
        evolution={
            "targets": {
                "parametric_memory": {
                    "enabled": True,
                    "method": "parametric_memory_register",
                    "config": {
                        "adapter_uri": "file:///adapter",
                        "base_model": "model",
                    },
                }
            }
        }
    )
    with pytest.raises(ValueError, match="execution mode"):
        _compile_experiment(
            parametric,
            registry_snapshot=_REGISTRY_SNAPSHOT,
            execution_profile=subscription_profile,
        )


def test_plan_id_is_stable_in_a_fresh_process() -> None:
    script = """
from openevo.evolution.framework import EvolutionExecutionProfile
from openevo.evolution.framework.builtins import ImplementationDistributionIdentity, build_builtin_registry
from openevo.experiments import ExperimentConfig, compile_experiment
snapshot = build_builtin_registry(ImplementationDistributionIdentity(distribution='openevo', distribution_version='0.1.0', distribution_digest='a' * 64))
profile = EvolutionExecutionProfile(execution_mode='self_deployed', capture_mode='transcript', harness_id='codex')
config = ExperimentConfig.model_validate({'version': 1, 'experiment': {'name': 'stable'}, 'agent': {'preset': 'codex', 'model': 'model'}, 'tasks': [{'id': 'task-a', 'instruction': 'Do A.'}]})
compiled = compile_experiment(config, run_id='run-a', registry_snapshot=snapshot, execution_profile=profile)
print(compiled.evolution_plan_for_round(0, prior_dataset_artifact_ids=[]).plan_id)
"""
    first = subprocess.check_output([sys.executable, "-c", script], text=True).strip()
    second = subprocess.check_output([sys.executable, "-c", script], text=True).strip()
    assert first == second
