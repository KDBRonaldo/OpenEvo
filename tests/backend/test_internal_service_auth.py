from __future__ import annotations

import asyncio
import hashlib
import json
import os
from pathlib import Path
from types import SimpleNamespace

from fastapi.testclient import TestClient
import httpx
from fastapi import FastAPI

from openevo.evolution.server import create_app
from openevo.gateway import server as gateway_server
from openevo.gateway.session_files import HeldCodexCredentialAuthority
from openevo.internal_auth import (
    GenerationBoundRunAdmissionCheck,
    CoreRunAdmissionHttpVerifier,
    INTERNAL_CREDENTIAL_FD_ENV,
    InternalServiceIdentity,
    RunAdmissionOperation,
    read_internal_service_identity,
)
from openevo.backend.run_admission import install_core_run_admission_endpoint
from openevo.rollout.models import SessionDispatchRequest
from openevo.rollout import server as rollout_server


GENERATION = "a" * 64
REGISTRY = "b" * 64
FRAMEWORK_LOCK = "c" * 64
CREDENTIAL = "release-internal-credential-value-0123456789abcdef"


def _identity(service_id: str = "evolution-backend") -> InternalServiceIdentity:
    return InternalServiceIdentity(
        service_id=service_id,
        generation_digest=GENERATION,
        registry_digest=REGISTRY,
        framework_lock_digest=FRAMEWORK_LOCK,
        credential=CREDENTIAL,
    )


def _credential_authority(tmp_path: Path) -> HeldCodexCredentialAuthority:
    auth = tmp_path / ".codex" / "auth.json"
    auth.parent.mkdir(mode=0o700)
    auth.write_text('{"tokens":{"access_token":"test-secret"}}', encoding="utf-8")
    auth.chmod(0o600)
    return HeldCodexCredentialAuthority.open(auth)


def test_internal_identity_is_consumed_from_fd_and_never_repr_visible(monkeypatch) -> None:
    expected = _identity()
    read_fd, write_fd = os.pipe()
    try:
        os.write(write_fd, expected.inherited_payload())
    finally:
        os.close(write_fd)
    monkeypatch.setenv(INTERNAL_CREDENTIAL_FD_ENV, str(read_fd))

    actual = read_internal_service_identity(
        required=True,
        expected_service_id="evolution-backend",
        actual_registry_digest=REGISTRY,
    )

    assert actual == expected
    assert CREDENTIAL not in repr(actual)
    assert INTERNAL_CREDENTIAL_FD_ENV not in os.environ


def test_http_run_admission_verifier_sends_only_bound_check_and_internal_auth() -> None:
    received: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        received["headers"] = dict(request.headers)
        received["payload"] = json.loads(request.content)
        return httpx.Response(204)

    identity = _identity("rollout")
    verifier = CoreRunAdmissionHttpVerifier(
        identity,
        "http://127.0.0.1:19000/internal/v1/run-admissions/verify",
        transport=httpx.MockTransport(handler),
    )
    check = GenerationBoundRunAdmissionCheck(
        operation=RunAdmissionOperation.ROLLOUT_TASK_SUBMIT,
        generation_digest=GENERATION,
        registry_digest=REGISTRY,
        framework_lock_digest=FRAMEWORK_LOCK,
        payload_sha256="d" * 64,
        task_id="release-task",
        session_id=None,
    )

    asyncio.run(verifier.verify(check))

    assert received["payload"] == {
        "framework_lock_digest": FRAMEWORK_LOCK,
        "generation_digest": GENERATION,
        "operation": "rollout_task_submit",
        "payload_sha256": "d" * 64,
        "registry_digest": REGISTRY,
        "session_id": None,
        "task_id": "release-task",
    }
    headers = received["headers"]
    assert isinstance(headers, dict)
    assert headers["authorization"] == f"Bearer {CREDENTIAL}"
    assert headers["x-openevo-internal-service"] == "rollout"


