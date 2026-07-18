from __future__ import annotations

import pytest

from openevo import experiments
from openevo.evolution.framework import EvolutionExecutionProfile
from openevo.evolution.framework.builtins import (
    ImplementationDistributionIdentity,
    build_builtin_registry,
)
from openevo.projects.science import PreparedWorkspace, ScienceProjectConfig, compile_science_project
from openevo.runtime.managed import (
    MANAGED_HOME,
    MANAGED_PATH,
    MANAGED_RUNTIME_RELEASES,
    MANAGED_SUBSCRIPTION_PREPARE_COMMAND,
)

_compile_experiment = experiments.compile_experiment
_REGISTRY_SNAPSHOT = build_builtin_registry(
    ImplementationDistributionIdentity(
        distribution="openevo",
        distribution_version="0.1.0",
        distribution_digest="a" * 64,
    )
)
_EXECUTION_PROFILE = EvolutionExecutionProfile(
    execution_mode="subscription",
    capture_mode="transcript",
    harness_id="codex",
)


def compile_experiment(config, *args, **kwargs):
    return _compile_experiment(
        config,
        *args,
        registry_snapshot=_REGISTRY_SNAPSHOT,
        execution_profile=_EXECUTION_PROFILE,
        **kwargs,
    )


def _project(**overrides: object) -> ScienceProjectConfig:
    payload = {
        "version": 1,
        "project": {"name": "protein-design"},
        "remote_profile": "science-team",
        "task": {
            "id": "folding-baseline",
            "objective": "Improve the folding baseline.",
            "source": {
                "type": "remote_path",
                "path": "/datasets/folding-baseline",
            },
            "setup_commands": ["python -m pip install -e ."],
            "metadata": {"domain": "protein"},
        },
    }
    payload.update(overrides)
    return ScienceProjectConfig.model_validate(payload)


def test_subscription_project_compiles_to_transcript_experiment_config() -> None:
    compiled = compile_science_project(_project())

    assert compiled.experiment.name == "protein-design"
    assert compiled.agent.preset == "codex"
    assert compiled.agent.model == "gpt-5.5"
    assert compiled.agent.auth == "subscription"
    assert compiled.agent.settings == {
        "auth_mode": "subscription",
        "capture_mode": "transcript",
    }
    assert compiled.agent.env == {}
    assert compiled.runtime.profile == "managed_science"
    assert (
        compiled.runtime.image
        == MANAGED_RUNTIME_RELEASES["managed_science"].immutable_reference
    )
    assert compiled.runtime.workdir == "/openevo/session/workspace"
    assert compiled.runtime.container_user == "host"
    assert compiled.runtime.env == {
        "HOME": MANAGED_HOME,
        "PATH": MANAGED_PATH,
    }
    assert [action.model_dump(mode="json") for action in compiled.runtime.prepare] == [
        {
            "type": "exec",
            "command": MANAGED_SUBSCRIPTION_PREPARE_COMMAND,
            "cwd": None,
            "env": None,
            "source": None,
            "target": None,
        },
        {
            "type": "exec",
            "command": "python -m pip install -e .",
            "cwd": "/openevo/session/workspace",
            "env": None,
            "source": None,
            "target": None,
        }
    ]
    assert len(compiled.tasks) == 1
    task = compiled.tasks[0]
    assert task.id == "folding-baseline"
    assert task.instruction == "Improve the folding baseline."
    assert task.workspace == "/datasets/folding-baseline"
    assert task.metadata == {
        "domain": "protein",
        "openevo": {
            "project_name": "protein-design",
            "remote_profile": "science-team",
            "source_type": "remote_path",
            "environment_profile": "managed_science",
            "execution_mode": "codex_subscription_transcript",
        },
    }
    assert compiled.evolution.targets == _project().evolution.targets
    assert not hasattr(compiled, "artifacts")


