from __future__ import annotations

import json
import time
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


def test_sidecar_exposes_core_capabilities() -> None:
    client = TestClient(create_sidecar_app())

    response = client.get("/openevo-api/desktop/capabilities")

    assert response.status_code == 200
    payload = response.json()
    execution_modes = {item["mode"] for item in payload["execution_modes"]}
    assert {
        "codex_subscription_transcript",
        "self-deployed",
    }.issubset(execution_modes)
    artifact_targets = {item["artifact_type"] for item in payload["artifact_targets"]}
    assert {"text_memory", "skill_bundle", "agent_system"}.issubset(
        artifact_targets
    )
    methods = {
        item["method_id"]: item
        for item in payload["evolution_methods"]
    }
    text_memory_method = methods["text_memory_reflector"]
    assert text_memory_method["visibility"]
    assert isinstance(text_memory_method["default_config"], dict)
    assert isinstance(text_memory_method["config_schema"], dict)


def test_sidecar_exposes_methods_alias() -> None:
    client = TestClient(create_sidecar_app())

    response = client.get("/openevo-api/desktop/methods")

    assert response.status_code == 200
    payload = response.json()
    assert set(payload) == {"methods"}
    method_ids = {item["method_id"] for item in payload["methods"]}
    assert "text_memory_reflector" in method_ids


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


def test_desktop_shell_endpoint_reports_ssh_auth_capabilities() -> None:
    project = ScienceProjectConfig.model_validate(_science_project_payload())
    profile = _remote_profile()
    client = TestClient(
        create_sidecar_app_for_project(
            project,
            profile,
            transport_factory=lambda _profile: _ApiDryRunTransport(),
            transport_kind="ssh",
        )
    )

    response = client.get("/openevo-api/desktop/shell")

    assert response.status_code == 200
    payload = response.json()
    assert payload["sidecar"]["transport"] == {
        "id": "ssh",
        "label": "SSH transport",
        "supports_password_ref": False,
        "supports_passphrase_ref": False,
    }


@pytest.mark.parametrize(
    "endpoint",
    [
        "/openevo-api/desktop/workspace",
        "/openevo-api/desktop/bootstrap",
        "/openevo-api/desktop/run",
    ],
    ids=["workspace", "bootstrap", "run"],
)
@pytest.mark.parametrize(
    ("auth", "detail"),
    [
        (
            {"method": "password_ref", "password_ref": "keyring://openevo/team"},
            (
                "SSH transport cannot resolve password_ref yet. Use SSH agent "
                "or a private key without a secret reference."
            ),
        ),
        (
            {
                "method": "private_key",
                "private_key_path": "/home/alice/.ssh/openevo",
                "passphrase_ref": "keyring://openevo/team",
            },
            (
                "SSH transport cannot resolve passphrase_ref yet. Use SSH agent "
                "or a private key without a secret reference."
            ),
        ),
    ],
    ids=["password-ref", "passphrase-ref"],
)
def test_lifecycle_endpoints_reject_unsupported_ssh_secret_refs(
    endpoint: str,
    auth: dict,
    detail: str,
) -> None:
    project = ScienceProjectConfig.model_validate(_science_project_payload())
    profile = _remote_profile(auth=auth)
    transport = _ApiDryRunTransport()
    client = TestClient(
        create_sidecar_app_for_project(
            project,
            profile,
            transport_factory=lambda _profile: transport,
            transport_kind="ssh",
        )
    )
    token = _sidecar_token(client)

    response = client.post(
        endpoint,
        headers={"X-OpenEvo-Sidecar-Token": token},
    )

    assert response.status_code == 409
    assert response.json()["detail"] == detail
    assert transport.commands == []
    assert transport.uploads == []


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


def test_services_endpoint_requires_config_backed_session() -> None:
    client = TestClient(create_sidecar_app())
    token = _sidecar_token(client)

    response = client.post(
        "/openevo-api/desktop/services",
        headers={"X-OpenEvo-Sidecar-Token": token},
    )

    assert response.status_code == 409
    assert response.json()["detail"] == (
        "Desktop services require a config-backed sidecar session."
    )


def test_services_endpoint_rejects_until_bootstrap_ready() -> None:
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
        "/openevo-api/desktop/services",
        headers={"X-OpenEvo-Sidecar-Token": token},
    )

    assert response.status_code == 409
    assert response.json()["detail"] == (
        "Desktop services require ready workspace and bootstrap."
    )


def test_services_endpoint_starts_after_bootstrap_and_refreshes_status() -> None:
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
    _prepare_workspace_and_bootstrap(client, headers)

    response = client.post("/openevo-api/desktop/services", headers=headers)

    assert response.status_code == 200
    payload = response.json()
    assert payload["services"]["ready"] is True
    assert [step["id"] for step in payload["report"]["steps"]] == [
        "write_topology",
        "evolution_backend",
        "rollout",
        "gateway",
        "evolution_worker",
    ]
    assert any("polar serve_rollout" in command for command in transport.commands)
    assert any("polar serve_gateway" in command for command in transport.commands)
    assert any(
        "polar serve_rollout --config" in command
        for command in transport.commands
    )
    assert any(
        "polar serve_gateway --config" in command
        for command in transport.commands
    )
    services = {service["id"]: service for service in payload["status"]["services"]}
    assert services["openevo-backend"] == {
        "id": "openevo-backend",
        "label": "OpenEvo backend",
        "state": "ready",
        "detail": "Remote runtime services are ready",
    }


def test_services_endpoint_preserves_failure_status() -> None:
    project = ScienceProjectConfig.model_validate(_science_project_payload())
    profile = _remote_profile()
    transport = _FailingServicesTransport()
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

    response = client.post("/openevo-api/desktop/services", headers=headers)

    assert response.status_code == 200
    payload = response.json()
    assert payload["services"]["ready"] is False
    assert payload["report"]["next_actions"] == [
        "Fix remote service failure and restart services."
    ]
    rollout = next(
        step for step in payload["report"]["steps"] if step["id"] == "rollout"
    )
    assert rollout["status"] == "fail"
    assert rollout["stderr"] == "rollout failed"
    services = {service["id"]: service for service in payload["status"]["services"]}
    assert services["openevo-backend"] == {
        "id": "openevo-backend",
        "label": "OpenEvo backend",
        "state": "blocked",
        "detail": "rollout failed",
    }


def test_services_endpoint_rejects_concurrent_runs() -> None:
    project = ScienceProjectConfig.model_validate(_science_project_payload())
    profile = _remote_profile()
    transport = _BlockingServicesTransport()
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
            "/openevo-api/desktop/services",
            headers=headers,
        )
        assert transport.service_started.wait(timeout=5)
        second = client.post("/openevo-api/desktop/services", headers=headers)
        transport.service_release.set()

    assert first.result(timeout=5).status_code == 200
    assert second.status_code == 409
    assert second.json()["detail"] == "Desktop services are already running."


