from __future__ import annotations

import base64
import asyncio
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path

from fastapi.testclient import TestClient
import pytest

from openevo.backend.contracts.v1 import (
    create_core_control_app,
    openapi_sha256,
)
from openevo.backend.contracts.v1.models import SseFrameV1, WorkspaceArchiveDeclarationV1
from openevo.backend.contracts.v1.workspace import (
    WorkspaceArchiveError,
    verify_workspace_archive,
)
from tests.framework_testkit import verified_builtin_registry


TOKEN = "core-provider-test-token"
AUTH = {"Authorization": f"Bearer {TOKEN}"}


def _timestamp() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _archive_policy() -> dict[str, object]:
    return {
        "media_type": "application/vnd.openevo.workspace-tar",
        "tar_format": "posix_ustar",
        "entry_types": "regular_files_and_directories",
        "path_policy": "utf8_nfc_posix_relative_ustar_split_v1",
        "entry_order": "header_path_byte_lexicographic_parents_first",
        "metadata_policy": "uid_gid_zero_names_empty_mtime_zero",
        "header_policy": "posix_ustar_canonical_header_v1",
        "body_policy": "zero_pad_to_512_bytes",
        "terminator_policy": "two_zero_blocks_no_trailing_bytes",
        "file_mode_policy": "0644_or_0755",
        "directory_mode": "0755",
        "allow_symlinks": False,
        "allow_hardlinks": False,
        "allow_devices": False,
        "allow_fifos": False,
        "allow_sparse_files": False,
        "allow_tar_extensions": False,
        "max_entries": 100_000,
        "max_path_depth": 32,
        "max_path_bytes": 256,
        "max_file_bytes": 0o77777777777,
        "max_extracted_bytes": 16 * 1024 * 1024 * 1024,
    }


def _tar_header(path: str, *, body_size: int, directory: bool = False) -> bytes:
    encoded = (path + "/" if directory else path).encode("utf-8")
    if len(encoded) <= 100:
        name, prefix = encoded, b""
    else:
        split = max(
            index
            for index, value in enumerate(encoded)
            if value == ord("/") and index <= 155 and len(encoded) - index - 1 <= 100
        )
        prefix, name = encoded[:split], encoded[split + 1 :]
    header = bytearray(512)
    header[0 : len(name)] = name
    header[100:108] = b"0000755\0" if directory else b"0000644\0"
    header[108:116] = b"0000000\0"
    header[116:124] = b"0000000\0"
    header[124:136] = f"{body_size:011o}\0".encode("ascii")
    header[136:148] = b"00000000000\0"
    header[148:156] = b"        "
    header[156:157] = b"5" if directory else b"0"
    header[257:263] = b"ustar\0"
    header[263:265] = b"00"
    header[329:337] = b"0000000\0"
    header[337:345] = b"0000000\0"
    header[345 : 345 + len(prefix)] = prefix
    checksum = sum(header)
    header[148:156] = f"{checksum:06o}\0 ".encode("ascii")
    return bytes(header)


def _workspace_archive() -> bytes:
    body = b"OpenEvo provider workspace\n"
    padding = b"\0" * ((512 - len(body) % 512) % 512)
    return b"".join(
        (
            _tar_header("src", body_size=0, directory=True),
            _tar_header("src/AGENTS.md", body_size=len(body)),
            body,
            padding,
            b"\0" * 1024,
        )
    )


def _project_create(*, archive: bytes | None = None) -> dict[str, object]:
    workspace: dict[str, object]
    if archive is None:
        workspace = {"kind": "scratch", "display_name": "Scratch workspace"}
    else:
        workspace = {
            "kind": "native_folder_snapshot",
            "display_name": "Imported workspace",
            "archive": {
                "content_sha256": hashlib.sha256(archive).hexdigest(),
                "byte_size": len(archive),
                "format": "openevo_deterministic_tar_v1",
                "entry_count": 2,
                "extracted_byte_size": len(b"OpenEvo provider workspace\n"),
                "policy": _archive_policy(),
            },
        }
    return {
        "schema_version": "1",
        "name": "Protein memory",
        "description": "Provider conformance project.",
        "spec": {
            "execution_mode": "codex_subscription_transcript",
            "capture_mode": "transcript",
            "harness_id": "codex",
            "agent_model_ref": "gpt-5.1-codex-mini",
            "evolution": {"targets": {}},
        },
        "task": {"title": "Improve folding", "objective": "Improve the folding baseline."},
        "workspace": workspace,
    }


