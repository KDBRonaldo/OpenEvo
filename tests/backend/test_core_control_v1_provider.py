from __future__ import annotations

import base64
import asyncio
from collections.abc import Mapping
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import hashlib
import hmac
import json
import os
from pathlib import Path
import shutil
import sqlite3
import stat
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
from openevo.backend.contracts.v1 import models as m
from openevo.backend.contracts.v1.models import (
    ApiErrorV1,
    SseFrameV1,
    WorkspaceArchiveDeclarationV1,
    WorkspaceUploadChunkV1,
)
import openevo.backend.contracts.v1.store as store_module
import openevo.backend.contracts.v1.provider as provider_module
import openevo.evolution.artifact_payloads as artifact_payloads_module
from openevo.backend.contracts.v1.store import (
    CoreControlStoreError,
    CoreControlStoreV1,
    IdempotencyConflictError,
    StoreCorruptionError,
)
from openevo.evolution.artifact_payloads import ArtifactPayloadService
from openevo.backend.run_control import RUN_OPERATION_IDS, CoreRunControlError
from openevo.backend.service_supervisor import (
    ServiceComponent,
    ServiceStatus,
    SupervisorError,
    SupervisorServiceSummary,
)
import openevo.backend.contracts.v1.workspace as workspace_module
from openevo.backend.contracts.v1.workspace import (
    WorkspaceArchiveError,
    verify_workspace_archive,
)
from desktop.sidecar.core_client_v1 import (
    CoreControlClientV1,
    CoreTunnelConnectionV1,
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
    service_supervisor=None,
    run_control=None,
    run_control_factory=None,
    evolution_artifact_root: Path | None = None,
    artifact_loader=None,
    event_replay_limit: int = 10_000,
    build_channel: str = "test",
):
    return create_core_control_app(
        state_root=state_root,
        bearer_token=TOKEN,
        build_version="0.1.0",
        source_commit="a" * 40,
        build_channel=build_channel,
        evolution_registry=registry,
        service_supervisor=service_supervisor,
        run_control=run_control,
        run_control_factory=run_control_factory,
        evolution_artifact_root=evolution_artifact_root,
        artifact_loader=artifact_loader,
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


def test_ready_project_delete_prunes_orphaned_revision_events_before_restart(
    tmp_path: Path,
) -> None:
    registry = verified_builtin_registry(tmp_path / "registry")
    with TestClient(_app(tmp_path, registry=registry)) as client:
        project, project_etag = _create_project(client, _project_create())
        other, _ = _create_project(
            client,
            _project_create(),
            idempotency_key="create-project-after-deleted-ready-project",
        )
        deleted = client.delete(
            f"/v1/projects/{project['id']}",
            headers={
                **AUTH,
                "Idempotency-Key": "delete-ready-project-with-revision",
                "If-Match": project_etag,
            },
        )
        assert deleted.status_code == 204, deleted.text

    with TestClient(_app(tmp_path, registry=registry)) as restarted:
        assert restarted.get(f"/v1/projects/{project['id']}", headers=AUTH).status_code == 404
        assert restarted.get(f"/v1/projects/{other['id']}", headers=AUTH).status_code == 200
        authority_count = restarted.app.state.core_control_provider.store._connection.execute(
            "SELECT COUNT(*) FROM revision_artifact_authorities WHERE project_id = ?",
            (project["id"],),
        ).fetchone()[0]
        assert authority_count == 0
        frames = restarted.app.state.core_control_provider.store.replay_events(None)
        assert [frame["event"] for frame in frames] == [
            "project.updated.v1",
            "revision.activated.v1",
        ]
        assert frames[0]["data"]["payload"]["id"] == other["id"]


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


def test_workspace_finalize_cleanup_preserves_replaced_temporary_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    archive = _workspace_archive()
    snapshot_root = tmp_path / "core-control-v1" / "workspace-snapshots"
    outside = tmp_path / "outside"
    outside.mkdir()
    outside_file = outside / "keep.txt"
    outside_file.write_text("keep", encoding="utf-8")
    observed: dict[str, object] = {}
    cleanup_calls: list[tuple[str, os.stat_result | None]] = []
    real_validate = workspace_module._validate_directory_metadata
    real_remove_tree = workspace_module._remove_tree_at

    def replace_temporary_root(metadata: os.stat_result, *, mode: int, label: str) -> None:
        real_validate(metadata, mode=mode, label=label)
        if label != "temporary root":
            return
        temporary = next(snapshot_root.glob(".workspace-*.tmp"))
        displaced = snapshot_root / f"{temporary.name}.displaced"
        temporary.rename(displaced)
        (displaced / "original.txt").write_text("original", encoding="utf-8")
        temporary.mkdir(mode=0o700)
        assert temporary.stat().st_uid == metadata.st_uid
        assert (displaced.stat().st_dev, displaced.stat().st_ino) == (
            metadata.st_dev,
            metadata.st_ino,
        )
        (temporary / "replacement.txt").write_text("replacement", encoding="utf-8")
        (temporary / "outside-link").symlink_to(outside, target_is_directory=True)
        observed.update(temporary=temporary, displaced=displaced, identity=metadata)
        raise WorkspaceArchiveError("injected temporary workspace failure")

    def observe_cleanup(
        parent_fd: int,
        name: str,
        *,
        expected_identity: os.stat_result | None = None,
    ) -> None:
        cleanup_calls.append((name, expected_identity))
        real_remove_tree(parent_fd, name, expected_identity=expected_identity)

    monkeypatch.setattr(workspace_module, "_validate_directory_metadata", replace_temporary_root)
    monkeypatch.setattr(workspace_module, "_remove_tree_at", observe_cleanup)

    with TestClient(_app(tmp_path)) as client:
        project, project_etag = _create_project(client, _project_create(archive=archive))
        upload = client.post(
            f"/v1/projects/{project['id']}/workspace-uploads",
            headers={
                **AUTH,
                "Idempotency-Key": "begin-upload-temporary-replacement",
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
                "Idempotency-Key": "chunk-upload-temporary-replacement",
                "If-Match": upload.headers["etag"],
            },
            json=_chunk(archive, offset=0),
        )
        finalized = client.post(
            f"/v1/projects/{project['id']}/workspace-uploads/{upload.json()['id']}/finalize",
            headers={
                **AUTH,
                "Idempotency-Key": "finalize-upload-temporary-replacement",
                "If-Match": chunk.headers["etag"],
                "If-Project-Match": project_etag,
            },
            json={"schema_version": "1", "content_sha256": hashlib.sha256(archive).hexdigest()},
        )

        assert finalized.status_code == 409
        assert finalized.json()["code"] == "workspace_archive_invalid"

    temporary = observed["temporary"]
    displaced = observed["displaced"]
    identity = observed["identity"]
    assert isinstance(temporary, Path)
    assert isinstance(displaced, Path)
    assert isinstance(identity, os.stat_result)
    assert cleanup_calls == [(temporary.name, identity)]
    assert (temporary / "replacement.txt").read_text(encoding="utf-8") == "replacement"
    assert os.path.lexists(temporary / "outside-link")
    assert (displaced / "original.txt").read_text(encoding="utf-8") == "original"
    assert outside_file.read_text(encoding="utf-8") == "keep"


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


def test_verified_subscription_project_publishes_durable_initial_revision(
    tmp_path: Path,
) -> None:
    state_root = tmp_path / "state"
    registry = verified_builtin_registry(tmp_path / "registry")
    with TestClient(_app(state_root, registry=registry)) as client:
        project, _ = _create_project(client, _project_create())
        assert project["status"] == "ready"
        assert project["registry_digest"] == registry.snapshot.registry_digest
        assert project["model_preparation"]["status"] == "ready"
        initial_ref = project["active_revision"]
        assert initial_ref is not None
        assert initial_ref["project_id"] == project["id"]
        assert initial_ref["generation"] == 0

        head = client.get(
            f"/v1/projects/{project['id']}/revisions/head",
            headers=AUTH,
        )
        assert head.status_code == 200, head.text
        assert head.headers["etag"] == head.json()["etag"]
        assert head.json() == {
            "schema_version": "1",
            "project_id": project["id"],
            "active_revision": initial_ref,
            "successor_revision": None,
            "transition": None,
            "updated_at": project["updated_at"],
            "etag": head.headers["etag"],
        }

        revision = client.get(f"/v1/revisions/{initial_ref['id']}", headers=AUTH)
        assert revision.status_code == 200, revision.text
        assert revision.headers["etag"] == revision.json()["etag"]
        assert revision.json()["revision"] == initial_ref
        assert revision.json()["status"] == "active"
        assert revision.json()["project_snapshot"] == project["current_project_snapshot"]
        assert revision.json()["task_snapshot"] == project["current_task_snapshot"]
        assert revision.json()["workspace_snapshot"] == project["current_workspace_snapshot"]
        assert revision.json()["registry_digest"] == registry.snapshot.registry_digest

        revisions = client.get(
            f"/v1/projects/{project['id']}/revisions",
            headers=AUTH,
        )
        assert revisions.status_code == 200, revisions.text
        assert [item["revision"] for item in revisions.json()["items"]] == [initial_ref]

    with TestClient(_app(state_root, registry=registry)) as restarted:
        project_after_restart = restarted.get(
            f"/v1/projects/{project['id']}",
            headers=AUTH,
        )
        assert project_after_restart.status_code == 200
        assert project_after_restart.json()["active_revision"] == initial_ref
        head_after_restart = restarted.get(
            f"/v1/projects/{project['id']}/revisions/head",
            headers=AUTH,
        )
        assert head_after_restart.status_code == 200
        assert head_after_restart.json()["active_revision"] == initial_ref


def test_store_activates_idempotent_cross_session_evolution_revision(
    tmp_path: Path,
) -> None:
    state_root = tmp_path / "state"
    registry = verified_builtin_registry(tmp_path / "registry")
    app = _app(state_root, registry=registry)
    with TestClient(app) as client:
        created, _etag = _create_project(client, _project_create())
        predecessor = m.RevisionRefV1.model_validate(created["active_revision"])
        store = app.state.core_control_provider.store
        revision = store.activate_evolution_revision(
            created["id"],
            predecessor=predecessor,
            run_id="run-evolution-1",
            context_artifact_ids={
                "dataset": ["dataset-1"],
                "text_memory": ["artifact-memory-1"],
            },
        )
        replay = store.activate_evolution_revision(
            created["id"],
            predecessor=predecessor,
            run_id="run-evolution-1",
            context_artifact_ids={
                "dataset": ["dataset-1"],
                "text_memory": ["artifact-memory-1"],
            },
        )

        assert revision.revision.generation == predecessor.generation + 1
        assert revision.predecessor_revision == predecessor
        assert revision.status is m.RevisionStatus.ACTIVE
        assert replay == revision
        assert store.get_project(created["id"]).active_revision == revision.revision

        with pytest.raises(IdempotencyConflictError):
            store.activate_evolution_revision(
                created["id"],
                predecessor=predecessor,
                run_id="run-evolution-1",
                context_artifact_ids={"text_memory": ["artifact-memory-2"]},
            )

    with TestClient(_app(state_root, registry=registry)) as restarted:
        response = restarted.get(
            f"/v1/projects/{created['id']}/revisions/head",
            headers=AUTH,
        )
        assert response.status_code == 200
        assert response.json()["active_revision"] == revision.revision.model_dump(mode="json")


def test_project_patch_publishes_a_durable_direct_successor_revision(
    tmp_path: Path,
) -> None:
    state_root = tmp_path / "state"
    registry = verified_builtin_registry(tmp_path / "registry")
    with TestClient(_app(state_root, registry=registry)) as client:
        project, project_etag = _create_project(client, _project_create())
        initial_ref = project["active_revision"]
        assert initial_ref is not None
        patched = client.patch(
            f"/v1/projects/{project['id']}",
            headers={
                **AUTH,
                "Idempotency-Key": "patch-project-revision-0001",
                "If-Match": project_etag,
            },
            json={"schema_version": "1", "name": "Protein memory successor"},
        )
        assert patched.status_code == 200, patched.text
        successor_ref = patched.json()["active_revision"]
        assert successor_ref["project_id"] == project["id"]
        assert successor_ref["generation"] == 1
        assert successor_ref["id"] != initial_ref["id"]
        replay = client.patch(
            f"/v1/projects/{project['id']}",
            headers={
                **AUTH,
                "Idempotency-Key": "patch-project-revision-0001",
                "If-Match": project_etag,
            },
            json={"schema_version": "1", "name": "Protein memory successor"},
        )
        assert replay.status_code == 200
        assert replay.json() == patched.json()

        revisions = client.get(
            f"/v1/projects/{project['id']}/revisions",
            headers=AUTH,
            params={"sort": "generation", "direction": "asc"},
        )
        assert revisions.status_code == 200, revisions.text
        assert [item["revision"] for item in revisions.json()["items"]] == [
            initial_ref,
            successor_ref,
        ]
        historical = client.get(f"/v1/revisions/{initial_ref['id']}", headers=AUTH)
        assert historical.status_code == 200
        assert historical.json()["status"] == "active"

    with TestClient(_app(state_root, registry=registry)) as restarted:
        head = restarted.get(
            f"/v1/projects/{project['id']}/revisions/head",
            headers=AUTH,
        )
        assert head.status_code == 200
        assert head.json()["active_revision"] == successor_ref


def test_project_successor_timestamp_is_strictly_monotonic_across_clock_rollback(
    tmp_path: Path,
) -> None:
    state_root = tmp_path / "state"
    registry = verified_builtin_registry(tmp_path / "registry")
    app = _app(state_root, registry=registry)
    provider = app.state.core_control_provider
    provider.store._clock = lambda: datetime(2030, 1, 1, tzinfo=timezone.utc)
    with TestClient(app) as client:
        project, project_etag = _create_project(client, _project_create())
        predecessor = client.get(
            f"/v1/revisions/{project['active_revision']['id']}", headers=AUTH
        ).json()

        fixed = client.patch(
            f"/v1/projects/{project['id']}",
            headers={
                **AUTH,
                "Idempotency-Key": "patch-project-fixed-clock",
                "If-Match": project_etag,
            },
            json={"schema_version": "1", "name": "Fixed-clock successor"},
        )
        assert fixed.status_code == 200, fixed.text
        fixed_successor = client.get(
            f"/v1/revisions/{fixed.json()['active_revision']['id']}", headers=AUTH
        ).json()
        assert fixed_successor["updated_at"] > predecessor["updated_at"]

        provider.store._clock = lambda: datetime(2020, 1, 1, tzinfo=timezone.utc)
        patched = client.patch(
            f"/v1/projects/{project['id']}",
            headers={
                **AUTH,
                "Idempotency-Key": "patch-project-clock-rollback",
                "If-Match": fixed.headers["etag"],
            },
            json={"schema_version": "1", "name": "Rollback-clock successor"},
        )
        assert patched.status_code == 200, patched.text
        successor = client.get(
            f"/v1/revisions/{patched.json()['active_revision']['id']}", headers=AUTH
        ).json()
        assert successor["updated_at"] > fixed_successor["updated_at"]

    with TestClient(_app(state_root, registry=registry)) as restarted:
        recovered = restarted.get(f"/v1/revisions/{successor['revision']['id']}", headers=AUTH)
        assert recovered.status_code == 200
        assert recovered.json() == successor
        replay = restarted.patch(
            f"/v1/projects/{project['id']}",
            headers={
                **AUTH,
                "Idempotency-Key": "patch-project-clock-rollback",
                "If-Match": fixed.headers["etag"],
            },
            json={"schema_version": "1", "name": "Rollback-clock successor"},
        )
        assert replay.status_code == 200
        assert replay.json() == patched.json()


def test_historical_successor_patch_replays_after_a_future_generation(
    tmp_path: Path,
) -> None:
    state_root = tmp_path / "state"
    registry = verified_builtin_registry(tmp_path / "registry")
    first_request = {"schema_version": "1", "name": "Historical successor"}
    with TestClient(_app(state_root, registry=registry)) as client:
        project, project_etag = _create_project(client, _project_create())
        first = client.patch(
            f"/v1/projects/{project['id']}",
            headers={
                **AUTH,
                "Idempotency-Key": "patch-project-historical-successor",
                "If-Match": project_etag,
            },
            json=first_request,
        )
        assert first.status_code == 200, first.text
        future = client.patch(
            f"/v1/projects/{project['id']}",
            headers={
                **AUTH,
                "Idempotency-Key": "patch-project-future-successor",
                "If-Match": first.headers["etag"],
            },
            json={"schema_version": "1", "description": "Future generation change."},
        )
        assert future.status_code == 200, future.text
        assert future.json()["active_revision"]["generation"] == 2

    with TestClient(_app(state_root, registry=registry)) as restarted:
        replay = restarted.patch(
            f"/v1/projects/{project['id']}",
            headers={
                **AUTH,
                "Idempotency-Key": "patch-project-historical-successor",
                "If-Match": project_etag,
            },
            json=first_request,
        )
        assert replay.status_code == 200
        assert replay.json() == first.json()
        assert replay.headers["etag"] == first.headers["etag"]


def test_successor_patch_idempotency_rejects_a_future_revision_response(
    tmp_path: Path,
) -> None:
    registry = verified_builtin_registry(tmp_path / "registry")
    with TestClient(_app(tmp_path, registry=registry)) as client:
        project, project_etag = _create_project(client, _project_create())
        first = client.patch(
            f"/v1/projects/{project['id']}",
            headers={
                **AUTH,
                "Idempotency-Key": "patch-project-bound-history",
                "If-Match": project_etag,
            },
            json={"schema_version": "1", "name": "Bound historical name"},
        )
        assert first.status_code == 200, first.text
        future = client.patch(
            f"/v1/projects/{project['id']}",
            headers={
                **AUTH,
                "Idempotency-Key": "patch-project-bound-future",
                "If-Match": first.headers["etag"],
            },
            json={"schema_version": "1", "description": "A later change."},
        )
        assert future.status_code == 200, future.text

    database = tmp_path / "core-control-v1" / "provider.sqlite3"
    with sqlite3.connect(database) as connection:
        connection.execute(
            "UPDATE idempotency_records SET response_json = ?, etag = ? "
            "WHERE operation_id = ? AND idempotency_key = ?",
            (
                store_module._canonical_bytes(future.json()),
                future.headers["etag"],
                "patchCoreProjectV1",
                "patch-project-bound-history",
            ),
        )

    with pytest.raises(StoreCorruptionError, match="revision.*request"):
        CoreControlStoreV1(tmp_path)


def test_deleted_project_patch_idempotency_retains_revision_request_binding(
    tmp_path: Path,
) -> None:
    registry = verified_builtin_registry(tmp_path / "registry")
    with TestClient(_app(tmp_path, registry=registry)) as client:
        project, project_etag = _create_project(client, _project_create())
        first = client.patch(
            f"/v1/projects/{project['id']}",
            headers={
                **AUTH,
                "Idempotency-Key": "patch-deleted-project-history",
                "If-Match": project_etag,
            },
            json={"schema_version": "1", "name": "Deleted project history"},
        )
        assert first.status_code == 200, first.text
        future = client.patch(
            f"/v1/projects/{project['id']}",
            headers={
                **AUTH,
                "Idempotency-Key": "patch-deleted-project-future",
                "If-Match": first.headers["etag"],
            },
            json={"schema_version": "1", "description": "Future before deletion."},
        )
        assert future.status_code == 200, future.text
        deleted = client.delete(
            f"/v1/projects/{project['id']}",
            headers={
                **AUTH,
                "Idempotency-Key": "delete-project-with-patch-history",
                "If-Match": future.headers["etag"],
            },
        )
        assert deleted.status_code == 204, deleted.text

    database = tmp_path / "core-control-v1" / "provider.sqlite3"
    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT COUNT(*) FROM project_revisions").fetchone()[0] == 0
        assert (
            connection.execute("SELECT COUNT(*) FROM revision_activation_bindings").fetchone()[0]
            == 3
        )
        connection.execute(
            "UPDATE idempotency_records SET response_json = ?, etag = ? "
            "WHERE operation_id = ? AND idempotency_key = ?",
            (
                store_module._canonical_bytes(future.json()),
                future.headers["etag"],
                "patchCoreProjectV1",
                "patch-deleted-project-history",
            ),
        )

    with pytest.raises(StoreCorruptionError, match="revision.*request"):
        CoreControlStoreV1(tmp_path)


def test_startup_rejects_a_missing_live_revision_activation_binding(
    tmp_path: Path,
) -> None:
    registry = verified_builtin_registry(tmp_path / "registry")
    with TestClient(_app(tmp_path, registry=registry)) as client:
        project, _ = _create_project(client, _project_create())

    database = tmp_path / "core-control-v1" / "provider.sqlite3"
    with sqlite3.connect(database) as connection:
        connection.execute(
            "DELETE FROM revision_activation_bindings WHERE revision_id = ?",
            (project["active_revision"]["id"],),
        )

    with pytest.raises(StoreCorruptionError, match="revision.*binding.*missing"):
        CoreControlStoreV1(tmp_path)


def test_project_revision_readiness_fails_closed_until_all_inputs_are_ready(
    tmp_path: Path,
) -> None:
    registry = verified_builtin_registry(tmp_path / "registry")
    with TestClient(_app(tmp_path / "no-registry")) as client:
        project, _ = _create_project(client, _project_create())
        assert project["status"] == "draft"
        assert project["active_revision"] is None
        assert project["registry_digest"] is None

    with TestClient(_app(tmp_path / "self-deployed", registry=registry)) as client:
        project, _ = _create_project(
            client,
            _project_create(
                execution_mode="self-deployed",
                capture_mode="token_level",
                harness_id="openhands",
            ),
        )
        assert project["status"] == "draft"
        assert project["active_revision"] is None
        assert project["model_preparation"]["status"] == "unresolved"

    with TestClient(_app(tmp_path / "demoted", registry=registry)) as client:
        project, etag = _create_project(client, _project_create())
        head_before = client.get(
            f"/v1/projects/{project['id']}/revisions/head",
            headers=AUTH,
        )
        self_deployed_spec = _project_create(
            execution_mode="self-deployed",
            capture_mode="token_level",
            harness_id="openhands",
        )["spec"]
        patched = client.patch(
            f"/v1/projects/{project['id']}",
            headers={
                **AUTH,
                "Idempotency-Key": "patch-project-unready-model",
                "If-Match": etag,
            },
            json={"schema_version": "1", "spec": self_deployed_spec},
        )
        assert patched.status_code == 200
        assert patched.json()["status"] == "draft"
        assert patched.json()["active_revision"] == project["active_revision"]
        head_after = client.get(
            f"/v1/projects/{project['id']}/revisions/head",
            headers=AUTH,
        )
        assert head_after.json() == head_before.json()
        assert head_after.headers["etag"] == head_before.headers["etag"]

    archive = _workspace_archive()
    payload = _project_create(archive=archive)
    with TestClient(_app(tmp_path / "imported", registry=registry)) as client:
        project, project_etag = _create_project(client, payload)
        assert project["status"] == "draft"
        assert project["active_revision"] is None
        upload = client.post(
            f"/v1/projects/{project['id']}/workspace-uploads",
            headers={
                **AUTH,
                "Idempotency-Key": "begin-ready-workspace",
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
                "Idempotency-Key": "chunk-ready-workspace",
                "If-Match": upload.headers["etag"],
            },
            json=_chunk(archive, offset=0),
        )
        finalized = client.post(
            f"/v1/projects/{project['id']}/workspace-uploads/{upload.json()['id']}/finalize",
            headers={
                **AUTH,
                "Idempotency-Key": "finalize-ready-workspace",
                "If-Match": chunk.headers["etag"],
                "If-Project-Match": project_etag,
            },
            json={"schema_version": "1", "content_sha256": hashlib.sha256(archive).hexdigest()},
        )
        assert finalized.status_code == 201, finalized.text
        published = finalized.json()["project"]
        assert published["status"] == "ready"
        assert published["active_revision"]["generation"] == 0
        assert published["current_workspace_snapshot"] is not None


def test_project_revision_reads_paginate_and_return_typed_not_found(
    tmp_path: Path,
) -> None:
    registry = verified_builtin_registry(tmp_path / "registry")
    with TestClient(_app(tmp_path / "draft")) as draft_client:
        draft, _ = _create_project(draft_client, _project_create())
        empty = draft_client.get(f"/v1/projects/{draft['id']}/revisions", headers=AUTH)
        assert empty.status_code == 200
        assert empty.json()["items"] == []
        missing_head = draft_client.get(f"/v1/projects/{draft['id']}/revisions/head", headers=AUTH)
        assert missing_head.status_code == 404
        assert missing_head.json()["code"] == "revision_head_not_found"
        missing_revision = draft_client.get("/v1/revisions/revision-missing", headers=AUTH)
        assert missing_revision.status_code == 404
        assert missing_revision.json()["code"] == "revision_not_found"

    with TestClient(_app(tmp_path / "ready", registry=registry)) as client:
        project, etag = _create_project(client, _project_create())
        for index in range(2):
            patched = client.patch(
                f"/v1/projects/{project['id']}",
                headers={
                    **AUTH,
                    "Idempotency-Key": f"patch-page-revision-{index}",
                    "If-Match": etag,
                },
                json={"schema_version": "1", "name": f"Revision page {index}"},
            )
            assert patched.status_code == 200
            etag = patched.headers["etag"]
        first = client.get(
            f"/v1/projects/{project['id']}/revisions",
            headers=AUTH,
            params={"limit": 1, "sort": "generation", "direction": "asc"},
        )
        assert first.status_code == 200
        assert first.json()["items"][0]["revision"]["generation"] == 0
        assert first.json()["has_more"] is True
        second = client.get(
            f"/v1/projects/{project['id']}/revisions",
            headers=AUTH,
            params={
                "limit": 1,
                "sort": "generation",
                "direction": "asc",
                "after": first.json()["next_cursor"],
            },
        )
        assert second.status_code == 200
        assert second.json()["items"][0]["revision"]["generation"] == 1
        tampered = client.get(
            f"/v1/projects/{project['id']}/revisions",
            headers=AUTH,
            params={"after": first.json()["next_cursor"] + "x"},
        )
        assert tampered.status_code == 400
        revision_id = second.json()["items"][0]["revision"]["id"]
        revision = client.get(f"/v1/revisions/{revision_id}", headers=AUTH)
        assert revision.headers["etag"] == revision.json()["etag"]


@pytest.mark.parametrize("budget_kind", ["rows", "bytes"])
def test_ready_project_write_cannot_commit_state_that_startup_cannot_recover(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    budget_kind: str,
) -> None:
    registry = verified_builtin_registry(tmp_path / "registry")
    probe_root = tmp_path / "probe"
    with TestClient(_app(probe_root, registry=registry)) as probe:
        _create_project(probe, _project_create())
    recovered_probe = CoreControlStoreV1(probe_root)
    try:
        populated_usage = {
            "rows": recovered_probe._startup_scan_rows,
            "bytes": recovered_probe._startup_scan_bytes,
        }
    finally:
        recovered_probe.close()

    state_root = tmp_path / "state"
    app = _app(state_root, registry=registry)
    store = app.state.core_control_provider.store
    baseline_usage = {
        "rows": store._startup_scan_rows,
        "bytes": store._startup_scan_bytes,
    }
    assert populated_usage[budget_kind] > baseline_usage[budget_kind] + 1
    reduced_limit = (
        baseline_usage[budget_kind]
        + (populated_usage[budget_kind] - baseline_usage[budget_kind]) // 2
    )
    monkeypatch.setattr(
        store_module,
        "_MAX_STARTUP_ROWS" if budget_kind == "rows" else "_MAX_STARTUP_BLOB_BYTES",
        reduced_limit,
    )

    with TestClient(app) as client:
        response = client.post(
            "/v1/projects",
            headers={**AUTH, "Idempotency-Key": f"recovery-budget-{budget_kind}"},
            json=_project_create(),
        )
        assert response.status_code == 500
        assert response.json()["code"] == "core_control_store_corrupt"
        assert client.get("/v1/projects", headers=AUTH).json()["items"] == []

    database = state_root / "core-control-v1" / "provider.sqlite3"
    with sqlite3.connect(database) as connection:
        for table in (
            "projects",
            "project_revisions",
            "revision_activation_bindings",
            "events",
            "idempotency_records",
        ):
            assert connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0

    reopened = CoreControlStoreV1(state_root)
    try:
        assert (
            reopened.list_projects(
                limit=1,
                after=None,
                sort="created_at",
                direction="asc",
            ).items
            == []
        )
    finally:
        reopened.close()


def test_project_revision_schema_migrates_exact_previous_store_with_state(
    tmp_path: Path,
) -> None:
    payload = _project_create()
    with TestClient(_app(tmp_path)) as client:
        project, _ = _create_project(client, payload)

    database = tmp_path / "core-control-v1" / "provider.sqlite3"
    with sqlite3.connect(database) as connection:
        connection.execute("DROP TABLE revision_artifact_authorities")
        connection.execute("DROP TABLE revision_activation_bindings")
        connection.execute("DROP TABLE project_revisions")

    with TestClient(_app(tmp_path)) as restarted:
        recovered = restarted.get(f"/v1/projects/{project['id']}", headers=AUTH)
        assert recovered.status_code == 200
        replay = restarted.post(
            "/v1/projects",
            headers={**AUTH, "Idempotency-Key": "create-project-0001"},
            json=payload,
        )
        assert replay.status_code == 201
        assert replay.json() == project
    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT COUNT(*) FROM project_revisions").fetchone()[0] == 0


def test_project_revision_schema_migrates_v1_ledger_and_backfills_request_binding(
    tmp_path: Path,
) -> None:
    registry = verified_builtin_registry(tmp_path / "registry")
    patch_request = {"schema_version": "1", "name": "Migrated ledger successor"}
    with TestClient(_app(tmp_path, registry=registry)) as client:
        project, project_etag = _create_project(client, _project_create())
        patched = client.patch(
            f"/v1/projects/{project['id']}",
            headers={
                **AUTH,
                "Idempotency-Key": "patch-project-migrated-ledger",
                "If-Match": project_etag,
            },
            json=patch_request,
        )
        assert patched.status_code == 200, patched.text

    database = tmp_path / "core-control-v1" / "provider.sqlite3"
    with sqlite3.connect(database) as connection:
        signing_key = bytes(
            connection.execute("SELECT value FROM metadata WHERE key = 'signing_key'").fetchone()[
                0
            ]
        )
        revisions = connection.execute(
            "SELECT revision_id, project_id, generation, document_json, resource_version, "
            "created_at, updated_at FROM project_revisions"
        ).fetchall()
        connection.execute("DROP TABLE revision_artifact_authorities")
        connection.execute("DROP TABLE revision_activation_bindings")
        connection.execute("DROP TABLE project_revisions")
        connection.execute(store_module._PROJECT_REVISIONS_SCHEMA_V1)
        for revision in revisions:
            revision_id = revision[0]
            identity_hmac = hmac.new(
                signing_key,
                f"resource.v1:revision:{revision_id}".encode("ascii"),
                hashlib.sha256,
            ).hexdigest()
            connection.execute(
                "INSERT INTO project_revisions VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (*revision[:3], identity_hmac, *revision[3:]),
            )

    with TestClient(_app(tmp_path, registry=registry)) as restarted:
        replay = restarted.patch(
            f"/v1/projects/{project['id']}",
            headers={
                **AUTH,
                "Idempotency-Key": "patch-project-migrated-ledger",
                "If-Match": project_etag,
            },
            json=patch_request,
        )
        assert replay.status_code == 200
        assert replay.json() == patched.json()

    with sqlite3.connect(database) as connection:
        rows = connection.execute(
            "SELECT activation_request_digest FROM project_revisions ORDER BY generation"
        ).fetchall()
        assert len(rows) == 2
        assert all(row[0] is not None for row in rows)
        assert (
            connection.execute("SELECT COUNT(*) FROM revision_activation_bindings").fetchone()[0]
            == 2
        )
        assert (
            connection.execute("SELECT COUNT(*) FROM revision_artifact_authorities").fetchone()[0]
            == 2
        )


def test_artifact_authority_schema_migrates_live_activation_envelope(
    tmp_path: Path,
) -> None:
    registry = verified_builtin_registry(tmp_path / "registry")
    artifact_root = tmp_path / "managed-artifacts"
    record, digest, byte_size, _primary = _artifact_payload(
        artifact_root,
        artifact_id="migrated-memory",
        artifact_type=m.ArtifactType.TEXT_MEMORY,
        content="migrated durable authority\n",
    )
    run_control = _ArtifactRunControl()
    app = _app(
        tmp_path / "state",
        registry=registry,
        run_control=run_control,
        evolution_artifact_root=artifact_root,
        artifact_loader={"migrated-memory": record}.__getitem__,
    )
    project = _ready_provider_project(app, registry)
    project, summary = _publish_artifact_summary(
        app,
        run_control,
        project,
        run_id="run-migrated-memory",
        record=record,
        content_sha256=digest,
        byte_size=byte_size,
    )
    app.state.core_control_provider.close()

    database = tmp_path / "state" / "core-control-v1" / "provider.sqlite3"
    with sqlite3.connect(database) as connection:
        connection.execute("DROP TABLE revision_artifact_authorities")

    restarted = _app(
        tmp_path / "state",
        registry=registry,
        run_control=run_control,
        evolution_artifact_root=artifact_root,
        artifact_loader={"migrated-memory": record}.__getitem__,
    )
    with TestClient(restarted) as client:
        response = client.get(f"/v1/projects/{project.id}/artifacts/{summary.id}", headers=AUTH)

    assert response.status_code == 200, response.text
    with sqlite3.connect(database) as connection:
        authority_json = connection.execute(
            "SELECT authority_json FROM revision_artifact_authorities WHERE revision_id = ?",
            (project.active_revision.id,),
        ).fetchone()[0]
    authority = json.loads(authority_json)
    assert authority["producing_run_id"] == "run-migrated-memory"
    assert authority["context_artifact_ids"] == {"text_memory": ["migrated-memory"]}


def test_artifact_authority_migration_fails_closed_without_durable_binding(
    tmp_path: Path,
) -> None:
    registry = verified_builtin_registry(tmp_path / "registry")
    with TestClient(_app(tmp_path, registry=registry)) as client:
        _create_project(client, _project_create())

    database = tmp_path / "core-control-v1" / "provider.sqlite3"
    with sqlite3.connect(database) as connection:
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("DROP TABLE revision_artifact_authorities")
        connection.execute("DELETE FROM idempotency_records")
        assert connection.execute(
            "SELECT COUNT(*) FROM revision_activation_bindings"
        ).fetchone()[0] == 0

    with pytest.raises(
        StoreCorruptionError,
        match="maintenance action: restore the pre-migration database",
    ):
        CoreControlStoreV1(tmp_path)

    with sqlite3.connect(database) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM sqlite_schema "
            "WHERE type = 'table' AND name = 'revision_artifact_authorities'"
        ).fetchone()[0] == 0


