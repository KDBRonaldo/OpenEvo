from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from openevo.projects.science import ScienceProjectConfig
from desktop.sidecar import (
    RemoteProfileConfig,
    SidecarSciencePlan,
    build_sidecar_science_plan,
)


def _profile(**overrides: object) -> RemoteProfileConfig:
    payload = {
        "version": 1,
        "id": "lab-gpu",
        "host": "gpu.example.edu",
        "user": "alice",
        "proxy": {
            "http_proxy": "http://127.0.0.1:7890",
            "huggingface_endpoint": "https://hf-mirror.com",
        },
    }
    payload.update(overrides)
    return RemoteProfileConfig.model_validate(payload)


def _project(**overrides: object) -> ScienceProjectConfig:
    payload = {
        "version": 1,
        "project": {"name": "protein-design"},
        "remote_profile": "lab-gpu",
        "task": {
            "id": "folding-baseline",
            "objective": "Improve the folding baseline.",
            "source": {"type": "scratch"},
        },
    }
    payload.update(overrides)
    return ScienceProjectConfig.model_validate(payload)


def test_subscription_plan_requires_codex_preflight_and_includes_proxy_env() -> None:
    plan = build_sidecar_science_plan(_project(), _profile())

    assert plan.project_name == "protein-design"
    assert plan.task_id == "folding-baseline"
    assert plan.remote_profile_id == "lab-gpu"
    assert plan.preflight.require_codex_subscription is True
    assert plan.preflight.min_home_available_kb == 20_000_000
    assert plan.proxy_env["HTTP_PROXY"] == "http://127.0.0.1:7890"
    assert plan.proxy_env["http_proxy"] == "http://127.0.0.1:7890"
    assert plan.proxy_env["HF_ENDPOINT"] == "https://hf-mirror.com"
    assert plan.experiment["agent"]["auth"] == "subscription"


def test_local_inference_plan_skips_codex_preflight_and_sets_managed_hf_model() -> None:
    plan = build_sidecar_science_plan(
        _project(
            execution={
                "mode": "codex_managed_local_inference",
                "hf_model": "Qwen/Qwen3-Coder-30B-A3B-Instruct",
            }
        ),
        _profile(min_home_available_kb=42),
    )

    assert plan.preflight.require_codex_subscription is False
    assert plan.preflight.min_home_available_kb == 42
    assert plan.experiment["agent"]["auth"] == "proxy"
    assert plan.experiment["runtime"]["env"]["OPENEVO_MANAGED_HF_MODEL"] == (
        "Qwen/Qwen3-Coder-30B-A3B-Instruct"
    )


def test_profile_id_must_match_science_project_remote_profile() -> None:
    profile = _profile(id="other-gpu")

    with pytest.raises(ValueError, match="remote_profile"):
        build_sidecar_science_plan(_project(), profile)


def test_local_folder_plan_compiles_with_prepared_workspace(tmp_path: Path) -> None:
    source_dir = tmp_path / "task-src"
    source_dir.mkdir()
    project = _project(
        path=tmp_path / "science.yaml",
        task={
            "id": "folding-baseline",
            "objective": "Improve the folding baseline.",
            "source": {"type": "local_folder", "path": "task-src"},
        },
    )

    plan = build_sidecar_science_plan(project, _profile())

    action = plan.workspace.actions[0]
    assert action.type == "upload_dir"
    assert action.source == str(source_dir)
    assert plan.experiment["tasks"][0]["workspace"] == action.target
    assert (
        plan.experiment["tasks"][0]["metadata"]["openevo"]["source_fingerprint"]
        == action.source_fingerprint
    )


def test_sidecar_plan_proxy_env_is_immutable() -> None:
    plan = build_sidecar_science_plan(_project(), _profile())

    with pytest.raises(TypeError):
        plan.proxy_env["X"] = "Y"


def test_sidecar_plan_experiment_snapshot_is_deeply_immutable() -> None:
    plan = build_sidecar_science_plan(_project(), _profile())

    with pytest.raises(TypeError):
        plan.experiment["agent"]["settings"]["capture_mode"] = "none"

    with pytest.raises(TypeError):
        plan.experiment["tasks"] += ({"id": "other"},)

    with pytest.raises(TypeError):
        plan.experiment["tasks"][0]["metadata"]["openevo"]["source_type"] = "changed"


def test_sidecar_plan_json_dump_uses_plain_json_objects() -> None:
    plan = build_sidecar_science_plan(_project(), _profile())

    dumped = plan.model_dump(mode="json")
    payload = json.loads(plan.model_dump_json())

    assert isinstance(dumped["proxy_env"], dict)
    assert isinstance(dumped["experiment"], dict)
    assert isinstance(dumped["experiment"]["tasks"], list)
    assert payload["proxy_env"]["HTTP_PROXY"] == "http://127.0.0.1:7890"
    assert isinstance(payload["proxy_env"], dict)
    assert payload["experiment"]["agent"]["auth"] == "subscription"
    assert payload["experiment"]["agent"]["settings"]["capture_mode"] == "transcript"
    assert isinstance(payload["experiment"], dict)
    assert isinstance(payload["experiment"]["tasks"], list)


def test_sidecar_plan_rejects_non_json_experiment_values() -> None:
    plan = build_sidecar_science_plan(_project(), _profile())
    payload = plan.model_dump(mode="json")
    payload["experiment"] = {"agent": object()}

    with pytest.raises(ValidationError, match="JSON-serializable"):
        SidecarSciencePlan.model_validate(payload)


def test_sidecar_plan_rejects_non_finite_experiment_numbers() -> None:
    plan = build_sidecar_science_plan(_project(), _profile())
    payload = plan.model_dump(mode="json")
    payload["experiment"] = {"score": float("nan")}

    with pytest.raises(ValidationError, match="finite"):
        SidecarSciencePlan.model_validate(payload)