def test_core_run_admission_endpoint_is_private_bounded_and_generation_authenticated() -> None:
    class ServiceControl:
        def authenticates_run_service(self, headers) -> bool:
            return _identity("core-control").authenticates(
                {str(key).lower(): str(value) for key, value in headers.items()}
            )

    class Authority:
        def __init__(self) -> None:
            self.checks: list[GenerationBoundRunAdmissionCheck] = []

        async def verify(self, check: GenerationBoundRunAdmissionCheck) -> None:
            self.checks.append(check)

    authority = Authority()
    app = FastAPI()
    install_core_run_admission_endpoint(app, ServiceControl(), authority)
    client = TestClient(app)
    payload = {
        "framework_lock_digest": FRAMEWORK_LOCK,
        "generation_digest": GENERATION,
        "operation": "rollout_task_submit",
        "payload_sha256": "d" * 64,
        "registry_digest": REGISTRY,
        "session_id": None,
        "task_id": "release-task",
    }

    assert client.post("/internal/v1/run-admissions/verify", json=payload).status_code == 401
    response = client.post(
        "/internal/v1/run-admissions/verify",
        headers=_identity("rollout").request_headers(),
        json=payload,
    )

    assert response.status_code == 204
    assert authority.checks == [
        GenerationBoundRunAdmissionCheck(
            operation=RunAdmissionOperation.ROLLOUT_TASK_SUBMIT,
            generation_digest=GENERATION,
            registry_digest=REGISTRY,
            framework_lock_digest=FRAMEWORK_LOCK,
            payload_sha256="d" * 64,
            task_id="release-task",
            session_id=None,
        )
    ]
    assert all(
        route.path != "/internal/v1/run-admissions/verify"
        for route in app.routes
        if getattr(route, "include_in_schema", False)
    )
    oversized = b"{" + b" " * 5000 + b"}"
    assert (
        client.post(
            "/internal/v1/run-admissions/verify",
            headers=_identity("rollout").request_headers(),
            content=oversized,
        ).status_code
        == 413
    )


def test_evolution_internal_surface_fails_closed_and_registers_exact_worker(tmp_path) -> None:
    identity = _identity()
    app = create_app(
        db_path=tmp_path / "evolution.db",
        artifact_root=tmp_path / "artifacts",
        internal_identity=identity,
    )
    with TestClient(app) as client:
        assert client.get("/v1/health").status_code == 401
        assert client.get("/v1/internal/jobs/missing-job").status_code == 401
        wrong = identity.request_headers()
        wrong["Authorization"] = "Bearer wrong-credential-value-that-is-long-enough"
        assert client.get("/v1/health", headers=wrong).status_code == 401

        health = client.get("/v1/health", headers=identity.request_headers())
        assert health.status_code == 200
        assert health.json()["internal_identity"] == identity.health_identity()
        assert "artifact_root" not in health.json()
        assert (
            client.get(
                "/v1/internal/jobs/missing-job",
                headers=identity.request_headers(),
            ).status_code
            == 404
        )

        registration = {
            "framework_lock_digest": FRAMEWORK_LOCK,
            "generation_digest": GENERATION,
            "registry_digest": REGISTRY,
            "worker_id": "core-reference-worker",
        }
        response = client.post(
            "/v1/internal/workers/register",
            headers=_identity("evolution-worker").request_headers(),
            json=registration,
        )
        assert response.status_code == 200
        assert response.json() == registration
        workers = client.get("/v1/health", headers=identity.request_headers()).json()[
            "workers"
        ]
        assert workers == [registration]


def test_inherited_payload_is_closed_and_canonical() -> None:
    payload = json.loads(_identity().inherited_payload())
    assert set(payload) == {
        "credential",
        "framework_lock_digest",
        "generation_digest",
        "registry_digest",
        "service_id",
    }


def test_rollout_health_requires_auth_and_proves_gateway_schedulability(
    tmp_path: Path,
) -> None:
    topology_path = tmp_path / "topology.json"
    topology_path.write_text(
        json.dumps(
            {
                "gateway": {
                    "heartbeat_interval_seconds": 30,
                    "nodes": [
                        {
                            "host": "127.0.0.1",
                            "id": "core-gateway",
                            "model_served": "subscription-model",
                            "port": 18101,
                            "public_url": "http://127.0.0.1:18101",
                        }
                    ],
                    "rollout_server_url": "http://127.0.0.1:18100",
                },
                "rollout": {
                    "host": "127.0.0.1",
                    "port": 18100,
                    "public_url": "http://127.0.0.1:18100",
                },
            }
        ),
        encoding="utf-8",
    )
    rollout_identity = _identity("rollout")
    gateway_identity = _identity("gateway")
    rollout_server.configure_server(
        str(topology_path),
        internal_identity=rollout_identity,
    )
    try:
        with TestClient(rollout_server.app) as client:
            assert client.get("/health").status_code == 401
            before = client.get(
                "/health",
                headers=rollout_identity.request_headers(),
            ).json()
            assert before["gateway_registration"]["schedulable"] is False

            registration = client.post(
                "/nodes/register",
                headers=gateway_identity.request_headers(),
                json={
                    "gateway_url": "http://127.0.0.1:18101",
                    "heartbeat_interval_seconds": 30,
                    "max_init_workers": 2,
                    "max_postrun_workers": 2,
                    "max_run_workers": 1,
                    "node_id": "core-gateway",
                },
            )
            assert registration.status_code == 200
            health = client.get(
                "/health",
                headers=rollout_identity.request_headers(),
            )
            assert health.status_code == 200
            assert health.json()["gateway_registration"] == {
                "gateway_url": "http://127.0.0.1:18101",
                "node_id": "core-gateway",
                "registered": True,
                "schedulable": True,
            }
            assert health.json()["internal_identity"] == rollout_identity.health_identity()
    finally:
        rollout_server.configure_server(str(topology_path), internal_identity=None)


