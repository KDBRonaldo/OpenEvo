from __future__ import annotations

import hashlib
import hmac
import json
import socket
import shlex
import time
import tomllib
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Event
from threading import Thread
from typing import cast
import urllib.request
from urllib.parse import urlparse
from zipfile import ZipFile

from fastapi.testclient import TestClient
import pytest
import uvicorn
import yaml

import desktop.sidecar.api as sidecar_api
from desktop.sidecar import (
    DesktopExecutionStatus,
    NativeSidecarInstance,
    build_desktop_shell_status,
    create_sidecar_app,
    create_sidecar_app_for_project,
    default_desktop_shell_status,
)
from desktop.sidecar.backend_client import BackendClient, BackendConnection
from openevo.projects.science import ScienceProjectConfig
from desktop.sidecar import RemoteProfileConfig
from openevo.backend.api import create_backend_app
from openevo.deployment import RemoteCommandResult
from openevo.evolution.framework import (
    CapabilityAudience,
    build_evolution_capabilities,
    execution_profile_for_release_mode,
)
from openevo.evolution.framework.builtins import (
    ImplementationDistributionIdentity,
    build_builtin_registry,
)
from openevo.evolution.models import ArtifactRegisterRequest
from openevo.evolution.store import EvolutionStore
from tests.framework_testkit import verified_builtin_registry


def test_sidecar_health_endpoint() -> None:
    client = TestClient(create_sidecar_app())

    response = client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"service": "openevo-sidecar", "status": "ok"}


def test_sidecar_native_health_proves_the_instance_credential() -> None:
    instance_id = "1a" * 16
    secret = bytes.fromhex("5a" * 32)
    challenge = "3c" * 32
    instance = NativeSidecarInstance(
        instance_id=instance_id,
        readiness_key=secret,
    )
    client = TestClient(create_sidecar_app(native_instance=instance))

    response = client.get(
        "/health",
        headers={"X-OpenEvo-Native-Challenge": challenge},
    )

    assert response.status_code == 200
    assert response.json() == {
        "service": "openevo-sidecar",
        "status": "ok",
        "protocol": sidecar_api.NATIVE_SIDECAR_PROTOCOL,
        "instance_id": instance_id,
        "instance_proof": hmac.new(
            secret,
            (f"{sidecar_api.NATIVE_SIDECAR_PROTOCOL}\0{instance_id}\0{challenge}").encode("ascii"),
            hashlib.sha256,
        ).hexdigest(),
    }
    assert secret.hex() not in repr(instance)


@pytest.mark.parametrize(
    ("instance_id", "readiness_key", "message"),
    [
        ("1a" * 15, b"x" * 32, "native instance id"),
        ("1A" * 16, b"x" * 32, "native instance id"),
        ("1a" * 16, b"too-short", "native readiness key"),
        ("1a" * 16, bytearray(b"x" * 32), "native readiness key"),
    ],
)
def test_sidecar_native_instance_rejects_invalid_closed_values(
    instance_id: str,
    readiness_key: bytes,
    message: str,
) -> None:
    with pytest.raises(ValueError, match=message):
        NativeSidecarInstance(
            instance_id=instance_id,
            readiness_key=readiness_key,
        )


@pytest.mark.parametrize(
    "challenge",
    [None, "", "not-hex", "ab" * 31, "AB" * 32, "ab" * 33],
)
def test_sidecar_native_health_rejects_invalid_challenges(
    challenge: str | None,
) -> None:
    client = TestClient(
        create_sidecar_app(
            native_instance=NativeSidecarInstance(
                instance_id="1a" * 16,
                readiness_key=b"x" * 32,
            )
        )
    )
    headers = {"X-OpenEvo-Native-Challenge": challenge} if challenge is not None else {}

    response = client.get("/health", headers=headers)

    assert response.status_code == 403
    assert "instance" not in response.text.casefold()


def test_sidecar_native_health_proof_cannot_be_replayed_for_a_fresh_challenge() -> None:
    instance_id = "1a" * 16
    secret = bytes.fromhex("5a" * 32)
    client = TestClient(
        create_sidecar_app(
            native_instance=NativeSidecarInstance(
                instance_id=instance_id,
                readiness_key=secret,
            )
        )
    )
    stale_challenge = "11" * 32
    fresh_challenge = "22" * 32

    stale_proof = client.get(
        "/health",
        headers={"X-OpenEvo-Native-Challenge": stale_challenge},
    ).json()["instance_proof"]
    fresh_expected = hmac.new(
        secret,
        (f"{sidecar_api.NATIVE_SIDECAR_PROTOCOL}\0{instance_id}\0{fresh_challenge}").encode(
            "ascii"
        ),
        hashlib.sha256,
    ).hexdigest()

    assert not hmac.compare_digest(stale_proof, fresh_expected)


def test_sidecar_allows_tauri_localhost_cors_preflight() -> None:
    client = TestClient(create_sidecar_app())

    response = client.options(
        "/openevo-api/desktop/shell",
        headers={
            "Origin": "http://tauri.localhost",
            "Access-Control-Request-Method": "GET",
        },
    )

    assert response.status_code == 200
    assert response.headers["access-control-allow-origin"] == ("http://tauri.localhost")


def test_sidecar_allows_vite_dev_cors_preflight() -> None:
    client = TestClient(create_sidecar_app())

    response = client.options(
        "/openevo-api/desktop/shell",
        headers={
            "Origin": "http://localhost:5173",
            "Access-Control-Request-Method": "GET",
        },
    )

    assert response.status_code == 200
    assert response.headers["access-control-allow-origin"] == "http://localhost:5173"


def test_sidecar_rejects_arbitrary_localhost_cors_preflight() -> None:
    client = TestClient(create_sidecar_app())

    response = client.options(
        "/openevo-api/desktop/shell",
        headers={
            "Origin": "http://localhost:3766",
            "Access-Control-Request-Method": "GET",
        },
    )

    assert response.status_code == 400
    assert "access-control-allow-origin" not in response.headers


def _remote_capabilities_payload(execution_mode: str) -> dict[str, object]:
    snapshot = build_builtin_registry(
        ImplementationDistributionIdentity(
            distribution="openevo-test",
            distribution_version="0.1.0-test",
            distribution_digest="a" * 64,
        )
    )
    return build_evolution_capabilities(
        snapshot,
        profile=execution_profile_for_release_mode(execution_mode),
        audience=CapabilityAudience.DESKTOP,
        core_version="0.1.0-test",
    ).model_dump(mode="json")


class _CapabilitiesBackendClient:
    def __init__(
        self,
        payload: object,
        *,
        validation_error: sidecar_api.DesktopBackendError | None = None,
    ) -> None:
        self.payload = payload
        self.calls: list[str] = []
        self.validation_calls: list[dict[str, object]] = []
        self.validation_error = validation_error

    def capabilities(self, execution_mode: str) -> object:
        self.calls.append(execution_mode)
        return self.payload

    def validate_evolution_project(
        self,
        payload: dict[str, object],
    ) -> dict[str, object]:
        self.validation_calls.append(payload)
        if self.validation_error is not None:
            raise self.validation_error
        return {
            "valid": True,
            "registry_digest": payload["expected_registry_digest"],
        }


def _capabilities_backend_factory(project: ScienceProjectConfig):
    payload = _remote_capabilities_payload(project.execution.mode)
    return lambda: _CapabilitiesBackendClient(payload)


