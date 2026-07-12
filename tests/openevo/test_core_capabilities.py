from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from openevo.backend.api import create_backend_app
from openevo.evolution import methods as evolution_methods
from openevo.evolution.framework import (
    CapabilityAudience,
    build_evolution_capabilities,
    execution_profile_for_release_mode,
)
from tests.framework_testkit import verified_builtin_registry


def test_remote_capabilities_project_the_passed_frozen_registry(tmp_path) -> None:
    registry = verified_builtin_registry(tmp_path)
    response = TestClient(create_backend_app(evolution_registry=registry)).get(
        "/capabilities",
        params={"execution_mode": "codex_subscription_transcript"},
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload == build_evolution_capabilities(
        registry.snapshot,
        profile=execution_profile_for_release_mode(
            "codex_subscription_transcript"
        ),
        audience=CapabilityAudience.DESKTOP,
        core_version=payload["core_version"],
    ).model_dump(mode="json")
    assert payload["registry_digest"] == registry.snapshot.registry_digest
    targets = {target["target_id"]: target for target in payload["targets"]}
    assert set(targets) == {"agent_system", "skill_bundle", "text_memory"}
    for target_id, target in targets.items():
        assert {method["method_id"] for method in target["accepted_methods"]} == {
            method_id
            for method_id, descriptor in registry.snapshot.methods.items()
            if descriptor.target_id == target_id
        }
        assert {method["method_id"] for method in target["methods"]} == {
            method_id
            for method_id, descriptor in registry.snapshot.methods.items()
            if descriptor.target_id == target_id
            and descriptor.exposure.value == "desktop"
        }
    auto = targets["agent_system"]["selection_resolvers"]
    assert len(auto) == 1
    assert auto[0]["selection_value"] == "auto"
    assert {method["method_id"] for method in auto[0]["resolved_methods"]} == {
        "agent_system_reflector",
        "agent_system_history_reflector",
    }
    assert all(
        method["support"]["overall"] == "supported"
        for method in auto[0]["resolved_methods"]
    )


class _ForbiddenLegacyCatalog:
    def __getattribute__(self, name: str):
        if name.startswith("__"):
            return object.__getattribute__(self, name)
        raise AssertionError(f"capability endpoint accessed legacy catalog: {name}")

    def __iter__(self):
        raise AssertionError("capability endpoint iterated legacy catalog")

    def __len__(self):
        raise AssertionError("capability endpoint sized legacy catalog")

    def __getitem__(self, key):
        raise AssertionError(f"capability endpoint indexed legacy catalog: {key}")


def test_remote_capabilities_do_not_access_legacy_catalogs(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    registry = verified_builtin_registry(tmp_path)
    sentinel = _ForbiddenLegacyCatalog()
    monkeypatch.setattr(evolution_methods, "METHOD_METADATA", sentinel)
    monkeypatch.setattr(evolution_methods, "METHOD_REGISTRY", sentinel)

    response = TestClient(create_backend_app(evolution_registry=registry)).get(
        "/capabilities",
        params={"execution_mode": "self-deployed"},
    )

    assert response.status_code == 200
    assert response.json()["registry_digest"] == registry.snapshot.registry_digest


def test_remote_capabilities_fail_closed_without_verified_registry() -> None:

    response = TestClient(create_backend_app()).get(
        "/capabilities",
        params={"execution_mode": "self-deployed"},
    )

    assert response.status_code == 503
    assert response.json()["code"] == "evolution_registry_unavailable"