def test_services_status_endpoint_requires_token_and_ready_bootstrap() -> None:
    project = ScienceProjectConfig.model_validate(_science_project_payload())
    profile = _remote_profile()
    client = TestClient(
        create_sidecar_app_for_project(
            project,
            profile,
            transport_factory=lambda _profile: _ApiLifecycleTransport(),
        )
    )
    token = _sidecar_token(client)

    missing_token = client.get("/openevo-api/desktop/services/status")
    not_ready = client.get(
        "/openevo-api/desktop/services/status",
        headers={"X-OpenEvo-Sidecar-Token": token},
    )

    assert missing_token.status_code == 403
    assert missing_token.json()["detail"] == "Invalid OpenEvo sidecar token."
    assert not_ready.status_code == 409
    assert not_ready.json()["detail"] == (
        "Desktop services require ready workspace and bootstrap."
    )


def test_services_status_and_health_endpoints_inspect_remote_services() -> None:
    project = ScienceProjectConfig.model_validate(_science_project_payload())
    profile = _remote_profile()
    transport = _ApiLifecycleTransport(
        pid_states={
            "evolution_backend": {"pid": 120, "alive": True},
            "rollout": {"pid": 121, "alive": True},
            "gateway": {"pid": 122, "alive": True},
            "evolution_worker": {"pid": 123, "alive": True},
        }
    )
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

    status = client.get("/openevo-api/desktop/services/status", headers=headers)
    health = client.get("/openevo-api/desktop/services/health", headers=headers)

    assert status.status_code == 200
    assert status.json()["ready"] is True
    services = {service["service_id"]: service for service in status.json()["services"]}
    assert services["gateway"]["state"] == "ready"
    assert services["gateway"]["pid"] == 122
    assert health.status_code == 200
    assert health.json() == status.json()


