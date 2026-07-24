from __future__ import annotations

from datetime import datetime, timedelta, timezone
import hashlib
import hmac
from pathlib import Path
import sqlite3
from threading import Event, Lock, Thread
import time
from typing import Any, cast
from unittest.mock import Mock

from fastapi.testclient import TestClient
import pytest

from desktop.sidecar.contracts.v1 import (
    DESKTOP_OPENAPI_SHA256,
    canonical_sha256,
    contract_app,
    create_contract_app,
)
from desktop.sidecar.contracts.v1 import models as local_v1
from desktop.sidecar.core_bridge_v1 import DesktopCoreBridgeV1
import desktop.sidecar.provider_store as provider_store_module
from desktop.sidecar.provider_store import (
    DesktopProviderStore,
    ProviderDataCorruptionError,
    ProviderMutation,
)
import desktop.sidecar.release_app as release_app_module
from desktop.sidecar.release_capabilities import (
    ReleaseAuthorityNegotiationError,
    V019_RELEASE_AUTHORITY_POLICY,
    negotiate_core_v2_mutation,
    negotiate_desktop_v2_mutation,
    negotiate_v019_mutation_authority,
    validate_v019_release_composition,
)
from desktop.sidecar.release_app import create_release_desktop_local_api_app
from desktop.sidecar.release_runtime import CoreRuntimeSessionBinding
from desktop.sidecar.remote_lifecycle import (
    DesktopRemoteLifecycle,
    RemoteConnectionFailedError,
    RemoteConnectionResult,
    RemoteLifecycleSnapshot,
    RemoteLifecycleSupersededError,
)
from openevo.backend.contracts.v2.snapshots import (
    events_schema_sha256 as core_events_schema_sha256,
    openapi_sha256 as core_openapi_sha256,
)
from openevo.deployment.host_keys import HostKeyCandidate


SESSION_TOKEN = "desktop-session-token-0000000000000001"
SESSION_HEADERS = {"X-OpenEvo-Desktop-Session": SESSION_TOKEN}
INSTANCE_ID = "1" * 32
READINESS_KEY = b"r" * 32
SOURCE_COMMIT = "89baeb26"


class MutableClock:
    def __init__(self) -> None:
        self.now = datetime(2026, 7, 14, 12, 0, tzinfo=timezone.utc)

    def __call__(self) -> datetime:
        return self.now


class FakeRemoteLifecycle(DesktopRemoteLifecycle):
    def __init__(self) -> None:
        self.candidate = HostKeyCandidate(
            algorithm="ssh-ed25519",
            public_key="AAAAC3NzaC1lZDI1NTE5AAAAITest",
            fingerprint="SHA256:AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=",
        )
        self.current = RemoteLifecycleSnapshot(None, "disconnected")
        self.connect_calls = 0
        self.accept_calls = 0
        self.disconnect_calls = 0
        self.closed = False
        self.connect_error: RemoteConnectionFailedError | None = None

    def snapshot(self) -> RemoteLifecycleSnapshot:
        return self.current

    def connect(self, profile) -> RemoteConnectionResult:
        self.connect_calls += 1
        if self.connect_error is not None:
            self.current = RemoteLifecycleSnapshot(
                profile.profile_id, "failed", failure_code="ssh_connection_failed"
            )
            raise self.connect_error
        self.current = RemoteLifecycleSnapshot(
            profile.profile_id,
            "host_key_required",
            host_key_candidate=self.candidate,
        )
        return RemoteConnectionResult(
            profile.profile_id,
            "host_key_required",
            host_key_candidate=self.candidate,
        )

    def accept_host_key(self, profile, request) -> RemoteConnectionResult:
        self.accept_calls += 1
        assert request.algorithm == self.candidate.algorithm
        assert request.fingerprint == self.candidate.fingerprint
        self.current = RemoteLifecycleSnapshot(profile.profile_id, "connected")
        return RemoteConnectionResult(profile.profile_id, "connected")

    def disconnect(self, profile_id: str | None = None) -> None:
        self.disconnect_calls += 1
        assert profile_id is None or self.current.profile_id in {None, profile_id}
        self.current = RemoteLifecycleSnapshot(None, "disconnected")

    def close(self) -> None:
        self.closed = True
        self.current = RemoteLifecycleSnapshot(None, "disconnected")


class RacingRemoteLifecycle(FakeRemoteLifecycle):
    def __init__(self) -> None:
        super().__init__()
        self.first_started = Event()
        self.release_first = Event()
        self.second_finished = Event()
        self._lock = Lock()

    def connect(self, profile) -> RemoteConnectionResult:
        if profile.name == "A":
            self.first_started.set()
            assert self.release_first.wait(5)
            with self._lock:
                self.current = RemoteLifecycleSnapshot(
                    profile.profile_id, "failed", failure_code="ssh_connection_failed"
                )
            raise RemoteConnectionFailedError("late A failure")
        with self._lock:
            self.current = RemoteLifecycleSnapshot(profile.profile_id, "connected")
        self.second_finished.set()
        return RemoteConnectionResult(profile.profile_id, "connected")


class BlockingRemoteLifecycle(FakeRemoteLifecycle):
    def __init__(self) -> None:
        super().__init__()
        self.started = Event()
        self.release = Event()

    def connect(self, profile) -> RemoteConnectionResult:
        self.connect_calls += 1
        self.started.set()
        assert self.release.wait(5)
        self.current = RemoteLifecycleSnapshot(profile.profile_id, "connected")
        return RemoteConnectionResult(profile.profile_id, "connected")


class CancellableBlockingRemoteLifecycle(BlockingRemoteLifecycle):
    def __init__(self) -> None:
        super().__init__()
        self.cancelled = Event()

    def connect(self, profile) -> RemoteConnectionResult:
        self.connect_calls += 1
        self.current = RemoteLifecycleSnapshot(profile.profile_id, "connecting")
        self.started.set()
        assert self.release.wait(5)
        if self.cancelled.is_set():
            raise RemoteLifecycleSupersededError("The profile connection was cancelled.")
        self.current = RemoteLifecycleSnapshot(profile.profile_id, "connected")
        return RemoteConnectionResult(profile.profile_id, "connected")

    def disconnect(self, profile_id: str | None = None) -> None:
        self.disconnect_calls += 1
        assert profile_id is None or self.current.profile_id in {None, profile_id}
        self.cancelled.set()
        self.current = RemoteLifecycleSnapshot(None, "disconnected")
        self.release.set()


class ObservedActionLock:
    def __init__(self) -> None:
        self._lock = Lock()
        self._metadata_lock = Lock()
        self._attempts = 0
        self.second_waiting = Event()

    def __enter__(self) -> ObservedActionLock:
        with self._metadata_lock:
            self._attempts += 1
            if self._attempts == 2:
                self.second_waiting.set()
        self._lock.acquire()
        return self

    def __exit__(self, *_args: object) -> None:
        self._lock.release()


def _app(
    state_root: Path,
    *,
    clock: MutableClock | None = None,
    remote_lifecycle: FakeRemoteLifecycle | None = None,
    core_bridge: DesktopCoreBridgeV1 | None = None,
):
    return create_release_desktop_local_api_app(
        state_root=state_root,
        session_token=SESSION_TOKEN,
        instance_id=INSTANCE_ID,
        readiness_key=READINESS_KEY,
        source_commit=SOURCE_COMMIT,
        build_version="0.1.8",
        build_channel="test",
        clock=clock,
        remote_lifecycle=cast(DesktopRemoteLifecycle | None, remote_lifecycle),
        core_bridge=core_bridge,
    )


def _profile(name: str = "Research server") -> dict[str, object]:
    return {
        "name": name,
        "host": "compute.example.org",
        "port": 2222,
        "user": "researcher",
    }


def _project(profile_id: str, *, name: str = "Protein design") -> dict[str, object]:
    return {
        "name": name,
        "profile_id": profile_id,
        "task": {"title": "Design", "objective": "Improve held-out stability."},
        "source": {"kind": "scratch", "display_name": "New project"},
        "execution": {
            "mode": "codex_subscription_transcript",
            "codex_model": "gpt-5.3-codex-spark",
        },
        "evolution": {"targets": {}},
    }


def _canonical_feature_digest(features: list[str]) -> str:
    import json

    return hashlib.sha256(
        json.dumps(features, ensure_ascii=True, separators=(",", ":")).encode()
    ).hexdigest()


def _v019_desktop_discovery() -> dict[str, object]:
    policy = V019_RELEASE_AUTHORITY_POLICY
    features = list(policy.required_desktop_feature_flags)
    return {
        "schema_version": "2",
        "api_name": "openevo-desktop-local-api",
        "preferred_major": 2,
        "supported_majors": [2],
        "mutation_major": 2,
        "openapi_sha256": policy.desktop_openapi_sha256,
        "event_schema_sha256": policy.desktop_event_schema_sha256,
        "release_version": "0.1.9",
        "build_id": "a" * 64,
        "source_commit": SOURCE_COMMIT,
        "build_channel": "release",
        "provider_kind": "desktop_sidecar",
        "feature_flags": features,
        "feature_set_sha256": _canonical_feature_digest(features),
        "required_core_api_major": 2,
        "mutation_compatible": True,
    }


def _v019_core_discovery() -> dict[str, object]:
    policy = V019_RELEASE_AUTHORITY_POLICY
    features = list(policy.required_core_feature_flags)
    return {
        "schema_version": "2",
        "api_name": "openevo-core-control-api",
        "preferred_major": 2,
        "supported_majors": [1, 2],
        "mutation_major": 2,
        "contracts": [
            {
                "schema_version": "2",
                "api_major": 1,
                "openapi_sha256": "1" * 64,
                "event_schema_sha256": "2" * 64,
                "access": "read_only_migration",
                "mutation_compatible": False,
            },
            {
                "schema_version": "2",
                "api_major": 2,
                "openapi_sha256": policy.core_openapi_sha256,
                "event_schema_sha256": policy.core_event_schema_sha256,
                "access": "mutation",
                "mutation_compatible": True,
            },
        ],
        "release_version": "0.1.9",
        "build_id": "b" * 64,
        "source_commit": SOURCE_COMMIT,
        "build_channel": "release",
        "provider_kind": "openevo_daemon",
        "feature_flags": features,
        "feature_set_sha256": _canonical_feature_digest(features),
        "registry_sha256": "c" * 64,
        "runtime_contract_sha256": "d" * 64,
        "mutation_compatible": True,
    }


