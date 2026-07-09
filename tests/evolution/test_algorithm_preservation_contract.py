from __future__ import annotations

from pathlib import Path

from openevo.evolution.methods import METHOD_METADATA, METHOD_REGISTRY
from openevo.evolution.models import ArtifactType, WorkerClaimedJob


OBJECT_CONFIG_SCHEMA = {"type": "object", "additionalProperties": True}

EXPECTED_METHOD_METADATA = {
    "text_memory": {
        "method_id": "text_memory",
        "display_name": "Text Memory",
        "description": "Build text memory directly from dataset records.",
        "artifact_type": "text_memory",
        "visibility": "dev_kit",
        "visible_in_desktop": False,
        "input_requirements": ("dataset",),
        "supported_execution_modes": ("self-deployed",),
        "default_config": {},
        "config_schema": OBJECT_CONFIG_SCHEMA,
        "stability_level": "stable",
    },
    "text_memory_reflector": {
        "method_id": "text_memory_reflector",
        "display_name": "Text Memory Reflector",
        "description": "Reflect over task trajectories to synthesize reusable text memory.",
        "artifact_type": "text_memory",
        "visibility": "ordinary_user",
        "visible_in_desktop": True,
        "input_requirements": ("dataset",),
        "supported_execution_modes": ("codex_subscription_transcript", "self-deployed"),
        "default_config": {},
        "config_schema": OBJECT_CONFIG_SCHEMA,
        "stability_level": "stable",
    },
    "text_memory_expel_reflector": {
        "method_id": "text_memory_expel_reflector",
        "display_name": "Text Memory ExpeL Reflector",
        "description": "Produce structured ExpeL-style text memory from success and failure traces.",
        "artifact_type": "text_memory",
        "visibility": "dev_kit",
        "visible_in_desktop": False,
        "input_requirements": ("dataset",),
        "supported_execution_modes": ("codex_subscription_transcript", "self-deployed"),
        "default_config": {},
        "config_schema": OBJECT_CONFIG_SCHEMA,
        "stability_level": "experimental",
    },
    "skill_bundle": {
        "method_id": "skill_bundle",
        "display_name": "Skill Bundle",
        "description": "Register a configured skill bundle artifact.",
        "artifact_type": "skill_bundle",
        "visibility": "dev_kit",
        "visible_in_desktop": False,
        "input_requirements": (),
        "supported_execution_modes": ("self-deployed",),
        "default_config": {},
        "config_schema": OBJECT_CONFIG_SCHEMA,
        "stability_level": "stable",
    },
    "skill_bundle_reflector": {
        "method_id": "skill_bundle_reflector",
        "display_name": "Skill Bundle Reflector",
        "description": "Reflect over trajectories to synthesize a harness-loadable skill bundle.",
        "artifact_type": "skill_bundle",
        "visibility": "ordinary_user",
        "visible_in_desktop": True,
        "input_requirements": ("dataset",),
        "supported_execution_modes": ("codex_subscription_transcript", "self-deployed"),
        "default_config": {},
        "config_schema": OBJECT_CONFIG_SCHEMA,
        "stability_level": "stable",
    },
    "agent_system": {
        "method_id": "agent_system",
        "display_name": "Agent System",
        "description": "Register a configured agent-system instruction artifact.",
        "artifact_type": "agent_system",
        "visibility": "dev_kit",
        "visible_in_desktop": False,
        "input_requirements": (),
        "supported_execution_modes": ("self-deployed",),
        "default_config": {"target_path": "AGENTS.md"},
        "config_schema": OBJECT_CONFIG_SCHEMA,
        "stability_level": "stable",
    },
    "agent_system_reflector": {
        "method_id": "agent_system_reflector",
        "display_name": "Agent System Reflector",
        "description": "Reflect over trajectories to synthesize improved agent instructions.",
        "artifact_type": "agent_system",
        "visibility": "ordinary_user",
        "visible_in_desktop": True,
        "input_requirements": ("dataset",),
        "supported_execution_modes": ("codex_subscription_transcript", "self-deployed"),
        "default_config": {"target_path": "AGENTS.md"},
        "config_schema": OBJECT_CONFIG_SCHEMA,
        "stability_level": "stable",
    },
    "agent_system_history_reflector": {
        "method_id": "agent_system_history_reflector",
        "display_name": "Agent System History Reflector",
        "description": "Use prior evolution rounds to synthesize the next agent-system artifact.",
        "artifact_type": "agent_system",
        "visibility": "dev_kit",
        "visible_in_desktop": False,
        "input_requirements": ("dataset",),
        "supported_execution_modes": ("codex_subscription_transcript", "self-deployed"),
        "default_config": {"target_path": "AGENTS.md"},
        "config_schema": OBJECT_CONFIG_SCHEMA,
        "stability_level": "experimental",
    },
    "agent_system_pareto_reflector": {
        "method_id": "agent_system_pareto_reflector",
        "display_name": "Agent System Pareto Reflector",
        "description": "Generate and select agent-system candidates with Pareto-style scoring.",
        "artifact_type": "agent_system",
        "visibility": "dev_kit",
        "visible_in_desktop": False,
        "input_requirements": ("dataset",),
        "supported_execution_modes": ("codex_subscription_transcript", "self-deployed"),
        "default_config": {"target_path": "AGENTS.md"},
        "config_schema": OBJECT_CONFIG_SCHEMA,
        "stability_level": "experimental",
    },
    "agent_system_gepa_reflector": {
        "method_id": "agent_system_gepa_reflector",
        "display_name": "Agent System GEPA Reflector",
        "description": "Generate agent-system prompt mutations using GEPA-style strategies.",
        "artifact_type": "agent_system",
        "visibility": "dev_kit",
        "visible_in_desktop": False,
        "input_requirements": ("dataset",),
        "supported_execution_modes": ("codex_subscription_transcript", "self-deployed"),
        "default_config": {"target_path": "AGENTS.md"},
        "config_schema": OBJECT_CONFIG_SCHEMA,
        "stability_level": "experimental",
    },
    "parametric_memory_register": {
        "method_id": "parametric_memory_register",
        "display_name": "Parametric Memory Register",
        "description": "Register an existing adapter as a parametric-memory artifact.",
        "artifact_type": "parametric_memory",
        "visibility": "dev_kit",
        "visible_in_desktop": False,
        "input_requirements": ("adapter",),
        "supported_execution_modes": ("self-deployed",),
        "default_config": {},
        "config_schema": OBJECT_CONFIG_SCHEMA,
        "stability_level": "experimental",
    },
    "parametric_memory_lora_sft": {
        "method_id": "parametric_memory_lora_sft",
        "display_name": "Parametric Memory LoRA SFT",
        "description": "Train or register a LoRA-style supervised fine-tuning adapter.",
        "artifact_type": "parametric_memory",
        "visibility": "dev_kit",
        "visible_in_desktop": False,
        "input_requirements": ("dataset",),
        "supported_execution_modes": ("self-deployed",),
        "default_config": {},
        "config_schema": OBJECT_CONFIG_SCHEMA,
        "stability_level": "experimental",
    },
}