def test_release_rollout_submit_requires_generation_bound_run_admission(
    tmp_path: Path,
) -> None:
    topology_path = tmp_path / "topology.json"
    topology_path.write_text(
        json.dumps(
            {
                "gateway": {
                    "nodes": [
                        {
                            "host": "127.0.0.1",
                            "id": "core-gateway",
                            "model_served": "subscription-model",
                            "port": 18101,
                            "public_url": "http://127.0.0.1:18101",
                        }
                    ],
                    "rollout_server_url": "http://127.0.0.1:18100",
                },
                "rollout": {
                    "host": "127.0.0.1",
                    "port": 18100,
                    "public_url": "http://127.0.0.1:18100",
                },
            }
        ),
        encoding="utf-8",
    )
    identity = _identity("rollout")
    rollout_server.configure_server(str(topology_path), internal_identity=identity)
    try:
        with TestClient(rollout_server.app) as client:
            response = client.post(
                "/rollout/task/submit",
                headers=identity.request_headers(),
                json={
                    "task_id": "release-task",
                    "instruction": "raw caller instruction must not grant authority",
                    "agent": {
                        "harness": "codex",
                        "settings": {"capture_mode": "transcript"},
                    },
                    "runtime": {"image": "caller-supplied-image"},
                    "run_ready": True,
                    "admission": {"status": "admitted"},
                },
            )
        assert response.status_code == 503
        assert response.json() == {
            "error": {
                "code": "run_admission_authority_unavailable",
                "message": (
                    "Core run admission authority is unavailable for this service generation."
                ),
                "retryable": True,
            }
        }
    finally:
        rollout_server.configure_server(str(topology_path), internal_identity=None)


def test_release_gateway_session_create_and_dispatch_require_run_admission(
    monkeypatch,
    tmp_path: Path,
) -> None:
    class DispatchProbe:
        called = False

        async def dispatch(self, _request) -> None:
            self.called = True

    dispatch = DispatchProbe()
    monkeypatch.setattr(
        gateway_server,
        "get_state",
        lambda: SimpleNamespace(
            node=SimpleNamespace(id="core-gateway"),
            node_manager=dispatch,
        ),
    )
    identity = _identity("gateway")
    gateway_server._internal_identity = identity
    gateway_server._run_admission_verifier = None
    authority = _credential_authority(tmp_path)
    gateway_server._credential_authority = authority
    try:
        client = TestClient(gateway_server.app)
        create_response = client.post(
            "/sessions",
            headers=identity.request_headers(),
            json={"session_id": "release-session", "task_id": "release-task"},
        )
        response = client.post(
            "/sessions",
            headers=identity.request_headers(),
            json={
                "session_id": "release-session",
                "task_id": "release-task",
                "instruction": "raw caller instruction must not grant authority",
                "remaining_timeout_seconds": 30,
                "agent": {
                    "harness": "codex",
                    "settings": {"capture_mode": "transcript"},
                },
                "runtime": {"image": "caller-supplied-image"},
                "run_ready": True,
                "admission": {"status": "admitted"},
            },
        )
        assert create_response.status_code == 503
        assert response.status_code == 503
        assert response.json() == {
            "error": {
                "code": "run_admission_authority_unavailable",
                "message": (
                    "Core run admission authority is unavailable for this service generation."
                ),
                "retryable": True,
            }
        }
        assert dispatch.called is False
    finally:
        authority.close()
        gateway_server._internal_identity = None
        gateway_server._run_admission_verifier = None
        gateway_server._credential_authority = None


