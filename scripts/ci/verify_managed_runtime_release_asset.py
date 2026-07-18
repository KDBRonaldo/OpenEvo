#!/usr/bin/env python3
"""Verify the exact GitHub prerelease asset and downloaded archive bytes."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import sys

from openevo.runtime.managed import (
    MANAGED_RUNTIME_ARCHIVE_RELEASE,
    verify_managed_runtime_archive,
)


class AssetVerificationError(RuntimeError):
    pass


def _load_json(path: Path) -> dict[str, object]:
    try:
        payload = json.loads(
            path.read_text(encoding="utf-8"),
            object_pairs_hook=_closed_json_object,
        )
    except (
        OSError,
        RecursionError,
        UnicodeDecodeError,
        json.JSONDecodeError,
        ValueError,
    ) as exc:
        raise AssetVerificationError(f"GitHub API metadata is unreadable: {path.name}") from exc
    if not isinstance(payload, dict):
        raise AssetVerificationError(f"GitHub API metadata is invalid: {path.name}")
    return payload


def _closed_json_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
    except OSError as exc:
        raise AssetVerificationError("managed runtime downloaded bytes are unreadable") from exc
    return digest.hexdigest()


def _asset_identity(payload: object) -> dict[str, object]:
    if not isinstance(payload, dict):
        raise AssetVerificationError("GitHub release asset metadata is invalid")
    return {
        "api_digest": payload.get("digest"),
        "byte_size": payload.get("size"),
        "id": payload.get("id"),
        "name": payload.get("name"),
        "state": payload.get("state"),
    }


def expected_source_evidence() -> dict[str, object]:
    release = MANAGED_RUNTIME_ARCHIVE_RELEASE
    return {
        "asset": {
            "api_digest": release.asset_api_digest,
            "byte_size": release.byte_size,
            "download_sha256": release.sha256,
            "id": release.asset_id,
            "name": release.filename,
        },
        "image": {
            "config_id": release.config_id,
            "oci_index_digest": release.oci_index_id,
        },
        "release": {
            "id": release.asset_release_id,
            "is_draft": False,
            "is_prerelease": True,
            "tag": release.asset_release_tag,
        },
        "repository": "CompLifeLab-ZJU/OpenEvo",
        "schema_version": 1,
    }


def verify_release_asset(
    release_json: Path,
    asset_json: Path,
    archive: Path,
    evidence_out: Path,
) -> None:
    expected = MANAGED_RUNTIME_ARCHIVE_RELEASE
    release_payload = _load_json(release_json)
    asset_payload = _load_json(asset_json)
    release_assets = release_payload.get("assets")
    expected_api_asset = {
        "api_digest": expected.asset_api_digest,
        "byte_size": expected.byte_size,
        "id": expected.asset_id,
        "name": expected.filename,
        "state": "uploaded",
    }
    if (
        release_payload.get("id") != expected.asset_release_id
        or release_payload.get("tag_name") != expected.asset_release_tag
        or release_payload.get("draft") is not False
        or release_payload.get("prerelease") is not True
        or not isinstance(release_assets, list)
        or len(release_assets) != 1
        or _asset_identity(release_assets[0]) != expected_api_asset
        or _asset_identity(asset_payload) != expected_api_asset
    ):
        raise AssetVerificationError("GitHub release/asset API identity is invalid")
    try:
        size = archive.stat().st_size
    except OSError as exc:
        raise AssetVerificationError("managed runtime downloaded bytes are unreadable") from exc
    digest = _sha256(archive)
    if (
        archive.name != expected.filename
        or size != expected.byte_size
        or digest != expected.sha256
    ):
        raise AssetVerificationError("managed runtime downloaded bytes do not match the API asset")
    verify_managed_runtime_archive(archive, release=expected)
    payload = expected_source_evidence()
    encoded = (json.dumps(payload, separators=(",", ":"), sort_keys=True) + "\n").encode()
    try:
        evidence_out.write_bytes(encoded)
    except OSError as exc:
        raise AssetVerificationError(
            "managed runtime source evidence could not be written"
        ) from exc


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("release_json", type=Path)
    parser.add_argument("asset_json", type=Path)
    parser.add_argument("archive", type=Path)
    parser.add_argument("--evidence-out", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        verify_release_asset(
            args.release_json,
            args.asset_json,
            args.archive,
            args.evidence_out,
        )
    except (AssetVerificationError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
