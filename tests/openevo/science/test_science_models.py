from __future__ import annotations

from copy import deepcopy
from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from openevo.projects.science import (
    EvolutionTargetsConfig,
    ScienceProjectConfig,
    load_science_project_config,
)


def _minimal_payload() -> dict:
    return {
        "version": 1,
        "project": {"name": "protein-design"},
        "remote_profile": "science-team",
        "task": {
            "id": "folding-baseline",
            "objective": "Improve the folding baseline.",
        },
    }


def _write_yaml(path: Path, payload: dict) -> Path:
    path.write_text(yaml.safe_dump(payload), encoding="utf-8")
    return path


def test_minimal_project_defaults_to_science_subscription_transcript() -> None:
    config = ScienceProjectConfig.model_validate(_minimal_payload())

    assert config.environment.profile == "managed_science"
    assert config.execution.mode == "codex_subscription_transcript"
    assert config.execution.codex_model == "gpt-5.5"
    assert config.evolution.model_dump(mode="json")["targets"] == {
        "text_memory": {
            "enabled": True,
            "method": "text_memory_reflector",
            "config": {},
        },
        "parametric_memory": {
            "enabled": False,
            "method": "parametric_memory_register",
            "config": {},
        },
        "skill_bundle": {
            "enabled": True,
            "method": "skill_bundle_reflector",
            "config": {},
        },
        "agent_system": {
            "enabled": True,
            "method": "auto",
            "config": {"target_path": "AGENTS.md"},
        },
    }


@pytest.mark.parametrize("env_name", ["HOME", "PATH", "CODEX_HOME"])
def test_managed_environment_rejects_core_owned_environment_overrides(
    env_name: str,
) -> None:
    payload = _minimal_payload()
    payload["environment"] = {"env": {env_name: "/attacker"}}

    with pytest.raises(ValidationError, match=f"{env_name} is Core-owned"):
        ScienceProjectConfig.model_validate(payload)


def test_custom_image_rejects_codex_subscription_with_actionable_error() -> None:
    payload = _minimal_payload() | {
        "environment": {
            "profile": "custom_image",
            "custom_image": "ghcr.io/example/science:latest",
        }
    }

    with pytest.raises(
        ValidationError,
        match="Codex subscription is not supported.*custom_image.*managed environment",
    ):
        ScienceProjectConfig.model_validate(payload)


def test_managed_local_inference_requires_hf_model() -> None:
    payload = _minimal_payload() | {
        "execution": {"mode": "codex_managed_local_inference"}
    }

    with pytest.raises(ValidationError, match="hf_model"):
        ScienceProjectConfig.model_validate(payload)


def test_science_project_accepts_legacy_managed_local_inference_alias() -> None:
    project = ScienceProjectConfig.model_validate(
        {
            "version": 1,
            "project": {"name": "protein"},
            "remote_profile": "lab",
            "task": {
                "id": "fold",
                "objective": "Analyze folding",
                "source": {"type": "scratch"},
            },
            "execution": {
                "mode": "codex_managed_local_inference",
                "hf_model": "Qwen/Qwen3-8B",
            },
        }
    )
    assert project.execution.mode == "self-deployed"
    assert project.model_dump(mode="json")["execution"]["mode"] == "self-deployed"


def test_science_project_emits_public_self_deployed_mode() -> None:
    project = ScienceProjectConfig.model_validate(
        {
            "version": 1,
            "project": {"name": "protein"},
            "remote_profile": "lab",
            "task": {
                "id": "fold",
                "objective": "Analyze folding",
                "source": {"type": "scratch"},
            },
            "execution": {"mode": "self-deployed", "hf_model": "Qwen/Qwen3-8B"},
        }
    )
    assert project.execution.mode == "self-deployed"


def test_subscription_transcript_rejects_hf_model() -> None:
    payload = _minimal_payload() | {
        "execution": {
            "mode": "codex_subscription_transcript",
            "hf_model": "Qwen/Qwen3-Coder-30B-A3B-Instruct",
        }
    }

    with pytest.raises(
        ValidationError,
        match="execution.hf_model is only valid for self-deployed mode",
    ):
        ScienceProjectConfig.model_validate(payload)