def test_revision_ledger_migration_rejects_ambiguous_retained_response_closure(
    tmp_path: Path,
) -> None:
    registry = verified_builtin_registry(tmp_path / "registry")
    with TestClient(_app(tmp_path, registry=registry)) as client:
        _create_project(client, _project_create())

    database = tmp_path / "core-control-v1" / "provider.sqlite3"
    with sqlite3.connect(database) as connection:
        signing_key = bytes(
            connection.execute("SELECT value FROM metadata WHERE key = 'signing_key'").fetchone()[
                0
            ]
        )
        revision = connection.execute(
            "SELECT revision_id, project_id, generation, document_json, resource_version, "
            "created_at, updated_at FROM project_revisions"
        ).fetchone()
        assert revision is not None
        connection.execute(
            "INSERT INTO idempotency_records(operation_id, resource_scope, idempotency_key, "
            "request_digest, request_json, semantic_headers_json, status_code, response_type, "
            "response_json, etag, created_at_epoch, expires_at_epoch) "
            "SELECT operation_id, resource_scope, 'ambiguous-create-replay', request_digest, "
            "request_json, semantic_headers_json, status_code, response_type, response_json, "
            "etag, created_at_epoch, expires_at_epoch FROM idempotency_records "
            "WHERE operation_id = 'createCoreProjectV1'"
        )
        connection.execute("DROP TABLE revision_artifact_authorities")
        connection.execute("DROP TABLE revision_activation_bindings")
        connection.execute("DROP TABLE project_revisions")
        connection.execute(store_module._PROJECT_REVISIONS_SCHEMA_V1)
        identity_hmac = hmac.new(
            signing_key,
            f"resource.v1:revision:{revision[0]}".encode("ascii"),
            hashlib.sha256,
        ).hexdigest()
        connection.execute(
            "INSERT INTO project_revisions VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (*revision[:3], identity_hmac, *revision[3:]),
        )

    with pytest.raises(
        StoreCorruptionError,
        match="cannot reconstruct an unambiguous durable activation authority",
    ):
        CoreControlStoreV1(tmp_path)