def _app(
    state_root: Path,
    *,
    registry=None,
    event_replay_limit: int = 10_000,
):
    return create_core_control_app(
        state_root=state_root,
        bearer_token=TOKEN,
        build_version="0.1.0",
        source_commit="a" * 40,
        build_channel="test",
        evolution_registry=registry,
        event_replay_limit=event_replay_limit,
    )


def _create_project(client: TestClient, payload: dict[str, object]) -> tuple[dict, str]:
    response = client.post(
        "/v1/projects",
        headers={**AUTH, "Idempotency-Key": "create-project-0001"},
        json=payload,
    )
    assert response.status_code == 201, response.text
    return response.json(), response.headers["etag"]


def test_provider_preserves_frozen_openapi_and_negotiates_v1(tmp_path: Path) -> None:
    with TestClient(_app(tmp_path)) as client:
        version = client.get("/version")
        assert version.status_code == 200
        assert version.json() == {
            "schema_version": "1",
            "preferred_major": 1,
            "supported_majors": [1],
            "openapi_sha256": openapi_sha256(),
            "build_version": "0.1.0",
            "source_commit": "a" * 40,
            "build_channel": "test",
            "provider_kind": "openevo_core",
            "features": [
                "projects",
                "workspace_sync",
                "verified_capabilities",
                "transcript_capture",
                "non_parametric_evolution",
                "sse_replay",
            ],
        }
        assert client.app.openapi() == json.loads(
            Path("src/openevo/backend/contracts/v1/openapi.json").read_text(encoding="utf-8")
        )
        unsupported = client.get("/v2/status", headers=AUTH)
        assert unsupported.status_code == 426
        assert unsupported.json()["code"] == "contract_version_unsupported"


@pytest.mark.parametrize(
    "headers",
    [
        {},
        {"Authorization": TOKEN},
        {"Authorization": f"Basic {TOKEN}"},
        {"Authorization": "Bearer wrong"},
        {"Authorization": f"Bearer  {TOKEN}"},
    ],
)
def test_every_v1_route_requires_exact_bearer(tmp_path: Path, headers: dict[str, str]) -> None:
    with TestClient(_app(tmp_path)) as client:
        response = client.get("/v1/status", headers=headers)
        assert response.status_code == 401
        assert response.json()["code"] == "core_bearer_invalid"
        assert response.headers["www-authenticate"] == "Bearer"


def test_duplicate_authorization_headers_are_rejected(tmp_path: Path) -> None:
    with TestClient(_app(tmp_path)) as client:
        response = client.get(
            "/v1/status",
            headers=[
                ("Authorization", f"Bearer {TOKEN}"),
                ("Authorization", f"Bearer {TOKEN}"),
            ],
        )
        assert response.status_code == 401
        assert response.json()["code"] == "core_bearer_invalid"