def test_services_logs_endpoint_tails_selected_service_with_redaction() -> None:
    project = ScienceProjectConfig.model_validate(_science_project_payload())
    profile = _remote_profile()
    transport = _ApiLifecycleTransport(
        log_content=(
            "Authorization: Bearer secret-token\n"
            "Proxy http://proxy-user:proxy-secret@127.0.0.1:7890\n"
            "Gateway ready\n"
        )
    )
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

    response = client.get(
        "/openevo-api/desktop/services/logs",
        headers=headers,
        params={"service_id": "gateway", "lines": 50},
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["service_id"] == "gateway"
    assert payload["line_count"] == 3
    assert "Gateway ready" in payload["content"]
    assert "secret-token" not in payload["content"]
    assert "proxy-secret" not in payload["content"]
    assert "Authorization: [REDACTED]" in payload["content"]


def test_services_stop_and_restart_endpoints_control_selected_service() -> None:
    project = ScienceProjectConfig.model_validate(_science_project_payload())
    profile = _remote_profile()
    transport = _ApiLifecycleTransport(pid_states={"gateway": {"pid": 122, "alive": True}})
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

    stop = client.post(
        "/openevo-api/desktop/services/stop",
        headers=headers,
        json={"service_id": "gateway"},
    )
    transport.pid_states["gateway"] = {"pid": 124, "alive": True}
    restart = client.post(
        "/openevo-api/desktop/services/restart",
        headers=headers,
        json={"service_id": "gateway"},
    )

    assert stop.status_code == 200
    assert stop.json()["state"] == "stopped"
    assert restart.status_code == 200
    assert restart.json()["state"] == "ready"
    assert transport.stopped_services == ["gateway", "gateway"]
    assert any("polar serve_gateway" in command for command in transport.commands)
    assert not any("polar serve_rollout" in command for command in transport.commands)


def test_services_control_endpoint_rejects_unknown_service_id() -> None:
    project = ScienceProjectConfig.model_validate(_science_project_payload())
    profile = _remote_profile()
    client = TestClient(
        create_sidecar_app_for_project(
            project,
            profile,
            transport_factory=lambda _profile: _ApiLifecycleTransport(),
        )
    )
    token = _sidecar_token(client)
    headers = {"X-OpenEvo-Sidecar-Token": token}
    _prepare_workspace_and_bootstrap(client, headers)

    response = client.post(
        "/openevo-api/desktop/services/stop",
        headers=headers,
        json={"service_id": "write_topology"},
    )

    assert response.status_code == 404
    assert response.json()["detail"] == "Unknown remote service id: write_topology"


def test_run_rejects_while_services_restart_is_running() -> None:
    project = ScienceProjectConfig.model_validate(_science_project_payload())
    profile = _remote_profile()
    transport = _BlockingSecondServicesTransport()
    client = TestClient(
        create_sidecar_app_for_project(
            project,
            profile,
            transport_factory=lambda _profile: transport,
        )
    )
    token = _sidecar_token(client)
    headers = {"X-OpenEvo-Sidecar-Token": token}
    _prepare_workspace_bootstrap_and_services(client, headers)

    with ThreadPoolExecutor(max_workers=1) as executor:
        services_restart = executor.submit(
            client.post,
            "/openevo-api/desktop/services",
            headers=headers,
        )
        assert transport.second_service_started.wait(timeout=5)
        run = client.post("/openevo-api/desktop/run", headers=headers)
        transport.second_service_release.set()

    assert services_restart.result(timeout=5).status_code == 200
    assert run.status_code == 409
    assert run.json()["detail"] == (
        "Desktop run launch requires ready workspace, bootstrap, and services."
    )


def test_bootstrap_rejects_while_services_are_running() -> None:
    project = ScienceProjectConfig.model_validate(_science_project_payload())
    profile = _remote_profile()
    transport = _BlockingSecondServicesTransport()
    client = TestClient(
        create_sidecar_app_for_project(
            project,
            profile,
            transport_factory=lambda _profile: transport,
        )
    )
    token = _sidecar_token(client)
    headers = {"X-OpenEvo-Sidecar-Token": token}
    _prepare_workspace_bootstrap_and_services(client, headers)

    with ThreadPoolExecutor(max_workers=1) as executor:
        services_restart = executor.submit(
            client.post,
            "/openevo-api/desktop/services",
            headers=headers,
        )
        assert transport.second_service_started.wait(timeout=5)
        bootstrap = client.post("/openevo-api/desktop/bootstrap", headers=headers)
        transport.second_service_release.set()

    assert services_restart.result(timeout=5).status_code == 200
    assert bootstrap.status_code == 409
    assert bootstrap.json()["detail"] == (
        "Desktop bootstrap cannot start while another lifecycle action is running."
    )


def test_bootstrap_clears_previous_service_readiness() -> None:
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
    headers = {"X-OpenEvo-Sidecar-Token": token}
    _prepare_workspace_bootstrap_and_services(client, headers)

    bootstrap = client.post("/openevo-api/desktop/bootstrap", headers=headers)
    run = client.post("/openevo-api/desktop/run", headers=headers)

    assert bootstrap.status_code == 200
    services = {service["id"]: service for service in bootstrap.json()["status"]["services"]}
    assert services["openevo-backend"] == {
        "id": "openevo-backend",
        "label": "OpenEvo backend",
        "state": "planned",
        "detail": "Remote runtime services have not started",
    }
    assert run.status_code == 409
    assert run.json()["detail"] == (
        "Desktop run launch requires ready workspace, bootstrap, and services."
    )


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


def test_bootstrap_rejects_while_workspace_sync_is_running(
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

    assert bootstrap.status_code == 409
    assert bootstrap.json()["detail"] == (
        "Desktop bootstrap cannot start while another lifecycle action is running."
    )
    assert workspace.result(timeout=5).status_code == 200
    status_response = client.get("/openevo-api/desktop/shell")
    services = {
        service["id"]: service for service in status_response.json()["services"]
    }
    assert services["workspace"]["state"] == "ready"
    assert services["bootstrap"]["state"] == "planned"


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


def test_project_config_catalog_lists_saved_configs_after_restart(
    tmp_path: Path,
) -> None:
    writer = TestClient(
        create_sidecar_app(
            config_root=tmp_path,
            transport_factory=lambda _profile: _ApiDryRunTransport(),
        )
    )
    writer_token = _sidecar_token(writer)
    saved = writer.post(
        "/openevo-api/desktop/project-config",
        headers={"X-OpenEvo-Sidecar-Token": writer_token},
        json=_desktop_config_draft_payload(),
    )
    assert saved.status_code == 200
    client = TestClient(
        create_sidecar_app(
            config_root=tmp_path,
            transport_factory=lambda _profile: _ApiDryRunTransport(),
        )
    )

    response = client.get("/openevo-api/desktop/project-configs")

    assert response.status_code == 200
    payload = response.json()
    assert len(payload["configs"]) == 1
    summary = payload["configs"][0]
    assert summary["project_slug"] == "protein-design"
    assert summary["valid"] is True
    assert summary["error"] is None
    assert summary["project_name"] == "Protein Design"
    assert summary["task_id"] == "folding-baseline"
    assert summary["source_type"] == "remote_path"
    assert summary["source_label"] == "/datasets/folding-baseline"
    assert summary["remote_profile_id"] == "science-team"
    assert summary["remote_host"] == "gpu.example.edu"
    assert summary["remote_user"] == "alice"
    assert "password" not in json.dumps(payload)
    assert "private_key_path" not in json.dumps(payload)


def test_project_config_catalog_requires_config_root() -> None:
    client = TestClient(create_sidecar_app())

    response = client.get("/openevo-api/desktop/project-configs")

    assert response.status_code == 409
    assert response.json()["detail"] == (
        "Desktop project config catalog requires a writable config root."
    )


def test_project_config_activate_loads_saved_config_after_restart(
    tmp_path: Path,
) -> None:
    writer = TestClient(
        create_sidecar_app(
            config_root=tmp_path,
            transport_factory=lambda _profile: _ApiDryRunTransport(),
        )
    )
    writer_token = _sidecar_token(writer)
    saved = writer.post(
        "/openevo-api/desktop/project-config",
        headers={"X-OpenEvo-Sidecar-Token": writer_token},
        json=_desktop_config_draft_payload(),
    )
    assert saved.status_code == 200
    transport = _ApiDryRunTransport()
    client = TestClient(
        create_sidecar_app(
            config_root=tmp_path,
            transport_factory=lambda _profile: transport,
        )
    )
    token = _sidecar_token(client)
    headers = {"X-OpenEvo-Sidecar-Token": token}

    activate = client.post(
        "/openevo-api/desktop/project-configs/protein-design/activate",
        headers=headers,
    )

    assert activate.status_code == 200
    payload = activate.json()
    assert payload["status"]["project"]["name"] == "Protein Design"
    assert payload["status"]["remote"]["host"] == "gpu.example.edu"
    assert payload["config"]["science_config_path"].endswith(
        "/projects/protein-design/science.yaml"
    )
    workspace = client.post("/openevo-api/desktop/workspace", headers=headers)
    assert workspace.status_code == 200
    assert workspace.json()["workspace"]["ready"] is True


def test_project_config_activate_rejects_missing_sidecar_token(
    tmp_path: Path,
) -> None:
    client = TestClient(
        create_sidecar_app(
            config_root=tmp_path,
            transport_factory=lambda _profile: _ApiDryRunTransport(),
        )
    )

    response = client.post(
        "/openevo-api/desktop/project-configs/protein-design/activate",
    )

    assert response.status_code == 403
    assert response.json()["detail"] == "Invalid OpenEvo sidecar token."


def test_project_config_activate_rejects_unknown_slug(tmp_path: Path) -> None:
    client = TestClient(
        create_sidecar_app(
            config_root=tmp_path,
            transport_factory=lambda _profile: _ApiDryRunTransport(),
        )
    )
    token = _sidecar_token(client)

    response = client.post(
        "/openevo-api/desktop/project-configs/missing-project/activate",
        headers={"X-OpenEvo-Sidecar-Token": token},
    )

    assert response.status_code == 404
    assert response.json()["detail"] == "Saved Desktop project config not found."


def test_project_config_activate_rejects_invalid_saved_config(
    tmp_path: Path,
) -> None:
    writer = TestClient(
        create_sidecar_app(
            config_root=tmp_path,
            transport_factory=lambda _profile: _ApiDryRunTransport(),
        )
    )
    writer_token = _sidecar_token(writer)
    saved = writer.post(
        "/openevo-api/desktop/project-config",
        headers={"X-OpenEvo-Sidecar-Token": writer_token},
        json=_desktop_config_draft_payload(),
    )
    assert saved.status_code == 200
    (tmp_path / "profiles" / "science-team.yaml").unlink()
    client = TestClient(
        create_sidecar_app(
            config_root=tmp_path,
            transport_factory=lambda _profile: _ApiDryRunTransport(),
        )
    )
    token = _sidecar_token(client)

    response = client.post(
        "/openevo-api/desktop/project-configs/protein-design/activate",
        headers={"X-OpenEvo-Sidecar-Token": token},
    )

    assert response.status_code == 422
    assert response.json()["detail"].startswith("Saved Desktop project config is invalid")
    assert str(tmp_path) not in response.json()["detail"]


def test_project_config_activate_sanitizes_invalid_profile_inputs(
    tmp_path: Path,
) -> None:
    writer = TestClient(
        create_sidecar_app(
            config_root=tmp_path,
            transport_factory=lambda _profile: _ApiDryRunTransport(),
        )
    )
    writer_token = _sidecar_token(writer)
    saved = writer.post(
        "/openevo-api/desktop/project-config",
        headers={"X-OpenEvo-Sidecar-Token": writer_token},
        json=_desktop_config_draft_payload(),
    )
    assert saved.status_code == 200
    profile_path = tmp_path / "profiles" / "science-team.yaml"
    profile_path.write_text(
        "\n".join(
            [
                "version: 1",
                "id: science-team",
                "host: gpu.example.edu",
                "port: 22",
                "user: alice",
                "password: super-secret-value",
            ]
        ),
        encoding="utf-8",
    )
    client = TestClient(
        create_sidecar_app(
            config_root=tmp_path,
            transport_factory=lambda _profile: _ApiDryRunTransport(),
        )
    )
    token = _sidecar_token(client)

    response = client.post(
        "/openevo-api/desktop/project-configs/protein-design/activate",
        headers={"X-OpenEvo-Sidecar-Token": token},
    )

    detail = response.json()["detail"]
    assert response.status_code == 422
    assert "Extra inputs are not permitted" in detail
    assert "super-secret-value" not in detail


def test_project_config_activate_rejects_path_traversal_slug(
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
        "/openevo-api/desktop/project-configs/%2E%2E/activate",
        headers={"X-OpenEvo-Sidecar-Token": token},
    )

    assert response.status_code == 400
    assert response.json()["detail"] == "Invalid Desktop project slug."


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


def test_project_config_endpoint_saves_managed_local_inference_draft(
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
        json=_desktop_config_draft_payload()
        | {
            "execution_mode": "codex_managed_local_inference",
            "codex_model": None,
            "hf_model": "Qwen/Qwen3-Coder-30B-A3B-Instruct",
        },
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["status"]["execution"] == {
        "mode": "self-deployed",
        "model": "Qwen/Qwen3-Coder-30B-A3B-Instruct",
        "token_metrics_available": False,
    }
    science_yaml = yaml.safe_load(
        Path(payload["config"]["science_config_path"]).read_text(encoding="utf-8")
    )
    assert science_yaml["execution"] == {
        "mode": "self-deployed",
        "hf_model": "Qwen/Qwen3-Coder-30B-A3B-Instruct",
    }


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
        "Desktop run launch requires ready workspace, bootstrap, and services."
    )


def test_run_endpoint_rejects_without_ready_services() -> None:
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
    headers = {"X-OpenEvo-Sidecar-Token": token}
    _prepare_workspace_and_bootstrap(client, headers)

    response = client.post("/openevo-api/desktop/run", headers=headers)

    assert response.status_code == 409
    assert response.json()["detail"] == (
        "Desktop run launch requires ready workspace, bootstrap, and services."
    )


def test_run_endpoint_launches_after_workspace_bootstrap_and_services() -> None:
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
    bootstrap = _prepare_workspace_bootstrap_and_services(client, headers)

    response = client.post("/openevo-api/desktop/run", headers=headers)

    assert response.status_code == 200
    payload = response.json()
    state_root = bootstrap["report"]["prepared_paths"]["state_root"]
    experiment_snapshot = bootstrap["report"]["prepared_paths"]["experiment_snapshot"]
    output_dir = f"{state_root}/runs/{payload['run']['id']}"
    expected_command = (
        f'PATH="$HOME/.local/bin:$PATH" openevo run {experiment_snapshot} '
        f"--output-dir {output_dir} --json"
    )
    assert payload["run"]["id"].startswith("run_")
    assert payload["run"]["state"] == "running"
    assert payload["run"]["ready"] is False
    assert payload["run"]["finished_at"] is None
    assert payload["run"]["command"] == expected_command
    assert payload["run"]["output_dir"] == output_dir
    assert payload["run"]["experiment_snapshot"] == experiment_snapshot

    terminal = _wait_latest_run_state(client, headers, "succeeded")
    assert payload["run"]["command"] in transport.commands
    assert transport.run_calls[-1] == (expected_command, state_root, 86400.0)
    assert terminal["run"]["ready"] is True
    assert terminal["run"]["return_code"] == 0
    assert terminal["run"]["stdout"] == "ok"
    assert terminal["run"]["finished_at"] is not None
    services = {service["id"]: service for service in terminal["status"]["services"]}
    assert services["openevo-backend"] == {
        "id": "openevo-backend",
        "label": "OpenEvo backend",
        "state": "ready",
        "detail": "Last run completed",
    }
    evolution = {step["id"]: step for step in terminal["status"]["evolution"]}
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
    _prepare_workspace_bootstrap_and_services(client, headers)

    response = client.post("/openevo-api/desktop/run", headers=headers)

    assert response.status_code == 200
    assert response.json()["run"]["state"] == "running"
    payload = _wait_latest_run_state(client, headers, "failed")
    assert payload["run"]["ready"] is False
    assert payload["run"]["return_code"] == 2
    assert payload["run"]["stderr"] == "run failed"
    assert payload["run"]["finished_at"] is not None
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
    _prepare_workspace_bootstrap_and_services(client, headers)

    first = client.post("/openevo-api/desktop/run", headers=headers)
    assert transport.run_started.wait(timeout=5)
    second = client.post("/openevo-api/desktop/run", headers=headers)
    transport.run_release.set()

    assert first.status_code == 200
    assert second.status_code == 409
    assert second.json()["detail"] == "Desktop run launch is already running."
    assert _wait_latest_run_state(client, headers, "succeeded")["run"]["ready"] is True


def test_run_status_endpoint_returns_latest_running_report() -> None:
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
    bootstrap = _prepare_workspace_bootstrap_and_services(client, headers)

    launch = client.post("/openevo-api/desktop/run", headers=headers)
    assert transport.run_started.wait(timeout=5)
    poll = client.get("/openevo-api/desktop/run", headers=headers)
    transport.run_release.set()

    assert launch.status_code == 200
    assert poll.status_code == 200
    assert poll.json()["run"]["id"] == launch.json()["run"]["id"]
    assert poll.json()["run"]["state"] == "running"
    assert poll.json()["run"]["ready"] is False
    assert poll.json()["run"]["finished_at"] is None
    state_root = bootstrap["report"]["prepared_paths"]["state_root"]
    assert poll.json()["run"]["output_dir"] == (
        f"{state_root}/runs/{launch.json()['run']['id']}"
    )
    services = {service["id"]: service for service in poll.json()["status"]["services"]}
    assert services["openevo-backend"]["state"] == "running"
    evolution = {step["id"]: step for step in poll.json()["status"]["evolution"]}
    assert evolution["transcript"]["state"] == "running"


def test_run_status_endpoint_rejects_missing_sidecar_token() -> None:
    project = ScienceProjectConfig.model_validate(_science_project_payload())
    profile = _remote_profile()
    client = TestClient(
        create_sidecar_app_for_project(
            project,
            profile,
            transport_factory=lambda _profile: _ApiDryRunTransport(),
        )
    )

    response = client.get("/openevo-api/desktop/run")

    assert response.status_code == 403
    assert response.json()["detail"] == "Invalid OpenEvo sidecar token."


def test_run_status_endpoint_rejects_invalid_sidecar_token() -> None:
    project = ScienceProjectConfig.model_validate(_science_project_payload())
    profile = _remote_profile()
    client = TestClient(
        create_sidecar_app_for_project(
            project,
            profile,
            transport_factory=lambda _profile: _ApiDryRunTransport(),
        )
    )

    response = client.get(
        "/openevo-api/desktop/run",
        headers={"X-OpenEvo-Sidecar-Token": "wrong-token"},
    )

    assert response.status_code == 403
    assert response.json()["detail"] == "Invalid OpenEvo sidecar token."


def test_run_status_endpoint_requires_launched_run() -> None:
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

    response = client.get(
        "/openevo-api/desktop/run",
        headers={"X-OpenEvo-Sidecar-Token": token},
    )

    assert response.status_code == 404
    assert response.json()["detail"] == "No Desktop run has been launched."


def test_run_artifacts_endpoint_requires_config_backed_session() -> None:
    client = TestClient(create_sidecar_app())
    token = _sidecar_token(client)

    response = client.get(
        "/openevo-api/desktop/run/artifacts",
        headers={"X-OpenEvo-Sidecar-Token": token},
    )

    assert response.status_code == 409
    assert response.json()["detail"] == (
        "Desktop run artifacts require a config-backed sidecar session."
    )


def test_run_artifacts_endpoint_requires_launched_run() -> None:
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

    response = client.get(
        "/openevo-api/desktop/run/artifacts",
        headers={"X-OpenEvo-Sidecar-Token": token},
    )

    assert response.status_code == 404
    assert response.json()["detail"] == "No Desktop run has been launched."


def test_run_artifacts_endpoint_rejects_active_run() -> None:
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
    _prepare_workspace_bootstrap_and_services(client, headers)

    launch = client.post("/openevo-api/desktop/run", headers=headers)
    assert transport.run_started.wait(timeout=5)
    response = client.get("/openevo-api/desktop/run/artifacts", headers=headers)
    transport.run_release.set()
    _wait_latest_run_state(client, headers, "succeeded")

    assert launch.status_code == 200
    assert response.status_code == 409
    assert response.json()["detail"] == (
        "Desktop run artifacts require a terminal run."
    )


def test_run_artifacts_endpoint_reads_latest_run_summary() -> None:
    project = ScienceProjectConfig.model_validate(_science_project_payload())
    profile = _remote_profile()
    transport = _RunArtifactsTransport(_sample_run_summary())
    client = TestClient(
        create_sidecar_app_for_project(
            project,
            profile,
            transport_factory=lambda _profile: transport,
        )
    )
    token = _sidecar_token(client)
    headers = {"X-OpenEvo-Sidecar-Token": token}
    _prepare_workspace_bootstrap_and_services(client, headers)
    launch = client.post("/openevo-api/desktop/run", headers=headers)
    terminal = _wait_latest_run_state(client, headers, "succeeded")

    response = client.get("/openevo-api/desktop/run/artifacts", headers=headers)

    assert launch.status_code == 200
    assert terminal["run"]["ready"] is True
    assert response.status_code == 200
    payload = response.json()
    assert payload["run_id"] == launch.json()["run"]["id"]
    assert payload["output_dir"] == launch.json()["run"]["output_dir"]
    assert payload["summary_status"] == "completed"
    assert payload["experiment_id"] == "biology-components"
    assert payload["tasks"] == [
        {
            "task_id": "folding-baseline",
            "rounds": [
                {
                    "round_index": 0,
                    "policy_version": "policy-r0",
                    "rollout_status": "completed",
                    "dataset_status": "ready",
                    "artifact_ids": {
                        "dataset": ["dataset-artifact-1"],
                        "text_memory": ["artifact-text-memory"],
                        "skill_bundle": ["artifact-skill-bundle"],
                        "agent_system": ["artifact-agent-system"],
                    },
                    "jobs": [
                        {
                            "artifact_type": "text_memory",
                            "method": "text_memory_reflector",
                            "worker_status": "succeeded",
                            "artifact_ids": ["artifact-text-memory"],
                            "approved_artifact_ids": ["artifact-text-memory"],
                            "promotion_status": "skipped",
                        },
                        {
                            "artifact_type": "skill_bundle",
                            "method": "skill_bundle_reflector",
                            "worker_status": "succeeded",
                            "artifact_ids": ["artifact-skill-bundle"],
                            "approved_artifact_ids": ["artifact-skill-bundle"],
                            "promotion_status": "approved",
                        },
                    ],
                }
            ],
        }
    ]
    assert any("summary.json" in command for command in transport.commands)


def test_run_artifacts_endpoint_reports_remote_summary_failure() -> None:
    project = ScienceProjectConfig.model_validate(_science_project_payload())
    profile = _remote_profile()
    transport = _MissingRunArtifactsTransport()
    client = TestClient(
        create_sidecar_app_for_project(
            project,
            profile,
            transport_factory=lambda _profile: transport,
        )
    )
    token = _sidecar_token(client)
    headers = {"X-OpenEvo-Sidecar-Token": token}
    _prepare_workspace_bootstrap_and_services(client, headers)
    client.post("/openevo-api/desktop/run", headers=headers)
    _wait_latest_run_state(client, headers, "succeeded")

    response = client.get("/openevo-api/desktop/run/artifacts", headers=headers)

    assert response.status_code == 404
    assert response.json()["detail"] == "OpenEvo run summary not found."


def test_run_artifacts_endpoint_reports_remote_read_failure() -> None:
    project = ScienceProjectConfig.model_validate(_science_project_payload())
    profile = _remote_profile()
    transport = _BrokenRunArtifactsTransport()
    client = TestClient(
        create_sidecar_app_for_project(
            project,
            profile,
            transport_factory=lambda _profile: transport,
        )
    )
    token = _sidecar_token(client)
    headers = {"X-OpenEvo-Sidecar-Token": token}
    _prepare_workspace_bootstrap_and_services(client, headers)
    client.post("/openevo-api/desktop/run", headers=headers)
    _wait_latest_run_state(client, headers, "succeeded")

    response = client.get("/openevo-api/desktop/run/artifacts", headers=headers)

    assert response.status_code == 502
    assert response.json()["detail"] == "python3: command not found"


def test_artifact_content_api_returns_memory_markdown() -> None:
    project = ScienceProjectConfig.model_validate(_science_project_payload())
    profile = _remote_profile()
    transport = _ArtifactContentTransport(
        _sample_run_summary_with_artifact_content(),
        "# Learned Memory\n\n- Prefer stable folds.\n",
        artifact_metadata={
            "artifact-text-memory": _artifact_metadata(
                artifact_id="artifact-text-memory",
                artifact_type="text_memory",
                uri="file:///remote/run/artifacts/text_memory/artifact-text-memory",
                manifest={"content_path": "memory.md"},
            ),
        },
        content_root="/remote/run/artifacts/text_memory/artifact-text-memory",
        content_relative_path="memory.md",
    )
    client = TestClient(
        create_sidecar_app_for_project(
            project,
            profile,
            transport_factory=lambda _profile: transport,
        )
    )
    token = _sidecar_token(client)
    headers = {"X-OpenEvo-Sidecar-Token": token}
    _prepare_workspace_bootstrap_and_services(client, headers)
    client.post("/openevo-api/desktop/run", headers=headers)
    _wait_latest_run_state(client, headers, "succeeded")

    response = client.get(
        "/openevo-api/desktop/artifacts/artifact-text-memory/content",
        headers=headers,
    )

    assert response.status_code == 200
    assert response.json() == {
        "artifact_id": "artifact-text-memory",
        "artifact_type": "text_memory",
        "filename": "memory.md",
        "content": "# Learned Memory\n\n- Prefer stable folds.\n",
        "mime_type": "text/markdown",
    }
    assert any(
        "/v1/artifacts/artifact-text-memory" in command
        for command in transport.commands
    )
    content_command = transport.content_commands[-1]
    assert "root = Path('/remote/run/artifacts/text_memory/artifact-text-memory')" in (
        content_command
    )
    assert "relative = Path('memory.md')" in content_command


def test_artifact_content_api_splits_nested_agent_system_path() -> None:
    project = ScienceProjectConfig.model_validate(_science_project_payload())
    profile = _remote_profile()
    transport = _ArtifactContentTransport(
        _sample_run_summary_with_artifact_content(),
        "# Repo Microagent\n",
        artifact_metadata={
            "artifact-agent-system": _artifact_metadata(
                artifact_id="artifact-agent-system",
                artifact_type="agent_system",
                uri=(
                    "file:///remote/run/artifacts/agent_system/"
                    "artifact-agent-system/.openhands/microagents/repo.md"
                ),
                manifest={"content_path": ".openhands/microagents/repo.md"},
            ),
        },
        content_root="/remote/run/artifacts/agent_system/artifact-agent-system",
        content_relative_path=".openhands/microagents/repo.md",
    )
    client = TestClient(
        create_sidecar_app_for_project(
            project,
            profile,
            transport_factory=lambda _profile: transport,
        )
    )
    token = _sidecar_token(client)
    headers = {"X-OpenEvo-Sidecar-Token": token}
    _prepare_workspace_bootstrap_and_services(client, headers)
    client.post("/openevo-api/desktop/run", headers=headers)
    _wait_latest_run_state(client, headers, "succeeded")

    response = client.get(
        "/openevo-api/desktop/artifacts/artifact-agent-system/content",
        headers=headers,
    )

    assert response.status_code == 200
    assert response.json() == {
        "artifact_id": "artifact-agent-system",
        "artifact_type": "agent_system",
        "filename": "repo.md",
        "content": "# Repo Microagent\n",
        "mime_type": "text/markdown",
    }
    content_command = transport.content_commands[-1]
    assert "root = Path('/remote/run/artifacts/agent_system/artifact-agent-system')" in (
        content_command
    )
    assert "relative = Path('.openhands/microagents/repo.md')" in content_command
    assert (
        "/remote/run/artifacts/agent_system/artifact-agent-system/"
        ".openhands/microagents/repo.md/.openhands/microagents/repo.md"
        not in content_command
    )


def test_artifact_content_api_rejects_unsafe_content_path() -> None:
    project = ScienceProjectConfig.model_validate(_science_project_payload())
    profile = _remote_profile()
    summary = _sample_run_summary_with_artifact_content()
    transport = _ArtifactContentTransport(
        summary,
        "secret",
        artifact_metadata={
            "artifact-text-memory": _artifact_metadata(
                artifact_id="artifact-text-memory",
                artifact_type="text_memory",
                uri="file:///remote/run/artifacts/text_memory/artifact-text-memory",
                manifest={"content_path": "../secrets.txt"},
            ),
        },
        content_root="/remote/run/artifacts/text_memory/artifact-text-memory",
        content_relative_path="../secrets.txt",
    )
    client = TestClient(
        create_sidecar_app_for_project(
            project,
            profile,
            transport_factory=lambda _profile: transport,
        )
    )
    token = _sidecar_token(client)
    headers = {"X-OpenEvo-Sidecar-Token": token}
    _prepare_workspace_bootstrap_and_services(client, headers)
    client.post("/openevo-api/desktop/run", headers=headers)
    _wait_latest_run_state(client, headers, "succeeded")

    response = client.get(
        "/openevo-api/desktop/artifacts/artifact-text-memory/content",
        headers=headers,
    )

    assert response.status_code == 422
    assert response.json()["detail"] == (
        "Artifact content_path must stay within the artifact root."
    )
    assert not any("secrets.txt" in command for command in transport.commands)


def test_artifact_content_api_ignores_worker_result_artifact_ids() -> None:
    project = ScienceProjectConfig.model_validate(_science_project_payload())
    profile = _remote_profile()
    summary = _sample_run_summary_with_artifact_content()
    summary["tasks"][0]["rounds"][0]["jobs"][0]["worker_results"] = [
        {"artifact_ids": ["artifact-smuggled"]}
    ]
    transport = _ArtifactContentTransport(
        summary,
        "# Smuggled\n",
        artifact_metadata={
            "artifact-smuggled": _artifact_metadata(
                artifact_id="artifact-smuggled",
                artifact_type="text_memory",
                uri="file:///remote/run/artifacts/text_memory/artifact-smuggled",
                manifest={"content_path": "memory.md"},
            ),
        },
        content_root="/remote/run/artifacts/text_memory/artifact-smuggled",
        content_relative_path="memory.md",
    )
    client = TestClient(
        create_sidecar_app_for_project(
            project,
            profile,
            transport_factory=lambda _profile: transport,
        )
    )
    token = _sidecar_token(client)
    headers = {"X-OpenEvo-Sidecar-Token": token}
    _prepare_workspace_bootstrap_and_services(client, headers)
    client.post("/openevo-api/desktop/run", headers=headers)
    _wait_latest_run_state(client, headers, "succeeded")

    response = client.get(
        "/openevo-api/desktop/artifacts/artifact-smuggled/content",
        headers=headers,
    )

    assert response.status_code == 404
    assert response.json()["detail"] == "Artifact not found in latest run summary."
    assert transport.metadata_commands == []


def test_artifact_content_api_ignores_malformed_nested_artifact_fields() -> None:
    project = ScienceProjectConfig.model_validate(_science_project_payload())
    profile = _remote_profile()
    summary = _sample_run_summary_with_artifact_content()
    round_payload = summary["tasks"][0]["rounds"][0]
    round_payload["artifact_ids"] = {
        "text_memory": [{"artifact_ids": ["artifact-smuggled"]}]
    }
    round_payload["jobs"][0]["artifact_ids"] = [
        {"artifact_ids": ["artifact-smuggled"]}
    ]
    round_payload["jobs"][0]["approved_artifact_ids"] = [
        {"artifact_ids": ["artifact-smuggled"]}
    ]
    transport = _ArtifactContentTransport(
        summary,
        "# Smuggled\n",
        artifact_metadata={
            "artifact-smuggled": _artifact_metadata(
                artifact_id="artifact-smuggled",
                artifact_type="text_memory",
                uri="file:///remote/run/artifacts/text_memory/artifact-smuggled",
                manifest={"content_path": "memory.md"},
            ),
        },
        content_root="/remote/run/artifacts/text_memory/artifact-smuggled",
        content_relative_path="memory.md",
    )
    client = TestClient(
        create_sidecar_app_for_project(
            project,
            profile,
            transport_factory=lambda _profile: transport,
        )
    )
    token = _sidecar_token(client)
    headers = {"X-OpenEvo-Sidecar-Token": token}
    _prepare_workspace_bootstrap_and_services(client, headers)
    client.post("/openevo-api/desktop/run", headers=headers)
    _wait_latest_run_state(client, headers, "succeeded")

    response = client.get(
        "/openevo-api/desktop/artifacts/artifact-smuggled/content",
        headers=headers,
    )

    assert response.status_code == 404
    assert response.json()["detail"] == "Artifact not found in latest run summary."
    assert transport.metadata_commands == []


def test_run_response_schema_has_structured_report_contract() -> None:
    client = TestClient(create_sidecar_app())

    schema = client.get("/openapi.json").json()

    response_schema = schema["components"]["schemas"]["OpenEvoDesktopRunResponse"]
    run_ref = response_schema["properties"]["run"]["$ref"]
    assert run_ref == "#/components/schemas/OpenEvoDesktopRunStatus"
    run_schema = schema["components"]["schemas"]["OpenEvoDesktopRunStatus"]
    assert set(run_schema["required"]) == {
        "id",
        "state",
        "ready",
        "command",
        "return_code",
        "stdout",
        "stderr",
        "output_dir",
        "experiment_snapshot",
        "started_at",
        "finished_at",
    }
    assert run_schema["properties"]["state"]["enum"] == [
        "running",
        "succeeded",
        "failed",
    ]
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


def test_execution_status_accepts_legacy_self_deployed_alias() -> None:
    status = DesktopExecutionStatus(
        mode="codex_managed_local_inference",
        model="Qwen/Qwen2.5-7B-Instruct",
        token_metrics_available=False,
    )

    assert status.mode == "self-deployed"
    assert status.model_dump(mode="json")["mode"] == "self-deployed"


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


def _remote_profile(auth: dict | None = None) -> RemoteProfileConfig:
    return RemoteProfileConfig(
        version=1,
        id="science-team",
        host="gpu.example.edu",
        user="alice",
        auth=auth or {"method": "ssh_agent"},
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


def _prepare_workspace_bootstrap_and_services(
    client: TestClient,
    headers: dict[str, str],
) -> dict:
    bootstrap = _prepare_workspace_and_bootstrap(client, headers)
    services = client.post("/openevo-api/desktop/services", headers=headers)
    assert services.status_code == 200
    assert services.json()["services"]["ready"] is True
    return bootstrap


def _wait_latest_run_state(
    client: TestClient,
    headers: dict[str, str],
    expected_state: str,
) -> dict:
    for _ in range(50):
        response = client.get("/openevo-api/desktop/run", headers=headers)
        assert response.status_code == 200
        payload = response.json()
        if payload["run"]["state"] == expected_state:
            return payload
        time.sleep(0.05)
    raise AssertionError(f"latest run did not reach {expected_state}")


def _sample_run_summary() -> dict:
    return {
        "mode": "run",
        "status": "completed",
        "experiment_id": "biology-components",
        "experiment_name": "Biology Components",
        "run_id": "compiled-run-id",
        "round_count": 1,
        "tasks": [
            {
                "task_id": "folding-baseline",
                "rounds": [
                    {
                        "round_index": 0,
                        "policy_version": "policy-r0",
                        "rollout_status": "completed",
                        "dataset_status": "ready",
                        "artifact_ids": {
                            "dataset": ["dataset-artifact-1"],
                            "text_memory": ["artifact-text-memory"],
                            "skill_bundle": ["artifact-skill-bundle"],
                            "agent_system": ["artifact-agent-system"],
                        },
                        "jobs": [
                            {
                                "artifact_type": "text_memory",
                                "method": "text_memory_reflector",
                                "worker_status": "succeeded",
                                "artifact_ids": ["artifact-text-memory"],
                                "approved_artifact_ids": ["artifact-text-memory"],
                                "promotion_status": "skipped",
                                "worker_results": [{"large": "not returned"}],
                            },
                            {
                                "artifact_type": "skill_bundle",
                                "method": "skill_bundle_reflector",
                                "worker_status": "succeeded",
                                "artifact_ids": ["artifact-skill-bundle"],
                                "approved_artifact_ids": ["artifact-skill-bundle"],
                                "promotion_status": "approved",
                                "job": {"large": "not returned"},
                            },
                        ],
                    }
                ],
            }
        ],
        "summary_path": "/remote/run/summary.json",
    }


def _sample_run_summary_with_artifact_content() -> dict:
    return _sample_run_summary()


def _artifact_metadata(
    *,
    artifact_id: str,
    artifact_type: str,
    uri: str,
    manifest: dict,
) -> dict:
    return {
        "artifact_id": artifact_id,
        "type": artifact_type,
        "name": artifact_id,
        "version": 1,
        "state": "ready",
        "uri": uri,
        "manifest": manifest,
        "compatibility": {},
        "scores": {},
        "tags": [],
        "promoted": False,
    }


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


class _ApiLifecycleTransport(_ApiDryRunTransport):
    def __init__(
        self,
        *,
        pid_states: dict[str, dict[str, object]] | None = None,
        health_failures: dict[str, str] | None = None,
        log_content: str = "",
    ) -> None:
        super().__init__()
        self.pid_states = pid_states or {}
        self.health_failures = health_failures or {}
        self.log_content = log_content
        self.stopped_services: list[str] = []

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
        service_id = _service_id_from_command(command)
        if command == 'df -Pk "$HOME"':
            return super().run(
                command,
                cwd=cwd,
                env=env,
                timeout_seconds=timeout_seconds,
            )
        if "json.dumps" in command and "pid_path =" in command:
            state = self.pid_states.get(service_id or "", {})
            return RemoteCommandResult(
                command=command,
                return_code=0,
                stdout=json.dumps(
                    {
                        "pid_exists": service_id in self.pid_states,
                        "pid": state.get("pid"),
                        "alive": bool(state.get("alive")),
                    }
                ),
            )
        if command.startswith("if [ -f ") and "tail -n" in command:
            return RemoteCommandResult(
                command=command,
                return_code=0,
                stdout=self.log_content,
            )
        if "os.kill(pid, signal.SIGTERM)" in command:
            if service_id not in self.pid_states:
                return RemoteCommandResult(
                    command=command,
                    return_code=0,
                    stdout=f"{service_id} is already stopped.",
                )
            self.stopped_services.append(service_id or "")
            self.pid_states.pop(service_id or "", None)
            return RemoteCommandResult(
                command=command,
                return_code=0,
                stdout=f"{service_id} stopped.",
            )
        if service_id in self.health_failures and (
            "/health" in command or "pid_path =" in command
        ):
            return RemoteCommandResult(
                command=command,
                return_code=1,
                stderr=self.health_failures[service_id],
            )
        return RemoteCommandResult(command=command, return_code=0, stdout="ok")


def _service_id_from_command(command: str) -> str | None:
    url_services = {
        "127.0.0.1:8200": "evolution_backend",
        "127.0.0.1:8080": "rollout",
        "127.0.0.1:8100": "gateway",
        "127.0.0.1:8000": "vllm",
    }
    for url_fragment, service_id in url_services.items():
        if url_fragment in command:
            return service_id
    for service_id in (
        "evolution_backend",
        "evolution_worker",
        "gateway",
        "rollout",
        "vllm",
    ):
        if f"/{service_id}.pid" in command or f"/{service_id}.log" in command:
            return service_id
        if f" {service_id} " in command:
            return service_id
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
        if command.startswith('PATH="$HOME/.local/bin:$PATH" openevo run '):
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


class _RunArtifactsTransport(_ApiDryRunTransport):
    def __init__(self, summary: dict) -> None:
        super().__init__()
        self.summary = summary

    def run(
        self,
        command: str,
        *,
        cwd: str | None = None,
        env: dict[str, str] | None = None,
        timeout_seconds: float = 30.0,
    ) -> RemoteCommandResult:
        if command.startswith('PATH="$HOME/.local/bin:$PATH" openevo run '):
            self.commands.append(command)
            self.run_calls.append((command, cwd, timeout_seconds))
            return RemoteCommandResult(command=command, return_code=0, stdout="ok")
        if "summary.json" in command:
            self.commands.append(command)
            self.run_calls.append((command, cwd, timeout_seconds))
            return RemoteCommandResult(
                command=command,
                return_code=0,
                stdout=json.dumps(self.summary),
            )
        return super().run(
            command,
            cwd=cwd,
            env=env,
            timeout_seconds=timeout_seconds,
        )


class _ArtifactContentTransport(_RunArtifactsTransport):
    def __init__(
        self,
        summary: dict,
        content: str,
        *,
        artifact_metadata: dict[str, dict],
        content_root: str,
        content_relative_path: str,
    ) -> None:
        super().__init__(summary)
        self.content = content
        self.artifact_metadata = artifact_metadata
        self.content_root = content_root
        self.content_relative_path = content_relative_path
        self.metadata_commands: list[str] = []
        self.content_commands: list[str] = []

    def run(
        self,
        command: str,
        *,
        cwd: str | None = None,
        env: dict[str, str] | None = None,
        timeout_seconds: float = 30.0,
    ) -> RemoteCommandResult:
        if "summary.json" in command or command.startswith(
            'PATH="$HOME/.local/bin:$PATH" openevo run '
        ):
            return super().run(
                command,
                cwd=cwd,
                env=env,
                timeout_seconds=timeout_seconds,
            )
        if "/v1/artifacts/" in command:
            self.commands.append(command)
            self.metadata_commands.append(command)
            self.run_calls.append((command, cwd, timeout_seconds))
            for artifact_id, metadata in self.artifact_metadata.items():
                if f"/v1/artifacts/{artifact_id}" in command:
                    return RemoteCommandResult(
                        command=command,
                        return_code=0,
                        stdout=json.dumps(metadata),
                    )
            return RemoteCommandResult(
                command=command,
                return_code=22,
                stderr="Artifact metadata not found.",
            )
        if "root = Path(" not in command or "relative = Path(" not in command:
            return super().run(
                command,
                cwd=cwd,
                env=env,
                timeout_seconds=timeout_seconds,
            )
        self.commands.append(command)
        self.content_commands.append(command)
        self.run_calls.append((command, cwd, timeout_seconds))
        expected_root = f"root = Path({self.content_root!r})"
        expected_relative = f"relative = Path({self.content_relative_path!r})"
        if expected_root not in command or expected_relative not in command:
            return RemoteCommandResult(
                command=command,
                return_code=2,
                stderr="unexpected artifact content path",
            )
        return RemoteCommandResult(
            command=command,
            return_code=0,
            stdout=self.content,
        )


class _MissingRunArtifactsTransport(_RunArtifactsTransport):
    def __init__(self) -> None:
        super().__init__(_sample_run_summary())

    def run(
        self,
        command: str,
        *,
        cwd: str | None = None,
        env: dict[str, str] | None = None,
        timeout_seconds: float = 30.0,
    ) -> RemoteCommandResult:
        if "summary.json" in command:
            self.commands.append(command)
            self.run_calls.append((command, cwd, timeout_seconds))
            return RemoteCommandResult(
                command=command,
                return_code=2,
                stderr="OpenEvo run summary not found.",
            )
        return super().run(
            command,
            cwd=cwd,
            env=env,
            timeout_seconds=timeout_seconds,
        )


class _BrokenRunArtifactsTransport(_RunArtifactsTransport):
    def __init__(self) -> None:
        super().__init__(_sample_run_summary())

    def run(
        self,
        command: str,
        *,
        cwd: str | None = None,
        env: dict[str, str] | None = None,
        timeout_seconds: float = 30.0,
    ) -> RemoteCommandResult:
        if "summary.json" in command:
            self.commands.append(command)
            self.run_calls.append((command, cwd, timeout_seconds))
            return RemoteCommandResult(
                command=command,
                return_code=127,
                stderr="python3: command not found",
            )
        return super().run(
            command,
            cwd=cwd,
            env=env,
            timeout_seconds=timeout_seconds,
        )


class _FailingServicesTransport(_ApiDryRunTransport):
    def run(
        self,
        command: str,
        *,
        cwd: str | None = None,
        env: dict[str, str] | None = None,
        timeout_seconds: float = 30.0,
    ) -> RemoteCommandResult:
        if "polar serve_rollout" in command:
            self.commands.append(command)
            return RemoteCommandResult(
                command=command,
                return_code=1,
                stderr="rollout failed",
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
        if command.startswith('PATH="$HOME/.local/bin:$PATH" openevo run '):
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


class _BlockingServicesTransport(_ApiDryRunTransport):
    def __init__(self) -> None:
        super().__init__()
        self.service_started = Event()
        self.service_release = Event()

    def run(
        self,
        command: str,
        *,
        cwd: str | None = None,
        env: dict[str, str] | None = None,
        timeout_seconds: float = 30.0,
    ) -> RemoteCommandResult:
        if "polar serve_rollout" in command and not self.service_started.is_set():
            self.commands.append(command)
            self.service_started.set()
            if not self.service_release.wait(timeout=5):
                raise TimeoutError("services concurrency test timed out")
            return RemoteCommandResult(command=command, return_code=0, stdout="ok")
        return super().run(
            command,
            cwd=cwd,
            env=env,
            timeout_seconds=timeout_seconds,
        )


class _BlockingSecondServicesTransport(_ApiDryRunTransport):
    def __init__(self) -> None:
        super().__init__()
        self.rollout_start_count = 0
        self.second_service_started = Event()
        self.second_service_release = Event()

    def run(
        self,
        command: str,
        *,
        cwd: str | None = None,
        env: dict[str, str] | None = None,
        timeout_seconds: float = 30.0,
    ) -> RemoteCommandResult:
        if "polar serve_rollout" in command:
            self.commands.append(command)
            self.run_calls.append((command, cwd, timeout_seconds))
            self.rollout_start_count += 1
            if self.rollout_start_count == 2:
                self.second_service_started.set()
                if not self.second_service_release.wait(timeout=5):
                    raise TimeoutError("second services test timed out")
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


def test_build_desktop_shell_status_includes_remote_setup_fields() -> None:
    project = ScienceProjectConfig.model_validate(_science_project_payload())
    profile = RemoteProfileConfig(
        version=1,
        id="science-team",
        host="gpu.example.edu",
        port=2222,
        user="alice",
        auth={
            "method": "private_key",
            "private_key_path": "/home/alice/.ssh/openevo",
            "passphrase_ref": "keyring://openevo/science-team",
        },
        proxy={
            "http_proxy": "http://127.0.0.1:7890",
            "https_proxy": "http://127.0.0.1:7891",
            "no_proxy": "localhost,127.0.0.1",
            "pip_index_url": "https://pypi.tuna.tsinghua.edu.cn/simple",
            "huggingface_endpoint": "https://hf-mirror.com",
            "hf_home": "/data/hf-cache",
        },
        workspace_root="/data/openevo/workspaces",
    )

    payload = build_desktop_shell_status(project, profile).model_dump(mode="json")

    assert payload["remote"]["port"] == 2222
    assert payload["remote"]["auth"] == {
        "method": "private_key",
        "private_key_path": "/home/alice/.ssh/openevo",
        "password_ref": None,
        "passphrase_ref": "keyring://openevo/science-team",
    }
    assert payload["remote"]["workspace_root"] == "/data/openevo/workspaces"
    assert payload["remote"]["proxy"] == {
        "http_proxy": "http://127.0.0.1:7890",
        "https_proxy": "http://127.0.0.1:7891",
        "no_proxy": "localhost,127.0.0.1",
        "pip_index_url": "https://pypi.tuna.tsinghua.edu.cn/simple",
        "huggingface_endpoint": "https://hf-mirror.com",
        "hf_home": "/data/hf-cache",
    }
    assert payload["bootstrap"]["workspace_root"] == "/data/openevo/workspaces"


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

    assert status.execution.mode == "self-deployed"
    assert status.execution.model == "Qwen/Qwen2.5-7B-Instruct"
    assert status.execution.token_metrics_available is False
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
