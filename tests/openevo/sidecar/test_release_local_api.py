from __future__ import annotations

from datetime import datetime, timedelta, timezone
import hashlib
import hmac
from pathlib import Path
import sqlite3
from threading import Event, Lock, Thread
from typing import Any, cast

from fastapi.testclient import TestClient
import pytest

from desktop.sidecar.contracts.v1 import (
    DESKTOP_OPENAPI_SHA256,
    canonical_sha256,
    contract_app,
    create_contract_app,
)
from desktop.sidecar.provider_store import DesktopProviderStore, ProviderDataCorruptionError
import desktop.sidecar.release_app as release_app_module
from desktop.sidecar.release_app import create_release_desktop_local_api_app
from desktop.sidecar.remote_lifecycle import (
    DesktopRemoteLifecycle,
    RemoteConnectionFailedError,
    RemoteConnectionResult,
    RemoteLifecycleSnapshot,
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


def _app(
    state_root: Path,
    *,
    clock: MutableClock | None = None,
    remote_lifecycle: FakeRemoteLifecycle | None = None,
):
    return create_release_desktop_local_api_app(
        state_root=state_root,
        session_token=SESSION_TOKEN,
        instance_id=INSTANCE_ID,
        readiness_key=READINESS_KEY,
        source_commit=SOURCE_COMMIT,
        build_channel="test",
        clock=clock,
        remote_lifecycle=cast(DesktopRemoteLifecycle | None, remote_lifecycle),
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
            "codex_model": "gpt-5",
        },
        "evolution": {"targets": {}},
    }


def _create_profile(client: TestClient, *, name: str, key: str):
    return client.post(
        "/desktop/v1/profiles",
        headers={**SESSION_HEADERS, "Idempotency-Key": key},
        json=_profile(name),
    )


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
            "build_version": "0.1.0",
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
        assert all(expected == SESSION_TOKEN.encode("utf-8") for _, expected in compared_values)
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
        replay = client.post(
            f"/desktop/v1/profiles/{profile_id}/connect", headers=connect_headers
        )
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


def test_late_failure_cannot_overwrite_newer_profile_connection(tmp_path: Path) -> None:
    lifecycle = RacingRemoteLifecycle()
    app = _app(tmp_path / "state", remote_lifecycle=lifecycle)
    with TestClient(app) as client:
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
            assert lifecycle.second_finished.wait(2)
        finally:
            lifecycle.release_first.set()
        first_thread.join(5)
        second_thread.join(5)

        assert responses["A"].status_code == 503
        assert responses["B"].status_code == 202
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


def test_reserved_action_survives_idempotency_capacity_filling_during_ssh(
    tmp_path: Path,
) -> None:
    lifecycle = BlockingRemoteLifecycle()
    app = _app(tmp_path / "state", remote_lifecycle=lifecycle)
    with TestClient(app) as client:
        profile = _create_profile(
            client, name="Capacity owner", key="profile-capacity-create-0001"
        ).json()
        store = app.state.desktop_release_provider._store
        store._max_idempotency_records = 2
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


@pytest.mark.parametrize("failure_timing", ["before_commit", "after_commit"])
def test_successful_ssh_is_closed_and_failed_action_is_replayed_when_commit_fails(
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

        failed = client.post(
            f"/desktop/v1/profiles/{profile['profile_id']}/connect", headers=headers
        )
        replay = client.post(
            f"/desktop/v1/profiles/{profile['profile_id']}/connect", headers=headers
        )

        assert failed.status_code == replay.status_code == 503
        assert failed.content == replay.content
        assert failed.json()["code"] == "local_provider_unavailable"
        assert lifecycle.connect_calls == 1
        assert lifecycle.disconnect_calls == 1
        stored = client.get(
            f"/desktop/v1/profiles/{profile['profile_id']}", headers=SESSION_HEADERS
        ).json()
        assert stored["connection_state"] == "disconnected"


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