def test_project_crud_etag_idempotency_and_restart_recovery(tmp_path: Path) -> None:
    payload = _project_create()
    with TestClient(_app(tmp_path)) as client:
        project, etag = _create_project(client, payload)
        replay = client.post(
            "/v1/projects",
            headers={**AUTH, "Idempotency-Key": "create-project-0001"},
            json=payload,
        )
        assert replay.status_code == 201
        assert replay.json() == project
        conflict = client.post(
            "/v1/projects",
            headers={**AUTH, "Idempotency-Key": "create-project-0001"},
            json={**payload, "name": "Different"},
        )
        assert conflict.status_code == 409
        assert conflict.json()["code"] == "idempotency_key_reused"

        stale = client.patch(
            f"/v1/projects/{project['id']}",
            headers={
                **AUTH,
                "Idempotency-Key": "patch-project-stale",
                "If-Match": '"' + "0" * 64 + '"',
            },
            json={"schema_version": "1", "name": "Renamed"},
        )
        assert stale.status_code == 412
        assert stale.json()["code"] == "etag_precondition_failed"
        stale_replay = client.patch(
            f"/v1/projects/{project['id']}",
            headers={
                **AUTH,
                "Idempotency-Key": "patch-project-stale",
                "If-Match": '"' + "0" * 64 + '"',
            },
            json={"schema_version": "1", "name": "Renamed"},
        )
        assert stale_replay.status_code == 412
        assert stale_replay.json() == stale.json()
        stale_key_conflict = client.patch(
            f"/v1/projects/{project['id']}",
            headers={
                **AUTH,
                "Idempotency-Key": "patch-project-stale",
                "If-Match": etag,
            },
            json={"schema_version": "1", "name": "Different reuse"},
        )
        assert stale_key_conflict.status_code == 409
        assert stale_key_conflict.json()["code"] == "idempotency_key_reused"

        patched = client.patch(
            f"/v1/projects/{project['id']}",
            headers={
                **AUTH,
                "Idempotency-Key": "patch-project-0001",
                "If-Match": etag,
            },
            json={"schema_version": "1", "name": "Renamed"},
        )
        assert patched.status_code == 200
        assert patched.json()["name"] == "Renamed"
        assert patched.headers["etag"] != etag
        patched_project = patched.json()

    with TestClient(_app(tmp_path)) as restarted:
        recovered = restarted.get(f"/v1/projects/{project['id']}", headers=AUTH)
        assert recovered.status_code == 200
        assert recovered.json() == patched_project
        assert recovered.headers["etag"] == patched_project["etag"]
        page = restarted.get("/v1/projects", headers=AUTH, params={"limit": 1})
        assert page.json()["items"] == [
            {
                key: value
                for key, value in patched_project.items()
                if key not in {"spec", "task", "workspace"}
            }
        ]
        failed_replay = restarted.patch(
            f"/v1/projects/{project['id']}",
            headers={
                **AUTH,
                "Idempotency-Key": "patch-project-stale",
                "If-Match": '"' + "0" * 64 + '"',
            },
            json={"schema_version": "1", "name": "Renamed"},
        )
        assert failed_replay.status_code == 412
        assert failed_replay.json() == stale.json()


def test_workspace_upload_is_ordered_bounded_idempotent_and_recoverable(tmp_path: Path) -> None:
    archive = _workspace_archive()
    payload = _project_create(archive=archive)
    with TestClient(_app(tmp_path)) as client:
        project, project_etag = _create_project(client, payload)
        begin_body = {
            "schema_version": "1",
            "project_snapshot": project["current_project_snapshot"],
            "archive": payload["workspace"]["archive"],
            "base_workspace_snapshot": None,
        }
        begin = client.post(
            f"/v1/projects/{project['id']}/workspace-uploads",
            headers={
                **AUTH,
                "Idempotency-Key": "begin-upload-0001",
                "If-Match": project_etag,
            },
            json=begin_body,
        )
        assert begin.status_code == 201, begin.text
        upload = begin.json()
        upload_etag = begin.headers["etag"]

        out_of_order = client.put(
            f"/v1/projects/{project['id']}/workspace-uploads/{upload['id']}/chunk",
            headers={
                **AUTH,
                "Idempotency-Key": "chunk-wrong-offset",
                "If-Match": upload_etag,
            },
            json=_chunk(archive, offset=512),
        )
        assert out_of_order.status_code == 409
        assert out_of_order.json()["code"] == "workspace_chunk_out_of_order"

        first_bytes = archive[:1024]
        first = client.put(
            f"/v1/projects/{project['id']}/workspace-uploads/{upload['id']}/chunk",
            headers={
                **AUTH,
                "Idempotency-Key": "chunk-upload-0001",
                "If-Match": upload_etag,
            },
            json=_chunk(first_bytes, offset=0),
        )
        assert first.status_code == 200
        first_response = first.json()
        replay = client.put(
            f"/v1/projects/{project['id']}/workspace-uploads/{upload['id']}/chunk",
            headers={
                **AUTH,
                "Idempotency-Key": "chunk-upload-0001",
                "If-Match": upload_etag,
            },
            json=_chunk(first_bytes, offset=0),
        )
        assert replay.status_code == 200
        assert replay.json() == first_response

    with TestClient(_app(tmp_path)) as restarted:
        status = restarted.get(
            f"/v1/projects/{project['id']}/workspace-uploads/{upload['id']}",
            headers=AUTH,
        )
        assert status.json()["accepted_offset"] == 1024
        second = restarted.put(
            f"/v1/projects/{project['id']}/workspace-uploads/{upload['id']}/chunk",
            headers={
                **AUTH,
                "Idempotency-Key": "chunk-upload-0002",
                "If-Match": status.headers["etag"],
            },
            json=_chunk(archive[1024:], offset=1024),
        )
        assert second.status_code == 200, second.text
        finalized = restarted.post(
            f"/v1/projects/{project['id']}/workspace-uploads/{upload['id']}/finalize",
            headers={
                **AUTH,
                "Idempotency-Key": "finalize-upload-0001",
                "If-Match": second.headers["etag"],
                "If-Project-Match": project_etag,
            },
            json={"schema_version": "1", "content_sha256": hashlib.sha256(archive).hexdigest()},
        )
        assert finalized.status_code == 201, finalized.text
        result = finalized.json()
        assert result["upload"]["status"] == "finalized"
        assert (
            result["project"]["current_workspace_snapshot"]
            == result["publication"]["workspace_snapshot"]
        )
        assert result["project"]["etag"] != project_etag
        snapshot_root = tmp_path / "core-control-v1" / "workspace-snapshots"
        assert (
            snapshot_root / result["publication"]["workspace_snapshot"]["id"] / "src" / "AGENTS.md"
        ).read_bytes() == b"OpenEvo provider workspace\n"


