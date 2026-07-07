from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Event

from fastapi.testclient import TestClient
import pytest

from openevo.sidecar import (
    DesktopExecutionStatus,
    build_desktop_shell_status,
    create_sidecar_app,
    create_sidecar_app_for_project,
    default_desktop_shell_status,
)
from openevo.science import ScienceProjectConfig
from openevo.sidecar import RemoteProfileConfig
from openevo.remote import RemoteCommandResult


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


def test_bootstrap_endpoint_requires_config_backed_session() -> None:
    client = TestClient(create_sidecar_app())
    token = _sidecar_token(client)

    response = client.post(
        "/openevo-api/desktop/bootstrap",
        headers={"X-OpenEvo-Sidecar-Token": token},
    )

    assert response.status_code == 409
    assert response.json()["detail"] == (
        "Desktop bootstrap requires a config-backed sidecar session."
    )


def test_bootstrap_endpoint_rejects_missing_sidecar_token() -> None:
    project = ScienceProjectConfig.model_validate(_science_project_payload())
    profile = _remote_profile()
    client = TestClient(
        create_sidecar_app_for_project(
            project,
            profile,
            transport_factory=lambda _profile: _ApiDryRunTransport(),
        )
    )

    response = client.post("/openevo-api/desktop/bootstrap")

    assert response.status_code == 403
    assert response.json()["detail"] == "Invalid OpenEvo sidecar token."


def test_bootstrap_endpoint_rejects_invalid_sidecar_token() -> None:
    project = ScienceProjectConfig.model_validate(_science_project_payload())
    profile = _remote_profile()
    client = TestClient(
        create_sidecar_app_for_project(
            project,
            profile,
            transport_factory=lambda _profile: _ApiDryRunTransport(),
        )
    )

    response = client.post(
        "/openevo-api/desktop/bootstrap",
        headers={"X-OpenEvo-Sidecar-Token": "wrong-token"},
    )

    assert response.status_code == 403
    assert response.json()["detail"] == "Invalid OpenEvo sidecar token."