def test_local_inference_compiles_to_transcript_proxy_auth_and_hf_model_metadata_env() -> None:
    compiled = compile_science_project(
        _project(
            execution={
                "mode": "codex_managed_local_inference",
                "hf_model": "Qwen/Qwen3-Coder-30B-A3B-Instruct",
            },
            environment={"env": {"SCIENCE_DATASET": "folding"}},
        )
    )

    assert compiled.agent.preset == "codex"
    assert compiled.agent.model == "Qwen/Qwen3-Coder-30B-A3B-Instruct"
    assert compiled.agent.auth == "proxy"
    assert compiled.agent.provider == "codex_cli"
    assert compiled.agent.settings == {
        "auth_mode": "proxy",
        "capture_mode": "transcript",
    }
    assert compiled.runtime.env == {
        "SCIENCE_DATASET": "folding",
        "OPENEVO_MANAGED_HF_MODEL": "Qwen/Qwen3-Coder-30B-A3B-Instruct",
        "HOME": MANAGED_HOME,
        "PATH": MANAGED_PATH,
    }
    assert compiled.agent.env["CODEX_HOME"] == "/openevo/session/home/.codex"
    assert compiled.runtime.container_user == "host"
    assert compiled.tasks[0].metadata["openevo"]["execution_mode"] == (
        "self-deployed"
    )
    assert compiled.evolution.targets["parametric_memory"].enabled is False


def test_custom_runtime_image_does_not_override_the_image_user() -> None:
    compiled = compile_science_project(
        _project(
            environment={
                "profile": "custom_image",
                "custom_image": "ghcr.io/example/science:latest",
            },
            execution={"mode": "self-deployed", "hf_model": "Qwen/Qwen3-8B"},
        )
    )

    assert compiled.runtime.image == "ghcr.io/example/science:latest"
    assert compiled.runtime.profile is None
    assert compiled.runtime.container_user == "image"
    assert "HOME" not in compiled.runtime.env
    assert "PATH" not in compiled.runtime.env
    assert compiled.agent.env == {}
    assert compiled.runtime.prepare[0].command == "mkdir -p /openevo/session/workspace"


def test_science_compiler_preserves_generic_targets_without_loss() -> None:
    project = _project(
        evolution={
            "targets": {
                "future_target": {
                    "enabled": False,
                    "method": "future_method",
                    "config": {"nested": {"values": [1, 2]}},
                },
                "agent_system": {
                    "enabled": True,
                    "method": "auto",
                    "config": {"target_path": "CLAUDE.md"},
                },
            }
        }
    )

    compiled = compile_science_project(project)

    assert compiled.evolution.targets == project.evolution.targets
    assert compiled.evolution.model_dump(mode="json")["targets"] == (
        project.evolution.model_dump(mode="json")["targets"]
    )
    assert compiled.evolution.rounds == 1
    assert compiled.evolution.backend_url == "http://127.0.0.1:8200"


def test_science_text_target_cannot_smuggle_parametric_method() -> None:
    experiment = compile_science_project(
        _project(
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
            }
        )
    )

    with pytest.raises(ValueError, match="does not belong to target 'text_memory'"):
        compile_experiment(experiment)


def test_self_deployed_science_compile_uses_codex_reflector_llm() -> None:
    project = ScienceProjectConfig.model_validate(
        {
            "version": 1,
            "project": {"name": "protein-design"},
            "remote_profile": "science-team",
            "task": {
                "id": "folding-baseline",
                "objective": "Improve the folding baseline.",
                "source": {
                    "type": "remote_path",
                    "path": "/datasets/folding-baseline",
                },
            },
            "execution": {
                "mode": "self-deployed",
                "hf_model": "Qwen/Qwen3-Coder-30B-A3B-Instruct",
            },
        }
    )

    experiment = compile_science_project(project)

    assert experiment.agent.auth == "proxy"
    assert experiment.agent.provider == "codex_cli"


