from __future__ import annotations

import hashlib
from pathlib import Path
import threading

from fastapi.testclient import TestClient
from httpx import Response as HttpResponse
import pytest

from desktop.server import launcher as desktop_launcher
from desktop.sidecar import native_workspace as native_workspace_module
from desktop.sidecar import workspace_imports as workspace_imports_module
from desktop.sidecar.workspace_imports import WorkspaceImportIntegrityError


SESSION_TOKEN = "7c" * 32
HANDOFF_TOKEN = "8d" * 32
SESSION_HEADERS = {desktop_launcher.NATIVE_SESSION_HEADER: SESSION_TOKEN}
HANDOFF_HEADERS = {desktop_launcher.NATIVE_HANDOFF_HEADER: HANDOFF_TOKEN}


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
    )


def _create_profile(client: TestClient) -> dict[str, object]:
    response = client.post(
        "/desktop/v1/profiles",
        headers={**SESSION_HEADERS, "Idempotency-Key": "profile-lifecycle-0001"},
        json={
            "name": "Research server",
            "host": "compute.example.org",
            "port": 22,
            "user": "researcher",
        },
    )
    assert response.status_code == 201
    return response.json()


def _import_source(
    client: TestClient,
    source_root: Path,
    *,
    action_id: str,
    project_id: str | None = None,
) -> dict[str, object]:
    return _import_pending(
        client,
        source_root,
        action_id=action_id,
        project_id=project_id,
    )["source"]


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
    assert response.status_code == 201
    payload = response.json()
    assert set(payload) == {"schema_version", "source", "lease_token"}
    assert payload["schema_version"] == "1"
    assert isinstance(payload["source"], dict)
    assert isinstance(payload["lease_token"], str)
    return payload


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
        "schema_version": "1",
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
    source = pending["source"]
    assert isinstance(source, dict)
    import_ref = source["import_ref"]
    request: dict[str, object] = {
        "schema_version": "1",
        "action_id": action_id,
        "import_ref": import_ref,
        "lease_token": pending["lease_token"],
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
            "schema_version": "1",
            "action_id": action_id,
            "cancellation_token": cancellation_token or _cancellation_token(action_id),
        },
    )


def _create_project(
    client: TestClient,
    profile_id: str,
    source: dict[str, object],
    *,
    idempotency_key: str = "project-lifecycle-0001",
    name: str = "Protein design",
) -> dict[str, object]:
    response = client.post(
        "/desktop/v1/projects",
        headers={**SESSION_HEADERS, "Idempotency-Key": idempotency_key},
        json={
            "name": name,
            "profile_id": profile_id,
            "task": {"title": "Design", "objective": "Improve held-out stability."},
            "source": source,
            "execution": {
                "mode": "codex_subscription_transcript",
                "codex_model": "gpt-5.3-codex-spark",
            },
            "evolution": {"targets": {}},
        },
    )
    assert response.status_code == 201
    return response.json()


def _import_directory(config_root: Path, source: dict[str, object]) -> Path:
    import_ref = source["import_ref"]
    assert isinstance(import_ref, dict)
    import_id = import_ref["import_id"]
    assert isinstance(import_id, str)
    return config_root / desktop_launcher.DESKTOP_STATE_DIRECTORY / "workspace-imports" / import_id


def test_picker_discard_and_failed_save_remove_pending_imports(tmp_path: Path) -> None:
    config_root = tmp_path / "config"
    static_root = _static_root(tmp_path / "static")
    source_root = tmp_path / "research"
    source_root.mkdir()
    (source_root / "notes.txt").write_text("observation", encoding="utf-8")

    with TestClient(_app(config_root, static_root)) as client:
        action_id = "native-source-failed-save-0001"
        pending = _import_pending(client, source_root, action_id=action_id)
        source = pending["source"]
        assert isinstance(source, dict)
        import_directory = _import_directory(config_root, source)
        failed = client.post(
            "/desktop/v1/projects",
            headers={**SESSION_HEADERS, "Idempotency-Key": "failed-project-save-0001"},
            json={
                "name": "Missing profile",
                "profile_id": "profile-does-not-exist",
                "task": {"title": "Design", "objective": "Cannot commit."},
                "source": source,
                "execution": {
                    "mode": "codex_subscription_transcript",
                    "codex_model": "gpt-5.3-codex-spark",
                },
                "evolution": {"targets": {}},
            },
        )
        assert failed.status_code >= 400
        assert import_directory.is_dir()

        discarded = _discard_pending(client, pending, action_id=action_id)
        assert discarded.status_code == 204
        assert not import_directory.exists()
        assert _discard_pending(client, pending, action_id=action_id).status_code == 204