def test_workspace_finalize_rejects_digest_and_stale_project_cas(tmp_path: Path) -> None:
    archive = _workspace_archive()
    with TestClient(_app(tmp_path)) as client:
        project, project_etag = _create_project(client, _project_create(archive=archive))
        upload = client.post(
            f"/v1/projects/{project['id']}/workspace-uploads",
            headers={**AUTH, "Idempotency-Key": "begin-upload-0002", "If-Match": project_etag},
            json={
                "schema_version": "1",
                "project_snapshot": project["current_project_snapshot"],
                "archive": _project_create(archive=archive)["workspace"]["archive"],
                "base_workspace_snapshot": None,
            },
        )
        chunk = client.put(
            f"/v1/projects/{project['id']}/workspace-uploads/{upload.json()['id']}/chunk",
            headers={
                **AUTH,
                "Idempotency-Key": "chunk-upload-0003",
                "If-Match": upload.headers["etag"],
            },
            json=_chunk(archive, offset=0),
        )
        bad_digest = client.post(
            f"/v1/projects/{project['id']}/workspace-uploads/{upload.json()['id']}/finalize",
            headers={
                **AUTH,
                "Idempotency-Key": "finalize-upload-bad-digest",
                "If-Match": chunk.headers["etag"],
                "If-Project-Match": project_etag,
            },
            json={"schema_version": "1", "content_sha256": "0" * 64},
        )
        assert bad_digest.status_code == 409
        assert bad_digest.json()["code"] == "workspace_digest_mismatch"

        patch = client.patch(
            f"/v1/projects/{project['id']}",
            headers={**AUTH, "Idempotency-Key": "patch-project-0002", "If-Match": project_etag},
            json={"schema_version": "1", "description": "Changed while upload was open."},
        )
        stale = client.post(
            f"/v1/projects/{project['id']}/workspace-uploads/{upload.json()['id']}/finalize",
            headers={
                **AUTH,
                "Idempotency-Key": "finalize-upload-stale-project",
                "If-Match": chunk.headers["etag"],
                "If-Project-Match": project_etag,
            },
            json={"schema_version": "1", "content_sha256": hashlib.sha256(archive).hexdigest()},
        )
        assert patch.status_code == 200
        assert stale.status_code == 412
        assert stale.json()["code"] == "project_etag_precondition_failed"


def test_capabilities_and_project_validation_use_verified_registry(tmp_path: Path) -> None:
    registry = verified_builtin_registry(tmp_path / "registry")
    with TestClient(_app(tmp_path / "state", registry=registry)) as client:
        capabilities = client.get(
            "/v1/capabilities",
            headers=AUTH,
            params={"execution_mode": "codex_subscription_transcript"},
        )
        assert capabilities.status_code == 200
        assert capabilities.json()["registry_digest"] == registry.snapshot.registry_digest
        project, _ = _create_project(client, _project_create())
        validated = client.post(
            f"/v1/projects/{project['id']}/validate",
            headers={**AUTH, "Idempotency-Key": "validate-project-0001"},
            json={
                "schema_version": "1",
                "project_snapshot": project["current_project_snapshot"],
                "workspace_snapshot": project["current_workspace_snapshot"],
                "expected_registry_digest": registry.snapshot.registry_digest,
            },
        )
        assert validated.status_code == 200, validated.text
        assert validated.json()["valid"] is True
        assert validated.json()["registry_digest"] == registry.snapshot.registry_digest

    with TestClient(_app(tmp_path / "no-registry")) as no_registry:
        unavailable = no_registry.get(
            "/v1/capabilities",
            headers=AUTH,
            params={"execution_mode": "codex_subscription_transcript"},
        )
        assert unavailable.status_code == 503
        assert unavailable.json()["code"] == "evolution_registry_unavailable"


