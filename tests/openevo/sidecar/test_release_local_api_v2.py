from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
from threading import Event, Thread
import time

from fastapi.testclient import TestClient
import pytest

from desktop.sidecar.askpass_broker import AskpassPromptObservation
from desktop.sidecar.contracts.v2 import models as local_v2
from desktop.sidecar.core_bridge_v2 import DesktopCoreBridgeErrorV2
from desktop.sidecar.event_broker_v2 import DesktopEventBrokerV2
from desktop.sidecar.provider_store_v2 import (
    DesktopProviderStoreV2,
    LegacyProfileImportV2,
    LifecycleLogAppendV2,
)
from desktop.sidecar.release_app import (
    create_packaged_release_desktop_local_api_v2_app,
    create_release_desktop_local_api_v2_app,
)
from desktop.sidecar.release_capabilities import V0110_RELEASE_AUTHORITY_POLICY
from desktop.sidecar.release_provider_v2 import DesktopReleaseProviderV2
from desktop.sidecar.system_ssh_session import (
    AskpassHelperAuthority,
    SystemOpenSshHostTrust,
    SystemOpenSshSessionError,
)
from openevo.backend.contracts.v2 import models as core_v2
from openevo.deployment.host_keys import (
    PendingSystemHostKeyReview,
    SystemKnownHostsPolicy,
)


SESSION = "desktop-session-" + ("s" * 48)
SOURCE_COMMIT = "1" * 40
NOW = datetime(2026, 7, 23, 10, 0, tzinfo=timezone.utc)


def _timestamp() -> str:
    return NOW.isoformat().replace("+00:00", "Z")


def _feature_digest(features: list[str]) -> str:
    return hashlib.sha256(json.dumps(features, separators=(",", ":")).encode("ascii")).hexdigest()


def _core_version() -> core_v2.VersionResponseV2:
    features = list(V0110_RELEASE_AUTHORITY_POLICY.required_core_feature_flags)
    return core_v2.VersionResponseV2(
        api_name="openevo-core-control-api",
        preferred_major=2,
        supported_majors=[2],
        mutation_major=2,
        contracts=[
            core_v2.ContractOfferV2(
                api_major=2,
                openapi_sha256=V0110_RELEASE_AUTHORITY_POLICY.core_openapi_sha256,
                event_schema_sha256=(V0110_RELEASE_AUTHORITY_POLICY.core_event_schema_sha256),
                access="mutation",
                mutation_compatible=True,
            )
        ],
        release_version="0.1.10",
        build_id="2" * 64,
        source_commit=SOURCE_COMMIT,
        build_channel="release",
        provider_kind="openevo_daemon",
        feature_flags=features,
        feature_set_sha256=_feature_digest(features),
        registry_sha256="3" * 64,
        runtime_contract_sha256="4" * 64,
        mutation_compatible=True,
    )


class _Catalog:
    def __init__(self) -> None:
        self.value = local_v2.SshHostCatalogV2(
            catalog_generation=1,
            hosts=[
                local_v2.SshHostHintV2(
                    ssh_host_alias="gpu-lab",
                    availability="selectable",
                    source_kind="literal_host",
                )
            ],
            warnings=[],
            scanned_at=_timestamp(),
        )

    def list_catalog(self) -> local_v2.SshHostCatalogV2:
        return self.value

    def rescan(
        self,
        request: local_v2.SshHostCatalogRescanV2,
        *,
        resource_generation: int,
        idempotency_key: str,
    ) -> local_v2.SshHostCatalogV2:
        assert type(request) is local_v2.SshHostCatalogRescanV2
        assert resource_generation == self.value.catalog_generation
        assert idempotency_key
        return self.value


class _Lifecycle:
    def __init__(self) -> None:
        self.active: tuple[str, int] | None = None
        self.calls: list[tuple[str, str, int]] = []
        self.connect_errors: list[Exception] = []
        self.disconnect_errors: list[Exception] = []
        self.review_outcome = "connected"
        self.review_cancel_events: list[Event | None] = []
        self.prompt_observer = None
        self.prompt_started: Event | None = None
        self.prompt_release: Event | None = None
        self.connect_started: Event | None = None
        self.connect_release: Event | None = None
        self.second_connect_started: Event | None = None
        self.connect_cancel_events: list[Event | None] = []
        self.block_connect_until_cancel = False

    def set_prompt_observer(self, observer) -> None:
        self.prompt_observer = observer

    def connect(
        self,
        profile: local_v2.RemoteWorkspaceProfileV2,
        *,
        cancel_event: Event | None = None,
    ) -> None:
        self.calls.append(("connect", profile.ssh_host_alias, profile.connection_generation))
        self.connect_cancel_events.append(cancel_event)
        connect_count = sum(call[0] == "connect" for call in self.calls)
        if connect_count >= 2 and self.second_connect_started is not None:
            self.second_connect_started.set()
        if self.connect_started is not None and self.connect_release is not None:
            self.connect_started.set()
            if self.block_connect_until_cancel:
                if cancel_event is not None:
                    assert cancel_event.wait(2)
                    raise SystemOpenSshSessionError(
                        "ssh_connection_cancelled",
                        "SSH connection was cancelled.",
                    )
                assert self.connect_release.wait(2)
                return
            assert self.connect_release.wait(2)
        if self.connect_errors:
            raise self.connect_errors.pop(0)
        if self.prompt_started is not None and self.prompt_release is not None:
            assert self.prompt_observer is not None
            self.prompt_observer(
                profile.profile_id,
                AskpassPromptObservation(
                    connection_generation=profile.connection_generation,
                    kind="passphrase",
                    state="pending",
                ),
            )
            self.prompt_started.set()
            assert self.prompt_release.wait(2)
            self.prompt_observer(
                profile.profile_id,
                AskpassPromptObservation(
                    connection_generation=profile.connection_generation,
                    kind="passphrase",
                    state="completed",
                ),
            )
        self.active = (profile.profile_id, profile.connection_generation)

    def disconnect(self, profile_id: str, connection_generation: int) -> None:
        self.calls.append(("disconnect", profile_id, connection_generation))
        if self.disconnect_errors:
            raise self.disconnect_errors.pop(0)
        if self.active == (profile_id, connection_generation - 1):
            self.active = None

    def review_host_key(
        self,
        profile: local_v2.RemoteWorkspaceProfileV2,
        request: local_v2.HostKeyReviewRequestV2,
        *,
        cancel_event: Event | None = None,
    ) -> str:
        del request
        self.review_cancel_events.append(cancel_event)
        self.calls.append(("review", profile.ssh_host_alias, profile.connection_generation))
        if self.review_outcome == "connected":
            self.active = (profile.profile_id, profile.connection_generation)
        return self.review_outcome

    def active_transport(self, profile_id: str, connection_generation: int) -> object:
        if self.active != (profile_id, connection_generation):
            raise RuntimeError("not active")
        return object()

    def close(self) -> None:
        self.active = None


class _CoreConnector:
    def __init__(self) -> None:
        self.calls: list[tuple[str, int]] = []
        self.version = _core_version()
        self.failure: DesktopCoreBridgeErrorV2 | None = None
        self.connect_started: Event | None = None
        self.connect_release: Event | None = None
        self.connect_cancelled = Event()
        self.cancel_events: list[Event | None] = []

    def connect_profile(
        self,
        profile_id: str,
        profile_connection_generation: int,
        *,
        cancel_event: Event | None = None,
    ) -> core_v2.VersionResponseV2:
        self.calls.append((profile_id, profile_connection_generation))
        self.cancel_events.append(cancel_event)
        if self.connect_started is not None:
            self.connect_started.set()
            if cancel_event is None:
                assert self.connect_release is not None
                assert self.connect_release.wait(2)
            else:
                assert cancel_event.wait(2)
                self.connect_cancelled.set()
                raise RuntimeError("connector cancelled")
        if self.failure is not None:
            raise self.failure
        return self.version

    def close(self) -> None:
        return None


