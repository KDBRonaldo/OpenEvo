from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Event

from fastapi.testclient import TestClient
import pytest
import yaml

import openevo.sidecar.api as sidecar_api
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


def test_workspace_endpoint_requires_config_backed_session() -> None:
    client = TestClient(create_sidecar_app())
    token = _sidecar_token(client)

    response = client.post(
        "/openevo-api/desktop/workspace",
        headers={"X-OpenEvo-Sidecar-Token": token},
    )

    assert response.status_code == 409
    assert response.json()["detail"] == (
        "Desktop workspace sync requires a config-backed sidecar session."
    )


def test_workspace_endpoint_rejects_missing_sidecar_token() -> None:
    project = ScienceProjectConfig.model_validate(_science_project_payload())
    profile = _remote_profile()
    client = TestClient(
        create_sidecar_app_for_project(
            project,
            profile,
            transport_factory=lambda _profile: _ApiDryRunTransport(),
        )
    )

    response = client.post("/openevo-api/desktop/workspace")

    assert response.status_code == 403
    assert response.json()["detail"] == "Invalid OpenEvo sidecar token."


def test_workspace_endpoint_rejects_invalid_sidecar_token() -> None:
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
        "/openevo-api/desktop/workspace",
        headers={"X-OpenEvo-Sidecar-Token": "wrong-token"},
    )

    assert response.status_code == 403
    assert response.json()["detail"] == "Invalid OpenEvo sidecar token."


def test_workspace_endpoint_marks_remote_path_ready() -> None:
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
        "/openevo-api/desktop/workspace",
        headers={"X-OpenEvo-Sidecar-Token": token},
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["workspace"]["ready"] is True
    assert payload["workspace"]["actions"][0]["type"] == "use_remote_path"
    assert payload["report"]["ready"] is True
    assert payload["report"]["remote_profile_id"] == "science-team"
    assert payload["report"]["task_id"] == "folding-baseline"
    services = {service["id"]: service for service in payload["status"]["services"]}
    assert services["workspace"] == {
        "id": "workspace",
        "label": "Workspace",
        "state": "ready",
        "detail": "Workspace source is already remote",
    }


