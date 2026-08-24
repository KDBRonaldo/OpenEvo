from __future__ import annotations

import os
from pathlib import Path
import shutil
import subprocess

import pytest

from openevo import launcher
from openevo.release_bundle import build_release_bundle


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.skipif(shutil.which("sh") is None, reason="remote installer requires POSIX sh")
def test_remote_release_installer_is_idempotent_and_detects_changes(tmp_path: Path) -> None:
    receipt = build_release_bundle(REPOSITORY_ROOT, tmp_path / "release.oevobundle")
    home = tmp_path / "home"
    incoming = home / ".openevo" / "dev-agent" / "incoming"
    incoming.mkdir(parents=True)
    remote_archive = incoming / f"release-{receipt.release_id}.oevobundle"
    shutil.copyfile(receipt.path, remote_archive)
    environment = {**os.environ, "HOME": os.fspath(home)}

    install = subprocess.run(
        ["sh", "-c", launcher.build_remote_release_install_script(receipt, archive_uploaded=True)],
        check=False,
        capture_output=True,
        text=True,
        env=environment,
    )

    assert install.returncode == 0, install.stderr
    release_root = home / ".openevo" / "dev-agent" / "releases" / receipt.release_id
    assert (release_root / "payload" / "src" / "openevo" / "daemon" / "product_app.py").is_file()
    assert not remote_archive.exists()
    assert (home / ".openevo" / "dev-agent" / "active-release-v1").read_text().strip() == receipt.release_id

    verify_again = subprocess.run(
        ["sh", "-c", launcher.build_remote_release_install_script(receipt, archive_uploaded=False)],
        check=False,
        capture_output=True,
        text=True,
        env=environment,
    )
    assert verify_again.returncode == 0, verify_again.stderr

    extra = release_root / "payload" / "unexpected.py"
    extra.write_text("raise RuntimeError", encoding="utf-8")
    detects_extra = subprocess.run(
        ["sh", "-c", launcher.build_remote_release_install_script(receipt, archive_uploaded=False)],
        check=False,
        capture_output=True,
        text=True,
        env=environment,
    )
    assert detects_extra.returncode != 0
    assert "installed release contains an extra file" in detects_extra.stderr
    extra.unlink()

    installed = release_root / "payload" / "README.md"
    installed.write_text("changed", encoding="utf-8")
    detects_change = subprocess.run(
        ["sh", "-c", launcher.build_remote_release_install_script(receipt, archive_uploaded=False)],
        check=False,
        capture_output=True,
        text=True,
        env=environment,
    )
    assert detects_change.returncode != 0
    assert "installed release file changed" in detects_change.stderr