@pytest.mark.parametrize(
    "execution_mode",
    ["codex_subscription_transcript", "self-deployed"],
)
def test_sidecar_forwards_remote_capabilities_for_execution_mode(
    execution_mode: str,
) -> None:
    backend = _CapabilitiesBackendClient(_remote_capabilities_payload(execution_mode))
    client = TestClient(create_sidecar_app(backend_client_factory=lambda: backend))
    headers = {"X-OpenEvo-Sidecar-Token": _sidecar_token(client)}

    response = client.get(
        "/openevo-api/desktop/capabilities",
        params={"execution_mode": execution_mode},
        headers=headers,
    )

    assert response.status_code == 200
    assert response.json() == _remote_capabilities_payload(execution_mode)
    assert backend.calls == [execution_mode]


def test_sidecar_capabilities_requires_sidecar_token() -> None:
    backend = _CapabilitiesBackendClient(_remote_capabilities_payload("self-deployed"))
    client = TestClient(create_sidecar_app(backend_client_factory=lambda: backend))

    response = client.get(
        "/openevo-api/desktop/capabilities",
        params={"execution_mode": "self-deployed"},
    )

    assert response.status_code == 403
    assert response.json()["detail"] == "Invalid OpenEvo sidecar token."
    assert backend.calls == []


def test_sidecar_capabilities_requires_execution_mode_query() -> None:
    backend = _CapabilitiesBackendClient(_remote_capabilities_payload("self-deployed"))
    client = TestClient(create_sidecar_app(backend_client_factory=lambda: backend))

    response = client.get(
        "/openevo-api/desktop/capabilities",
        headers={"X-OpenEvo-Sidecar-Token": _sidecar_token(client)},
    )

    assert response.status_code == 422
    assert backend.calls == []


def test_sidecar_capabilities_reports_typed_setup_error_without_backend() -> None:
    client = TestClient(create_sidecar_app())

    response = client.get(
        "/openevo-api/desktop/capabilities",
        params={"execution_mode": "self-deployed"},
        headers={"X-OpenEvo-Sidecar-Token": _sidecar_token(client)},
    )

    assert response.status_code == 409
    assert response.json()["code"] == "backend_tunnel_not_configured"
    assert response.json()["severity"] == "blocking"


def test_active_session_capabilities_never_fall_back_to_shared_backend() -> None:
    project = ScienceProjectConfig.model_validate(_science_project_payload())
    client = TestClient(
        create_sidecar_app_for_project(
            project,
            _remote_profile(),
            transport_factory=lambda _profile: _ApiDryRunTransport(),
            backend_connection=BackendConnection("http://127.0.0.1:9"),
        )
    )

    response = client.get(
        "/openevo-api/desktop/capabilities",
        params={"execution_mode": project.execution.mode},
        headers={"X-OpenEvo-Sidecar-Token": _sidecar_token(client)},
    )

    assert response.status_code == 409
    assert response.json()["code"] == "backend_tunnel_not_configured"


def test_sidecar_capabilities_preserves_remote_typed_error() -> None:
    class ErrorBackendClient:
        def capabilities(self, execution_mode: str) -> object:
            raise sidecar_api.DesktopBackendError(
                503,
                {
                    "code": "capabilities_unavailable",
                    "message": "Remote capabilities are unavailable.",
                    "severity": "blocking",
                    "category": "service",
                    "retryable": True,
                    "repair_action": "openevo_can_retry",
                    "details": {"execution_mode": execution_mode},
                    "logs_ref": "services/openevo-backend",
                },
            )

    client = TestClient(create_sidecar_app(backend_client_factory=ErrorBackendClient))
    response = client.get(
        "/openevo-api/desktop/capabilities",
        params={"execution_mode": "codex_subscription_transcript"},
        headers={"X-OpenEvo-Sidecar-Token": _sidecar_token(client)},
    )

    assert response.status_code == 503
    assert response.json()["code"] == "capabilities_unavailable"
    assert response.json()["details"] == {"execution_mode": "codex_subscription_transcript"}


def test_sidecar_capabilities_rejects_invalid_remote_payload() -> None:
    backend = _CapabilitiesBackendClient(
        {**_remote_capabilities_payload("self-deployed"), "unexpected": True}
    )
    client = TestClient(create_sidecar_app(backend_client_factory=lambda: backend))

    response = client.get(
        "/openevo-api/desktop/capabilities",
        params={"execution_mode": "self-deployed"},
        headers={"X-OpenEvo-Sidecar-Token": _sidecar_token(client)},
    )

    assert response.status_code == 502
    assert response.json()["code"] == "backend_capabilities_invalid"
    assert response.json()["severity"] == "blocking"
    assert response.json()["details"]["execution_mode"] == "self-deployed"
    assert "validation_errors" not in response.json()["details"]
    assert "unexpected" not in str(response.json()["details"])


@pytest.mark.parametrize("owner", ["target", "method"])
def test_sidecar_capabilities_rejects_non_desktop_visible_entries(
    owner: str,
) -> None:
    payload = _remote_capabilities_payload("self-deployed")
    targets = cast(list[dict[str, object]], payload["targets"])
    if owner == "target":
        targets[0]["exposure"] = "internal"
    else:
        methods = cast(list[dict[str, object]], targets[0]["methods"])
        methods[0]["exposure"] = "maintainer"
    backend = _CapabilitiesBackendClient(payload)
    client = TestClient(create_sidecar_app(backend_client_factory=lambda: backend))

    response = client.get(
        "/openevo-api/desktop/capabilities",
        params={"execution_mode": "self-deployed"},
        headers={"X-OpenEvo-Sidecar-Token": _sidecar_token(client)},
    )

    assert response.status_code == 502
    assert response.json()["code"] == "backend_capabilities_invalid"


@pytest.mark.parametrize(
    ("field", "encoded"),
    [
        (
            "config_schema_json",
            '{"$ref":"#","additionalProperties":false,"properties":{},"type":"object"}',
        ),
        ("default_config_json", '{"count":9007199254740992}'),
    ],
)
def test_sidecar_capabilities_rejects_unrenderable_method_config_contract(
    field: str,
    encoded: str,
) -> None:
    payload = _remote_capabilities_payload("self-deployed")
    targets = cast(list[dict[str, object]], payload["targets"])
    methods = cast(list[dict[str, object]], targets[0]["methods"])
    method = methods[0]
    if field == "default_config_json":
        method["config_schema_json"] = (
            '{"additionalProperties":false,"properties":{"count":'
            '{"type":"integer"}},"type":"object"}'
        )
    method[field] = encoded
    backend = _CapabilitiesBackendClient(payload)
    client = TestClient(create_sidecar_app(backend_client_factory=lambda: backend))

    response = client.get(
        "/openevo-api/desktop/capabilities",
        params={"execution_mode": "self-deployed"},
        headers={"X-OpenEvo-Sidecar-Token": _sidecar_token(client)},
    )

    assert response.status_code == 502
    assert response.json()["code"] == "backend_capabilities_invalid"
    assert response.json()["details"] == {"execution_mode": "self-deployed"}
    assert encoded not in str(response.json())


