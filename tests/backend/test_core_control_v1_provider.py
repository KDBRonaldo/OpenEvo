from __future__ import annotations

import base64
import asyncio
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import threading
import time

from fastapi.testclient import TestClient
import httpx
import pytest

from openevo.backend.contracts.v1 import (
    create_core_control_app,
    openapi_sha256,
)
from openevo.backend.contracts.v1.models import (
    ApiErrorV1,
    SseFrameV1,
    WorkspaceArchiveDeclarationV1,
    WorkspaceUploadChunkV1,
)
import openevo.backend.contracts.v1.store as store_module
import openevo.backend.contracts.v1.provider as provider_module
from openevo.backend.contracts.v1.store import (
    CoreControlStoreError,
    CoreControlStoreV1,
    StoreCorruptionError,
)
import openevo.backend.contracts.v1.workspace as workspace_module
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


def _project_create(
    *,
    archive: bytes | None = None,
    archive_entry_count: int = 2,
    extracted_byte_size: int = len(b"OpenEvo provider workspace\n"),
    execution_mode: str = "codex_subscription_transcript",
    capture_mode: str = "transcript",
    harness_id: str = "codex",
) -> dict[str, object]:
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
                "entry_count": archive_entry_count,
                "extracted_byte_size": extracted_byte_size,
                "policy": _archive_policy(),
            },
        }
    return {
        "schema_version": "1",
        "name": "Protein memory",
        "description": "Provider conformance project.",
        "spec": {
            "execution_mode": execution_mode,
            "capture_mode": capture_mode,
            "harness_id": harness_id,
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


def _create_project(
    client: TestClient,
    payload: dict[str, object],
    *,
    idempotency_key: str = "create-project-0001",
) -> tuple[dict, str]:
    response = client.post(
        "/v1/projects",
        headers={**AUTH, "Idempotency-Key": idempotency_key},
        json=payload,
    )
    assert response.status_code == 201, response.text
    return response.json(), response.headers["etag"]


def _finalize_workspace(tmp_path: Path, archive: bytes, *, key_suffix: str) -> dict:
    payload = _project_create(archive=archive)
    with TestClient(_app(tmp_path)) as client:
        project, project_etag = _create_project(client, payload)
        upload = client.post(
            f"/v1/projects/{project['id']}/workspace-uploads",
            headers={
                **AUTH,
                "Idempotency-Key": f"begin-upload-{key_suffix}",
                "If-Match": project_etag,
            },
            json={
                "schema_version": "1",
                "project_snapshot": project["current_project_snapshot"],
                "archive": payload["workspace"]["archive"],
                "base_workspace_snapshot": None,
            },
        )
        chunk = client.put(
            f"/v1/projects/{project['id']}/workspace-uploads/{upload.json()['id']}/chunk",
            headers={
                **AUTH,
                "Idempotency-Key": f"chunk-upload-{key_suffix}",
                "If-Match": upload.headers["etag"],
            },
            json=_chunk(archive, offset=0),
        )
        finalized = client.post(
            f"/v1/projects/{project['id']}/workspace-uploads/{upload.json()['id']}/finalize",
            headers={
                **AUTH,
                "Idempotency-Key": f"finalize-upload-{key_suffix}",
                "If-Match": chunk.headers["etag"],
                "If-Project-Match": project_etag,
            },
            json={"schema_version": "1", "content_sha256": hashlib.sha256(archive).hexdigest()},
        )
        assert finalized.status_code == 201, finalized.text
        return finalized.json()


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


def test_workspace_upload_at_offset_zero_survives_restart(tmp_path: Path) -> None:
    archive = _workspace_archive()
    with TestClient(_app(tmp_path)) as client:
        project, project_etag = _create_project(client, _project_create(archive=archive))
        begin = client.post(
            f"/v1/projects/{project['id']}/workspace-uploads",
            headers={
                **AUTH,
                "Idempotency-Key": "begin-upload-offset-zero",
                "If-Match": project_etag,
            },
            json={
                "schema_version": "1",
                "project_snapshot": project["current_project_snapshot"],
                "archive": project["workspace"]["archive"],
                "base_workspace_snapshot": None,
            },
        )
        assert begin.status_code == 201, begin.text
        upload = begin.json()
        upload_etag = begin.headers["etag"]

    with TestClient(_app(tmp_path)) as restarted:
        recovered = restarted.get(
            f"/v1/projects/{project['id']}/workspace-uploads/{upload['id']}",
            headers=AUTH,
        )

        assert recovered.status_code == 200, recovered.text
        assert recovered.json()["schema_version"] == "1"
        assert recovered.json()["accepted_offset"] == 0
        assert recovered.headers["etag"] == upload_etag


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
        assert bad_digest.json()["category"] == "project"
        assert bad_digest.json()["retryable"] is False
        assert bad_digest.json()["repair_action"] == "openevo_can_reconfigure"

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


def test_create_upload_discards_exact_file_when_transaction_rolls_back(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    archive = _workspace_archive()
    with TestClient(_app(tmp_path)) as client:
        project, etag = _create_project(client, _project_create(archive=archive))
        store = client.app.state.core_control_provider.store
        original = store._store_idempotency

        def fail_create(operation_id, *args, **kwargs):
            if operation_id == "createCoreWorkspaceUploadV1":
                raise CoreControlStoreError("injected create rollback")
            return original(operation_id, *args, **kwargs)

        monkeypatch.setattr(store, "_store_idempotency", fail_create)
        response = client.post(
            f"/v1/projects/{project['id']}/workspace-uploads",
            headers={
                **AUTH,
                "Idempotency-Key": "create-upload-rollback",
                "If-Match": etag,
            },
            json={
                "schema_version": "1",
                "project_snapshot": project["current_project_snapshot"],
                "archive": project["workspace"]["archive"],
                "base_workspace_snapshot": None,
            },
        )
        assert response.status_code == 500
        assert list((tmp_path / "core-control-v1" / "workspace-uploads").iterdir()) == []


def test_finalize_discards_exact_snapshot_when_transaction_rolls_back(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    archive = _workspace_archive()
    with TestClient(_app(tmp_path)) as client:
        project, project_etag = _create_project(client, _project_create(archive=archive))
        upload = client.post(
            f"/v1/projects/{project['id']}/workspace-uploads",
            headers={
                **AUTH,
                "Idempotency-Key": "begin-finalize-rollback",
                "If-Match": project_etag,
            },
            json={
                "schema_version": "1",
                "project_snapshot": project["current_project_snapshot"],
                "archive": project["workspace"]["archive"],
                "base_workspace_snapshot": None,
            },
        )
        chunk = client.put(
            f"/v1/projects/{project['id']}/workspace-uploads/{upload.json()['id']}/chunk",
            headers={
                **AUTH,
                "Idempotency-Key": "chunk-finalize-rollback",
                "If-Match": upload.headers["etag"],
            },
            json=_chunk(archive, offset=0),
        )
        store = client.app.state.core_control_provider.store
        original = store._store_idempotency

        def fail_finalize(operation_id, *args, **kwargs):
            if operation_id == "finalizeCoreWorkspaceUploadV1":
                raise CoreControlStoreError("injected finalize rollback")
            return original(operation_id, *args, **kwargs)

        monkeypatch.setattr(store, "_store_idempotency", fail_finalize)
        response = client.post(
            f"/v1/projects/{project['id']}/workspace-uploads/{upload.json()['id']}/finalize",
            headers={
                **AUTH,
                "Idempotency-Key": "finalize-rollback",
                "If-Match": chunk.headers["etag"],
                "If-Project-Match": project_etag,
            },
            json={
                "schema_version": "1",
                "content_sha256": hashlib.sha256(archive).hexdigest(),
            },
        )
        assert response.status_code == 500
        assert list((tmp_path / "core-control-v1" / "workspace-snapshots").iterdir()) == []


def test_project_delete_discards_owned_upload_and_workspace_snapshot(tmp_path: Path) -> None:
    finalized = _finalize_workspace(tmp_path, _workspace_archive(), key_suffix="delete-owned")
    project = finalized["project"]
    with TestClient(_app(tmp_path)) as client:
        deleted = client.delete(
            f"/v1/projects/{project['id']}",
            headers={
                **AUTH,
                "Idempotency-Key": "delete-owned-publication",
                "If-Match": project["etag"],
            },
        )
        assert deleted.status_code == 204, deleted.text
        assert list((tmp_path / "core-control-v1" / "workspace-uploads").iterdir()) == []
        assert list((tmp_path / "core-control-v1" / "workspace-snapshots").iterdir()) == []


def test_aborted_upload_releases_reserved_managed_byte_capacity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    archive = _workspace_archive()
    monkeypatch.setattr(store_module, "_MAX_MANAGED_WORKSPACE_BYTES", len(archive))
    with TestClient(_app(tmp_path)) as client:
        first, first_etag = _create_project(
            client, _project_create(archive=archive), idempotency_key="quota-project-first"
        )
        first_upload = client.post(
            f"/v1/projects/{first['id']}/workspace-uploads",
            headers={
                **AUTH,
                "Idempotency-Key": "quota-upload-first",
                "If-Match": first_etag,
            },
            json={
                "schema_version": "1",
                "project_snapshot": first["current_project_snapshot"],
                "archive": first["workspace"]["archive"],
                "base_workspace_snapshot": None,
            },
        )
        aborted = client.post(
            f"/v1/projects/{first['id']}/workspace-uploads/{first_upload.json()['id']}/abort",
            headers={
                **AUTH,
                "Idempotency-Key": "quota-abort-first",
                "If-Match": first_upload.headers["etag"],
            },
            json={"schema_version": "1", "reason": "release capacity"},
        )
        assert aborted.status_code == 200

        second, second_etag = _create_project(
            client, _project_create(archive=archive), idempotency_key="quota-project-second"
        )
        second_upload = client.post(
            f"/v1/projects/{second['id']}/workspace-uploads",
            headers={
                **AUTH,
                "Idempotency-Key": "quota-upload-second",
                "If-Match": second_etag,
            },
            json={
                "schema_version": "1",
                "project_snapshot": second["current_project_snapshot"],
                "archive": second["workspace"]["archive"],
                "base_workspace_snapshot": None,
            },
        )
        assert second_upload.status_code == 201, second_upload.text


def test_abort_cleanup_failure_persists_intent_and_exact_replay_converges(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    archive = _workspace_archive()
    outside = tmp_path / "outside.txt"
    outside.write_text("do not delete", encoding="utf-8")
    with TestClient(_app(tmp_path)) as client:
        project, etag = _create_project(client, _project_create(archive=archive))
        upload = client.post(
            f"/v1/projects/{project['id']}/workspace-uploads",
            headers={
                **AUTH,
                "Idempotency-Key": "abort-cleanup-begin",
                "If-Match": etag,
            },
            json={
                "schema_version": "1",
                "project_snapshot": project["current_project_snapshot"],
                "archive": project["workspace"]["archive"],
                "base_workspace_snapshot": None,
            },
        )
        assert upload.status_code == 201
        upload_name = f"{upload.json()['id']}.part"
        upload_root = tmp_path / "core-control-v1" / "workspace-uploads"
        displaced = upload_root / ".reviewer-displaced-upload"
        raced = False

        def replace_after_observe(root_kind, parent_fd, name, identity):
            nonlocal raced
            del parent_fd, identity
            if raced or root_kind != "upload" or name != upload_name:
                return
            raced = True
            (upload_root / name).rename(displaced)
            (upload_root / name).symlink_to(outside)

        monkeypatch.setattr(
            store_module,
            "_after_cleanup_entry_observed",
            replace_after_observe,
            raising=False,
        )
        abort_headers = {
            **AUTH,
            "Idempotency-Key": "abort-cleanup-replay",
            "If-Match": upload.headers["etag"],
        }
        abort_body = {"schema_version": "1", "reason": "reviewer race"}
        failed = client.post(
            f"/v1/projects/{project['id']}/workspace-uploads/{upload.json()['id']}/abort",
            headers=abort_headers,
            json=abort_body,
        )
        assert failed.status_code == 500
        assert raced
        assert outside.read_text(encoding="utf-8") == "do not delete"
        assert (upload_root / upload_name).is_symlink()
        assert displaced.is_file()
        with sqlite3.connect(tmp_path / "core-control-v1" / "provider.sqlite3") as connection:
            assert (
                connection.execute("SELECT COUNT(*) FROM managed_cleanup_intents").fetchone()[0]
                == 1
            )

        monkeypatch.setattr(
            store_module,
            "_after_cleanup_entry_observed",
            lambda *args: None,
            raising=False,
        )
        replay = client.post(
            f"/v1/projects/{project['id']}/workspace-uploads/{upload.json()['id']}/abort",
            headers=abort_headers,
            json=abort_body,
        )
        assert replay.status_code == 200, replay.text
        assert outside.read_text(encoding="utf-8") == "do not delete"
        assert list(upload_root.iterdir()) == []
        with sqlite3.connect(tmp_path / "core-control-v1" / "provider.sqlite3") as connection:
            assert (
                connection.execute("SELECT COUNT(*) FROM managed_cleanup_intents").fetchone()[0]
                == 0
            )


def test_delete_cleanup_crash_recovers_before_live_quota_and_replays(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    finalized = _finalize_workspace(tmp_path, _workspace_archive(), key_suffix="delete-crash")
    project = finalized["project"]
    crashed = False

    def crash_after_quarantine(root_kind, parent_fd, original_name, quarantine_name):
        nonlocal crashed
        del root_kind, parent_fd, original_name, quarantine_name
        if not crashed:
            crashed = True
            raise OSError("injected crash after quarantine")

    app = _app(tmp_path)
    with TestClient(app) as client:
        monkeypatch.setattr(
            store_module,
            "_after_managed_quarantine",
            crash_after_quarantine,
            raising=False,
        )
        headers = {
            **AUTH,
            "Idempotency-Key": "delete-cleanup-crash",
            "If-Match": project["etag"],
        }
        failed = client.delete(f"/v1/projects/{project['id']}", headers=headers)
        assert failed.status_code == 500
        assert crashed
        managed_names = {
            path.name
            for root_name in ("workspace-uploads", "workspace-snapshots")
            for path in (tmp_path / "core-control-v1" / root_name).iterdir()
        }
        assert any(name.startswith(".quarantine-") for name in managed_names)

    monkeypatch.setattr(
        store_module,
        "_after_managed_quarantine",
        lambda *args: None,
        raising=False,
    )
    monkeypatch.setattr(store_module, "_MAX_MANAGED_WORKSPACE_BYTES", 0)
    with TestClient(_app(tmp_path)) as restarted:
        replay = restarted.delete(
            f"/v1/projects/{project['id']}",
            headers={
                **AUTH,
                "Idempotency-Key": "delete-cleanup-crash",
                "If-Match": project["etag"],
            },
        )
        assert replay.status_code == 204, replay.text
        assert list((tmp_path / "core-control-v1" / "workspace-uploads").iterdir()) == []
        assert list((tmp_path / "core-control-v1" / "workspace-snapshots").iterdir()) == []


def test_delete_cleanup_never_removes_replacement_directory_target(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    finalized = _finalize_workspace(
        tmp_path,
        _workspace_archive(),
        key_suffix="delete-directory-race",
    )
    project = finalized["project"]
    snapshot_name = finalized["publication"]["workspace_snapshot"]["id"]
    snapshot_root = tmp_path / "core-control-v1" / "workspace-snapshots"
    displaced = snapshot_root / ".reviewer-displaced-snapshot"
    outside = tmp_path / "outside-directory"
    outside.mkdir()
    outside_file = outside / "keep.txt"
    outside_file.write_text("keep", encoding="utf-8")
    raced = False

    def replace_after_observe(root_kind, parent_fd, name, identity):
        nonlocal raced
        del parent_fd, identity
        if raced or root_kind != "workspace" or name != snapshot_name:
            return
        raced = True
        (snapshot_root / name).rename(displaced)
        (snapshot_root / name).symlink_to(outside, target_is_directory=True)

    headers = {
        **AUTH,
        "Idempotency-Key": "delete-directory-race",
        "If-Match": project["etag"],
    }
    with TestClient(_app(tmp_path)) as client:
        monkeypatch.setattr(
            store_module,
            "_after_cleanup_entry_observed",
            replace_after_observe,
            raising=False,
        )
        failed = client.delete(f"/v1/projects/{project['id']}", headers=headers)
        assert failed.status_code == 500
        assert raced
        assert displaced.is_dir()
        assert (snapshot_root / snapshot_name).is_symlink()
        assert outside_file.read_text(encoding="utf-8") == "keep"

        monkeypatch.setattr(
            store_module,
            "_after_cleanup_entry_observed",
            lambda *args: None,
            raising=False,
        )
        replay = client.delete(f"/v1/projects/{project['id']}", headers=headers)
        assert replay.status_code == 204, replay.text
        assert list(snapshot_root.iterdir()) == []
        assert outside_file.read_text(encoding="utf-8") == "keep"


def test_cleanup_budget_is_cumulative_and_orphans_do_not_block_live_quota(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = CoreControlStoreV1(tmp_path)
    store.close()
    upload_root = tmp_path / "core-control-v1" / "workspace-uploads"
    upload_orphan = upload_root / "orphan.part"
    upload_orphan.write_bytes(b"x" * 32)
    upload_orphan.chmod(0o600)
    snapshot_root = tmp_path / "core-control-v1" / "workspace-snapshots"
    snapshot_orphan = snapshot_root / "workspace-snapshot-orphan"
    snapshot_orphan.mkdir(mode=0o700)

    monkeypatch.setattr(store_module, "_MAX_RECOVERY_CLEANUP_NODES", 1, raising=False)
    monkeypatch.setattr(store_module, "_MAX_MANAGED_WORKSPACE_BYTES", 0)
    limited = CoreControlStoreV1(tmp_path)
    limited.close()
    assert list(upload_root.iterdir()) == []
    assert snapshot_orphan.is_dir()

    monkeypatch.setattr(store_module, "_MAX_RECOVERY_CLEANUP_NODES", 128, raising=False)
    converged = CoreControlStoreV1(tmp_path)
    converged.close()
    assert list(upload_root.iterdir()) == []
    assert list(snapshot_root.iterdir()) == []


def test_same_archive_and_clock_publish_distinct_project_owned_snapshots(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fixed = "2026-07-14T12:00:00.000000Z"
    monkeypatch.setattr(CoreControlStoreV1, "_timestamp", lambda self: fixed)
    archive = b"\0" * 1024
    payload = _project_create(
        archive=archive,
        archive_entry_count=0,
        extracted_byte_size=0,
    )

    publications = []
    with TestClient(_app(tmp_path)) as client:
        for index in range(2):
            project, project_etag = _create_project(
                client,
                payload,
                idempotency_key=f"owner-project-{index}",
            )
            upload = client.post(
                f"/v1/projects/{project['id']}/workspace-uploads",
                headers={
                    **AUTH,
                    "Idempotency-Key": f"owner-upload-{index}",
                    "If-Match": project_etag,
                },
                json={
                    "schema_version": "1",
                    "project_snapshot": project["current_project_snapshot"],
                    "archive": project["workspace"]["archive"],
                    "base_workspace_snapshot": None,
                },
            )
            chunk = client.put(
                f"/v1/projects/{project['id']}/workspace-uploads/{upload.json()['id']}/chunk",
                headers={
                    **AUTH,
                    "Idempotency-Key": f"owner-chunk-{index}",
                    "If-Match": upload.headers["etag"],
                },
                json=_chunk(archive, offset=0),
            )
            finalized = client.post(
                f"/v1/projects/{project['id']}/workspace-uploads/{upload.json()['id']}/finalize",
                headers={
                    **AUTH,
                    "Idempotency-Key": f"owner-finalize-{index}",
                    "If-Match": chunk.headers["etag"],
                    "If-Project-Match": project_etag,
                },
                json={
                    "schema_version": "1",
                    "content_sha256": hashlib.sha256(archive).hexdigest(),
                },
            )
            assert finalized.status_code == 201, finalized.text
            publications.append(finalized.json()["publication"])

    assert (
        publications[0]["workspace_snapshot"]["id"]
        != (publications[1]["workspace_snapshot"]["id"])
    )
    assert (
        publications[0]["content_ref"]["content_id"]
        != (publications[1]["content_ref"]["content_id"])
    )
    with sqlite3.connect(tmp_path / "core-control-v1" / "provider.sqlite3") as connection:
        owners = connection.execute(
            "SELECT snapshot_id, content_id, project_id, upload_id "
            "FROM workspace_publication_owners"
        ).fetchall()
    assert len(owners) == 2
    assert {row[0] for row in owners} == {
        publication["workspace_snapshot"]["id"] for publication in publications
    }
    assert {row[1] for row in owners} == {
        publication["content_ref"]["content_id"] for publication in publications
    }
    assert len({row[2] for row in owners}) == 2
    assert len({row[3] for row in owners}) == 2
    restarted = CoreControlStoreV1(tmp_path)
    restarted.close()


def test_workspace_publication_no_replace_preserves_competing_inode(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fixed = "2026-07-14T12:00:00.000000Z"
    monkeypatch.setattr(CoreControlStoreV1, "_timestamp", lambda self: fixed)
    archive = _workspace_archive()
    with TestClient(_app(tmp_path)) as client:
        project, project_etag = _create_project(
            client,
            _project_create(archive=archive),
            idempotency_key="no-replace-project",
        )
        upload = client.post(
            f"/v1/projects/{project['id']}/workspace-uploads",
            headers={
                **AUTH,
                "Idempotency-Key": "no-replace-upload",
                "If-Match": project_etag,
            },
            json={
                "schema_version": "1",
                "project_snapshot": project["current_project_snapshot"],
                "archive": project["workspace"]["archive"],
                "base_workspace_snapshot": None,
            },
        )
        chunk = client.put(
            f"/v1/projects/{project['id']}/workspace-uploads/{upload.json()['id']}/chunk",
            headers={
                **AUTH,
                "Idempotency-Key": "no-replace-chunk",
                "If-Match": upload.headers["etag"],
            },
            json=_chunk(archive, offset=0),
        )
        store = client.app.state.core_control_provider.store
        snapshot = store_module._workspace_publication_snapshot(
            store._signing_key,
            project_id=project["id"],
            upload_id=upload.json()["id"],
            archive_sha256=hashlib.sha256(archive).hexdigest(),
            now=fixed,
        )
        competing = tmp_path / "core-control-v1" / "workspace-snapshots" / snapshot.id
        competing.mkdir(mode=0o700)
        marker = competing / "keep.txt"
        marker.write_text("competing inode", encoding="utf-8")

        finalized = client.post(
            f"/v1/projects/{project['id']}/workspace-uploads/{upload.json()['id']}/finalize",
            headers={
                **AUTH,
                "Idempotency-Key": "no-replace-finalize",
                "If-Match": chunk.headers["etag"],
                "If-Project-Match": project_etag,
            },
            json={
                "schema_version": "1",
                "content_sha256": hashlib.sha256(archive).hexdigest(),
            },
        )
        assert finalized.status_code == 500
        assert marker.read_text(encoding="utf-8") == "competing inode"

    recovered = CoreControlStoreV1(tmp_path)
    recovered.close()
    assert list((tmp_path / "core-control-v1" / "workspace-snapshots").iterdir()) == []


def test_workspace_chunk_handles_short_writes_before_advancing_offset(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    archive = _workspace_archive()
    app = _app(tmp_path)
    with TestClient(app) as client:
        project, project_etag = _create_project(client, _project_create(archive=archive))
        upload = client.post(
            f"/v1/projects/{project['id']}/workspace-uploads",
            headers={
                **AUTH,
                "Idempotency-Key": "begin-upload-short-write",
                "If-Match": project_etag,
            },
            json={
                "schema_version": "1",
                "project_snapshot": project["current_project_snapshot"],
                "archive": project["workspace"]["archive"],
                "base_workspace_snapshot": None,
            },
        )
        real_write = os.write
        write_lengths: list[int] = []

        def short_write(fd: int, content: bytes | memoryview) -> int:
            requested = len(content)
            accepted = max(1, requested // 2) if not write_lengths else requested
            write_lengths.append(accepted)
            return real_write(fd, content[:accepted])

        monkeypatch.setattr("openevo.backend.contracts.v1.store.os.write", short_write)
        chunk = client.put(
            f"/v1/projects/{project['id']}/workspace-uploads/{upload.json()['id']}/chunk",
            headers={
                **AUTH,
                "Idempotency-Key": "chunk-upload-short-write",
                "If-Match": upload.headers["etag"],
            },
            json=_chunk(archive, offset=0),
        )

        assert chunk.status_code == 200, chunk.text
        assert len(write_lengths) >= 2
        assert chunk.json()["accepted_offset"] == len(archive)
        upload_path = (
            tmp_path / "core-control-v1" / "workspace-uploads" / f"{upload.json()['id']}.part"
        )
        assert upload_path.read_bytes() == archive


def test_workspace_chunk_partial_then_zero_write_rolls_back_to_zero_across_restart(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    archive = _workspace_archive()
    app = _app(tmp_path)
    with TestClient(app, raise_server_exceptions=False) as client:
        project, project_etag = _create_project(client, _project_create(archive=archive))
        upload = client.post(
            f"/v1/projects/{project['id']}/workspace-uploads",
            headers={
                **AUTH,
                "Idempotency-Key": "begin-upload-zero-write",
                "If-Match": project_etag,
            },
            json={
                "schema_version": "1",
                "project_snapshot": project["current_project_snapshot"],
                "archive": project["workspace"]["archive"],
                "base_workspace_snapshot": None,
            },
        )
        real_write = os.write
        write_count = 0

        def partial_then_zero(fd: int, content: bytes | memoryview) -> int:
            nonlocal write_count
            write_count += 1
            if write_count == 1:
                return real_write(fd, content[: max(1, len(content) // 2)])
            return 0

        monkeypatch.setattr("openevo.backend.contracts.v1.store.os.write", partial_then_zero)
        chunk = client.put(
            f"/v1/projects/{project['id']}/workspace-uploads/{upload.json()['id']}/chunk",
            headers={
                **AUTH,
                "Idempotency-Key": "chunk-upload-zero-write",
                "If-Match": upload.headers["etag"],
            },
            json=_chunk(archive, offset=0),
        )
        status = client.get(
            f"/v1/projects/{project['id']}/workspace-uploads/{upload.json()['id']}",
            headers=AUTH,
        )

        assert chunk.status_code == 500
        assert status.json()["accepted_offset"] == 0
        upload_path = (
            tmp_path / "core-control-v1" / "workspace-uploads" / f"{upload.json()['id']}.part"
        )
        assert upload_path.stat().st_size == 0

    with TestClient(_app(tmp_path)) as restarted:
        recovered = restarted.get(
            f"/v1/projects/{project['id']}/workspace-uploads/{upload.json()['id']}",
            headers=AUTH,
        )
        assert recovered.status_code == 200, recovered.text
        assert recovered.json()["accepted_offset"] == 0


def test_workspace_chunk_post_commit_binding_failure_preserves_committed_bytes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    archive = _workspace_archive()
    app = _app(tmp_path)
    with TestClient(app, raise_server_exceptions=False) as client:
        project, project_etag = _create_project(client, _project_create(archive=archive))
        upload = client.post(
            f"/v1/projects/{project['id']}/workspace-uploads",
            headers={
                **AUTH,
                "Idempotency-Key": "begin-upload-post-commit-binding",
                "If-Match": project_etag,
            },
            json={
                "schema_version": "1",
                "project_snapshot": project["current_project_snapshot"],
                "archive": project["workspace"]["archive"],
                "base_workspace_snapshot": None,
            },
        )
        snapshot_root = tmp_path / "core-control-v1" / "workspace-snapshots"
        displaced_root = tmp_path / "core-control-v1" / "workspace-snapshots.displaced"
        original_post_commit_verify = store_module._Transaction._verify_after_commit
        replace_root = True

        def replace_root_after_commit(transaction) -> None:
            nonlocal replace_root
            if replace_root:
                replace_root = False
                snapshot_root.rename(displaced_root)
                snapshot_root.mkdir(mode=0o700)
                try:
                    original_post_commit_verify(transaction)
                finally:
                    snapshot_root.rmdir()
                    displaced_root.rename(snapshot_root)
                return
            original_post_commit_verify(transaction)

        monkeypatch.setattr(
            store_module._Transaction,
            "_verify_after_commit",
            replace_root_after_commit,
        )
        chunk = client.put(
            f"/v1/projects/{project['id']}/workspace-uploads/{upload.json()['id']}/chunk",
            headers={
                **AUTH,
                "Idempotency-Key": "chunk-upload-post-commit-binding",
                "If-Match": upload.headers["etag"],
            },
            json=_chunk(archive, offset=0),
        )

        upload_path = (
            tmp_path / "core-control-v1" / "workspace-uploads" / f"{upload.json()['id']}.part"
        )
        assert chunk.status_code == 500
        assert chunk.json()["code"] == "core_control_store_failed"
        assert upload_path.read_bytes() == archive

        replay = client.put(
            f"/v1/projects/{project['id']}/workspace-uploads/{upload.json()['id']}/chunk",
            headers={
                **AUTH,
                "Idempotency-Key": "chunk-upload-post-commit-binding",
                "If-Match": upload.headers["etag"],
            },
            json=_chunk(archive, offset=0),
        )
        assert replay.status_code == 200, replay.text
        assert replay.json()["accepted_offset"] == len(archive)
        assert upload_path.read_bytes() == archive


def test_workspace_chunk_reconciles_commit_that_is_durable_when_commit_raises(
    tmp_path: Path,
) -> None:
    archive = _workspace_archive()
    app = _app(tmp_path)
    with TestClient(app, raise_server_exceptions=False) as client:
        project, project_etag = _create_project(client, _project_create(archive=archive))
        upload = client.post(
            f"/v1/projects/{project['id']}/workspace-uploads",
            headers={
                **AUTH,
                "Idempotency-Key": "begin-upload-unknown-commit",
                "If-Match": project_etag,
            },
            json={
                "schema_version": "1",
                "project_snapshot": project["current_project_snapshot"],
                "archive": project["workspace"]["archive"],
                "base_workspace_snapshot": None,
            },
        )
        provider = app.state.core_control_provider
        real_connection = provider.store._connection

        class DurableCommitRaises:
            def __init__(self, connection) -> None:
                self._connection = connection
                self._raise_commit = True

            def execute(self, statement, parameters=()):
                result = self._connection.execute(statement, parameters)
                if self._raise_commit and statement.strip().upper() == "COMMIT":
                    self._raise_commit = False
                    raise sqlite3.OperationalError("COMMIT result is unknown")
                return result

            def close(self) -> None:
                self._connection.close()

            def __getattr__(self, name):
                return getattr(self._connection, name)

        provider.store._connection = DurableCommitRaises(real_connection)
        chunk_body = _chunk(archive, offset=0)
        chunk = client.put(
            f"/v1/projects/{project['id']}/workspace-uploads/{upload.json()['id']}/chunk",
            headers={
                **AUTH,
                "Idempotency-Key": "chunk-upload-unknown-commit",
                "If-Match": upload.headers["etag"],
            },
            json=chunk_body,
        )
        upload_path = (
            tmp_path / "core-control-v1" / "workspace-uploads" / f"{upload.json()['id']}.part"
        )

        assert chunk.status_code == 200, chunk.text
        assert chunk.json()["accepted_offset"] == len(archive)
        assert upload_path.read_bytes() == archive
        replay = client.put(
            f"/v1/projects/{project['id']}/workspace-uploads/{upload.json()['id']}/chunk",
            headers={
                **AUTH,
                "Idempotency-Key": "chunk-upload-unknown-commit",
                "If-Match": upload.headers["etag"],
            },
            json=chunk_body,
        )
        assert replay.status_code == 200, replay.text
        assert replay.json() == chunk.json()

    with TestClient(_app(tmp_path)) as restarted:
        recovered = restarted.get(
            f"/v1/projects/{project['id']}/workspace-uploads/{upload.json()['id']}",
            headers=AUTH,
        )
        assert recovered.status_code == 200, recovered.text
        assert recovered.json()["accepted_offset"] == len(archive)


def _inject_duplicate_failed_chunk(
    tmp_path: Path,
    *,
    project_id: str,
    upload_id: str,
    upload_etag: str,
    idempotency_key: str,
    chunk_body: dict[str, object],
    error_payload: dict[str, object],
) -> None:
    request = WorkspaceUploadChunkV1.model_validate(chunk_body)
    arguments = {
        "project_id": project_id,
        "upload_id": upload_id,
        "request": request,
        "if_match": upload_etag,
        "idempotency_key": idempotency_key,
    }
    identity = store_module._failed_idempotency_identity(
        "putCoreWorkspaceUploadChunkV1", arguments
    )
    assert identity is not None
    scope, key, digest = identity
    error = ApiErrorV1.model_validate_json(json.dumps(error_payload))
    now = int(datetime.now(timezone.utc).timestamp())
    with sqlite3.connect(tmp_path / "core-control-v1" / "provider.sqlite3") as connection:
        connection.execute(
            "INSERT INTO failed_idempotency_records VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                "putCoreWorkspaceUploadChunkV1",
                scope,
                key,
                digest,
                store_module._model_bytes(error),
                now,
                now + 3600,
            ),
        )


def test_startup_prefers_valid_success_over_duplicate_failed_idempotency(tmp_path: Path) -> None:
    archive = _workspace_archive()
    chunk_body = _chunk(archive, offset=0)
    target_key = "chunk-upload-duplicate-success"
    with TestClient(_app(tmp_path)) as client:
        project, project_etag = _create_project(client, _project_create(archive=archive))
        upload = client.post(
            f"/v1/projects/{project['id']}/workspace-uploads",
            headers={
                **AUTH,
                "Idempotency-Key": "begin-upload-duplicate-success",
                "If-Match": project_etag,
            },
            json={
                "schema_version": "1",
                "project_snapshot": project["current_project_snapshot"],
                "archive": project["workspace"]["archive"],
                "base_workspace_snapshot": None,
            },
        )
        stale = client.put(
            f"/v1/projects/{project['id']}/workspace-uploads/{upload.json()['id']}/chunk",
            headers={
                **AUTH,
                "Idempotency-Key": "chunk-upload-source-failure",
                "If-Match": '"' + ("0" * 64) + '"',
            },
            json=chunk_body,
        )
        success = client.put(
            f"/v1/projects/{project['id']}/workspace-uploads/{upload.json()['id']}/chunk",
            headers={
                **AUTH,
                "Idempotency-Key": target_key,
                "If-Match": upload.headers["etag"],
            },
            json=chunk_body,
        )
        assert stale.status_code == 412
        assert success.status_code == 200

    _inject_duplicate_failed_chunk(
        tmp_path,
        project_id=project["id"],
        upload_id=upload.json()["id"],
        upload_etag=upload.headers["etag"],
        idempotency_key=target_key,
        chunk_body=chunk_body,
        error_payload=stale.json(),
    )

    with TestClient(_app(tmp_path)) as restarted:
        replay = restarted.put(
            f"/v1/projects/{project['id']}/workspace-uploads/{upload.json()['id']}/chunk",
            headers={
                **AUTH,
                "Idempotency-Key": target_key,
                "If-Match": upload.headers["etag"],
            },
            json=chunk_body,
        )
        assert replay.status_code == 200, replay.text
        assert replay.json() == success.json()

    with sqlite3.connect(tmp_path / "core-control-v1" / "provider.sqlite3") as connection:
        remaining = connection.execute(
            "SELECT count(*) FROM failed_idempotency_records "
            "WHERE operation_id = ? AND idempotency_key = ?",
            ("putCoreWorkspaceUploadChunkV1", target_key),
        ).fetchone()[0]
    assert remaining == 0


def test_startup_keeps_failed_audit_when_duplicate_success_is_invalid(tmp_path: Path) -> None:
    archive = _workspace_archive()
    chunk_body = _chunk(archive, offset=0)
    target_key = "chunk-upload-duplicate-invalid-success"
    with TestClient(_app(tmp_path)) as client:
        project, project_etag = _create_project(client, _project_create(archive=archive))
        upload = client.post(
            f"/v1/projects/{project['id']}/workspace-uploads",
            headers={
                **AUTH,
                "Idempotency-Key": "begin-upload-duplicate-invalid-success",
                "If-Match": project_etag,
            },
            json={
                "schema_version": "1",
                "project_snapshot": project["current_project_snapshot"],
                "archive": project["workspace"]["archive"],
                "base_workspace_snapshot": None,
            },
        )
        stale = client.put(
            f"/v1/projects/{project['id']}/workspace-uploads/{upload.json()['id']}/chunk",
            headers={
                **AUTH,
                "Idempotency-Key": "chunk-upload-source-invalid-failure",
                "If-Match": '"' + ("0" * 64) + '"',
            },
            json=chunk_body,
        )
        success = client.put(
            f"/v1/projects/{project['id']}/workspace-uploads/{upload.json()['id']}/chunk",
            headers={
                **AUTH,
                "Idempotency-Key": target_key,
                "If-Match": upload.headers["etag"],
            },
            json=chunk_body,
        )
        assert stale.status_code == 412
        assert success.status_code == 200

    _inject_duplicate_failed_chunk(
        tmp_path,
        project_id=project["id"],
        upload_id=upload.json()["id"],
        upload_etag=upload.headers["etag"],
        idempotency_key=target_key,
        chunk_body=chunk_body,
        error_payload=stale.json(),
    )
    with sqlite3.connect(tmp_path / "core-control-v1" / "provider.sqlite3") as connection:
        connection.execute(
            "UPDATE idempotency_records SET response_json = ? "
            "WHERE operation_id = ? AND idempotency_key = ?",
            (b"{}", "putCoreWorkspaceUploadChunkV1", target_key),
        )

    with pytest.raises(StoreCorruptionError, match="persisted WorkspaceUploadSessionV1"):
        CoreControlStoreV1(tmp_path)
    with sqlite3.connect(tmp_path / "core-control-v1" / "provider.sqlite3") as connection:
        remaining = connection.execute(
            "SELECT count(*) FROM failed_idempotency_records "
            "WHERE operation_id = ? AND idempotency_key = ?",
            ("putCoreWorkspaceUploadChunkV1", target_key),
        ).fetchone()[0]
    assert remaining == 1


def test_startup_rejects_cross_upload_success_before_deleting_failed_audit(
    tmp_path: Path,
) -> None:
    archive = _workspace_archive()
    payload = _project_create(archive=archive)
    target_key = "chunk-upload-cross-resource-target"
    source_key = "chunk-upload-cross-resource-source"
    with TestClient(_app(tmp_path)) as client:
        project, project_etag = _create_project(client, payload)
        uploads = []
        for suffix in ("target", "source"):
            upload = client.post(
                f"/v1/projects/{project['id']}/workspace-uploads",
                headers={
                    **AUTH,
                    "Idempotency-Key": f"begin-upload-cross-resource-{suffix}",
                    "If-Match": project_etag,
                },
                json={
                    "schema_version": "1",
                    "project_snapshot": project["current_project_snapshot"],
                    "archive": payload["workspace"]["archive"],
                    "base_workspace_snapshot": None,
                },
            )
            assert upload.status_code == 201, upload.text
            uploads.append(upload)
        target_upload, source_upload = uploads
        chunk_body = _chunk(archive, offset=0)
        stale = client.put(
            f"/v1/projects/{project['id']}/workspace-uploads/{target_upload.json()['id']}/chunk",
            headers={
                **AUTH,
                "Idempotency-Key": "chunk-upload-cross-resource-failure-source",
                "If-Match": '"' + ("0" * 64) + '"',
            },
            json=chunk_body,
        )
        target = client.put(
            f"/v1/projects/{project['id']}/workspace-uploads/{target_upload.json()['id']}/chunk",
            headers={
                **AUTH,
                "Idempotency-Key": target_key,
                "If-Match": target_upload.headers["etag"],
            },
            json=chunk_body,
        )
        source = client.put(
            f"/v1/projects/{project['id']}/workspace-uploads/{source_upload.json()['id']}/chunk",
            headers={
                **AUTH,
                "Idempotency-Key": source_key,
                "If-Match": source_upload.headers["etag"],
            },
            json=chunk_body,
        )
        assert stale.status_code == 412
        assert target.status_code == 200
        assert source.status_code == 200

    _inject_duplicate_failed_chunk(
        tmp_path,
        project_id=project["id"],
        upload_id=target_upload.json()["id"],
        upload_etag=target_upload.headers["etag"],
        idempotency_key=target_key,
        chunk_body=chunk_body,
        error_payload=stale.json(),
    )
    with sqlite3.connect(tmp_path / "core-control-v1" / "provider.sqlite3") as connection:
        source_row = connection.execute(
            "SELECT response_json, etag FROM idempotency_records "
            "WHERE operation_id = ? AND idempotency_key = ?",
            ("putCoreWorkspaceUploadChunkV1", source_key),
        ).fetchone()
        assert source_row is not None
        connection.execute(
            "UPDATE idempotency_records SET response_json = ?, etag = ? "
            "WHERE operation_id = ? AND idempotency_key = ?",
            (
                source_row[0],
                source_row[1],
                "putCoreWorkspaceUploadChunkV1",
                target_key,
            ),
        )

    def restart() -> None:
        store = CoreControlStoreV1(tmp_path)
        store.close()

    with pytest.raises(StoreCorruptionError, match="idempotency response semantic binding"):
        restart()
    with sqlite3.connect(tmp_path / "core-control-v1" / "provider.sqlite3") as connection:
        remaining = connection.execute(
            "SELECT count(*) FROM failed_idempotency_records "
            "WHERE operation_id = ? AND idempotency_key = ?",
            ("putCoreWorkspaceUploadChunkV1", target_key),
        ).fetchone()[0]
    assert remaining == 1


def test_success_idempotency_semantics_are_closed_per_operation(tmp_path: Path) -> None:
    archive = _workspace_archive()
    payload = _project_create(archive=archive)
    state_root = tmp_path / "state"
    registry = verified_builtin_registry(tmp_path / "registry")
    keys = {
        "create_a": "semantic-create-project-a",
        "patch_a": "semantic-patch-project-a",
        "begin_finalize_a": "semantic-begin-finalize-a",
        "chunk_a": "semantic-chunk-a",
        "finalize_a": "semantic-finalize-a",
        "begin_abort_a": "semantic-begin-abort-a",
        "abort_a": "semantic-abort-a",
        "validate_a": "semantic-validate-a",
        "create_b": "semantic-create-project-b",
        "begin_b": "semantic-begin-b",
        "delete_b": "semantic-delete-b",
    }
    with TestClient(_app(state_root, registry=registry)) as client:
        project_a, etag_a = _create_project(client, payload, idempotency_key=keys["create_a"])
        patched_a = client.patch(
            f"/v1/projects/{project_a['id']}",
            headers={
                **AUTH,
                "Idempotency-Key": keys["patch_a"],
                "If-Match": etag_a,
            },
            json={"schema_version": "1", "name": "Semantic project A"},
        )
        assert patched_a.status_code == 200, patched_a.text
        project_a = patched_a.json()
        etag_a = patched_a.headers["etag"]
        upload_a = client.post(
            f"/v1/projects/{project_a['id']}/workspace-uploads",
            headers={
                **AUTH,
                "Idempotency-Key": keys["begin_finalize_a"],
                "If-Match": etag_a,
            },
            json={
                "schema_version": "1",
                "project_snapshot": project_a["current_project_snapshot"],
                "archive": payload["workspace"]["archive"],
                "base_workspace_snapshot": None,
            },
        )
        assert upload_a.status_code == 201, upload_a.text
        chunk_a = client.put(
            f"/v1/projects/{project_a['id']}/workspace-uploads/{upload_a.json()['id']}/chunk",
            headers={
                **AUTH,
                "Idempotency-Key": keys["chunk_a"],
                "If-Match": upload_a.headers["etag"],
            },
            json=_chunk(archive, offset=0),
        )
        assert chunk_a.status_code == 200, chunk_a.text
        finalized_a = client.post(
            f"/v1/projects/{project_a['id']}/workspace-uploads/{upload_a.json()['id']}/finalize",
            headers={
                **AUTH,
                "Idempotency-Key": keys["finalize_a"],
                "If-Match": chunk_a.headers["etag"],
                "If-Project-Match": etag_a,
            },
            json={
                "schema_version": "1",
                "content_sha256": hashlib.sha256(archive).hexdigest(),
            },
        )
        assert finalized_a.status_code == 201, finalized_a.text
        project_a = finalized_a.json()["project"]
        etag_a = project_a["etag"]
        abort_upload_a = client.post(
            f"/v1/projects/{project_a['id']}/workspace-uploads",
            headers={
                **AUTH,
                "Idempotency-Key": keys["begin_abort_a"],
                "If-Match": etag_a,
            },
            json={
                "schema_version": "1",
                "project_snapshot": project_a["current_project_snapshot"],
                "archive": payload["workspace"]["archive"],
                "base_workspace_snapshot": project_a["current_workspace_snapshot"],
            },
        )
        assert abort_upload_a.status_code == 201, abort_upload_a.text
        aborted_a = client.post(
            f"/v1/projects/{project_a['id']}/workspace-uploads/"
            f"{abort_upload_a.json()['id']}/abort",
            headers={
                **AUTH,
                "Idempotency-Key": keys["abort_a"],
                "If-Match": abort_upload_a.headers["etag"],
            },
            json={"schema_version": "1", "reason": "semantic validator fixture"},
        )
        assert aborted_a.status_code == 200, aborted_a.text
        validated_a = client.post(
            f"/v1/projects/{project_a['id']}/validate",
            headers={**AUTH, "Idempotency-Key": keys["validate_a"]},
            json={
                "schema_version": "1",
                "project_snapshot": project_a["current_project_snapshot"],
                "workspace_snapshot": project_a["current_workspace_snapshot"],
                "expected_registry_digest": registry.snapshot.registry_digest,
            },
        )
        assert validated_a.status_code == 200, validated_a.text

        project_b, etag_b = _create_project(client, payload, idempotency_key=keys["create_b"])
        upload_b = client.post(
            f"/v1/projects/{project_b['id']}/workspace-uploads",
            headers={
                **AUTH,
                "Idempotency-Key": keys["begin_b"],
                "If-Match": etag_b,
            },
            json={
                "schema_version": "1",
                "project_snapshot": project_b["current_project_snapshot"],
                "archive": payload["workspace"]["archive"],
                "base_workspace_snapshot": None,
            },
        )
        assert upload_b.status_code == 201, upload_b.text
        deleted_b = client.delete(
            f"/v1/projects/{project_b['id']}",
            headers={
                **AUTH,
                "Idempotency-Key": keys["delete_b"],
                "If-Match": etag_b,
            },
        )
        assert deleted_b.status_code == 204, deleted_b.text

    database = state_root / "core-control-v1" / "provider.sqlite3"
    with sqlite3.connect(database) as connection:
        connection.row_factory = sqlite3.Row
        rows = {
            row["idempotency_key"]: dict(row)
            for row in connection.execute("SELECT * FROM idempotency_records").fetchall()
        }

    selected_keys = (
        "create_a",
        "patch_a",
        "begin_finalize_a",
        "chunk_a",
        "finalize_a",
        "abort_a",
        "validate_a",
        "delete_b",
    )
    for name in selected_keys:
        store_module._validate_idempotency_row(rows[keys[name]])

    def transplant(target: str, source: str) -> dict[str, object]:
        row = dict(rows[keys[target]])
        row["response_json"] = rows[keys[source]]["response_json"]
        row["etag"] = rows[keys[source]]["etag"]
        return row

    bad_create = dict(rows[keys["create_a"]])
    bad_create["resource_scope"] = project_a["id"]
    bad_patch = transplant("patch_a", "create_b")
    bad_delete = dict(rows[keys["delete_b"]])
    bad_delete["resource_scope"] = upload_b.json()["id"]
    bad_upload_create = transplant("begin_finalize_a", "begin_b")
    bad_chunk = dict(rows[keys["chunk_a"]])
    bad_chunk["resource_scope"] = f"{project_a['id']}:{abort_upload_a.json()['id']}"
    bad_finalize = dict(rows[keys["finalize_a"]])
    bad_finalize["resource_scope"] = f"{project_b['id']}:{upload_b.json()['id']}"
    bad_abort = transplant("abort_a", "begin_abort_a")
    bad_validation = dict(rows[keys["validate_a"]])
    validation_payload = json.loads(bytes(bad_validation["response_json"]))
    validation_payload["valid"] = False
    validation_payload["checks"][0]["status"] = "blocking"
    bad_validation["response_json"] = store_module._canonical_bytes(validation_payload)

    cases = {
        "project create scope": bad_create,
        "project patch resource": bad_patch,
        "project delete scope": bad_delete,
        "upload create parent": bad_upload_create,
        "upload chunk resource": bad_chunk,
        "upload finalize resource": bad_finalize,
        "upload abort status": bad_abort,
        "project validation result": bad_validation,
    }

    def rewrite_request(target: str, **updates: object) -> dict[str, object]:
        row = dict(rows[keys[target]])
        request = json.loads(bytes(row["request_json"]))
        request.update(updates)
        row["request_json"] = store_module._canonical_bytes(request)
        row["request_digest"] = store_module._idempotency_request_digest(
            row["operation_id"],
            row["resource_scope"],
            row["request_json"],
            bytes(row["semantic_headers_json"]),
        )
        return row

    cases.update(
        {
            "upload chunk request relation": rewrite_request("chunk_a", offset=512),
            "upload finalize request relation": rewrite_request(
                "finalize_a", content_sha256="0" * 64
            ),
            "project validation registry relation": rewrite_request(
                "validate_a", expected_registry_digest="0" * 64
            ),
        }
    )
    for label, row in cases.items():
        try:
            store_module._validate_idempotency_row(row)
        except StoreCorruptionError as exc:
            assert "idempotency response semantic binding" in str(
                exc
            ) or "idempotency request digest" in str(exc), label
        else:
            pytest.fail(f"accepted invalid {label} idempotency row")


def test_workspace_finalize_rejects_same_inode_mutation_during_verification(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    archive = _workspace_archive()
    with TestClient(_app(tmp_path)) as client:
        project, project_etag = _create_project(client, _project_create(archive=archive))
        upload = client.post(
            f"/v1/projects/{project['id']}/workspace-uploads",
            headers={
                **AUTH,
                "Idempotency-Key": "begin-upload-inode-mutation",
                "If-Match": project_etag,
            },
            json={
                "schema_version": "1",
                "project_snapshot": project["current_project_snapshot"],
                "archive": project["workspace"]["archive"],
                "base_workspace_snapshot": None,
            },
        )
        chunk = client.put(
            f"/v1/projects/{project['id']}/workspace-uploads/{upload.json()['id']}/chunk",
            headers={
                **AUTH,
                "Idempotency-Key": "chunk-upload-inode-mutation",
                "If-Match": upload.headers["etag"],
            },
            json=_chunk(archive, offset=0),
        )
        upload_path = (
            tmp_path / "core-control-v1" / "workspace-uploads" / f"{upload.json()['id']}.part"
        )
        initial_identity = upload_path.stat()
        real_parse = workspace_module._parse_archive

        def mutate_same_inode(stream, declaration, **kwargs) -> None:
            with upload_path.open("r+b", buffering=0) as archive_file:
                archive_file.seek(1024)
                archive_file.write(b"X")
                os.fsync(archive_file.fileno())
            mutated_identity = upload_path.stat()
            assert mutated_identity.st_ino == initial_identity.st_ino
            assert mutated_identity.st_size == initial_identity.st_size
            real_parse(stream, declaration, **kwargs)

        monkeypatch.setattr(workspace_module, "_parse_archive", mutate_same_inode)
        finalized = client.post(
            f"/v1/projects/{project['id']}/workspace-uploads/{upload.json()['id']}/finalize",
            headers={
                **AUTH,
                "Idempotency-Key": "finalize-upload-inode-mutation",
                "If-Match": chunk.headers["etag"],
                "If-Project-Match": project_etag,
            },
            json={"schema_version": "1", "content_sha256": hashlib.sha256(archive).hexdigest()},
        )

        assert finalized.status_code == 409
        assert finalized.json()["code"] == "workspace_archive_invalid"
        assert list((tmp_path / "core-control-v1" / "workspace-snapshots").iterdir()) == []


def test_workspace_finalize_rejects_nonprivate_archive_mode(tmp_path: Path) -> None:
    archive = _workspace_archive()
    with TestClient(_app(tmp_path)) as client:
        project, project_etag = _create_project(client, _project_create(archive=archive))
        upload = client.post(
            f"/v1/projects/{project['id']}/workspace-uploads",
            headers={
                **AUTH,
                "Idempotency-Key": "begin-upload-mode-0644",
                "If-Match": project_etag,
            },
            json={
                "schema_version": "1",
                "project_snapshot": project["current_project_snapshot"],
                "archive": project["workspace"]["archive"],
                "base_workspace_snapshot": None,
            },
        )
        chunk = client.put(
            f"/v1/projects/{project['id']}/workspace-uploads/{upload.json()['id']}/chunk",
            headers={
                **AUTH,
                "Idempotency-Key": "chunk-upload-mode-0644",
                "If-Match": upload.headers["etag"],
            },
            json=_chunk(archive, offset=0),
        )
        upload_path = (
            tmp_path / "core-control-v1" / "workspace-uploads" / f"{upload.json()['id']}.part"
        )
        upload_path.chmod(0o644)
        finalized = client.post(
            f"/v1/projects/{project['id']}/workspace-uploads/{upload.json()['id']}/finalize",
            headers={
                **AUTH,
                "Idempotency-Key": "finalize-upload-mode-0644",
                "If-Match": chunk.headers["etag"],
                "If-Project-Match": project_etag,
            },
            json={"schema_version": "1", "content_sha256": hashlib.sha256(archive).hexdigest()},
        )

        assert finalized.status_code == 409
        assert finalized.json()["code"] == "workspace_archive_invalid"
        assert list((tmp_path / "core-control-v1" / "workspace-snapshots").iterdir()) == []


def test_workspace_root_replacement_fails_operations_and_second_owner(tmp_path: Path) -> None:
    store = CoreControlStoreV1(tmp_path)
    snapshot_root = tmp_path / "core-control-v1" / "workspace-snapshots"
    displaced_root = tmp_path / "core-control-v1" / "workspace-snapshots.displaced"
    try:
        snapshot_root.rename(displaced_root)
        snapshot_root.mkdir(mode=0o700)

        with pytest.raises(StoreCorruptionError, match="binding changed"):
            store.list_projects(limit=1, after=None, sort="created_at", direction="asc")
        with pytest.raises(CoreControlStoreError, match="already owned"):
            CoreControlStoreV1(tmp_path)
    finally:
        store.close()


def test_provider_lock_unlink_fails_operations_and_second_owner(tmp_path: Path) -> None:
    store = CoreControlStoreV1(tmp_path)
    lock_path = tmp_path / "core-control-v1" / "provider.lock"
    try:
        lock_path.unlink()

        with pytest.raises(StoreCorruptionError, match="private file is unsafe"):
            store.list_projects(limit=1, after=None, sort="created_at", direction="asc")
        with pytest.raises(CoreControlStoreError, match="already owned"):
            CoreControlStoreV1(tmp_path)
        assert not lock_path.exists()
    finally:
        store.close()


@pytest.mark.parametrize("install_replacement", [False, True])
def test_provider_root_replacement_cannot_admit_second_owner(
    tmp_path: Path, install_replacement: bool
) -> None:
    store = CoreControlStoreV1(tmp_path)
    provider_root = tmp_path / "core-control-v1"
    displaced_root = tmp_path / "core-control-v1.displaced"
    provider_root.rename(displaced_root)
    if install_replacement:
        provider_root.mkdir(mode=0o700)
    try:
        with pytest.raises(StoreCorruptionError, match="binding"):
            store.list_projects(limit=1, after=None, sort="created_at", direction="asc")
        second_owner = subprocess.run(
            [
                sys.executable,
                "-c",
                "\n".join(
                    (
                        "import sys",
                        "from openevo.backend.contracts.v1.store import "
                        "CoreControlStoreError, CoreControlStoreV1",
                        "try:",
                        "    store = CoreControlStoreV1(sys.argv[1])",
                        "except CoreControlStoreError as exc:",
                        "    raise SystemExit(0 if 'already owned' in str(exc) else 2)",
                        "else:",
                        "    store.close()",
                        "    raise SystemExit(3)",
                    )
                ),
                os.fspath(tmp_path),
            ],
            check=False,
            capture_output=True,
            text=True,
        )
        assert second_owner.returncode == 0, second_owner.stderr
        if install_replacement:
            assert list(provider_root.iterdir()) == []
        else:
            assert not provider_root.exists()
    finally:
        store.close()
        if provider_root.exists():
            provider_root.rmdir()
        displaced_root.rename(provider_root)


@pytest.mark.parametrize("damage", ["corrupt", "missing"])
def test_workspace_recovery_rejects_invalid_published_snapshot(
    tmp_path: Path, damage: str
) -> None:
    result = _finalize_workspace(tmp_path, _workspace_archive(), key_suffix=damage)
    snapshot = result["publication"]["workspace_snapshot"]["id"]
    published_file = (
        tmp_path / "core-control-v1" / "workspace-snapshots" / snapshot / "src" / "AGENTS.md"
    )
    if damage == "corrupt":
        published_file.write_bytes(b"corrupt\n")
    else:
        published_file.unlink()
        published_file.parent.rmdir()
        published_file.parent.parent.rmdir()

    try:
        recovered = CoreControlStoreV1(tmp_path)
    except StoreCorruptionError:
        pass
    else:
        recovered.close()
        pytest.fail(f"{damage} published workspace snapshot was accepted")


def test_workspace_recovery_removes_orphans_without_following_symlinks(tmp_path: Path) -> None:
    result = _finalize_workspace(tmp_path, _workspace_archive(), key_suffix="orphan")
    snapshot_root = tmp_path / "core-control-v1" / "workspace-snapshots"
    orphan = snapshot_root / "workspace-snapshot-orphan"
    orphan.mkdir(mode=0o700)
    (orphan / "uncommitted.txt").write_text("uncommitted", encoding="utf-8")
    outside = tmp_path / "outside"
    outside.mkdir()
    outside_file = outside / "keep.txt"
    outside_file.write_text("keep", encoding="utf-8")
    symlink = snapshot_root / "workspace-snapshot-symlink"
    symlink.symlink_to(outside, target_is_directory=True)

    with TestClient(_app(tmp_path)) as client:
        project = client.get(f"/v1/projects/{result['project_id']}", headers=AUTH)
        assert project.status_code == 200

    assert not orphan.exists()
    assert not os.path.lexists(symlink)
    assert outside_file.read_text(encoding="utf-8") == "keep"


@pytest.mark.parametrize("directory", [False, True])
def test_workspace_finalize_maps_root_dot_entry_to_archive_rejection(
    tmp_path: Path, directory: bool
) -> None:
    body = b"" if directory else b"root entry\n"
    archive = b"".join(
        (
            _tar_header(".", body_size=len(body), directory=directory),
            body,
            b"\0" * ((512 - len(body) % 512) % 512),
            b"\0" * 1024,
        )
    )
    app = _app(tmp_path)
    with TestClient(app, raise_server_exceptions=False) as client:
        payload = _project_create(
            archive=archive,
            archive_entry_count=1,
            extracted_byte_size=len(body),
        )
        project, project_etag = _create_project(client, payload)
        upload = client.post(
            f"/v1/projects/{project['id']}/workspace-uploads",
            headers={**AUTH, "Idempotency-Key": "begin-upload-root-dot", "If-Match": project_etag},
            json={
                "schema_version": "1",
                "project_snapshot": project["current_project_snapshot"],
                "archive": payload["workspace"]["archive"],
                "base_workspace_snapshot": None,
            },
        )
        chunk = client.put(
            f"/v1/projects/{project['id']}/workspace-uploads/{upload.json()['id']}/chunk",
            headers={
                **AUTH,
                "Idempotency-Key": "chunk-upload-root-dot",
                "If-Match": upload.headers["etag"],
            },
            json=_chunk(archive, offset=0),
        )
        finalized = client.post(
            f"/v1/projects/{project['id']}/workspace-uploads/{upload.json()['id']}/finalize",
            headers={
                **AUTH,
                "Idempotency-Key": "finalize-upload-root-dot",
                "If-Match": chunk.headers["etag"],
                "If-Project-Match": project_etag,
            },
            json={"schema_version": "1", "content_sha256": hashlib.sha256(archive).hexdigest()},
        )

        assert finalized.status_code == 409
        assert finalized.json()["code"] == "workspace_archive_invalid"


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


def test_project_validation_uses_the_exact_persisted_execution_profile(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    registry = verified_builtin_registry(tmp_path / "registry")
    observed = []

    def capture_profile(*args, **kwargs) -> None:
        del args
        observed.append(kwargs["execution_profile"])

    monkeypatch.setattr(provider_module, "validate_project_evolution_selections", capture_profile)
    payload = _project_create(
        execution_mode="self-deployed",
        capture_mode="token_level",
        harness_id="openhands",
    )
    with TestClient(_app(tmp_path / "state", registry=registry)) as client:
        project, _ = _create_project(client, payload)
        validated = client.post(
            f"/v1/projects/{project['id']}/validate",
            headers={**AUTH, "Idempotency-Key": "validate-exact-profile"},
            json={
                "schema_version": "1",
                "project_snapshot": project["current_project_snapshot"],
                "workspace_snapshot": project["current_workspace_snapshot"],
                "expected_registry_digest": registry.snapshot.registry_digest,
            },
        )

    assert validated.status_code == 200, validated.text
    assert len(observed) == 1
    assert observed[0].execution_mode.value == "self_deployed"
    assert observed[0].capture_mode.value == "token_level"
    assert observed[0].harness_id == "openhands"


def test_success_idempotency_persists_and_revalidates_canonical_request_envelope(
    tmp_path: Path,
) -> None:
    with TestClient(_app(tmp_path)) as client:
        response = client.post(
            "/v1/projects",
            headers={**AUTH, "Idempotency-Key": "create-envelope-binding"},
            json=_project_create(),
        )
        assert response.status_code == 201

    database = tmp_path / "core-control-v1" / "provider.sqlite3"
    with sqlite3.connect(database) as connection:
        connection.row_factory = sqlite3.Row
        row = connection.execute(
            "SELECT * FROM idempotency_records WHERE operation_id = ?",
            ("createCoreProjectV1",),
        ).fetchone()
        assert row is not None
        assert bytes(row["semantic_headers_json"]) == b"{}"
        request = json.loads(bytes(row["request_json"]))
        request["name"] = "A different canonical request"
        request_json = store_module._canonical_bytes(request)
        request_digest = store_module._idempotency_request_digest(
            row["operation_id"],
            row["resource_scope"],
            request_json,
            bytes(row["semantic_headers_json"]),
        )
        connection.execute(
            "UPDATE idempotency_records SET request_json = ?, request_digest = ? "
            "WHERE operation_id = ? AND idempotency_key = ?",
            (
                request_json,
                request_digest,
                "createCoreProjectV1",
                "create-envelope-binding",
            ),
        )

    with pytest.raises(StoreCorruptionError, match="request.*response"):
        CoreControlStoreV1(tmp_path)


def test_startup_recomputes_project_and_task_snapshot_bindings(tmp_path: Path) -> None:
    with TestClient(_app(tmp_path)) as client:
        project, _ = _create_project(client, _project_create())

    database = tmp_path / "core-control-v1" / "provider.sqlite3"
    with sqlite3.connect(database) as connection:
        connection.row_factory = sqlite3.Row
        row = connection.execute(
            "SELECT * FROM projects WHERE project_id = ?", (project["id"],)
        ).fetchone()
        assert row is not None
        persisted = store_module._validate_bytes(store_module.m.ProjectV1, row["document_json"])
        data = persisted.model_dump(mode="python", exclude={"etag"})
        data["current_task_snapshot"]["content_sha256"] = "0" * 64
        damaged = store_module._model_with_etag(
            store_module.m.ProjectV1, data, version=int(row["resource_version"])
        )
        connection.execute(
            "UPDATE projects SET document_json = ? WHERE project_id = ?",
            (store_module._model_bytes(damaged), project["id"]),
        )

    with pytest.raises(StoreCorruptionError, match="task snapshot"):
        CoreControlStoreV1(tmp_path)


def test_startup_binds_workspace_publication_to_an_upload_from_the_same_project(
    tmp_path: Path,
) -> None:
    finalized = _finalize_workspace(tmp_path, _workspace_archive(), key_suffix="project-bind")
    with TestClient(_app(tmp_path)) as client:
        other, _ = _create_project(
            client,
            _project_create(),
            idempotency_key="create-publication-other-project",
        )

    upload_id = finalized["upload"]["id"]
    database = tmp_path / "core-control-v1" / "provider.sqlite3"
    with sqlite3.connect(database) as connection:
        connection.row_factory = sqlite3.Row
        row = connection.execute(
            "SELECT * FROM workspace_uploads WHERE upload_id = ?", (upload_id,)
        ).fetchone()
        assert row is not None
        upload = store_module._validate_bytes(
            store_module.m.WorkspaceUploadSessionV1, row["document_json"]
        )
        data = upload.model_dump(mode="python", exclude={"etag"})
        data["project_id"] = other["id"]
        damaged = store_module._model_with_etag(
            store_module.m.WorkspaceUploadSessionV1,
            data,
            version=int(row["resource_version"]),
        )
        connection.execute(
            "UPDATE workspace_uploads SET project_id = ?, document_json = ? WHERE upload_id = ?",
            (other["id"], store_module._model_bytes(damaged), upload_id),
        )

    with pytest.raises(StoreCorruptionError, match="same project"):
        CoreControlStoreV1(tmp_path)


def test_startup_rejects_shared_workspace_publication_regardless_of_project_order(
    tmp_path: Path,
) -> None:
    archive = _workspace_archive()
    payload = _project_create(archive=archive)
    with TestClient(_app(tmp_path)) as client:
        first_project, _ = _create_project(
            client,
            payload,
            idempotency_key="create-publication-first-owner",
        )
    finalized = _finalize_workspace(tmp_path, archive, key_suffix="shared-owner")

    database = tmp_path / "core-control-v1" / "provider.sqlite3"
    with sqlite3.connect(database) as connection:
        connection.row_factory = sqlite3.Row
        signing_key = bytes(
            connection.execute("SELECT value FROM metadata WHERE key = 'signing_key'").fetchone()[
                0
            ]
        )
        row = connection.execute(
            "SELECT * FROM projects WHERE project_id = ?", (first_project["id"],)
        ).fetchone()
        assert row is not None
        project = store_module._validate_bytes(store_module.m.ProjectV1, row["document_json"])
        data = project.model_dump(mode="python", exclude={"etag", "current_project_snapshot"})
        data.update(
            current_workspace_snapshot=finalized["publication"]["workspace_snapshot"],
            workspace_publication=finalized["publication"],
        )
        data["current_project_snapshot"] = store_module._snapshot(
            signing_key,
            store_module.m.SnapshotKind.PROJECT,
            store_module._project_snapshot_payload(data),
            project.current_project_snapshot.created_at,
        )
        damaged = store_module._model_with_etag(
            store_module.m.ProjectV1,
            data,
            version=int(row["resource_version"]),
        )
        connection.execute(
            "UPDATE projects SET document_json = ? WHERE project_id = ?",
            (store_module._model_bytes(damaged), first_project["id"]),
        )

    with pytest.raises(StoreCorruptionError, match="workspace publication.*owner"):
        CoreControlStoreV1(tmp_path)


@pytest.mark.parametrize("damage", ["cursor", "canonical_frame"])
def test_startup_authenticates_event_cursor_and_canonical_frame(
    tmp_path: Path, damage: str
) -> None:
    with TestClient(_app(tmp_path)) as client:
        _create_project(client, _project_create())

    database = tmp_path / "core-control-v1" / "provider.sqlite3"
    with sqlite3.connect(database) as connection:
        connection.row_factory = sqlite3.Row
        row = connection.execute("SELECT * FROM events ORDER BY sequence LIMIT 1").fetchone()
        assert row is not None
        if damage == "canonical_frame":
            connection.execute(
                "UPDATE events SET frame_json = ? WHERE sequence = ?",
                (bytes(row["frame_json"]) + b" ", row["sequence"]),
            )
        else:
            frame = json.loads(bytes(row["frame_json"]))
            forged = f"evt.v1.{row['sequence']}." + ("0" * 24)
            frame["id"] = forged
            frame["data"]["id"] = forged
            connection.execute(
                "UPDATE events SET event_id = ?, frame_json = ? WHERE sequence = ?",
                (forged, store_module._canonical_bytes(frame), row["sequence"]),
            )

    with pytest.raises(StoreCorruptionError, match="event"):
        CoreControlStoreV1(tmp_path)


@pytest.mark.parametrize("extra_kind", ["table", "trigger"])
def test_startup_rejects_noncanonical_sqlite_schema(tmp_path: Path, extra_kind: str) -> None:
    with TestClient(_app(tmp_path)) as client:
        _create_project(client, _project_create())

    database = tmp_path / "core-control-v1" / "provider.sqlite3"
    with sqlite3.connect(database) as connection:
        if extra_kind == "table":
            connection.execute("CREATE TABLE injected(value TEXT)")
        else:
            connection.execute(
                "CREATE TRIGGER injected AFTER INSERT ON projects BEGIN SELECT 1; END"
            )

    with pytest.raises(StoreCorruptionError, match="schema fingerprint"):
        CoreControlStoreV1(tmp_path)


def test_live_store_rejects_database_hardlink_alias(tmp_path: Path) -> None:
    store = CoreControlStoreV1(tmp_path)
    database = tmp_path / "core-control-v1" / "provider.sqlite3"
    alias = tmp_path / "provider-alias.sqlite3"
    try:
        os.link(database, alias)
        with pytest.raises(StoreCorruptionError, match="database"):
            store.list_projects(limit=1, after=None, sort="created_at", direction="asc")
    finally:
        alias.unlink(missing_ok=True)
        store.close()


def test_sqlite_connection_remains_bound_to_authority_inode_during_path_swap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = CoreControlStoreV1(tmp_path)
    authoritative_key = store._signing_key
    store.close()

    database = tmp_path / "core-control-v1" / "provider.sqlite3"
    replacement = database.with_name("replacement.sqlite3")
    displaced = database.with_name("authoritative.sqlite3")
    shutil.copy2(database, replacement)
    replacement_key = b"r" * 32
    with sqlite3.connect(replacement) as connection:
        connection.execute(
            "UPDATE metadata SET value = ? WHERE key = 'signing_key'",
            (replacement_key,),
        )

    real_connect = store_module.sqlite3.connect
    swapped = False

    def swapping_connect(target, *args, **kwargs):
        nonlocal swapped
        target_text = os.fspath(target)
        is_provider = target_text != ":memory:" and (
            target_text.endswith("provider.sqlite3") or "/proc/self/fd/" in target_text
        )
        if not swapped and is_provider:
            swapped = True
            database.rename(displaced)
            replacement.rename(database)
            try:
                connection = real_connect(target, *args, **kwargs)
            finally:
                database.rename(replacement)
                displaced.rename(database)
            return connection
        return real_connect(target, *args, **kwargs)

    monkeypatch.setattr(store_module.sqlite3, "connect", swapping_connect)
    reopened = CoreControlStoreV1(tmp_path)
    try:
        assert swapped
        assert reopened._signing_key == authoritative_key
        assert reopened._signing_key != replacement_key
    finally:
        reopened.close()


def test_startup_metadata_is_length_guarded_and_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = CoreControlStoreV1(tmp_path)
    store.close()
    database = tmp_path / "core-control-v1" / "provider.sqlite3"
    with sqlite3.connect(database) as connection:
        connection.execute("INSERT INTO metadata VALUES ('unknown', zeroblob(32))")

    statements: list[str] = []
    real_connect = store_module.sqlite3.connect

    def traced_connect(target, *args, **kwargs):
        connection = real_connect(target, *args, **kwargs)
        if os.fspath(target) != ":memory:":
            connection.set_trace_callback(statements.append)
        return connection

    monkeypatch.setattr(store_module.sqlite3, "connect", traced_connect)
    with pytest.raises(StoreCorruptionError, match="metadata"):
        CoreControlStoreV1(tmp_path)
    normalized = [" ".join(statement.lower().split()) for statement in statements]
    assert not any(
        statement == "select value from metadata where key = 'signing_key'"
        for statement in normalized
    )
    assert any("length(cast(value as blob))" in statement for statement in normalized)


def test_startup_length_guards_text_columns_before_recovery_fetch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with TestClient(_app(tmp_path)) as client:
        _create_project(client, _project_create())

    monkeypatch.setattr(store_module, "_MAX_STARTUP_VALUE_BYTES", 48)
    with pytest.raises(StoreCorruptionError, match="projects recovery quota"):
        CoreControlStoreV1(tmp_path)


def test_startup_rejects_database_symlink_and_foreign_key_corruption(
    tmp_path: Path,
) -> None:
    with TestClient(_app(tmp_path)) as client:
        project, etag = _create_project(client, _project_create(archive=_workspace_archive()))
        upload = client.post(
            f"/v1/projects/{project['id']}/workspace-uploads",
            headers={
                **AUTH,
                "Idempotency-Key": "begin-foreign-key-corruption",
                "If-Match": etag,
            },
            json={
                "schema_version": "1",
                "project_snapshot": project["current_project_snapshot"],
                "archive": project["workspace"]["archive"],
                "base_workspace_snapshot": None,
            },
        )
        assert upload.status_code == 201

    database = tmp_path / "core-control-v1" / "provider.sqlite3"
    with sqlite3.connect(database) as connection:
        connection.execute("PRAGMA foreign_keys = OFF")
        connection.execute(
            "UPDATE workspace_uploads SET project_id = ? WHERE upload_id = ?",
            ("project-" + ("0" * 32), upload.json()["id"]),
        )
    with pytest.raises(StoreCorruptionError, match="foreign key"):
        CoreControlStoreV1(tmp_path)

    with sqlite3.connect(database) as connection:
        connection.execute("PRAGMA foreign_keys = OFF")
        connection.execute(
            "UPDATE workspace_uploads SET project_id = ? WHERE upload_id = ?",
            (project["id"], upload.json()["id"]),
        )
    displaced = database.with_suffix(".real")
    database.rename(displaced)
    database.symlink_to(displaced.name)
    try:
        with pytest.raises(CoreControlStoreError) as captured:
            CoreControlStoreV1(tmp_path)
        assert str(database) not in str(captured.value)
    finally:
        database.unlink()
        displaced.rename(database)


def test_startup_wraps_filesystem_errors_without_path_disclosure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    private_path = "/srv/private/core-control-state"

    def fail_quota(*args, **kwargs):
        del args, kwargs
        raise OSError(private_path)

    monkeypatch.setattr(store_module, "_verify_managed_disk_quota", fail_quota)
    with pytest.raises(CoreControlStoreError) as captured:
        CoreControlStoreV1(tmp_path)
    assert private_path not in str(captured.value)


def test_sync_store_work_does_not_block_health_on_the_asgi_event_loop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    app = _app(tmp_path)
    store = app.state.core_control_provider.store
    original = store.list_projects
    release = threading.Event()

    def slow_list_projects(**kwargs):
        release.wait(timeout=2)
        return original(**kwargs)

    monkeypatch.setattr(store, "list_projects", slow_list_projects)

    async def exercise() -> float:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            started = time.monotonic()
            listing = asyncio.create_task(client.get("/v1/projects", headers=AUTH))
            await asyncio.sleep(0)
            health = await client.get("/health")
            elapsed = time.monotonic() - started
            release.set()
            listed = await listing
            assert health.status_code == 200
            assert listed.status_code == 200
            return elapsed

    timer = threading.Timer(0.75, release.set)
    timer.start()
    try:
        elapsed = asyncio.run(exercise())
    finally:
        release.set()
        timer.cancel()
        app.state.core_control_provider.close()
    assert elapsed < 0.5


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