def test_managed_local_inference_accepts_hf_model() -> None:
    payload = _minimal_payload() | {
        "execution": {
            "mode": "codex_managed_local_inference",
            "hf_model": "Qwen/Qwen3-Coder-30B-A3B-Instruct",
        }
    }

    config = ScienceProjectConfig.model_validate(payload)

    assert config.execution.mode == "self-deployed"
    assert config.execution.hf_model == "Qwen/Qwen3-Coder-30B-A3B-Instruct"
    assert config.execution.codex_model is None
    assert config.execution.reasoning_effort is None
    assert config.evolution.targets["parametric_memory"].enabled is False


def test_managed_local_inference_round_trips_through_json_model_dump() -> None:
    payload = _minimal_payload() | {
        "execution": {
            "mode": "codex_managed_local_inference",
            "hf_model": "Qwen/Qwen3-Coder-30B-A3B-Instruct",
        }
    }
    config = ScienceProjectConfig.model_validate(payload)

    dumped = config.model_dump(mode="json")
    assert "codex_model" not in dumped["execution"]

    round_tripped = ScienceProjectConfig.model_validate(dumped)

    assert round_tripped == config
    assert round_tripped.execution.codex_model is None


def test_managed_local_inference_rejects_explicit_codex_model() -> None:
    payload = _minimal_payload() | {
        "execution": {
            "mode": "codex_managed_local_inference",
            "codex_model": "gpt-5.1-codex-mini",
            "hf_model": "Qwen/Qwen3-Coder-30B-A3B-Instruct",
        }
    }

    with pytest.raises(
        ValidationError,
        match="execution.codex_model is only valid for subscription transcript mode",
    ):
        ScienceProjectConfig.model_validate(payload)


def test_managed_local_inference_rejects_explicit_null_codex_model() -> None:
    payload = _minimal_payload() | {
        "execution": {
            "mode": "codex_managed_local_inference",
            "codex_model": None,
            "hf_model": "Qwen/Qwen3-Coder-30B-A3B-Instruct",
        }
    }

    with pytest.raises(
        ValidationError,
        match="execution.codex_model is only valid for subscription transcript mode",
    ):
        ScienceProjectConfig.model_validate(payload)


def test_subscription_accepts_reasoning_effort() -> None:
    payload = _minimal_payload() | {
        "execution": {
            "mode": "codex_subscription_transcript",
            "codex_model": "gpt-5.3-codex-spark",
            "reasoning_effort": "xhigh",
        }
    }

    config = ScienceProjectConfig.model_validate(payload)

    assert config.execution.reasoning_effort == "xhigh"


@pytest.mark.parametrize(
    "codex_model",
    [
        "gpt-5",
        "openai/gpt-5",
        "anthropic/gpt-5",
        "google/gpt-5",
        "gcp/google/gpt-5",
    ],
)
def test_subscription_rejects_unsupported_gpt5_codex_model(codex_model: str) -> None:
    payload = _minimal_payload() | {
        "execution": {
            "mode": "codex_subscription_transcript",
            "codex_model": codex_model,
        }
    }

    with pytest.raises(ValidationError, match="bare gpt-5 is unsupported"):
        ScienceProjectConfig.model_validate(payload)


def test_managed_local_inference_rejects_reasoning_effort() -> None:
    payload = _minimal_payload() | {
        "execution": {
            "mode": "self-deployed",
            "hf_model": "Qwen/Qwen3-Coder-30B-A3B-Instruct",
            "reasoning_effort": "high",
        }
    }

    with pytest.raises(
        ValidationError,
        match="execution.reasoning_effort is only valid for subscription",
    ):
        ScienceProjectConfig.model_validate(payload)


def test_custom_image_profile_requires_custom_image() -> None:
    payload = _minimal_payload() | {"environment": {"profile": "custom_image"}}

    with pytest.raises(ValidationError, match="custom_image"):
        ScienceProjectConfig.model_validate(payload)


def test_custom_image_is_invalid_for_non_custom_image_profile() -> None:
    payload = _minimal_payload() | {
        "environment": {
            "profile": "managed_science",
            "custom_image": "ghcr.io/example/science:latest",
        }
    }

    with pytest.raises(ValidationError, match="custom_image"):
        ScienceProjectConfig.model_validate(payload)