@pytest.mark.parametrize("mismatch", ["identity", "support"])
def test_sidecar_capabilities_rejects_resolver_method_metadata_mismatch(
    mismatch: str,
) -> None:
    payload = _remote_capabilities_payload("self-deployed")
    agent_system = next(
        target
        for target in cast(list[dict[str, object]], payload["targets"])
        if target["target_id"] == "agent_system"
    )
    accepted = next(
        method
        for method in cast(list[dict[str, object]], agent_system["accepted_methods"])
        if method["method_id"] == "agent_system_history_reflector"
    )
    if mismatch == "identity":
        accepted["implementation_identity_digest"] = "b" * 64
    else:
        support = cast(dict[str, object], accepted["support"])
        support["overall"] = "unavailable"
        support["runtime"] = {
            "state": "unavailable",
            "reason_code": "missing_runtime_capabilities",
            "message": "Required runtime capability is unavailable.",
            "missing_requirements": ["adapter_serving"],
        }
    backend = _CapabilitiesBackendClient(payload)
    client = TestClient(create_sidecar_app(backend_client_factory=lambda: backend))

    response = client.get(
        "/openevo-api/desktop/capabilities",
        params={"execution_mode": "self-deployed"},
        headers={"X-OpenEvo-Sidecar-Token": _sidecar_token(client)},
    )

    assert response.status_code == 502
    assert response.json()["code"] == "backend_capabilities_invalid"


@pytest.mark.parametrize(
    ("execution_mode", "profile_field", "invalid_value"),
    [
        ("self-deployed", "execution_mode", "subscription"),
        ("self-deployed", "capture_mode", "proxy"),
        ("self-deployed", "harness_id", "claude-code"),
        ("self-deployed", "harness_capabilities", ["unexpected"]),
        ("self-deployed", "runtime_capabilities", ["adapter_serving"]),
        (
            "codex_subscription_transcript",
            "execution_mode",
            "self_deployed",
        ),
    ],
)
def test_sidecar_capabilities_rejects_release_profile_mismatch(
    execution_mode: str,
    profile_field: str,
    invalid_value: object,
) -> None:
    payload = _remote_capabilities_payload(execution_mode)
    evaluated_profile = cast(dict[str, object], payload["evaluated_profile"])
    evaluated_profile[profile_field] = invalid_value
    backend = _CapabilitiesBackendClient(payload)
    client = TestClient(create_sidecar_app(backend_client_factory=lambda: backend))

    response = client.get(
        "/openevo-api/desktop/capabilities",
        params={"execution_mode": execution_mode},
        headers={"X-OpenEvo-Sidecar-Token": _sidecar_token(client)},
    )

    assert response.status_code == 502
    assert response.json()["code"] == "backend_capabilities_invalid"
    assert response.json()["details"] == {"execution_mode": execution_mode}
    assert str(invalid_value) not in str(response.json()["details"])


def test_sidecar_methods_endpoint_is_removed() -> None:
    client = TestClient(create_sidecar_app())

    response = client.get("/openevo-api/desktop/methods")

    assert response.status_code == 404


def assert_desktop_science_payload(payload: dict[str, object]) -> None:
    execution = payload["execution"]
    assert isinstance(execution, dict)
    execution_payload = cast(dict[str, object], execution)
    assert execution_payload["mode"] in {
        "codex_subscription_transcript",
        "self-deployed",
    }
    assert "token_metrics_available" in execution_payload
    diagnostics = payload.get("diagnostics", {})
    assert isinstance(diagnostics, dict)
    assert "capabilities" not in diagnostics


def test_desktop_science_smoke_exercises_ordinary_user_route_set(
    tmp_path: Path,
) -> None:
    class ProductBackendClient:
        def capabilities(self, execution_mode: str) -> dict[str, object]:
            return _remote_capabilities_payload(execution_mode)

        def validate_evolution_project(
            self,
            payload: dict[str, object],
        ) -> dict[str, object]:
            return {
                "valid": True,
                "registry_digest": payload["expected_registry_digest"],
            }

        def run_timeline(self, run_id: str) -> list[dict[str, object]]:
            return [
                {
                    "id": f"{run_id}:memory",
                    "phase": "evolution",
                    "title": "Memory updated",
                    "message": "Text memory worker promoted one artifact.",
                    "artifact_ids": ["artifact-text-memory"],
                }
            ]

        def run_artifacts(self, run_id: str) -> list[dict[str, object]]:
            return [
                {
                    "id": "artifact-text-memory",
                    "run_id": run_id,
                    "artifact_type": "text_memory",
                    "title": "Initial memory draft",
                    "promoted": True,
                    "lineage": {"method": "text_memory_reflector"},
                }
            ]

        def artifact_content(self, artifact_id: str) -> dict[str, object]:
            return {
                "id": artifact_id,
                "artifact_type": "text_memory",
                "content": "# Learned Memory\n\n- Prefer stable folds.\n",
                "metadata": {
                    "target_path": "memory.md",
                    "lineage": {"method": "text_memory_reflector"},
                },
            }

        def artifact_diff(self, artifact_id: str) -> dict[str, object]:
            return {
                "id": artifact_id,
                "before": "",
                "after": "# Learned Memory\n\n- Prefer stable folds.\n",
                "format": "unified_text",
            }

    transport = _ApiLifecycleTransport(pid_states=_ready_service_pid_states())
    client = TestClient(
        create_sidecar_app(
            config_root=tmp_path,
            transport_factory=lambda _profile: transport,
            backend_client_factory=ProductBackendClient,
        )
    )
    token = _sidecar_token(client)
    headers = {"X-OpenEvo-Sidecar-Token": token}

    capabilities = client.get(
        "/openevo-api/desktop/capabilities",
        params={"execution_mode": "codex_subscription_transcript"},
        headers=headers,
    )
    shell = client.get("/openevo-api/desktop/shell")
    project_config = client.post(
        "/openevo-api/desktop/project-config",
        headers=headers,
        json=_desktop_config_draft_payload(),
    )
    project_configs = client.get("/openevo-api/desktop/project-configs")
    workspace = client.post("/openevo-api/desktop/workspace", headers=headers)
    bootstrap = client.post("/openevo-api/desktop/bootstrap", headers=headers)
    services = client.post("/openevo-api/desktop/services", headers=headers)
    services_status = client.get(
        "/openevo-api/desktop/services/status",
        headers=headers,
    )
    launch = client.post("/openevo-api/desktop/run", headers=headers)
    terminal = _wait_latest_run_state(client, headers, "succeeded")
    run_id = terminal["run"]["id"]
    timeline = client.get(
        f"/openevo-api/backend/runs/{run_id}/timeline",
        headers=headers,
    )
    artifacts = client.get(
        f"/openevo-api/backend/runs/{run_id}/artifacts",
        headers=headers,
    )
    content = client.get(
        "/openevo-api/backend/artifacts/artifact-text-memory/content",
        headers=headers,
    )
    diff = client.get(
        "/openevo-api/backend/artifacts/artifact-text-memory/diff",
        headers=headers,
    )

    for response in (
        capabilities,
        shell,
        project_config,
        project_configs,
        workspace,
        bootstrap,
        services,
        services_status,
        launch,
        timeline,
        artifacts,
        content,
        diff,
    ):
        assert response.status_code == 200
    assert_desktop_science_payload(cast(dict[str, object], shell.json()))
    assert_desktop_science_payload(cast(dict[str, object], project_config.json()["status"]))
    assert capabilities.json()["evaluated_profile"]["execution_mode"] == ("subscription")
    assert services_status.json()["ready"] is True
    assert timeline.json()[0]["artifact_ids"] == ["artifact-text-memory"]
    assert artifacts.json()[0]["run_id"] == launch.json()["run"]["id"]
    assert terminal["run"]["ready"] is True
    assert content.json()["content"].startswith("# Learned Memory")
    assert diff.json()["after"].startswith("# Learned Memory")