class _Bridge:
    active_activation = None

    def close(self) -> None:
        return None


def _provider(
    tmp_path: Path,
    *,
    max_lifecycle_log_entries: int | None = None,
    event_broker: DesktopEventBrokerV2 | None = None,
) -> tuple[DesktopReleaseProviderV2, DesktopProviderStoreV2, _Lifecycle, _CoreConnector]:
    store_options = {}
    if max_lifecycle_log_entries is not None:
        store_options["max_lifecycle_log_entries"] = max_lifecycle_log_entries
    store = DesktopProviderStoreV2(
        tmp_path / "provider-v2",
        clock=lambda: NOW,
        **store_options,
    )
    lifecycle = _Lifecycle()
    connector = _CoreConnector()
    provider = DesktopReleaseProviderV2(
        store=store,
        catalog=_Catalog(),
        lifecycle=lifecycle,
        core_connector=connector,
        bridge=_Bridge(),
        bridge_store=None,
        workspace_import_store=None,
        event_broker=event_broker or DesktopEventBrokerV2(clock=lambda: NOW),
        build_version="0.1.10",
        source_commit=SOURCE_COMMIT,
        build_channel="release",
        instance_id="instance-v2",
        clock=lambda: NOW,
        own_resources=False,
    )
    return provider, store, lifecycle, connector


def _app_client(provider: DesktopReleaseProviderV2) -> TestClient:
    app = create_release_desktop_local_api_v2_app(
        session_token=SESSION,
        provider=provider,
        close_on_shutdown=False,
    )
    return TestClient(app)


def _headers(**extra: str) -> dict[str, str]:
    return {"X-OpenEvo-Desktop-Session": SESSION, **extra}


