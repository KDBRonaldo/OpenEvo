from __future__ import annotations

from pathlib import Path
import shutil
import zipfile

import pytest

from openevo.release_bundle import (
    ReleaseBundleError,
    build_release_bundle,
    verify_release_bundle,
)


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]


def test_builds_deterministic_runtime_only_release_bundle(tmp_path: Path) -> None:
    first = build_release_bundle(REPOSITORY_ROOT, tmp_path / "first.oevobundle")
    second = build_release_bundle(REPOSITORY_ROOT, tmp_path / "second.oevobundle")

    assert first.release_id == second.release_id
    assert first.sha256 == second.sha256
    assert first.source_commit == second.source_commit
    assert first.file_count > 10
    with zipfile.ZipFile(first.path) as archive:
        names = set(archive.namelist())
    assert "manifest.json" in names
    assert "payload/LICENSE" in names
    assert "payload/src/openevo/daemon/product_app.py" in names
    assert "payload/src/openevo/web_gateway/static/index.html" in names
    assert "payload/desktop/sidecar/contracts/v2/app.py" in names
    assert not any(name.startswith("payload/tests/") for name in names)
    assert not any(name.startswith("payload/desktop/src/") for name in names)
    assert not any(name.startswith("payload/.git/") for name in names)


def test_verifier_rejects_duplicate_archive_entries(tmp_path: Path) -> None:
    receipt = build_release_bundle(REPOSITORY_ROOT, tmp_path / "valid.oevobundle")
    tampered = tmp_path / "tampered.oevobundle"
    shutil.copyfile(receipt.path, tampered)
    with pytest.warns(UserWarning, match="Duplicate name"):
        with zipfile.ZipFile(tampered, "a") as archive:
            archive.writestr("payload/README.md", b"replacement")

    with pytest.raises(ReleaseBundleError, match="duplicate entries"):
        verify_release_bundle(tampered)


def test_verifier_rejects_payload_digest_changes(tmp_path: Path) -> None:
    receipt = build_release_bundle(REPOSITORY_ROOT, tmp_path / "valid.oevobundle")
    tampered = tmp_path / "tampered.oevobundle"
    with zipfile.ZipFile(receipt.path, "r") as source, zipfile.ZipFile(tampered, "w") as target:
        for info in source.infolist():
            data = source.read(info)
            if info.filename == "payload/README.md":
                data += b"\ntampered\n"
            target.writestr(info, data)

    with pytest.raises(ReleaseBundleError, match="size mismatch"):
        verify_release_bundle(tampered)
