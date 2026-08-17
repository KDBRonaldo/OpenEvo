from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from scripts.dev import formal_desktop_assets as formal_assets


COMMIT = "a" * 40


def test_reads_registry_digest_only_from_matching_daemon_manifest(tmp_path: Path) -> None:
    manifest = tmp_path / "openevo-daemon-bundle.json"
    manifest.write_text(
        json.dumps(
            {
                "core": {"registry_digest": "b" * 64},
                "release": {"source_commit": COMMIT},
            }
        ),
        encoding="utf-8",
    )

    assert formal_assets._read_registry_digest(manifest, source_commit=COMMIT) == "b" * 64
    with pytest.raises(formal_assets.FormalDevelopmentAssetError):
        formal_assets._read_registry_digest(manifest, source_commit="c" * 40)


def test_prepare_uses_commit_scoped_formal_assets_without_legacy_daemon(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repository = tmp_path / "repo"
    repository.mkdir()
    runtime = tmp_path / "runtime.tar.gz"
    runtime.write_bytes(b"runtime")
    calls: list[str] = []

    def fake_runtime(*, cache_root: Path, configured: Path | None) -> Path:
        assert cache_root == tmp_path / "cache"
        assert configured == runtime
        calls.append("runtime")
        return runtime

    def fake_release_assets(**kwargs: object) -> None:
        calls.append("release")
        root = Path(kwargs["commit_root"]) / "openevo-release-assets"
        for relative in (
            "core/framework-lock.json",
            "daemon/openevo-daemon-bundle.json",
            "daemon/openevo-daemon-linux-x86_64",
            "runtime/runtime.tar.gz",
        ):
            path = root / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"asset")
        (root / "release-assets.json").write_text(
            json.dumps({"schema_version": 1, "source_commit": COMMIT}),
            encoding="utf-8",
        )

    def fake_askpass(**kwargs: object) -> None:
        calls.append("askpass")
        destination = Path(kwargs["destination"])
        destination.write_bytes(b"helper")
        os.chmod(destination, 0o755)

    monkeypatch.setattr(formal_assets, "_prepare_runtime_archive", fake_runtime)
    monkeypatch.setattr(formal_assets, "_build_release_assets", fake_release_assets)
    monkeypatch.setattr(formal_assets, "_build_askpass_helper", fake_askpass)

    prepared = formal_assets.prepare_formal_development_assets(
        repository_root=repository,
        source_commit=COMMIT,
        cache_root=tmp_path / "cache",
        managed_runtime_archive=runtime,
    )

    assert calls == ["runtime", "release", "askpass"]
    assert prepared.source_commit == COMMIT
    assert prepared.release_assets_root == (
        tmp_path / "cache" / COMMIT / "openevo-release-assets"
    )
    assert prepared.askpass_helper == tmp_path / "cache" / COMMIT / "openevo-ssh-askpass"


def test_staged_assets_must_match_the_current_commit(tmp_path: Path) -> None:
    root = tmp_path / "openevo-release-assets"
    for relative in (
        "core/framework-lock.json",
        "daemon/openevo-daemon-bundle.json",
        "daemon/openevo-daemon-linux-x86_64",
        "runtime/runtime.tar.gz",
    ):
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"asset")
    (root / "release-assets.json").write_text(
        json.dumps({"schema_version": 1, "source_commit": "b" * 40}),
        encoding="utf-8",
    )

    with pytest.raises(
        formal_assets.FormalDevelopmentAssetError,
        match="another commit",
    ):
        formal_assets._verify_staged_assets(root, source_commit=COMMIT)


def test_formal_entrypoint_does_not_launch_the_development_daemon() -> None:
    source = Path("scripts/dev/formal_desktop_assets.py").read_text(encoding="utf-8")
    launcher = Path("scripts/dev/run_desktop_live.py").read_text(encoding="utf-8")

    assert "live_agent_daemon.py" not in source
    assert "live_agent_daemon.py" not in launcher
    assert "desktop.server.launcher" in launcher