def test_task_metadata_merges_existing_openevo_dict_with_compiler_keys_winning() -> None:
    compiled = compile_science_project(
        _project(
            task={
                "id": "folding-baseline",
                "objective": "Improve the folding baseline.",
                "source": {
                    "type": "remote_path",
                    "path": "/datasets/folding-baseline",
                },
                "metadata": {
                    "domain": "protein",
                    "openevo": {
                        "sidecar_run_id": "abc",
                        "project_name": "wrong",
                    },
                },
            }
        )
    )

    openevo_metadata = compiled.tasks[0].metadata["openevo"]
    assert openevo_metadata["sidecar_run_id"] == "abc"
    assert openevo_metadata["project_name"] == "protein-design"


def test_local_folder_requires_prepared_workspace_mapping() -> None:
    project = _project(
        task={
            "id": "local-task",
            "objective": "Run local workflow.",
            "source": {
                "type": "local_folder",
                "path": "workflows/local-task",
            },
        }
    )

    with pytest.raises(ValueError, match="prepared workspace is required"):
        compile_science_project(project)


def test_local_folder_uses_prepared_workspace_mapping_and_source_fingerprint() -> None:
    project = _project(
        task={
            "id": "local-task",
            "objective": "Run local workflow.",
            "source": {
                "type": "local_folder",
                "path": "workflows/local-task",
            },
        }
    )

    compiled = compile_science_project(
        project,
        prepared_workspaces={
            "local-task": PreparedWorkspace(
                path="/tmp/prepared/local-task",
                source_fingerprint="sha256:abc123",
            )
        },
    )

    assert compiled.tasks[0].workspace == "/tmp/prepared/local-task"
    assert compiled.tasks[0].metadata["openevo"]["source_fingerprint"] == (
        "sha256:abc123"
    )


def test_custom_image_profile_controls_runtime_image() -> None:
    compiled = compile_science_project(
        _project(
            environment={
                "profile": "custom_image",
                "custom_image": "ghcr.io/example/science:latest",
            },
            execution={"mode": "self-deployed", "hf_model": "Qwen/Qwen3-8B"},
        )
    )

    assert compiled.runtime.image == "ghcr.io/example/science:latest"


def test_scratch_source_has_no_workspace_and_keeps_runtime_and_setup_commands() -> None:
    compiled = compile_science_project(
        _project(
            task={
                "id": "scratch-task",
                "objective": "Create a new experiment.",
                "source": {"type": "scratch"},
                "setup_commands": ["python -m pip install numpy"],
            },
            environment={"profile": "python_research"},
        )
    )

    assert (
        compiled.runtime.image
        == MANAGED_RUNTIME_RELEASES["python_research"].immutable_reference
    )
    assert compiled.runtime.container_user == "host"
    assert compiled.tasks[0].workspace is None
    assert [action.model_dump(mode="json") for action in compiled.runtime.prepare] == [
        {
            "type": "exec",
            "command": MANAGED_SUBSCRIPTION_PREPARE_COMMAND,
            "cwd": None,
            "env": None,
            "source": None,
            "target": None,
        },
        {
            "type": "exec",
            "command": "python -m pip install numpy",
            "cwd": "/openevo/session/workspace",
            "env": None,
            "source": None,
            "target": None,
        }
    ]


def test_experiment_compiler_uploads_workspace_before_science_prepare_actions() -> None:
    experiment = compile_science_project(_project())
    compiled = compile_experiment(experiment)

    payload = compiled.tasks[0].rollout_payload_for_round(0, context_artifact_ids=[])

    assert payload["runtime"]["container_user"] == "host"
    assert payload["runtime"]["prepare"] == [
        {
            "type": "upload_dir",
            "source": "/datasets/folding-baseline",
            "target": "/openevo/session/workspace",
        },
        {
            "type": "exec",
            "source": None,
            "target": None,
            "command": MANAGED_SUBSCRIPTION_PREPARE_COMMAND,
            "cwd": None,
            "env": None,
        },
        {
            "type": "exec",
            "source": None,
            "target": None,
            "command": "python -m pip install -e .",
            "cwd": "/openevo/session/workspace",
            "env": None,
        },
    ]
