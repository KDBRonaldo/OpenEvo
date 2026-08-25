from __future__ import annotations

from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
import os
from pathlib import Path
import shutil
import subprocess
import threading

import pytest

from scripts.release.launcher_distribution import build_launcher_distribution


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]


class _QuietHandler(SimpleHTTPRequestHandler):
    def log_message(self, _format: str, *_args: object) -> None:
        return


@pytest.fixture(scope="module")
def published_release(tmp_path_factory: pytest.TempPathFactory) -> tuple[Path, str]:
    root = tmp_path_factory.mktemp("online-installer-release")
    download_root = root / "releases" / "latest" / "download"
    download_root.mkdir(parents=True)
    archive = download_root / "openevo-launcher.tar.gz"
    receipt = build_launcher_distribution(REPOSITORY_ROOT, archive)
    (download_root / "openevo-launcher.tar.gz.sha256").write_text(
        f"{receipt.sha256}  {archive.name}\n",
        encoding="utf-8",
    )
    return root, receipt.distribution_id


def _serve(root: Path) -> tuple[ThreadingHTTPServer, threading.Thread, str]:
    handler = partial(_QuietHandler, directory=os.fspath(root))
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, thread, f"http://127.0.0.1:{server.server_address[1]}/releases"


@pytest.mark.skipif(
    any(shutil.which(command) is None for command in ("sh", "curl", "tar", "ssh")),
    reason="online installation requires POSIX sh, curl, tar, and system OpenSSH",
)
def test_online_installer_downloads_verifies_and_installs_without_repository(
    published_release: tuple[Path, str],
    tmp_path: Path,
) -> None:
    release_root, distribution_id = published_release
    server, thread, base_url = _serve(release_root)
    prefix = tmp_path / "prefix"
    environment = {
        **os.environ,
        "PYTHONPATH": "",
        "OPENEVO_RELEASE_BASE_URL": base_url,
    }
    try:
        result = subprocess.run(
            ["sh", os.fspath(REPOSITORY_ROOT / "install.sh"), "--prefix", os.fspath(prefix)],
            cwd=tmp_path,
            env=environment,
            check=False,
            capture_output=True,
            text=True,
            timeout=30,
        )
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)

    assert result.returncode == 0, result.stderr
    assert (prefix / "bin" / "openevo").is_file()
    assert (
        prefix / "share" / "openevo" / "active-launcher-v1"
    ).read_text(encoding="utf-8").strip() == distribution_id
    help_result = subprocess.run(
        [os.fspath(prefix / "bin" / "openevo"), "webui", "--help"],
        cwd=tmp_path,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert help_result.returncode == 0, help_result.stderr


@pytest.mark.skipif(
    any(shutil.which(command) is None for command in ("sh", "curl", "tar", "ssh")),
    reason="online installation requires POSIX sh, curl, tar, and system OpenSSH",
)
def test_online_installer_rejects_archive_that_does_not_match_checksum(
    published_release: tuple[Path, str],
    tmp_path: Path,
) -> None:
    release_root, _distribution_id = published_release
    tampered_root = tmp_path / "tampered-release"
    shutil.copytree(release_root, tampered_root)
    archive = (
        tampered_root
        / "releases"
        / "latest"
        / "download"
        / "openevo-launcher.tar.gz"
    )
    with archive.open("ab") as output:
        output.write(b"tampered")
    server, thread, base_url = _serve(tampered_root)
    try:
        result = subprocess.run(
            [
                "sh",
                os.fspath(REPOSITORY_ROOT / "install.sh"),
                "--prefix",
                os.fspath(tmp_path / "prefix"),
            ],
            cwd=tmp_path,
            env={
                **os.environ,
                "PYTHONPATH": "",
                "OPENEVO_RELEASE_BASE_URL": base_url,
            },
            check=False,
            capture_output=True,
            text=True,
            timeout=30,
        )
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)

    assert result.returncode == 21
    assert "checksum verification failed" in result.stderr
