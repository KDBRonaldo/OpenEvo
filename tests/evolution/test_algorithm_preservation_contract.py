from __future__ import annotations

from openevo.evolution.methods import METHOD_METADATA, METHOD_REGISTRY


PROTECTED_METHOD_CONTRACTS = {
    "text_memory_expel_reflector": {
        "artifact_type": "text_memory",
        "input_requirements": ("dataset",),
        "supported_execution_modes": (
            "codex_subscription_transcript",
            "self-deployed",
        ),
        "default_config": {},
    },
    "skill_bundle_reflector": {
        "artifact_type": "skill_bundle",
        "input_requirements": ("dataset",),
        "supported_execution_modes": (
            "codex_subscription_transcript",
            "self-deployed",
        ),
        "default_config": {},
    },
    "agent_system_gepa_reflector": {
        "artifact_type": "agent_system",
        "input_requirements": ("dataset",),
        "supported_execution_modes": (
            "codex_subscription_transcript",
            "self-deployed",
        ),
        "default_config": {"target_path": "AGENTS.md"},
    },
}


def test_protected_methods_keep_algorithm_facing_registry_contract() -> None:
    for method_id, expected in PROTECTED_METHOD_CONTRACTS.items():
        assert METHOD_REGISTRY[method_id].__name__ == method_id
        metadata = METHOD_METADATA[method_id]
        assert metadata["method_id"] == method_id
        assert {key: metadata[key] for key in expected} == expected