def test_workspace_endpoint_uploads_local_folder_and_refreshes_status(
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
    transport = _ApiDryRunTransport()
    client = TestClient(
        create_sidecar_app_for_project(
            project,
            profile,
            transport_factory=lambda _profile: transport,
        )
    )
    token = _sidecar_token(client)

    response = client.post(
        "/openevo-api/desktop/workspace",
        headers={"X-OpenEvo-Sidecar-Token": token},
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["workspace"]["ready"] is True
    assert transport.uploads[0][0] == str(local_source)
    services = {service["id"]: service for service in payload["status"]["services"]}
    assert services["workspace"] == {
        "id": "workspace",
        "label": "Workspace",
        "state": "ready",
        "detail": "Workspace prepared",
    }


def test_workspace_endpoint_preserves_upload_failure_status(
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
            transport_factory=lambda _profile: _FailingUploadTransport(),
        )
    )
    token = _sidecar_token(client)

    response = client.post(
        "/openevo-api/desktop/workspace",
        headers={"X-OpenEvo-Sidecar-Token": token},
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["workspace"]["ready"] is False
    assert payload["report"]["ready"] is False
    assert payload["report"]["workspace"]["actions"][0]["stderr"] == "upload failed"
    services = {service["id"]: service for service in payload["status"]["services"]}
    assert services["workspace"] == {
        "id": "workspace",
        "label": "Workspace",
        "state": "blocked",
        "detail": "upload failed",
    }


def test_workspace_endpoint_rejects_concurrent_runs(tmp_path: Path) -> None:
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
    transport = _BlockingWorkspaceTransport()
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
            "/openevo-api/desktop/workspace",
            headers=headers,
        )
        assert transport.started.wait(timeout=5)
        second = client.post("/openevo-api/desktop/workspace", headers=headers)
        transport.release.set()

    assert second.status_code == 409
    assert second.json()["detail"] == "Desktop workspace sync is already running."
    assert first.result(timeout=5).status_code == 200


def test_workspace_endpoint_preserves_preflight_failure_report() -> None:
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
        "/openevo-api/desktop/workspace",
        headers={"X-OpenEvo-Sidecar-Token": token},
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["workspace"] == {"actions": [], "ready": False}
    assert payload["report"]["ready"] is False
    assert payload["report"]["preflight"]["checks"][0]["name"] == "ssh"
    assert payload["report"]["preflight"]["checks"][0]["status"] == "fail"
    services = {service["id"]: service for service in payload["status"]["services"]}
    assert services["ssh"]["state"] == "blocked"
    assert services["workspace"] == {
        "id": "workspace",
        "label": "Workspace",
        "state": "blocked",
        "detail": "Remote preflight failed",
    }


def test_workspace_and_bootstrap_concurrent_status_updates_do_not_clobber(
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
    transport = _BlockingWorkspaceTransport()
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
        workspace = executor.submit(
            client.post,
            "/openevo-api/desktop/workspace",
            headers=headers,
        )
        assert transport.started.wait(timeout=5)
        bootstrap = client.post("/openevo-api/desktop/bootstrap", headers=headers)
        transport.release.set()

    assert bootstrap.status_code == 200
    assert workspace.result(timeout=5).status_code == 200
    status_response = client.get("/openevo-api/desktop/shell")
    services = {
        service["id"]: service for service in status_response.json()["services"]
    }
    assert services["workspace"]["state"] == "ready"
    assert services["bootstrap"]["state"] == "ready"


def test_project_config_endpoint_rejects_missing_sidecar_token(tmp_path: Path) -> None:
    client = TestClient(
        create_sidecar_app(
            config_root=tmp_path,
            transport_factory=lambda _profile: _ApiDryRunTransport(),
        )
    )

    response = client.post(
        "/openevo-api/desktop/project-config",
        json=_desktop_config_draft_payload(),
    )

    assert response.status_code == 403
    assert response.json()["detail"] == "Invalid OpenEvo sidecar token."


def test_project_config_endpoint_saves_config_and_enables_workspace(
    tmp_path: Path,
) -> None:
    client = TestClient(
        create_sidecar_app(
            config_root=tmp_path,
            transport_factory=lambda _profile: _ApiDryRunTransport(),
        )
    )
    token = _sidecar_token(client)
    headers = {"X-OpenEvo-Sidecar-Token": token}

    response = client.post(
        "/openevo-api/desktop/project-config",
        headers=headers,
        json=_desktop_config_draft_payload(),
    )

    assert response.status_code == 200
    payload = response.json()
    science_path = Path(payload["config"]["science_config_path"])
    profile_path = Path(payload["config"]["remote_profile_path"])
    assert science_path == tmp_path / "projects" / "protein-design" / "science.yaml"
    assert profile_path == tmp_path / "profiles" / "science-team.yaml"
    assert science_path.is_file()
    assert profile_path.is_file()
    science_yaml = yaml.safe_load(science_path.read_text(encoding="utf-8"))
    profile_yaml = yaml.safe_load(profile_path.read_text(encoding="utf-8"))
    assert science_yaml["project"]["name"] == "Protein Design"
    assert profile_yaml["host"] == "gpu.example.edu"
    assert payload["status"]["project"]["name"] == "Protein Design"
    assert payload["status"]["remote"]["host"] == "gpu.example.edu"

    workspace = client.post("/openevo-api/desktop/workspace", headers=headers)
    assert workspace.status_code == 200
    assert workspace.json()["workspace"]["ready"] is True


def test_project_config_endpoint_returns_status_for_submitted_draft_when_overlapped(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first_status_requested = Event()
    release_first_status = Event()
    original_session = sidecar_api.OpenEvoSidecarSession
    session_count = 0

    class _BlockingStatusLock:
        def __enter__(self):
            first_status_requested.set()
            if not release_first_status.wait(timeout=5):
                raise TimeoutError("project config status test timed out")
            return self

        def __exit__(self, exc_type, exc, traceback):
            return False

    def session_factory(*args, **kwargs):
        nonlocal session_count
        session_count += 1
        created = original_session(*args, **kwargs)
        if session_count == 1:
            created.status_lock = _BlockingStatusLock()
        return created

    monkeypatch.setattr(sidecar_api, "OpenEvoSidecarSession", session_factory)
    client = TestClient(
        sidecar_api.create_sidecar_app(
            config_root=tmp_path,
            transport_factory=lambda _profile: _ApiDryRunTransport(),
        )
    )
    token = _sidecar_token(client)
    headers = {"X-OpenEvo-Sidecar-Token": token}
    first_draft = _desktop_config_draft_payload() | {
        "project_name": "First Project",
        "task_id": "first-task",
        "remote_profile_id": "first-team",
    }
    second_draft = _desktop_config_draft_payload() | {
        "project_name": "Second Project",
        "task_id": "second-task",
        "remote_profile_id": "second-team",
        "remote_host": "gpu2.example.edu",
    }

    with ThreadPoolExecutor(max_workers=1) as executor:
        first = executor.submit(
            client.post,
            "/openevo-api/desktop/project-config",
            headers=headers,
            json=first_draft,
        )
        assert first_status_requested.wait(timeout=5)
        second = client.post(
            "/openevo-api/desktop/project-config",
            headers=headers,
            json=second_draft,
        )
        release_first_status.set()

    first_response = first.result(timeout=5)
    assert second.status_code == 200
    assert first_response.status_code == 200
    assert second.json()["status"]["project"]["name"] == "Second Project"
    assert first_response.json()["status"]["project"]["name"] == "First Project"


def test_project_config_endpoint_rejects_invalid_draft(tmp_path: Path) -> None:
    client = TestClient(
        create_sidecar_app(
            config_root=tmp_path,
            transport_factory=lambda _profile: _ApiDryRunTransport(),
        )
    )
    token = _sidecar_token(client)

    response = client.post(
        "/openevo-api/desktop/project-config",
        headers={"X-OpenEvo-Sidecar-Token": token},
        json=_desktop_config_draft_payload() | {"remote_host": " "},
    )

    assert response.status_code == 422
    assert not (tmp_path / "projects").exists()
    assert not (tmp_path / "profiles").exists()


def test_project_config_endpoint_returns_422_for_model_validation_failures(
    tmp_path: Path,
) -> None:
    client = TestClient(
        create_sidecar_app(
            config_root=tmp_path,
            transport_factory=lambda _profile: _ApiDryRunTransport(),
        )
    )
    token = _sidecar_token(client)

    response = client.post(
        "/openevo-api/desktop/project-config",
        headers={"X-OpenEvo-Sidecar-Token": token},
        json=_desktop_config_draft_payload() | {"task_id": "bad/task"},
    )

    assert response.status_code == 422
    assert not (tmp_path / "projects").exists()
    assert not (tmp_path / "profiles").exists()


def test_project_config_endpoint_rejects_raw_secret_extra(tmp_path: Path) -> None:
    client = TestClient(
        create_sidecar_app(
            config_root=tmp_path,
            transport_factory=lambda _profile: _ApiDryRunTransport(),
        )
    )
    token = _sidecar_token(client)

    response = client.post(
        "/openevo-api/desktop/project-config",
        headers={"X-OpenEvo-Sidecar-Token": token},
        json=_desktop_config_draft_payload() | {"password": "super-secret-value"},
    )

    assert response.status_code == 422
    assert "super-secret-value" not in json.dumps(response.json())
    assert not (tmp_path / "projects").exists()
    assert not (tmp_path / "profiles").exists()


def test_project_config_endpoint_rejects_while_workspace_is_running(
    tmp_path: Path,
) -> None:
    local_source = tmp_path / "workflow"
    local_source.mkdir()
    transport = _BlockingWorkspaceTransport()
    client = TestClient(
        create_sidecar_app(
            config_root=tmp_path / "config",
            transport_factory=lambda _profile: transport,
        )
    )
    token = _sidecar_token(client)
    headers = {"X-OpenEvo-Sidecar-Token": token}
    config = client.post(
        "/openevo-api/desktop/project-config",
        headers=headers,
        json=_desktop_config_draft_payload()
        | {"source_type": "local_folder", "source_path": str(local_source)},
    )
    assert config.status_code == 200

    with ThreadPoolExecutor(max_workers=1) as executor:
        workspace = executor.submit(
            client.post,
            "/openevo-api/desktop/workspace",
            headers=headers,
        )
        assert transport.started.wait(timeout=5)
        second = client.post(
            "/openevo-api/desktop/project-config",
            headers=headers,
            json=_desktop_config_draft_payload(),
        )
        transport.release.set()

    assert second.status_code == 409
    assert second.json()["detail"] == (
        "Desktop project config cannot run while another lifecycle action is running."
    )
    assert workspace.result(timeout=5).status_code == 200


def test_run_endpoint_requires_config_backed_session() -> None:
    client = TestClient(create_sidecar_app())
    token = _sidecar_token(client)

    response = client.post(
        "/openevo-api/desktop/run",
        headers={"X-OpenEvo-Sidecar-Token": token},
    )

    assert response.status_code == 409
    assert response.json()["detail"] == (
        "Desktop run launch requires a config-backed sidecar session."
    )


def test_run_endpoint_rejects_missing_sidecar_token() -> None:
    project = ScienceProjectConfig.model_validate(_science_project_payload())
    profile = _remote_profile()
    client = TestClient(
        create_sidecar_app_for_project(
            project,
            profile,
            transport_factory=lambda _profile: _ApiDryRunTransport(),
        )
    )

    response = client.post("/openevo-api/desktop/run")

    assert response.status_code == 403
    assert response.json()["detail"] == "Invalid OpenEvo sidecar token."


def test_run_endpoint_rejects_invalid_sidecar_token() -> None:
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
        "/openevo-api/desktop/run",
        headers={"X-OpenEvo-Sidecar-Token": "wrong-token"},
    )

    assert response.status_code == 403
    assert response.json()["detail"] == "Invalid OpenEvo sidecar token."


def test_run_endpoint_rejects_when_not_ready() -> None:
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
        "/openevo-api/desktop/run",
        headers={"X-OpenEvo-Sidecar-Token": token},
    )

    assert response.status_code == 409
    assert response.json()["detail"] == (
        "Desktop run launch requires ready workspace and bootstrap."
    )


def test_run_endpoint_launches_after_workspace_and_bootstrap() -> None:
    project = ScienceProjectConfig.model_validate(_science_project_payload())
    profile = _remote_profile()
    transport = _ApiDryRunTransport()
    client = TestClient(
        create_sidecar_app_for_project(
            project,
            profile,
            transport_factory=lambda _profile: transport,
        )
    )
    token = _sidecar_token(client)
    headers = {"X-OpenEvo-Sidecar-Token": token}
    bootstrap = _prepare_workspace_and_bootstrap(client, headers)

    response = client.post("/openevo-api/desktop/run", headers=headers)

    assert response.status_code == 200
    payload = response.json()
    state_root = bootstrap["report"]["prepared_paths"]["state_root"]
    experiment_snapshot = bootstrap["report"]["prepared_paths"]["experiment_snapshot"]
    output_dir = f"{state_root}/runs/latest"
    expected_command = (
        f"openevo run {experiment_snapshot} "
        f"--output-dir {output_dir} --json"
    )
    assert payload["run"]["ready"] is True
    assert payload["run"]["status"] == "pass"
    assert payload["run"]["command"] == expected_command
    assert payload["run"]["output_dir"] == output_dir
    assert payload["run"]["experiment_snapshot"] == experiment_snapshot
    assert payload["run"]["command"] in transport.commands
    assert transport.run_calls[-1] == (expected_command, state_root, 86400.0)
    services = {service["id"]: service for service in payload["status"]["services"]}
    assert services["openevo-backend"] == {
        "id": "openevo-backend",
        "label": "OpenEvo backend",
        "state": "ready",
        "detail": "Last run completed",
    }
    evolution = {step["id"]: step for step in payload["status"]["evolution"]}
    assert evolution["transcript"]["state"] == "complete"
    assert evolution["transcript"]["detail"] == "Run completed and transcript captured"


def test_run_endpoint_preserves_command_failure_status() -> None:
    project = ScienceProjectConfig.model_validate(_science_project_payload())
    profile = _remote_profile()
    transport = _FailingRunTransport()
    client = TestClient(
        create_sidecar_app_for_project(
            project,
            profile,
            transport_factory=lambda _profile: transport,
        )
    )
    token = _sidecar_token(client)
    headers = {"X-OpenEvo-Sidecar-Token": token}
    _prepare_workspace_and_bootstrap(client, headers)

    response = client.post("/openevo-api/desktop/run", headers=headers)

    assert response.status_code == 200
    payload = response.json()
    assert payload["run"]["ready"] is False
    assert payload["run"]["status"] == "fail"
    assert payload["run"]["stderr"] == "run failed"
    services = {service["id"]: service for service in payload["status"]["services"]}
    assert services["openevo-backend"] == {
        "id": "openevo-backend",
        "label": "OpenEvo backend",
        "state": "blocked",
        "detail": "run failed",
    }


def test_run_endpoint_rejects_concurrent_runs() -> None:
    project = ScienceProjectConfig.model_validate(_science_project_payload())
    profile = _remote_profile()
    transport = _BlockingRunTransport()
    client = TestClient(
        create_sidecar_app_for_project(
            project,
            profile,
            transport_factory=lambda _profile: transport,
        )
    )
    token = _sidecar_token(client)
    headers = {"X-OpenEvo-Sidecar-Token": token}
    _prepare_workspace_and_bootstrap(client, headers)

    with ThreadPoolExecutor(max_workers=1) as executor:
        first = executor.submit(
            client.post,
            "/openevo-api/desktop/run",
            headers=headers,
        )
        assert transport.run_started.wait(timeout=5)
        second = client.post("/openevo-api/desktop/run", headers=headers)
        transport.run_release.set()

    assert second.status_code == 409
    assert second.json()["detail"] == "Desktop run launch is already running."
    assert first.result(timeout=5).status_code == 200


def test_run_response_schema_has_structured_report_contract() -> None:
    client = TestClient(create_sidecar_app())

    schema = client.get("/openapi.json").json()

    response_schema = schema["components"]["schemas"]["OpenEvoDesktopRunResponse"]
    run_ref = response_schema["properties"]["run"]["$ref"]
    assert run_ref == "#/components/schemas/OpenEvoDesktopRunReport"
    run_schema = schema["components"]["schemas"]["OpenEvoDesktopRunReport"]
    assert set(run_schema["required"]) == {
        "ready",
        "status",
        "command",
        "return_code",
        "stdout",
        "stderr",
        "output_dir",
        "experiment_snapshot",
        "started_at",
    }
    assert run_schema["properties"]["status"]["enum"] == ["pass", "fail"]
    assert run_schema["properties"]["output_dir"]["type"] == "string"


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


def _desktop_config_draft_payload() -> dict:
    return {
        "project_name": "Protein Design",
        "task_id": "folding-baseline",
        "objective": "Improve the folding baseline.",
        "source_type": "remote_path",
        "source_path": "/datasets/folding-baseline",
        "remote_profile_id": "science-team",
        "remote_host": "gpu.example.edu",
        "remote_port": 22,
        "remote_user": "alice",
        "auth_method": "ssh_agent",
        "https_proxy": "http://127.0.0.1:7890",
        "huggingface_endpoint": "https://hf-mirror.com",
        "codex_model": "gpt-5.1-codex-mini",
        "text_memory": True,
        "skill_bundle": True,
        "agent_system": True,
    }


def _prepare_workspace_and_bootstrap(
    client: TestClient,
    headers: dict[str, str],
) -> dict:
    workspace = client.post("/openevo-api/desktop/workspace", headers=headers)
    assert workspace.status_code == 200
    bootstrap = client.post("/openevo-api/desktop/bootstrap", headers=headers)
    assert bootstrap.status_code == 200
    return bootstrap.json()


class _ApiDryRunTransport:
    def __init__(self) -> None:
        self.uploads: list[tuple[str, str]] = []
        self.commands: list[str] = []
        self.run_calls: list[tuple[str, str | None, float]] = []

    def run(
        self,
        command: str,
        *,
        cwd: str | None = None,
        env: dict[str, str] | None = None,
        timeout_seconds: float = 30.0,
    ) -> RemoteCommandResult:
        self.commands.append(command)
        self.run_calls.append((command, cwd, timeout_seconds))
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
        self.uploads.append((local_path, remote_path))
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


class _FailingUploadTransport(_ApiDryRunTransport):
    def upload_dir(self, local_path: str, remote_path: str) -> None:
        self.uploads.append((local_path, remote_path))
        raise RuntimeError("upload failed")


class _FailingRunTransport(_ApiDryRunTransport):
    def run(
        self,
        command: str,
        *,
        cwd: str | None = None,
        env: dict[str, str] | None = None,
        timeout_seconds: float = 30.0,
    ) -> RemoteCommandResult:
        if command.startswith("openevo run "):
            self.commands.append(command)
            return RemoteCommandResult(
                command=command,
                return_code=2,
                stderr="run failed",
            )
        return super().run(
            command,
            cwd=cwd,
            env=env,
            timeout_seconds=timeout_seconds,
        )


class _BlockingTransport(_ApiDryRunTransport):
    def __init__(self) -> None:
        super().__init__()
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


class _BlockingWorkspaceTransport(_ApiDryRunTransport):
    def __init__(self) -> None:
        super().__init__()
        self.started = Event()
        self.release = Event()

    def upload_dir(self, local_path: str, remote_path: str) -> None:
        self.started.set()
        if not self.release.wait(timeout=5):
            raise TimeoutError("workspace concurrency test timed out")
        super().upload_dir(local_path, remote_path)


class _BlockingRunTransport(_ApiDryRunTransport):
    def __init__(self) -> None:
        super().__init__()
        self.run_started = Event()
        self.run_release = Event()

    def run(
        self,
        command: str,
        *,
        cwd: str | None = None,
        env: dict[str, str] | None = None,
        timeout_seconds: float = 30.0,
    ) -> RemoteCommandResult:
        if command.startswith("openevo run "):
            self.commands.append(command)
            self.run_started.set()
            if not self.run_release.wait(timeout=5):
                raise TimeoutError("run concurrency test timed out")
            return RemoteCommandResult(command=command, return_code=0, stdout="ok")
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