def test_identical_archives_have_project_bound_authority_and_replay_after_restart(
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
        profile = _create_profile(client)
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
        first_project = _create_project(
            client,
            str(profile["profile_id"]),
            first_source,
            idempotency_key="identical-empty-project-a-0001",
            name="First empty project",
        )
        second_project = _create_project(
            client,
            str(profile["profile_id"]),
            second_source,
            idempotency_key="identical-empty-project-b-0001",
            name="Second empty project",
        )
        assert first_project["project_id"] != second_project["project_id"]

    with TestClient(_app(config_root, static_root)) as restarted:
        replay_pending = _import_pending(restarted, first_root, action_id=first_action)
        assert replay_pending["source"] == first_source
        replayed_project = _create_project(
            restarted,
            str(profile["profile_id"]),
            first_source,
            idempotency_key="identical-empty-project-a-0001",
            name="First empty project",
        )
        assert replayed_project == first_project

        first_status = first_root.stat()
        owner_conflict = restarted.post(
            "/openevo-native/workspace-imports",
            headers=HANDOFF_HEADERS,
            json={
                "schema_version": "1",
                "kind": "native_folder_snapshot",
                "action_id": first_action,
                "selected_path": str(first_root.resolve()),
                "selected_device": first_status.st_dev,
                "selected_inode": first_status.st_ino,
                "cancellation_token": _cancellation_token(first_action),
                "project_id": second_project["project_id"],
            },
        )
        assert owner_conflict.status_code == 409

        (first_root / "changed.txt").write_text("changed", encoding="utf-8")
        changed_content = restarted.post(
            "/openevo-native/workspace-imports",
            headers=HANDOFF_HEADERS,
            json={
                "schema_version": "1",
                "kind": "native_folder_snapshot",
                "action_id": first_action,
                "selected_path": str(first_root.resolve()),
                "selected_device": first_status.st_dev,
                "selected_inode": first_status.st_ino,
                "cancellation_token": _cancellation_token(first_action),
            },
        )
        assert changed_content.status_code == 409


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
    responses: list[HttpResponse] = []
    with TestClient(_app(config_root, static_root)) as client:
        worker = threading.Thread(
            target=lambda: responses.append(
                _import_response(
                    client,
                    source_root,
                    action_id=action_id,
                    cancellation_token=cancellation_token,
                )
            )
        )
        worker.start()
        assert archive_written.wait(timeout=5)
        stale = _cancel_import(
            client,
            action_id=action_id,
            cancellation_token="ff" * 32,
        )
        assert stale.status_code == 409
        assert worker.is_alive()
        assert (
            _cancel_import(
                client,
                action_id=action_id,
                cancellation_token=cancellation_token,
            ).status_code
            == 204
        )
        release_worker.set()
        worker.join(timeout=5)

    assert not worker.is_alive()
    assert len(responses) == 1
    assert responses[0].status_code == 409
    assert responses[0].json()["code"] == "workspace_import_cancelled"
    import_root = config_root / desktop_launcher.DESKTOP_STATE_DIRECTORY / "workspace-imports"
    assert list(import_root.iterdir()) == []


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
        cancelled = _import_response(client, source_root, action_id=action_id)
        assert cancelled.status_code == 409
        assert cancelled.json()["code"] == "workspace_import_cancelled"

        stale_action = "native-stale-cancel-before-begin-0001"
        assert (
            _cancel_import(
                client,
                action_id=stale_action,
                cancellation_token="ff" * 32,
            ).status_code
            == 204
        )
        pending = _import_pending(client, source_root, action_id=stale_action)
        assert _discard_pending(client, pending, action_id=stale_action).status_code == 204

    import_root = config_root / desktop_launcher.DESKTOP_STATE_DIRECTORY / "workspace-imports"
    assert list(import_root.iterdir()) == []


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
    responses: list[HttpResponse] = []
    with TestClient(_app(config_root, static_root)) as client:
        worker = threading.Thread(
            target=lambda: responses.append(
                _import_response(client, source_root, action_id=action_id)
            )
        )
        worker.start()
        assert published.wait(timeout=5)
        assert _cancel_import(client, action_id=action_id).status_code == 204
        release_worker.set()
        worker.join(timeout=5)

    assert not worker.is_alive()
    assert len(responses) == 1
    assert responses[0].status_code == 409
    assert responses[0].json()["code"] == "workspace_import_cancelled"
    import_root = config_root / desktop_launcher.DESKTOP_STATE_DIRECTORY / "workspace-imports"
    assert list(import_root.iterdir()) == []


def test_discard_rereads_references_after_a_concurrent_project_commit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_root = tmp_path / "config"
    static_root = _static_root(tmp_path / "static")
    source_root = tmp_path / "research"
    source_root.mkdir()
    (source_root / "notes.txt").write_text("observation", encoding="utf-8")
    app = _app(config_root, static_root)

    with TestClient(app) as client:
        profile = _create_profile(client)
        action_id = "native-source-concurrent-commit-0001"
        pending = _import_pending(client, source_root, action_id=action_id)
        source = pending["source"]
        assert isinstance(source, dict)
        import_directory = _import_directory(config_root, source)
        provider = app.state.desktop_release_provider
        original_adopt = provider._adopt_project_source
        commit_durable = threading.Event()
        allow_adopt = threading.Event()

        def delayed_adopt(source_to_adopt, *, project_id: str) -> None:
            commit_durable.set()
            assert allow_adopt.wait(timeout=5)
            original_adopt(source_to_adopt, project_id=project_id)

        monkeypatch.setattr(provider, "_adopt_project_source", delayed_adopt)
        create_responses: list[HttpResponse] = []
        discard_responses: list[HttpResponse] = []

        creator = threading.Thread(
            target=lambda: create_responses.append(
                _create_project(client, str(profile["profile_id"]), source)
            )
        )
        creator.start()
        assert commit_durable.wait(timeout=5)
        discarder = threading.Thread(
            target=lambda: discard_responses.append(
                _discard_pending(client, pending, action_id=action_id)
            )
        )
        discarder.start()
        assert not allow_adopt.wait(timeout=0.1)
        allow_adopt.set()
        creator.join(timeout=5)
        discarder.join(timeout=5)

        assert not creator.is_alive()
        assert not discarder.is_alive()
        assert len(create_responses) == 1
        assert len(discard_responses) == 1
        assert discard_responses[0].status_code == 204
        assert import_directory.is_dir()


def test_project_source_replacement_and_delete_release_committed_imports(
    tmp_path: Path,
) -> None:
    config_root = tmp_path / "config"
    static_root = _static_root(tmp_path / "static")
    source_root = tmp_path / "research"
    source_root.mkdir()
    (source_root / "results.csv").write_text("sample,value\na,4\n", encoding="utf-8")

    with TestClient(_app(config_root, static_root)) as client:
        profile = _create_profile(client)
        source = _import_source(
            client,
            source_root,
            action_id="native-source-lifecycle-0001",
        )
        project = _create_project(client, str(profile["profile_id"]), source)
        import_directory = _import_directory(config_root, source)
        assert import_directory.is_dir()

        replaced = client.patch(
            f"/desktop/v1/projects/{project['project_id']}",
            headers={**SESSION_HEADERS, "If-Match": str(project["etag"])},
            json={"source": {"kind": "scratch", "display_name": "New workspace"}},
        )
        assert replaced.status_code == 200
        assert not import_directory.exists()

        second_source = _import_source(
            client,
            source_root,
            action_id="native-source-lifecycle-0002",
            project_id=str(project["project_id"]),
        )
        second_directory = _import_directory(config_root, second_source)
        restored = client.patch(
            f"/desktop/v1/projects/{project['project_id']}",
            headers={**SESSION_HEADERS, "If-Match": str(replaced.json()["etag"])},
            json={"source": second_source},
        )
        assert restored.status_code == 200
        assert second_directory.is_dir()

        deleted = client.delete(
            f"/desktop/v1/projects/{project['project_id']}",
            headers={**SESSION_HEADERS, "If-Match": str(restored.json()["etag"])},
        )
        assert deleted.status_code == 204
        assert not second_directory.exists()


def test_project_source_display_name_change_retains_same_import_ref(tmp_path: Path) -> None:
    config_root = tmp_path / "config"
    static_root = _static_root(tmp_path / "static")
    source_root = tmp_path / "research"
    source_root.mkdir()
    (source_root / "results.csv").write_text("sample,value\na,4\n", encoding="utf-8")

    with TestClient(_app(config_root, static_root)) as client:
        profile = _create_profile(client)
        source = _import_source(
            client,
            source_root,
            action_id="native-source-display-name-0001",
        )
        project = _create_project(client, str(profile["profile_id"]), source)
        import_directory = _import_directory(config_root, source)
        renamed_source = dict(source)
        renamed_source["display_name"] = "Renamed research workspace"

        renamed = client.patch(
            f"/desktop/v1/projects/{project['project_id']}",
            headers={**SESSION_HEADERS, "If-Match": str(project["etag"])},
            json={"source": renamed_source},
        )

        assert renamed.status_code == 200
        assert renamed.json()["source"] == renamed_source
        assert import_directory.is_dir()


def test_post_commit_cleanup_rechecks_references_against_concurrent_patch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_root = tmp_path / "config"
    static_root = _static_root(tmp_path / "static")
    source_root = tmp_path / "research"
    source_root.mkdir()
    (source_root / "notes.txt").write_text("observation", encoding="utf-8")
    app = _app(config_root, static_root)

    with TestClient(app) as client:
        profile = _create_profile(client)
        source = _import_source(
            client,
            source_root,
            action_id="native-source-concurrent-patch-0001",
        )
        project = _create_project(client, str(profile["profile_id"]), source)
        import_directory = _import_directory(config_root, source)
        provider = app.state.desktop_release_provider
        original_release = provider._release_project_source
        cleanup_entered = threading.Event()
        continue_cleanup = threading.Event()

        def delayed_release(source_to_release, *, project_id: str) -> None:
            if source_to_release.kind == "native_folder_snapshot":
                cleanup_entered.set()
                assert continue_cleanup.wait(timeout=5)
            original_release(source_to_release, project_id=project_id)

        monkeypatch.setattr(provider, "_release_project_source", delayed_release)
        responses: list[HttpResponse] = []

        def replace_with_scratch() -> None:
            responses.append(
                client.patch(
                    f"/desktop/v1/projects/{project['project_id']}",
                    headers={**SESSION_HEADERS, "If-Match": str(project["etag"])},
                    json={"source": {"kind": "scratch", "display_name": "Temporary"}},
                )
            )

        worker = threading.Thread(target=replace_with_scratch)
        worker.start()
        try:
            assert cleanup_entered.wait(timeout=5)
            interim = client.get(
                f"/desktop/v1/projects/{project['project_id']}",
                headers=SESSION_HEADERS,
            )
            assert interim.status_code == 200
            restored = client.patch(
                f"/desktop/v1/projects/{project['project_id']}",
                headers={**SESSION_HEADERS, "If-Match": str(interim.json()["etag"])},
                json={"source": source},
            )
            assert restored.status_code == 200
        finally:
            continue_cleanup.set()
            worker.join(timeout=5)

        assert not worker.is_alive()
        assert len(responses) == 1
        assert responses[0].status_code == 200
        assert import_directory.is_dir()
        current = client.get(
            f"/desktop/v1/projects/{project['project_id']}",
            headers=SESSION_HEADERS,
        )
        assert current.status_code == 200
        assert current.json()["source"] == source


def test_startup_reconciliation_discards_orphans_and_retains_project_sources(
    tmp_path: Path,
) -> None:
    config_root = tmp_path / "config"
    static_root = _static_root(tmp_path / "static")
    source_root = tmp_path / "research"
    source_root.mkdir()
    (source_root / "notes.txt").write_text("observation", encoding="utf-8")

    with TestClient(_app(config_root, static_root)) as client:
        profile = _create_profile(client)
        retained_pending = _import_pending(
            client,
            source_root,
            action_id="native-source-retained-0001",
        )
        retained = retained_pending["source"]
        assert isinstance(retained, dict)
        project = _create_project(client, str(profile["profile_id"]), retained)
        (source_root / "new-observation.txt").write_text("changed", encoding="utf-8")
        orphan = _import_source(
            client,
            source_root,
            action_id="native-source-orphaned-0001",
            project_id=str(project["project_id"]),
        )
        retained_directory = _import_directory(config_root, retained)
        orphan_directory = _import_directory(config_root, orphan)
        assert retained_directory.is_dir()
        assert orphan_directory.is_dir()

    with TestClient(_app(config_root, static_root)) as client:
        fetched = client.get(
            f"/desktop/v1/projects/{project['project_id']}",
            headers=SESSION_HEADERS,
        )
        assert fetched.status_code == 200
        assert fetched.json()["source"] == retained
        assert (
            _discard_pending(
                client,
                retained_pending,
                action_id="native-source-retained-0001",
            ).status_code
            == 204
        )
        assert retained_directory.is_dir()
        assert not orphan_directory.exists()


def test_startup_preserves_corrupt_referenced_import_and_fails_closed(tmp_path: Path) -> None:
    config_root = tmp_path / "config"
    static_root = _static_root(tmp_path / "static")
    source_root = tmp_path / "research"
    source_root.mkdir()
    (source_root / "notes.txt").write_text("observation", encoding="utf-8")

    with TestClient(_app(config_root, static_root)) as client:
        profile = _create_profile(client)
        source = _import_source(
            client,
            source_root,
            action_id="native-source-corrupt-startup-0001",
        )
        project = _create_project(client, str(profile["profile_id"]), source)
        import_directory = _import_directory(config_root, source)
        (source_root / "later.txt").write_text("unreferenced", encoding="utf-8")
        orphan = _import_source(
            client,
            source_root,
            action_id="native-source-corrupt-orphan-0001",
            project_id=str(project["project_id"]),
        )
        orphan_directory = _import_directory(config_root, orphan)

    archive = import_directory / "archive.tar"
    with archive.open("r+b") as stream:
        stream.seek(0)
        stream.write(b"X")
        stream.flush()

    with pytest.raises(WorkspaceImportIntegrityError, match="referenced workspace import"):
        _app(config_root, static_root)

    assert import_directory.is_dir()
    assert archive.is_file()
    assert orphan_directory.is_dir()