def test_artifact_authority_migration_rejects_oversize_before_revision_decode(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry = verified_builtin_registry(tmp_path / "registry")
    with TestClient(_app(tmp_path, registry=registry)) as client:
        project, _ = _create_project(client, _project_create())

    database = tmp_path / "core-control-v1" / "provider.sqlite3"
    with sqlite3.connect(database) as connection:
        connection.execute("DROP TABLE revision_artifact_authorities")
        connection.execute(
            "UPDATE project_revisions SET document_json = zeroblob(?) "
            "WHERE revision_id = ?",
            (store_module._MAX_STARTUP_VALUE_BYTES + 1, project["active_revision"]["id"]),
        )

    original_validate_bytes = store_module._validate_bytes

    def reject_revision_decode(model, value):
        if model is m.RevisionV1:
            raise AssertionError("oversize revision entered Python validation")
        return original_validate_bytes(model, value)

    monkeypatch.setattr(store_module, "_validate_bytes", reject_revision_decode)
    with pytest.raises(StoreCorruptionError, match="project_revisions recovery quota"):
        CoreControlStoreV1(tmp_path)


def test_artifact_authority_migration_rejects_non_utf8_revision_bytes(
    tmp_path: Path,
) -> None:
    registry = verified_builtin_registry(tmp_path / "registry")
    with TestClient(_app(tmp_path, registry=registry)) as client:
        project, _ = _create_project(client, _project_create())

    database = tmp_path / "core-control-v1" / "provider.sqlite3"
    with sqlite3.connect(database) as connection:
        connection.execute("DROP TABLE revision_artifact_authorities")
        connection.execute(
            "UPDATE project_revisions SET document_json = ? WHERE revision_id = ?",
            (sqlite3.Binary(b"\x80"), project["active_revision"]["id"]),
        )

    with pytest.raises(StoreCorruptionError, match="persisted RevisionV1 is invalid"):
        CoreControlStoreV1(tmp_path)


@pytest.mark.parametrize("budget_kind", ["rows", "bytes"])
def test_revision_ledger_backfill_cannot_exceed_startup_recovery_budget(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    budget_kind: str,
) -> None:
    registry = verified_builtin_registry(tmp_path / "registry")
    with TestClient(_app(tmp_path, registry=registry)) as client:
        project, project_etag = _create_project(client, _project_create())
        patched = client.patch(
            f"/v1/projects/{project['id']}",
            headers={
                **AUTH,
                "Idempotency-Key": "patch-project-budgeted-backfill",
                "If-Match": project_etag,
            },
            json={"schema_version": "1", "name": "Budgeted ledger successor"},
        )
        assert patched.status_code == 200, patched.text

    recovered = CoreControlStoreV1(tmp_path)
    try:
        populated_usage = {
            "rows": recovered._startup_scan_rows,
            "bytes": recovered._startup_scan_bytes,
        }
    finally:
        recovered.close()

    database = tmp_path / "core-control-v1" / "provider.sqlite3"
    with sqlite3.connect(database) as connection:
        signing_key = bytes(
            connection.execute("SELECT value FROM metadata WHERE key = 'signing_key'").fetchone()[
                0
            ]
        )
        revisions = connection.execute(
            "SELECT revision_id, project_id, generation, document_json, resource_version, "
            "created_at, updated_at FROM project_revisions"
        ).fetchall()
        assert len(revisions) == 2
        connection.execute("DROP TABLE revision_artifact_authorities")
        connection.execute("DROP TABLE revision_activation_bindings")
        connection.execute("DROP TABLE project_revisions")
        connection.execute(store_module._PROJECT_REVISIONS_SCHEMA_V1)
        for revision in revisions:
            revision_id = revision[0]
            identity_hmac = hmac.new(
                signing_key,
                f"resource.v1:revision:{revision_id}".encode("ascii"),
                hashlib.sha256,
            ).hexdigest()
            connection.execute(
                "INSERT INTO project_revisions VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (*revision[:3], identity_hmac, *revision[3:]),
            )

    monkeypatch.setattr(
        store_module,
        "_MAX_STARTUP_ROWS" if budget_kind == "rows" else "_MAX_STARTUP_BLOB_BYTES",
        populated_usage[budget_kind] - 1,
    )
    with pytest.raises(StoreCorruptionError, match="aggregate startup quota"):
        CoreControlStoreV1(tmp_path)

    with sqlite3.connect(database) as connection:
        table_names = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_schema WHERE type = 'table'"
            ).fetchall()
        }
        assert "revision_activation_bindings" not in table_names
        columns = {
            row[1] for row in connection.execute("PRAGMA table_info(project_revisions)")
        }
        assert "activation_request_digest" not in columns


def test_project_revision_manifest_corruption_fails_closed_on_restart(
    tmp_path: Path,
) -> None:
    registry = verified_builtin_registry(tmp_path / "registry")
    with TestClient(_app(tmp_path, registry=registry)) as client:
        project, _ = _create_project(client, _project_create())

    database = tmp_path / "core-control-v1" / "provider.sqlite3"
    with sqlite3.connect(database) as connection:
        connection.row_factory = sqlite3.Row
        row = connection.execute(
            "SELECT document_json FROM project_revisions WHERE revision_id = ?",
            (project["active_revision"]["id"],),
        ).fetchone()
        revision = store_module._validate_bytes(store_module.m.RevisionV1, row["document_json"])
        data = revision.model_dump(mode="python", exclude={"etag"})
        data["revision"]["manifest_sha256"] = "0" * 64
        damaged = store_module._model_with_etag(store_module.m.RevisionV1, data, version=1)
        connection.execute(
            "UPDATE project_revisions SET document_json = ? WHERE revision_id = ?",
            (store_module._model_bytes(damaged), project["active_revision"]["id"]),
        )

    with pytest.raises(StoreCorruptionError, match="revision"):
        CoreControlStoreV1(tmp_path)


def test_revision_artifact_authority_requires_canonical_signed_bytes_on_restart(
    tmp_path: Path,
) -> None:
    registry = verified_builtin_registry(tmp_path / "registry")
    with TestClient(_app(tmp_path, registry=registry)) as client:
        project, _ = _create_project(client, _project_create())

    database = tmp_path / "core-control-v1" / "provider.sqlite3"
    with sqlite3.connect(database) as connection:
        row = connection.execute(
            "SELECT authority_json FROM revision_artifact_authorities WHERE revision_id = ?",
            (project["active_revision"]["id"],),
        ).fetchone()
        connection.execute(
            "UPDATE revision_artifact_authorities SET authority_json = ? WHERE revision_id = ?",
            (b" " + bytes(row[0]), project["active_revision"]["id"]),
        )

    with pytest.raises(StoreCorruptionError, match="artifact authority"):
        CoreControlStoreV1(tmp_path)


def test_project_revision_recovery_length_guards_before_document_fetch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry = verified_builtin_registry(tmp_path / "registry")
    with TestClient(_app(tmp_path, registry=registry)) as client:
        _create_project(client, _project_create())

    database = tmp_path / "core-control-v1" / "provider.sqlite3"
    with sqlite3.connect(database) as connection:
        project_bytes = int(
            connection.execute(
                "SELECT MAX(length(CAST(document_json AS BLOB))) FROM projects"
            ).fetchone()[0]
        )
        limit = project_bytes + 128
        connection.execute(
            "UPDATE project_revisions SET document_json = zeroblob(?)",
            (limit + 1,),
        )

    monkeypatch.setattr(store_module, "_MAX_STARTUP_VALUE_BYTES", limit)
    with pytest.raises(StoreCorruptionError, match="project_revisions recovery quota"):
        CoreControlStoreV1(tmp_path)


def test_revision_activation_event_is_durable_and_canonical(tmp_path: Path) -> None:
    registry = verified_builtin_registry(tmp_path / "registry")
    app = _app(tmp_path, registry=registry)
    provider = app.state.core_control_provider
    with TestClient(app) as client:
        project, _ = _create_project(client, _project_create())
        frames = provider.store.replay_events(None)
        assert [frame["event"] for frame in frames] == [
            "project.updated.v1",
            "revision.activated.v1",
        ]
        activated = SseFrameV1.model_validate_json(json.dumps(frames[-1]))
        assert activated.data.root.payload.revision.id == project["active_revision"]["id"]

    restarted = _app(tmp_path, registry=registry)
    with TestClient(restarted):
        frames = restarted.state.core_control_provider.store.replay_events(None)
        assert frames[-1]["event"] == "revision.activated.v1"


def test_revision_event_retention_preserves_complete_activation_pairs(tmp_path: Path) -> None:
    registry = verified_builtin_registry(tmp_path / "registry")
    app = _app(tmp_path, registry=registry, event_replay_limit=3)
    provider = app.state.core_control_provider
    with TestClient(app) as client:
        for index in range(2):
            project, _ = _create_project(
                client,
                {**_project_create(), "name": f"Ready project {index}"},
                idempotency_key=f"create-ready-event-project-{index}",
            )
        frames = provider.store.replay_events(None)
        assert [frame["event"] for frame in frames] == [
            "project.updated.v1",
            "revision.activated.v1",
        ]
        assert frames[0]["data"]["payload"]["id"] == project["id"]
        assert frames[1]["data"]["payload"]["revision"]["id"] == project["active_revision"]["id"]

    with TestClient(_app(tmp_path, registry=registry, event_replay_limit=3)) as restarted:
        recovered = restarted.app.state.core_control_provider.store.replay_events(None)
        assert recovered == frames


@pytest.mark.parametrize(
    "damage",
    ["missing_project_update", "wrong_project_update", "wrong_activation_revision"],
)
def test_startup_validates_revision_event_ledger_order_closure(
    tmp_path: Path,
    damage: str,
) -> None:
    registry = verified_builtin_registry(tmp_path / "registry")
    with TestClient(_app(tmp_path, registry=registry)) as client:
        project, project_etag = _create_project(client, _project_create())
        patched = client.patch(
            f"/v1/projects/{project['id']}",
            headers={
                **AUTH,
                "Idempotency-Key": "patch-project-event-closure",
                "If-Match": project_etag,
            },
            json={"schema_version": "1", "name": "Event closure successor"},
        )
        assert patched.status_code == 200, patched.text

    database = tmp_path / "core-control-v1" / "provider.sqlite3"
    with sqlite3.connect(database) as connection:
        connection.row_factory = sqlite3.Row
        rows = connection.execute("SELECT * FROM events ORDER BY sequence").fetchall()
        assert [json.loads(bytes(row["frame_json"]))["event"] for row in rows] == [
            "project.updated.v1",
            "revision.activated.v1",
            "project.updated.v1",
            "revision.activated.v1",
        ]
        if damage == "missing_project_update":
            connection.execute("DELETE FROM events WHERE sequence = ?", (rows[2]["sequence"],))
        else:
            target = rows[2] if damage == "wrong_project_update" else rows[3]
            source = rows[0] if damage == "wrong_project_update" else rows[1]
            target_frame = json.loads(bytes(target["frame_json"]))
            source_frame = json.loads(bytes(source["frame_json"]))
            target_frame["data"]["payload"] = source_frame["data"]["payload"]
            target_frame["data"]["change"] = source_frame["data"]["change"]
            target_frame["data"]["change"]["change_id"] = json.loads(bytes(target["frame_json"]))[
                "data"
            ]["change"]["change_id"]
            connection.execute(
                "UPDATE events SET frame_json = ? WHERE sequence = ?",
                (store_module._canonical_bytes(target_frame), target["sequence"]),
            )

    with pytest.raises(StoreCorruptionError, match="event"):
        CoreControlStoreV1(tmp_path)


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


def test_store_identity_marker_is_private_durable_and_restart_bound(tmp_path: Path) -> None:
    store = CoreControlStoreV1(tmp_path)
    store.close()

    root = tmp_path / "core-control-v1"
    marker = root / "provider.identity"
    database = root / "provider.sqlite3"
    marker_metadata = marker.stat(follow_symlinks=False)
    assert stat.S_ISREG(marker_metadata.st_mode)
    assert stat.S_IMODE(marker_metadata.st_mode) == 0o600
    assert marker_metadata.st_uid == os.geteuid()
    assert marker_metadata.st_nlink == 1
    marker_document = json.loads(marker.read_bytes())
    with sqlite3.connect(database) as connection:
        identity = connection.execute(
            "SELECT store_id, binding_state, root_dev, root_ino, marker_dev, marker_ino "
            "FROM store_identity WHERE singleton = 1"
        ).fetchone()
    assert identity is not None
    assert identity[1] == "bound"
    assert marker_document == {
        "root_dev": identity[2],
        "root_ino": identity[3],
        "schema_version": "1",
        "store_id": identity[0],
    }
    assert (marker_metadata.st_dev, marker_metadata.st_ino) == (identity[4], identity[5])

    restarted = CoreControlStoreV1(tmp_path)
    try:
        assert restarted._store_id == identity[0]
    finally:
        restarted.close()


def test_copied_database_cannot_claim_another_provider_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "source"
    target = tmp_path / "target"
    CoreControlStoreV1(source).close()
    CoreControlStoreV1(target).close()
    target_root = target / "core-control-v1"
    orphan = target_root / "workspace-snapshots" / "workspace-snapshot-existing"
    orphan.mkdir(mode=0o700)
    keep = orphan / "keep.txt"
    keep.write_text("keep", encoding="utf-8")
    shutil.copyfile(
        source / "core-control-v1" / "provider.sqlite3",
        target_root / "provider.sqlite3",
    )
    quota_calls = 0

    def observe_quota(*args, **kwargs):
        nonlocal quota_calls
        del args, kwargs
        quota_calls += 1

    monkeypatch.setattr(store_module, "_verify_managed_disk_quota", observe_quota)
    with pytest.raises(StoreCorruptionError, match="store identity"):
        CoreControlStoreV1(target)
    assert quota_calls == 0
    assert keep.read_text(encoding="utf-8") == "keep"


def test_fresh_database_cannot_claim_existing_managed_workspace(tmp_path: Path) -> None:
    root = tmp_path / "core-control-v1"
    upload_root = root / "workspace-uploads"
    workspace_root = root / "workspace-snapshots"
    upload_root.mkdir(parents=True, mode=0o700)
    workspace_root.mkdir(mode=0o700)
    orphan = workspace_root / "workspace-snapshot-existing"
    orphan.mkdir(mode=0o700)
    keep = orphan / "keep.txt"
    keep.write_text("keep", encoding="utf-8")
    database = root / "provider.sqlite3"
    database.touch(mode=0o600)

    with pytest.raises(StoreCorruptionError, match="unbound managed state"):
        CoreControlStoreV1(tmp_path)
    assert keep.read_text(encoding="utf-8") == "keep"
    assert not (root / "provider.identity").exists()


def _prepare_unbound_identity_store(state_root: Path, identity_kind: str) -> None:
    root = state_root / "core-control-v1"
    database = root / "provider.sqlite3"
    if identity_kind == "pending":
        CoreControlStoreV1(state_root).close()
        with sqlite3.connect(database) as connection:
            connection.execute(
                "UPDATE store_identity SET binding_state = 'pending', "
                "marker_dev = NULL, marker_ino = NULL WHERE singleton = 1"
            )
        return

    root.mkdir(parents=True, mode=0o700)
    if identity_kind == "fresh":
        database.touch(mode=0o600)
        return
    if identity_kind != "legacy":
        raise AssertionError(f"unknown identity kind: {identity_kind}")
    with sqlite3.connect(database) as connection:
        for statement in store_module._LEGACY_SCHEMA:
            connection.execute(statement)
        connection.execute(
            "INSERT INTO metadata(key, value) VALUES ('signing_key', ?)",
            (b"l" * 32,),
        )
    database.chmod(0o600)


@pytest.mark.parametrize("identity_kind", ["fresh", "legacy", "pending"])
@pytest.mark.parametrize(
    "insertion_point",
    ["after_initial_inventory", "after_marker_durable", "after_final_inventory"],
)
def test_identity_binding_rejects_managed_state_created_after_empty_inventory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    identity_kind: str,
    insertion_point: str,
) -> None:
    _prepare_unbound_identity_store(tmp_path, identity_kind)
    root = tmp_path / "core-control-v1"
    injected = root / "workspace-snapshots" / "workspace-snapshot-raced"
    cleanup_calls = 0
    injected_once = False

    def inject() -> None:
        nonlocal injected_once
        if injected_once:
            return
        injected_once = True
        injected.mkdir(mode=0o700)
        (injected / "keep.txt").write_text("keep", encoding="utf-8")

    def after_inventory(stage: str) -> None:
        if insertion_point == f"after_{stage}_inventory":
            inject()

    def after_marker() -> None:
        if insertion_point == "after_marker_durable":
            inject()

    def observe_cleanup(*args, **kwargs) -> None:
        nonlocal cleanup_calls
        del args, kwargs
        cleanup_calls += 1

    monkeypatch.setattr(
        store_module,
        "_after_unbound_managed_inventory",
        after_inventory,
    )
    monkeypatch.setattr(
        store_module,
        "_after_store_identity_marker_durable",
        after_marker,
    )
    monkeypatch.setattr(store_module, "_verify_managed_disk_quota", observe_cleanup)

    with pytest.raises(StoreCorruptionError, match="unbound managed state"):
        CoreControlStoreV1(tmp_path)

    assert injected_once
    assert cleanup_calls == 0
    assert (injected / "keep.txt").read_text(encoding="utf-8") == "keep"
    with sqlite3.connect(root / "provider.sqlite3") as connection:
        assert connection.execute(
            "SELECT binding_state FROM store_identity WHERE singleton = 1"
        ).fetchone() == ("pending",)


@pytest.mark.parametrize("identity_kind", ["fresh", "legacy", "pending"])
def test_identity_binding_keeps_one_managed_root_authority_across_inventory_checks(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    identity_kind: str,
) -> None:
    _prepare_unbound_identity_store(tmp_path, identity_kind)
    root = tmp_path / "core-control-v1"
    workspace_root = root / "workspace-snapshots"
    displaced = root / "workspace-snapshots.displaced"
    replaced = False
    cleanup_calls = 0

    def replace_after_initial(stage: str) -> None:
        nonlocal replaced
        if stage != "initial" or replaced:
            return
        replaced = True
        workspace_root.rename(displaced)
        workspace_root.mkdir(mode=0o700)

    def observe_cleanup(*args, **kwargs) -> None:
        nonlocal cleanup_calls
        del args, kwargs
        cleanup_calls += 1

    monkeypatch.setattr(
        store_module,
        "_after_unbound_managed_inventory",
        replace_after_initial,
    )
    monkeypatch.setattr(store_module, "_verify_managed_disk_quota", observe_cleanup)

    with pytest.raises(StoreCorruptionError, match="managed entry binding"):
        CoreControlStoreV1(tmp_path)

    assert replaced
    assert cleanup_calls == 0
    assert displaced.is_dir()
    assert workspace_root.is_dir()


