from __future__ import annotations

from datetime import datetime, timedelta, timezone
import hashlib
import hmac
from pathlib import Path

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


def _app(state_root: Path, *, clock: MutableClock | None = None):
    return create_release_desktop_local_api_app(
        state_root=state_root,
        session_token=SESSION_TOKEN,
        instance_id=INSTANCE_ID,
        readiness_key=READINESS_KEY,
        source_commit=SOURCE_COMMIT,
        build_channel="test",
        clock=clock,
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