def test_v019_release_policy_pins_exact_local_and_core_v2_authority() -> None:
    policy = V019_RELEASE_AUTHORITY_POLICY
    assert policy.release_version == "0.1.9"
    assert policy.desktop_mutation_api_major == 2
    assert policy.core_mutation_api_major == 2
    assert policy.core_openapi_sha256 == core_openapi_sha256()
    assert policy.core_event_schema_sha256 == core_events_schema_sha256()
    assert policy.allow_direct_core_url is False
    assert policy.allow_legacy_route_fallback is False

    assert negotiate_desktop_v2_mutation(_v019_desktop_discovery()).mutation_compatible
    assert negotiate_core_v2_mutation(_v019_core_discovery()).registry_sha256 == "c" * 64
    authority = negotiate_v019_mutation_authority(
        _v019_desktop_discovery(), _v019_core_discovery()
    )
    assert authority.desktop_build_id == "a" * 64
    assert authority.core_build_id == "b" * 64
    assert authority.source_commit == SOURCE_COMMIT

    mismatched_core = {**_v019_core_discovery(), "source_commit": "abcdef1"}
    with pytest.raises(ReleaseAuthorityNegotiationError, match="source identities"):
        negotiate_v019_mutation_authority(_v019_desktop_discovery(), mismatched_core)


@pytest.mark.parametrize(
    ("override", "message"),
    [
        ({"provider_kind": "contract_simulator"}, "Desktop v2 discovery"),
        ({"openapi_sha256": "f" * 64}, "Desktop OpenAPI"),
        ({"event_schema_sha256": "f" * 64}, "Desktop event schema"),
        ({"supported_majors": [1]}, "Desktop v2 discovery"),
        ({"mutation_compatible": False}, "not mutation-compatible"),
    ],
)
def test_v019_desktop_negotiation_rejects_nonrelease_authority(
    override: dict[str, object], message: str
) -> None:
    with pytest.raises(ReleaseAuthorityNegotiationError, match=message):
        negotiate_desktop_v2_mutation({**_v019_desktop_discovery(), **override})


def test_v019_core_negotiation_rejects_v1_missing_registry_and_digest_drift() -> None:
    with pytest.raises(ReleaseAuthorityNegotiationError, match="Core v2 discovery"):
        negotiate_core_v2_mutation(
            {
                "schema_version": "1",
                "preferred_major": 1,
                "supported_majors": [1],
            }
        )
    with pytest.raises(ReleaseAuthorityNegotiationError, match="registry"):
        negotiate_core_v2_mutation({**_v019_core_discovery(), "registry_sha256": None})
    payload = _v019_core_discovery()
    contracts = cast(list[dict[str, object]], payload["contracts"])
    contracts[1] = {**contracts[1], "openapi_sha256": "f" * 64}
    with pytest.raises(ReleaseAuthorityNegotiationError, match="Core OpenAPI"):
        negotiate_core_v2_mutation(payload)


@pytest.mark.parametrize(
    "override",
    [
        {"provider_kind": "scaffold"},
        {"provider_kind": "dry_run"},
        {"provider_kind": "direct_backend"},
        {"local_api_major": 1},
        {"core_transport": "direct_core_url"},
        {"allow_direct_core_url": True},
        {"allow_legacy_route_fallback": True},
    ],
)
def test_v019_release_composition_rejects_fallbacks(override: dict[str, object]) -> None:
    values: dict[str, object] = {
        "provider_kind": "desktop_sidecar",
        "local_api_major": 2,
        "core_transport": "active_project_ssh_tunnel",
        "allow_direct_core_url": False,
        "allow_legacy_route_fallback": False,
    }
    values.update(override)
    with pytest.raises(ReleaseAuthorityNegotiationError):
        validate_v019_release_composition(**values)


def test_v019_cannot_start_the_frozen_v1_release_provider(tmp_path: Path) -> None:
    state_root = tmp_path / "state"
    with pytest.raises(ReleaseAuthorityNegotiationError, match="Local API v2"):
        create_release_desktop_local_api_app(
            state_root=state_root,
            session_token=SESSION_TOKEN,
            instance_id=INSTANCE_ID,
            readiness_key=READINESS_KEY,
            source_commit=SOURCE_COMMIT,
            build_version="0.1.9",
            build_channel="release",
        )
    assert not state_root.exists()


def test_release_app_marks_state_store_before_provider_store_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    phases: list[str] = []
    canary = RuntimeError("Traceback /Users/private token=secret")

    def fail_provider_store(*_args: object, **_kwargs: object):
        raise canary

    monkeypatch.setattr(release_app_module, "DesktopProviderStore", fail_provider_store)

    with pytest.raises(RuntimeError) as exc_info:
        create_release_desktop_local_api_app(
            state_root=tmp_path / "state",
            session_token=SESSION_TOKEN,
            instance_id=INSTANCE_ID,
            readiness_key=READINESS_KEY,
            source_commit=SOURCE_COMMIT,
            build_version="0.1.8",
            build_channel="release",
            startup_phase=phases.append,
        )

    assert exc_info.value is canary
    assert phases == ["provider_store"]


def test_release_local_api_allows_only_packaged_tauri_cors_origins(tmp_path: Path) -> None:
    app = _app(tmp_path / "state")

    with TestClient(app) as client:
        discovery = client.get("/version", headers={"Origin": "tauri://localhost"})
        discovery_preflight = client.options(
            "/version",
            headers={
                "Origin": "tauri://localhost",
                "Access-Control-Request-Method": "GET",
                "Access-Control-Request-Headers": "Cache-Control, Pragma",
            },
        )
        preflight = client.options(
            "/desktop/v1/state",
            headers={
                "Origin": "http://tauri.localhost",
                "Access-Control-Request-Method": "PATCH",
                "Access-Control-Request-Headers": (
                    "Cache-Control, Content-Type, X-OpenEvo-Desktop-Session, "
                    "Idempotency-Key, If-Match, Last-Event-ID, Pragma"
                ),
            },
        )
        forbidden_method = client.options(
            "/desktop/v1/state",
            headers={
                "Origin": "tauri://localhost",
                "Access-Control-Request-Method": "PUT",
                "Access-Control-Request-Headers": "Content-Type",
            },
        )
        forbidden_header = client.options(
            "/desktop/v1/state",
            headers={
                "Origin": "tauri://localhost",
                "Access-Control-Request-Method": "PATCH",
                "Access-Control-Request-Headers": "X-Not-Allowed",
            },
        )
        hostile = client.get(
            "/version",
            headers={"Origin": "https://tauri.localhost.attacker.example"},
        )

    assert discovery.status_code == 200
    assert discovery.headers["access-control-allow-origin"] == "tauri://localhost"
    assert discovery_preflight.status_code == 200
    assert discovery_preflight.headers["access-control-allow-origin"] == ("tauri://localhost")
    assert preflight.status_code == 200
    assert preflight.headers["access-control-allow-origin"] == "http://tauri.localhost"
    assert {
        value.strip().lower()
        for value in preflight.headers["access-control-allow-headers"].split(",")
    } == {
        "accept",
        "accept-language",
        "cache-control",
        "content-language",
        "content-type",
        "idempotency-key",
        "if-match",
        "last-event-id",
        "pragma",
        "x-openevo-desktop-session",
        "x-openevo-resource-generation",
    }
    assert {
        value.strip() for value in preflight.headers["access-control-allow-methods"].split(",")
    } == {"GET", "POST", "PATCH", "DELETE"}
    assert forbidden_method.status_code == 400
    assert forbidden_header.status_code == 400
    assert "access-control-allow-origin" not in hostile.headers


def test_release_local_api_applies_cors_outside_unhandled_errors(tmp_path: Path) -> None:
    app = _app(tmp_path / "state")

    @app.get("/_test/unhandled", include_in_schema=False)
    def unhandled_error() -> None:
        raise RuntimeError("test canary")

    with TestClient(app, raise_server_exceptions=False) as client:
        response = client.get(
            "/_test/unhandled",
            headers={"Origin": "tauri://localhost"},
        )

    assert response.status_code == 500
    assert response.headers["access-control-allow-origin"] == "tauri://localhost"


def _create_profile(client: TestClient, *, name: str, key: str):
    return client.post(
        "/desktop/v1/profiles",
        headers={**SESSION_HEADERS, "Idempotency-Key": key},
        json=_profile(name),
    )


def test_profile_create_accepts_json_array_for_no_proxy(tmp_path: Path) -> None:
    app = _app(tmp_path / "state")
    request = {
        "name": "Exhibition GPU server",
        "host": "127.0.0.1",
        "port": 22472,
        "user": "root",
        "authentication_kind": "ssh_agent",
        "proxy": {
            "http_url": None,
            "https_url": None,
            "no_proxy": ["127.0.0.1", "localhost"],
        },
    }

    with TestClient(app) as client:
        created = client.post(
            "/desktop/v1/profiles",
            headers={
                **SESSION_HEADERS,
                "Idempotency-Key": "profile-create-json-array-0001",
            },
            json=request,
        )
        assert created.status_code == 201
        response = client.patch(
            f"/desktop/v1/profiles/{created.json()['profile_id']}",
            headers={**SESSION_HEADERS, "If-Match": created.json()["etag"]},
            json={"proxy": {**request["proxy"], "no_proxy": ["compute.internal"]}},
        )

    assert created.json()["proxy"]["no_proxy"] == ["127.0.0.1", "localhost"]
    assert response.status_code == 200
    assert response.json()["proxy"]["no_proxy"] == ["compute.internal"]


@pytest.mark.parametrize("no_proxy", ["localhost", {"host": "localhost"}, 1, True])
def test_profile_create_rejects_non_array_no_proxy(tmp_path: Path, no_proxy: object) -> None:
    app = _app(tmp_path / "state")
    request = _profile()
    request["proxy"] = {
        "http_url": None,
        "https_url": None,
        "no_proxy": no_proxy,
    }

    with TestClient(app) as client:
        response = client.post(
            "/desktop/v1/profiles",
            headers={
                **SESSION_HEADERS,
                "Idempotency-Key": "profile-create-invalid-array-0001",
            },
            json=request,
        )

    assert response.status_code == 422
    assert response.json()["code"] == "contract_validation_failed"


def test_project_create_rejects_an_unregistered_native_workspace_reference(
    tmp_path: Path,
) -> None:
    app = _app(tmp_path / "state")
    with TestClient(app) as client:
        profile = _create_profile(
            client,
            name="Research server",
            key="create-profile-native-0001",
        ).json()
        request = _project(profile["profile_id"])
        request["source"] = {
            "kind": "native_folder_snapshot",
            "display_name": "research",
            "import_ref": {
                "import_id": f"workspace-import-{'1a' * 24}",
                "content_sha256": "2b" * 32,
                "byte_size": 1024,
                "entry_count": 0,
                "extracted_byte_size": 0,
            },
        }

        response = client.post(
            "/desktop/v1/projects",
            headers={**SESSION_HEADERS, "Idempotency-Key": "create-project-native-0001"},
            json=request,
        )

        assert response.status_code == 422
        assert response.json()["code"] == "workspace_import_invalid"
        assert "workspace-import" not in response.text
        assert str(tmp_path) not in response.text