def test_swapped_store_identity_marker_fails_before_workspace_recovery(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    first = tmp_path / "first"
    second = tmp_path / "second"
    CoreControlStoreV1(first).close()
    CoreControlStoreV1(second).close()
    first_marker = first / "core-control-v1" / "provider.identity"
    second_marker = second / "core-control-v1" / "provider.identity"
    displaced = tmp_path / "identity.displaced"
    first_marker.rename(displaced)
    second_marker.rename(first_marker)
    displaced.rename(second_marker)
    orphan = first / "core-control-v1" / "workspace-snapshots" / "workspace-snapshot-existing"
    orphan.mkdir(mode=0o700)
    keep = orphan / "keep.txt"
    keep.write_text("keep", encoding="utf-8")
    quota_calls = 0

    def observe_quota(*args, **kwargs):
        nonlocal quota_calls
        del args, kwargs
        quota_calls += 1

    monkeypatch.setattr(store_module, "_verify_managed_disk_quota", observe_quota)
    with pytest.raises(StoreCorruptionError, match="store identity"):
        CoreControlStoreV1(first)
    assert quota_calls == 0
    assert keep.read_text(encoding="utf-8") == "keep"


@pytest.mark.parametrize("marker_present", [False, True])
def test_pending_store_identity_crash_window_recovers(
    tmp_path: Path, marker_present: bool
) -> None:
    CoreControlStoreV1(tmp_path).close()
    root = tmp_path / "core-control-v1"
    database = root / "provider.sqlite3"
    marker = root / "provider.identity"
    with sqlite3.connect(database) as connection:
        connection.execute(
            "UPDATE store_identity SET binding_state = 'pending', "
            "marker_dev = NULL, marker_ino = NULL WHERE singleton = 1"
        )
    if not marker_present:
        marker.unlink()

    recovered = CoreControlStoreV1(tmp_path)
    recovered.close()
    with sqlite3.connect(database) as connection:
        identity = connection.execute(
            "SELECT binding_state, marker_dev, marker_ino FROM store_identity WHERE singleton = 1"
        ).fetchone()
    assert identity is not None
    assert identity[0] == "bound"
    assert (identity[1], identity[2]) == (
        marker.stat(follow_symlinks=False).st_dev,
        marker.stat(follow_symlinks=False).st_ino,
    )


def test_first_restart_recovers_identity_bind_crash_before_commit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    script = """
import os
import sys

from openevo.backend.contracts.v1 import store as store_module

original_exit = store_module._Transaction.__exit__

def crash_before_identity_bind_commit(transaction, exc_type, exc, traceback):
    if exc_type is None:
        try:
            row = transaction.connection.execute(
                "SELECT binding_state FROM store_identity WHERE singleton = 1"
            ).fetchone()
        except Exception:
            row = None
        if row is not None and row[0] == "bound":
            os._exit(92)
    return original_exit(transaction, exc_type, exc, traceback)

store_module._Transaction.__exit__ = crash_before_identity_bind_commit
store_module.CoreControlStoreV1(sys.argv[1])
raise SystemExit(3)
"""
    crashed = subprocess.run(
        [sys.executable, "-c", script, os.fspath(tmp_path)],
        check=False,
        capture_output=True,
        text=True,
        cwd=Path(__file__).parents[2],
    )
    assert crashed.returncode == 92, crashed.stderr

    root = tmp_path / "core-control-v1"
    marker = root / "provider.identity"
    journal = root / "provider.sqlite3-journal"
    journal_identity = journal.stat(follow_symlinks=False)
    assert marker.is_file()
    recovery_observations = 0

    def observe_non_hot_journal() -> None:
        nonlocal recovery_observations
        current = journal.stat(follow_symlinks=False)
        assert (current.st_dev, current.st_ino) == (
            journal_identity.st_dev,
            journal_identity.st_ino,
        )
        assert current.st_nlink == 1
        recovery_observations += 1

    monkeypatch.setattr(store_module, "_after_sqlite_recovery", observe_non_hot_journal)

    recovered = CoreControlStoreV1(tmp_path)
    try:
        assert recovery_observations == 1
        row = recovered._read_store_identity_row()
        assert row["binding_state"] == "bound"
        authority_name = "provider.sqlite3-journal"
        assert authority_name in recovered._database_fds
        consumed = os.fstat(recovered._database_fds[authority_name])
        assert (consumed.st_dev, consumed.st_ino) == (
            journal_identity.st_dev,
            journal_identity.st_ino,
        )
        assert consumed.st_nlink == 0
        assert not journal.exists()
    finally:
        recovered.close()


def test_pending_identity_recovers_after_unpublished_marker_temp(tmp_path: Path) -> None:
    CoreControlStoreV1(tmp_path).close()
    root = tmp_path / "core-control-v1"
    database = root / "provider.sqlite3"
    marker = root / "provider.identity"
    stale = root / ".provider.identity.interrupted.tmp"
    stale.write_bytes(b'{"partial":')
    stale.chmod(0o600)
    with sqlite3.connect(database) as connection:
        connection.execute(
            "UPDATE store_identity SET binding_state = 'pending', "
            "marker_dev = NULL, marker_ino = NULL WHERE singleton = 1"
        )
    marker.unlink()

    recovered = CoreControlStoreV1(tmp_path)
    recovered.close()
    assert marker.exists()
    assert stale.read_bytes() == b'{"partial":'


@pytest.mark.parametrize("failure_point", ["before_publish", "after_publish"])
def test_fresh_identity_bootstrap_recovers_across_marker_publication_crash(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure_point: str,
) -> None:
    root = tmp_path / "core-control-v1"
    marker = root / "provider.identity"
    if failure_point == "before_publish":
        real_operation = store_module._rename_noreplace

        def interrupt_publish(*args, **kwargs):
            del args, kwargs
            raise OSError("injected pre-publication crash")

        monkeypatch.setattr(store_module, "_rename_noreplace", interrupt_publish)
    else:
        real_operation = CoreControlStoreV1._verify_store_identity_marker
        interrupted = False

        def interrupt_after_publish(self, expected):
            nonlocal interrupted
            if not interrupted:
                interrupted = True
                raise StoreCorruptionError("injected post-publication crash")
            return real_operation(self, expected)

        monkeypatch.setattr(
            CoreControlStoreV1,
            "_verify_store_identity_marker",
            interrupt_after_publish,
        )

    with pytest.raises(CoreControlStoreError):
        CoreControlStoreV1(tmp_path)
    database = root / "provider.sqlite3"
    with sqlite3.connect(database) as connection:
        assert store_module._schema_fingerprint(connection) == (
            store_module._expected_schema_fingerprint()
        )
        assert connection.execute(
            "SELECT binding_state, marker_dev, marker_ino FROM store_identity WHERE singleton = 1"
        ).fetchone() == ("pending", None, None)
    assert marker.exists() is (failure_point == "after_publish")

    if failure_point == "before_publish":
        monkeypatch.setattr(store_module, "_rename_noreplace", real_operation)
    else:
        monkeypatch.setattr(
            CoreControlStoreV1,
            "_verify_store_identity_marker",
            real_operation,
        )
    recovered = CoreControlStoreV1(tmp_path)
    recovered.close()
    with sqlite3.connect(database) as connection:
        assert connection.execute(
            "SELECT binding_state FROM store_identity WHERE singleton = 1"
        ).fetchone() == ("bound",)


def test_bound_identity_missing_marker_preserves_managed_state(tmp_path: Path) -> None:
    CoreControlStoreV1(tmp_path).close()
    root = tmp_path / "core-control-v1"
    marker = root / "provider.identity"
    orphan = root / "workspace-snapshots" / "workspace-snapshot-existing"
    orphan.mkdir(mode=0o700)
    keep = orphan / "keep.txt"
    keep.write_text("keep", encoding="utf-8")
    marker.unlink()

    with pytest.raises(StoreCorruptionError, match="identity marker is missing"):
        CoreControlStoreV1(tmp_path)
    assert keep.read_text(encoding="utf-8") == "keep"


@pytest.mark.parametrize("damage", ["mode", "hardlink", "symlink"])
def test_store_identity_marker_rejects_unsafe_metadata(tmp_path: Path, damage: str) -> None:
    CoreControlStoreV1(tmp_path).close()
    root = tmp_path / "core-control-v1"
    marker = root / "provider.identity"
    alias = tmp_path / "identity-alias"
    if damage == "mode":
        marker.chmod(0o644)
    elif damage == "hardlink":
        os.link(marker, alias)
    else:
        marker.rename(alias)
        marker.symlink_to(alias)
    try:
        with pytest.raises(CoreControlStoreError):
            CoreControlStoreV1(tmp_path)
    finally:
        alias.unlink(missing_ok=True)


def test_empty_legacy_store_migrates_through_pending_identity(tmp_path: Path) -> None:
    root = tmp_path / "core-control-v1"
    root.mkdir(parents=True, mode=0o700)
    database = root / "provider.sqlite3"
    with sqlite3.connect(database) as connection:
        for statement in store_module._LEGACY_SCHEMA:
            connection.execute(statement)
        connection.execute(
            "INSERT INTO metadata(key, value) VALUES ('signing_key', ?)",
            (b"l" * 32,),
        )
    database.chmod(0o600)

    migrated = CoreControlStoreV1(tmp_path)
    try:
        assert migrated._signing_key == b"l" * 32
    finally:
        migrated.close()
    with sqlite3.connect(database) as connection:
        assert connection.execute(
            "SELECT binding_state FROM store_identity WHERE singleton = 1"
        ).fetchone() == ("bound",)


def test_legacy_store_with_managed_state_is_not_claimed(tmp_path: Path) -> None:
    root = tmp_path / "core-control-v1"
    workspace_root = root / "workspace-snapshots"
    workspace_root.mkdir(parents=True, mode=0o700)
    orphan = workspace_root / "workspace-snapshot-existing"
    orphan.mkdir(mode=0o700)
    keep = orphan / "keep.txt"
    keep.write_text("keep", encoding="utf-8")
    database = root / "provider.sqlite3"
    with sqlite3.connect(database) as connection:
        for statement in store_module._LEGACY_SCHEMA:
            connection.execute(statement)
        connection.execute(
            "INSERT INTO metadata(key, value) VALUES ('signing_key', ?)",
            (b"l" * 32,),
        )
    database.chmod(0o600)

    with pytest.raises(StoreCorruptionError, match="unbound managed state"):
        CoreControlStoreV1(tmp_path)
    assert keep.read_text(encoding="utf-8") == "keep"
    assert not (root / "provider.identity").exists()


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


def _leave_hot_provider_journal(state_root: Path) -> tuple[Path, bytes]:
    store = CoreControlStoreV1(state_root)
    signing_key = store._signing_key
    store.close()
    database = state_root / "core-control-v1" / "provider.sqlite3"
    script = """
import os
import sqlite3
import sys

connection = sqlite3.connect(sys.argv[1], isolation_level=None)
connection.execute("PRAGMA journal_mode = DELETE")
connection.execute("PRAGMA synchronous = FULL")
connection.execute("PRAGMA cache_size = 2")
connection.execute("PRAGMA cache_spill = ON")
connection.execute("BEGIN IMMEDIATE")
connection.execute("UPDATE metadata SET value = ? WHERE key = 'signing_key'", (b'x' * 32,))
for index in range(64):
    connection.execute(
        "INSERT INTO events(event_id, frame_json, created_at_epoch) VALUES (?, ?, ?)",
        (f"uncommitted-{index}", b"x" * 65536, index),
    )
os._exit(91)
"""
    completed = subprocess.run(
        [sys.executable, "-c", script, os.fspath(database)],
        check=False,
        cwd=Path(__file__).parents[2],
    )
    assert completed.returncode == 91
    journal = database.with_name("provider.sqlite3-journal")
    assert journal.is_file()
    return journal, signing_key


def _test_rollback_journal_authority(
    tmp_path: Path,
) -> tuple[CoreControlStoreV1, Path, int, int]:
    root = tmp_path / "journal-authority"
    root.mkdir(mode=0o700)
    journal = root / "provider.sqlite3-journal"
    journal.write_bytes(b"held rollback journal")
    journal.chmod(0o600)
    root_fd = os.open(
        root,
        os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0),
    )
    journal_fd = os.open(
        journal.name,
        os.O_RDWR | getattr(os, "O_NOFOLLOW", 0),
        dir_fd=root_fd,
    )
    authority = object.__new__(CoreControlStoreV1)
    authority._root_fd = root_fd
    authority._database_fds = {journal.name: journal_fd}
    authority._database_identities = {journal.name: os.fstat(journal_fd)}
    authority._consumed_database_sidecars = set()
    return authority, journal, root_fd, journal_fd


@pytest.mark.parametrize("boundary", ["COMMIT", "ROLLBACK"])
@pytest.mark.parametrize("journal_outcome", ["preserved", "consumed"])
def test_transaction_boundary_reconciles_allowed_rollback_journal_states(
    tmp_path: Path,
    boundary: str,
    journal_outcome: str,
) -> None:
    authority, journal, root_fd, journal_fd = _test_rollback_journal_authority(tmp_path)
    connection = sqlite3.connect(":memory:", isolation_level=None)
    events: list[str] = []
    mutated = False

    def mutate_at_boundary(statement: str) -> None:
        nonlocal mutated
        if statement.strip().upper() != boundary or mutated:
            return
        mutated = True
        if journal_outcome == "consumed":
            journal.unlink()

    def reconcile() -> None:
        events.append("reconcile")
        authority._reconcile_rollback_journal_authority()

    def verify() -> None:
        events.append("verify")
        authority._verify_database_authority()

    connection.set_trace_callback(mutate_at_boundary)
    transaction = store_module._Transaction(connection, verify, reconcile, lambda: None)
    try:
        if boundary == "COMMIT":
            with transaction:
                connection.execute("SELECT 1")
            assert transaction.outcome == "committed"
        else:
            with pytest.raises(RuntimeError, match="force rollback"):
                with transaction:
                    raise RuntimeError("force rollback")
            assert transaction.outcome == "rolled_back"
        assert mutated
        assert events[-2:] == ["reconcile", "verify"]
        assert (journal.name in authority._consumed_database_sidecars) is (
            journal_outcome == "consumed"
        )
    finally:
        connection.close()
        os.close(journal_fd)
        os.close(root_fd)


@pytest.mark.parametrize("boundary", ["COMMIT", "ROLLBACK"])
@pytest.mark.parametrize(
    "damage",
    ["replacement", "replacement_after_consumed", "mode", "hardlink", "owner"],
)
def test_transaction_boundary_rejects_unsafe_rollback_journal_transition(
    tmp_path: Path,
    boundary: str,
    damage: str,
) -> None:
    if damage == "owner" and os.geteuid() != 0:
        pytest.skip("changing journal ownership requires root")
    authority, journal, root_fd, journal_fd = _test_rollback_journal_authority(tmp_path)
    connection = sqlite3.connect(":memory:", isolation_level=None)
    alias = tmp_path / "journal-alias"
    events: list[str] = []
    mutated = False

    def damage_at_boundary(statement: str) -> None:
        nonlocal mutated
        if statement.strip().upper() != boundary or mutated:
            return
        mutated = True
        if damage == "replacement":
            journal.unlink()
            journal.write_bytes(b"replacement journal")
            journal.chmod(0o600)
        elif damage == "replacement_after_consumed":
            journal.unlink()
            authority._reconcile_rollback_journal_authority()
            journal.write_bytes(b"replacement after consumption")
            journal.chmod(0o600)
        elif damage == "mode":
            os.fchmod(journal_fd, 0o640)
        elif damage == "hardlink":
            os.link(journal, alias)
        else:
            os.fchown(journal_fd, 1, -1)

    def reconcile() -> None:
        events.append("reconcile")
        authority._reconcile_rollback_journal_authority()

    def verify() -> None:
        events.append("verify")
        authority._verify_database_authority()

    connection.set_trace_callback(damage_at_boundary)
    transaction = store_module._Transaction(connection, verify, reconcile, lambda: None)
    expected_error = (
        store_module.PostCommitStoreError if boundary == "COMMIT" else StoreCorruptionError
    )
    try:
        with pytest.raises(expected_error):
            with transaction:
                if boundary == "ROLLBACK":
                    raise RuntimeError("force rollback")
                connection.execute("SELECT 1")
        assert mutated
        assert events[-1] == "reconcile"
    finally:
        connection.close()
        os.close(journal_fd)
        os.close(root_fd)


def test_startup_pins_and_recovers_real_hot_rollback_journal(tmp_path: Path) -> None:
    journal, signing_key = _leave_hot_provider_journal(tmp_path)
    journal_identity = journal.stat(follow_symlinks=False)

    recovered = CoreControlStoreV1(tmp_path)
    try:
        assert recovered._signing_key == signing_key
        authority_name = "provider.sqlite3-journal"
        assert authority_name in recovered._database_fds
        consumed = os.fstat(recovered._database_fds[authority_name])
        assert (consumed.st_dev, consumed.st_ino) == (
            journal_identity.st_dev,
            journal_identity.st_ino,
        )
        assert consumed.st_nlink == 0
        assert not journal.exists()
    finally:
        recovered.close()


@pytest.mark.parametrize("damage", ["symlink", "owner", "mode", "hardlink"])
def test_startup_rejects_unsafe_rollback_journal_before_sqlite_connect(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    damage: str,
) -> None:
    journal, _signing_key = _leave_hot_provider_journal(tmp_path)
    alias = tmp_path / "journal-alias"
    if damage == "symlink":
        journal.rename(alias)
        journal.symlink_to(alias)
    elif damage == "owner":
        if os.geteuid() != 0:
            pytest.skip("changing journal ownership requires root")
        os.chown(journal, 1, -1)
    elif damage == "mode":
        journal.chmod(0o644)
    else:
        os.link(journal, alias)
    connect_calls = 0
    real_connect = store_module.sqlite3.connect

    def observe_connect(*args, **kwargs):
        nonlocal connect_calls
        connect_calls += 1
        return real_connect(*args, **kwargs)

    monkeypatch.setattr(store_module.sqlite3, "connect", observe_connect)
    try:
        with pytest.raises(CoreControlStoreError):
            CoreControlStoreV1(tmp_path)
        assert connect_calls == 0
    finally:
        if damage == "symlink":
            journal.unlink(missing_ok=True)
        alias.unlink(missing_ok=True)


def test_hot_journal_replacement_during_sqlite_recovery_fails_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    journal, _signing_key = _leave_hot_provider_journal(tmp_path)
    original_identity = journal.stat(follow_symlinks=False)
    replacement_identity: os.stat_result | None = None

    def replace_after_recovery() -> None:
        nonlocal replacement_identity
        assert not journal.exists()
        journal.write_bytes(b"replacement journal")
        journal.chmod(0o600)
        replacement_identity = journal.stat(follow_symlinks=False)

    monkeypatch.setattr(
        store_module,
        "_after_sqlite_recovery",
        replace_after_recovery,
    )

    with pytest.raises(StoreCorruptionError, match="journal.*replaced"):
        CoreControlStoreV1(tmp_path)

    assert replacement_identity is not None
    assert (replacement_identity.st_dev, replacement_identity.st_ino) != (
        original_identity.st_dev,
        original_identity.st_ino,
    )
    assert journal.read_bytes() == b"replacement journal"


def test_hot_journal_authority_coexists_with_wal_and_shm_entries(tmp_path: Path) -> None:
    journal, signing_key = _leave_hot_provider_journal(tmp_path)
    root = journal.parent
    wal = root / "provider.sqlite3-wal"
    shm = root / "provider.sqlite3-shm"
    wal.touch(mode=0o600)
    shm.touch(mode=0o600)

    recovered = CoreControlStoreV1(tmp_path)
    try:
        assert recovered._signing_key == signing_key
        assert set(recovered._database_fds) == {
            "provider.sqlite3",
            "provider.sqlite3-journal",
            "provider.sqlite3-wal",
            "provider.sqlite3-shm",
        }
    finally:
        recovered.close()


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


class _RecordingServiceSupervisor:
    def __init__(self) -> None:
        self.close_calls = 0
        self._services = (
            SupervisorServiceSummary(
                id="evolution-backend",
                display_name="Evolution backend",
                component=ServiceComponent.EVOLUTION_BACKEND,
                status=ServiceStatus.RUNNING,
                restartable=True,
                status_message="Ready.",
                error_code=None,
                updated_at="2026-07-14T00:00:00Z",
                observed_at="2026-07-14T00:00:01Z",
                identity_digest="a" * 64,
                pid=1234,
                port=8200,
                etag='"' + "b" * 64 + '"',
            ),
        )

    def list(self) -> tuple[SupervisorServiceSummary, ...]:
        return self._services

    def close(self) -> None:
        self.close_calls += 1


class _FailingServiceSupervisor(_RecordingServiceSupervisor):
    def list(self) -> tuple[SupervisorServiceSummary, ...]:
        raise SupervisorError("managed service state is unavailable")


class _RecordingRunControl:
    def __init__(self) -> None:
        self.close_calls = 0
        self.invocations: list[tuple[str, Mapping[str, object]]] = []

    def invoke(self, operation_id: str, arguments: Mapping[str, object]) -> object:
        self.invocations.append((operation_id, arguments))
        if operation_id == "listCoreRunsV1":
            return m.RunPageV1(items=[], next_cursor=None, has_more=False)
        raise AssertionError(operation_id)

    def counts(self) -> tuple[int, int]:
        return (2, 3)

    async def verify(self, _check) -> None:
        return None

    def close(self) -> None:
        self.close_calls += 1


class _ArtifactRunControl(_RecordingRunControl):
    def __init__(self) -> None:
        super().__init__()
        self.artifacts: dict[str, list[m.ArtifactSummaryV1]] = {}

    def invoke(self, operation_id: str, arguments: Mapping[str, object]) -> object:
        self.invocations.append((operation_id, arguments))
        if operation_id != "listCoreRunArtifactsV1":
            return super().invoke(operation_id, arguments)
        run_id = str(arguments["run_id"])
        if run_id not in self.artifacts:
            raise CoreRunControlError(
                "run_not_found",
                "The run does not exist.",
                http_status=404,
                retryable=False,
            )
        artifact_type = arguments["artifact_type"]
        values = [
            artifact
            for artifact in self.artifacts[run_id]
            if artifact_type is None or artifact.artifact_type is artifact_type
        ]
        return m.ArtifactPageV1(items=values, next_cursor=None, has_more=False)


