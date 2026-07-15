from __future__ import annotations

from pathlib import Path

from pydantic import ValidationError
import pytest
import yaml

from desktop.sidecar.config import (
    DesktopProjectConfigDraft,
    build_desktop_project_configs,
    list_desktop_project_configs,
    load_desktop_project_config,
    save_desktop_project_config,
)


EVOLUTION_TARGETS = {
    "text_memory": {
        "enabled": True,
        "method": "text_memory_reflector",
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
    "parametric_memory": {
        "enabled": False,
        "method": "parametric_memory_register",
        "config": {},
    },
}


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
    "codex_model": "gpt-5.5",
    "evolution": {"targets": EVOLUTION_TARGETS},
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
    assert project.execution.codex_model == "gpt-5.5"
    assert project.evolution.model_dump(mode="json") == {"targets": EVOLUTION_TARGETS}
    assert profile.id == "science-team"
    assert profile.host == "gpu.example.edu"
    assert profile.port == 22
    assert profile.user == "alice"
    assert profile.auth.method == "ssh_agent"
    assert profile.proxy.https_proxy == "http://127.0.0.1:7890"
    assert profile.proxy.huggingface_endpoint == "https://hf-mirror.com"


def test_draft_secret_references_and_credentialed_urls_are_repr_safe() -> None:
    key_canary = "/private/SECRET_DRAFT_KEY_PATH"
    reference_canary = "SECRET_DRAFT_REFERENCE"
    proxy_canary = "http://proxy-user:SECRET_DRAFT_PROXY@example.test"
    draft = DesktopProjectConfigDraft.model_validate(
        VALID_DRAFT
        | {
            "auth_method": "private_key",
            "private_key_path": key_canary,
            "passphrase_ref": reference_canary,
            "https_proxy": proxy_canary,
        }
    )

    rendered = repr(draft)
    assert key_canary not in rendered
    assert reference_canary not in rendered
    assert proxy_canary not in rendered

    with pytest.raises(ValidationError) as exc_info:
        DesktopProjectConfigDraft.model_validate(
            VALID_DRAFT
            | {
                "auth_method": "invalid-auth",
                "password_ref": reference_canary,
            }
        )
    assert reference_canary not in str(exc_info.value)
    assert reference_canary not in repr(exc_info.value)


def test_desktop_project_config_draft_defaults_subscription_codex_model() -> None:
    draft_payload = dict(VALID_DRAFT)
    draft_payload.pop("codex_model")
    draft = DesktopProjectConfigDraft.model_validate(draft_payload)

    project, _profile = build_desktop_project_configs(draft)

    assert project.execution.mode == "codex_subscription_transcript"
    assert project.execution.codex_model == "gpt-5.5"
    assert project.execution.hf_model is None


def test_desktop_project_config_draft_builds_self_deployed() -> None:
    draft = DesktopProjectConfigDraft.model_validate(
        VALID_DRAFT
        | {
            "execution_mode": "self-deployed",
            "codex_model": None,
            "hf_model": "Qwen/Qwen3-Coder-30B-A3B-Instruct",
        }
    )

    project, _profile = build_desktop_project_configs(draft)

    assert draft.execution_mode == "self-deployed"
    assert draft.model_dump(mode="json")["execution_mode"] == "self-deployed"
    assert project.execution.mode == "self-deployed"
    assert project.execution.codex_model is None
    assert project.execution.hf_model == "Qwen/Qwen3-Coder-30B-A3B-Instruct"


def test_desktop_project_config_draft_accepts_legacy_managed_local_inference_alias() -> None:
    draft = DesktopProjectConfigDraft.model_validate(
        VALID_DRAFT
        | {
            "execution_mode": "codex_managed_local_inference",
            "codex_model": None,
            "hf_model": "Qwen/Qwen3-Coder-30B-A3B-Instruct",
        }
    )

    project, _profile = build_desktop_project_configs(draft)

    assert draft.execution_mode == "self-deployed"
    assert draft.model_dump(mode="json")["execution_mode"] == "self-deployed"
    assert project.execution.mode == "self-deployed"
    assert project.execution.codex_model is None
    assert project.execution.hf_model == "Qwen/Qwen3-Coder-30B-A3B-Instruct"


def test_desktop_project_config_draft_builds_self_deployed_without_codex_model() -> None:
    draft_payload = dict(VALID_DRAFT)
    draft_payload.pop("codex_model")
    draft = DesktopProjectConfigDraft.model_validate(
        draft_payload
        | {
            "execution_mode": "self-deployed",
            "hf_model": "Qwen/Qwen3-Coder-30B-A3B-Instruct",
        }
    )

    project, _profile = build_desktop_project_configs(draft)

    assert project.execution.mode == "self-deployed"
    assert project.execution.codex_model is None
    assert project.execution.hf_model == "Qwen/Qwen3-Coder-30B-A3B-Instruct"


def test_desktop_project_config_draft_rejects_self_deployed_without_hf_model() -> None:
    with pytest.raises(
        ValidationError,
        match="hf_model is required for self-deployed mode",
    ):
        DesktopProjectConfigDraft.model_validate(
            VALID_DRAFT
            | {
                "execution_mode": "self-deployed",
                "codex_model": None,
            }
        )


def test_desktop_project_config_draft_rejects_hf_model_for_subscription() -> None:
    with pytest.raises(
        ValidationError,
        match="hf_model is only valid for self-deployed mode",
    ):
        DesktopProjectConfigDraft.model_validate(
            VALID_DRAFT | {"hf_model": "Qwen/Qwen3-Coder-30B-A3B-Instruct"}
        )


def test_save_desktop_project_config_writes_deterministic_yaml(tmp_path: Path) -> None:
    draft = DesktopProjectConfigDraft.model_validate(VALID_DRAFT)

    project, profile, paths = save_desktop_project_config(draft, tmp_path)

    assert project.path == paths.science_config_path
    assert profile.path == paths.remote_profile_path
    assert paths.science_config_path == (tmp_path / "projects" / "protein-design" / "science.yaml")
    assert paths.remote_profile_path == tmp_path / "profiles" / "science-team.yaml"
    assert paths.science_config_path.is_file()
    assert paths.remote_profile_path.is_file()
    science_yaml = yaml.safe_load(paths.science_config_path.read_text(encoding="utf-8"))
    remote_yaml = yaml.safe_load(paths.remote_profile_path.read_text(encoding="utf-8"))
    assert science_yaml["project"]["name"] == "Protein Design"
    assert science_yaml["remote_profile"] == "science-team"
    assert science_yaml["task"]["source"]["path"] == "/datasets/folding-baseline"
    assert science_yaml["execution"]["mode"] == "codex_subscription_transcript"
    assert science_yaml["evolution"] == {"targets": EVOLUTION_TARGETS}
    assert "artifacts" not in science_yaml
    assert remote_yaml["id"] == "science-team"
    assert remote_yaml["proxy"]["https_proxy"] == "http://127.0.0.1:7890"
    assert "path" not in science_yaml
    assert "path" not in remote_yaml


def test_desktop_project_config_draft_rejects_removed_target_booleans() -> None:
    payload = dict(VALID_DRAFT)
    payload.pop("evolution")
    payload["text_memory"] = True

    with pytest.raises(ValidationError, match="text_memory"):
        DesktopProjectConfigDraft.model_validate(payload)


def test_save_desktop_project_config_writes_self_deployed_yaml(
    tmp_path: Path,
) -> None:
    draft = DesktopProjectConfigDraft.model_validate(
        VALID_DRAFT
        | {
            "execution_mode": "self-deployed",
            "codex_model": None,
            "hf_model": "Qwen/Qwen3-Coder-30B-A3B-Instruct",
        }
    )

    _project, _profile, paths = save_desktop_project_config(draft, tmp_path)

    science_yaml = yaml.safe_load(paths.science_config_path.read_text(encoding="utf-8"))
    assert science_yaml["execution"] == {
        "mode": "self-deployed",
        "hf_model": "Qwen/Qwen3-Coder-30B-A3B-Instruct",
    }


def test_list_desktop_project_configs_returns_non_secret_summaries(
    tmp_path: Path,
) -> None:
    alpha = DesktopProjectConfigDraft.model_validate(
        VALID_DRAFT | {"project_name": "Alpha Project", "remote_profile_id": "alpha"}
    )
    zeta = DesktopProjectConfigDraft.model_validate(
        VALID_DRAFT
        | {
            "project_name": "Zeta Project",
            "remote_profile_id": "zeta",
            "remote_host": "zeta.example.edu",
        }
    )
    save_desktop_project_config(zeta, tmp_path)
    save_desktop_project_config(alpha, tmp_path)

    configs = list_desktop_project_configs(tmp_path)

    assert [config.project_slug for config in configs] == [
        "alpha-project",
        "zeta-project",
    ]
    first = configs[0]
    assert first.valid is True
    assert first.error is None
    assert first.project_name == "Alpha Project"
    assert first.task_id == "folding-baseline"
    assert first.source_type == "remote_path"
    assert first.source_label == "/datasets/folding-baseline"
    assert first.remote_profile_id == "alpha"
    assert first.remote_host == "gpu.example.edu"
    assert first.remote_user == "alice"
    assert first.science_config_path == (tmp_path / "projects" / "alpha-project" / "science.yaml")
    assert first.remote_profile_path == tmp_path / "profiles" / "alpha.yaml"
    assert "password" not in first.model_dump_json()
    assert "private_key_path" not in first.model_dump_json()


def test_list_desktop_project_configs_omits_auth_secret_references(
    tmp_path: Path,
) -> None:
    draft = DesktopProjectConfigDraft.model_validate(
        VALID_DRAFT
        | {
            "auth_method": "private_key",
            "private_key_path": "/home/alice/.ssh/openevo",
            "passphrase_ref": "keyring://openevo/science-team-passphrase",
        }
    )
    save_desktop_project_config(draft, tmp_path)

    configs = list_desktop_project_configs(tmp_path)

    serialized = configs[0].model_dump_json()
    assert configs[0].valid is True
    assert "private_key_path" not in serialized
    assert "/home/alice/.ssh/openevo" not in serialized
    assert "passphrase_ref" not in serialized
    assert "science-team-passphrase" not in serialized


def test_list_desktop_project_configs_redacts_git_url_userinfo(tmp_path: Path) -> None:
    draft = DesktopProjectConfigDraft.model_validate(
        VALID_DRAFT
        | {
            "source_type": "git_repository",
            "source_path": None,
            "source_url": "https://alice:super-secret-token@example.com/repo.git",
            "source_branch": "main",
        }
    )
    save_desktop_project_config(draft, tmp_path)

    configs = list_desktop_project_configs(tmp_path)

    assert len(configs) == 1
    assert configs[0].source_label == "https://example.com/repo.git@main"
    assert "super-secret-token" not in configs[0].model_dump_json()


def test_list_desktop_project_configs_redacts_git_url_userinfo_with_bad_port(
    tmp_path: Path,
) -> None:
    draft = DesktopProjectConfigDraft.model_validate(
        VALID_DRAFT
        | {
            "source_type": "git_repository",
            "source_path": None,
            "source_url": "https://alice:super-secret-token@example.com:notaport/repo.git",
        }
    )
    save_desktop_project_config(draft, tmp_path)

    configs = list_desktop_project_configs(tmp_path)

    assert len(configs) == 1
    assert configs[0].valid is True
    assert configs[0].source_label == "https://example.com:notaport/repo.git"
    assert "super-secret-token" not in configs[0].model_dump_json()


def test_list_desktop_project_configs_redacts_scp_like_git_userinfo(
    tmp_path: Path,
) -> None:
    draft = DesktopProjectConfigDraft.model_validate(
        VALID_DRAFT
        | {
            "source_type": "git_repository",
            "source_path": None,
            "source_url": "super-secret-token@example.com:org/repo.git",
        }
    )
    save_desktop_project_config(draft, tmp_path)

    configs = list_desktop_project_configs(tmp_path)

    assert len(configs) == 1
    assert configs[0].source_label == "example.com:org/repo.git"
    assert "super-secret-token" not in configs[0].model_dump_json()


def test_list_desktop_project_configs_marks_missing_profile_invalid(
    tmp_path: Path,
) -> None:
    draft = DesktopProjectConfigDraft.model_validate(VALID_DRAFT)
    _project, _profile, paths = save_desktop_project_config(draft, tmp_path)
    paths.remote_profile_path.unlink()

    configs = list_desktop_project_configs(tmp_path)

    assert len(configs) == 1
    summary = configs[0]
    assert summary.project_slug == "protein-design"
    assert summary.valid is False
    assert summary.project_name == "Protein Design"
    assert summary.remote_profile_id == "science-team"
    assert summary.remote_profile_path == tmp_path / "profiles" / "science-team.yaml"
    assert summary.remote_host is None
    assert summary.remote_user is None
    assert "Remote profile config not found" in (summary.error or "")
    assert str(tmp_path) not in (summary.error or "")


def test_list_desktop_project_configs_sanitizes_invalid_profile_inputs(
    tmp_path: Path,
) -> None:
    draft = DesktopProjectConfigDraft.model_validate(VALID_DRAFT)
    _project, _profile, paths = save_desktop_project_config(draft, tmp_path)
    paths.remote_profile_path.write_text(
        "\n".join(
            [
                "version: 1",
                "id: science-team",
                "host: gpu.example.edu",
                "port: 22",
                "user: alice",
                "password: super-secret-value",
            ]
        ),
        encoding="utf-8",
    )

    configs = list_desktop_project_configs(tmp_path)

    summary = configs[0]
    assert summary.valid is False
    assert "Extra inputs are not permitted" in (summary.error or "")
    assert "super-secret-value" not in (summary.error or "")


def test_load_desktop_project_config_rejects_path_traversal(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="Invalid Desktop project slug"):
        load_desktop_project_config(tmp_path, "../profiles")


def test_load_desktop_project_config_loads_saved_models(tmp_path: Path) -> None:
    draft = DesktopProjectConfigDraft.model_validate(VALID_DRAFT)
    save_desktop_project_config(draft, tmp_path)

    project, profile, paths = load_desktop_project_config(tmp_path, "protein-design")

    assert project.project.name == "Protein Design"
    assert project.remote_profile == "science-team"
    assert profile.id == "science-team"
    assert profile.host == "gpu.example.edu"
    assert paths.science_config_path == (tmp_path / "projects" / "protein-design" / "science.yaml")
    assert paths.remote_profile_path == tmp_path / "profiles" / "science-team.yaml"


def test_desktop_project_config_draft_rejects_raw_secret_fields() -> None:
    with pytest.raises(ValidationError, match="password"):
        DesktopProjectConfigDraft.model_validate(VALID_DRAFT | {"password": "secret"})


def test_desktop_project_config_draft_validates_source_requirements() -> None:
    with pytest.raises(ValidationError, match="source_url"):
        DesktopProjectConfigDraft.model_validate(
            (VALID_DRAFT | {"source_type": "git_repository"}) | {"source_path": None}
        )
