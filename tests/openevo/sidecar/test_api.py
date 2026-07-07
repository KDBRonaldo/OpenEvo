from __future__ import annotations

from fastapi.testclient import TestClient
import pytest

from openevo.sidecar import (
    DesktopExecutionStatus,
    build_desktop_shell_status,
    create_sidecar_app,
    default_desktop_shell_status,
)
from openevo.science import ScienceProjectConfig
from openevo.sidecar import RemoteProfileConfig


def test_sidecar_health_endpoint() -> None:
    client = TestClient(create_sidecar_app())

    response = client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"service": "openevo-sidecar", "status": "ok"}


def test_desktop_shell_endpoint_preserves_subscription_readiness() -> None:
    client = TestClient(create_sidecar_app())

    response = client.get("/openevo-api/desktop/shell")

    assert response.status_code == 200
    payload = response.json()
    assert payload["remote"]["id"] == "lab-gpu"
    assert payload["execution"]["mode"] == "codex_subscription_transcript"
    assert payload["execution"]["token_metrics_available"] is False
    assert payload["bootstrap"]["ready"] is True
    assert payload["bootstrap"]["readiness_notes"] == [
        "Codex subscription login available"
    ]


def test_default_desktop_status_round_trips_as_json() -> None:
    status = default_desktop_shell_status()

    restored = type(status).model_validate(status.model_dump(mode="json"))

    assert restored == status


def test_subscription_transcript_status_rejects_token_metrics() -> None:
    with pytest.raises(ValueError, match="token_metrics_available"):
        DesktopExecutionStatus(
            mode="codex_subscription_transcript",
            model="gpt-5.1-codex-mini",
            token_metrics_available=True,
        )


def _science_project_payload() -> dict:
    return {
        "version": 1,
        "project": {"name": "protein-design"},
        "remote_profile": "science-team",
        "task": {
            "id": "folding-baseline",
            "objective": "Improve the folding baseline.",
            "source": {
                "type": "remote_path",
                "path": "/datasets/folding-baseline",
            },
        },
    }


def _remote_profile() -> RemoteProfileConfig:
    return RemoteProfileConfig(
        version=1,
        id="science-team",
        host="gpu.example.edu",
        user="alice",
        proxy={"https_proxy": "http://127.0.0.1:7890"},
    )


def test_build_desktop_shell_status_from_subscription_project() -> None:
    project = ScienceProjectConfig.model_validate(_science_project_payload())
    profile = _remote_profile()

    status = build_desktop_shell_status(project, profile)

    assert status.remote.id == "science-team"
    assert status.remote.proxy.https_proxy == "http://127.0.0.1:7890"
    assert status.project.task_id == "folding-baseline"
    assert status.project.source == "Remote path: /datasets/folding-baseline"
    assert status.execution.mode == "codex_subscription_transcript"
    assert status.execution.model == "gpt-5.1-codex-mini"
    assert status.execution.token_metrics_available is False
    assert status.bootstrap.ready is False
    assert status.bootstrap.workspace_root == "/home/alice/.openevo/workspaces"
    assert status.bootstrap.readiness_notes == ("Remote bootstrap has not run yet.",)
    assert status.services[1].state == "ready"
    assert status.services[-1].state == "planned"
    assert [step.id for step in status.evolution] == [
        "transcript",
        "text-memory",
        "skill-bundle",
        "agent-system",
    ]


def test_build_desktop_shell_status_from_managed_local_inference_project() -> None:
    project = ScienceProjectConfig.model_validate(
        _science_project_payload()
        | {
            "execution": {
                "mode": "codex_managed_local_inference",
                "hf_model": "Qwen/Qwen2.5-7B-Instruct",
            },
            "task": {
                "id": "local-task",
                "objective": "Run the local workflow.",
                "source": {"type": "local_folder", "path": "workflows/local-task"},
            },
        }
    )
    profile = _remote_profile()

    status = build_desktop_shell_status(project, profile)

    assert status.execution.mode == "codex_managed_local_inference"
    assert status.execution.model == "Qwen/Qwen2.5-7B-Instruct"
    assert status.execution.token_metrics_available is True
    assert status.services[1].state == "planned"
    assert status.project.source == "Local folder: workflows/local-task"


def test_build_desktop_shell_status_describes_scratch_workspace() -> None:
    project = ScienceProjectConfig.model_validate(
        _science_project_payload()
        | {
            "task": {
                "id": "scratch-task",
                "objective": "Start from an empty workspace.",
                "source": {"type": "scratch"},
            }
        }
    )
    profile = _remote_profile()

    status = build_desktop_shell_status(project, profile)

    assert status.project.source == "Scratch workspace"
    assert status.services[1].state == "ready"
    assert status.services[1].detail == "Scratch workspace does not need source preparation"
