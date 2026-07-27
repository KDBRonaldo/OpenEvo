from __future__ import annotations

import hashlib
from pathlib import Path
import threading
import time

from fastapi.testclient import TestClient
from httpx import Response as HttpResponse
import pytest

from desktop.server import launcher as desktop_launcher
from desktop.sidecar import native_workspace as native_workspace_module
from desktop.sidecar import workspace_imports as workspace_imports_module


SESSION_TOKEN = "7c" * 32
HANDOFF_TOKEN = "8d" * 32
HANDOFF_HEADERS = {desktop_launcher.NATIVE_HANDOFF_HEADER: HANDOFF_TOKEN}
SESSION_HEADERS = {desktop_launcher.NATIVE_SESSION_HEADER: SESSION_TOKEN}


def _cancellation_token(action_id: str) -> str:
    return hashlib.sha256(f"test-cancel:{action_id}".encode()).hexdigest()


def _static_root(root: Path) -> Path:
    assets = root / "assets"
    assets.mkdir(parents=True)
    (root / "index.html").write_text(
        "<!doctype html><title>OpenEvo</title><script src='/assets/app.js'></script>",
        encoding="utf-8",
    )
    (assets / "app.js").write_text("window.__openevoTest = true;", encoding="utf-8")
    return root


def _app(config_root: Path, static_root: Path):
    config_root.mkdir(mode=0o700, parents=True, exist_ok=True)
    helper_path = config_root / "openevo-ssh-askpass"
    helper_bytes = b"#!/bin/sh\nexit 1\n"
    helper_path.write_bytes(helper_bytes)
    helper_path.chmod(0o755)
    return desktop_launcher.create_app(
        static_root=static_root,
        desktop_config_root=config_root,
        native_frame=desktop_launcher._NativeLauncherFrame(
            instance_id="1a" * 16,
            readiness_key=bytes.fromhex("5a" * 32),
            session_token=SESSION_TOKEN,
            handoff_token=HANDOFF_TOKEN,
        ),
        source_commit="89baeb26",
        build_channel="test",
        core_assets_root=config_root / "deferred-core-assets",
        packaged_askpass_helper_path=helper_path,
        packaged_askpass_helper_sha256=hashlib.sha256(helper_bytes).hexdigest(),
        packaged_askpass_helper_byte_size=len(helper_bytes),
    )


def _import_pending(
    client: TestClient,
    source_root: Path,
    *,
    action_id: str,
    project_id: str | None = None,
    cancellation_token: str | None = None,
) -> dict[str, object]:
    response = _import_response(
        client,
        source_root,
        action_id=action_id,
        project_id=project_id,
        cancellation_token=cancellation_token,
    )
    assert response.status_code == 202, response.text
    operation = _wait_operation(client, response)
    assert operation["status"] == "succeeded", operation
    result = operation["result"]
    assert isinstance(result, dict)
    return {
        "schema_version": "2",
        "source": {
            "kind": "native_folder_snapshot",
            "display_name": result["display_name"],
            "import_ref": {
                "import_id": result["import_id"],
                "content_sha256": result["content_sha256"],
                "byte_size": result["byte_size"],
                "entry_count": result["entry_count"],
                "extracted_byte_size": result["extracted_byte_size"],
            },
        },
    }


def _wait_operation(
    client: TestClient,
    response: HttpResponse,
    *,
    timeout: float = 5.0,
) -> dict[str, object]:
    assert response.status_code == 202, response.text
    operation_id = response.json()["operation_id"]
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        observed = client.get(
            f"/desktop/v2/operations/{operation_id}",
            headers=SESSION_HEADERS,
        )
        assert observed.status_code == 200, observed.text
        operation = observed.json()
        if operation["status"] in {"succeeded", "failed", "cancelled"}:
            return operation
        time.sleep(0.01)
    raise AssertionError("native workspace lifecycle operation did not finish")


def _import_response(
    client: TestClient,
    source_root: Path,
    *,
    action_id: str,
    project_id: str | None = None,
    cancellation_token: str | None = None,
) -> HttpResponse:
    status = source_root.stat()
    request: dict[str, object] = {
        "schema_version": "2",
        "kind": "native_folder_snapshot",
        "action_id": action_id,
        "selected_path": str(source_root.resolve()),
        "selected_device": status.st_dev,
        "selected_inode": status.st_ino,
        "cancellation_token": cancellation_token or _cancellation_token(action_id),
    }
    if project_id is not None:
        request["project_id"] = project_id
    return client.post(
        "/openevo-native/workspace-imports",
        headers=HANDOFF_HEADERS,
        json=request,
    )


def _discard_pending(
    client: TestClient,
    pending: dict[str, object],
    *,
    action_id: str,
    project_id: str | None = None,
) -> HttpResponse:
    request: dict[str, object] = {
        "schema_version": "2",
        "action_id": action_id,
    }
    if project_id is not None:
        request["project_id"] = project_id
    return client.post(
        "/openevo-native/workspace-imports/discard",
        headers=HANDOFF_HEADERS,
        json=request,
    )


