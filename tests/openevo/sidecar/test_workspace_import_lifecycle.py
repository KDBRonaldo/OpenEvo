from __future__ import annotations

from pathlib import Path
import threading

from fastapi.testclient import TestClient
from httpx import Response as HttpResponse
import pytest

from desktop.server import launcher as desktop_launcher
from desktop.sidecar.workspace_imports import WorkspaceImportIntegrityError


SESSION_TOKEN = "7c" * 32
HANDOFF_TOKEN = "8d" * 32
SESSION_HEADERS = {desktop_launcher.NATIVE_SESSION_HEADER: SESSION_TOKEN}
HANDOFF_HEADERS = {desktop_launcher.NATIVE_HANDOFF_HEADER: HANDOFF_TOKEN}


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
    status = source_root.stat()
    request: dict[str, object] = {
        "schema_version": "1",
        "kind": "native_folder_snapshot",
        "action_id": action_id,
        "selected_path": str(source_root.resolve()),
        "selected_device": status.st_dev,
        "selected_inode": status.st_ino,
    }
    if project_id is not None:
        request["project_id"] = project_id
    response = client.post(
        "/openevo-native/workspace-imports",
        headers=HANDOFF_HEADERS,
        json=request,
    )
    assert response.status_code == 201
    return response.json()


def _create_project(
    client: TestClient,
    profile_id: str,
    source: dict[str, object],
) -> dict[str, object]:
    response = client.post(
        "/desktop/v1/projects",
        headers={**SESSION_HEADERS, "Idempotency-Key": "project-lifecycle-0001"},
        json={
            "name": "Protein design",
            "profile_id": profile_id,
            "task": {"title": "Design", "objective": "Improve held-out stability."},
            "source": source,
            "execution": {
                "mode": "codex_subscription_transcript",
                "codex_model": "gpt-5",
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
    return (
        config_root
        / desktop_launcher.LOCAL_API_STATE_DIRECTORY
        / "workspace-imports"
        / import_id
    )


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
        retained = _import_source(
            client,
            source_root,
            action_id="native-source-retained-0001",
        )
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