def test_desktop_shell_endpoint_starts_in_setup_required_state() -> None:
    client = TestClient(create_sidecar_app())

    response = client.get("/openevo-api/desktop/shell")

    assert response.status_code == 200
    payload = response.json()
    assert payload["remote"]["id"] == "not-configured"
    assert payload["remote"]["host"] == ""
    assert payload["project"]["name"] == "Untitled Science Project"
    assert payload["execution"]["mode"] == "codex_subscription_transcript"
    assert payload["execution"]["token_metrics_available"] is False
    assert payload["bootstrap"]["ready"] is False
    assert payload["bootstrap"]["readiness_notes"] == [
        "Configure a project and remote backend to begin."
    ]
    assert "Protein Folding Literature Sprint" not in json.dumps(payload)
    assert "gpu.example.edu" not in json.dumps(payload)


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
    assert payload["report"]["prepared_paths"]["bootstrap_manifest"].endswith("/bootstrap.json")
    assert payload["status"]["bootstrap"]["ready"] is True
    assert payload["status"]["bootstrap"]["readiness_notes"] == ["Remote bootstrap is ready."]
    services = {service["id"]: service for service in payload["status"]["services"]}
    assert services["ssh"]["state"] == "ready"
    assert services["ssh"]["detail"] == "Remote preflight passed"
    assert services["workspace"]["state"] == "ready"
    assert services["workspace"]["detail"] == "Workspace source is already remote"
    assert services["bootstrap"]["state"] == "ready"
    assert services["bootstrap"]["detail"] == "Runtime image and manifests prepared"

    status_response = client.get("/openevo-api/desktop/shell")
    assert status_response.json()["bootstrap"]["ready"] is True


