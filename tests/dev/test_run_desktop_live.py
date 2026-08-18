from __future__ import annotations

import subprocess
import os
from pathlib import Path
from unittest.mock import patch

from scripts.dev import run_desktop_live


def _git(repository: Path, *arguments: str) -> None:
    subprocess.run(
        ["git", *arguments],
        cwd=repository,
        check=True,
        capture_output=True,
    )


def test_checkout_identity_content_addresses_dirty_source_bytes(tmp_path: Path) -> None:
    repository = tmp_path / "repo"
    repository.mkdir()
    _git(repository, "init", "--quiet")
    _git(repository, "config", "user.email", "developer@example.invalid")
    _git(repository, "config", "user.name", "OpenEvo test")
    tracked = repository / "tracked.py"
    tracked.write_text("VALUE = 1\n", encoding="utf-8")
    _git(repository, "add", "tracked.py")
    _git(repository, "commit", "--quiet", "-m", "initial")

    clean = run_desktop_live._checkout_identity(repository)
    assert not clean.is_dirty
    assert clean.development_snapshot_sha256 is None

    tracked.write_text("VALUE = 2\n", encoding="utf-8")
    first_dirty = run_desktop_live._checkout_identity(repository)
    assert first_dirty.is_dirty
    assert len(first_dirty.development_snapshot_sha256 or "") == 64

    tracked.write_text("VALUE = 3\n", encoding="utf-8")
    second_dirty = run_desktop_live._checkout_identity(repository)
    assert (
        second_dirty.development_snapshot_sha256
        != first_dirty.development_snapshot_sha256
    )

    (repository / "new.txt").write_text("untracked content\n", encoding="utf-8")
    with_untracked = run_desktop_live._checkout_identity(repository)
    assert (
        with_untracked.development_snapshot_sha256
        != second_dirty.development_snapshot_sha256
    )


def test_dirty_checkout_guard_only_requires_prepare() -> None:
    source = Path("scripts/dev/run_desktop_live.py").read_text(encoding="utf-8")

    assert "commit or stash local changes" not in source
    assert "a dirty checkout must use --prepare" in source
    assert "development_snapshot_sha256" in source


def test_browser_npm_command_prefers_compatible_native_node() -> None:
    with patch.object(
        run_desktop_live,
        "_node_version",
        return_value=(22, 19, 0),
    ):
        assert run_desktop_live._browser_npm_command() == ["npm"]


def test_browser_npm_command_reuses_windows_node_from_wsl() -> None:
    def version_for(command: list[str]) -> tuple[int, int, int] | None:
        if command[0] == "node":
            return (18, 19, 1)
        return (22, 19, 0)

    with (
        patch.object(run_desktop_live, "_node_version", side_effect=version_for),
        patch.dict("os.environ", {"WSL_DISTRO_NAME": "Ubuntu"}),
    ):
        assert run_desktop_live._browser_npm_command() == [
            "cmd.exe",
            "/d",
            "/c",
            "npm",
        ]


def test_ensure_uv_on_path_adds_user_local_installation(tmp_path: Path) -> None:
    user_bin = tmp_path / ".local/bin"
    user_bin.mkdir(parents=True)
    uv = user_bin / "uv"
    uv.write_text("#!/bin/sh\n", encoding="utf-8")
    uv.chmod(0o755)

    with (
        patch.object(run_desktop_live.shutil, "which", return_value=None),
        patch.object(run_desktop_live.Path, "home", return_value=tmp_path),
        patch.dict(os.environ, {"PATH": "/usr/bin"}, clear=False),
    ):
        assert run_desktop_live._ensure_uv_on_path() == uv.resolve()
        assert os.environ["PATH"].split(os.pathsep)[0] == os.fspath(user_bin)


def test_ensure_cargo_on_path_adds_rustup_installation(tmp_path: Path) -> None:
    cargo_bin = tmp_path / ".cargo/bin"
    cargo_bin.mkdir(parents=True)
    cargo = cargo_bin / "cargo"
    cargo.write_text("#!/bin/sh\n", encoding="utf-8")
    cargo.chmod(0o755)

    with (
        patch.object(run_desktop_live.shutil, "which", return_value=None),
        patch.object(run_desktop_live.Path, "home", return_value=tmp_path),
        patch.dict(os.environ, {"PATH": "/usr/bin"}, clear=False),
    ):
        assert run_desktop_live._ensure_cargo_on_path() == cargo.resolve()
        assert os.environ["PATH"].split(os.pathsep)[0] == os.fspath(cargo_bin)