EXPECTED_METHOD_REGISTRY = {
    "text_memory": "text_memory",
    "text_memory_reflector": "text_memory_reflector",
    "text_memory_expel_reflector": "text_memory_expel_reflector",
    "skill_bundle": "skill_bundle",
    "skill_bundle_reflector": "skill_bundle_reflector",
    "agent_system": "agent_system",
    "agent_system_reflector": "agent_system_reflector",
    "agent_system_history_reflector": "agent_system_history_reflector",
    "agent_system_pareto_reflector": "agent_system_pareto_reflector",
    "agent_system_gepa_reflector": "agent_system_gepa_reflector",
    "parametric_memory_register": "parametric_memory_register",
    "parametric_memory_lora_sft": "parametric_memory_lora_sft",
}


def _job(method: str, config: dict) -> WorkerClaimedJob:
    return WorkerClaimedJob(
        job_id=f"job-{method}",
        lease_id="lease-1",
        job_type="evolution",
        method=method,
        config=config,
    )


def test_method_registry_and_metadata_contract_is_preserved() -> None:
    assert {method: implementation.__name__ for method, implementation in METHOD_REGISTRY.items()} == (
        EXPECTED_METHOD_REGISTRY
    )
    assert METHOD_METADATA == EXPECTED_METHOD_METADATA


def test_configured_skill_bundle_output_contract(tmp_path: Path) -> None:
    artifacts = METHOD_REGISTRY["skill_bundle"](
        _job(
            "skill_bundle",
            {
                "name": "Careful Skill",
                "skill_markdown": "# Careful Skill\n\nCheck assumptions.\n",
                "tags": ["science"],
                "promoted": True,
            },
        ),
        tmp_path,
    )
    artifact = artifacts[0]
    assert artifact.type == ArtifactType.SKILL_BUNDLE
    assert artifact.name == "Careful Skill"
    assert artifact.manifest == {"entrypoint": "SKILL.md", "files": ["SKILL.md"]}
    assert artifact.tags == ["science"]
    assert artifact.promoted is True
    skill_path = Path(artifact.uri.removeprefix("file://")) / "SKILL.md"
    assert skill_path.read_text(encoding="utf-8").endswith("Check assumptions.\n")


def test_configured_agent_system_output_contract(tmp_path: Path) -> None:
    artifacts = METHOD_REGISTRY["agent_system"](
        _job(
            "agent_system",
            {
                "name": "Agent Instructions",
                "target_path": "AGENTS.md",
                "agent_system_markdown": "# Instructions\n\nPreserve behavior.\n",
                "lineage": {"source": "test"},
            },
        ),
        tmp_path,
    )
    artifact = artifacts[0]
    assert artifact.type == ArtifactType.AGENT_SYSTEM
    assert artifact.manifest["target_path"] == "AGENTS.md"
    assert artifact.lineage == {"source": "test"}
    output_path = Path(artifact.uri.removeprefix("file://"))
    assert output_path.name == "AGENTS.md"
    assert "Preserve behavior." in output_path.read_text(encoding="utf-8")


def test_parametric_memory_register_output_contract(tmp_path: Path) -> None:
    adapter_dir = tmp_path / "adapter"
    adapter_dir.mkdir()
    (adapter_dir / "adapter_config.json").write_text("{}", encoding="utf-8")

    artifacts = METHOD_REGISTRY["parametric_memory_register"](
        _job(
            "parametric_memory_register",
            {
                "adapter_uri": adapter_dir.resolve().as_uri(),
                "base_model": "Qwen/Qwen2.5-Coder-7B-Instruct",
                "adapter_format": "lora",
                "name": "science adapter",
            },
        ),
        tmp_path,
    )
    artifact = artifacts[0]
    assert artifact.type == ArtifactType.PARAMETRIC_MEMORY
    assert artifact.name == "science adapter"
    assert artifact.manifest["base_model"] == "Qwen/Qwen2.5-Coder-7B-Instruct"
    assert artifact.manifest["adapter_format"] == "lora"