def _artifact_payload(
    artifact_root: Path,
    *,
    artifact_id: str,
    artifact_type: m.ArtifactType,
    content: str | bytes,
    extra_documents: Mapping[str, str | bytes] | None = None,
) -> tuple[dict[str, object], str, int, Path]:
    output = artifact_root / "workers" / f"job-{artifact_id}" / str(artifact_type)
    output.mkdir(mode=0o700, parents=True)
    if artifact_type is m.ArtifactType.TEXT_MEMORY:
        relative_path = "memory.md"
        manifest: dict[str, object] = {"content_path": relative_path, "record_count": 1}
        uri_path = output / relative_path
    elif artifact_type is m.ArtifactType.SKILL_BUNDLE:
        relative_path = "SKILL.md"
        manifest = {"entrypoint": relative_path, "files": [relative_path]}
        uri_path = output
    elif artifact_type is m.ArtifactType.AGENT_SYSTEM:
        relative_path = "AGENTS.md"
        manifest = {"content_path": relative_path, "target_path": relative_path}
        uri_path = output / relative_path
    else:
        relative_path = "adapter.bin"
        manifest = {
            "adapter_id": artifact_id,
            "base_model": "gpt-5.1-codex-mini",
            "adapter_format": "lora",
        }
        uri_path = output
    primary = output / relative_path
    primary.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    if isinstance(content, bytes):
        primary.write_bytes(content)
    else:
        primary.write_text(content, encoding="utf-8")
    for path, value in (extra_documents or {}).items():
        destination = output / path
        destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        if isinstance(value, bytes):
            destination.write_bytes(value)
        else:
            destination.write_text(value, encoding="utf-8")
    record: dict[str, object] = {
        "artifact_id": artifact_id,
        "type": str(artifact_type),
        "name": f"{artifact_type.value} artifact",
        "version": 1,
        "state": "active",
        "uri": uri_path.absolute().as_uri(),
        "manifest": manifest,
        "compatibility": {},
        "scores": {},
        "tags": [],
        "promoted": True,
    }
    with ArtifactPayloadService(artifact_root) as payloads:
        snapshot = payloads.issue_snapshot(
            artifact_id=artifact_id,
            artifact_type=str(artifact_type),
            name=str(record["name"]),
            uri=str(record["uri"]),
            manifest=manifest,
            scores={},
            rank_index=0,
        )
    return (
        record,
        snapshot.payload_manifest_digest,
        sum(entry.size_bytes for entry in snapshot.payload_entries),
        primary,
    )


def _ready_provider_project(app, registry, *, key: str = "artifact-project-create") -> m.ProjectV1:
    result = app.state.core_control_provider.store.create_project(
        m.ProjectCreateV1.model_validate(_project_create()),
        idempotency_key=key,
        registry_digest=registry.snapshot.registry_digest,
    )
    assert isinstance(result.model, m.ProjectV1)
    assert result.model.active_revision is not None
    return result.model


def _publish_artifact_summary(
    app,
    run_control: _ArtifactRunControl,
    project: m.ProjectV1,
    *,
    run_id: str,
    record: Mapping[str, object],
    content_sha256: str,
    byte_size: int,
    source_artifact_ids: list[str] | None = None,
) -> tuple[m.ProjectV1, m.ArtifactSummaryV1]:
    artifact_id = str(record["artifact_id"])
    artifact_type = m.ArtifactType(str(record["type"]))
    assert project.active_revision is not None
    revision = app.state.core_control_provider.store.activate_evolution_revision(
        project.id,
        predecessor=project.active_revision,
        run_id=run_id,
        context_artifact_ids={artifact_type.value: [artifact_id]},
    )
    project = app.state.core_control_provider.store.get_project(project.id)
    common: dict[str, object] = {
        "id": artifact_id,
        "project_id": project.id,
        "run_id": run_id,
        "target_id": artifact_type.value,
        "display_name": record["name"],
        "summary": "Verified provider artifact fixture.",
        "byte_size": byte_size,
        "produced_revision": revision.revision,
        "membership_revisions": [revision.revision],
        "content_sha256": content_sha256,
        "selected": True,
        "promoted": True,
        "release_enabled": artifact_type is not m.ArtifactType.PARAMETRIC_MEMORY,
        "compatibility": {
            "execution_modes": [project.spec.execution_mode],
            "harness_ids": [project.spec.harness_id],
            "base_model_refs": [project.spec.agent_model_ref],
        },
        "lineage": {
            "method_id": f"{artifact_type.value}_method",
            "job_id": f"job-{artifact_id}",
            "source_dataset_ids": ["dataset-1"],
            "source_artifact_ids": source_artifact_ids or [],
        },
        "scores": [{"name": "quality", "value": 1.0}],
        "created_at": revision.created_at,
        "artifact_type": artifact_type,
    }
    if artifact_type is m.ArtifactType.TEXT_MEMORY:
        common["metadata"] = {"record_count": 1, "source_dataset_ids": ["dataset-1"]}
        summary = m.TextMemoryArtifactSummaryV1.model_validate(common)
    elif artifact_type is m.ArtifactType.SKILL_BUNDLE:
        common["metadata"] = {
            "document_count": 1,
            "root_document": "SKILL.md",
        }
        summary = m.SkillBundleArtifactSummaryV1.model_validate(common)
    elif artifact_type is m.ArtifactType.AGENT_SYSTEM:
        common["metadata"] = {"target_path": "AGENTS.md"}
        summary = m.AgentSystemArtifactSummaryV1.model_validate(common)
    else:
        common["release_enabled"] = False
        common["metadata"] = {
            "adapter_id": artifact_id,
            "base_model_ref": project.spec.agent_model_ref,
            "adapter_format": "lora",
        }
        summary = m.ParametricMemoryArtifactSummaryV1.model_validate(common)
    run_control.artifacts[run_id] = [summary]
    return project, summary


class _FailingRunControl(_RecordingRunControl):
    def invoke(self, operation_id: str, arguments: Mapping[str, object]) -> object:
        del operation_id, arguments
        raise CoreRunControlError(
            "run_owner_unavailable",
            "The managed run owner is unavailable.",
            http_status=503,
            retryable=True,
        )

    def counts(self) -> tuple[int, int]:
        raise CoreRunControlError(
            "run_owner_unavailable",
            "The managed run owner is unavailable.",
            http_status=503,
            retryable=True,
        )


class _MutationFailingRunControl(_RecordingRunControl):
    def __init__(self, *, retryable: bool, fail_once: bool = False) -> None:
        super().__init__()
        self.retryable = retryable
        self.fail_once = fail_once

    def invoke(self, operation_id: str, arguments: Mapping[str, object]) -> object:
        self.invocations.append((operation_id, arguments))
        if self.fail_once and len(self.invocations) > 1:
            return "owner-retried"
        raise CoreRunControlError(
            "run_owner_temporarily_unavailable" if self.retryable else "run_request_rejected",
            "The managed run owner rejected the operation.",
            http_status=503 if self.retryable else 409,
            retryable=self.retryable,
        )


class _SuccessfulMutationRunControl(_RecordingRunControl):
    def invoke(self, operation_id: str, arguments: Mapping[str, object]) -> object:
        self.invocations.append((operation_id, arguments))
        return "owner-retried"


class _BlockingSuccessfulRunControl(_RecordingRunControl):
    def __init__(self) -> None:
        super().__init__()
        self.entered = threading.Event()
        self.release = threading.Event()

    def invoke(self, operation_id: str, arguments: Mapping[str, object]) -> object:
        self.invocations.append((operation_id, arguments))
        self.entered.set()
        assert self.release.wait(timeout=5)
        return "owner-retried"


class _BlockingRetryableThenSuccessfulRunControl(_RecordingRunControl):
    def __init__(self) -> None:
        super().__init__()
        self.entered = threading.Event()
        self.release = threading.Event()

    def invoke(self, operation_id: str, arguments: Mapping[str, object]) -> object:
        self.invocations.append((operation_id, arguments))
        if len(self.invocations) == 1:
            self.entered.set()
            assert self.release.wait(timeout=5)
            raise CoreRunControlError(
                "run_owner_temporarily_unavailable",
                "The managed run owner rejected the operation.",
                http_status=503,
                retryable=True,
            )
        return "owner-retried"


class _BlockingNonRetryableRunControl(_RecordingRunControl):
    def __init__(self) -> None:
        super().__init__()
        self.entered = threading.Event()
        self.release = threading.Event()

    def invoke(self, operation_id: str, arguments: Mapping[str, object]) -> object:
        self.invocations.append((operation_id, arguments))
        self.entered.set()
        assert self.release.wait(timeout=5)
        raise CoreRunControlError(
            "run_request_rejected",
            "The managed run owner rejected the operation.",
            http_status=409,
            retryable=False,
        )


def _run_failure_error(*, retryable: bool) -> ApiErrorV1:
    error = CoreRunControlError(
        "run_owner_temporarily_unavailable" if retryable else "run_request_rejected",
        "The managed run owner rejected the operation.",
        http_status=503 if retryable else 409,
        retryable=retryable,
    )
    return provider_module._run_control_http_error(error).error


def _persist_legacy_failure(
    state_root: Path,
    operation_id: str,
    arguments: Mapping[str, object],
    *,
    retryable: bool,
) -> ApiErrorV1:
    error = _run_failure_error(retryable=retryable)
    store = CoreControlStoreV1(state_root)
    try:
        store.record_failed_idempotency(operation_id, arguments, error)
    finally:
        store.close()
    return error


def test_injected_service_supervisor_is_projected_and_closed(tmp_path: Path) -> None:
    supervisor = _RecordingServiceSupervisor()
    app = _app(tmp_path, service_supervisor=supervisor)
    with TestClient(app) as client:
        services = client.get("/v1/services", headers=AUTH)
        assert services.status_code == 200
        assert [item["id"] for item in services.json()["items"]] == [
            "core-control",
            "evolution-backend",
        ]
        evolution = client.get("/v1/services/evolution-backend", headers=AUTH)
        assert evolution.status_code == 200
        assert evolution.json()["kind"] == "control"
        assert evolution.json()["status"] == "running"
        assert evolution.headers["etag"] == '"' + "b" * 64 + '"'

    assert supervisor.close_calls == 1


def test_service_supervisor_failure_is_typed_and_retryable(tmp_path: Path) -> None:
    app = _app(tmp_path, service_supervisor=_FailingServiceSupervisor())
    with TestClient(app) as client:
        response = client.get("/v1/services", headers=AUTH)

    assert response.status_code == 503
    assert response.json()["code"] == "core_service_supervisor_failed"
    assert response.json()["retryable"] is True


def test_injected_run_control_owns_frozen_routes_and_status_counts(tmp_path: Path) -> None:
    run_control = _RecordingRunControl()
    app = _app(tmp_path, run_control=run_control)
    with TestClient(app) as client:
        response = client.get("/v1/runs", headers=AUTH)
        status = client.get("/v1/status", headers=AUTH)

    assert response.status_code == 200
    assert response.json() == {
        "schema_version": "1",
        "items": [],
        "next_cursor": None,
        "has_more": False,
    }
    assert run_control.invocations[0][0] == "listCoreRunsV1"
    assert status.json()["active_runs"] == 2
    assert status.json()["queued_runs"] == 3
    assert run_control.close_calls == 1


def test_run_control_factory_receives_and_shares_the_provider_store(tmp_path: Path) -> None:
    run_control = _RecordingRunControl()
    stores: list[CoreControlStoreV1] = []

    def factory(store: CoreControlStoreV1) -> _RecordingRunControl:
        stores.append(store)
        return run_control

    app = _app(tmp_path, run_control_factory=factory)
    with TestClient(app) as client:
        response = client.get("/v1/runs", headers=AUTH)
        assert response.status_code == 200
        assert stores == [app.state.core_control_provider.store]

    assert run_control.close_calls == 1


def test_run_control_factory_and_instance_are_mutually_exclusive(tmp_path: Path) -> None:
    run_control = _RecordingRunControl()
    with pytest.raises(ValueError, match="mutually exclusive"):
        _app(
            tmp_path,
            run_control=run_control,
            run_control_factory=lambda _store: run_control,
        )
    assert not (tmp_path / "core-control-v1").exists()


def test_run_control_operation_ids_exactly_match_frozen_run_routes(tmp_path: Path) -> None:
    app = _app(tmp_path)
    with TestClient(app):
        frozen_run_operation_ids = frozenset(
            route.operation_id
            for route in provider_module._iter_api_routes(app.routes)
            if route.path.startswith("/v1/runs")
        )

    assert RUN_OPERATION_IDS == frozen_run_operation_ids


@pytest.mark.parametrize(
    ("artifact_type", "content", "relative_path"),
    [
        (m.ArtifactType.TEXT_MEMORY, "# Memory\n\nKeep verified evidence.\n", "memory.md"),
        (m.ArtifactType.SKILL_BUNDLE, "# Skill\n\nUse the verified workflow.\n", "SKILL.md"),
        (m.ArtifactType.AGENT_SYSTEM, "# Agent system\n\nCheck every result.\n", "AGENTS.md"),
    ],
)
def test_artifact_list_get_and_verified_text_content(
    tmp_path: Path,
    artifact_type: m.ArtifactType,
    content: str,
    relative_path: str,
) -> None:
    registry = verified_builtin_registry(tmp_path / "registry")
    artifact_root = tmp_path / "managed-artifacts"
    artifact_id = f"artifact-{artifact_type.value}"
    record, digest, byte_size, _primary = _artifact_payload(
        artifact_root,
        artifact_id=artifact_id,
        artifact_type=artifact_type,
        content=content,
    )
    records = {artifact_id: record}
    run_control = _ArtifactRunControl()
    app = _app(
        tmp_path / "state",
        registry=registry,
        run_control=run_control,
        evolution_artifact_root=artifact_root,
        artifact_loader=records.__getitem__,
    )
    project = _ready_provider_project(app, registry)
    _project, summary = _publish_artifact_summary(
        app,
        run_control,
        project,
        run_id=f"run-{artifact_type.value}",
        record=record,
        content_sha256=digest,
        byte_size=byte_size,
    )
    with TestClient(app) as client:
        listed = client.get(
            f"/v1/runs/{summary.run_id}/artifacts",
            headers=AUTH,
        )
        fetched = client.get(f"/v1/projects/{project.id}/artifacts/{artifact_id}", headers=AUTH)
        preview = client.get(
            f"/v1/projects/{project.id}/artifacts/{artifact_id}/content", headers=AUTH
        )

    assert listed.status_code == fetched.status_code == preview.status_code == 200
    assert listed.json()["items"] == [fetched.json()]
    assert "uri" not in fetched.json()
    assert preview.json() == {
        "schema_version": "1",
        "artifact_id": artifact_id,
        "artifact_type": artifact_type.value,
        "documents": [
            {
                "document_id": provider_module._artifact_document_id(artifact_id, relative_path),
                "display_name": relative_path,
                "relative_path": relative_path,
                "mime_type": "text/markdown",
                "content": content,
                "content_sha256": hashlib.sha256(content.encode("utf-8")).hexdigest(),
                "byte_size": len(content.encode("utf-8")),
                "truncated": False,
            }
        ],
        "total_documents": 1,
        "total_utf8_bytes": len(content.encode("utf-8")),
        "returned_utf8_bytes": len(content.encode("utf-8")),
        "truncated": False,
    }


def test_artifact_lookup_does_not_authorize_unreachable_evolution_output(
    tmp_path: Path,
) -> None:
    registry = verified_builtin_registry(tmp_path / "registry")
    artifact_root = tmp_path / "managed-artifacts"
    record, _digest, _byte_size, _primary = _artifact_payload(
        artifact_root,
        artifact_id="foreign-artifact",
        artifact_type=m.ArtifactType.TEXT_MEMORY,
        content="secret foreign output\n",
    )
    loader_calls: list[str] = []

    def loader(artifact_id: str) -> Mapping[str, object]:
        loader_calls.append(artifact_id)
        return record

    app = _app(
        tmp_path / "state",
        registry=registry,
        run_control=_ArtifactRunControl(),
        evolution_artifact_root=artifact_root,
        artifact_loader=loader,
    )
    project = _ready_provider_project(app, registry)
    with TestClient(app) as client:
        fetched = client.get(f"/v1/projects/{project.id}/artifacts/foreign-artifact", headers=AUTH)
        content = client.get(
            f"/v1/projects/{project.id}/artifacts/foreign-artifact/content", headers=AUTH
        )

    assert fetched.status_code == content.status_code == 404
    assert fetched.json()["code"] == content.json()["code"] == "artifact_not_found"
    assert loader_calls == []


def test_artifact_content_uses_declared_503_when_authority_is_unavailable(
    tmp_path: Path,
) -> None:
    registry = verified_builtin_registry(tmp_path / "registry")
    artifact_root = tmp_path / "managed-artifacts"
    record, digest, byte_size, _primary = _artifact_payload(
        artifact_root,
        artifact_id="unavailable-authority-memory",
        artifact_type=m.ArtifactType.TEXT_MEMORY,
        content="authority unavailable\n",
    )
    run_control = _ArtifactRunControl()
    app = _app(
        tmp_path / "state",
        registry=registry,
        run_control=run_control,
        artifact_loader={"unavailable-authority-memory": record}.__getitem__,
    )
    project = _ready_provider_project(app, registry)
    _project, summary = _publish_artifact_summary(
        app,
        run_control,
        project,
        run_id="run-unavailable-authority",
        record=record,
        content_sha256=digest,
        byte_size=byte_size,
    )

    with TestClient(app) as client:
        response = client.get(
            f"/v1/projects/{project.id}/artifacts/{summary.id}/content", headers=AUTH
        )

    assert response.status_code == 503
    assert response.json()["code"] == "artifact_authority_invalid"


@pytest.mark.parametrize("suffix", ["", "/content", "/diff"])
def test_artifact_routes_translate_missing_authoritative_run_to_retryable_503(
    tmp_path: Path,
    suffix: str,
) -> None:
    registry = verified_builtin_registry(tmp_path / "registry")
    artifact_root = tmp_path / "managed-artifacts"
    artifact_id = "missing-authoritative-run"
    record, digest, byte_size, _primary = _artifact_payload(
        artifact_root,
        artifact_id=artifact_id,
        artifact_type=m.ArtifactType.TEXT_MEMORY,
        content="durably reachable\n",
    )
    run_control = _ArtifactRunControl()
    app = _app(
        tmp_path / "state",
        registry=registry,
        run_control=run_control,
        evolution_artifact_root=artifact_root,
        artifact_loader={artifact_id: record}.__getitem__,
    )
    project = _ready_provider_project(app, registry)
    _project, summary = _publish_artifact_summary(
        app,
        run_control,
        project,
        run_id="run-missing-authority",
        record=record,
        content_sha256=digest,
        byte_size=byte_size,
    )
    run_control.artifacts.clear()

    with TestClient(app) as client:
        response = client.get(
            f"/v1/projects/{project.id}/artifacts/{summary.id}{suffix}", headers=AUTH
        )

    assert response.status_code == 503
    assert response.json()["code"] == "artifact_authority_invalid"
    assert response.json()["retryable"] is True