def test_subscription_mode_rejects_parametric_memory() -> None:
    payload = _minimal_payload() | {
        "evolution": {
            "targets": {
                "parametric_memory": {
                    "enabled": True,
                    "method": "parametric_memory_register",
                }
            }
        }
    }

    with pytest.raises(
        ValidationError,
        match="Science Projects do not support parametric_memory yet",
    ):
        ScienceProjectConfig.model_validate(payload)


def test_managed_local_inference_rejects_parametric_memory() -> None:
    payload = _minimal_payload() | {
        "execution": {
            "mode": "codex_managed_local_inference",
            "hf_model": "Qwen/Qwen3-Coder-30B-A3B-Instruct",
        },
        "evolution": {
            "targets": {
                "parametric_memory": {
                    "enabled": True,
                    "method": "parametric_memory_register",
                }
            }
        },
    }

    with pytest.raises(
        ValidationError,
        match="Science Projects do not support parametric_memory yet",
    ):
        ScienceProjectConfig.model_validate(payload)


def test_science_targets_defer_unknown_ids_and_retain_disabled_drafts() -> None:
    payload = _minimal_payload() | {
        "evolution": {
            "targets": {
                "future_target": {
                    "enabled": False,
                    "method": "future_method",
                    "config": {"nested": ["draft"]},
                }
            }
        }
    }
    config = ScienceProjectConfig.model_validate(payload)
    payload["evolution"]["targets"]["future_target"]["config"]["nested"].append(
        "changed"
    )

    assert config.evolution.targets["future_target"].model_dump(mode="json") == {
        "enabled": False,
        "method": "future_method",
        "config": {"nested": ["draft"]},
    }
    assert ScienceProjectConfig.model_validate(config.model_dump(mode="json")) == config
    assert ScienceProjectConfig.model_validate(
        config.model_dump(round_trip=True)
    ) == config
    with pytest.raises(TypeError):
        config.evolution.targets["mutated_target"] = config.evolution.targets[
            "future_target"
        ]
    assert deepcopy(config) == config
    assert config.model_copy(deep=True) == config
    assert EvolutionTargetsConfig(targets=config.evolution.targets) == config.evolution
    with pytest.raises(ValidationError, match="enabled target requires method"):
        config.evolution.model_copy(
            update={"targets": {"bad_target": {"enabled": True}}}
        )


def test_science_evolution_target_map_schema_has_typed_values() -> None:
    for mode in ("validation", "serialization"):
        schema = ScienceProjectConfig.model_json_schema(mode=mode)
        targets = schema["$defs"]["EvolutionTargetsConfig"]["properties"]["targets"]
        value_schema = targets["additionalProperties"]
        assert value_schema["$ref"].endswith("/ProjectEvolutionTargetSelection")


def test_science_rejects_old_boolean_evolution_schema() -> None:
    payload = _minimal_payload() | {"evolution": {"text_memory": True}}

    with pytest.raises(ValidationError, match="text_memory"):
        ScienceProjectConfig.model_validate(payload)


def test_science_target_map_is_closed_and_keys_use_stable_ids() -> None:
    extra = _minimal_payload() | {
        "evolution": {
            "targets": {"future_target": {"enabled": False, "unexpected": True}}
        }
    }
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        ScienceProjectConfig.model_validate(extra)

    invalid_key = _minimal_payload() | {
        "evolution": {"targets": {"bad/target": {"enabled": False}}}
    }
    with pytest.raises(ValidationError, match="stable identifier"):
        ScienceProjectConfig.model_validate(invalid_key)

    coerced_key = _minimal_payload() | {
        "evolution": {"targets": {b"text_memory": {"enabled": False}}}
    }
    with pytest.raises(ValidationError, match="target IDs must be strings"):
        ScienceProjectConfig.model_validate(coerced_key)


