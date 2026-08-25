from __future__ import annotations

import os
from pathlib import Path
import shutil
import subprocess
import tarfile

import pytest

from scripts.release.launcher_distribution import (
    LAUNCHER_DISTRIBUTION_ROOT,
    build_launcher_distribution,
    verify_launcher_distribution,
)


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture(scope="module")
def built_distributions(
    tmp_path_factory: pytest.TempPathFactory,
) -> tuple[Path, Path]:
    output_root = tmp_path_factory.mktemp("launcher-distributions")
    first = output_root / "first.tar.gz"
    second = output_root / "second.tar.gz"
    build_launcher_distribution(REPOSITORY_ROOT, first)
    build_launcher_distribution(REPOSITORY_ROOT, second)
    return first, second


def _extract(archive_path: Path, destination: Path) -> Path:
    destination.mkdir()
    with tarfile.open(archive_path, "r:gz") as archive:
        archive.extractall(destination)
    return destination / LAUNCHER_DISTRIBUTION_ROOT


def test_launcher_distribution_is_deterministic_and_runtime_complete(
    built_distributions: tuple[Path, Path],
) -> None:
    first_path, second_path = built_distributions
    first = verify_launcher_distribution(first_path)
    second = verify_launcher_distribution(second_path)

    assert first.distribution_id == second.distribution_id
    assert first.server_release_id == second.server_release_id
    assert first.sha256 == second.sha256
    assert first.file_count == 5
    with tarfile.open(first_path, "r:gz") as archive:
        names = set(archive.getnames())
    assert names == {
        f"{LAUNCHER_DISTRIBUTION_ROOT}/LICENSE",
        f"{LAUNCHER_DISTRIBUTION_ROOT}/README.txt",
        f"{LAUNCHER_DISTRIBUTION_ROOT}/install.sh",
        f"{LAUNCHER_DISTRIBUTION_ROOT}/manifest.json",
        f"{LAUNCHER_DISTRIBUTION_ROOT}/openevo.pyz",
        f"{LAUNCHER_DISTRIBUTION_ROOT}/openevo-server.oevobundle",
    }


@pytest.mark.skipif(
    shutil.which("sh") is None or shutil.which("ssh") is None,
    reason="ordinary-user installation requires POSIX sh and system OpenSSH",
)
def test_installer_runs_without_repository_and_is_idempotent(
    built_distributions: tuple[Path, Path],
    tmp_path: Path,
) -> None:
    package_root = _extract(built_distributions[0], tmp_path / "extracted")
    prefix = tmp_path / "user-prefix"
    environment = {**os.environ, "PYTHONPATH": ""}
    command = ["sh", os.fspath(package_root / "install.sh"), "--prefix", os.fspath(prefix)]

    first_install = subprocess.run(
        command,
        cwd=tmp_path,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
    )
    second_install = subprocess.run(
        command,
        cwd=tmp_path,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
    )

    assert first_install.returncode == 0, first_install.stderr
    assert second_install.returncode == 0, second_install.stderr
    wrapper = prefix / "bin" / "openevo"
    assert wrapper.is_file()
    wrapper_text = wrapper.read_text(encoding="utf-8")
    assert '--release-bundle "$release_root/openevo-server.oevobundle"' in wrapper_text
    active_id = (prefix / "share" / "openevo" / "active-launcher-v1").read_text().strip()
    installed = prefix / "share" / "openevo" / "releases" / active_id
    assert (installed / "openevo.pyz").is_file()
    assert (installed / "openevo-server.oevobundle").is_file()

    help_result = subprocess.run(
        [os.fspath(wrapper), "webui", "--help"],
        cwd=tmp_path,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
    )
    assert help_result.returncode == 0, help_result.stderr
    assert "--release-bundle" in help_result.stdout
    assert "--ssh-alias" in help_result.stdout


@pytest.mark.skipif(
    shutil.which("sh") is None or shutil.which("ssh") is None,
    reason="ordinary-user installation requires POSIX sh and system OpenSSH",
)
def test_installer_rejects_tampered_launcher_bytes(
    built_distributions: tuple[Path, Path],
    tmp_path: Path,
) -> None:
    package_root = _extract(built_distributions[0], tmp_path / "tampered")
    with (package_root / "openevo.pyz").open("ab") as output:
        output.write(b"tampered")

    result = subprocess.run(
        [
            "sh",
            os.fspath(package_root / "install.sh"),
            "--prefix",
            os.fspath(tmp_path / "prefix"),
        ],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode != 0
    assert "launcher file size changed" in result.stderr


@pytest.mark.skipif(
    shutil.which("sh") is None or shutil.which("ssh") is None,
    reason="ordinary-user installation requires POSIX sh and system OpenSSH",
)
def test_installer_does_not_replace_an_unmanaged_command(
    built_distributions: tuple[Path, Path],
    tmp_path: Path,
) -> None:
    package_root = _extract(built_distributions[0], tmp_path / "unmanaged")
    prefix = tmp_path / "prefix"
    command_path = prefix / "bin" / "openevo"
    command_path.parent.mkdir(parents=True)
    command_path.write_text("user-owned command\n", encoding="utf-8")

    result = subprocess.run(
        [
            "sh",
            os.fspath(package_root / "install.sh"),
            "--prefix",
            os.fspath(prefix),
        ],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode != 0
    assert "refusing to replace unmanaged command" in result.stderr
    assert command_path.read_text(encoding="utf-8") == "user-owned command\n"
