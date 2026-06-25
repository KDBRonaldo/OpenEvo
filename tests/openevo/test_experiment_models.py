from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from openevo.experiment.models import ExperimentConfig, load_experiment_config


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
    assert config.runtime.workdir == "/polar/session/workspace"
    assert config.rollout.url == "http://127.0.0.1:8080"
    assert config.evolution.backend_url == "http://127.0.0.1:8200"
    assert config.evolution.rounds == 1
    assert config.evolution.worker.mode == "local_once"
    assert config.artifacts.text_memory.enabled is True
    assert config.artifacts.text_memory.method == "text_memory_reflector"
    assert config.artifacts.skill_bundle.enabled is True
    assert config.artifacts.skill_bundle.method == "skill_bundle_reflector"
    assert config.artifacts.agent_system.enabled is True
    assert config.artifacts.agent_system.method == "auto"
    assert config.artifacts.agent_system.target_path == "AGENTS.md"
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


@pytest.mark.parametrize("capture_mode", ["agent_transcript", "pure_text"])
def test_subscription_agents_accept_transcript_capture_aliases(capture_mode: str) -> None:
    payload = _minimal_payload()
    payload["agent"] = {
        "preset": "codex",
        "model": "gpt-5.1-codex-mini",
        "auth": "subscription",
        "settings": {"capture_mode": capture_mode},
    }

    config = ExperimentConfig.model_validate(payload)

    assert config.agent.settings["capture_mode"] == capture_mode


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
