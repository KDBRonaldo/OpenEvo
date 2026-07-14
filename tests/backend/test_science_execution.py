from __future__ import annotations

from pathlib import Path

from openevo.backend.contracts.v1 import models as m
from openevo.backend.contracts.v1.store import CoreControlStoreV1
from openevo.backend.science_execution import compile_science_execution
from openevo.backend.service_supervisor import ServiceRunBinding
from openevo.internal_auth import InternalServiceIdentity


def _project(tmp_path: Path) -> m.ProjectV1:
    store = CoreControlStoreV1(tmp_path)
    result = store.create_project(
        m.ProjectCreateV1.model_validate(
            {
                "name": "Protein analysis",
                "spec": {
                    "execution_mode": "codex_subscription_transcript",
                    "capture_mode": "transcript",
                    "harness_id": "codex",
                    "agent_model_ref": "gpt-5.1-codex-mini",
                    "evolution": {"targets": {}},
                },
                "task": {
                    "title": "Analyze structure",
                    "objective": "Analyze the supplied protein structure and write findings.",
                },
                "workspace": {"kind": "scratch", "display_name": "Scratch"},
            }
        ),
        idempotency_key="create-project",
        registry_digest="a" * 64,
    )
    assert isinstance(result.model, m.ProjectV1)
    return result.model


def _binding() -> ServiceRunBinding:
    identity = InternalServiceIdentity(
        service_id="core-control",
        generation_digest="b" * 64,
        registry_digest="a" * 64,
        framework_lock_digest="c" * 64,
        credential="private-run-binding-credential-value-0123456789",
    )
    return ServiceRunBinding(
        generation_digest="b" * 64,
        registry_digest="a" * 64,
        framework_lock_digest="c" * 64,
        rollout_url="http://127.0.0.1:18100",
        evolution_backend_url="http://127.0.0.1:18200",
        gateway_url="http://127.0.0.1:18300",
        _identity=identity,
    )


def test_project_compiles_to_single_session_existing_experiment_path(tmp_path: Path) -> None:
    project = _project(tmp_path)

    execution = compile_science_execution(
        project,
        run_id="run-public-1",
        binding=_binding(),
        workspace_path=None,
    )

    assert execution.config.experiment.name == project.name
    assert execution.config.evolution.rounds == 1
    assert execution.config.rollout.url == "http://127.0.0.1:18100"
    assert execution.config.evolution.backend_url == "http://127.0.0.1:18200"
    assert execution.config.agent.auth == "subscription"
    assert execution.config.agent.settings["capture_mode"] == "transcript"
    assert execution.config.runtime.image == "openevo/science-runtime:0.1.0"
    assert execution.config.tasks[0].instruction == project.task.objective
    assert execution.config.tasks[0].workspace is None
    assert execution.execution_profile.execution_mode == "subscription"
    assert execution.execution_profile.capture_mode == "transcript"
    assert execution.task_id.startswith("science-")
    assert execution.submitted_task_id == (
        f"{execution.task_id}--run-run-public-1--round-0"
    )