def test_bootstrap_endpoint_runs_config_backed_dry_run_and_refreshes_status() -> None:
    project = ScienceProjectConfig.model_validate(_science_project_payload())
    profile = _remote_profile()
    client = TestClient(
        create_sidecar_app_for_project(
            project,
            profile,
            transport_factory=lambda _profile: _ApiDryRunTransport(),
        )
    )
    token = _sidecar_token(client)

    response = client.post(
        "/openevo-api/desktop/bootstrap",
        headers={"X-OpenEvo-Sidecar-Token": token},
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["bootstrap"]["ready"] is True
    assert payload["report"]["ready"] is True
    assert payload["report"]["prepared_paths"]["bootstrap_manifest"].endswith(
        "/bootstrap.json"
    )
    assert payload["status"]["bootstrap"]["ready"] is True
    assert payload["status"]["bootstrap"]["readiness_notes"] == [
        "Remote bootstrap is ready."
    ]
    services = {service["id"]: service for service in payload["status"]["services"]}
    assert services["ssh"]["state"] == "ready"
    assert services["ssh"]["detail"] == "Remote preflight passed"
    assert services["workspace"]["state"] == "ready"
    assert services["workspace"]["detail"] == "Workspace source is already remote"
    assert services["bootstrap"]["state"] == "ready"
    assert services["bootstrap"]["detail"] == "Runtime image and manifests prepared"

    status_response = client.get("/openevo-api/desktop/shell")
    assert status_response.json()["bootstrap"]["ready"] is True


def test_bootstrap_endpoint_preserves_failure_status() -> None:
    project = ScienceProjectConfig.model_validate(_science_project_payload())
    profile = _remote_profile()
    client = TestClient(
        create_sidecar_app_for_project(
            project,
            profile,
            transport_factory=lambda _profile: _FailingPreflightTransport(),
        )
    )
    token = _sidecar_token(client)

    response = client.post(
        "/openevo-api/desktop/bootstrap",
        headers={"X-OpenEvo-Sidecar-Token": token},
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["bootstrap"]["ready"] is False
    assert payload["report"]["ready"] is False
    assert payload["report"]["next_actions"] == [
        "Fix remote preflight failures and rerun bootstrap."
    ]
    services = {service["id"]: service for service in payload["status"]["services"]}
    assert services["ssh"] == {
        "id": "ssh",
        "label": "SSH transport",
        "state": "blocked",
        "detail": "Remote preflight failed",
    }
    assert services["bootstrap"] == {
        "id": "bootstrap",
        "label": "Bootstrap",
        "state": "blocked",
        "detail": "Fix remote preflight failures and rerun bootstrap.",
    }


def test_bootstrap_endpoint_rejects_concurrent_runs() -> None:
    project = ScienceProjectConfig.model_validate(_science_project_payload())
    profile = _remote_profile()
    transport = _BlockingTransport()
    client = TestClient(
        create_sidecar_app_for_project(
            project,
            profile,
            transport_factory=lambda _profile: transport,
        )
    )
    token = _sidecar_token(client)
    headers = {"X-OpenEvo-Sidecar-Token": token}

    with ThreadPoolExecutor(max_workers=1) as executor:
        first = executor.submit(
            client.post,
            "/openevo-api/desktop/bootstrap",
            headers=headers,
        )
        assert transport.started.wait(timeout=5)
        second = client.post("/openevo-api/desktop/bootstrap", headers=headers)
        transport.release.set()

    assert second.status_code == 409
    assert second.json()["detail"] == "Desktop bootstrap is already running."
    assert first.result(timeout=5).status_code == 200


def test_bootstrap_endpoint_keeps_unprepared_workspace_planned(
    tmp_path: Path,
) -> None:
    local_source = tmp_path / "workflow"
    local_source.mkdir()
    project = ScienceProjectConfig.model_validate(
        _science_project_payload()
        | {
            "task": {
                "id": "local-workflow",
                "objective": "Run the local workflow.",
                "source": {"type": "local_folder", "path": str(local_source)},
            }
        }
    )
    profile = _remote_profile()
    client = TestClient(
        create_sidecar_app_for_project(
            project,
            profile,
            transport_factory=lambda _profile: _ApiDryRunTransport(),
        )
    )
    token = _sidecar_token(client)

    response = client.post(
        "/openevo-api/desktop/bootstrap",
        headers={"X-OpenEvo-Sidecar-Token": token},
    )

    assert response.status_code == 200
    services = {
        service["id"]: service for service in response.json()["status"]["services"]
    }
    assert services["bootstrap"]["state"] == "ready"
    assert services["workspace"] == {
        "id": "workspace",
        "label": "Workspace",
        "state": "planned",
        "detail": "Workspace preparation has not run yet",
    }


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


def _sidecar_token(client: TestClient) -> str:
    payload = client.get("/openevo-api/desktop/shell").json()
    token = payload["sidecar"]["mutation_token"]
    assert token
    return token


class _ApiDryRunTransport:
    def run(
        self,
        command: str,
        *,
        cwd: str | None = None,
        env: dict[str, str] | None = None,
        timeout_seconds: float = 30.0,
    ) -> RemoteCommandResult:
        if command == 'df -Pk "$HOME"':
            return RemoteCommandResult(
                command=command,
                return_code=0,
                stdout=(
                    "Filesystem 1024-blocks Used Available Capacity Mounted on\n"
                    "/dev/root 100000000 1 99999999 1% /home\n"
                ),
            )
        return RemoteCommandResult(command=command, return_code=0, stdout="ok")

    def upload_dir(self, local_path: str, remote_path: str) -> None:
        return None


class _FailingPreflightTransport(_ApiDryRunTransport):
    def run(
        self,
        command: str,
        *,
        cwd: str | None = None,
        env: dict[str, str] | None = None,
        timeout_seconds: float = 30.0,
    ) -> RemoteCommandResult:
        if command == "true":
            return RemoteCommandResult(
                command=command,
                return_code=1,
                stderr="ssh unavailable",
            )
        return super().run(
            command,
            cwd=cwd,
            env=env,
            timeout_seconds=timeout_seconds,
        )


class _BlockingTransport(_ApiDryRunTransport):
    def __init__(self) -> None:
        self.started = Event()
        self.release = Event()

    def run(
        self,
        command: str,
        *,
        cwd: str | None = None,
        env: dict[str, str] | None = None,
        timeout_seconds: float = 30.0,
    ) -> RemoteCommandResult:
        if command == "true" and not self.started.is_set():
            self.started.set()
            if not self.release.wait(timeout=5):
                raise TimeoutError("bootstrap concurrency test timed out")
        return super().run(
            command,
            cwd=cwd,
            env=env,
            timeout_seconds=timeout_seconds,
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