def _cancel_import(
    client: TestClient,
    *,
    action_id: str,
    cancellation_token: str | None = None,
) -> HttpResponse:
    return client.post(
        "/openevo-native/workspace-imports/cancel",
        headers=HANDOFF_HEADERS,
        json={
            "schema_version": "2",
            "action_id": action_id,
            "cancellation_token": cancellation_token or _cancellation_token(action_id),
        },
    )


def _workspace_import_root(config_root: Path) -> Path:
    return config_root / desktop_launcher.DESKTOP_STATE_DIRECTORY / "workspace-imports-v2"


def _import_directory(config_root: Path, pending: dict[str, object]) -> Path:
    source = pending["source"]
    assert isinstance(source, dict)
    import_ref = source["import_ref"]
    assert isinstance(import_ref, dict)
    import_id = import_ref["import_id"]
    assert isinstance(import_id, str)
    return _workspace_import_root(config_root) / import_id


def test_picker_pending_import_requires_exact_explicit_discard(tmp_path: Path) -> None:
    config_root = tmp_path / "config"
    static_root = _static_root(tmp_path / "static")
    source_root = tmp_path / "research"
    source_root.mkdir()
    (source_root / "notes.txt").write_text("observation", encoding="utf-8")

    with TestClient(_app(config_root, static_root)) as client:
        action_id = "native-source-explicit-discard-0001"
        pending = _import_pending(client, source_root, action_id=action_id)
        import_directory = _import_directory(config_root, pending)
        assert import_directory.is_dir()

        assert (
            _discard_pending(
                client,
                pending,
                action_id=action_id,
                project_id="project-owned-by-another-action",
            ).status_code
            == 409
        )
        assert import_directory.is_dir()

        assert _discard_pending(client, pending, action_id=action_id).status_code == 204
        assert not import_directory.exists()
        assert _discard_pending(client, pending, action_id=action_id).status_code == 204


def test_identical_archives_have_action_bound_authority_and_replay_after_restart(
    tmp_path: Path,
) -> None:
    config_root = tmp_path / "config"
    static_root = _static_root(tmp_path / "static")
    first_root = tmp_path / "first-empty-project"
    second_root = tmp_path / "second-empty-project"
    first_root.mkdir()
    second_root.mkdir()
    first_action = "native-identical-empty-project-a-0001"
    second_action = "native-identical-empty-project-b-0001"

    with TestClient(_app(config_root, static_root)) as client:
        first_pending = _import_pending(client, first_root, action_id=first_action)
        second_pending = _import_pending(client, second_root, action_id=second_action)
        first_source = first_pending["source"]
        second_source = second_pending["source"]
        assert isinstance(first_source, dict)
        assert isinstance(second_source, dict)
        assert (
            first_source["import_ref"]["content_sha256"]
            == second_source["import_ref"]["content_sha256"]
        )
        assert first_source["import_ref"]["import_id"] != second_source["import_ref"]["import_id"]

    with TestClient(_app(config_root, static_root)) as restarted:
        assert _import_pending(restarted, first_root, action_id=first_action) == first_pending

        owner_conflict = _import_response(
            restarted,
            first_root,
            action_id=first_action,
            project_id="project-owned-by-another-action",
        )
        assert owner_conflict.status_code == 409

        (first_root / "changed.txt").write_text("changed", encoding="utf-8")
        changed_content = _import_pending(
            restarted,
            first_root,
            action_id=first_action,
        )
        assert changed_content == first_pending

        assert (
            _discard_pending(restarted, first_pending, action_id=first_action).status_code == 204
        )
        assert (
            _discard_pending(restarted, second_pending, action_id=second_action).status_code == 204
        )


def test_native_import_cancel_stops_archive_work_and_rejects_stale_identity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_root = tmp_path / "config"
    static_root = _static_root(tmp_path / "static")
    source_root = tmp_path / "cancel-during-archive"
    source_root.mkdir()
    (source_root / "payload.bin").write_bytes(b"x" * (2 * 1024 * 1024))
    action_id = "native-cancel-during-archive-0001"
    cancellation_token = _cancellation_token(action_id)
    archive_written = threading.Event()
    release_worker = threading.Event()

    def hold_after_archive(_root_descriptor: int) -> None:
        archive_written.set()
        assert release_worker.wait(timeout=5)

    monkeypatch.setattr(native_workspace_module, "_after_archive_write", hold_after_archive)
    with TestClient(_app(config_root, static_root)) as client:
        response = _import_response(
            client,
            source_root,
            action_id=action_id,
            cancellation_token=cancellation_token,
        )
        assert response.status_code == 202, response.text
        assert archive_written.wait(timeout=5)
        stale = _cancel_import(
            client,
            action_id=action_id,
            cancellation_token="ff" * 32,
        )
        assert stale.status_code == 409
        assert (
            _cancel_import(
                client,
                action_id=action_id,
                cancellation_token=cancellation_token,
            ).status_code
            == 204
        )
        release_worker.set()
        assert _wait_operation(client, response)["status"] == "cancelled"

    assert list(_workspace_import_root(config_root).iterdir()) == []