@pytest.mark.parametrize(
    "failure",
    ["wrong_response_type", "empty", "duplicate", "identity_mismatch", "run_corruption"],
)
def test_artifact_lookup_translates_invalid_run_authority_to_retryable_503(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure: str,
) -> None:
    registry = verified_builtin_registry(tmp_path / "registry")
    artifact_root = tmp_path / "managed-artifacts"
    artifact_id = f"invalid-run-authority-{failure}"
    record, digest, byte_size, _primary = _artifact_payload(
        artifact_root,
        artifact_id=artifact_id,
        artifact_type=m.ArtifactType.TEXT_MEMORY,
        content="durably reachable\n",
    )
    run_control = _ArtifactRunControl()
    app = _app(
        tmp_path / "state",
        registry=registry,
        run_control=run_control,
        evolution_artifact_root=artifact_root,
        artifact_loader={artifact_id: record}.__getitem__,
    )
    project = _ready_provider_project(app, registry)
    _project, summary = _publish_artifact_summary(
        app,
        run_control,
        project,
        run_id=f"run-invalid-authority-{failure}",
        record=record,
        content_sha256=digest,
        byte_size=byte_size,
    )
    assert summary.run_id is not None

    if failure == "wrong_response_type":
        monkeypatch.setattr(
            run_control,
            "invoke",
            lambda _operation_id, _arguments: m.RunPageV1(
                items=[], next_cursor=None, has_more=False
            ),
        )
    elif failure == "empty":
        run_control.artifacts[summary.run_id] = []
    elif failure == "duplicate":
        run_control.artifacts[summary.run_id] = [summary, summary]
    elif failure == "identity_mismatch":
        run_control.artifacts[summary.run_id] = [
            summary.model_copy(update={"run_id": "run-authority-mismatch"})
        ]
    else:

        def fail_with_run_corruption(
            _operation_id: str, _arguments: Mapping[str, object]
        ) -> object:
            raise CoreRunControlError(
                "run_store_corrupt",
                "The run authority store is corrupt.",
                http_status=500,
                retryable=False,
            )

        monkeypatch.setattr(run_control, "invoke", fail_with_run_corruption)

    with TestClient(app) as client:
        response = client.get(f"/v1/projects/{project.id}/artifacts/{summary.id}", headers=AUTH)

    assert response.status_code == 503
    assert response.json()["code"] == "artifact_authority_invalid"
    assert response.json()["retryable"] is True


