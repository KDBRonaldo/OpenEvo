from __future__ import annotations

from copy import deepcopy
from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from openevo import experiments
from openevo.runtime.managed import MANAGED_CODEX_HOME, MANAGED_RUNTIME_RELEASES

ExperimentConfig = experiments.ExperimentConfig
load_experiment_config = experiments.load_experiment_config


def _write_yaml(path: Path, payload: dict) -> Path:
    path.write_text(yaml.safe_dump(payload), encoding="utf-8")
    return path


def _minimal_payload() -> dict:
    return {
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


def test_minimal_yaml_loads_with_defaults(tmp_path: Path) -> None:
    config = load_experiment_config(
        _write_yaml(tmp_path / "experiment.yaml", _minimal_payload())
    )

    assert config.version == 1
    assert config.experiment.name == "biology-components"
    assert config.agent.preset == "codex"
    assert config.agent.model == "gpt-5.1-codex-mini"
    assert config.agent.auth == "proxy"
    assert config.runtime.kind == "docker"
    assert config.runtime.image == "runtime:latest"
    assert config.runtime.workdir == "/openevo/session/workspace"
    assert config.rollout.url == "http://127.0.0.1:8080"
    assert config.evolution.backend_url == "http://127.0.0.1:8200"
    assert config.evolution.rounds == 1
    assert config.evolution.worker.mode == "local_once"
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
    assert not hasattr(config, "artifacts")
    assert config.tasks[0].id == "component-extraction-train"


def test_environment_url_defaults_are_honored(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OPENEVO_ROLLOUT_URL", "http://rollout.example:8080/")
    monkeypatch.setenv("OPENEVO_EVOLUTION_URL", "https://evolution.example/")

    config = load_experiment_config(
        _write_yaml(tmp_path / "experiment.yaml", _minimal_payload())
    )

    assert config.rollout.url == "http://rollout.example:8080"
    assert config.evolution.backend_url == "https://evolution.example"


def test_rounds_must_be_at_least_one() -> None:
    payload = _minimal_payload() | {"evolution": {"rounds": 0}}

    with pytest.raises(ValidationError, match="greater than or equal to 1"):
        ExperimentConfig.model_validate(payload)


def test_workspace_requires_runtime_image() -> None:
    payload = _minimal_payload()
    payload.pop("runtime")

    with pytest.raises(ValidationError, match="runtime.image is required"):
        ExperimentConfig.model_validate(payload)


def test_workspace_is_optional_for_default_runtime_tasks() -> None:
    payload = _minimal_payload()
    payload.pop("runtime")
    payload["tasks"][0].pop("workspace")

    config = ExperimentConfig.model_validate(payload)

    assert config.tasks[0].workspace is None
    assert config.runtime.image is None


def test_host_container_user_requires_explicit_runtime_image() -> None:
    payload = _minimal_payload()
    payload["tasks"][0].pop("workspace")
    payload["runtime"] = {"container_user": "host"}

    with pytest.raises(ValidationError, match="runtime.image is required"):
        ExperimentConfig.model_validate(payload)


def test_subscription_agents_cannot_disable_transcript_capture() -> None:
    payload = _minimal_payload()
    payload["agent"] = {
        "preset": "codex",
        "model": "gpt-5.1-codex-mini",
        "auth": "subscription",
        "settings": {"capture_mode": "none"},
    }

    with pytest.raises(ValidationError, match="subscription agents require transcript"):
        ExperimentConfig.model_validate(payload)


def test_subscription_agents_must_set_capture_mode_explicitly() -> None:
    payload = _minimal_payload()
    payload["agent"] = {
        "preset": "codex",
        "model": "gpt-5.1-codex-mini",
        "auth": "subscription",
    }

    with pytest.raises(ValidationError, match="subscription agents require transcript"):
        ExperimentConfig.model_validate(payload)


@pytest.mark.parametrize("auth", ["proxy", "subscription"])
@pytest.mark.parametrize("capture_mode", ["transcript", "agent_transcript", "pure_text"])
def test_agents_normalize_transcript_capture_aliases(
    auth: str,
    capture_mode: str,
) -> None:
    payload = _minimal_payload()
    payload["agent"] = {
        "preset": "codex",
        "model": "gpt-5.1-codex-mini",
        "auth": auth,
        "settings": {"capture_mode": capture_mode},
    }
    payload["runtime"] = {
        "profile": "managed_science",
        "image": MANAGED_RUNTIME_RELEASES["managed_science"].immutable_reference,
        "container_user": "host",
    }

    config = ExperimentConfig.model_validate(payload)

    assert config.agent.settings["capture_mode"] == "transcript"


def test_subscription_agent_rejects_image_user_runtime() -> None:
    payload = _minimal_payload()
    payload["agent"] = {
        "preset": "codex",
        "model": "gpt-5.1-codex-mini",
        "auth": "subscription",
        "settings": {"capture_mode": "transcript"},
    }
    payload["runtime"] = {
        "profile": "managed_science",
        "image": MANAGED_RUNTIME_RELEASES["managed_science"].immutable_reference,
        "container_user": "image",
    }

    with pytest.raises(
        ValidationError,
        match="Core-managed runtime profiles require runtime.container_user='host'",
    ):
        ExperimentConfig.model_validate(payload)


def test_subscription_agent_requires_exact_managed_runtime_profile_and_image() -> None:
    payload = _minimal_payload()
    payload["agent"] = {
        "preset": "codex",
        "model": "gpt-5.1-codex-mini",
        "auth": "subscription",
        "settings": {"capture_mode": "transcript"},
    }
    payload["runtime"]["container_user"] = "host"

    with pytest.raises(ValidationError, match="managed runtime profile"):
        ExperimentConfig.model_validate(payload)

    payload["runtime"]["profile"] = "managed_science"
    with pytest.raises(ValidationError, match="exact Core-managed image"):
        ExperimentConfig.model_validate(payload)

    payload["runtime"]["image"] = MANAGED_RUNTIME_RELEASES[
        "managed_science"
    ].immutable_reference
    config = ExperimentConfig.model_validate(payload)

    assert config.runtime.profile == "managed_science"


@pytest.mark.parametrize(
    ("override", "message"),
    [
        ({"kind": "apptainer"}, "Docker"),
        ({"image": "attacker:latest"}, "exact Core-managed image"),
        ({"container_user": "image"}, "container_user='host'"),
    ],
)
def test_self_deployed_managed_science_requires_exact_runtime_binding(
    override: dict[str, str],
    message: str,
) -> None:
    payload = _minimal_payload()
    payload["runtime"] = {
        "kind": "docker",
        "profile": "managed_science",
        "image": MANAGED_RUNTIME_RELEASES["managed_science"].immutable_reference,
        "container_user": "host",
        **override,
    }

    with pytest.raises(ValidationError, match=message):
        ExperimentConfig.model_validate(payload)


def test_self_deployed_managed_science_accepts_exact_runtime_binding() -> None:
    payload = _minimal_payload()
    payload["runtime"] = {
        "kind": "docker",
        "profile": "managed_science",
        "image": MANAGED_RUNTIME_RELEASES["managed_science"].immutable_reference,
        "container_user": "host",
    }

    config = ExperimentConfig.model_validate(payload)

    assert config.agent.auth == "proxy"
    assert config.runtime.profile == "managed_science"


@pytest.mark.parametrize(
    "codex_home",
    [
        "/openevo/session/workspace/.codex",
        "/openevo/session/artifacts/.codex",
        "/openevo/session/logs/.codex",
        MANAGED_CODEX_HOME,
    ],
)
def test_subscription_agent_cannot_supply_codex_home(codex_home: str) -> None:
    payload = _minimal_payload()
    payload["agent"] = {
        "preset": "codex",
        "model": "gpt-5.1-codex-mini",
        "auth": "subscription",
        "settings": {"capture_mode": "transcript"},
        "env": {"CODEX_HOME": codex_home},
    }
    payload["runtime"] = {
        "profile": "managed_science",
        "image": MANAGED_RUNTIME_RELEASES["managed_science"].immutable_reference,
        "container_user": "host",
    }

    with pytest.raises(ValidationError, match="CODEX_HOME is Core-owned"):
        ExperimentConfig.model_validate(payload)


@pytest.mark.parametrize("env_name", ["HOME", "PATH", "CODEX_HOME"])
@pytest.mark.parametrize("env_owner", ["agent", "runtime", "prepare"])
def test_subscription_closed_environment_rejects_caller_overrides(
    env_name: str,
    env_owner: str,
) -> None:
    payload = _minimal_payload()
    payload["agent"] = {
        "preset": "codex",
        "model": "gpt-5.5",
        "auth": "subscription",
        "settings": {"capture_mode": "transcript"},
    }
    payload["runtime"] = {
        "profile": "managed_science",
        "image": MANAGED_RUNTIME_RELEASES["managed_science"].immutable_reference,
        "container_user": "host",
    }
    if env_owner == "agent":
        payload["agent"]["env"] = {env_name: "/attacker"}
    elif env_owner == "runtime":
        payload["runtime"]["env"] = {env_name: "/attacker"}
    else:
        payload["runtime"]["prepare"] = [
            {"type": "exec", "command": "true", "env": {env_name: "/attacker"}}
        ]

    with pytest.raises(ValidationError, match=f"{env_name} is Core-owned"):
        ExperimentConfig.model_validate(payload)


def test_subscription_agents_cannot_enable_parametric_memory() -> None:
    payload = _minimal_payload()
    payload["agent"] = {
        "preset": "codex",
        "model": "gpt-5.1-codex-mini",
        "auth": "subscription",
        "settings": {"capture_mode": "transcript"},
    }
    payload["runtime"] = {
        "profile": "managed_science",
        "image": MANAGED_RUNTIME_RELEASES["managed_science"].immutable_reference,
        "container_user": "host",
    }
    payload["evolution"] = {
        "targets": {
            "parametric_memory": {
                "enabled": True,
                "method": "parametric_memory_register",
                "config": {"adapter_uri": "file:///adapters/parser-memory"},
            }
        }
    }

    with pytest.raises(ValidationError, match="parametric_memory requires proxy"):
        ExperimentConfig.model_validate(payload)


def test_experiment_rejects_removed_artifacts_schema() -> None:
    payload = _minimal_payload() | {"artifacts": {}}

    with pytest.raises(ValidationError, match="artifacts"):
        ExperimentConfig.model_validate(payload)


def test_evolution_targets_are_generic_closed_and_round_trip_without_aliases() -> None:
    payload = _minimal_payload() | {
        "evolution": {
            "rounds": 3,
            "targets": {
                "future_target": {
                    "enabled": False,
                    "method": "future_method",
                    "config": {"nested": {"weights": [1, 2]}},
                }
            },
        }
    }
    config = ExperimentConfig.model_validate(payload)
    payload["evolution"]["targets"]["future_target"]["config"]["nested"][
        "weights"
    ].append(3)

    target = config.evolution.targets["future_target"]
    assert target.enabled is False
    assert target.method == "future_method"
    assert target.config == {"nested": {"weights": [1, 2]}}
    dumped = config.model_dump(mode="json")
    assert "artifacts" not in dumped
    assert ExperimentConfig.model_validate(dumped) == config
    assert ExperimentConfig.model_validate(config.model_dump(round_trip=True)) == config

    invalid = _minimal_payload() | {
        "evolution": {"targets": {"text_memory": {"enabled": False, "extra": 1}}}
    }
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        ExperimentConfig.model_validate(invalid)

    with pytest.raises(TypeError):
        config.evolution.targets["mutated_target"] = target

    assert deepcopy(config) == config
    assert config.model_copy(deep=True) == config
    rebuilt = type(config.evolution)(targets=config.evolution.targets)
    assert rebuilt.targets == config.evolution.targets
    assert config.evolution.model_dump(
        include={"targets": {"future_target"}}
    ) == {"targets": {"future_target": target.model_dump(mode="python")}}
    with pytest.raises(ValidationError, match="enabled target requires method"):
        config.evolution.model_copy(
            update={"targets": {"bad_target": {"enabled": True}}}
        )


def test_experiment_evolution_target_map_schema_has_typed_values() -> None:
    for mode in ("validation", "serialization"):
        schema = ExperimentConfig.model_json_schema(mode=mode)
        targets = schema["$defs"]["EvolutionConfig"]["properties"]["targets"]
        value_schema = targets["additionalProperties"]
        assert value_schema["$ref"].endswith("/ProjectEvolutionTargetSelection")


@pytest.mark.parametrize("target_id", ["bad/target", " spaced", "method:target"])
def test_experiment_evolution_target_keys_must_be_stable_ids(target_id: str) -> None:
    payload = _minimal_payload() | {
        "evolution": {"targets": {target_id: {"enabled": False}}}
    }

    with pytest.raises(ValidationError, match="stable identifier"):
        ExperimentConfig.model_validate(payload)


def test_experiment_evolution_target_keys_do_not_coerce_bytes() -> None:
    payload = _minimal_payload() | {
        "evolution": {"targets": {b"text_memory": {"enabled": False}}}
    }

    with pytest.raises(ValidationError, match="target IDs must be strings"):
        ExperimentConfig.model_validate(payload)


def test_agent_settings_auth_mode_must_match_agent_auth() -> None:
    payload = _minimal_payload()
    payload["agent"] = {
        "preset": "codex",
        "model": "gpt-5.1-codex-mini",
        "auth": "proxy",
        "settings": {"auth_mode": "subscription", "capture_mode": "transcript"},
    }

    with pytest.raises(ValidationError, match="agent\\.settings\\.auth_mode"):
        ExperimentConfig.model_validate(payload)


def test_runtime_overrides_without_image_are_rejected_for_default_runtime_tasks() -> None:
    payload = _minimal_payload()
    payload["runtime"] = {"env": {"TOKEN": "abc"}}
    payload["tasks"][0].pop("workspace")

    with pytest.raises(ValidationError, match="runtime.image is required"):
        ExperimentConfig.model_validate(payload)


def test_task_ids_must_be_unique() -> None:
    payload = _minimal_payload()
    payload["tasks"] = [
        {"id": "task-a", "instruction": "Do A.", "workspace": "/tmp/a"},
        {"id": "task-a", "instruction": "Do A again.", "workspace": "/tmp/b"},
    ]

    with pytest.raises(ValidationError, match="tasks\\[\\]\\.id values must be unique"):
        ExperimentConfig.model_validate(payload)


def test_task_ids_must_be_url_path_segments() -> None:
    payload = _minimal_payload()
    payload["tasks"][0]["id"] = "bench/foo"

    with pytest.raises(ValidationError, match="tasks\\[\\]\\.id must not contain '/'"):
        ExperimentConfig.model_validate(payload)


def test_unknown_top_level_keys_are_rejected() -> None:
    payload = _minimal_payload() | {"unexpected": True}

    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        ExperimentConfig.model_validate(payload)


def test_load_experiment_config_requires_top_level_mapping(tmp_path: Path) -> None:
    path = tmp_path / "experiment.yaml"
    path.write_text("- not\n- a\n- mapping\n", encoding="utf-8")

    with pytest.raises(ValueError, match="top-level mapping"):
        load_experiment_config(path)


def test_runtime_prepare_accepts_exec_actions() -> None:
    payload = _minimal_payload()
    payload["runtime"]["prepare"] = [
        {
            "type": "exec",
            "command": "pip install -r requirements.txt",
            "cwd": "/openevo/session/workspace",
            "env": {"PIP_INDEX_URL": "https://pypi.example/simple"},
        }
    ]

    config = ExperimentConfig.model_validate(payload)

    [action] = config.runtime.prepare
    assert action.type == "exec"
    assert action.command == "pip install -r requirements.txt"
    assert action.cwd == "/openevo/session/workspace"
    assert action.env == {"PIP_INDEX_URL": "https://pypi.example/simple"}


def test_runtime_prepare_rejects_upload_without_target() -> None:
    payload = _minimal_payload()
    payload["runtime"]["prepare"] = [{"type": "upload_dir", "source": "/tmp/src"}]

    with pytest.raises(ValidationError, match="upload_dir requires source and target"):
        ExperimentConfig.model_validate(payload)
