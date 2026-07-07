from __future__ import annotations

from pathlib import Path

from pydantic import ValidationError
import pytest
import yaml

from openevo.sidecar.config import (
    DesktopProjectConfigDraft,
    build_desktop_project_configs,
    save_desktop_project_config,
)


VALID_DRAFT = {
    "project_name": "Protein Design",
    "task_id": "folding-baseline",
    "objective": "Improve the folding baseline.",
    "source_type": "remote_path",
    "source_path": "/datasets/folding-baseline",
    "remote_profile_id": "science-team",
    "remote_host": "gpu.example.edu",
    "remote_port": 22,
    "remote_user": "alice",
    "auth_method": "ssh_agent",
    "https_proxy": "http://127.0.0.1:7890",
    "huggingface_endpoint": "https://hf-mirror.com",
    "codex_model": "gpt-5.1-codex-mini",
    "text_memory": True,
    "skill_bundle": True,
    "agent_system": True,
}


def test_desktop_project_config_draft_builds_existing_models() -> None:
    draft = DesktopProjectConfigDraft.model_validate(VALID_DRAFT)

    project, profile = build_desktop_project_configs(draft)

    assert project.project.name == "Protein Design"
    assert project.remote_profile == "science-team"
    assert project.task.id == "folding-baseline"
    assert project.task.source.type == "remote_path"
    assert project.task.source.path == "/datasets/folding-baseline"
    assert project.execution.mode == "codex_subscription_transcript"
    assert project.execution.codex_model == "gpt-5.1-codex-mini"
    assert project.evolution.text_memory is True
    assert project.evolution.skill_bundle is True
    assert project.evolution.agent_system is True
    assert profile.id == "science-team"
    assert profile.host == "gpu.example.edu"
    assert profile.port == 22
    assert profile.user == "alice"
    assert profile.auth.method == "ssh_agent"
    assert profile.proxy.https_proxy == "http://127.0.0.1:7890"
    assert profile.proxy.huggingface_endpoint == "https://hf-mirror.com"


def test_save_desktop_project_config_writes_deterministic_yaml(tmp_path: Path) -> None:
    draft = DesktopProjectConfigDraft.model_validate(VALID_DRAFT)

    project, profile, paths = save_desktop_project_config(draft, tmp_path)

    assert project.path == paths.science_config_path
    assert profile.path == paths.remote_profile_path
    assert paths.science_config_path == (
        tmp_path / "projects" / "protein-design" / "science.yaml"
    )
    assert paths.remote_profile_path == tmp_path / "profiles" / "science-team.yaml"
    assert paths.science_config_path.is_file()
    assert paths.remote_profile_path.is_file()
    science_yaml = yaml.safe_load(paths.science_config_path.read_text(encoding="utf-8"))
    remote_yaml = yaml.safe_load(
        paths.remote_profile_path.read_text(encoding="utf-8")
    )
    assert science_yaml["project"]["name"] == "Protein Design"
    assert science_yaml["remote_profile"] == "science-team"
    assert science_yaml["task"]["source"]["path"] == "/datasets/folding-baseline"
    assert science_yaml["execution"]["mode"] == "codex_subscription_transcript"
    assert remote_yaml["id"] == "science-team"
    assert remote_yaml["proxy"]["https_proxy"] == "http://127.0.0.1:7890"
    assert "path" not in science_yaml
    assert "path" not in remote_yaml


def test_desktop_project_config_draft_rejects_raw_secret_fields() -> None:
    with pytest.raises(ValidationError, match="password"):
        DesktopProjectConfigDraft.model_validate(VALID_DRAFT | {"password": "secret"})


def test_desktop_project_config_draft_validates_source_requirements() -> None:
    with pytest.raises(ValidationError, match="source_url"):
        DesktopProjectConfigDraft.model_validate(
            (VALID_DRAFT | {"source_type": "git_repository"})
            | {"source_path": None}
        )