def test_artifact_lookup_translates_durable_authority_store_corruption_to_503(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry = verified_builtin_registry(tmp_path / "registry")
    artifact_root = tmp_path / "managed-artifacts"
    artifact_id = "corrupt-durable-artifact-authority"
    record, digest, byte_size, _primary = _artifact_payload(
        artifact_root,
        artifact_id=artifact_id,
        artifact_type=m.ArtifactType.TEXT_MEMORY,
        content="durably reachable\n",
    )
    run_control = _ArtifactRunControl()
    app = _app(
        tmp_path / "state",
        registry=registry,
        run_control=run_control,
        evolution_artifact_root=artifact_root,
        artifact_loader={artifact_id: record}.__getitem__,
    )
    project = _ready_provider_project(app, registry)
    _project, summary = _publish_artifact_summary(
        app,
        run_control,
        project,
        run_id="run-corrupt-durable-authority",
        record=record,
        content_sha256=digest,
        byte_size=byte_size,
    )

    def fail_reachability(*_args: object, **_kwargs: object) -> object:
        raise StoreCorruptionError("durable artifact authority is corrupt")

    monkeypatch.setattr(
        app.state.core_control_provider.store,
        "artifact_reachability",
        fail_reachability,
    )

    with TestClient(app) as client:
        response = client.get(f"/v1/projects/{project.id}/artifacts/{summary.id}", headers=AUTH)

    assert response.status_code == 503
    assert response.json()["code"] == "artifact_authority_invalid"
    assert response.json()["retryable"] is True


@pytest.mark.parametrize("failure_type", [RuntimeError, asyncio.CancelledError])
def test_artifact_lookup_does_not_translate_unrelated_run_owner_failures(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure_type: type[BaseException],
) -> None:
    registry = verified_builtin_registry(tmp_path / "registry")
    artifact_root = tmp_path / "managed-artifacts"
    artifact_id = f"internal-failure-{failure_type.__name__.lower()}"
    record, digest, byte_size, _primary = _artifact_payload(
        artifact_root,
        artifact_id=artifact_id,
        artifact_type=m.ArtifactType.TEXT_MEMORY,
        content="durably reachable\n",
    )
    run_control = _ArtifactRunControl()
    app = _app(
        tmp_path / "state",
        registry=registry,
        run_control=run_control,
        evolution_artifact_root=artifact_root,
        artifact_loader={artifact_id: record}.__getitem__,
    )
    project = _ready_provider_project(app, registry)
    _project, summary = _publish_artifact_summary(
        app,
        run_control,
        project,
        run_id=f"run-internal-failure-{failure_type.__name__.lower()}",
        record=record,
        content_sha256=digest,
        byte_size=byte_size,
    )
    failure = failure_type("not an authority-domain error")

    def fail_internally(_operation_id: str, _arguments: Mapping[str, object]) -> object:
        raise failure

    monkeypatch.setattr(run_control, "invoke", fail_internally)

    with pytest.raises(failure_type, match="not an authority-domain error"):
        app.state.core_control_provider.invoke(
            "getCoreArtifactV1",
            {"project_id": project.id, "artifact_id": summary.id},
        )


def test_artifact_lookup_is_scoped_to_the_requested_active_project(tmp_path: Path) -> None:
    registry = verified_builtin_registry(tmp_path / "registry")
    artifact_root = tmp_path / "managed-artifacts"
    record, digest, byte_size, _primary = _artifact_payload(
        artifact_root,
        artifact_id="project-one-memory",
        artifact_type=m.ArtifactType.TEXT_MEMORY,
        content="project one only\n",
    )
    loader_calls: list[str] = []

    def loader(artifact_id: str) -> Mapping[str, object]:
        loader_calls.append(artifact_id)
        return record

    run_control = _ArtifactRunControl()
    app = _app(
        tmp_path / "state",
        registry=registry,
        run_control=run_control,
        evolution_artifact_root=artifact_root,
        artifact_loader=loader,
    )
    first_project = _ready_provider_project(app, registry, key="artifact-scope-first")
    second_project = _ready_provider_project(app, registry, key="artifact-scope-second")
    _first_project, summary = _publish_artifact_summary(
        app,
        run_control,
        first_project,
        run_id="run-project-one-memory",
        record=record,
        content_sha256=digest,
        byte_size=byte_size,
    )

    with TestClient(app) as client:
        response = client.get(
            f"/v1/projects/{second_project.id}/artifacts/{summary.id}",
            headers=AUTH,
        )

    assert response.status_code == 404
    assert response.json()["code"] == "artifact_not_found"
    assert response.json()["retryable"] is False
    assert loader_calls == []


@pytest.mark.parametrize("damage", ["missing", "symlink"])
def test_artifact_content_fails_closed_for_missing_or_symlink_payload(
    tmp_path: Path,
    damage: str,
) -> None:
    registry = verified_builtin_registry(tmp_path / "registry")
    artifact_root = tmp_path / "managed-artifacts"
    artifact_id = f"artifact-{damage}"
    record, digest, byte_size, primary = _artifact_payload(
        artifact_root,
        artifact_id=artifact_id,
        artifact_type=m.ArtifactType.TEXT_MEMORY,
        content="verified memory\n",
    )
    run_control = _ArtifactRunControl()
    app = _app(
        tmp_path / "state",
        registry=registry,
        run_control=run_control,
        evolution_artifact_root=artifact_root,
        artifact_loader={artifact_id: record}.__getitem__,
    )
    project = _ready_provider_project(app, registry)
    _publish_artifact_summary(
        app,
        run_control,
        project,
        run_id=f"run-{damage}",
        record=record,
        content_sha256=digest,
        byte_size=byte_size,
    )
    primary.unlink()
    if damage == "symlink":
        secret = tmp_path / "outside-secret.md"
        secret.write_text("must not be exposed\n", encoding="utf-8")
        primary.symlink_to(secret)

    with TestClient(app) as client:
        response = client.get(
            f"/v1/projects/{project.id}/artifacts/{artifact_id}/content", headers=AUTH
        )

    assert response.status_code == 422
    assert response.json()["code"] == "artifact_content_invalid"
    assert "must not be exposed" not in response.text
    assert os.fspath(tmp_path) not in response.text


@pytest.mark.parametrize(
    ("artifact_id", "artifact_type", "content", "expected_code"),
    [
        (
            "artifact-oversize",
            m.ArtifactType.TEXT_MEMORY,
            "x" * (m.MAX_ARTIFACT_PREVIEW_UTF8_BYTES + 1),
            "artifact_content_oversize",
        ),
        (
            "artifact-binary",
            m.ArtifactType.TEXT_MEMORY,
            b"\xff\xfe\x00",
            "artifact_content_invalid",
        ),
        (
            "artifact-parametric",
            m.ArtifactType.PARAMETRIC_MEMORY,
            b"adapter",
            "artifact_content_type_unsupported",
        ),
    ],
)
def test_artifact_content_rejects_oversize_binary_and_unsupported_types(
    tmp_path: Path,
    artifact_id: str,
    artifact_type: m.ArtifactType,
    content: str | bytes,
    expected_code: str,
) -> None:
    registry = verified_builtin_registry(tmp_path / "registry")
    artifact_root = tmp_path / "managed-artifacts"
    record, digest, byte_size, _primary = _artifact_payload(
        artifact_root,
        artifact_id=artifact_id,
        artifact_type=artifact_type,
        content=content,
    )
    run_control = _ArtifactRunControl()
    app = _app(
        tmp_path / "state",
        registry=registry,
        run_control=run_control,
        evolution_artifact_root=artifact_root,
        artifact_loader={artifact_id: record}.__getitem__,
    )
    project = _ready_provider_project(app, registry)
    _publish_artifact_summary(
        app,
        run_control,
        project,
        run_id=f"run-{artifact_id}",
        record=record,
        content_sha256=digest,
        byte_size=byte_size,
    )

    with TestClient(app) as client:
        response = client.get(
            f"/v1/projects/{project.id}/artifacts/{artifact_id}/content", headers=AUTH
        )

    assert response.status_code == 422
    assert response.json()["code"] == expected_code


def test_artifact_content_rejects_sparse_oversize_before_hashing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry = verified_builtin_registry(tmp_path / "registry")
    artifact_root = tmp_path / "managed-artifacts"
    primary = artifact_root / "workers" / "job-sparse" / "text_memory" / "memory.md"
    primary.parent.mkdir(mode=0o700, parents=True)
    with primary.open("wb") as stream:
        stream.truncate(4 * 1024 * 1024 * 1024)
    record: dict[str, object] = {
        "artifact_id": "artifact-sparse-oversize",
        "type": "text_memory",
        "name": "sparse oversize artifact",
        "version": 1,
        "state": "active",
        "uri": primary.absolute().as_uri(),
        "manifest": {"content_path": "memory.md", "record_count": 1},
        "compatibility": {},
        "scores": {},
        "tags": [],
        "promoted": True,
    }
    run_control = _ArtifactRunControl()
    app = _app(
        tmp_path / "state",
        registry=registry,
        run_control=run_control,
        evolution_artifact_root=artifact_root,
        artifact_loader={"artifact-sparse-oversize": record}.__getitem__,
    )
    project = _ready_provider_project(app, registry)
    _project, summary = _publish_artifact_summary(
        app,
        run_control,
        project,
        run_id="run-sparse-oversize",
        record=record,
        content_sha256="a" * 64,
        byte_size=1,
    )

    def unexpected_hash(_fd: int):
        raise AssertionError("oversize payload was hashed")

    monkeypatch.setattr(artifact_payloads_module, "_stream_fd_chunks", unexpected_hash)
    with TestClient(app) as client:
        response = client.get(
            f"/v1/projects/{project.id}/artifacts/{summary.id}/content", headers=AUTH
        )

    assert response.status_code == 422
    assert response.json()["code"] == "artifact_content_oversize"


def test_artifact_content_rejects_binary_file_outside_typed_inventory(
    tmp_path: Path,
) -> None:
    registry = verified_builtin_registry(tmp_path / "registry")
    artifact_root = tmp_path / "managed-artifacts"
    record, digest, byte_size, _primary = _artifact_payload(
        artifact_root,
        artifact_id="skill-with-hidden-binary",
        artifact_type=m.ArtifactType.SKILL_BUNDLE,
        content="# Skill\n",
        extra_documents={"hidden.md": b"\xff\xfe"},
    )
    run_control = _ArtifactRunControl()
    app = _app(
        tmp_path / "state",
        registry=registry,
        run_control=run_control,
        evolution_artifact_root=artifact_root,
        artifact_loader={"skill-with-hidden-binary": record}.__getitem__,
    )
    project = _ready_provider_project(app, registry)
    _project, summary = _publish_artifact_summary(
        app,
        run_control,
        project,
        run_id="run-skill-hidden-binary",
        record=record,
        content_sha256=digest,
        byte_size=byte_size,
    )
    summary_data = summary.model_dump(mode="python")
    summary_data["metadata"] = {"document_count": 2, "root_document": "SKILL.md"}
    summary = m.SkillBundleArtifactSummaryV1.model_validate(summary_data)
    assert summary.run_id is not None
    run_control.artifacts[summary.run_id] = [summary]

    with TestClient(app) as client:
        response = client.get(
            f"/v1/projects/{project.id}/artifacts/{summary.id}/content", headers=AUTH
        )

    assert response.status_code == 422
    assert response.json()["code"] == "artifact_content_invalid"


def test_artifact_diff_uses_reachable_cross_revision_lineage(tmp_path: Path) -> None:
    registry = verified_builtin_registry(tmp_path / "registry")
    artifact_root = tmp_path / "managed-artifacts"
    previous_record, previous_digest, previous_size, _previous_path = _artifact_payload(
        artifact_root,
        artifact_id="memory-previous",
        artifact_type=m.ArtifactType.TEXT_MEMORY,
        content="# Memory\n\nKeep alpha.\nRemove beta.\n",
    )
    current_record, current_digest, current_size, _current_path = _artifact_payload(
        artifact_root,
        artifact_id="memory-current",
        artifact_type=m.ArtifactType.TEXT_MEMORY,
        content="# Memory\n\nKeep alpha.\nAdd gamma.\n",
    )
    records = {
        "memory-previous": previous_record,
        "memory-current": current_record,
    }
    run_control = _ArtifactRunControl()
    app = _app(
        tmp_path / "state",
        registry=registry,
        run_control=run_control,
        evolution_artifact_root=artifact_root,
        artifact_loader=records.__getitem__,
        build_channel="release",
    )
    project = _ready_provider_project(app, registry)
    project, previous = _publish_artifact_summary(
        app,
        run_control,
        project,
        run_id="run-memory-previous",
        record=previous_record,
        content_sha256=previous_digest,
        byte_size=previous_size,
    )
    _project, current = _publish_artifact_summary(
        app,
        run_control,
        project,
        run_id="run-memory-current",
        record=current_record,
        content_sha256=current_digest,
        byte_size=current_size,
        source_artifact_ids=[previous.id],
    )

    forwarded_paths: list[str] = []
    with TestClient(app) as client:
        historical = client.get(f"/v1/projects/{project.id}/artifacts/{previous.id}", headers=AUTH)
        response = client.get(
            f"/v1/projects/{project.id}/artifacts/{current.id}/diff", headers=AUTH
        )

        def forward(request: httpx.Request) -> httpx.Response:
            target = request.url.raw_path.decode("ascii")
            if request.url.query and "?" not in target:
                target = f"{target}?{request.url.query.decode('ascii')}"
            forwarded_paths.append(target.split("?", 1)[0])
            forwarded_headers = dict(request.headers)
            forwarded_headers["authorization"] = AUTH["Authorization"]
            forwarded = client.request(
                request.method,
                target,
                headers=forwarded_headers,
                content=request.content,
            )
            return httpx.Response(
                forwarded.status_code,
                headers=dict(forwarded.headers),
                content=forwarded.content,
                request=request,
            )

        sidecar = CoreControlClientV1(
            CoreTunnelConnectionV1(
                endpoint="http://127.0.0.1:48765",
                bearer_token="0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefg",
                project_id=project.id,
                session_id="cross-layer-artifact-diff",
            ),
            transport=httpx.MockTransport(forward),
        )
        sidecar.version()
        sidecar.get_artifact(current.id, project_id=project.id)
        sidecar_diff = sidecar.artifact_diff(current.id, project_id=project.id)
        sidecar.close()

    assert historical.status_code == 404
    assert historical.json()["code"] == "artifact_not_found"
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["artifact_id"] == current.id
    assert payload["artifact_content_sha256"] == current.content_sha256
    assert payload["previous_artifact_id"] == previous.id
    assert payload["previous_artifact_content_sha256"] == previous.content_sha256
    assert sidecar_diff.previous_artifact_id == previous.id
    historical_detail_path = f"/v1/projects/{project.id}/artifacts/{previous.id}"
    assert historical_detail_path not in forwarded_paths
    assert payload["truncated"] is False
    assert [change["kind"] for change in payload["document_changes"]] == ["modified"]
    lines = payload["document_changes"][0]["hunks"][0]["lines"]
    assert {line["kind"] for line in lines} == {"context", "removed", "added"}
    assert any(line["text"] == "Remove beta." for line in lines)
    assert any(line["text"] == "Add gamma." for line in lines)


def test_artifact_read_without_run_authority_uses_retryable_503_contract(
    tmp_path: Path,
) -> None:
    registry = verified_builtin_registry(tmp_path / "registry")
    artifact_root = tmp_path / "managed-artifacts"
    record, digest, byte_size, _path = _artifact_payload(
        artifact_root,
        artifact_id="memory-without-run-authority",
        artifact_type=m.ArtifactType.TEXT_MEMORY,
        content="durable reachability\n",
    )
    run_control = _ArtifactRunControl()
    state_root = tmp_path / "state"
    app = _app(
        state_root,
        registry=registry,
        run_control=run_control,
        evolution_artifact_root=artifact_root,
        artifact_loader={"memory-without-run-authority": record}.__getitem__,
    )
    project = _ready_provider_project(app, registry)
    project, summary = _publish_artifact_summary(
        app,
        run_control,
        project,
        run_id="run-without-authority",
        record=record,
        content_sha256=digest,
        byte_size=byte_size,
    )
    app.state.core_control_provider.close()

    restarted = _app(
        state_root,
        registry=registry,
        evolution_artifact_root=artifact_root,
        artifact_loader={"memory-without-run-authority": record}.__getitem__,
    )
    with TestClient(restarted) as client:
        response = client.get(
            f"/v1/projects/{project.id}/artifacts/{summary.id}",
            headers=AUTH,
        )

    assert response.status_code == 503
    assert response.json()["code"] == "artifact_authority_invalid"
    assert response.json()["retryable"] is True


def test_artifact_diff_rejects_repeated_20k_lines_before_matcher(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry = verified_builtin_registry(tmp_path / "registry")
    artifact_root = tmp_path / "managed-artifacts"
    previous_record, previous_digest, previous_size, _ = _artifact_payload(
        artifact_root,
        artifact_id="repeated-previous",
        artifact_type=m.ArtifactType.TEXT_MEMORY,
        content="same\n" * 20_000,
    )
    current_record, current_digest, current_size, _ = _artifact_payload(
        artifact_root,
        artifact_id="repeated-current",
        artifact_type=m.ArtifactType.TEXT_MEMORY,
        content=("same\n" * 19_999) + "changed\n",
    )
    records = {
        "repeated-previous": previous_record,
        "repeated-current": current_record,
    }
    run_control = _ArtifactRunControl()
    app = _app(
        tmp_path / "state",
        registry=registry,
        run_control=run_control,
        evolution_artifact_root=artifact_root,
        artifact_loader=records.__getitem__,
    )
    project = _ready_provider_project(app, registry)
    project, previous = _publish_artifact_summary(
        app,
        run_control,
        project,
        run_id="run-repeated-previous",
        record=previous_record,
        content_sha256=previous_digest,
        byte_size=previous_size,
    )
    _project, current = _publish_artifact_summary(
        app,
        run_control,
        project,
        run_id="run-repeated-current",
        record=current_record,
        content_sha256=current_digest,
        byte_size=current_size,
        source_artifact_ids=[previous.id],
    )

    def unexpected_matcher(*_args, **_kwargs):
        raise AssertionError("unbounded matcher was invoked")

    monkeypatch.setattr(provider_module.difflib, "SequenceMatcher", unexpected_matcher)
    started = time.monotonic()
    with TestClient(app) as client:
        response = client.get(
            f"/v1/projects/{project.id}/artifacts/{current.id}/diff", headers=AUTH
        )
    elapsed = time.monotonic() - started

    assert response.status_code == 422
    assert response.json()["code"] == "artifact_diff_oversize"
    assert elapsed < 2


def test_current_revision_can_inherit_artifact_from_durable_lineage(tmp_path: Path) -> None:
    registry = verified_builtin_registry(tmp_path / "registry")
    artifact_root = tmp_path / "managed-artifacts"
    record, digest, byte_size, _primary = _artifact_payload(
        artifact_root,
        artifact_id="inherited-memory",
        artifact_type=m.ArtifactType.TEXT_MEMORY,
        content="durable inherited memory\n",
    )
    run_control = _ArtifactRunControl()
    app = _app(
        tmp_path / "state",
        registry=registry,
        run_control=run_control,
        evolution_artifact_root=artifact_root,
        artifact_loader={"inherited-memory": record}.__getitem__,
    )
    project = _ready_provider_project(app, registry)
    project, summary = _publish_artifact_summary(
        app,
        run_control,
        project,
        run_id="run-inherited-producer",
        record=record,
        content_sha256=digest,
        byte_size=byte_size,
    )
    assert project.active_revision is not None
    app.state.core_control_provider.store.activate_evolution_revision(
        project.id,
        predecessor=project.active_revision,
        run_id="run-inherited-consumer",
        context_artifact_ids={"text_memory": [summary.id]},
    )
    run_control.artifacts["run-inherited-consumer"] = []

    app.state.core_control_provider.close()
    restarted = _app(
        tmp_path / "state",
        registry=registry,
        run_control=run_control,
        evolution_artifact_root=artifact_root,
        artifact_loader={"inherited-memory": record}.__getitem__,
    )
    with TestClient(restarted) as client:
        response = client.get(f"/v1/projects/{project.id}/artifacts/{summary.id}", headers=AUTH)

    assert response.status_code == 200, response.text
    assert response.json()["run_id"] == "run-inherited-producer"


def test_artifact_authority_survives_idempotency_retention_cleanup(tmp_path: Path) -> None:
    registry = verified_builtin_registry(tmp_path / "registry")
    artifact_root = tmp_path / "managed-artifacts"
    record, digest, byte_size, _primary = _artifact_payload(
        artifact_root,
        artifact_id="retained-memory",
        artifact_type=m.ArtifactType.TEXT_MEMORY,
        content="retained beyond replay records\n",
    )
    run_control = _ArtifactRunControl()
    app = _app(
        tmp_path / "state",
        registry=registry,
        run_control=run_control,
        evolution_artifact_root=artifact_root,
        artifact_loader={"retained-memory": record}.__getitem__,
    )
    project = _ready_provider_project(app, registry)
    project, summary = _publish_artifact_summary(
        app,
        run_control,
        project,
        run_id="run-retained-memory",
        record=record,
        content_sha256=digest,
        byte_size=byte_size,
    )
    store = app.state.core_control_provider.store
    with store._mutex, store._transaction():
        store._connection.execute(
            "UPDATE idempotency_records SET expires_at_epoch = 0 "
            "WHERE operation_id = ? AND idempotency_key = ?",
            ("activateCoreEvolutionRevisionInternalV1", "run-retained-memory"),
        )
        store._prune_expired_idempotency_records(1)
        binding_count = store._connection.execute(
            "SELECT COUNT(*) FROM revision_activation_bindings WHERE revision_id = ?",
            (project.active_revision.id,),
        ).fetchone()[0]
        authority_count = store._connection.execute(
            "SELECT COUNT(*) FROM revision_artifact_authorities WHERE revision_id = ?",
            (project.active_revision.id,),
        ).fetchone()[0]

    app.state.core_control_provider.close()
    restarted = _app(
        tmp_path / "state",
        registry=registry,
        run_control=run_control,
        evolution_artifact_root=artifact_root,
        artifact_loader={"retained-memory": record}.__getitem__,
    )
    with TestClient(restarted) as client:
        response = client.get(f"/v1/projects/{project.id}/artifacts/{summary.id}", headers=AUTH)

    assert binding_count == 0
    assert authority_count == 1
    assert response.status_code == 200, response.text


def test_artifact_diff_rejects_cross_project_predecessor(tmp_path: Path) -> None:
    registry = verified_builtin_registry(tmp_path / "registry")
    artifact_root = tmp_path / "managed-artifacts"
    records: dict[str, Mapping[str, object]] = {}
    run_control = _ArtifactRunControl()
    app = _app(
        tmp_path / "state",
        registry=registry,
        run_control=run_control,
        evolution_artifact_root=artifact_root,
        artifact_loader=records.__getitem__,
    )
    first_project = _ready_provider_project(app, registry, key="artifact-project-first")
    second_project = _ready_provider_project(app, registry, key="artifact-project-second")
    first_record, first_digest, first_size, _ = _artifact_payload(
        artifact_root,
        artifact_id="first-project-memory",
        artifact_type=m.ArtifactType.TEXT_MEMORY,
        content="first project\n",
    )
    second_record, second_digest, second_size, _ = _artifact_payload(
        artifact_root,
        artifact_id="second-project-memory",
        artifact_type=m.ArtifactType.TEXT_MEMORY,
        content="second project\n",
    )
    records.update(
        {
            "first-project-memory": first_record,
            "second-project-memory": second_record,
        }
    )
    _first_project, first = _publish_artifact_summary(
        app,
        run_control,
        first_project,
        run_id="run-first-project",
        record=first_record,
        content_sha256=first_digest,
        byte_size=first_size,
    )
    _second_project, second = _publish_artifact_summary(
        app,
        run_control,
        second_project,
        run_id="run-second-project",
        record=second_record,
        content_sha256=second_digest,
        byte_size=second_size,
        source_artifact_ids=[first.id],
    )

    with TestClient(app) as client:
        response = client.get(
            f"/v1/projects/{second_project.id}/artifacts/{second.id}/diff",
            headers=AUTH,
            params={"previous_artifact_id": first.id},
        )

    assert response.status_code == 404
    assert response.json()["code"] == "artifact_not_found"


@pytest.mark.parametrize("mismatch", ["target", "type", "lineage"])
def test_artifact_diff_rejects_cross_target_type_and_unlisted_lineage(
    tmp_path: Path,
    mismatch: str,
) -> None:
    registry = verified_builtin_registry(tmp_path / "registry")
    artifact_root = tmp_path / "managed-artifacts"
    previous_type = (
        m.ArtifactType.SKILL_BUNDLE if mismatch == "type" else m.ArtifactType.TEXT_MEMORY
    )
    previous_record, previous_digest, previous_size, _ = _artifact_payload(
        artifact_root,
        artifact_id=f"{mismatch}-previous",
        artifact_type=previous_type,
        content="previous document\n",
    )
    current_record, current_digest, current_size, _ = _artifact_payload(
        artifact_root,
        artifact_id=f"{mismatch}-current",
        artifact_type=m.ArtifactType.TEXT_MEMORY,
        content="current document\n",
    )
    records = {
        str(previous_record["artifact_id"]): previous_record,
        str(current_record["artifact_id"]): current_record,
    }
    run_control = _ArtifactRunControl()
    app = _app(
        tmp_path / "state",
        registry=registry,
        run_control=run_control,
        evolution_artifact_root=artifact_root,
        artifact_loader=records.__getitem__,
    )
    project = _ready_provider_project(app, registry)
    project, previous = _publish_artifact_summary(
        app,
        run_control,
        project,
        run_id=f"run-{mismatch}-previous",
        record=previous_record,
        content_sha256=previous_digest,
        byte_size=previous_size,
    )
    _project, current = _publish_artifact_summary(
        app,
        run_control,
        project,
        run_id=f"run-{mismatch}-current",
        record=current_record,
        content_sha256=current_digest,
        byte_size=current_size,
        source_artifact_ids=[] if mismatch == "lineage" else [previous.id],
    )
    if mismatch == "target":
        previous_data = previous.model_dump(mode="python")
        previous_data["target_id"] = "other-target"
        previous = m.TextMemoryArtifactSummaryV1.model_validate(previous_data)
        assert previous.run_id is not None
        run_control.artifacts[previous.run_id] = [previous]

    with TestClient(app) as client:
        response = client.get(
            f"/v1/projects/{project.id}/artifacts/{current.id}/diff",
            headers=AUTH,
            params={"previous_artifact_id": previous.id},
        )

    assert response.status_code == 404
    assert response.json()["code"] == "artifact_not_found"


@pytest.mark.parametrize(
    ("operation_id", "arguments"),
    [
        (
            "createCoreRunV1",
            {"request": {"project_id": "project-1"}, "idempotency_key": "same-key"},
        ),
        (
            "cancelCoreRunV1",
            {
                "run_id": "run-1",
                "request": {"reason": "user_requested"},
                "if_match": '"' + "a" * 64 + '"',
                "idempotency_key": "same-key",
            },
        ),
        (
            "retryCoreRunV1",
            {
                "run_id": "run-1",
                "request": {"terminal_attempt_id": "attempt-1"},
                "if_match": '"' + "a" * 64 + '"',
                "idempotency_key": "same-key",
            },
        ),
        (
            "deleteCoreRunV1",
            {
                "run_id": "run-1",
                "if_match": '"' + "a" * 64 + '"',
                "idempotency_key": "same-key",
            },
        ),
    ],
)
def test_retryable_run_mutation_failure_is_not_persisted_for_idempotency_replay(
    tmp_path: Path,
    operation_id: str,
    arguments: Mapping[str, object],
) -> None:
    run_control = _MutationFailingRunControl(retryable=True, fail_once=True)
    app = _app(tmp_path, run_control=run_control)
    provider = app.state.core_control_provider

    with TestClient(app):
        with pytest.raises(provider_module.CoreControlHTTPError) as raised:
            provider.invoke(operation_id, arguments)
        assert raised.value.status_code == 503
        assert raised.value.error.retryable is True
        assert provider.invoke(operation_id, arguments) == "owner-retried"

    assert [invocation[0] for invocation in run_control.invocations] == [
        operation_id,
        operation_id,
    ]


def test_non_retryable_run_mutation_failure_keeps_failure_idempotency_replay(
    tmp_path: Path,
) -> None:
    run_control = _MutationFailingRunControl(retryable=False)
    app = _app(tmp_path, run_control=run_control)
    provider = app.state.core_control_provider
    arguments = {
        "run_id": "run-1",
        "request": {"reason": "user_requested"},
        "if_match": '"' + "a" * 64 + '"',
        "idempotency_key": "same-key",
    }

    with TestClient(app):
        for _ in range(2):
            with pytest.raises(provider_module.CoreControlHTTPError) as raised:
                provider.invoke("cancelCoreRunV1", arguments)
            assert raised.value.status_code == 409
            assert raised.value.error.retryable is False

    assert [invocation[0] for invocation in run_control.invocations] == ["cancelCoreRunV1"]


@pytest.mark.parametrize(
    ("operation_id", "arguments"),
    [
        (
            "createCoreRunV1",
            {
                "request": {"project_id": "project-1"},
                "idempotency_key": "legacy-retryable-create-key",
            },
        ),
        (
            "cancelCoreRunV1",
            {
                "run_id": "run-1",
                "request": {"reason": "user_requested"},
                "if_match": '"' + "a" * 64 + '"',
                "idempotency_key": "legacy-retryable-cancel-key",
            },
        ),
        (
            "retryCoreRunV1",
            {
                "run_id": "run-1",
                "request": {"terminal_attempt_id": "attempt-1"},
                "if_match": '"' + "a" * 64 + '"',
                "idempotency_key": "legacy-retryable-retry-key",
            },
        ),
        (
            "deleteCoreRunV1",
            {
                "run_id": "run-1",
                "if_match": '"' + "a" * 64 + '"',
                "idempotency_key": "legacy-retryable-delete-key",
            },
        ),
    ],
)
def test_legacy_retryable_run_failure_is_cleared_before_owner_retry(
    tmp_path: Path,
    operation_id: str,
    arguments: Mapping[str, object],
) -> None:
    _persist_legacy_failure(
        tmp_path,
        operation_id,
        arguments,
        retryable=True,
    )
    run_control = _SuccessfulMutationRunControl()
    app = _app(tmp_path, run_control=run_control)
    provider = app.state.core_control_provider

    with TestClient(app):
        assert provider.invoke(operation_id, arguments) == "owner-retried"
        assert provider.store.replay_failed_idempotency(operation_id, arguments) is None

    assert [invocation[0] for invocation in run_control.invocations] == [operation_id]


def test_legacy_retryable_run_failure_conflict_does_not_delete_original_row(
    tmp_path: Path,
) -> None:
    original_arguments = {
        "run_id": "run-1",
        "request": {"reason": "user_requested"},
        "if_match": '"' + "a" * 64 + '"',
        "idempotency_key": "legacy-retryable-conflict-key",
    }
    persisted = _persist_legacy_failure(
        tmp_path,
        "cancelCoreRunV1",
        original_arguments,
        retryable=True,
    )
    conflicting_arguments = {
        **original_arguments,
        "request": {"reason": "superseded"},
    }
    run_control = _SuccessfulMutationRunControl()
    app = _app(tmp_path, run_control=run_control)
    provider = app.state.core_control_provider

    with TestClient(app):
        with pytest.raises(provider_module.CoreControlHTTPError) as raised:
            provider.invoke("cancelCoreRunV1", conflicting_arguments)
        assert raised.value.status_code == 409
        assert raised.value.error.code == "idempotency_key_reused"
        assert (
            provider.store.replay_failed_idempotency("cancelCoreRunV1", original_arguments)
            == persisted
        )

    assert run_control.invocations == []


def test_legacy_non_retryable_run_failure_keeps_exact_replay_after_restart(
    tmp_path: Path,
) -> None:
    arguments = {
        "run_id": "run-1",
        "request": {"reason": "user_requested"},
        "if_match": '"' + "a" * 64 + '"',
        "idempotency_key": "legacy-non-retryable-run-key",
    }
    persisted = _persist_legacy_failure(
        tmp_path,
        "cancelCoreRunV1",
        arguments,
        retryable=False,
    )
    run_control = _SuccessfulMutationRunControl()
    app = _app(tmp_path, run_control=run_control)
    provider = app.state.core_control_provider

    with TestClient(app):
        for _ in range(2):
            with pytest.raises(provider_module.CoreControlHTTPError) as raised:
                provider.invoke("cancelCoreRunV1", arguments)
            assert raised.value.error == persisted

    assert run_control.invocations == []


def test_legacy_retryable_non_run_failure_keeps_existing_replay_policy(
    tmp_path: Path,
) -> None:
    arguments = {
        "project_id": "project-1",
        "if_match": '"' + "a" * 64 + '"',
        "idempotency_key": "legacy-retryable-project-key",
    }
    persisted = _persist_legacy_failure(
        tmp_path,
        "deleteCoreProjectV1",
        arguments,
        retryable=True,
    )
    app = _app(tmp_path)
    provider = app.state.core_control_provider

    with TestClient(app):
        with pytest.raises(provider_module.CoreControlHTTPError) as raised:
            provider.invoke("deleteCoreProjectV1", arguments)
        assert raised.value.error == persisted
        assert (
            provider.store.replay_failed_idempotency("deleteCoreProjectV1", arguments) == persisted
        )


def test_legacy_retryable_run_cleanup_survives_post_commit_failure_without_cross_deletion(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_control = _SuccessfulMutationRunControl()
    app = _app(tmp_path, run_control=run_control)
    provider = app.state.core_control_provider
    retryable_arguments = {
        "run_id": "run-retryable",
        "request": {"reason": "user_requested"},
        "if_match": '"' + "a" * 64 + '"',
        "idempotency_key": "legacy-retryable-crash-key",
    }
    non_retryable_arguments = {
        "run_id": "run-non-retryable",
        "request": {"reason": "user_requested"},
        "if_match": '"' + "b" * 64 + '"',
        "idempotency_key": "legacy-non-retryable-peer-key",
    }

    with TestClient(app) as client:
        project_payload = _project_create()
        project, _ = _create_project(
            client,
            project_payload,
            idempotency_key="successful-project-peer-key",
        )
        provider.store.record_failed_idempotency(
            "cancelCoreRunV1",
            retryable_arguments,
            _run_failure_error(retryable=True),
        )
        non_retryable_error = _run_failure_error(retryable=False)
        provider.store.record_failed_idempotency(
            "cancelCoreRunV1",
            non_retryable_arguments,
            non_retryable_error,
        )

        original_post_commit_verify = store_module._Transaction._verify_after_commit
        fail_once = True

        def fail_after_cleanup_commit(transaction) -> None:
            nonlocal fail_once
            original_post_commit_verify(transaction)
            if fail_once:
                fail_once = False
                raise OSError("injected failure after legacy retryable cleanup commit")

        monkeypatch.setattr(
            store_module._Transaction,
            "_verify_after_commit",
            fail_after_cleanup_commit,
        )
        with pytest.raises(store_module.PostCommitStoreError):
            provider.invoke("cancelCoreRunV1", retryable_arguments)
        monkeypatch.setattr(
            store_module._Transaction,
            "_verify_after_commit",
            original_post_commit_verify,
        )

        replayed_project, _ = _create_project(
            client,
            project_payload,
            idempotency_key="successful-project-peer-key",
        )
        assert replayed_project == project
        with pytest.raises(provider_module.CoreControlHTTPError) as raised:
            provider.invoke("cancelCoreRunV1", non_retryable_arguments)
        assert raised.value.error == non_retryable_error
        assert provider.invoke("cancelCoreRunV1", retryable_arguments) == "owner-retried"

    assert [invocation[0] for invocation in run_control.invocations] == ["cancelCoreRunV1"]


def test_concurrent_exact_retries_are_coalesced_after_legacy_retryable_cleanup(
    tmp_path: Path,
) -> None:
    arguments = {
        "run_id": "run-1",
        "request": {"reason": "user_requested"},
        "if_match": '"' + "a" * 64 + '"',
        "idempotency_key": "legacy-retryable-concurrent-key",
    }
    _persist_legacy_failure(
        tmp_path,
        "cancelCoreRunV1",
        arguments,
        retryable=True,
    )
    run_control = _BlockingSuccessfulRunControl()
    app = _app(tmp_path, run_control=run_control)
    provider = app.state.core_control_provider

    with TestClient(app), ThreadPoolExecutor(max_workers=2) as executor:
        first = executor.submit(provider.invoke, "cancelCoreRunV1", arguments)
        assert run_control.entered.wait(timeout=5)
        second = executor.submit(provider.invoke, "cancelCoreRunV1", arguments)
        time.sleep(0.05)
        run_control.release.set()
        futures = [first, second]
        assert [future.result(timeout=10) for future in futures] == [
            "owner-retried",
            "owner-retried",
        ]

    assert [invocation[0] for invocation in run_control.invocations] == ["cancelCoreRunV1"]


def test_concurrent_retryable_run_failure_is_coalesced_but_not_cached(
    tmp_path: Path,
) -> None:
    arguments = {
        "run_id": "run-1",
        "request": {"reason": "user_requested"},
        "if_match": '"' + "a" * 64 + '"',
        "idempotency_key": "retryable-concurrent-key",
    }
    run_control = _BlockingRetryableThenSuccessfulRunControl()
    app = _app(tmp_path, run_control=run_control)
    provider = app.state.core_control_provider

    with TestClient(app), ThreadPoolExecutor(max_workers=2) as executor:
        first = executor.submit(provider.invoke, "cancelCoreRunV1", arguments)
        assert run_control.entered.wait(timeout=5)
        second = executor.submit(provider.invoke, "cancelCoreRunV1", arguments)
        time.sleep(0.05)
        run_control.release.set()
        errors = []
        for future in (first, second):
            with pytest.raises(provider_module.CoreControlHTTPError) as raised:
                future.result(timeout=10)
            errors.append(raised.value.error)
        assert errors[0] == errors[1]
        assert provider.invoke("cancelCoreRunV1", arguments) == "owner-retried"

    assert [invocation[0] for invocation in run_control.invocations] == [
        "cancelCoreRunV1",
        "cancelCoreRunV1",
    ]


def test_concurrent_non_retryable_run_failure_is_coalesced_and_durable(
    tmp_path: Path,
) -> None:
    arguments = {
        "run_id": "run-1",
        "request": {"reason": "user_requested"},
        "if_match": '"' + "a" * 64 + '"',
        "idempotency_key": "non-retryable-concurrent-key",
    }
    run_control = _BlockingNonRetryableRunControl()
    app = _app(tmp_path, run_control=run_control)
    provider = app.state.core_control_provider

    with TestClient(app), ThreadPoolExecutor(max_workers=2) as executor:
        first = executor.submit(provider.invoke, "cancelCoreRunV1", arguments)
        assert run_control.entered.wait(timeout=5)
        second = executor.submit(provider.invoke, "cancelCoreRunV1", arguments)
        time.sleep(0.05)
        run_control.release.set()
        errors = []
        for future in (first, second):
            with pytest.raises(provider_module.CoreControlHTTPError) as raised:
                future.result(timeout=10)
            errors.append(raised.value.error)
        assert errors[0] == errors[1]
        with pytest.raises(provider_module.CoreControlHTTPError) as replayed:
            provider.invoke("cancelCoreRunV1", arguments)
        assert replayed.value.error == errors[0]

    assert [invocation[0] for invocation in run_control.invocations] == ["cancelCoreRunV1"]


def test_concurrent_run_mutation_same_key_with_different_payload_conflicts(
    tmp_path: Path,
) -> None:
    arguments = {
        "run_id": "run-1",
        "request": {"reason": "user_requested"},
        "if_match": '"' + "a" * 64 + '"',
        "idempotency_key": "concurrent-conflict-key",
    }
    conflicting_arguments = {**arguments, "request": {"reason": "superseded"}}
    run_control = _BlockingSuccessfulRunControl()
    app = _app(tmp_path, run_control=run_control)
    provider = app.state.core_control_provider

    with TestClient(app), ThreadPoolExecutor(max_workers=1) as executor:
        first = executor.submit(provider.invoke, "cancelCoreRunV1", arguments)
        assert run_control.entered.wait(timeout=5)
        with pytest.raises(provider_module.CoreControlHTTPError) as raised:
            provider.invoke("cancelCoreRunV1", conflicting_arguments)
        assert raised.value.status_code == 409
        assert raised.value.error.code == "idempotency_key_reused"
        run_control.release.set()
        assert first.result(timeout=10) == "owner-retried"

    assert [invocation[0] for invocation in run_control.invocations] == ["cancelCoreRunV1"]


def test_run_mutation_success_replay_is_bounded_and_lru_evicted(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(provider_module, "_RUN_MUTATION_SINGLEFLIGHT_CAPACITY", 2)
    run_control = _SuccessfulMutationRunControl()
    app = _app(tmp_path, run_control=run_control)
    provider = app.state.core_control_provider

    def arguments(key: str) -> dict[str, object]:
        return {
            "run_id": f"run-{key}",
            "if_match": '"' + "a" * 64 + '"',
            "idempotency_key": key,
        }

    with TestClient(app):
        assert provider.invoke("deleteCoreRunV1", arguments("a")) == "owner-retried"
        assert provider.invoke("deleteCoreRunV1", arguments("b")) == "owner-retried"
        assert provider.invoke("deleteCoreRunV1", arguments("a")) == "owner-retried"
        assert provider.invoke("deleteCoreRunV1", arguments("c")) == "owner-retried"
        assert provider.invoke("deleteCoreRunV1", arguments("a")) == "owner-retried"
        assert provider.invoke("deleteCoreRunV1", arguments("b")) == "owner-retried"

    assert [invocation[1]["run_id"] for invocation in run_control.invocations] == [
        "run-a",
        "run-b",
        "run-c",
        "run-b",
    ]


def test_run_mutation_singleflight_capacity_fails_closed_when_all_entries_are_active(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(provider_module, "_RUN_MUTATION_SINGLEFLIGHT_CAPACITY", 1)
    run_control = _BlockingSuccessfulRunControl()
    app = _app(tmp_path, run_control=run_control)
    provider = app.state.core_control_provider
    first_arguments = {
        "run_id": "run-1",
        "if_match": '"' + "a" * 64 + '"',
        "idempotency_key": "capacity-a",
    }
    second_arguments = {
        "run_id": "run-2",
        "if_match": '"' + "b" * 64 + '"',
        "idempotency_key": "capacity-b",
    }

    with TestClient(app), ThreadPoolExecutor(max_workers=1) as executor:
        first = executor.submit(provider.invoke, "deleteCoreRunV1", first_arguments)
        assert run_control.entered.wait(timeout=5)
        with pytest.raises(provider_module.CoreControlHTTPError) as raised:
            provider.invoke("deleteCoreRunV1", second_arguments)
        assert raised.value.status_code == 503
        assert raised.value.error.code == "run_mutation_capacity_exhausted"
        assert raised.value.error.retryable is True
        run_control.release.set()
        assert first.result(timeout=10) == "owner-retried"


def test_run_mutation_singleflight_does_not_evict_a_resolving_waiter() -> None:
    singleflight = provider_module._RunMutationSingleFlight(1)
    retained_identity = ("deleteCoreRunV1", "scope-a", "key-a")
    retained = provider_module._RunMutationFlight(retained_identity, "a" * 64)
    retained.future.set_result("owner-result")
    retained.owner_active = False
    retained.waiters = 1
    singleflight._entries[retained_identity] = retained

    with pytest.raises(provider_module.CoreControlHTTPError) as raised:
        singleflight.invoke(
            ("deleteCoreRunV1", "scope-b", "key-b"),
            "b" * 64,
            lambda: "second-owner-result",
        )
    assert raised.value.error.code == "run_mutation_capacity_exhausted"

    retained.waiters = 0
    assert (
        singleflight.invoke(
            ("deleteCoreRunV1", "scope-b", "key-b"),
            "b" * 64,
            lambda: "second-owner-result",
        )
        == "second-owner-result"
    )


def test_retired_failed_run_mutation_freezes_digest_without_replaying_error() -> None:
    singleflight = provider_module._RunMutationSingleFlight(2)
    identity = ("cancelCoreRunV1", "scope-a", "key-a")
    retired = provider_module._RunMutationFlight(identity, "a" * 64)
    retired.future.set_exception(
        provider_module._run_control_http_error(
            CoreRunControlError(
                "run_owner_temporarily_unavailable",
                "The managed run owner is unavailable.",
                http_status=503,
                retryable=True,
            )
        )
    )
    retired.owner_active = False
    retired.waiters = 1
    singleflight._retired.add(retired)

    with pytest.raises(provider_module.CoreControlHTTPError) as conflict:
        singleflight.invoke(identity, "b" * 64, lambda: "unexpected-owner-call")
    assert conflict.value.error.code == "idempotency_key_reused"

    owner_calls = 0

    def retry_owner() -> str:
        nonlocal owner_calls
        owner_calls += 1
        return "owner-retried"

    assert singleflight.invoke(identity, "a" * 64, retry_owner) == "owner-retried"
    assert owner_calls == 1


def test_run_mutation_shutdown_drains_admitted_leader_and_waiter_before_owner_close(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    arguments = {
        "run_id": "run-1",
        "if_match": '"' + "a" * 64 + '"',
        "idempotency_key": "shutdown-key",
    }
    run_control = _SuccessfulMutationRunControl()
    app = _app(tmp_path, run_control=run_control)
    provider = app.state.core_control_provider
    leader_joined = threading.Event()
    allow_callback = threading.Event()
    original_invoke = provider._run_mutations.invoke

    def invoke_through_barrier(identity, request_digest, call):
        def paused_call():
            leader_joined.set()
            assert allow_callback.wait(timeout=5)
            assert run_control.close_calls == 0
            return call()

        return original_invoke(identity, request_digest, paused_call)

    monkeypatch.setattr(provider._run_mutations, "invoke", invoke_through_barrier)

    with ThreadPoolExecutor(max_workers=3) as executor:
        first = executor.submit(provider.invoke, "deleteCoreRunV1", arguments)
        assert leader_joined.wait(timeout=5)
        second = executor.submit(provider.invoke, "deleteCoreRunV1", arguments)

        deadline = time.monotonic() + 5
        while True:
            with provider._run_mutations._lock:
                entries = tuple(provider._run_mutations._entries.values())
                waiter_count = sum(entry.waiters for entry in entries)
            if waiter_count == 1:
                break
            if time.monotonic() >= deadline:
                raise AssertionError("single-flight waiter did not join")
            time.sleep(0.005)

        closed = executor.submit(provider.close)
        deadline = time.monotonic() + 5
        while True:
            with provider._run_mutations._lock:
                closing = provider._run_mutations._closing
            if closing:
                break
            if time.monotonic() >= deadline:
                raise AssertionError("provider close did not stop admission")
            time.sleep(0.005)
        assert run_control.close_calls == 0
        assert not closed.done()
        allow_callback.set()
        assert first.result(timeout=10) == "owner-retried"
        assert second.result(timeout=10) == "owner-retried"
        closed.result(timeout=10)

    provider.close()
    assert run_control.close_calls == 1
    assert [invocation[0] for invocation in run_control.invocations] == ["deleteCoreRunV1"]

    restarted_control = _SuccessfulMutationRunControl()
    restarted = _app(tmp_path, run_control=restarted_control)
    with TestClient(restarted):
        assert (
            restarted.state.core_control_provider.invoke("deleteCoreRunV1", arguments)
            == "owner-retried"
        )
    assert [invocation[0] for invocation in restarted_control.invocations] == ["deleteCoreRunV1"]


def test_run_mutation_shutdown_drain_timeout_retains_owner_for_idempotent_retry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(provider_module, "_RUN_MUTATION_SHUTDOWN_DRAIN_TIMEOUT_SECONDS", 0.05)
    arguments = {
        "run_id": "run-1",
        "if_match": '"' + "a" * 64 + '"',
        "idempotency_key": "shutdown-timeout-key",
    }
    run_control = _SuccessfulMutationRunControl()
    app = _app(tmp_path, run_control=run_control)
    provider = app.state.core_control_provider
    leader_joined = threading.Event()
    allow_callback = threading.Event()
    original_invoke = provider._run_mutations.invoke

    def invoke_through_barrier(identity, request_digest, call):
        def paused_call():
            leader_joined.set()
            assert allow_callback.wait(timeout=5)
            return call()

        return original_invoke(identity, request_digest, paused_call)

    monkeypatch.setattr(provider._run_mutations, "invoke", invoke_through_barrier)

    with ThreadPoolExecutor(max_workers=1) as executor:
        mutation = executor.submit(provider.invoke, "deleteCoreRunV1", arguments)
        assert leader_joined.wait(timeout=5)
        started_at = time.monotonic()
        with pytest.raises(RuntimeError, match="run mutations did not drain"):
            provider.close()
        assert time.monotonic() - started_at < 1
        assert run_control.close_calls == 0
        assert provider._run_control is run_control
        allow_callback.set()
        assert mutation.result(timeout=10) == "owner-retried"

    provider.close()
    provider.close()
    assert run_control.close_calls == 1


def test_failed_run_mutation_shutdown_waits_for_admitted_waiter_exit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    arguments = {
        "run_id": "run-1",
        "request": {"reason": "user_requested"},
        "if_match": '"' + "a" * 64 + '"',
        "idempotency_key": "failed-shutdown-waiter-key",
    }
    run_control = _BlockingRetryableThenSuccessfulRunControl()
    app = _app(tmp_path, run_control=run_control)
    provider = app.state.core_control_provider
    waiter_exiting = threading.Event()
    allow_waiter_exit = threading.Event()
    original_release_waiter = provider._run_mutations._release_waiter

    def release_waiter_through_barrier(entry) -> None:
        waiter_exiting.set()
        assert allow_waiter_exit.wait(timeout=5)
        original_release_waiter(entry)

    monkeypatch.setattr(provider._run_mutations, "_release_waiter", release_waiter_through_barrier)

    with ThreadPoolExecutor(max_workers=3) as executor:
        leader = executor.submit(provider.invoke, "cancelCoreRunV1", arguments)
        assert run_control.entered.wait(timeout=5)
        waiter = executor.submit(provider.invoke, "cancelCoreRunV1", arguments)

        deadline = time.monotonic() + 5
        while True:
            with provider._run_mutations._lock:
                entries = tuple(provider._run_mutations._entries.values())
                waiter_count = sum(entry.waiters for entry in entries)
            if waiter_count == 1:
                break
            if time.monotonic() >= deadline:
                raise AssertionError("failed-flight waiter did not join")
            time.sleep(0.005)

        run_control.release.set()
        with pytest.raises(provider_module.CoreControlHTTPError) as leader_raised:
            leader.result(timeout=10)
        assert waiter_exiting.wait(timeout=5)

        closed = executor.submit(provider.close)
        deadline = time.monotonic() + 5
        while True:
            with provider._run_mutations._lock:
                closing = provider._run_mutations._closing
            if closing:
                break
            if time.monotonic() >= deadline:
                raise AssertionError("provider close did not stop admission")
            time.sleep(0.005)
        assert run_control.close_calls == 0
        assert not closed.done()

        allow_waiter_exit.set()
        with pytest.raises(provider_module.CoreControlHTTPError) as waiter_raised:
            waiter.result(timeout=10)
        closed.result(timeout=10)
        assert waiter_raised.value.error == leader_raised.value.error

    provider.close()
    assert run_control.close_calls == 1

    restarted_control = _SuccessfulMutationRunControl()
    restarted = _app(tmp_path, run_control=restarted_control)
    with TestClient(restarted):
        assert (
            restarted.state.core_control_provider.invoke("cancelCoreRunV1", arguments)
            == "owner-retried"
        )
    assert [invocation[0] for invocation in restarted_control.invocations] == ["cancelCoreRunV1"]


def test_failed_run_mutation_waiter_drain_timeout_preserves_idempotent_close(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(provider_module, "_RUN_MUTATION_SHUTDOWN_DRAIN_TIMEOUT_SECONDS", 0.05)
    arguments = {
        "run_id": "run-1",
        "request": {"reason": "user_requested"},
        "if_match": '"' + "a" * 64 + '"',
        "idempotency_key": "failed-shutdown-timeout-key",
    }
    run_control = _BlockingRetryableThenSuccessfulRunControl()
    app = _app(tmp_path, run_control=run_control)
    provider = app.state.core_control_provider
    waiter_exiting = threading.Event()
    allow_waiter_exit = threading.Event()
    original_release_waiter = provider._run_mutations._release_waiter

    def release_waiter_through_barrier(entry) -> None:
        waiter_exiting.set()
        assert allow_waiter_exit.wait(timeout=5)
        original_release_waiter(entry)

    monkeypatch.setattr(provider._run_mutations, "_release_waiter", release_waiter_through_barrier)

    with ThreadPoolExecutor(max_workers=2) as executor:
        leader = executor.submit(provider.invoke, "cancelCoreRunV1", arguments)
        assert run_control.entered.wait(timeout=5)
        waiter = executor.submit(provider.invoke, "cancelCoreRunV1", arguments)

        deadline = time.monotonic() + 5
        while True:
            with provider._run_mutations._lock:
                entries = tuple(provider._run_mutations._entries.values())
                waiter_count = sum(entry.waiters for entry in entries)
            if waiter_count == 1:
                break
            if time.monotonic() >= deadline:
                raise AssertionError("failed-flight waiter did not join")
            time.sleep(0.005)

        run_control.release.set()
        with pytest.raises(provider_module.CoreControlHTTPError):
            leader.result(timeout=10)
        assert waiter_exiting.wait(timeout=5)

        started_at = time.monotonic()
        with pytest.raises(RuntimeError, match="run mutations did not drain"):
            provider.close()
        assert time.monotonic() - started_at < 1
        assert run_control.close_calls == 0
        assert provider._run_control is run_control

        allow_waiter_exit.set()
        with pytest.raises(provider_module.CoreControlHTTPError):
            waiter.result(timeout=10)

    provider.close()
    provider.close()
    assert run_control.close_calls == 1


def test_legacy_retryable_cleanup_does_not_delete_concurrent_non_retryable_replacement(
    tmp_path: Path,
) -> None:
    arguments = {
        "run_id": "run-1",
        "request": {"reason": "user_requested"},
        "if_match": '"' + "a" * 64 + '"',
        "idempotency_key": "legacy-retryable-replacement-key",
    }
    _persist_legacy_failure(
        tmp_path,
        "cancelCoreRunV1",
        arguments,
        retryable=True,
    )
    run_control = _BlockingSuccessfulRunControl()
    app = _app(tmp_path, run_control=run_control)
    provider = app.state.core_control_provider

    with TestClient(app), ThreadPoolExecutor(max_workers=1) as executor:
        retried = executor.submit(provider.invoke, "cancelCoreRunV1", arguments)
        assert run_control.entered.wait(timeout=5)
        replacement = _run_failure_error(retryable=False)
        try:
            provider.store.record_failed_idempotency(
                "cancelCoreRunV1",
                arguments,
                replacement,
            )
        finally:
            run_control.release.set()
        assert retried.result(timeout=10) == "owner-retried"
        assert (
            provider.store.replay_failed_idempotency("cancelCoreRunV1", arguments) == replacement
        )


def test_run_control_failures_use_the_frozen_typed_error_contract(tmp_path: Path) -> None:
    run_control = _FailingRunControl()
    app = _app(tmp_path, run_control=run_control)
    with TestClient(app) as client:
        runs = client.get("/v1/runs", headers=AUTH)
        status = client.get("/v1/status", headers=AUTH)

    for response in (runs, status):
        assert response.status_code == 503
        assert response.json()["code"] == "run_owner_unavailable"
        assert response.json()["retryable"] is True


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