def test_services_are_observed_and_unowned_actions_fail_closed(tmp_path: Path) -> None:
    with TestClient(_app(tmp_path)) as client:
        services = client.get("/v1/services", headers=AUTH)
        assert services.status_code == 200
        assert [item["id"] for item in services.json()["items"]] == ["core-control"]
        assert services.json()["items"][0]["status"] == "running"
        service = client.get("/v1/services/core-control", headers=AUTH)
        assert service.headers["etag"] == service.json()["etag"]

        restart = client.post(
            "/v1/services/core-control/restart",
            headers={
                **AUTH,
                "Idempotency-Key": "restart-control-0001",
                "If-Match": service.headers["etag"],
            },
            json={"schema_version": "1", "reason": "test"},
        )
        assert restart.status_code == 503
        assert restart.json()["code"] == "provider_capability_unavailable"
        doctor = client.post(
            "/v1/environment/doctor",
            headers={**AUTH, "Idempotency-Key": "doctor-environment-0001"},
            json={
                "schema_version": "1",
                "execution_mode": "codex_subscription_transcript",
                "checks": ["registry"],
            },
        )
        assert doctor.status_code == 503
        assert doctor.json()["code"] == "provider_capability_unavailable"
        run = client.post(
            "/v1/runs",
            headers={**AUTH, "Idempotency-Key": "create-run-unowned"},
            json={},
        )
        assert run.status_code == 422
        listed = client.get("/v1/runs", headers=AUTH)
        assert listed.status_code == 503
        assert listed.json()["code"] == "provider_capability_unavailable"


def test_event_replay_is_ordered_durable_and_expires(tmp_path: Path) -> None:
    app = _app(tmp_path, event_replay_limit=2)
    provider = app.state.core_control_provider
    with TestClient(app) as client:
        for index in range(3):
            response = client.post(
                "/v1/projects",
                headers={**AUTH, "Idempotency-Key": f"create-project-event-{index}"},
                json={**_project_create(), "name": f"Project {index}"},
            )
            assert response.status_code == 201
        frames = provider.store.replay_events(None)
        assert len(frames) == 2
        parsed = [SseFrameV1.model_validate_json(json.dumps(frame)) for frame in frames]
        assert [frame.data.root.sequence for frame in parsed] == sorted(
            frame.data.root.sequence for frame in parsed
        )
        last_id = parsed[0].id
        expired_id = provider.store.event_cursor(1)
        response = provider.invoke("streamCoreEventsV1", {"last_event_id": last_id})

        async def first_stream_frame() -> bytes:
            frame = await anext(response.body_iterator)
            await response.body_iterator.aclose()
            return frame

        wire = asyncio.run(first_stream_frame())
        assert wire.startswith(f"id: {parsed[1].id}\n".encode())
        assert b"event: project.updated.v1\n" in wire

    restarted_app = _app(tmp_path, event_replay_limit=2)
    restarted_provider = restarted_app.state.core_control_provider
    with TestClient(restarted_app):
        replay = restarted_provider.store.replay_events(last_id)
        assert [frame["id"] for frame in replay] == [parsed[1].id]
        with pytest.raises(Exception, match="expired"):
            restarted_provider.store.replay_events(expired_id)


def test_workspace_verifier_rejects_noncanonical_ustar(tmp_path: Path) -> None:
    archive = bytearray(_workspace_archive())
    archive[0] = ord("X")
    archive_path = tmp_path / "bad.tar"
    archive_path.write_bytes(archive)
    declaration = _project_create(archive=bytes(archive))["workspace"]["archive"]
    model = WorkspaceArchiveDeclarationV1.model_validate_json(json.dumps(declaration))
    with pytest.raises(WorkspaceArchiveError, match="checksum"):
        verify_workspace_archive(archive_path, model)


def _chunk(content: bytes, *, offset: int) -> dict[str, object]:
    return {
        "schema_version": "1",
        "offset": offset,
        "byte_length": len(content),
        "content_base64": base64.b64encode(content).decode("ascii"),
        "content_sha256": hashlib.sha256(content).hexdigest(),
    }
