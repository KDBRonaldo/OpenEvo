from __future__ import annotations

from pathlib import Path

import pytest

from openevo.backend.contracts.v1 import models as m
from openevo.backend.contracts.v1.store import CoreControlStoreV1
from openevo.backend.science_execution import compile_science_execution
from openevo.backend.service_supervisor import ServiceExecutionMode, ServiceRunBinding
from openevo.internal_auth import InternalServiceIdentity
from openevo.runtime.managed import MANAGED_HOME, MANAGED_PATH, MANAGED_RUNTIME_RELEASES


def _project(
    tmp_path: Path,
    *,
    execution_mode: m.ExecutionMode = m.ExecutionMode.CODEX_SUBSCRIPTION_TRANSCRIPT,
    capture_mode: m.CaptureMode = m.CaptureMode.TRANSCRIPT,
) -> m.ProjectV1:
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
    project = result.model
    if (
        execution_mode is m.ExecutionMode.CODEX_SUBSCRIPTION_TRANSCRIPT
        and capture_mode is m.CaptureMode.TRANSCRIPT
    ):
        return project
    payload = project.model_dump(mode="python")
    payload["execution_mode"] = execution_mode
    payload["spec"] = {
        **payload["spec"],
        "execution_mode": execution_mode,
        "capture_mode": capture_mode,
    }
    return m.ProjectV1.model_validate(payload)


def _binding(
    execution_mode: ServiceExecutionMode = ServiceExecutionMode.CODEX_SUBSCRIPTION_TRANSCRIPT,
) -> ServiceRunBinding:
    identity = InternalServiceIdentity(
        service_id="core-control",
        generation_digest="b" * 64,
        registry_digest="a" * 64,
        framework_lock_digest="c" * 64,
        credential="private-run-binding-credential-value-0123456789",
    )
    return ServiceRunBinding(
        execution_mode=execution_mode,
        runtime_image="openevo/science-runtime:0.1.0",
        runtime_image_immutable_reference=(
            MANAGED_RUNTIME_RELEASES["managed_science"].trusted_digest
        ),
        runtime_identity_digest="d" * 64,
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
    assert execution.config.agent.env == {}
    assert execution.config.agent.settings["capture_mode"] == "transcript"
    assert execution.config.runtime.profile == "managed_science"
    assert execution.config.runtime.image == (
        MANAGED_RUNTIME_RELEASES["managed_science"].trusted_digest
    )
    assert execution.config.runtime.container_user == "host"
    assert execution.config.runtime.env == {"HOME": MANAGED_HOME, "PATH": MANAGED_PATH}
    assert execution.config.runtime.prepare[0].command.startswith(
        f"mkdir -p {MANAGED_HOME}/.codex"
    )
    assert execution.config.tasks[0].instruction == project.task.objective
    assert execution.config.tasks[0].workspace is None
    assert execution.execution_profile.execution_mode == "subscription"
    assert execution.execution_profile.capture_mode == "transcript"
    assert execution.task_id.startswith("science-")
    assert execution.submitted_task_id == (
        f"{execution.task_id}--run-run-public-1--round-0"
    )


@pytest.mark.parametrize("capture_mode", [m.CaptureMode.TOKEN_LEVEL, m.CaptureMode.TRANSCRIPT])
def test_self_deployed_project_preserves_capture_mode_in_agent_and_profile(
    tmp_path: Path,
    capture_mode: m.CaptureMode,
) -> None:
    project = _project(
        tmp_path,
        execution_mode=m.ExecutionMode.SELF_DEPLOYED,
        capture_mode=capture_mode,
    )

    execution = compile_science_execution(
        project,
        run_id=f"run-{capture_mode.value}",
        binding=_binding(ServiceExecutionMode.SELF_DEPLOYED),
        workspace_path=None,
    )

    assert execution.config.agent.auth == "proxy"
    assert execution.config.agent.env == {"CODEX_HOME": f"{MANAGED_HOME}/.codex"}
    assert execution.config.agent.settings["auth_mode"] == "proxy"
    assert execution.config.agent.settings["capture_mode"] == capture_mode.value
    assert execution.config.runtime.profile == "managed_science"
    assert execution.config.runtime.container_user == "host"
    assert execution.config.runtime.env == {
        "HOME": MANAGED_HOME,
        "PATH": MANAGED_PATH,
        "OPENEVO_MANAGED_HF_MODEL": project.spec.agent_model_ref,
    }
    assert execution.execution_profile.execution_mode == "self_deployed"
    assert execution.execution_profile.capture_mode == capture_mode.value


def test_subscription_execution_rejects_non_transcript_capture(tmp_path: Path) -> None:
    project = _project(tmp_path)
    invalid_spec = project.spec.model_copy(update={"capture_mode": m.CaptureMode.TOKEN_LEVEL})
    invalid_project = project.model_copy(update={"spec": invalid_spec})

    with pytest.raises(ValueError, match="subscription.*transcript"):
        compile_science_execution(
            invalid_project,
            run_id="run-invalid-subscription-capture",
            binding=_binding(),
            workspace_path=None,
        )


def test_execution_rejects_service_binding_for_another_mode(tmp_path: Path) -> None:
    project = _project(tmp_path)

    with pytest.raises(ValueError, match="service execution modes differ"):
        compile_science_execution(
            project,
            run_id="run-mismatched-service-mode",
            binding=_binding(ServiceExecutionMode.SELF_DEPLOYED),
            workspace_path=None,
        )