def test_release_execution_mode_gate_rejects_create_update_activate_and_run(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bridge = Mock(spec=DesktopCoreBridgeV1)
    app = _app(tmp_path / "state", core_bridge=bridge)
    with TestClient(app) as client:
        profile = _create_profile(
            client,
            name="Release mode server",
            key="create-profile-release-mode-0001",
        ).json()
        request = _project(profile["profile_id"])
        request["execution"] = {
            "mode": "self-deployed",
            "hf_model": "open-models/release-gated-model",
        }
        headers = {
            **SESSION_HEADERS,
            "Idempotency-Key": "create-project-release-mode-0001",
        }

        rejected_create = client.post("/desktop/v1/projects", headers=headers, json=request)
        replayed_create = client.post("/desktop/v1/projects", headers=headers, json=request)
        assert rejected_create.status_code == 409
        assert replayed_create.json()["code"] == "self_deployed_release_unavailable"
        assert replayed_create.json()["message"] == rejected_create.json()["message"]
        assert client.get("/desktop/v1/projects", headers=SESSION_HEADERS).json()["items"] == []

        supported_request = {
            **request,
            "name": "Supported release mode project",
            "execution": {
                "mode": "codex_subscription_transcript",
                "codex_model": "gpt-5.5",
            },
        }
        accepted_after_rejection = client.post(
            "/desktop/v1/projects",
            headers=headers,
            json=supported_request,
        )
        assert accepted_after_rejection.status_code == 201

        provider = app.state.desktop_release_provider
        project = provider._store.create_project(
            local_v1.ProjectCreateV1.model_validate(request),
            idempotency_key="seed-existing-release-mode-0001",
        )
        rejected_capabilities = client.get(
            f"/desktop/v1/projects/{project.project_id}/capabilities",
            headers=SESSION_HEADERS,
        )
        rejected_validation = client.post(
            f"/desktop/v1/projects/{project.project_id}/validate",
            headers={
                **SESSION_HEADERS,
                "If-Match": project.etag,
                "Idempotency-Key": "validate-project-release-mode-0001",
            },
        )
        for rejected_bridge_action in (rejected_capabilities, rejected_validation):
            assert rejected_bridge_action.status_code == 409
            assert rejected_bridge_action.json()["code"] == "self_deployed_release_unavailable"
        assert bridge.method_calls == []

        rejected_update = client.patch(
            f"/desktop/v1/projects/{project.project_id}",
            headers={**SESSION_HEADERS, "If-Match": project.etag},
            json={"task": {"title": "Design", "objective": "Do not persist this edit."}},
        )
        assert rejected_update.status_code == 409
        assert provider._store.get_project(project.project_id).etag == project.etag

        rejected_activation = client.post(
            f"/desktop/v1/projects/{project.project_id}/activate",
            headers={
                **SESSION_HEADERS,
                "If-Match": project.etag,
                "Idempotency-Key": "activate-project-release-mode-0001",
            },
        )
        assert rejected_activation.status_code == 409
        assert provider._store.pending_operation_ids() == ()

        monkeypatch.setattr(
            provider,
            "_active_project_for_runtime",
            lambda: CoreRuntimeSessionBinding(project=project, generation=1),
        )
        rejected_run = client.post(
            "/desktop/v1/runs",
            headers={
                **SESSION_HEADERS,
                "If-Match": project.etag,
                "Idempotency-Key": "create-run-release-mode-0001",
            },
            json={"project_id": project.project_id},
        )
        assert rejected_run.status_code == 409
        assert rejected_run.json()["category"] == "run"

        switched = client.patch(
            f"/desktop/v1/projects/{project.project_id}",
            headers={**SESSION_HEADERS, "If-Match": project.etag},
            json={
                "execution": {
                    "mode": "codex_subscription_transcript",
                    "codex_model": "gpt-5.5",
                }
            },
        )
        assert switched.status_code == 200
        assert switched.json()["execution"]["mode"] == "codex_subscription_transcript"

        bridge.activate_project.assert_not_called()
        bridge.create_run.assert_not_called()
        assert bridge.method_calls == []


def test_create_run_gates_active_self_deployed_mode_before_request_project_identity(
    tmp_path: Path,
) -> None:
    bridge = Mock(spec=DesktopCoreBridgeV1)
    lifecycle = FakeRemoteLifecycle()
    app = _app(
        tmp_path / "state",
        core_bridge=bridge,
        remote_lifecycle=lifecycle,
    )
    with TestClient(app) as client:
        profile = _create_profile(
            client,
            name="Run authority server",
            key="create-profile-run-authority-0001",
        ).json()
        provider = app.state.desktop_release_provider
        active_request = _project(profile["profile_id"], name="Active unavailable project")
        active_request["execution"] = {
            "mode": "self-deployed",
            "hf_model": "open-models/release-gated-model",
        }
        active_project = provider._store.create_project(
            local_v1.ProjectCreateV1.model_validate(active_request),
            idempotency_key="seed-active-unavailable-run-project-0001",
        )
        requested_project = provider._store.create_project(
            local_v1.ProjectCreateV1.model_validate(
                _project(profile["profile_id"], name="Requested subscription project")
            ),
            idempotency_key="seed-requested-subscription-run-project-0001",
        )
        provider._active_project_for_runtime = (  # type: ignore[method-assign]
            lambda: CoreRuntimeSessionBinding(project=active_project, generation=1)
        )
        headers = {
            **SESSION_HEADERS,
            "If-Match": requested_project.etag,
            "Idempotency-Key": "create-run-active-authority-0001",
        }

        first = client.post(
            "/desktop/v1/runs",
            headers=headers,
            json={"project_id": requested_project.project_id},
        )
        replay = client.post(
            "/desktop/v1/runs",
            headers=headers,
            json={"project_id": requested_project.project_id},
        )

        for response in (first, replay):
            assert response.status_code == 409
            assert response.json()["code"] == "self_deployed_release_unavailable"
            assert response.json()["category"] == "run"
        assert bridge.method_calls == []
        assert lifecycle.connect_calls == 0
        assert lifecycle.accept_calls == 0
        assert lifecycle.disconnect_calls == 0


def test_create_run_rejects_supported_non_active_project_before_core(
    tmp_path: Path,
) -> None:
    bridge = Mock(spec=DesktopCoreBridgeV1)
    lifecycle = FakeRemoteLifecycle()
    app = _app(
        tmp_path / "state",
        core_bridge=bridge,
        remote_lifecycle=lifecycle,
    )
    with TestClient(app) as client:
        profile = _create_profile(
            client,
            name="Run identity server",
            key="create-profile-run-identity-0001",
        ).json()
        provider = app.state.desktop_release_provider
        active_project = provider._store.create_project(
            local_v1.ProjectCreateV1.model_validate(
                _project(profile["profile_id"], name="Active subscription project")
            ),
            idempotency_key="seed-active-subscription-run-project-0001",
        )
        requested_project = provider._store.create_project(
            local_v1.ProjectCreateV1.model_validate(
                _project(profile["profile_id"], name="Different subscription project")
            ),
            idempotency_key="seed-different-subscription-run-project-0001",
        )
        provider._active_project_for_runtime = (  # type: ignore[method-assign]
            lambda: CoreRuntimeSessionBinding(project=active_project, generation=1)
        )

        response = client.post(
            "/desktop/v1/runs",
            headers={
                **SESSION_HEADERS,
                "If-Match": requested_project.etag,
                "Idempotency-Key": "create-run-project-mismatch-0001",
            },
            json={"project_id": requested_project.project_id},
        )

        assert response.status_code == 409
        assert response.json()["code"] == "active_project_mismatch"
        assert response.json()["category"] == "service"
        assert response.json()["repair_action"] == "unsupported"
        assert response.json()["next_action"] == "Reconnect and activate the saved project."
        assert bridge.method_calls == []
        assert lifecycle.connect_calls == 0
        assert lifecycle.accept_calls == 0
        assert lifecycle.disconnect_calls == 0


def test_activation_replay_rechecks_unavailable_release_mode_before_return(
    tmp_path: Path,
) -> None:
    bridge = Mock(spec=DesktopCoreBridgeV1)
    lifecycle = FakeRemoteLifecycle()
    app = _app(
        tmp_path / "state",
        core_bridge=bridge,
        remote_lifecycle=lifecycle,
    )
    with TestClient(app) as client:
        profile = _create_profile(
            client,
            name="Activation replay server",
            key="create-profile-activation-replay-0001",
        ).json()
        request = _project(profile["profile_id"], name="Unavailable replay project")
        request["execution"] = {
            "mode": "self-deployed",
            "hf_model": "open-models/release-gated-model",
        }
        provider = app.state.desktop_release_provider
        project = provider._store.create_project(
            local_v1.ProjectCreateV1.model_validate(request),
            idempotency_key="seed-unavailable-activation-replay-project-0001",
        )
        action = {
            "route": f"/desktop/v1/projects/{project.project_id}/activate",
            "operation_kind": "project_activate",
            "project_id": project.project_id,
            "key": "activation-unavailable-replay-0001",
            "body": {},
            "if_match": project.etag,
        }
        reservation = provider._store.begin_project_runtime_action(**action)

        response = client.post(
            action["route"],
            headers={
                **SESSION_HEADERS,
                "If-Match": project.etag,
                "Idempotency-Key": action["key"],
            },
        )
        conflicting_replay = client.post(
            action["route"],
            headers={
                **SESSION_HEADERS,
                "If-Match": '"' + "a" * 64 + '"',
                "Idempotency-Key": action["key"],
            },
        )

        for replay_response in (response, conflicting_replay):
            assert replay_response.status_code == 409
            assert replay_response.json()["code"] == "self_deployed_release_unavailable"
        assert (
            provider._store.get_local_operation(reservation.operation.operation_id)
            == reservation.operation
        )
        assert bridge.method_calls == []
        assert lifecycle.connect_calls == 0
        assert lifecycle.accept_calls == 0
        assert lifecycle.disconnect_calls == 0


def test_supported_subscription_activation_replay_remains_exact_and_local(
    tmp_path: Path,
) -> None:
    bridge = Mock(spec=DesktopCoreBridgeV1)
    lifecycle = FakeRemoteLifecycle()
    app = _app(
        tmp_path / "state",
        core_bridge=bridge,
        remote_lifecycle=lifecycle,
    )
    with TestClient(app) as client:
        profile = _create_profile(
            client,
            name="Supported activation replay server",
            key="create-profile-supported-activation-replay-0001",
        ).json()
        provider = app.state.desktop_release_provider
        project = provider._store.create_project(
            local_v1.ProjectCreateV1.model_validate(
                _project(profile["profile_id"], name="Supported replay project")
            ),
            idempotency_key="seed-supported-activation-replay-project-0001",
        )
        action = {
            "route": f"/desktop/v1/projects/{project.project_id}/activate",
            "operation_kind": "project_activate",
            "project_id": project.project_id,
            "key": "activation-supported-replay-0001",
            "body": {},
            "if_match": project.etag,
        }
        reservation = provider._store.begin_project_runtime_action(**action)

        response = client.post(
            action["route"],
            headers={
                **SESSION_HEADERS,
                "If-Match": project.etag,
                "Idempotency-Key": action["key"],
            },
        )
        conflicting_replay = client.post(
            action["route"],
            headers={
                **SESSION_HEADERS,
                "If-Match": '"' + "a" * 64 + '"',
                "Idempotency-Key": action["key"],
            },
        )

        assert response.status_code == 202
        assert response.json() == reservation.operation.model_dump(mode="json")
        assert response.headers["etag"] == reservation.operation.etag
        assert conflicting_replay.status_code == 409
        assert conflicting_replay.json()["code"] == "idempotency_key_reused"
        assert bridge.method_calls == []
        assert lifecycle.connect_calls == 0
        assert lifecycle.accept_calls == 0
        assert lifecycle.disconnect_calls == 0


def test_release_local_operation_cancel_is_wired_and_replayable(
    tmp_path: Path,
) -> None:
    app = _app(tmp_path / "state")
    with TestClient(app) as client:
        profile = _create_profile(
            client,
            name="Cancellation server",
            key="create-profile-cancellation-0001",
        ).json()
        provider = app.state.desktop_release_provider
        project = provider._store.create_project(
            local_v1.ProjectCreateV1.model_validate(
                _project(profile["profile_id"], name="Cancellable project")
            ),
            idempotency_key="seed-cancellable-project-0001",
        )
        reservation = provider._store.begin_project_runtime_action(
            route=f"/desktop/v1/projects/{project.project_id}/activate",
            operation_kind="project_activate",
            project_id=project.project_id,
            key="reserve-cancellable-activation-0001",
            body={},
            if_match=project.etag,
        )
        operation = reservation.operation
        route = f"/desktop/v1/operations/{operation.operation_id}/cancel"
        headers = {
            **SESSION_HEADERS,
            "If-Match": operation.etag,
            "Idempotency-Key": "cancel-local-operation-0001",
        }

        response = client.post(route, headers=headers)
        replay = client.post(route, headers=headers)

        assert response.status_code == 202
        assert response.json()["state"] == "cancelled"
        assert response.json()["operation_id"] == operation.operation_id
        assert replay.status_code == 202
        assert replay.json() == response.json()
        assert provider._store.get_project(project.project_id).state == "draft"


def test_release_local_operation_cancel_rejects_unbound_remote_maintenance(
    tmp_path: Path,
) -> None:
    app = _app(tmp_path / "state")
    with TestClient(app) as client:
        profile = _create_profile(
            client,
            name="Maintenance server",
            key="create-profile-maintenance-cancel-0001",
        ).json()
        provider = app.state.desktop_release_provider
        project = provider._store.create_project(
            local_v1.ProjectCreateV1.model_validate(
                _project(profile["profile_id"], name="Maintenance project")
            ),
            idempotency_key="seed-maintenance-project-0001",
        )
        reservation = provider._store.begin_project_runtime_action(
            route=f"/desktop/v1/projects/{project.project_id}/repair",
            operation_kind="project_repair",
            project_id=project.project_id,
            key="reserve-maintenance-repair-0001",
            body={},
            if_match=project.etag,
        )
        operation = reservation.operation

        response = client.post(
            f"/desktop/v1/operations/{operation.operation_id}/cancel",
            headers={
                **SESSION_HEADERS,
                "If-Match": operation.etag,
                "Idempotency-Key": "cancel-maintenance-repair-0001",
            },
        )

        assert response.status_code == 409
        assert response.json()["code"] == "operation_cancellation_unavailable"
        assert response.json()["next_action"] == (
            "Wait for the operation to finish, then run Check again."
        )
        assert (
            provider._store.get_local_operation(operation.operation_id).state
            == "queued"
        )


def test_running_profile_connect_cancel_interrupts_lifecycle_and_is_replayable(
    tmp_path: Path,
) -> None:
    lifecycle = CancellableBlockingRemoteLifecycle()
    app = _app(tmp_path / "state", remote_lifecycle=lifecycle)
    with TestClient(app) as client:
        profile = _create_profile(
            client,
            name="Interruptible connection",
            key="profile-interruptible-create-0001",
        ).json()
        provider = app.state.desktop_release_provider
        original_publish = provider.publish_state_changed
        operation_published = Event()

        def publish_state_changed() -> None:
            original_publish()
            operation_published.set()

        provider.publish_state_changed = publish_state_changed
        connect_responses: list[Any] = []

        def connect() -> None:
            connect_responses.append(
                client.post(
                    f"/desktop/v1/profiles/{profile['profile_id']}/connect",
                    headers={
                        **SESSION_HEADERS,
                        "If-Match": profile["etag"],
                        "Idempotency-Key": "profile-interruptible-connect-0001",
                    },
                )
            )

        thread = Thread(target=connect)
        thread.start()
        assert lifecycle.started.wait(2)
        assert operation_published.wait(1)
        state = client.get("/desktop/v1/state", headers=SESSION_HEADERS).json()
        assert len(state["pending_operation_ids"]) == 1
        operation_id = state["pending_operation_ids"][0]
        running = client.get(
            f"/desktop/v1/operations/{operation_id}", headers=SESSION_HEADERS
        ).json()
        assert running["state"] == "running"
        cancel_headers = {
            **SESSION_HEADERS,
            "If-Match": running["etag"],
            "Idempotency-Key": "profile-interruptible-cancel-0001",
        }

        started = time.monotonic()
        cancelled = client.post(
            f"/desktop/v1/operations/{operation_id}/cancel",
            headers=cancel_headers,
        )
        elapsed = time.monotonic() - started
        replay = client.post(
            f"/desktop/v1/operations/{operation_id}/cancel",
            headers=cancel_headers,
        )
        thread.join(2)

        assert elapsed < 1.0
        assert cancelled.status_code == 202
        assert cancelled.json()["state"] == "cancelled"
        assert replay.content == cancelled.content
        assert not thread.is_alive()
        assert len(connect_responses) == 1
        assert connect_responses[0].json()["state"] == "cancelled"
        assert lifecycle.disconnect_calls == 1
        assert lifecycle.current == RemoteLifecycleSnapshot(None, "disconnected")
        assert (
            client.get(
                f"/desktop/v1/profiles/{profile['profile_id']}", headers=SESSION_HEADERS
            ).json()["connection_state"]
            == "disconnected"
        )


def test_release_execution_mode_gate_rejects_unsupported_retry_before_core(
    tmp_path: Path,
) -> None:
    bridge = Mock(spec=DesktopCoreBridgeV1)
    app = _app(tmp_path / "state", core_bridge=bridge)
    with TestClient(app) as client:
        profile = _create_profile(
            client,
            name="Unavailable release mode server",
            key="create-profile-unavailable-retry-0001",
        ).json()
        request = _project(profile["profile_id"])
        request["execution"] = {
            "mode": "self-deployed",
            "hf_model": "open-models/release-gated-model",
        }
        provider = app.state.desktop_release_provider
        project = provider._store.create_project(
            local_v1.ProjectCreateV1.model_validate(request),
            idempotency_key="seed-unavailable-retry-project-0001",
        )
        provider._active_project_for_runtime = (  # type: ignore[method-assign]
            lambda: CoreRuntimeSessionBinding(project=project, generation=1)
        )
        headers = {
            **SESSION_HEADERS,
            "If-Match": project.etag,
            "Idempotency-Key": "unavailable-retry-mutation-0001",
        }

        retry_body = {"terminal_attempt_id": "attempt-terminal-1"}
        retry = client.post("/desktop/v1/runs/run-1/retry", headers=headers, json=retry_body)
        retry_replay = client.post(
            "/desktop/v1/runs/run-1/retry", headers=headers, json=retry_body
        )

        for response in (retry, retry_replay):
            assert response.status_code == 409
            assert response.json()["code"] == "self_deployed_release_unavailable"
            assert response.json()["category"] == "run"
        assert bridge.method_calls == []


def test_create_run_rejects_explicitly_pending_evolution_setup_before_core(
    tmp_path: Path,
) -> None:
    bridge = Mock(spec=DesktopCoreBridgeV1)
    app = _app(tmp_path / "state", core_bridge=bridge)
    with TestClient(app) as client:
        profile = _create_profile(
            client,
            name="Pending setup server",
            key="pending-setup-profile-create-0001",
        ).json()
        request = _project(profile["profile_id"], name="Pending setup project")
        request["evolution_configuration_state"] = "pending"
        provider = app.state.desktop_release_provider
        project = provider._store.create_project(
            local_v1.ProjectCreateV1.model_validate(request),
            idempotency_key="pending-setup-project-create-0001",
        )
        provider._active_project_for_runtime = (  # type: ignore[method-assign]
            lambda: CoreRuntimeSessionBinding(project=project, generation=1)
        )

        response = client.post(
            "/desktop/v1/runs",
            headers={
                **SESSION_HEADERS,
                "If-Match": project.etag,
                "Idempotency-Key": "pending-setup-run-create-0001",
            },
            json={"project_id": project.project_id},
        )

        assert response.status_code == 409
        assert response.json()["code"] == "evolution_configuration_pending"
        assert response.json()["category"] == "project"
        assert bridge.method_calls == []


def test_release_discovery_health_and_desktop_session_auth(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    compared_values: list[tuple[bytes, bytes]] = []
    compare_digest = hmac.compare_digest

    def observe_compare_digest(candidate: bytes, expected: bytes) -> bool:
        compared_values.append((candidate, expected))
        return compare_digest(candidate, expected)

    monkeypatch.setattr(release_app_module.hmac, "compare_digest", observe_compare_digest)
    app = _app(tmp_path / "state")
    with TestClient(app) as client:
        version = client.get("/version")
        assert version.status_code == 200
        assert version.json() == {
            "schema_version": "1",
            "api_name": "openevo-desktop-local-api",
            "preferred_major": 1,
            "supported_majors": [1],
            "openapi_sha256": DESKTOP_OPENAPI_SHA256,
            "build_version": "0.1.8",
            "source_commit": SOURCE_COMMIT,
            "build_channel": "test",
            "provider_kind": "desktop_sidecar",
            "feature_flags": ["remote_profiles"],
        }
        assert SESSION_TOKEN not in version.text

        challenge = "a" * 64
        health = client.get("/health", headers={"X-OpenEvo-Native-Challenge": challenge})
        domain = f"openevo-native-sidecar-v1\0{INSTANCE_ID}\0{challenge}".encode("ascii")
        assert health.status_code == 200
        assert (
            health.json()["instance_proof"]
            == hmac.new(READINESS_KEY, domain, hashlib.sha256).hexdigest()
        )
        assert SESSION_TOKEN not in health.text

        release_state = client.get("/desktop/v1/state", headers=SESSION_HEADERS)
        assert release_state.status_code == 200
        assert release_state.json()["execution_mode_capabilities"] == {
            "schema_version": "1",
            "modes": [
                {
                    "mode": "codex_subscription_transcript",
                    "display_name": "Subscription",
                    "support_state": "supported",
                    "reason_code": None,
                    "message": "Available in this OpenEvo Desktop release.",
                },
                {
                    "mode": "self-deployed",
                    "display_name": "Self-deployed",
                    "support_state": "unavailable",
                    "reason_code": "self_deployed_release_unavailable",
                    "message": (
                        "Self-deployed execution is not available in this OpenEvo Desktop "
                        "release. Choose Subscription to save or run this project."
                    ),
                },
            ],
        }

        for response in (
            client.get("/desktop/v1/state"),
            client.get(
                "/desktop/v1/state",
                headers={"X-OpenEvo-Desktop-Session": "wrong" * 8},
            ),
            client.get(
                "/desktop/v1/state",
                headers=[
                    ("X-OpenEvo-Desktop-Session", SESSION_TOKEN),
                    ("X-OpenEvo-Desktop-Session", SESSION_TOKEN),
                ],
            ),
        ):
            assert response.status_code == 401
            assert response.json()["code"] == "desktop_session_invalid"
            assert response.json()["http_status"] == 401
            assert SESSION_TOKEN not in response.text

        missing_challenge = client.get("/health")
        malformed_challenge = client.get(
            "/health", headers={"X-OpenEvo-Native-Challenge": "not-a-challenge"}
        )
        assert missing_challenge.status_code == 403
        assert malformed_challenge.status_code == 403
        assert missing_challenge.json()["code"] == "native_challenge_invalid"
        assert compared_values
        session_comparisons = [
            (candidate, expected)
            for candidate, expected in compared_values
            if expected == SESSION_TOKEN.encode("utf-8")
        ]
        assert len(session_comparisons) >= 3
        assert SESSION_TOKEN not in caplog.text


def test_profile_and_project_crud_preserve_idempotency_and_etags(tmp_path: Path) -> None:
    app = _app(tmp_path / "state")
    with TestClient(app) as client:
        created = _create_profile(client, name="A server", key="profile-create-0001")
        replay = _create_profile(client, name="A server", key="profile-create-0001")
        assert created.status_code == replay.status_code == 201
        assert created.json() == replay.json()
        assert created.headers["etag"] == created.json()["etag"]
        profile = created.json()

        conflict = _create_profile(client, name="Different", key="profile-create-0001")
        assert conflict.status_code == 409
        assert conflict.json()["code"] == "idempotency_key_reused"

        fetched = client.get(
            f"/desktop/v1/profiles/{profile['profile_id']}", headers=SESSION_HEADERS
        )
        assert fetched.status_code == 200
        assert fetched.headers["etag"] == fetched.json()["etag"] == profile["etag"]

        stale = client.patch(
            f"/desktop/v1/profiles/{profile['profile_id']}",
            headers={**SESSION_HEADERS, "If-Match": f'"{"0" * 64}"'},
            json={"name": "Renamed"},
        )
        assert stale.status_code == 412
        assert stale.json()["code"] == "etag_precondition_failed"

        patched = client.patch(
            f"/desktop/v1/profiles/{profile['profile_id']}",
            headers={**SESSION_HEADERS, "If-Match": profile["etag"]},
            json={"name": "Renamed"},
        )
        assert patched.status_code == 200
        assert patched.headers["etag"] == patched.json()["etag"]
        assert patched.json()["etag"] != profile["etag"]
        profile = patched.json()

        project = client.post(
            "/desktop/v1/projects",
            headers={**SESSION_HEADERS, "Idempotency-Key": "project-create-0001"},
            json=_project(profile["profile_id"]),
        )
        assert project.status_code == 201
        assert project.headers["etag"] == project.json()["etag"]
        project_body = project.json()

        in_use = client.delete(
            f"/desktop/v1/profiles/{profile['profile_id']}",
            headers={**SESSION_HEADERS, "If-Match": profile["etag"]},
        )
        assert in_use.status_code == 409
        assert in_use.json()["code"] == "resource_in_use"

        project_patch = client.patch(
            f"/desktop/v1/projects/{project_body['project_id']}",
            headers={**SESSION_HEADERS, "If-Match": project_body["etag"]},
            json={"name": "Updated project"},
        )
        assert project_patch.status_code == 200
        assert project_patch.headers["etag"] == project_patch.json()["etag"]
        project_body = project_patch.json()

        deleted_project = client.delete(
            f"/desktop/v1/projects/{project_body['project_id']}",
            headers={**SESSION_HEADERS, "If-Match": project_body["etag"]},
        )
        assert deleted_project.status_code == 204
        deleted_profile = client.delete(
            f"/desktop/v1/profiles/{profile['profile_id']}",
            headers={**SESSION_HEADERS, "If-Match": profile["etag"]},
        )
        assert deleted_profile.status_code == 204


def test_remote_connection_lifecycle_is_etag_bound_and_idempotent(tmp_path: Path) -> None:
    lifecycle = FakeRemoteLifecycle()
    app = _app(tmp_path / "state", remote_lifecycle=lifecycle)
    with TestClient(app) as client:
        created = _create_profile(client, name="Remote", key="profile-connect-create-0001")
        profile = created.json()
        profile_id = profile["profile_id"]

        renamed = client.patch(
            f"/desktop/v1/profiles/{profile_id}",
            headers={**SESSION_HEADERS, "If-Match": profile["etag"]},
            json={"name": "Remote renamed"},
        ).json()
        stale = client.post(
            f"/desktop/v1/profiles/{profile_id}/connect",
            headers={
                **SESSION_HEADERS,
                "If-Match": profile["etag"],
                "Idempotency-Key": "profile-connect-stale-0001",
            },
        )
        assert stale.status_code == 412
        assert lifecycle.connect_calls == 0

        connect_headers = {
            **SESSION_HEADERS,
            "If-Match": renamed["etag"],
            "Idempotency-Key": "profile-connect-action-0001",
        }
        connected = client.post(
            f"/desktop/v1/profiles/{profile_id}/connect", headers=connect_headers
        )
        replay = client.post(f"/desktop/v1/profiles/{profile_id}/connect", headers=connect_headers)
        assert connected.status_code == replay.status_code == 202
        assert connected.json() == replay.json()
        assert connected.headers["etag"] == connected.json()["etag"]
        assert replay.headers["etag"] == replay.json()["etag"]
        assert connected.json()["state"] == "succeeded"
        assert connected.json()["result"]["connection_state"] == "host_key_required"
        assert lifecycle.connect_calls == 1

        review_state = client.get("/desktop/v1/state", headers=SESSION_HEADERS).json()["core"]
        assert review_state == {
            "state": "host_key_review",
            "profile_id": profile_id,
            "active_tunnel": False,
            "operation_id": connected.json()["operation_id"],
            "host_key_review": {
                "algorithm": lifecycle.candidate.algorithm,
                "fingerprint": lifecycle.candidate.fingerprint,
            },
            "core": None,
            "failure": None,
        }
        reviewed_profile = client.get(
            f"/desktop/v1/profiles/{profile_id}", headers=SESSION_HEADERS
        ).json()
        assert reviewed_profile["connection_state"] == "host_key_required"
        assert reviewed_profile["host_key_fingerprint"] is None

        blocked_patch = client.patch(
            f"/desktop/v1/profiles/{profile_id}",
            headers={**SESSION_HEADERS, "If-Match": reviewed_profile["etag"]},
            json={"name": "Must not change"},
        )
        assert blocked_patch.status_code == 409
        assert blocked_patch.json()["code"] == "resource_in_use"

        accept_headers = {
            **SESSION_HEADERS,
            "If-Match": reviewed_profile["etag"],
            "Idempotency-Key": "profile-host-key-accept-0001",
        }
        acceptance = {
            "algorithm": lifecycle.candidate.algorithm,
            "fingerprint": lifecycle.candidate.fingerprint,
        }
        accepted = client.post(
            f"/desktop/v1/profiles/{profile_id}/host-key/accept",
            headers=accept_headers,
            json=acceptance,
        )
        accepted_replay = client.post(
            f"/desktop/v1/profiles/{profile_id}/host-key/accept",
            headers=accept_headers,
            json=acceptance,
        )
        assert accepted.status_code == accepted_replay.status_code == 202
        assert accepted.json() == accepted_replay.json()
        assert accepted.headers["etag"] == accepted.json()["etag"]
        assert accepted_replay.headers["etag"] == accepted_replay.json()["etag"]
        assert lifecycle.accept_calls == 1
        online_profile = client.get(
            f"/desktop/v1/profiles/{profile_id}", headers=SESSION_HEADERS
        ).json()
        assert online_profile["connection_state"] == "connected"
        core_state = client.get("/desktop/v1/state", headers=SESSION_HEADERS).json()["core"]
        assert core_state["state"] == "offline"
        assert core_state["failure"]["code"] == "core_not_started"

        disconnect_headers = {
            **SESSION_HEADERS,
            "If-Match": online_profile["etag"],
            "Idempotency-Key": "profile-disconnect-action-0001",
        }
        disconnected = client.post(
            f"/desktop/v1/profiles/{profile_id}/disconnect",
            headers=disconnect_headers,
        )
        disconnected_replay = client.post(
            f"/desktop/v1/profiles/{profile_id}/disconnect",
            headers=disconnect_headers,
        )
        assert disconnected.status_code == disconnected_replay.status_code == 202
        assert disconnected.json() == disconnected_replay.json()
        assert disconnected.headers["etag"] == disconnected.json()["etag"]
        assert disconnected_replay.headers["etag"] == disconnected_replay.json()["etag"]
        assert lifecycle.disconnect_calls == 1
        assert (
            client.get("/desktop/v1/state", headers=SESSION_HEADERS).json()["core"]["state"]
            == "disconnected"
        )

    assert lifecycle.closed


def test_cross_profile_keys_serialize_reservation_and_lifecycle_order(tmp_path: Path) -> None:
    lifecycle = RacingRemoteLifecycle()
    app = _app(tmp_path / "state", remote_lifecycle=lifecycle)
    with TestClient(app) as client:
        action_lock = ObservedActionLock()
        app.state.desktop_release_provider._connection_action_lock = action_lock
        first = _create_profile(client, name="A", key="profile-race-create-a").json()
        second = _create_profile(client, name="B", key="profile-race-create-b").json()
        responses: dict[str, Any] = {}

        def connect(name: str, profile: dict[str, object]) -> None:
            responses[name] = client.post(
                f"/desktop/v1/profiles/{profile['profile_id']}/connect",
                headers={
                    **SESSION_HEADERS,
                    "If-Match": cast(str, profile["etag"]),
                    "Idempotency-Key": f"profile-race-connect-{name.lower()}",
                },
            )

        first_thread = Thread(target=connect, args=("A", first))
        second_thread = Thread(target=connect, args=("B", second))
        first_thread.start()
        assert lifecycle.first_started.wait(2)
        second_thread.start()
        try:
            assert action_lock.second_waiting.wait(2)
            assert not lifecycle.second_finished.is_set()
            assert second_thread.is_alive()
        finally:
            lifecycle.release_first.set()
        first_thread.join(5)
        second_thread.join(5)

        assert not first_thread.is_alive()
        assert not second_thread.is_alive()
        assert lifecycle.second_finished.is_set()
        assert responses["A"].status_code == 503
        assert responses["B"].status_code == 202
        assert lifecycle.current.profile_id == second["profile_id"]
        assert lifecycle.current.state == "connected"
        state = client.get("/desktop/v1/state", headers=SESSION_HEADERS).json()["core"]
        assert state["profile_id"] == second["profile_id"]
        assert state["failure"]["code"] == "core_not_started"
        stored_first = client.get(
            f"/desktop/v1/profiles/{first['profile_id']}", headers=SESSION_HEADERS
        ).json()
        stored_second = client.get(
            f"/desktop/v1/profiles/{second['profile_id']}", headers=SESSION_HEADERS
        ).json()
        assert stored_first["connection_state"] == "disconnected"
        assert stored_second["connection_state"] == "connected"


def test_disconnect_of_non_owner_does_not_displace_actual_owner(tmp_path: Path) -> None:
    root = tmp_path / "state"
    lifecycle = FakeRemoteLifecycle()

    def connect(profile) -> RemoteConnectionResult:
        lifecycle.connect_calls += 1
        lifecycle.current = RemoteLifecycleSnapshot(profile.profile_id, "connected")
        return RemoteConnectionResult(profile.profile_id, "connected")

    lifecycle.connect = connect  # type: ignore[method-assign]
    app = _app(root, remote_lifecycle=lifecycle)
    with TestClient(app) as client:
        owner = _create_profile(client, name="Owner A", key="profile-owner-a-create").json()
        other = _create_profile(client, name="Other B", key="profile-other-b-create").json()
        connected = client.post(
            f"/desktop/v1/profiles/{owner['profile_id']}/connect",
            headers={
                **SESSION_HEADERS,
                "If-Match": owner["etag"],
                "Idempotency-Key": "profile-owner-a-connect",
            },
        )
        assert connected.status_code == 202

        headers = {
            **SESSION_HEADERS,
            "If-Match": other["etag"],
            "Idempotency-Key": "profile-other-b-disconnect",
        }
        failed = client.post(
            f"/desktop/v1/profiles/{other['profile_id']}/disconnect", headers=headers
        )
        replay = client.post(
            f"/desktop/v1/profiles/{other['profile_id']}/disconnect", headers=headers
        )

        assert failed.status_code == replay.status_code == 503
        assert failed.content == replay.content
        assert lifecycle.disconnect_calls == 0
        assert lifecycle.current == RemoteLifecycleSnapshot(owner["profile_id"], "connected")
        assert (
            client.get(
                f"/desktop/v1/profiles/{owner['profile_id']}", headers=SESSION_HEADERS
            ).json()["connection_state"]
            == "connected"
        )
        assert (
            client.get(
                f"/desktop/v1/profiles/{other['profile_id']}", headers=SESSION_HEADERS
            ).json()["connection_state"]
            == "disconnected"
        )
        state = client.get("/desktop/v1/state", headers=SESSION_HEADERS).json()["core"]
        assert state["profile_id"] == owner["profile_id"]

    restarted_lifecycle = FakeRemoteLifecycle()
    restarted = _app(root, remote_lifecycle=restarted_lifecycle)
    with TestClient(restarted) as client:
        restarted_replay = client.post(
            f"/desktop/v1/profiles/{other['profile_id']}/disconnect", headers=headers
        )
        assert restarted_replay.status_code == 503
        assert restarted_replay.content == failed.content
        assert restarted_lifecycle.disconnect_calls == 0


def test_disconnect_reservation_blocks_concurrent_profile_delete_until_terminal(
    tmp_path: Path,
) -> None:
    lifecycle = FakeRemoteLifecycle()
    disconnect_started = Event()
    release_disconnect = Event()

    def disconnect(profile_id: str | None = None) -> None:
        lifecycle.disconnect_calls += 1
        assert profile_id is not None
        disconnect_started.set()
        assert release_disconnect.wait(5)
        lifecycle.current = RemoteLifecycleSnapshot(None, "disconnected")

    lifecycle.disconnect = disconnect  # type: ignore[method-assign]
    app = _app(tmp_path / "state", remote_lifecycle=lifecycle)
    with TestClient(app) as client:
        profile = _create_profile(
            client, name="Delete reservation", key="profile-delete-reservation-create"
        ).json()
        disconnect_headers = {
            **SESSION_HEADERS,
            "If-Match": profile["etag"],
            "Idempotency-Key": "profile-delete-reservation-disconnect",
        }
        responses: list[Any] = []
        thread = Thread(
            target=lambda: responses.append(
                client.post(
                    f"/desktop/v1/profiles/{profile['profile_id']}/disconnect",
                    headers=disconnect_headers,
                )
            )
        )
        thread.start()
        assert disconnect_started.wait(2)
        try:
            blocked = client.delete(
                f"/desktop/v1/profiles/{profile['profile_id']}",
                headers={**SESSION_HEADERS, "If-Match": profile["etag"]},
            )
            assert blocked.status_code == 409
            assert blocked.json()["code"] == "resource_in_use"
        finally:
            release_disconnect.set()
        thread.join(5)

        assert not thread.is_alive()
        assert len(responses) == 1
        completed = responses[0]
        replay = client.post(
            f"/desktop/v1/profiles/{profile['profile_id']}/disconnect",
            headers=disconnect_headers,
        )
        assert completed.status_code == replay.status_code == 202
        assert completed.content == replay.content
        assert completed.headers["etag"] == replay.headers["etag"]
        assert completed.json()["state"] == "succeeded"
        assert lifecycle.disconnect_calls == 1

        terminal_profile = client.get(
            f"/desktop/v1/profiles/{profile['profile_id']}", headers=SESSION_HEADERS
        ).json()
        deleted = client.delete(
            f"/desktop/v1/profiles/{profile['profile_id']}",
            headers={**SESSION_HEADERS, "If-Match": terminal_profile["etag"]},
        )
        assert deleted.status_code == 204
        replay_after_delete = client.post(
            f"/desktop/v1/profiles/{profile['profile_id']}/disconnect",
            headers=disconnect_headers,
        )
        assert replay_after_delete.status_code == 202
        assert replay_after_delete.content == completed.content
        assert replay_after_delete.headers["etag"] == completed.headers["etag"]
        assert lifecycle.disconnect_calls == 1


def test_remote_connection_failure_is_typed_and_does_not_persist_details(
    tmp_path: Path,
) -> None:
    root = tmp_path / "state"
    lifecycle = FakeRemoteLifecycle()
    lifecycle.connect_error = RemoteConnectionFailedError(
        "ssh failed with secret at /Users/researcher/.ssh/id_ed25519"
    )
    app = _app(root, remote_lifecycle=lifecycle)
    with TestClient(app) as client:
        profile = _create_profile(
            client, name="Failing remote", key="profile-failure-create-0001"
        ).json()
        failed = client.post(
            f"/desktop/v1/profiles/{profile['profile_id']}/connect",
            headers={
                **SESSION_HEADERS,
                "If-Match": profile["etag"],
                "Idempotency-Key": "profile-connect-failure-0001",
            },
        )
        replay = client.post(
            f"/desktop/v1/profiles/{profile['profile_id']}/connect",
            headers={
                **SESSION_HEADERS,
                "If-Match": profile["etag"],
                "Idempotency-Key": "profile-connect-failure-0001",
            },
        )
        assert failed.status_code == 503
        assert replay.status_code == 503
        assert replay.content == failed.content
        assert failed.json()["code"] == "ssh_connection_failed"
        assert lifecycle.connect_calls == 1
        assert "secret" not in failed.text.lower()
        assert "/users/" not in failed.text.lower()
        stored = client.get(
            f"/desktop/v1/profiles/{profile['profile_id']}", headers=SESSION_HEADERS
        ).json()
        assert stored["connection_state"] == "disconnected"
        operation = app.state.desktop_release_provider._store._connection.execute(
            "SELECT document_json FROM local_operations"
        ).fetchone()
        assert operation is not None
        assert b'"state":"failed"' in bytes(operation[0])
        assert failed.json()["request_id"].encode() in bytes(operation[0])
        core_state = client.get("/desktop/v1/state", headers=SESSION_HEADERS).json()["core"]
        assert core_state["state"] == "offline"
        assert core_state["failure"]["code"] == "ssh_connection_failed"

    restarted_lifecycle = FakeRemoteLifecycle()
    restarted = _app(root, remote_lifecycle=restarted_lifecycle)
    with TestClient(restarted) as client:
        restarted_replay = client.post(
            f"/desktop/v1/profiles/{profile['profile_id']}/connect",
            headers={
                **SESSION_HEADERS,
                "If-Match": profile["etag"],
                "Idempotency-Key": "profile-connect-failure-0001",
            },
        )
        assert restarted_replay.status_code == 503
        assert restarted_replay.content == failed.content
        assert restarted_lifecycle.connect_calls == 0


@pytest.mark.parametrize("failure_timing", ["before_commit", "after_commit"])
def test_failure_finalization_reconciles_commit_return_ambiguity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure_timing: str
) -> None:
    lifecycle = FakeRemoteLifecycle()
    lifecycle.connect_error = RemoteConnectionFailedError("injected SSH failure")
    app = _app(tmp_path / "state", remote_lifecycle=lifecycle)
    with TestClient(app) as client:
        profile = _create_profile(
            client, name="Failure finalization", key="profile-failure-finalize-create"
        ).json()
        store = app.state.desktop_release_provider._store
        original_fail = store.fail_profile_runtime_action
        calls = 0

        def fail_once(*args: object, **kwargs: object):
            nonlocal calls
            calls += 1
            if calls == 1:
                if failure_timing == "after_commit":
                    original_fail(*args, **kwargs)
                raise sqlite3.OperationalError("injected failure finalization error")
            return original_fail(*args, **kwargs)

        monkeypatch.setattr(store, "fail_profile_runtime_action", fail_once)
        headers = {
            **SESSION_HEADERS,
            "If-Match": profile["etag"],
            "Idempotency-Key": "profile-failure-finalize-connect",
        }

        failed = client.post(
            f"/desktop/v1/profiles/{profile['profile_id']}/connect", headers=headers
        )
        replay = client.post(
            f"/desktop/v1/profiles/{profile['profile_id']}/connect", headers=headers
        )

        assert failed.status_code == replay.status_code == 503
        assert failed.content == replay.content
        assert failed.json()["code"] == "ssh_connection_failed"
        assert calls == (2 if failure_timing == "before_commit" else 1)
        assert lifecycle.connect_calls == 1
        assert lifecycle.disconnect_calls == 1
        assert lifecycle.current == RemoteLifecycleSnapshot(None, "disconnected")
        terminal_profile = client.get(
            f"/desktop/v1/profiles/{profile['profile_id']}", headers=SESSION_HEADERS
        ).json()
        assert terminal_profile["connection_state"] == "disconnected"
        deleted = client.delete(
            f"/desktop/v1/profiles/{profile['profile_id']}",
            headers={**SESSION_HEADERS, "If-Match": terminal_profile["etag"]},
        )
        assert deleted.status_code == 204


def test_uncommitted_failure_finalization_is_not_treated_as_terminal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    lifecycle = FakeRemoteLifecycle()
    lifecycle.connect_error = RemoteConnectionFailedError("injected SSH failure")
    app = _app(tmp_path / "state", remote_lifecycle=lifecycle)
    with TestClient(app) as client:
        profile = _create_profile(
            client, name="Uncommitted failure", key="profile-uncommitted-failure-create"
        ).json()
        store = app.state.desktop_release_provider._store
        calls = 0

        def fail_before_commit(*args: object, **kwargs: object):
            nonlocal calls
            calls += 1
            raise sqlite3.OperationalError("injected pre-commit failure")

        monkeypatch.setattr(store, "fail_profile_runtime_action", fail_before_commit)
        failed = client.post(
            f"/desktop/v1/profiles/{profile['profile_id']}/connect",
            headers={
                **SESSION_HEADERS,
                "If-Match": profile["etag"],
                "Idempotency-Key": "profile-uncommitted-failure-connect",
            },
        )

        assert failed.status_code == 503
        assert failed.json()["code"] == "local_provider_unavailable"
        assert calls == 2
        assert lifecycle.disconnect_calls == 0
        assert store.get_profile(profile["profile_id"]).connection_state == "connecting"
        operation = store._connection.execute(
            "SELECT state FROM local_operations WHERE resource_id = ?",
            (profile["profile_id"],),
        ).fetchone()
        assert operation is not None and operation[0] == "running"


@pytest.mark.parametrize("transport_owner", ["same", "other", "disconnected"])
def test_exact_failed_replay_repairs_only_its_owned_transport(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    transport_owner: str,
) -> None:
    lifecycle = FakeRemoteLifecycle()
    lifecycle.connect_error = RemoteConnectionFailedError("injected SSH failure")
    app = _app(tmp_path / "state", remote_lifecycle=lifecycle)
    with TestClient(app) as client:
        profile = _create_profile(
            client, name="Replay cleanup", key="profile-replay-cleanup-create"
        ).json()
        original_disconnect = lifecycle.disconnect
        cleanup_attempts = 0

        def fail_cleanup(profile_id: str | None = None) -> None:
            nonlocal cleanup_attempts
            cleanup_attempts += 1
            raise RemoteConnectionFailedError("injected cleanup failure")

        monkeypatch.setattr(lifecycle, "disconnect", fail_cleanup)
        headers = {
            **SESSION_HEADERS,
            "If-Match": profile["etag"],
            "Idempotency-Key": "profile-replay-cleanup-connect",
        }
        failed = client.post(
            f"/desktop/v1/profiles/{profile['profile_id']}/connect", headers=headers
        )
        assert failed.status_code == 503
        assert failed.json()["code"] == "ssh_connection_failed"
        assert cleanup_attempts == 1

        if transport_owner == "same":
            lifecycle.current = RemoteLifecycleSnapshot(profile["profile_id"], "connected")
        elif transport_owner == "other":
            lifecycle.current = RemoteLifecycleSnapshot("profile-other-owner", "connected")
        else:
            lifecycle.current = RemoteLifecycleSnapshot(None, "disconnected")
        monkeypatch.setattr(lifecycle, "disconnect", original_disconnect)

        replay = client.post(
            f"/desktop/v1/profiles/{profile['profile_id']}/connect", headers=headers
        )

        assert replay.status_code == 503
        assert replay.content == failed.content
        assert lifecycle.connect_calls == 1
        assert lifecycle.disconnect_calls == (1 if transport_owner == "same" else 0)
        if transport_owner == "same":
            assert lifecycle.current == RemoteLifecycleSnapshot(None, "disconnected")
        elif transport_owner == "other":
            assert lifecycle.current == RemoteLifecycleSnapshot("profile-other-owner", "connected")
        else:
            assert lifecycle.current == RemoteLifecycleSnapshot(None, "disconnected")

        terminal_profile = client.get(
            f"/desktop/v1/profiles/{profile['profile_id']}", headers=SESSION_HEADERS
        ).json()
        deleted = client.delete(
            f"/desktop/v1/profiles/{profile['profile_id']}",
            headers={**SESSION_HEADERS, "If-Match": terminal_profile["etag"]},
        )
        assert deleted.status_code == 204


def test_reserved_action_keeps_terminal_byte_slots_during_ssh(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "state"
    lifecycle = BlockingRemoteLifecycle()
    app = _app(root, remote_lifecycle=lifecycle)
    response_body = b""
    response_etag = ""
    with TestClient(app) as client:
        profile = _create_profile(
            client, name="Capacity owner", key="profile-capacity-create-0001"
        ).json()
        store = app.state.desktop_release_provider._store
        responses: list[Any] = []

        def connect() -> None:
            responses.append(
                client.post(
                    f"/desktop/v1/profiles/{profile['profile_id']}/connect",
                    headers={
                        **SESSION_HEADERS,
                        "If-Match": profile["etag"],
                        "Idempotency-Key": "profile-capacity-connect-0001",
                    },
                )
            )

        thread = Thread(target=connect)
        thread.start()
        assert lifecycle.started.wait(2)
        try:
            with store._transaction(write=False) as connection:
                _, used_bytes = store._recovery_usage(connection)
            monkeypatch.setattr(
                provider_store_module,
                "MAX_RECOVERY_BYTES",
                used_bytes + provider_store_module.PROFILE_RUNTIME_TERMINAL_RESERVATION_BYTES,
            )
            saturated = _create_profile(
                client, name="Capacity contender", key="profile-capacity-create-0002"
            )
            assert saturated.status_code == 503
            assert saturated.json()["code"] == "local_provider_unavailable"
        finally:
            lifecycle.release.set()
        thread.join(5)

        assert not thread.is_alive()
        assert len(responses) == 1
        assert responses[0].status_code == 202
        assert responses[0].json()["state"] == "succeeded"
        assert lifecycle.connect_calls == 1
        response_body = responses[0].content
        response_etag = responses[0].headers["etag"]

    restarted_lifecycle = FakeRemoteLifecycle()
    restarted = _app(root, remote_lifecycle=restarted_lifecycle)
    with TestClient(restarted) as client:
        replay = client.post(
            f"/desktop/v1/profiles/{profile['profile_id']}/connect",
            headers={
                **SESSION_HEADERS,
                "If-Match": profile["etag"],
                "Idempotency-Key": "profile-capacity-connect-0001",
            },
        )
        assert replay.status_code == 202
        assert replay.content == response_body
        assert replay.headers["etag"] == response_etag
        assert restarted_lifecycle.connect_calls == 0


def test_succeeded_connection_replay_is_exact_across_restart(tmp_path: Path) -> None:
    root = tmp_path / "state"
    lifecycle = FakeRemoteLifecycle()

    def connect(profile) -> RemoteConnectionResult:
        lifecycle.connect_calls += 1
        lifecycle.current = RemoteLifecycleSnapshot(profile.profile_id, "connected")
        return RemoteConnectionResult(profile.profile_id, "connected")

    lifecycle.connect = connect  # type: ignore[method-assign]
    app = _app(root, remote_lifecycle=lifecycle)
    with TestClient(app) as client:
        profile = _create_profile(
            client, name="Frozen success", key="profile-frozen-create-0001"
        ).json()
        headers = {
            **SESSION_HEADERS,
            "If-Match": profile["etag"],
            "Idempotency-Key": "profile-frozen-connect-0001",
        }
        succeeded = client.post(
            f"/desktop/v1/profiles/{profile['profile_id']}/connect", headers=headers
        )
        assert succeeded.status_code == 202
        assert succeeded.json()["state"] == "succeeded"
        frozen_body = succeeded.content
        frozen_etag = succeeded.headers["etag"]

    restarted_lifecycle = FakeRemoteLifecycle()
    restarted = _app(root, remote_lifecycle=restarted_lifecycle)
    with TestClient(restarted) as client:
        replay = client.post(
            f"/desktop/v1/profiles/{profile['profile_id']}/connect", headers=headers
        )
        assert replay.status_code == 202
        assert replay.content == frozen_body
        assert replay.headers["etag"] == frozen_etag
        assert restarted_lifecycle.connect_calls == 0


@pytest.mark.parametrize("failure_timing", ["before_commit", "after_commit"])
def test_commit_return_failure_preserves_committed_success_or_compensates_running(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure_timing: str
) -> None:
    lifecycle = FakeRemoteLifecycle()

    def connect(profile) -> RemoteConnectionResult:
        lifecycle.connect_calls += 1
        lifecycle.current = RemoteLifecycleSnapshot(profile.profile_id, "connected")
        return RemoteConnectionResult(profile.profile_id, "connected")

    lifecycle.connect = connect  # type: ignore[method-assign]
    app = _app(tmp_path / "state", remote_lifecycle=lifecycle)
    with TestClient(app) as client:
        profile = _create_profile(
            client, name="Commit failure", key="profile-commit-create-0001"
        ).json()
        store = app.state.desktop_release_provider._store
        original_complete = store.complete_profile_runtime_action
        calls = 0

        def fail_once(*args: object, **kwargs: object):
            nonlocal calls
            calls += 1
            if calls == 1:
                if failure_timing == "after_commit":
                    original_complete(*args, **kwargs)
                raise sqlite3.OperationalError("injected final commit failure")
            return original_complete(*args, **kwargs)

        monkeypatch.setattr(store, "complete_profile_runtime_action", fail_once)
        headers = {
            **SESSION_HEADERS,
            "If-Match": profile["etag"],
            "Idempotency-Key": "profile-commit-connect-0001",
        }

        response = client.post(
            f"/desktop/v1/profiles/{profile['profile_id']}/connect", headers=headers
        )
        replay = client.post(
            f"/desktop/v1/profiles/{profile['profile_id']}/connect", headers=headers
        )

        assert response.content == replay.content
        assert lifecycle.connect_calls == 1
        stored = client.get(
            f"/desktop/v1/profiles/{profile['profile_id']}", headers=SESSION_HEADERS
        ).json()
        if failure_timing == "before_commit":
            assert response.status_code == replay.status_code == 503
            assert response.json()["code"] == "local_provider_unavailable"
            assert lifecycle.disconnect_calls == 1
            assert stored["connection_state"] == "disconnected"
        else:
            assert response.status_code == replay.status_code == 202
            assert response.json()["state"] == "succeeded"
            assert response.headers["etag"] == replay.headers["etag"]
            assert lifecycle.disconnect_calls == 0
            assert lifecycle.current.profile_id == profile["profile_id"]
            assert lifecycle.current.state == "connected"
            assert stored["connection_state"] == "connected"


def test_committed_success_survives_full_budget_before_commit_error_returns(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    lifecycle = FakeRemoteLifecycle()

    def connect(profile) -> RemoteConnectionResult:
        lifecycle.connect_calls += 1
        lifecycle.current = RemoteLifecycleSnapshot(profile.profile_id, "connected")
        return RemoteConnectionResult(profile.profile_id, "connected")

    lifecycle.connect = connect  # type: ignore[method-assign]
    app = _app(tmp_path / "state", remote_lifecycle=lifecycle)
    with TestClient(app) as client:
        profile = _create_profile(
            client, name="Committed owner", key="profile-committed-create-0001"
        ).json()
        store = app.state.desktop_release_provider._store
        original_complete = store.complete_profile_runtime_action
        committed = Event()
        release_error = Event()

        def commit_then_raise(*args: object, **kwargs: object):
            original_complete(*args, **kwargs)
            committed.set()
            assert release_error.wait(5)
            raise sqlite3.OperationalError("injected error after committed success")

        monkeypatch.setattr(store, "complete_profile_runtime_action", commit_then_raise)
        headers = {
            **SESSION_HEADERS,
            "If-Match": profile["etag"],
            "Idempotency-Key": "profile-committed-connect-0001",
        }
        responses: list[Any] = []

        thread = Thread(
            target=lambda: responses.append(
                client.post(
                    f"/desktop/v1/profiles/{profile['profile_id']}/connect", headers=headers
                )
            )
        )
        thread.start()
        assert committed.wait(2)
        filler = _create_profile(
            client, name="Concurrent filler", key="profile-concurrent-filler-0001"
        )
        assert filler.status_code == 201
        with store._transaction(write=False) as connection:
            _, used_bytes = store._recovery_usage(connection)
        monkeypatch.setattr(provider_store_module, "MAX_RECOVERY_BYTES", used_bytes)
        release_error.set()
        thread.join(5)

        assert not thread.is_alive()
        assert len(responses) == 1
        response = responses[0]
        replay = client.post(
            f"/desktop/v1/profiles/{profile['profile_id']}/connect", headers=headers
        )
        assert response.status_code == replay.status_code == 202
        assert response.content == replay.content
        assert response.headers["etag"] == replay.headers["etag"]
        assert response.json()["state"] == "succeeded"
        assert lifecycle.disconnect_calls == 0
        assert lifecycle.current.profile_id == profile["profile_id"]
        assert (
            client.get(
                f"/desktop/v1/profiles/{profile['profile_id']}", headers=SESSION_HEADERS
            ).json()["connection_state"]
            == "connected"
        )


def test_late_success_keeps_cancelled_terminal_and_closes_its_transport(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    lifecycle = FakeRemoteLifecycle()

    def connect(profile) -> RemoteConnectionResult:
        lifecycle.connect_calls += 1
        lifecycle.current = RemoteLifecycleSnapshot(profile.profile_id, "connected")
        return RemoteConnectionResult(profile.profile_id, "connected")

    lifecycle.connect = connect  # type: ignore[method-assign]
    app = _app(tmp_path / "state", remote_lifecycle=lifecycle)
    with TestClient(app) as client:
        profile = _create_profile(
            client, name="Cancelled terminal", key="profile-cancelled-create-0001"
        ).json()
        store = app.state.desktop_release_provider._store
        original_complete = store.complete_profile_runtime_action

        def cancel_before_complete(*args: object, **kwargs: object):
            current = store.get_profile(profile["profile_id"])
            with store._transaction(write=True) as connection:
                ProviderMutation(store, connection).set_profile_runtime_state(
                    profile["profile_id"],
                    if_match=current.etag,
                    connection_state="disconnected",
                    credential_slots=current.credential_slots,
                    host_key_fingerprint=current.host_key_fingerprint,
                )
            return original_complete(*args, **kwargs)

        monkeypatch.setattr(store, "complete_profile_runtime_action", cancel_before_complete)
        headers = {
            **SESSION_HEADERS,
            "If-Match": profile["etag"],
            "Idempotency-Key": "profile-cancelled-connect-0001",
        }
        cancelled = client.post(
            f"/desktop/v1/profiles/{profile['profile_id']}/connect", headers=headers
        )
        replay = client.post(
            f"/desktop/v1/profiles/{profile['profile_id']}/connect", headers=headers
        )

        assert cancelled.status_code == replay.status_code == 202
        assert cancelled.content == replay.content
        assert cancelled.headers["etag"] == replay.headers["etag"]
        assert cancelled.json()["state"] == "cancelled"
        assert lifecycle.connect_calls == 1
        assert lifecycle.disconnect_calls == 1
        assert lifecycle.current.state == "disconnected"
        assert (
            client.get(
                f"/desktop/v1/profiles/{profile['profile_id']}", headers=SESSION_HEADERS
            ).json()["connection_state"]
            == "disconnected"
        )


def test_pagination_cursor_and_typed_contract_errors(tmp_path: Path) -> None:
    clock = MutableClock()
    app = _app(tmp_path / "state", clock=clock)
    with TestClient(app) as client:
        first_profile = _create_profile(client, name="A", key="profile-page-create-0001")
        _create_profile(client, name="B", key="profile-page-create-0002")
        page = client.get(
            "/desktop/v1/profiles?limit=1&sort=name&direction=asc", headers=SESSION_HEADERS
        )
        assert page.status_code == 200
        assert [item["name"] for item in page.json()["items"]] == ["A"]
        cursor = page.json()["next_cursor"]
        assert cursor is not None

        next_page = client.get(
            "/desktop/v1/profiles",
            headers=SESSION_HEADERS,
            params={"limit": 1, "sort": "name", "direction": "asc", "after": cursor},
        )
        assert next_page.status_code == 200
        assert [item["name"] for item in next_page.json()["items"]] == ["B"]

        rebound = client.get(
            "/desktop/v1/profiles",
            headers=SESSION_HEADERS,
            params={"limit": 1, "sort": "updated_at", "direction": "asc", "after": cursor},
        )
        assert rebound.status_code == 400
        assert rebound.json()["code"] == "cursor_invalid"

        clock.now += timedelta(minutes=16)
        expired = client.get(
            "/desktop/v1/profiles",
            headers=SESSION_HEADERS,
            params={"limit": 1, "sort": "name", "direction": "asc", "after": cursor},
        )
        assert expired.status_code == 410
        assert expired.json()["code"] == "cursor_expired"

        missing = client.get("/desktop/v1/profiles/not-present", headers=SESSION_HEADERS)
        invalid = client.patch(
            f"/desktop/v1/profiles/{first_profile.json()['profile_id']}",
            headers={**SESSION_HEADERS, "If-Match": first_profile.json()["etag"]},
            json={},
        )
        assert missing.status_code == 404
        assert missing.json()["code"] == "resource_not_found"
        assert invalid.status_code == 422
        assert invalid.json()["code"] == "contract_validation_failed"


def test_unimplemented_routes_and_store_failures_are_typed_503(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    app = _app(tmp_path / "state")
    with TestClient(app) as client:
        unavailable = client.get("/desktop/v1/runs", headers=SESSION_HEADERS)
        assert unavailable.status_code == 503
        assert unavailable.json()["code"] == "provider_capability_unavailable"
        assert unavailable.json()["category"] == "run"

        def corrupt_store(*_args: object, **_kwargs: object):
            raise ProviderDataCorruptionError("unsafe SQLite detail at /private/provider.sqlite3")

        monkeypatch.setattr(DesktopProviderStore, "list_profiles", corrupt_store)
        failed = client.get("/desktop/v1/profiles", headers=SESSION_HEADERS)
        assert failed.status_code == 503
        assert failed.json()["code"] == "local_provider_unavailable"
        assert "sqlite" not in failed.text.lower()
        assert "/private" not in failed.text


def test_release_app_openapi_is_canonical_and_store_survives_restart(tmp_path: Path) -> None:
    root = tmp_path / "state"
    app = _app(root)
    assert app.openapi() == contract_app.openapi()
    assert canonical_sha256(app.openapi()) == DESKTOP_OPENAPI_SHA256
    with TestClient(create_contract_app()) as contract_client:
        assert contract_client.get("/version").status_code == 501

    with TestClient(app) as client:
        profile = _create_profile(client, name="Persistent", key="profile-persist-0001").json()
        project = client.post(
            "/desktop/v1/projects",
            headers={**SESSION_HEADERS, "Idempotency-Key": "project-persist-0001"},
            json=_project(profile["profile_id"], name="Persistent project"),
        ).json()

    restarted = _app(root)
    with TestClient(restarted) as client:
        fetched_profile = client.get(
            f"/desktop/v1/profiles/{profile['profile_id']}", headers=SESSION_HEADERS
        )
        fetched_project = client.get(
            f"/desktop/v1/projects/{project['project_id']}", headers=SESSION_HEADERS
        )
        state = client.get("/desktop/v1/state", headers=SESSION_HEADERS)
        assert fetched_profile.status_code == 200
        assert fetched_profile.json() == profile
        assert fetched_project.status_code == 200
        assert fetched_project.json() == project
        assert state.status_code == 200
        assert state.json()["contract"]["desktop_openapi_sha256"] == DESKTOP_OPENAPI_SHA256
        assert state.json()["core"]["state"] == "disconnected"