def test_load_science_project_config_reads_yaml_and_sets_path(tmp_path: Path) -> None:
    path = _write_yaml(
        tmp_path / "science.yaml",
        _minimal_payload()
        | {
            "task": {
                "id": "repo-task",
                "objective": "Run the repository workflow.",
                "source": {
                    "type": "git_repository",
                    "url": "https://github.com/example/science.git",
                    "branch": "main",
                },
                "setup_commands": ["  pip install -e .  "],
            }
        },
    )

    config = load_science_project_config(path)

    assert config.path == path
    assert config.task.source.type == "git_repository"
    assert config.task.source.url == "https://github.com/example/science.git"
    assert config.task.source.branch == "main"
    assert config.task.setup_commands == ["pip install -e ."]


@pytest.mark.parametrize("source_type", ["remote_path", "local_folder"])
def test_path_based_sources_require_path(source_type: str) -> None:
    payload = _minimal_payload() | {
        "task": {
            "id": "source-task",
            "objective": "Load task source.",
            "source": {"type": source_type},
        }
    }

    with pytest.raises(ValidationError, match="requires path"):
        ScienceProjectConfig.model_validate(payload)


def test_git_repository_requires_url() -> None:
    payload = _minimal_payload() | {
        "task": {
            "id": "source-task",
            "objective": "Load task source.",
            "source": {"type": "git_repository"},
        }
    }

    with pytest.raises(ValidationError, match="requires url"):
        ScienceProjectConfig.model_validate(payload)


def test_git_repository_accepts_optional_path_and_branch() -> None:
    payload = _minimal_payload() | {
        "task": {
            "id": "source-task",
            "objective": "Load task source.",
            "source": {
                "type": "git_repository",
                "url": "https://github.com/example/science.git",
                "path": "benchmarks/task-1",
                "branch": "main",
            },
        }
    }

    config = ScienceProjectConfig.model_validate(payload)

    assert config.task.source.url == "https://github.com/example/science.git"
    assert config.task.source.path == "benchmarks/task-1"
    assert config.task.source.branch == "main"


@pytest.mark.parametrize(
    "extra_source",
    [
        {"path": "tasks/local"},
        {"url": "https://example.com/task.tar.gz"},
        {"branch": "main"},
    ],
)
def test_scratch_rejects_path_url_or_branch(extra_source: dict[str, str]) -> None:
    payload = _minimal_payload() | {
        "task": {
            "id": "source-task",
            "objective": "Load task source.",
            "source": {"type": "scratch"} | extra_source,
        }
    }

    with pytest.raises(ValidationError, match="scratch"):
        ScienceProjectConfig.model_validate(payload)


def test_task_id_rejects_slash() -> None:
    payload = _minimal_payload() | {
        "task": {
            "id": "bad/task",
            "objective": "Run task.",
        }
    }

    with pytest.raises(ValidationError, match="task.id"):
        ScienceProjectConfig.model_validate(payload)


@pytest.mark.parametrize("invalid_command", ["", "  "])
def test_setup_commands_strip_valid_commands_and_reject_empty_commands(
    invalid_command: str,
) -> None:
    payload = _minimal_payload() | {
        "task": {
            "id": "setup-task",
            "objective": "Run setup.",
            "setup_commands": ["  pip install -e .  ", "pytest -q"],
        }
    }

    config = ScienceProjectConfig.model_validate(payload)

    assert config.task.setup_commands == ["pip install -e .", "pytest -q"]

    payload["task"]["setup_commands"] = [invalid_command]
    with pytest.raises(ValidationError, match="setup_commands"):
        ScienceProjectConfig.model_validate(payload)


def test_unknown_extra_top_level_key_is_rejected() -> None:
    payload = _minimal_payload() | {"unexpected": True}

    with pytest.raises(ValidationError, match="unexpected"):
        ScienceProjectConfig.model_validate(payload)


@pytest.mark.parametrize(
    "field_path",
    [
        ("project", "name"),
        ("task", "objective"),
    ],
)
@pytest.mark.parametrize("value", ["", "   "])
def test_required_project_name_and_task_objective_reject_empty_text(
    field_path: tuple[str, str],
    value: str,
) -> None:
    payload = _minimal_payload()
    section, key = field_path
    payload[section][key] = value

    with pytest.raises(ValidationError, match=key):
        ScienceProjectConfig.model_validate(payload)