def _create_profile(client: TestClient) -> dict[str, object]:
    response = client.post(
        "/desktop/v2/profiles",
        headers=_headers(
            **{
                "X-OpenEvo-Resource-Generation": "1",
                "Idempotency-Key": "create-profile-key-0001",
            }
        ),
        json={
            "schema_version": "2",
            "display_name": "Lab GPU",
            "connection_authority": "system_openssh",
            "ssh_host_alias": "gpu-lab",
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


def _wait_lifecycle_operation(
    client: TestClient,
    operation: dict[str, object],
    *,
    timeout: float = 3.0,
) -> dict[str, object]:
    operation_id = str(operation["operation_id"])
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        response = client.get(
            f"/desktop/v2/operations/{operation_id}",
            headers=_headers(),
        )
        assert response.status_code == 200, response.text
        current = response.json()
        if current["status"] in {"succeeded", "failed", "cancelled"}:
            return current
        time.sleep(0.01)
    raise AssertionError("lifecycle operation did not become terminal")


def _wait_store_lifecycle_operation(
    store: DesktopProviderStoreV2,
    operation_id: str,
    *,
    timeout: float = 3.0,
) -> local_v2.LifecycleOperationV2:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        current = store.get_lifecycle_operation(operation_id)
        if current.status in {"succeeded", "failed", "cancelled"}:
            return current
        time.sleep(0.01)
    raise AssertionError("stored lifecycle operation did not become terminal")


def _changed_key_review(profile_id: str, generation: int) -> PendingSystemHostKeyReview:
    return PendingSystemHostKeyReview(
        review_id="host-key-review-1",
        review_sha256="9" * 64,
        profile_id=profile_id,
        connection_generation=generation,
        key_fingerprints=(("ssh-ed25519", "SHA256:" + ("A" * 43)),),
        repair_support="automatic_replacement_available",
        _policy=SystemKnownHostsPolicy(
            repair_support="automatic_replacement_available",
            reason="test",
            known_hosts_file=None,
            lookup_token=None,
            _file_identity=None,
        ),
        _authority_token=object(),
    )


def test_release_app_mounts_only_authenticated_v2_mutation_routes(tmp_path: Path) -> None:
    provider, store, _lifecycle, _connector = _provider(tmp_path)
    client = _app_client(provider)
    try:
        version = client.get("/version")
        assert version.status_code == 200
        payload = version.json()
        assert payload["preferred_major"] == 2
        assert payload["mutation_major"] == 2
        assert payload["mutation_compatible"] is True
        assert payload["openapi_sha256"] == (V0110_RELEASE_AUTHORITY_POLICY.desktop_openapi_sha256)

        assert client.get("/desktop/v2/state").status_code == 401
        assert client.get("/desktop/v2/state", headers=_headers()).status_code == 200
        assert client.get("/desktop/v1/state", headers=_headers()).status_code == 404
    finally:
        client.close()
        provider.close()
        store.close()


def test_lifecycle_routes_expose_pending_logs_cancel_ack_and_cursor_expiry(
    tmp_path: Path,
) -> None:
    provider, store, lifecycle, _connector = _provider(
        tmp_path,
        max_lifecycle_log_entries=2,
    )
    lifecycle.connect_started = Event()
    lifecycle.connect_release = Event()
    client = _app_client(provider)
    try:
        profile = _create_profile(client)
        started = client.post(
            f"/desktop/v2/profiles/{profile['profile_id']}/connect",
            headers=_headers(
                **{
                    "X-OpenEvo-Resource-Generation": "1",
                    "If-Match": str(profile["etag"]),
                    "Idempotency-Key": "observable-connect-profile-key-1",
                }
            ),
            json={"schema_version": "2", "expected_connection_generation": 1},
        )
        assert started.status_code == 202, started.text
        operation_id = started.json()["operation_id"]
        assert lifecycle.connect_started.wait(2)

        by_action = client.get(
            "/desktop/v2/operations/by-action",
            params={
                "action_id": "observable-connect-profile-key-1",
                "kind": "profile_connect",
            },
            headers=_headers(),
        )
        assert by_action.status_code == 200, by_action.text
        assert by_action.json()["operation_id"] == operation_id
        assert client.get(f"/desktop/v2/operations/{operation_id}").status_code == 401
        running = client.get(f"/desktop/v2/operations/{operation_id}", headers=_headers())
        assert running.status_code == 200, running.text
        assert running.json()["status"] == "running"
        state = client.get("/desktop/v2/state", headers=_headers()).json()
        assert [item["operation_id"] for item in state["pending_operations"]] == [operation_id]

        for text, source in (
            ("resolving alias", "desktop"),
            ("ssh connected", "ssh_stdout"),
        ):
            store.append_lifecycle_log(
                LifecycleLogAppendV2(
                    operation_id=operation_id,
                    source=source,
                    text=text,
                    truncated=False,
                )
            )
        first_page = client.get(
            f"/desktop/v2/operations/{operation_id}/logs?limit=1",
            headers=_headers(),
        )
        assert first_page.status_code == 200, first_page.text
        assert first_page.json()["items"][0]["text"] == "resolving alias"
        old_cursor = first_page.json()["next_cursor"]
        assert old_cursor

        for text, source in (
            ("daemon starting", "daemon_stdout"),
            ("daemon ready", "daemon_stderr"),
        ):
            store.append_lifecycle_log(
                LifecycleLogAppendV2(
                    operation_id=operation_id,
                    source=source,
                    text=text,
                    truncated=False,
                )
            )
        retained = client.get(
            f"/desktop/v2/operations/{operation_id}/logs?limit=100",
            headers=_headers(),
        )
        assert retained.status_code == 200, retained.text
        assert retained.json()["dropped_before_sequence"] == 2
        assert [item["text"] for item in retained.json()["items"]] == [
            "daemon starting",
            "daemon ready",
        ]
        incremental = client.get(
            f"/desktop/v2/operations/{operation_id}/logs",
            params={"after_sequence": 3, "limit": 100},
            headers=_headers(),
        )
        assert incremental.status_code == 200, incremental.text
        assert [item["sequence"] for item in incremental.json()["items"]] == [4]
        expired = client.get(
            f"/desktop/v2/operations/{operation_id}/logs",
            params={"after": old_cursor},
            headers=_headers(),
        )
        assert expired.status_code == 410, expired.text
        assert expired.json()["code"] == "lifecycle_log_cursor_expired"

        current = client.get(f"/desktop/v2/operations/{operation_id}", headers=_headers()).json()
        assert current["cancellable"] is True
        stale_cancel = client.post(
            f"/desktop/v2/operations/{operation_id}/cancel",
            headers=_headers(
                **{
                    "X-OpenEvo-Resource-Generation": "0",
                    "If-Match": '"' + ("0" * 64) + '"',
                    "Idempotency-Key": "cancel-observable-operation-01",
                }
            ),
            json={"schema_version": "2", "expected_operation_id": operation_id},
        )
        assert stale_cancel.status_code == 412, stale_cancel.text
        lifecycle.connect_release.set()
        terminal = _wait_lifecycle_operation(client, running.json())
        assert terminal["status"] == "succeeded"
        assert [
            item["operation_id"]
            for item in client.get("/desktop/v2/state", headers=_headers()).json()[
                "pending_operations"
            ]
        ] == [operation_id]

        acknowledged = client.post(
            f"/desktop/v2/operations/{operation_id}/acknowledge",
            headers=_headers(
                **{
                    "X-OpenEvo-Resource-Generation": "0",
                    "If-Match": terminal["etag"],
                    "Idempotency-Key": "ack-observable-operation-001",
                }
            ),
            json={
                "schema_version": "2",
                "expected_operation_id": operation_id,
                "expected_terminal_status": "succeeded",
            },
        )
        assert acknowledged.status_code == 204, acknowledged.text
        assert (
            client.get("/desktop/v2/state", headers=_headers()).json()["pending_operations"] == []
        )
    finally:
        lifecycle.connect_release.set()
        client.close()
        provider.close()
        store.close()


def test_profile_connect_cancellation_reaches_remote_install_stream(
    tmp_path: Path,
) -> None:
    provider, store, _lifecycle, connector = _provider(tmp_path)
    connector.connect_started = Event()
    connector.connect_release = Event()
    client = _app_client(provider)
    try:
        profile = _create_profile(client)
        started = client.post(
            f"/desktop/v2/profiles/{profile['profile_id']}/connect",
            headers=_headers(
                **{
                    "X-OpenEvo-Resource-Generation": "1",
                    "If-Match": str(profile["etag"]),
                    "Idempotency-Key": "cancel-streaming-connect-profile-1",
                }
            ),
            json={"schema_version": "2", "expected_connection_generation": 1},
        )
        assert started.status_code == 202, started.text
        operation_id = started.json()["operation_id"]
        assert connector.connect_started.wait(2)

        current = client.get(
            f"/desktop/v2/operations/{operation_id}",
            headers=_headers(),
        ).json()
        assert current["status"] == "running"
        assert current["cancellable"] is True
        cancelled = client.post(
            f"/desktop/v2/operations/{operation_id}/cancel",
            headers=_headers(
                **{
                    "X-OpenEvo-Resource-Generation": "0",
                    "If-Match": current["etag"],
                    "Idempotency-Key": "cancel-streaming-connect-operation-1",
                }
            ),
            json={"schema_version": "2", "expected_operation_id": operation_id},
        )
        assert cancelled.status_code == 202, cancelled.text
        assert connector.connect_cancelled.wait(2)
        terminal = _wait_lifecycle_operation(client, started.json())
        assert terminal["status"] == "cancelled"
        cancelled_profile = client.get(
            f"/desktop/v2/profiles/{profile['profile_id']}",
            headers=_headers(),
        ).json()
        assert cancelled_profile["connection_state"] == "disconnected"
        assert cancelled_profile["failure"] is None
        assert len(connector.cancel_events) == 1
        assert isinstance(connector.cancel_events[0], Event)
    finally:
        connector.connect_release.set()
        client.close()
        provider.close()
        store.close()


def test_profile_connect_cancellation_interrupts_initial_system_ssh(
    tmp_path: Path,
) -> None:
    provider, store, lifecycle, connector = _provider(tmp_path)
    lifecycle.connect_started = Event()
    lifecycle.connect_release = Event()
    lifecycle.block_connect_until_cancel = True
    client = _app_client(provider)
    try:
        profile = _create_profile(client)
        started = client.post(
            f"/desktop/v2/profiles/{profile['profile_id']}/connect",
            headers=_headers(
                **{
                    "X-OpenEvo-Resource-Generation": "1",
                    "If-Match": str(profile["etag"]),
                    "Idempotency-Key": "cancel-initial-ssh-connect-profile-1",
                }
            ),
            json={"schema_version": "2", "expected_connection_generation": 1},
        )
        assert started.status_code == 202, started.text
        operation_id = started.json()["operation_id"]
        assert lifecycle.connect_started.wait(2)
        current = client.get(
            f"/desktop/v2/operations/{operation_id}",
            headers=_headers(),
        ).json()
        cancelled = client.post(
            f"/desktop/v2/operations/{operation_id}/cancel",
            headers=_headers(
                **{
                    "X-OpenEvo-Resource-Generation": "0",
                    "If-Match": current["etag"],
                    "Idempotency-Key": "cancel-initial-ssh-operation-1",
                }
            ),
            json={"schema_version": "2", "expected_operation_id": operation_id},
        )
        assert cancelled.status_code == 202, cancelled.text
        terminal = _wait_lifecycle_operation(client, started.json())
        assert terminal["status"] == "cancelled"
        assert len(lifecycle.connect_cancel_events) == 1
        assert isinstance(lifecycle.connect_cancel_events[0], Event)
        assert connector.calls == []
        cancelled_profile = client.get(
            f"/desktop/v2/profiles/{profile['profile_id']}",
            headers=_headers(),
        ).json()
        assert cancelled_profile["connection_state"] == "disconnected"
        assert cancelled_profile["failure"] is None
    finally:
        lifecycle.connect_release.set()
        client.close()
        provider.close()
        store.close()


def test_release_v2_cors_admits_the_exact_renderer_headers_outside_error_boundary(
    tmp_path: Path,
) -> None:
    provider, store, _lifecycle, _connector = _provider(tmp_path)
    app = create_release_desktop_local_api_v2_app(
        session_token=SESSION,
        provider=provider,
        close_on_shutdown=False,
    )
    client = TestClient(app, raise_server_exceptions=False)
    try:
        preflight = client.options(
            "/desktop/v2/profiles/profile-1/connect",
            headers={
                "Origin": "tauri://localhost",
                "Access-Control-Request-Method": "POST",
                "Access-Control-Request-Headers": (
                    "content-type,idempotency-key,if-match,"
                    "x-openevo-desktop-session,x-openevo-resource-generation"
                ),
            },
        )
        assert preflight.status_code == 200, preflight.text
        assert preflight.headers["access-control-allow-origin"] == "tauri://localhost"
        assert (
            "x-openevo-resource-generation"
            in preflight.headers["access-control-allow-headers"].lower()
        )

        provider._health = lambda _arguments: (_ for _ in ()).throw(  # type: ignore[method-assign]
            RuntimeError("injected renderer-safe boundary failure")
        )
        failed = client.get("/health", headers={"Origin": "tauri://localhost"})
        assert failed.status_code == 500
        assert failed.headers["access-control-allow-origin"] == "tauri://localhost"

        lookalike = client.options(
            "/desktop/v2/state",
            headers={
                "Origin": "https://tauri.localhost.example",
                "Access-Control-Request-Method": "GET",
                "Access-Control-Request-Headers": "x-openevo-desktop-session",
            },
        )
        assert lookalike.status_code == 400
        assert "access-control-allow-origin" not in lookalike.headers
    finally:
        client.close()
        provider.close()
        store.close()


def test_packaged_v2_composition_owns_catalog_state_runtime_and_ssh_authorities(
    tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    ssh_dir = home / ".ssh"
    ssh_dir.mkdir(parents=True)
    (ssh_dir / "config").write_text("Host gpu-lab\n  IdentityFile ~/.ssh/lab\n")
    helper_path = tmp_path / "openevo-ssh-askpass"
    helper_path.write_bytes(b"#!/bin/sh\nexit 1\n")
    helper_path.chmod(0o755)
    helper_bytes = helper_path.read_bytes()
    helper = AskpassHelperAuthority.open(
        helper_path,
        expected_sha256=hashlib.sha256(helper_bytes).hexdigest(),
        expected_byte_size=len(helper_bytes),
    )
    trust = SystemOpenSshHostTrust(home=home, inherited_environment={"HOME": str(home)})
    state_root = tmp_path / "state-v2"
    environment = {
        "HOME": str(home),
        "OPENAI_API_KEY": "packaged-environment-secret",
    }

    app = create_packaged_release_desktop_local_api_v2_app(
        state_root=state_root,
        session_token=SESSION,
        instance_id="packaged-instance-v2",
        source_commit=SOURCE_COMMIT,
        build_version="0.1.10",
        build_channel="test",
        core_assets_root=tmp_path / "deferred-core-assets",
        system_ssh_askpass_helper=helper,
        system_ssh_host_trust=trust,
        home=home,
        inherited_environment=environment,
        close_on_shutdown=False,
    )
    client = TestClient(app)
    try:
        version = client.get("/version")
        assert version.status_code == 200, version.text
        assert version.json()["preferred_major"] == 2
        catalog = client.get("/desktop/v2/ssh-hosts", headers=_headers())
        assert catalog.status_code == 200, catalog.text
        assert [item["ssh_host_alias"] for item in catalog.json()["hosts"]] == ["gpu-lab"]
        assert client.get("/desktop/v1/state", headers=_headers()).status_code == 404
        assert (state_root / "provider-v2" / "provider-v2.sqlite3").is_file()
        assert (state_root / "provider-v2" / "core-bridge-v2").is_dir()
        assert (state_root / "workspace-imports-v2").is_dir()
        assert not (tmp_path / "deferred-core-assets").exists()
        executor = app.state.desktop_release_provider._lifecycle_executor
        assert SESSION in executor._secret_canaries
        assert "packaged-environment-secret" in executor._secret_canaries
        assert str(state_root.resolve()) in executor._forbidden_paths
        assert str(home.resolve()) in executor._forbidden_paths
    finally:
        client.close()
        app.state.desktop_release_provider.close()

    with pytest.raises(SystemOpenSshSessionError, match="authority is closed"):
        helper.verify()


def test_packaged_v2_restart_invalidates_process_authority_and_preserves_project(
    tmp_path: Path,
) -> None:
    state_root = tmp_path / "state-v2"
    state_root.mkdir(mode=0o700)
    state_root.chmod(0o700)
    seed = DesktopProviderStoreV2(state_root / "provider-v2", clock=lambda: NOW)
    created = seed.create_system_profile(
        local_v2.SystemOpenSshProfileCreateV2(
            display_name="Restart GPU",
            ssh_host_alias="gpu-lab",
        ),
        catalog_generation=1,
        idempotency_key="restart-create-profile-key-01",
    )
    connecting = seed.begin_profile_action(
        created.profile_id,
        local_v2.ProfileConnectionActionV2(
            expected_connection_generation=created.connection_generation
        ),
        action="connect",
        resource_generation=created.connection_generation,
        if_match=created.etag,
        idempotency_key="restart-connect-profile-key-1",
    )
    connected = seed.complete_profile_connection(
        created.profile_id,
        connection_generation=connecting.connection_generation,
        core_version=_core_version(),
    )
    seed.bind_active_project(
        created.profile_id,
        connection_generation=connected.connection_generation,
        project_id="desktop-project-restart",
    )
    seed.close()

    home = tmp_path / "home"
    ssh_dir = home / ".ssh"
    ssh_dir.mkdir(parents=True)
    (ssh_dir / "config").write_text("Host gpu-lab\n")
    helper_path = tmp_path / "openevo-ssh-askpass"
    helper_path.write_bytes(b"#!/bin/sh\nexit 1\n")
    helper_path.chmod(0o755)
    helper_bytes = helper_path.read_bytes()
    helper = AskpassHelperAuthority.open(
        helper_path,
        expected_sha256=hashlib.sha256(helper_bytes).hexdigest(),
        expected_byte_size=len(helper_bytes),
    )
    trust = SystemOpenSshHostTrust(home=home, inherited_environment={"HOME": str(home)})

    app = create_packaged_release_desktop_local_api_v2_app(
        state_root=state_root,
        session_token=SESSION,
        instance_id="packaged-restart-instance-v2",
        source_commit=SOURCE_COMMIT,
        build_version="0.1.10",
        build_channel="test",
        core_assets_root=tmp_path / "deferred-core-assets",
        system_ssh_askpass_helper=helper,
        system_ssh_host_trust=trust,
        home=home,
        inherited_environment={"HOME": str(home)},
        close_on_shutdown=False,
    )
    client = TestClient(app)
    try:
        recovered = client.get(f"/desktop/v2/profiles/{created.profile_id}", headers=_headers())
        assert recovered.status_code == 200, recovered.text
        profile = recovered.json()
        assert profile["connection_state"] == "disconnected"
        assert profile["connection_generation"] == 3
        assert profile["active_project_id"] == "desktop-project-restart"
        assert profile["core_api_major"] is None
        assert profile["core_registry_sha256"] is None
    finally:
        client.close()
        app.state.desktop_release_provider.close()


def test_profile_connect_uses_literal_alias_and_exact_generation(
    tmp_path: Path,
) -> None:
    provider, store, lifecycle, connector = _provider(tmp_path)
    client = _app_client(provider)
    try:
        profile = _create_profile(client)
        response = client.post(
            f"/desktop/v2/profiles/{profile['profile_id']}/connect",
            headers=_headers(
                **{
                    "X-OpenEvo-Resource-Generation": str(profile["connection_generation"]),
                    "If-Match": str(profile["etag"]),
                    "Idempotency-Key": "connect-profile-key-0001",
                }
            ),
            json={
                "schema_version": "2",
                "expected_connection_generation": profile["connection_generation"],
            },
        )
        assert response.status_code == 202, response.text
        operation = response.json()
        assert operation["kind"] == "profile_connect"
        assert operation["status"] in {"queued", "running"}
        operation = _wait_lifecycle_operation(client, operation)
        assert operation["status"] == "succeeded"

        connected = client.get(
            f"/desktop/v2/profiles/{profile['profile_id']}", headers=_headers()
        ).json()
        assert connected["connection_state"] == "connected"
        assert connected["connection_generation"] == 2
        assert connected["ssh_host_alias"] == "gpu-lab"
        assert connected["core_api_major"] == 2
        assert connected["core_registry_sha256"] == "3" * 64
        assert lifecycle.calls == [("connect", "gpu-lab", 2)]
        assert connector.calls == [(profile["profile_id"], 2)]

        stale = client.post(
            f"/desktop/v2/profiles/{profile['profile_id']}/disconnect",
            headers=_headers(
                **{
                    "X-OpenEvo-Resource-Generation": "1",
                    "If-Match": str(connected["etag"]),
                    "Idempotency-Key": "disconnect-stale-key-01",
                }
            ),
            json={
                "schema_version": "2",
                "expected_connection_generation": 1,
            },
        )
        assert stale.status_code == 412
        assert stale.json()["code"] == "profile_generation_changed"
        assert lifecycle.calls == [("connect", "gpu-lab", 2)]
    finally:
        client.close()
        provider.close()
        store.close()


def test_remote_account_failure_is_stable_and_private_across_public_surfaces(
    tmp_path: Path,
) -> None:
    event_broker = DesktopEventBrokerV2(clock=lambda: NOW)
    provider, store, lifecycle, _connector = _provider(
        tmp_path,
        event_broker=event_broker,
    )
    private_user = "private-account-canary"
    private_uid = "424242"
    private_home = "/srv/research/private-account-canary"
    private_nss = (
        f"{private_user}:x:{private_uid}:1000:Private User:{private_home}:/bin/sh"
    )
    private_command = "getent passwd 424242 && /bin/sh -c private-command-canary"
    lifecycle.connect_errors.append(
        SystemOpenSshSessionError(
            "ssh_remote_account_unavailable",
            " ".join(
                (
                    f"user={private_user}",
                    f"uid={private_uid}",
                    f"nss={private_nss}",
                    f"home={private_home}",
                    f"command={private_command}",
                )
            ),
        )
    )
    client = _app_client(provider)
    try:
        profile = _create_profile(client)
        response = client.post(
            f"/desktop/v2/profiles/{profile['profile_id']}/connect",
            headers=_headers(
                **{
                    "X-OpenEvo-Resource-Generation": "1",
                    "If-Match": str(profile["etag"]),
                    "Idempotency-Key": "connect-private-account-failure-01",
                }
            ),
            json={"schema_version": "2", "expected_connection_generation": 1},
        )

        assert response.status_code == 202, response.text
        operation = _wait_lifecycle_operation(client, response.json())
        expected_failure = {
            "schema_version": "2",
            "code": "ssh_remote_account_unavailable",
            "summary": "Desktop could not verify a supported writable remote account home.",
            "retryable": True,
            "action": "administrator_action",
            "affected_resource_id": profile["profile_id"],
        }
        assert operation["status"] == "failed"
        assert operation["failure"] == expected_failure

        public_profile = client.get(
            f"/desktop/v2/profiles/{profile['profile_id']}",
            headers=_headers(),
        ).json()
        state = client.get("/desktop/v2/state", headers=_headers()).json()
        stored_operation = store.get_lifecycle_operation(str(operation["operation_id"]))
        assert public_profile["failure"] == expected_failure
        assert stored_operation.failure is not None
        assert stored_operation.failure.model_dump(mode="json") == expected_failure

        http_json = json.dumps(
            [response.json(), operation, public_profile, state],
            separators=(",", ":"),
            sort_keys=True,
        )
        event_bytes = b"".join(frame.frame for frame in event_broker._events)
        persisted_bytes = b"".join(
            path.read_bytes() for path in tmp_path.rglob("*") if path.is_file()
        )
        public_bytes = http_json.encode() + event_bytes + persisted_bytes
        for canary in (
            private_user,
            private_uid,
            private_home,
            private_nss,
            "private-command-canary",
        ):
            assert canary.encode() not in public_bytes

        profile_fields = set(local_v2.RemoteWorkspaceProfileV2.model_fields)
        assert profile_fields.isdisjoint(
            {
                "remote_user",
                "remote_uid",
                "remote_home",
                "workspace_root",
                "daemon_bundle_root",
                "host_path",
            }
        )
    finally:
        client.close()
        provider.close()
        store.close()


def test_profile_disconnect_cleanup_failure_never_reports_success(
    tmp_path: Path,
) -> None:
    provider, store, lifecycle, _connector = _provider(tmp_path)
    client = _app_client(provider)
    try:
        profile = _create_profile(client)
        connected_response = client.post(
            f"/desktop/v2/profiles/{profile['profile_id']}/connect",
            headers=_headers(
                **{
                    "X-OpenEvo-Resource-Generation": "1",
                    "If-Match": str(profile["etag"]),
                    "Idempotency-Key": "connect-before-cleanup-failure-1",
                }
            ),
            json={"schema_version": "2", "expected_connection_generation": 1},
        )
        assert connected_response.status_code == 202, connected_response.text
        assert (
            _wait_lifecycle_operation(client, connected_response.json())["status"]
            == "succeeded"
        )
        connected = client.get(
            f"/desktop/v2/profiles/{profile['profile_id']}", headers=_headers()
        ).json()
        lifecycle.disconnect_errors.append(
            SystemOpenSshSessionError(
                "ssh_cleanup_failed",
                "SSH master did not stop before its deadline.",
            )
        )
        disconnect_headers = _headers(
            **{
                "X-OpenEvo-Resource-Generation": str(connected["connection_generation"]),
                "If-Match": str(connected["etag"]),
                "Idempotency-Key": "disconnect-cleanup-failure-01",
            }
        )
        disconnect_body = {
            "schema_version": "2",
            "expected_connection_generation": connected["connection_generation"],
        }

        response = client.post(
            f"/desktop/v2/profiles/{profile['profile_id']}/disconnect",
            headers=disconnect_headers,
            json=disconnect_body,
        )

        assert response.status_code == 202, response.text
        terminal = _wait_lifecycle_operation(client, response.json())
        assert terminal["status"] == "failed"
        assert terminal["failure"] == {
            "schema_version": "2",
            "code": "ssh_cleanup_failed",
            "summary": "The system OpenSSH connection could not be closed safely.",
            "retryable": True,
            "action": "retry",
            "affected_resource_id": profile["profile_id"],
        }
        unresolved = client.get(
            f"/desktop/v2/profiles/{profile['profile_id']}", headers=_headers()
        ).json()
        assert unresolved["connection_state"] == "disconnecting"
        assert lifecycle.active == (profile["profile_id"], connected["connection_generation"])

        replay = client.post(
            f"/desktop/v2/profiles/{profile['profile_id']}/disconnect",
            headers=disconnect_headers,
            json=disconnect_body,
        )
        assert replay.status_code == 202, replay.text
        assert replay.json()["operation_id"] == terminal["operation_id"]
        assert [call[0] for call in lifecycle.calls].count("disconnect") == 1
    finally:
        client.close()
        provider.close()
        store.close()


def test_profile_connect_preserves_typed_daemon_bootstrap_failure(
    tmp_path: Path,
) -> None:
    provider, store, lifecycle, connector = _provider(tmp_path)
    connector.failure = DesktopCoreBridgeErrorV2(
        504,
        local_v2.DesktopErrorV2(
            code="daemon_bootstrap_timeout",
            summary="The OpenEvo Daemon operation exceeded its deadline.",
            retryable=True,
            action="retry",
            affected_resource_id=None,
        ),
    )
    client = _app_client(provider)
    try:
        profile = _create_profile(client)
        response = client.post(
            f"/desktop/v2/profiles/{profile['profile_id']}/connect",
            headers=_headers(
                **{
                    "X-OpenEvo-Resource-Generation": str(profile["connection_generation"]),
                    "If-Match": str(profile["etag"]),
                    "Idempotency-Key": "connect-profile-daemon-timeout-01",
                }
            ),
            json={
                "schema_version": "2",
                "expected_connection_generation": profile["connection_generation"],
            },
        )

        assert response.status_code == 202, response.text
        operation = _wait_lifecycle_operation(client, response.json())
        assert operation["status"] == "failed"
        assert operation["failure"] == {
            "schema_version": "2",
            "code": "daemon_bootstrap_timeout",
            "summary": "The OpenEvo Daemon operation exceeded its deadline.",
            "retryable": True,
            "action": "retry",
            "affected_resource_id": profile["profile_id"],
        }
        failed = client.get(
            f"/desktop/v2/profiles/{profile['profile_id']}", headers=_headers()
        ).json()
        assert failed["connection_state"] == "failed"
        assert failed["failure"]["code"] == "daemon_bootstrap_timeout"
        assert lifecycle.active is None
    finally:
        client.close()
        provider.close()
        store.close()


def test_legacy_profile_rebind_requires_the_current_ssh_catalog_generation(
    tmp_path: Path,
) -> None:
    provider, store, _lifecycle, _connector = _provider(tmp_path)
    legacy = store.import_legacy_profile(
        LegacyProfileImportV2(
            source_ref_sha256="5" * 64,
            source_document_sha256="6" * 64,
            display_name="Preview GPU",
            migration_state="rebind_required",
            created_at="2026-07-23T10:00:00.000000Z",
            updated_at="2026-07-23T10:00:00.000000Z",
        )
    )
    client = _app_client(provider)
    try:
        route = f"/desktop/v2/profiles/{legacy.profile_id}/rebind"
        headers = _headers(
            **{
                "X-OpenEvo-Resource-Generation": "0",
                "If-Match": legacy.etag,
                "Idempotency-Key": "rebind-current-catalog-key-01",
            }
        )
        stale = client.post(
            route,
            headers=headers,
            json={
                "schema_version": "2",
                "connection_authority": "system_openssh",
                "ssh_host_alias": "gpu-lab",
                "catalog_generation": 0,
            },
        )
        assert stale.status_code == 412, stale.text
        assert stale.json()["code"] == "ssh_catalog_generation_changed"
        assert store.list_profiles() == (legacy,)

        current_headers = dict(headers)
        current_headers["Idempotency-Key"] = "rebind-current-catalog-key-02"
        current = client.post(
            route,
            headers=current_headers,
            json={
                "schema_version": "2",
                "connection_authority": "system_openssh",
                "ssh_host_alias": "gpu-lab",
                "catalog_generation": 1,
            },
        )
        assert current.status_code == 201, current.text
        assert current.json()["ssh_host_alias"] == "gpu-lab"
        assert current.json()["catalog_generation"] == 1
    finally:
        client.close()
        provider.close()
        store.close()


def test_profile_connect_failure_racing_cancellation_does_not_stop_worker(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider, store, lifecycle, _connector = _provider(tmp_path)
    lifecycle.connect_errors.append(
        SystemOpenSshSessionError(
            "ssh_connection_failed",
            "simulated connection failure",
        )
    )
    failure_entered = Event()
    release_failure = Event()
    original_fail = provider._fail_profile_connect

    def fail_after_cancellation(
        profile: local_v2.RemoteWorkspaceProfileV2,
        failure_code: str,
        *,
        abort_transport: bool = False,
    ):
        if not failure_entered.is_set():
            failure_entered.set()
            assert release_failure.wait(2)
        return original_fail(
            profile,
            failure_code,
            abort_transport=abort_transport,
        )

    monkeypatch.setattr(provider, "_fail_profile_connect", fail_after_cancellation)
    client = _app_client(provider)
    try:
        profile = _create_profile(client)
        started = client.post(
            f"/desktop/v2/profiles/{profile['profile_id']}/connect",
            headers=_headers(
                **{
                    "X-OpenEvo-Resource-Generation": "1",
                    "If-Match": str(profile["etag"]),
                    "Idempotency-Key": "cancel-racing-failure-connect-1",
                }
            ),
            json={"schema_version": "2", "expected_connection_generation": 1},
        )
        assert started.status_code == 202, started.text
        assert failure_entered.wait(2)
        operation_id = started.json()["operation_id"]
        running = client.get(
            f"/desktop/v2/operations/{operation_id}",
            headers=_headers(),
        ).json()
        assert running["status"] == "running"
        cancelled = client.post(
            f"/desktop/v2/operations/{operation_id}/cancel",
            headers=_headers(
                **{
                    "X-OpenEvo-Resource-Generation": "0",
                    "If-Match": running["etag"],
                    "Idempotency-Key": "cancel-racing-failure-operation-1",
                }
            ),
            json={"schema_version": "2", "expected_operation_id": operation_id},
        )
        assert cancelled.status_code == 202, cancelled.text
        release_failure.set()
        terminal = _wait_lifecycle_operation(client, started.json())
        assert terminal["status"] == "cancelled"

        after_cancel = client.get(
            f"/desktop/v2/profiles/{profile['profile_id']}",
            headers=_headers(),
        ).json()
        assert after_cancel["connection_generation"] == 2
        assert after_cancel["connection_state"] == "disconnected"
        assert after_cancel["failure"] is None

        reconnected = client.post(
            f"/desktop/v2/profiles/{profile['profile_id']}/connect",
            headers=_headers(
                **{
                    "X-OpenEvo-Resource-Generation": "2",
                    "If-Match": str(after_cancel["etag"]),
                    "Idempotency-Key": "connect-after-racing-cancellation-1",
                }
            ),
            json={"schema_version": "2", "expected_connection_generation": 2},
        )
        assert reconnected.status_code == 202, reconnected.text
        assert _wait_lifecycle_operation(client, reconnected.json())["status"] == "succeeded"
        connected = client.get(
            f"/desktop/v2/profiles/{profile['profile_id']}",
            headers=_headers(),
        ).json()
        assert connected["connection_generation"] == 3
        assert connected["connection_state"] == "connected"
    finally:
        release_failure.set()
        client.close()
        provider.close()
        store.close()


def test_profile_retry_cannot_overtake_cancelled_failure_terminal(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider, store, lifecycle, _connector = _provider(tmp_path)
    lifecycle.connect_errors.append(
        SystemOpenSshSessionError(
            "ssh_connection_failed",
            "simulated connection failure",
        )
    )
    failure_published = Event()
    release_terminal = Event()
    original_fail = provider._fail_profile_connect

    def fail_before_terminal(
        profile: local_v2.RemoteWorkspaceProfileV2,
        failure_code: str,
        *,
        abort_transport: bool = False,
    ):
        failure = original_fail(
            profile,
            failure_code,
            abort_transport=abort_transport,
        )
        if not failure_published.is_set():
            failure_published.set()
            assert release_terminal.wait(2)
        return failure

    monkeypatch.setattr(provider, "_fail_profile_connect", fail_before_terminal)
    client = _app_client(provider)
    try:
        profile = _create_profile(client)
        started = client.post(
            f"/desktop/v2/profiles/{profile['profile_id']}/connect",
            headers=_headers(
                **{
                    "X-OpenEvo-Resource-Generation": "1",
                    "If-Match": str(profile["etag"]),
                    "Idempotency-Key": "failure-before-cancel-connect-1",
                }
            ),
            json={"schema_version": "2", "expected_connection_generation": 1},
        )
        assert started.status_code == 202, started.text
        assert failure_published.wait(2)
        operation_id = started.json()["operation_id"]
        running = client.get(
            f"/desktop/v2/operations/{operation_id}",
            headers=_headers(),
        ).json()
        failed_profile = client.get(
            f"/desktop/v2/profiles/{profile['profile_id']}",
            headers=_headers(),
        ).json()
        assert running["status"] == "running"
        assert failed_profile["connection_generation"] == 2
        assert failed_profile["connection_state"] == "failed"

        cancelled = client.post(
            f"/desktop/v2/operations/{operation_id}/cancel",
            headers=_headers(
                **{
                    "X-OpenEvo-Resource-Generation": "0",
                    "If-Match": running["etag"],
                    "Idempotency-Key": "cancel-after-published-failure-1",
                }
            ),
            json={"schema_version": "2", "expected_operation_id": operation_id},
        )
        assert cancelled.status_code == 202, cancelled.text

        overtaking = client.post(
            f"/desktop/v2/profiles/{profile['profile_id']}/connect",
            headers=_headers(
                **{
                    "X-OpenEvo-Resource-Generation": "2",
                    "If-Match": str(failed_profile["etag"]),
                    "Idempotency-Key": "overtaking-profile-connect-1",
                }
            ),
            json={"schema_version": "2", "expected_connection_generation": 2},
        )
        assert overtaking.status_code == 409, overtaking.text
        assert overtaking.json() == {
            "schema_version": "2",
            "code": "lifecycle_operation_in_progress",
            "summary": "Another lifecycle operation still owns this local resource.",
            "retryable": True,
            "action": "retry",
            "affected_resource_id": profile["profile_id"],
        }

        release_terminal.set()
        assert _wait_lifecycle_operation(client, started.json())["status"] == "cancelled"
        after_cancel = client.get(
            f"/desktop/v2/profiles/{profile['profile_id']}",
            headers=_headers(),
        ).json()
        reconnected = client.post(
            f"/desktop/v2/profiles/{profile['profile_id']}/connect",
            headers=_headers(
                **{
                    "X-OpenEvo-Resource-Generation": "2",
                    "If-Match": str(after_cancel["etag"]),
                    "Idempotency-Key": "connect-after-overtaking-rejected-1",
                }
            ),
            json={"schema_version": "2", "expected_connection_generation": 2},
        )
        assert reconnected.status_code == 202, reconnected.text
        assert _wait_lifecycle_operation(client, reconnected.json())["status"] == "succeeded"
    finally:
        release_terminal.set()
        client.close()
        provider.close()
        store.close()


def test_profile_connect_retry_is_durable_and_does_not_repeat_ssh(tmp_path: Path) -> None:
    provider, store, lifecycle, _connector = _provider(tmp_path)
    client = _app_client(provider)
    try:
        profile = _create_profile(client)
        headers = _headers(
            **{
                "X-OpenEvo-Resource-Generation": "1",
                "If-Match": str(profile["etag"]),
                "Idempotency-Key": "connect-profile-replay-01",
            }
        )
        body = {
            "schema_version": "2",
            "expected_connection_generation": 1,
        }
        first = client.post(
            f"/desktop/v2/profiles/{profile['profile_id']}/connect",
            headers=headers,
            json=body,
        )
        second = client.post(
            f"/desktop/v2/profiles/{profile['profile_id']}/connect",
            headers=headers,
            json=body,
        )
        assert first.status_code == second.status_code == 202
        assert first.json()["operation_id"] == second.json()["operation_id"]
        assert _wait_lifecycle_operation(client, first.json())["status"] == "succeeded"
        assert lifecycle.calls == [("connect", "gpu-lab", 2)]
    finally:
        client.close()
        provider.close()
        store.close()


def test_old_connect_replay_after_a_new_generation_never_reopens_ssh(
    tmp_path: Path,
) -> None:
    provider, store, lifecycle, _connector = _provider(tmp_path)
    client = _app_client(provider)
    try:
        profile = _create_profile(client)
        connect_headers = _headers(
            **{
                "X-OpenEvo-Resource-Generation": "1",
                "If-Match": str(profile["etag"]),
                "Idempotency-Key": "connect-old-generation-key-1",
            }
        )
        connect_body = {
            "schema_version": "2",
            "expected_connection_generation": 1,
        }
        connected_response = client.post(
            f"/desktop/v2/profiles/{profile['profile_id']}/connect",
            headers=connect_headers,
            json=connect_body,
        )
        assert connected_response.status_code == 202, connected_response.text
        assert (
            _wait_lifecycle_operation(client, connected_response.json())["status"] == "succeeded"
        )
        connected = client.get(
            f"/desktop/v2/profiles/{profile['profile_id']}", headers=_headers()
        ).json()
        disconnected_response = client.post(
            f"/desktop/v2/profiles/{profile['profile_id']}/disconnect",
            headers=_headers(
                **{
                    "X-OpenEvo-Resource-Generation": "2",
                    "If-Match": str(connected["etag"]),
                    "Idempotency-Key": "disconnect-new-generation-key-1",
                }
            ),
            json={
                "schema_version": "2",
                "expected_connection_generation": 2,
            },
        )
        assert disconnected_response.status_code == 202, disconnected_response.text
        assert (
            _wait_lifecycle_operation(client, disconnected_response.json())["status"]
            == "succeeded"
        )
        calls_before_replay = list(lifecycle.calls)

        stale_replay = client.post(
            f"/desktop/v2/profiles/{profile['profile_id']}/connect",
            headers=connect_headers,
            json=connect_body,
        )
        assert stale_replay.status_code == 202, stale_replay.text
        assert stale_replay.json()["operation_id"] == connected_response.json()["operation_id"]
        assert lifecycle.calls == calls_before_replay
    finally:
        client.close()
        provider.close()
        store.close()


def test_concurrent_exact_connect_retry_has_one_system_ssh_owner(
    tmp_path: Path,
) -> None:
    provider, store, lifecycle, connector = _provider(tmp_path)
    created = store.create_system_profile(
        local_v2.SystemOpenSshProfileCreateV2(
            display_name="Concurrent GPU",
            ssh_host_alias="gpu-lab",
        ),
        catalog_generation=1,
        idempotency_key="concurrent-create-profile-key-1",
    )
    lifecycle.connect_started = Event()
    lifecycle.connect_release = Event()
    lifecycle.second_connect_started = Event()
    results: list[object] = []
    failures: list[BaseException] = []
    arguments = {
        "profile_id": created.profile_id,
        "request": local_v2.ProfileConnectionActionV2(
            expected_connection_generation=created.connection_generation
        ),
        "resource_generation": created.connection_generation,
        "if_match": created.etag,
        "idempotency_key": "concurrent-connect-profile-key-1",
    }

    def connect() -> None:
        try:
            results.append(provider.invoke("connectRemoteWorkspaceProfileV2", arguments))
        except BaseException as exc:
            failures.append(exc)

    first = Thread(target=connect)
    second = Thread(target=connect)
    first.start()
    try:
        assert lifecycle.connect_started.wait(2)
        second.start()
        assert not lifecycle.second_connect_started.wait(0.2)
    finally:
        lifecycle.connect_release.set()
        first.join(2)
        second.join(2)
        assert len(results) == 2
        operation_id = results[0].operation_id
        assert results[1].operation_id == operation_id
        assert _wait_store_lifecycle_operation(store, operation_id).status == "succeeded"
        provider.close()
        store.close()

    assert not first.is_alive()
    assert not second.is_alive()
    assert failures == []
    assert len(results) == 2
    assert lifecycle.calls == [("connect", "gpu-lab", 2)]
    assert connector.calls == [(created.profile_id, 2)]


def test_native_askpass_prompt_is_visible_while_system_ssh_owns_the_connect(
    tmp_path: Path,
) -> None:
    provider, store, lifecycle, _connector = _provider(tmp_path)
    created = store.create_system_profile(
        local_v2.SystemOpenSshProfileCreateV2(
            display_name="Prompt GPU",
            ssh_host_alias="gpu-lab",
        ),
        catalog_generation=1,
        idempotency_key="prompt-visible-create-key-01",
    )
    lifecycle.prompt_started = Event()
    lifecycle.prompt_release = Event()
    failures: list[BaseException] = []

    def connect() -> None:
        try:
            provider.invoke(
                "connectRemoteWorkspaceProfileV2",
                {
                    "profile_id": created.profile_id,
                    "request": local_v2.ProfileConnectionActionV2(
                        expected_connection_generation=created.connection_generation
                    ),
                    "resource_generation": created.connection_generation,
                    "if_match": created.etag,
                    "idempotency_key": "prompt-visible-connect-key-1",
                },
            )
        except BaseException as exc:
            failures.append(exc)

    thread = Thread(target=connect)
    thread.start()
    try:
        assert lifecycle.prompt_started.wait(2)
        pending = store.get_profile(created.profile_id)
        assert pending.connection_state == "prompt_pending"
        assert pending.prompt is not None
        assert pending.prompt.kind == "passphrase"
        assert pending.prompt.state == "pending"
    finally:
        lifecycle.prompt_release.set()
        thread.join(2)
        provider.close()
        store.close()
    assert not thread.is_alive()
    assert failures == []


def test_changed_system_host_key_is_reviewed_then_reconnected_exactly(
    tmp_path: Path,
) -> None:
    provider, store, lifecycle, connector = _provider(tmp_path)
    client = _app_client(provider)
    try:
        profile = _create_profile(client)
        review = _changed_key_review(str(profile["profile_id"]), 2)
        lifecycle.connect_errors.append(
            SystemOpenSshSessionError(
                "ssh_host_key_changed",
                "The configured server identity changed and requires review.",
                host_key_review=review,
            )
        )
        failed = client.post(
            f"/desktop/v2/profiles/{profile['profile_id']}/connect",
            headers=_headers(
                **{
                    "X-OpenEvo-Resource-Generation": "1",
                    "If-Match": str(profile["etag"]),
                    "Idempotency-Key": "connect-host-review-key-01",
                }
            ),
            json={"schema_version": "2", "expected_connection_generation": 1},
        )
        assert failed.status_code == 202, failed.text
        failed_operation = _wait_lifecycle_operation(client, failed.json())
        assert failed_operation["status"] == "failed"
        assert failed_operation["failure"] == {
            "schema_version": "2",
            "code": "ssh_host_key_changed",
            "summary": "The configured server identity changed and requires review.",
            "retryable": False,
            "action": "review_host_key",
            "affected_resource_id": profile["profile_id"],
        }
        pending = client.get(
            f"/desktop/v2/profiles/{profile['profile_id']}", headers=_headers()
        ).json()
        assert pending["connection_state"] == "host_key_review"
        assert pending["connection_generation"] == 2
        assert pending["trust"] == {
            "schema_version": "2",
            "connection_generation": 2,
            "state": "changed_key_blocked",
            "review_id": review.review_id,
            "review_sha256": review.review_sha256,
            "key_fingerprints": [
                {
                    "schema_version": "2",
                    "algorithm": "ssh-ed25519",
                    "sha256_fingerprint": "SHA256:" + ("A" * 43),
                    "role": "presented",
                }
            ],
            "repair_support": "automatic_replacement_available",
        }
        assert connector.calls == []

        mismatched = client.post(
            f"/desktop/v2/profiles/{profile['profile_id']}/host-key/review",
            headers=_headers(
                **{
                    "X-OpenEvo-Resource-Generation": "2",
                    "If-Match": str(pending["etag"]),
                    "Idempotency-Key": "mismatched-host-review-key-01",
                }
            ),
            json={
                "schema_version": "2",
                "expected_connection_generation": 2,
                "review_id": review.review_id,
                "review_sha256": "8" * 64,
                "action": "replace_changed_key",
            },
        )
        assert mismatched.status_code == 409, mismatched.text
        assert mismatched.json()["code"] == "local_resource_conflict"
        assert lifecycle.calls == [("connect", "gpu-lab", 2)]

        replay = client.post(
            f"/desktop/v2/profiles/{profile['profile_id']}/connect",
            headers=_headers(
                **{
                    "X-OpenEvo-Resource-Generation": "1",
                    "If-Match": str(profile["etag"]),
                    "Idempotency-Key": "connect-host-review-key-01",
                }
            ),
            json={"schema_version": "2", "expected_connection_generation": 1},
        )
        assert replay.status_code == 202
        assert replay.json()["operation_id"] == failed_operation["operation_id"]
        assert lifecycle.calls == [("connect", "gpu-lab", 2)]
        assert connector.calls == []

        accepted = client.post(
            f"/desktop/v2/profiles/{profile['profile_id']}/host-key/review",
            headers=_headers(
                **{
                    "X-OpenEvo-Resource-Generation": "2",
                    "If-Match": str(pending["etag"]),
                    "Idempotency-Key": "replace-host-review-key-01",
                }
            ),
            json={
                "schema_version": "2",
                "expected_connection_generation": 2,
                "review_id": review.review_id,
                "review_sha256": review.review_sha256,
                "action": "replace_changed_key",
            },
        )
        assert accepted.status_code == 202, accepted.text
        assert _wait_lifecycle_operation(client, accepted.json())["status"] == "succeeded"
        connected = client.get(
            f"/desktop/v2/profiles/{profile['profile_id']}", headers=_headers()
        ).json()
        assert connected["connection_state"] == "connected"
        assert connected["connection_generation"] == 3
        assert lifecycle.calls == [
            ("connect", "gpu-lab", 2),
            ("review", "gpu-lab", 3),
        ]
        assert len(lifecycle.review_cancel_events) == 1
        assert isinstance(lifecycle.review_cancel_events[0], Event)
        assert connector.calls == [(profile["profile_id"], 3)]
    finally:
        client.close()
        provider.close()
        store.close()


def test_changed_system_host_key_rejection_is_terminal_and_exactly_replayable(
    tmp_path: Path,
) -> None:
    provider, store, lifecycle, connector = _provider(tmp_path)
    client = _app_client(provider)
    try:
        profile = _create_profile(client)
        review = _changed_key_review(str(profile["profile_id"]), 2)
        lifecycle.connect_errors.append(
            SystemOpenSshSessionError(
                "ssh_host_key_changed",
                "The configured server identity changed and requires review.",
                host_key_review=review,
            )
        )
        blocked = client.post(
            f"/desktop/v2/profiles/{profile['profile_id']}/connect",
            headers=_headers(
                **{
                    "X-OpenEvo-Resource-Generation": "1",
                    "If-Match": str(profile["etag"]),
                    "Idempotency-Key": "reject-host-connect-key-01",
                }
            ),
            json={"schema_version": "2", "expected_connection_generation": 1},
        )
        assert blocked.status_code == 202
        blocked_operation = _wait_lifecycle_operation(client, blocked.json())
        assert blocked_operation["status"] == "failed"
        pending = client.get(
            f"/desktop/v2/profiles/{profile['profile_id']}", headers=_headers()
        ).json()
        lifecycle.review_outcome = "rejected"
        headers = _headers(
            **{
                "X-OpenEvo-Resource-Generation": "2",
                "If-Match": str(pending["etag"]),
                "Idempotency-Key": "reject-host-review-key-01",
            }
        )
        body = {
            "schema_version": "2",
            "expected_connection_generation": 2,
            "review_id": review.review_id,
            "review_sha256": review.review_sha256,
            "action": "reject",
        }

        rejected = client.post(
            f"/desktop/v2/profiles/{profile['profile_id']}/host-key/review",
            headers=headers,
            json=body,
        )
        replay = client.post(
            f"/desktop/v2/profiles/{profile['profile_id']}/host-key/review",
            headers=headers,
            json=body,
        )

        assert rejected.status_code == replay.status_code == 202
        assert rejected.json()["operation_id"] == replay.json()["operation_id"]
        assert _wait_lifecycle_operation(client, rejected.json())["status"] == "succeeded"
        terminal = client.get(
            f"/desktop/v2/profiles/{profile['profile_id']}", headers=_headers()
        ).json()
        assert terminal["connection_state"] == "disconnected"
        assert terminal["trust"]["state"] == "rejected"
        assert lifecycle.calls == [
            ("connect", "gpu-lab", 2),
            ("review", "gpu-lab", 3),
        ]
        assert connector.calls == []
    finally:
        client.close()
        provider.close()
        store.close()


def test_incompatible_daemon_fails_closed_and_exact_retry_never_reopens_ssh(
    tmp_path: Path,
) -> None:
    provider, store, lifecycle, connector = _provider(tmp_path)
    connector.version = connector.version.model_copy(update={"source_commit": "f" * 40})
    client = _app_client(provider)
    try:
        profile = _create_profile(client)
        headers = _headers(
            **{
                "X-OpenEvo-Resource-Generation": "1",
                "If-Match": str(profile["etag"]),
                "Idempotency-Key": "incompatible-core-connect-key",
            }
        )
        body = {"schema_version": "2", "expected_connection_generation": 1}

        failed = client.post(
            f"/desktop/v2/profiles/{profile['profile_id']}/connect",
            headers=headers,
            json=body,
        )
        replay = client.post(
            f"/desktop/v2/profiles/{profile['profile_id']}/connect",
            headers=headers,
            json=body,
        )

        assert failed.status_code == replay.status_code == 202
        terminal = _wait_lifecycle_operation(client, failed.json())
        assert terminal["status"] == "failed"
        assert replay.json()["operation_id"] == terminal["operation_id"]
        assert terminal["failure"]["code"] == "core_release_incompatible"
        current = client.get(
            f"/desktop/v2/profiles/{profile['profile_id']}", headers=_headers()
        ).json()
        assert current["connection_state"] == "failed"
        assert lifecycle.calls == [
            ("connect", "gpu-lab", 2),
            ("disconnect", profile["profile_id"], 3),
        ]
        assert connector.calls == [(profile["profile_id"], 2)]
    finally:
        client.close()
        provider.close()
        store.close()