def test_native_import_cancel_before_begin_is_identity_bound_and_fail_closed(
    tmp_path: Path,
) -> None:
    config_root = tmp_path / "config"
    static_root = _static_root(tmp_path / "static")
    source_root = tmp_path / "cancel-before-begin"
    source_root.mkdir()
    (source_root / "notes.txt").write_text("do not import", encoding="utf-8")
    action_id = "native-cancel-before-begin-0001"

    with TestClient(_app(config_root, static_root)) as client:
        assert _cancel_import(client, action_id=action_id).status_code == 204
        pending = _import_pending(client, source_root, action_id=action_id)
        assert _discard_pending(client, pending, action_id=action_id).status_code == 204

        stale_action = "native-stale-cancel-before-begin-0001"
        assert (
            _cancel_import(
                client,
                action_id=stale_action,
                cancellation_token="ff" * 32,
            ).status_code
            == 204
        )
        stale_pending = _import_pending(client, source_root, action_id=stale_action)
        assert _discard_pending(client, stale_pending, action_id=stale_action).status_code == 204

    assert list(_workspace_import_root(config_root).iterdir()) == []


def test_cancel_after_atomic_publication_discards_the_recoverable_lease(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_root = tmp_path / "config"
    static_root = _static_root(tmp_path / "static")
    source_root = tmp_path / "cancel-after-publication"
    source_root.mkdir()
    (source_root / "notes.txt").write_text("published", encoding="utf-8")
    action_id = "native-cancel-after-publication-0001"
    published = threading.Event()
    release_worker = threading.Event()

    def hold_after_publish(_root_descriptor: int, _import_id: str) -> None:
        published.set()
        assert release_worker.wait(timeout=5)

    monkeypatch.setattr(workspace_imports_module, "_after_import_publish", hold_after_publish)
    with TestClient(_app(config_root, static_root)) as client:
        response = _import_response(client, source_root, action_id=action_id)
        assert response.status_code == 202, response.text
        assert published.wait(timeout=5)
        assert _cancel_import(client, action_id=action_id).status_code == 204
        release_worker.set()
        assert _wait_operation(client, response)["status"] == "cancelled"

    assert list(_workspace_import_root(config_root).iterdir()) == []


def test_v2_restart_retains_valid_pending_and_discards_corrupt_pending(
    tmp_path: Path,
) -> None:
    config_root = tmp_path / "config"
    static_root = _static_root(tmp_path / "static")
    valid_root = tmp_path / "valid-research"
    corrupt_root = tmp_path / "corrupt-research"
    valid_root.mkdir()
    corrupt_root.mkdir()
    (valid_root / "notes.txt").write_text("retain", encoding="utf-8")
    (corrupt_root / "notes.txt").write_text("recover", encoding="utf-8")
    valid_action = "native-valid-restart-import-0001"
    corrupt_action = "native-corrupt-restart-import-0001"

    with TestClient(_app(config_root, static_root)) as client:
        valid_pending = _import_pending(client, valid_root, action_id=valid_action)
        corrupt_pending = _import_pending(client, corrupt_root, action_id=corrupt_action)

    valid_directory = _import_directory(config_root, valid_pending)
    corrupt_directory = _import_directory(config_root, corrupt_pending)
    archive = corrupt_directory / "archive.tar"
    with archive.open("r+b") as stream:
        stream.seek(0)
        stream.write(b"X")
        stream.flush()

    with TestClient(_app(config_root, static_root)) as restarted:
        assert valid_directory.is_dir()
        assert not corrupt_directory.exists()
        assert _import_pending(restarted, valid_root, action_id=valid_action) == valid_pending
        unavailable = _import_response(
            restarted,
            corrupt_root,
            action_id=corrupt_action,
        )
        assert unavailable.status_code == 409
        recovered_action = "native-corrupt-restart-import-0002"
        recovered = _import_pending(
            restarted,
            corrupt_root,
            action_id=recovered_action,
        )
        assert (
            recovered["source"]["import_ref"]["content_sha256"]
            == (corrupt_pending["source"]["import_ref"]["content_sha256"])
        )
        assert (
            _discard_pending(
                restarted,
                valid_pending,
                action_id=valid_action,
            ).status_code
            == 204
        )
        assert (
            _discard_pending(
                restarted,
                recovered,
                action_id=recovered_action,
            ).status_code
            == 204
        )

    assert list(_workspace_import_root(config_root).iterdir()) == []