def test_release_gateway_verifier_receives_only_generation_bound_digest(
    monkeypatch,
    tmp_path: Path,
) -> None:
    class DispatchProbe:
        called = False

        async def dispatch(self, _request) -> None:
            self.called = True

    class AdmissionVerifier:
        def __init__(self) -> None:
            self.checks: list[GenerationBoundRunAdmissionCheck] = []

        async def verify(self, check: GenerationBoundRunAdmissionCheck) -> None:
            self.checks.append(check)

    dispatch = DispatchProbe()
    verifier = AdmissionVerifier()
    monkeypatch.setattr(
        gateway_server,
        "get_state",
        lambda: SimpleNamespace(
            node=SimpleNamespace(id="core-gateway"),
            node_manager=dispatch,
        ),
    )
    identity = _identity("gateway")
    gateway_server._internal_identity = identity
    gateway_server._run_admission_verifier = verifier
    authority = _credential_authority(tmp_path)
    gateway_server._credential_authority = authority
    body = {
        "session_id": "release-session",
        "task_id": "release-task",
        "instruction": "authority binds this payload without receiving its raw values",
        "remaining_timeout_seconds": 30,
        "agent": {
            "harness": "codex",
            "settings": {"capture_mode": "transcript"},
        },
        "runtime": {"image": "caller-supplied-image"},
        "run_ready": True,
        "admission": {"status": "admitted"},
    }
    try:
        response = TestClient(gateway_server.app).post(
            "/sessions",
            headers=identity.request_headers(),
            json=body,
        )
        assert response.status_code == 200
        assert dispatch.called is True
        assert len(verifier.checks) == 1
        check = verifier.checks[0]
        canonical = json.dumps(
            SessionDispatchRequest.model_validate(body).model_dump(mode="json"),
            ensure_ascii=True,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
        assert check == GenerationBoundRunAdmissionCheck(
            operation=RunAdmissionOperation.GATEWAY_SESSION_DISPATCH,
            generation_digest=GENERATION,
            registry_digest=REGISTRY,
            framework_lock_digest=FRAMEWORK_LOCK,
            payload_sha256=hashlib.sha256(canonical).hexdigest(),
            task_id="release-task",
            session_id="release-session",
        )
        assert not hasattr(check, "instruction")
        assert not hasattr(check, "runtime")
        assert not hasattr(check, "credential")
    finally:
        authority.close()
        gateway_server._internal_identity = None
        gateway_server._run_admission_verifier = None
        gateway_server._credential_authority = None


def test_gateway_auth_replacement_fails_before_admission_or_session_side_effect(
    monkeypatch,
    tmp_path: Path,
) -> None:
    class DispatchProbe:
        called = False

        async def dispatch(self, _request) -> None:
            self.called = True

    class AdmissionVerifier:
        def __init__(self) -> None:
            self.checks: list[GenerationBoundRunAdmissionCheck] = []

        async def verify(self, check: GenerationBoundRunAdmissionCheck) -> None:
            self.checks.append(check)

    dispatch = DispatchProbe()
    verifier = AdmissionVerifier()
    monkeypatch.setattr(
        gateway_server,
        "get_state",
        lambda: SimpleNamespace(
            node=SimpleNamespace(id="core-gateway"),
            node_manager=dispatch,
        ),
    )
    identity = _identity("gateway")
    authority = _credential_authority(tmp_path)
    replacement = tmp_path / ".codex" / "auth.replacement"
    replacement.write_text(
        '{"tokens":{"access_token":"replacement-secret"}}',
        encoding="utf-8",
    )
    replacement.chmod(0o600)
    os.replace(replacement, tmp_path / ".codex" / "auth.json")
    gateway_server._internal_identity = identity
    gateway_server._run_admission_verifier = verifier
    gateway_server._credential_authority = authority
    try:
        response = TestClient(gateway_server.app).post(
            "/sessions",
            headers=identity.request_headers(),
            json={
                "session_id": "replaced-credential-session",
                "task_id": "replaced-credential-task",
                "instruction": "must not be admitted",
                "remaining_timeout_seconds": 30,
                "agent": {
                    "harness": "codex",
                    "settings": {
                        "auth_mode": "subscription",
                        "capture_mode": "transcript",
                    },
                },
                "runtime": {"image": "caller-supplied-image"},
            },
        )

        assert response.status_code == 503
        assert response.json() == {
            "error": {
                "code": "credential_authority_changed",
                "message": (
                    "The managed subscription credential authority changed after readiness."
                ),
                "retryable": True,
            }
        }
        assert verifier.checks == []
        assert dispatch.called is False
    finally:
        authority.close()
        gateway_server._internal_identity = None
        gateway_server._run_admission_verifier = None
        gateway_server._credential_authority = None


def test_gateway_internal_management_routes_fail_closed_without_auth() -> None:
    gateway_server._internal_identity = _identity("gateway")
    try:
        client = TestClient(gateway_server.app)
        assert client.get("/health").status_code == 401
        assert client.get("/sessions").status_code == 401
        assert client.get("/admin/inference/status").status_code == 401
        assert client.get("/events").status_code == 401
    finally:
        gateway_server._internal_identity = None
