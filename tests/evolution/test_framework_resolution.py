from __future__ import annotations

import pytest

from openevo.evolution.framework import (
    resolve_agent_system_method,
    resolve_evolution_method,
)


def test_agent_system_auto_uses_only_prior_dataset_presence() -> None:
    assert resolve_agent_system_method("auto", ()) == "agent_system_reflector"
    assert resolve_agent_system_method(
        "auto",
        ("dataset-round-0",),
    ) == "agent_system_history_reflector"


def test_agent_system_explicit_method_is_never_reinterpreted() -> None:
    assert resolve_agent_system_method(
        "agent_system_gepa_reflector",
        ("dataset-round-0",),
    ) == "agent_system_gepa_reflector"


@pytest.mark.parametrize("method", ["", " "])
def test_agent_system_method_must_be_nonempty(method: str) -> None:
    with pytest.raises(ValueError, match="must not be empty"):
        resolve_agent_system_method(method, ())


def test_agent_system_prior_dataset_ids_must_be_nonempty() -> None:
    with pytest.raises(ValueError, match="dataset artifact IDs"):
        resolve_agent_system_method("auto", ("",))


def test_agent_system_prior_dataset_ids_reject_bare_string_and_non_strings() -> None:
    with pytest.raises(TypeError, match="sequence of strings"):
        resolve_agent_system_method("auto", "dataset-round-0")
    with pytest.raises(TypeError, match="contain only strings"):
        resolve_agent_system_method("auto", (b"dataset-round-0",))


def test_generic_method_resolution_keeps_algorithm_policy_in_core() -> None:
    assert resolve_evolution_method(
        target_id="agent_system",
        requested_method="auto",
        prior_dataset_artifact_ids=(),
    ) == "agent_system_reflector"
    assert resolve_evolution_method(
        target_id="agent_system",
        requested_method="auto",
        prior_dataset_artifact_ids=("dataset-round-0",),
    ) == "agent_system_history_reflector"
    assert resolve_evolution_method(
        target_id="text_memory",
        requested_method="text_memory_expel_reflector",
        prior_dataset_artifact_ids=("dataset-round-0",),
    ) == "text_memory_expel_reflector"
