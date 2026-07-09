from openevo.capabilities import (
    MethodVisibility,
    build_core_capabilities,
    method_metadata_by_id,
)
from openevo.evolution.methods import METHOD_METADATA, METHOD_REGISTRY


def test_core_capabilities_expose_desktop_visible_non_parametric_methods() -> None:
    capabilities = build_core_capabilities()

    visible_targets = {
        target.artifact_type
        for target in capabilities.artifact_targets
        if target.visible_in_desktop
    }
    assert visible_targets == {"text_memory", "skill_bundle", "agent_system"}

    method_ids = {
        method.method_id
        for method in capabilities.evolution_methods
        if method.visible_in_desktop
    }
    assert {
        "text_memory_reflector",
        "skill_bundle_reflector",
        "agent_system_reflector",
    }.issubset(method_ids)


def test_method_metadata_contains_required_schema_fields() -> None:
    metadata = method_metadata_by_id()
    text_memory = metadata["text_memory_reflector"]
    agent_system = metadata["agent_system_reflector"]

    assert text_memory.method_id == "text_memory_reflector"
    assert text_memory.artifact_type == "text_memory"
    assert text_memory.visibility == MethodVisibility.ORDINARY_USER
    assert text_memory.input_requirements == ("dataset",)
    assert text_memory.default_config == {}
    assert text_memory.supported_execution_modes == (
        "codex_subscription_transcript",
        "self-deployed",
    )
    assert text_memory.config_schema["type"] == "object"
    assert agent_system.default_config == {"target_path": "AGENTS.md"}


def test_capability_models_have_stable_json_shape() -> None:
    payload = build_core_capabilities().model_dump(mode="json")

    assert {item["mode"] for item in payload["execution_modes"]} == {
        "codex_subscription_transcript",
        "self-deployed",
    }
    assert all("display_name" in item for item in payload["evolution_methods"])
    assert all("stability_level" in item for item in payload["evolution_methods"])
    assert all("input_requirements" in item for item in payload["evolution_methods"])
    assert all("default_config" in item for item in payload["evolution_methods"])


def test_every_registered_method_has_metadata() -> None:
    required_fields = {
        "method_id",
        "display_name",
        "artifact_type",
        "description",
        "input_requirements",
        "supported_execution_modes",
        "config_schema",
        "default_config",
        "stability_level",
        "visibility",
        "visible_in_desktop",
    }

    assert set(METHOD_METADATA) == set(METHOD_REGISTRY)
    for method_id, payload in METHOD_METADATA.items():
        assert required_fields.issubset(payload)
        assert payload["method_id"] == method_id