def test_bootstrap_uploads_exact_core_wheel_when_available(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    wheel_dir = tmp_path / "dist"
    wheel = _write_openevo_wheel(
        wheel_dir / f"openevo-{sidecar_api.OPENEVO_VERSION}-py3-none-any.whl"
    )
    monkeypatch.setattr(sidecar_api, "discover_local_openevo_wheel", lambda: wheel)
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

    response = client.post(
        "/openevo-api/desktop/bootstrap",
        headers={"X-OpenEvo-Sidecar-Token": token},
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["report"]["ready"] is True
    assert len(transport.uploads) == 1
    uploaded_local, uploaded_remote = transport.uploads[0]
    assert Path(uploaded_local) != wheel_dir
    assert transport.upload_contents == [["framework-lock.json", wheel.name]]
    assert uploaded_remote == ("/home/alice/.openevo/runs/protein-design/folding-baseline/wheels")
    ensure_step = next(
        step for step in payload["report"]["steps"] if step["id"] == "ensure_openevo_cli"
    )
    remote_wheel = (
        "/home/alice/.openevo/runs/protein-design/"
        f"folding-baseline/wheels/openevo-{sidecar_api.OPENEVO_VERSION}-py3-none-any.whl"
    )
    assert ensure_step["remediation_kind"] == "upload_exact_openevo_wheel"
    assert remote_wheel in ensure_step["command"]
    assert f"expected = '{sidecar_api.OPENEVO_VERSION}'" in ensure_step["command"]
    assert "pip install --user --upgrade openevo" not in ensure_step["command"]


def test_bootstrap_reports_sanitized_exact_core_wheel_upload_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    wheel = _write_openevo_wheel(
        tmp_path / f"openevo-{sidecar_api.OPENEVO_VERSION}-py3-none-any.whl"
    )
    monkeypatch.setattr(sidecar_api, "discover_local_openevo_wheel", lambda: wheel)
    project = ScienceProjectConfig.model_validate(_science_project_payload())
    profile = _remote_profile(
        proxy={"https_proxy": "http://proxy-user:proxy-secret@127.0.0.1:7890"}
    )
    transport = _WheelUploadFailingTransport()
    client = TestClient(
        create_sidecar_app_for_project(
            project,
            profile,
            transport_factory=lambda _profile: transport,
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
    assert payload["report"]["steps"][0]["id"] == "ensure_openevo_cli"
    assert payload["report"]["steps"][0]["status"] == "fail"
    assert payload["report"]["steps"][0]["remediation_kind"] == ("upload_exact_openevo_wheel")
    assert "proxy-secret" not in payload["report"]["steps"][0]["stderr"]
    assert "[REDACTED]" in payload["report"]["steps"][0]["stderr"]


def test_bootstrap_runs_preflight_before_exact_core_wheel_upload(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    wheel = _write_openevo_wheel(
        tmp_path / f"openevo-{sidecar_api.OPENEVO_VERSION}-py3-none-any.whl"
    )
    monkeypatch.setattr(sidecar_api, "discover_local_openevo_wheel", lambda: wheel)
    project = ScienceProjectConfig.model_validate(_science_project_payload())
    profile = _remote_profile()
    transport = _FailingPreflightTransport()
    client = TestClient(
        create_sidecar_app_for_project(
            project,
            profile,
            transport_factory=lambda _profile: transport,
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
    assert payload["report"]["steps"] == []
    assert payload["report"]["preflight"]["checks"][0]["name"] == "ssh"
    assert payload["report"]["preflight"]["checks"][0]["status"] == "fail"
    assert transport.uploads == []


def test_wheel_discovery_ignores_untrusted_cwd_dist(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    dist = tmp_path / "dist"
    _write_openevo_wheel(
        dist / f"openevo-{sidecar_api.OPENEVO_VERSION}-py3-none-any.whl"
    )
    monkeypatch.chdir(tmp_path)

    assert sidecar_api.discover_local_openevo_wheel() is None


def test_wheel_discovery_uses_only_package_relative_bundled_dirs() -> None:
    package_root = Path(sidecar_api.openevo.__file__).resolve().parent

    assert sidecar_api._openevo_wheel_search_dirs() == (package_root / "wheels",)


def test_wheel_discovery_requires_matching_openevo_metadata(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    trusted = tmp_path / "trusted"
    invalid = _write_openevo_wheel(
        trusted / f"openevo-{sidecar_api.OPENEVO_VERSION}-py3-none-any.whl",
        metadata_name="not-openevo",
    )
    valid = _write_openevo_wheel(
        trusted / f"openevo-{sidecar_api.OPENEVO_VERSION}-local-py3-none-any.whl"
    )
    monkeypatch.setattr(sidecar_api, "_openevo_wheel_search_dirs", lambda: (trusted,))

    assert sidecar_api.discover_local_openevo_wheel() == valid
    invalid.unlink()
    valid.unlink()
    _write_openevo_wheel(
        trusted / f"openevo-{sidecar_api.OPENEVO_VERSION}-py3-none-any.whl",
        metadata_version="0.2.0",
    )

    assert sidecar_api.discover_local_openevo_wheel() is None


def test_core_artifact_endpoint_reports_exact_packaged_wheel_identity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    trusted = tmp_path / "trusted"
    wheel = _write_openevo_wheel(
        trusted / f"openevo-{sidecar_api.OPENEVO_VERSION}-py3-none-any.whl"
    )
    monkeypatch.setattr(sidecar_api, "_openevo_wheel_search_dirs", lambda: (trusted,))

    response = TestClient(create_sidecar_app()).get("/openevo-api/desktop/core-artifact")

    assert response.status_code == 200
    payload = response.json()
    digest = hashlib.sha256(wheel.read_bytes()).hexdigest()
    assert payload == {
        "available": True,
        "distribution": "openevo",
        "distribution_version": sidecar_api.OPENEVO_VERSION,
        "wheel_filename": wheel.name,
        "distribution_digest": digest,
        "framework_lock": {
            "schema_version": "1",
            "distribution": "openevo",
            "distribution_version": sidecar_api.OPENEVO_VERSION,
            "distribution_digest": digest,
            "wheel_filename": wheel.name,
        },
    }


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
    services = {service["id"]: service for service in response.json()["status"]["services"]}
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
    assert response.json()["detail"] == ("Desktop services require ready workspace and bootstrap.")


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
    assert any("python3 -m openevo.rollout.server" in command for command in transport.commands)
    assert any("python3 -m openevo.gateway.server" in command for command in transport.commands)
    assert any(
        "python3 -m openevo.rollout.server --config" in command for command in transport.commands
    )
    assert any(
        "python3 -m openevo.gateway.server --config" in command for command in transport.commands
    )
    services = {service["id"]: service for service in payload["status"]["services"]}
    assert services["openevo-backend"] == {
        "id": "openevo-backend",
        "label": "OpenEvo backend",
        "state": "ready",
        "detail": "Remote runtime services are ready",
    }


def test_legacy_services_endpoint_does_not_open_a_core_tunnel() -> None:
    project = ScienceProjectConfig.model_validate(_science_project_payload())
    profile = _remote_profile()
    with _BackendFacadeTestServer() as backend:
        transport = _BackendTunnelTransport(backend.base_url)
        with TestClient(
            create_sidecar_app_for_project(
                project,
                profile,
                transport_factory=lambda _profile: transport,
                transport_kind="ssh",
            )
        ) as client:
            token = _sidecar_token(client)
            headers = {"X-OpenEvo-Sidecar-Token": token}
            _prepare_workspace_and_bootstrap(client, headers)

            before_services = client.get("/openevo-api/backend/health", headers=headers)
            services = client.post("/openevo-api/desktop/services", headers=headers)
            health = client.get("/openevo-api/backend/health", headers=headers)
            timeline = client.get(
                "/openevo-api/backend/runs/run-1/timeline",
                headers=headers,
            )

        assert transport.tunnel_requests == []
        assert transport.tunnel is None

    assert before_services.status_code == 409
    assert before_services.json()["code"] == "backend_tunnel_not_configured"
    assert services.status_code == 200
    assert services.json()["services"]["ready"] is True
    assert health.status_code == 409
    assert health.json()["code"] == "backend_tunnel_not_configured"
    assert timeline.status_code == 409
    assert timeline.json()["code"] == "backend_tunnel_not_configured"


def test_backend_facade_reads_actual_sidecar_run_state_from_remote_backend(
    tmp_path: Path,
) -> None:
    project = ScienceProjectConfig.model_validate(_science_project_payload())
    profile = RemoteProfileConfig(
        version=1,
        id="science-team",
        host="gpu.example.edu",
        user="alice",
        workspace_root=str(tmp_path / "workspaces"),
        proxy={"https_proxy": "http://127.0.0.1:7890"},
    )
    state_root = tmp_path / "runs" / "protein-design" / "folding-baseline"
    with _BackendApiStateRootTestServer(state_root) as backend:
        transport = _StateRootRunTransport(
            state_root=state_root,
            backend_base_url=backend.base_url,
        )
        with TestClient(
            create_sidecar_app_for_project(
                project,
                profile,
                transport_factory=lambda _profile: transport,
                transport_kind="ssh",
                backend_client_factory=lambda: BackendClient(
                    BackendConnection(backend.base_url)
                ),
            )
        ) as client:
            token = _sidecar_token(client)
            headers = {"X-OpenEvo-Sidecar-Token": token}
            _prepare_workspace_bootstrap_and_services(client, headers)

            launch = client.post("/openevo-api/desktop/run", headers=headers)
            terminal = _wait_latest_run_state(client, headers, "succeeded")
            run_id = terminal["run"]["id"]
            timeline = client.get(
                f"/openevo-api/backend/runs/{run_id}/timeline",
                headers=headers,
            )
            artifacts = client.get(
                f"/openevo-api/backend/runs/{run_id}/artifacts",
                headers=headers,
            )
            artifact_id = artifacts.json()[0]["id"]
            content = client.get(
                f"/openevo-api/backend/artifacts/{artifact_id}/content",
                headers=headers,
            )
            diff = client.get(
                f"/openevo-api/backend/artifacts/{artifact_id}/diff",
                headers=headers,
            )

    assert launch.status_code == 200
    assert timeline.status_code == 200
    assert any(artifact_id in event["artifact_ids"] for event in timeline.json())
    assert artifacts.status_code == 200
    assert artifacts.json()[0]["run_id"] == run_id
    assert artifacts.json()[0]["artifact_type"] == "text_memory"
    assert artifacts.json()[0]["promoted"] is True
    assert content.status_code == 200
    assert content.json()["content"].startswith("# Learned Memory")
    assert diff.status_code == 200
    assert diff.json()["after"].startswith("# Learned Memory")


def test_services_endpoint_ignores_removed_legacy_backend_tunnel() -> None:
    project = ScienceProjectConfig.model_validate(_science_project_payload())
    profile = _remote_profile()
    transport = _FailingBackendTunnelTransport()
    client = TestClient(
        create_sidecar_app_for_project(
            project,
            profile,
            transport_factory=lambda _profile: transport,
            transport_kind="ssh",
        )
    )
    token = _sidecar_token(client)
    headers = {"X-OpenEvo-Sidecar-Token": token}

    assert client.post("/openevo-api/desktop/workspace", headers=headers).status_code == 200
    assert client.post("/openevo-api/desktop/bootstrap", headers=headers).status_code == 200
    services = client.post("/openevo-api/desktop/services", headers=headers)
    shell = client.get("/openevo-api/desktop/shell")

    assert services.status_code == 200
    assert transport.tunnel_requests == []
    service_rows = {item["id"]: item for item in shell.json()["services"]}
    assert service_rows["openevo-backend"]["state"] == "ready"
    assert service_rows["openevo-backend"]["detail"] == "Remote runtime services are ready"


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
    rollout = next(step for step in payload["report"]["steps"] if step["id"] == "rollout")
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


def test_services_logs_endpoint_returns_structured_log_on_transport_exception() -> None:
    project = ScienceProjectConfig.model_validate(_science_project_payload())
    profile = _remote_profile()
    transport = _ApiLifecycleTransport(
        log_exception=RuntimeError(
            "tail failed via http://proxy-user:proxy-secret@127.0.0.1:7890\n"
            "Authorization: Bearer secret-token"
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
    assert payload["line_count"] == 2
    assert "tail failed" in payload["content"]
    assert "proxy-secret" not in payload["content"]
    assert "secret-token" not in payload["content"]
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
    assert any("python3 -m openevo.gateway.server" in command for command in transport.commands)
    assert not any(
        "python3 -m openevo.rollout.server" in command for command in transport.commands
    )


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
    services = {service["id"]: service for service in status_response.json()["services"]}
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
    draft = _desktop_config_draft_payload()
    evolution = cast(dict[str, object], draft["evolution"])
    targets = cast(dict[str, object], evolution["targets"])
    targets["quality_notes_external"] = {
        "enabled": False,
        "method": "synthesize_notes",
        "config": {
            "style": "concise",
            "limits": {"records": 8},
        },
    }
    saved = writer.post(
        "/openevo-api/desktop/project-config",
        headers={"X-OpenEvo-Sidecar-Token": writer_token},
        json=draft,
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
    assert (
        payload["status"]["project"]["evolution_targets"]["quality_notes_external"]
        == targets["quality_notes_external"]
    )
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
    assert science_yaml["evolution"] == {"targets": _evolution_targets_payload()}
    assert "artifacts" not in science_yaml
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
    profile = _remote_profile(
        proxy={
            "https_proxy": "http://127.0.0.1:7890",
            "extra_env": {"HF_ENDPOINT": "https://hf-mirror.example.test"},
        }
    )
    transport = _ApiDryRunTransport()
    client = TestClient(
        create_sidecar_app_for_project(
            project,
            profile,
            transport_factory=lambda _profile: transport,
            backend_client_factory=_capabilities_backend_factory(project),
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
    artifact_root = f"{state_root}/evolution/artifacts"
    framework_lock = f"{state_root}/wheels/framework-lock.json"
    expected_command = (
        f'PATH="$HOME/.local/bin:$PATH" openevo-backend run {experiment_snapshot} '
        f"--output-dir {output_dir} --artifact-root {artifact_root} "
        f"--framework-lock {framework_lock} --json"
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
    assert transport.run_envs[-1] == profile.proxy.to_env()
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


def test_run_endpoint_revalidates_active_selections_against_remote_registry() -> None:
    project = ScienceProjectConfig.model_validate(_science_project_payload())
    profile = _remote_profile()
    transport = _ApiDryRunTransport()
    payload = _remote_capabilities_payload(project.execution.mode)
    backend = _CapabilitiesBackendClient(payload)
    client = TestClient(
        create_sidecar_app_for_project(
            project,
            profile,
            transport_factory=lambda _profile: transport,
            backend_client_factory=lambda: backend,
        )
    )
    token = _sidecar_token(client)
    headers = {"X-OpenEvo-Sidecar-Token": token}
    _prepare_workspace_bootstrap_and_services(client, headers)
    discovered = client.get(
        "/openevo-api/desktop/capabilities",
        params={"execution_mode": project.execution.mode},
        headers=headers,
    )
    assert discovered.status_code == 200

    text_memory = next(
        target
        for target in cast(list[dict[str, object]], payload["targets"])
        if target["target_id"] == "text_memory"
    )
    text_memory["accepted_methods"] = [
        method
        for method in cast(list[dict[str, object]], text_memory["accepted_methods"])
        if method["method_id"] != "text_memory_reflector"
    ]

    response = client.post("/openevo-api/desktop/run", headers=headers)

    assert response.status_code == 409
    assert response.json()["code"] == "evolution_selection_unavailable"
    assert response.json()["details"]["target_id"] == "text_memory"
    assert response.json()["details"]["selection"] == "text_memory_reflector"
    assert not any(
        command.startswith('PATH="$HOME/.local/bin:$PATH" openevo-backend run ')
        for command in transport.commands
    )


def test_run_endpoint_revalidates_visible_method_config_against_latest_schema() -> None:
    project = ScienceProjectConfig.model_validate(_science_project_payload())
    profile = _remote_profile()
    transport = _ApiDryRunTransport()
    payload = _remote_capabilities_payload(project.execution.mode)
    backend = _CapabilitiesBackendClient(payload)
    client = TestClient(
        create_sidecar_app_for_project(
            project,
            profile,
            transport_factory=lambda _profile: transport,
            backend_client_factory=lambda: backend,
        )
    )
    token = _sidecar_token(client)
    headers = {"X-OpenEvo-Sidecar-Token": token}
    _prepare_workspace_bootstrap_and_services(client, headers)

    skill_target = next(
        target
        for target in cast(list[dict[str, object]], payload["targets"])
        if target["target_id"] == "skill_bundle"
    )
    skill_method = next(
        method
        for method in cast(list[dict[str, object]], skill_target["methods"])
        if method["method_id"] == "skill_bundle_reflector"
    )
    skill_method["config_schema_json"] = (
        '{"additionalProperties":false,"properties":{"required_value":'
        '{"type":"string"}},"required":["required_value"],"type":"object"}'
    )
    skill_method["default_config_json"] = "{}"

    response = client.post("/openevo-api/desktop/run", headers=headers)

    assert response.status_code == 409
    assert response.json()["code"] == "evolution_config_invalid"
    assert response.json()["details"] == {
        "target_id": "skill_bundle",
        "selection": "skill_bundle_reflector",
        "registry_digest": payload["registry_digest"],
    }
    assert not any(
        command.startswith('PATH="$HOME/.local/bin:$PATH" openevo-backend run ')
        for command in transport.commands
    )


@pytest.mark.parametrize(
    ("target_id", "selection", "execution"),
    [
        (
            "text_memory",
            "text_memory",
            {
                "mode": "self-deployed",
                "hf_model": "Qwen/Qwen3-Coder-30B-A3B-Instruct",
            },
        ),
        ("agent_system", "auto", None),
    ],
    ids=["hidden-method", "resolver"],
)
def test_run_endpoint_requires_remote_validation_for_opaque_config(
    target_id: str,
    selection: str,
    execution: dict[str, object] | None,
) -> None:
    project_payload = _science_project_payload()
    if execution is not None:
        project_payload["execution"] = execution
    targets = cast(dict[str, object], project_payload["evolution"])["targets"]
    assert isinstance(targets, dict)
    targets[target_id] = {
        "enabled": True,
        "method": selection,
        "config": {"unexpected": True},
    }
    project = ScienceProjectConfig.model_validate(project_payload)
    profile = _remote_profile()
    transport = _ApiDryRunTransport()
    validation_error = sidecar_api.DesktopBackendError(
        409,
        {
            "code": "evolution_project_invalid",
            "message": "The active project evolution configuration is invalid.",
            "severity": "blocking",
            "category": "project",
            "retryable": False,
            "repair_action": "openevo_can_reconfigure",
            "details": {
                "target_id": target_id,
                "selection": selection,
                "reason_code": "invalid_method_config_or_profile",
                "registry_digest": "a" * 64,
            },
            "logs_ref": None,
        },
    )
    backend = _CapabilitiesBackendClient(
        _remote_capabilities_payload(project.execution.mode),
        validation_error=validation_error,
    )
    client = TestClient(
        create_sidecar_app_for_project(
            project,
            profile,
            transport_factory=lambda _profile: transport,
            backend_client_factory=lambda: backend,
        )
    )
    headers = {"X-OpenEvo-Sidecar-Token": _sidecar_token(client)}
    _prepare_workspace_bootstrap_and_services(client, headers)

    response = client.post("/openevo-api/desktop/run", headers=headers)

    assert response.status_code == 409
    assert response.json()["code"] == "evolution_project_invalid"
    assert response.json()["details"]["target_id"] == target_id
    assert len(backend.validation_calls) == 1
    assert (
        backend.validation_calls[0]["expected_registry_digest"]
        == (_remote_capabilities_payload(project.execution.mode)["registry_digest"])
    )
    assert not any(
        command.startswith('PATH="$HOME/.local/bin:$PATH" openevo-backend run ')
        for command in transport.commands
    )


def test_run_endpoint_uses_installed_backend_entrypoint() -> None:
    project = ScienceProjectConfig.model_validate(_science_project_payload())
    profile = _remote_profile()
    transport = _ApiDryRunTransport()
    client = TestClient(
        create_sidecar_app_for_project(
            project,
            profile,
            transport_factory=lambda _profile: transport,
            backend_client_factory=_capabilities_backend_factory(project),
        )
    )
    token = _sidecar_token(client)
    headers = {"X-OpenEvo-Sidecar-Token": token}
    _prepare_workspace_bootstrap_and_services(client, headers)

    response = client.post("/openevo-api/desktop/run", headers=headers)

    assert response.status_code == 200
    scripts = tomllib.loads(Path("pyproject.toml").read_text(encoding="utf-8"))["project"][
        "scripts"
    ]
    tokens = shlex.split(response.json()["run"]["command"])
    assert tokens[0].startswith("PATH=")
    assert tokens[1] == "openevo-backend"
    assert scripts[tokens[1]] == "openevo.backend.launcher:main"
    assert "openevo" not in scripts


def test_run_endpoint_preserves_command_failure_status() -> None:
    project = ScienceProjectConfig.model_validate(_science_project_payload())
    profile = _remote_profile()
    transport = _FailingRunTransport()
    client = TestClient(
        create_sidecar_app_for_project(
            project,
            profile,
            transport_factory=lambda _profile: transport,
            backend_client_factory=_capabilities_backend_factory(project),
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
            backend_client_factory=_capabilities_backend_factory(project),
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
            backend_client_factory=_capabilities_backend_factory(project),
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
    assert poll.json()["run"]["output_dir"] == (f"{state_root}/runs/{launch.json()['run']['id']}")
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
    assert (
        status.project.evolution_targets
        == ScienceProjectConfig.model_validate(_science_project_payload()).evolution.targets
    )


def test_subscription_transcript_status_rejects_token_metrics() -> None:
    with pytest.raises(ValueError, match="token_metrics_available"):
        DesktopExecutionStatus(
            mode="codex_subscription_transcript",
            model="gpt-5.5",
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
        "evolution": {"targets": _evolution_targets_payload()},
    }


def _evolution_targets_payload() -> dict:
    return {
        "text_memory": {
            "enabled": True,
            "method": "text_memory_reflector",
            "config": {},
        },
        "skill_bundle": {
            "enabled": True,
            "method": "skill_bundle_reflector",
            "config": {},
        },
        "agent_system": {
            "enabled": True,
            "method": "auto",
            "config": {"target_path": "AGENTS.md"},
        },
        "parametric_memory": {
            "enabled": False,
            "method": "parametric_memory_register",
            "config": {},
        },
    }


def _remote_profile(
    auth: dict | None = None,
    proxy: dict | None = None,
) -> RemoteProfileConfig:
    return RemoteProfileConfig(
        version=1,
        id="science-team",
        host="gpu.example.edu",
        user="alice",
        auth=auth or {"method": "ssh_agent"},
        proxy=proxy or {"https_proxy": "http://127.0.0.1:7890"},
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
        "codex_model": "gpt-5.5",
        "evolution": {"targets": _evolution_targets_payload()},
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


def _ready_service_pid_states() -> dict[str, dict[str, object]]:
    return {
        "evolution_backend": {"pid": 120, "alive": True},
        "rollout": {"pid": 121, "alive": True},
        "gateway": {"pid": 122, "alive": True},
        "evolution_worker": {"pid": 123, "alive": True},
    }


def _write_openevo_wheel(
    path: Path,
    *,
    metadata_name: str = "openevo",
    metadata_version: str = sidecar_api.OPENEVO_VERSION,
) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with ZipFile(path, "w") as wheel:
        wheel.writestr(
            f"openevo-{metadata_version}.dist-info/METADATA",
            "\n".join(
                [
                    "Metadata-Version: 2.4",
                    f"Name: {metadata_name}",
                    f"Version: {metadata_version}",
                    "",
                ]
            ),
        )
    return path


class _BackendFacadeTestServer:
    def __init__(self) -> None:
        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:
                path = urlparse(self.path).path
                if path == "/health":
                    self._write_json({"status": "ok"})
                    return
                if path == "/runs/run-1/timeline":
                    self._write_json(
                        [
                            {
                                "id": "run-1-created",
                                "phase": "created",
                                "title": "Run created",
                                "message": "codex run is queued.",
                                "artifact_ids": ["artifact-1"],
                            }
                        ]
                    )
                    return
                self.send_response(404)
                self.end_headers()

            def log_message(self, _format: str, *_args: object) -> None:
                return

            def _write_json(self, payload: object) -> None:
                data = json.dumps(payload).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._thread = Thread(target=self._server.serve_forever, daemon=True)

    @property
    def base_url(self) -> str:
        port = self._server.server_address[1]
        return f"http://127.0.0.1:{port}"

    def __enter__(self) -> "_BackendFacadeTestServer":
        self._thread.start()
        return self

    def __exit__(self, *_exc_info: object) -> None:
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=5)


class _BackendApiStateRootTestServer:
    def __init__(self, state_root: Path) -> None:
        registry = verified_builtin_registry(state_root.parent / "verified-registry")
        self._port = _allocate_test_port()
        self._server = uvicorn.Server(
            uvicorn.Config(
                create_backend_app(
                    state_root=state_root,
                    evolution_registry=registry,
                ),
                host="127.0.0.1",
                port=self._port,
                log_level="critical",
            )
        )
        self._thread = Thread(target=self._server.run, daemon=True)

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self._port}"

    def __enter__(self) -> "_BackendApiStateRootTestServer":
        self._thread.start()
        _wait_for_test_backend(self.base_url)
        return self

    def __exit__(self, *_exc_info: object) -> None:
        self._server.should_exit = True
        self._thread.join(timeout=5)


def _allocate_test_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _wait_for_test_backend(base_url: str) -> None:
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(f"{base_url}/health", timeout=0.25) as response:
                if response.status == 200:
                    return
        except OSError:
            time.sleep(0.05)
    raise RuntimeError("backend test server did not become ready")


class _BackendTunnel:
    def __init__(self, base_url: str) -> None:
        port = urlparse(base_url).port
        if port is None:
            raise AssertionError("test backend URL must include a port")
        self.local_port = port
        self.base_url = base_url
        self.closed = False

    def close(self) -> None:
        self.closed = True


class _BackendTunnelTransport:
    def __init__(self, base_url: str) -> None:
        self._base_url = base_url
        self.tunnel_requests: list[tuple[str, int]] = []
        self.tunnel: _BackendTunnel | None = None
        self._delegate = _ApiDryRunTransport()

    def run(
        self,
        command: str,
        *,
        cwd: str | None = None,
        env: dict[str, str] | None = None,
        timeout_seconds: float = 30.0,
    ) -> RemoteCommandResult:
        return self._delegate.run(
            command,
            cwd=cwd,
            env=env,
            timeout_seconds=timeout_seconds,
        )

    def upload_dir(self, local_path: str, remote_path: str) -> None:
        self._delegate.upload_dir(local_path, remote_path)

    def open_tunnel(
        self,
        *,
        remote_port: int,
        remote_host: str = "127.0.0.1",
        wait_for_ready: bool = True,
    ) -> _BackendTunnel:
        self.tunnel_requests.append((remote_host, remote_port))
        self.tunnel = _BackendTunnel(self._base_url)
        return self.tunnel


class _FailingBackendTunnelTransport(_BackendTunnelTransport):
    def __init__(self) -> None:
        super().__init__("http://127.0.0.1:1")


class _StateRootRunTransport(_BackendTunnelTransport):
    def __init__(self, *, state_root: Path, backend_base_url: str) -> None:
        super().__init__(backend_base_url)
        self._state_root = state_root

    def run(
        self,
        command: str,
        *,
        cwd: str | None = None,
        env: dict[str, str] | None = None,
        timeout_seconds: float = 30.0,
    ) -> RemoteCommandResult:
        if command.startswith('PATH="$HOME/.local/bin:$PATH" openevo-backend run '):
            self._delegate.commands.append(command)
            self._delegate.run_calls.append((command, cwd, timeout_seconds))
            self._write_run_output(command)
            return RemoteCommandResult(command=command, return_code=0, stdout="ok")
        return super().run(
            command,
            cwd=cwd,
            env=env,
            timeout_seconds=timeout_seconds,
        )

    def _write_run_output(self, command: str) -> None:
        tokens = shlex.split(command)
        output_dir = Path(tokens[tokens.index("--output-dir") + 1])
        artifact_root = Path(tokens[tokens.index("--artifact-root") + 1])
        assert artifact_root == self._state_root / "evolution" / "artifacts"
        store = EvolutionStore(
            db_path=self._state_root / "evolution" / "evolution.db",
            artifact_root=artifact_root,
        )
        store.initialize()
        artifact_dir = artifact_root / "run-memory"
        artifact_dir.mkdir(parents=True, exist_ok=True)
        (artifact_dir / "memory.md").write_text(
            "# Learned Memory\n\n- Prefer stable folds.\n",
            encoding="utf-8",
        )
        artifact = store.register_artifact(
            ArtifactRegisterRequest(
                type="text_memory",
                name="Learned memory",
                uri=artifact_dir.as_uri(),
                manifest={"content_path": "memory.md"},
                promoted=True,
            )
        )
        output_dir.mkdir(parents=True, exist_ok=True)
        run_id = output_dir.name
        (output_dir / "summary.json").write_text(
            json.dumps(
                {
                    "mode": "run",
                    "status": "completed",
                    "experiment_id": "biology-components",
                    "experiment_name": "Biology Components",
                    "run_id": run_id,
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
                                        "text_memory": [artifact.artifact_id],
                                    },
                                    "jobs": [
                                        {
                                            "artifact_type": "text_memory",
                                            "method": "text_memory_reflector",
                                            "worker_status": "succeeded",
                                            "artifact_ids": [artifact.artifact_id],
                                            "approved_artifact_ids": [artifact.artifact_id],
                                            "promotion_status": "approved",
                                        }
                                    ],
                                }
                            ],
                        }
                    ],
                    "summary_path": str(output_dir / "summary.json"),
                }
            ),
            encoding="utf-8",
        )


class _ApiDryRunTransport:
    def __init__(self) -> None:
        self.uploads: list[tuple[str, str]] = []
        self.upload_contents: list[list[str]] = []
        self.commands: list[str] = []
        self.run_calls: list[tuple[str, str | None, float]] = []
        self.run_envs: list[dict[str, str] | None] = []

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
        self.run_envs.append(env)
        if command == 'df -Pk "$HOME"':
            return RemoteCommandResult(
                command=command,
                return_code=0,
                stdout=(
                    "Filesystem 1024-blocks Used Available Capacity Mounted on\n"
                    "/dev/root 100000000 1 99999999 1% /home\n"
                ),
            )
        if "importlib.metadata" in command and "version('openevo')" in command:
            return RemoteCommandResult(
                command=command,
                return_code=0,
                stdout=f"{sidecar_api.OPENEVO_VERSION}\n",
            )
        if "openevo-backend --version" in command:
            return RemoteCommandResult(
                command=command,
                return_code=0,
                stdout=f"openevo {sidecar_api.OPENEVO_VERSION}\n",
            )
        if "openevo-backend --help" in command:
            return RemoteCommandResult(command=command, return_code=0, stdout="help")
        return RemoteCommandResult(command=command, return_code=0, stdout="ok")

    def upload_dir(self, local_path: str, remote_path: str) -> None:
        self.uploads.append((local_path, remote_path))
        self.upload_contents.append(sorted(path.name for path in Path(local_path).iterdir()))
        return None


class _WheelUploadFailingTransport(_ApiDryRunTransport):
    def upload_dir(self, local_path: str, remote_path: str) -> None:
        self.uploads.append((local_path, remote_path))
        raise RuntimeError("upload failed via http://proxy-user:proxy-secret@127.0.0.1:7890")


class _ApiLifecycleTransport(_ApiDryRunTransport):
    def __init__(
        self,
        *,
        pid_states: dict[str, dict[str, object]] | None = None,
        health_failures: dict[str, str] | None = None,
        log_content: str = "",
        log_exception: Exception | None = None,
    ) -> None:
        super().__init__()
        self.pid_states = pid_states or {}
        self.health_failures = health_failures or {}
        self.log_content = log_content
        self.log_exception = log_exception
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
        if "importlib.metadata" in command and "version('openevo')" in command:
            return super().run(
                command,
                cwd=cwd,
                env=env,
                timeout_seconds=timeout_seconds,
            )
        if "openevo-backend --version" in command or "openevo-backend --help" in command:
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
            if self.log_exception is not None:
                raise self.log_exception
            return RemoteCommandResult(
                command=command,
                return_code=0,
                stdout=self.log_content,
            )
        if "service_id =" in command and "os.kill(pid, signal.SIGTERM)" in command:
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
        if command.startswith('PATH="$HOME/.local/bin:$PATH" openevo-backend run '):
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


class _FailingServicesTransport(_ApiDryRunTransport):
    def run(
        self,
        command: str,
        *,
        cwd: str | None = None,
        env: dict[str, str] | None = None,
        timeout_seconds: float = 30.0,
    ) -> RemoteCommandResult:
        if "python3 -m openevo.rollout.server" in command:
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
        if command.startswith('PATH="$HOME/.local/bin:$PATH" openevo-backend run '):
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
        if "python3 -m openevo.rollout.server" in command and not self.service_started.is_set():
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
        if "python3 -m openevo.rollout.server" in command:
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
    assert status.project.evolution_targets == project.evolution.targets
    assert status.execution.mode == "codex_subscription_transcript"
    assert status.execution.model == "gpt-5.5"
    assert status.execution.token_metrics_available is False
    assert status.bootstrap.ready is False
    assert status.bootstrap.workspace_root == "/home/alice/.openevo/workspaces"
    assert status.bootstrap.readiness_notes == ("Remote bootstrap has not run yet.",)
    assert status.services[1].state == "ready"
    assert status.services[-1].state == "planned"
    assert [step.id for step in status.evolution] == [
        "transcript",
        "agent-system",
        "skill-bundle",
        "text-memory",
    ]


def test_build_desktop_shell_status_uses_enabled_generic_targets() -> None:
    project = ScienceProjectConfig.model_validate(
        _science_project_payload()
        | {
            "evolution": {
                "targets": {
                    "text_memory": _evolution_targets_payload()["text_memory"],
                    "quality_notes_external": {
                        "enabled": True,
                        "method": "synthesize_notes",
                        "config": {"style": "concise"},
                    },
                    "agent_system": _evolution_targets_payload()["agent_system"],
                }
            }
        }
    )

    status = build_desktop_shell_status(project, _remote_profile())

    assert [step.id for step in status.evolution] == [
        "transcript",
        "agent-system",
        "quality-notes-external",
        "text-memory",
    ]
    assert (
        next(step.label for step in status.evolution if step.id == "quality-notes-external")
        == "Quality notes external"
    )


def test_build_desktop_shell_status_preserves_complete_evolution_target_map() -> None:
    targets = {
        "text_memory": {
            "enabled": True,
            "method": "custom_memory_method",
            "config": {"threshold": 0.75, "nested": {"mode": "strict"}},
        },
        "skill_bundle": {
            "enabled": False,
            "method": None,
            "config": {"draft_prompt": "retain me"},
        },
        "future_target": {
            "enabled": False,
            "method": "future_method",
            "config": {"opaque": [1, 2, 3]},
        },
    }
    project = ScienceProjectConfig.model_validate(
        _science_project_payload() | {"evolution": {"targets": targets}}
    )

    payload = build_desktop_shell_status(project, _remote_profile()).model_dump(mode="json")

    assert payload["project"]["evolution_targets"] == targets


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
