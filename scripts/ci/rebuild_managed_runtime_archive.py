#!/usr/bin/env python3
"""Rebuild a managed-runtime OCI archive without tag or attestation references."""

from __future__ import annotations

import argparse
from dataclasses import replace
from io import BytesIO
import gzip
import hashlib
import json
import os
from pathlib import Path
import tarfile
from typing import NamedTuple

from openevo.runtime.managed import (
    MANAGED_RUNTIME_ARCHIVE_RELEASE,
    verify_managed_runtime_archive,
)


OCI_INDEX = "application/vnd.oci.image.index.v1+json"
OCI_MANIFEST = "application/vnd.oci.image.manifest.v1+json"


class RebuildError(RuntimeError):
    pass


class RebuiltArchiveIdentity(NamedTuple):
    archive_sha256: str
    archive_size: int
    config_id: str
    oci_index_id: str
    retained_blob_count: int


def _canonical_json(value: object) -> bytes:
    return (json.dumps(value, separators=(",", ":"), sort_keys=True) + "\n").encode()


def _digest(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _blob_name(digest: str) -> str:
    if (
        not digest.startswith("sha256:")
        or len(digest) != 71
        or any(character not in "0123456789abcdef" for character in digest[7:])
    ):
        raise RebuildError("OCI descriptor digest is invalid")
    return "blobs/sha256/" + digest[7:]


def _load_json(archive: tarfile.TarFile, name: str) -> object:
    try:
        member = archive.getmember(name)
        stream = archive.extractfile(member)
        if stream is None or member.size > 1024 * 1024:
            raise RebuildError("OCI metadata is unavailable")
        payload = stream.read(member.size + 1)
        if not member.isfile() or len(payload) != member.size or stream.read(1):
            raise RebuildError("OCI metadata is unavailable")
        return json.loads(
            payload.decode("utf-8"),
            object_pairs_hook=_closed_json_object,
        )
    except (
        KeyError,
        RecursionError,
        UnicodeDecodeError,
        json.JSONDecodeError,
        tarfile.TarError,
        ValueError,
    ) as exc:
        raise RebuildError(f"OCI metadata is invalid: {name}") from exc


def _closed_json_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _without_annotations(value: object) -> object:
    if isinstance(value, dict):
        return {
            key: _without_annotations(child)
            for key, child in value.items()
            if key != "annotations"
        }
    if isinstance(value, list):
        return [_without_annotations(child) for child in value]
    return value


def _normalized_file(name: str, size: int) -> tarfile.TarInfo:
    member = tarfile.TarInfo(name)
    member.mode = 0o644
    member.mtime = 0
    member.size = size
    member.uid = 0
    member.gid = 0
    return member


def _normalized_directory(name: str) -> tarfile.TarInfo:
    member = tarfile.TarInfo(name)
    member.type = tarfile.DIRTYPE
    member.mode = 0o755
    member.mtime = 0
    member.uid = 0
    member.gid = 0
    return member


def rebuild_archive(source: Path, destination: Path) -> RebuiltArchiveIdentity:
    if destination.exists():
        raise RebuildError("refusing to replace an existing rebuilt archive")
    try:
        with tarfile.open(source, "r:gz") as archive:
            root_index = _load_json(archive, "index.json")
            legacy_manifest = _load_json(archive, "manifest.json")
            oci_layout = _load_json(archive, "oci-layout")
            if (
                not isinstance(root_index, dict)
                or root_index.get("mediaType") != OCI_INDEX
                or root_index.get("schemaVersion") != 2
                or not isinstance(root_index.get("manifests"), list)
                or len(root_index["manifests"]) != 1
                or oci_layout != {"imageLayoutVersion": "1.0.0"}
                or not isinstance(legacy_manifest, list)
                or len(legacy_manifest) != 1
            ):
                raise RebuildError("source archive layout is invalid")
            old_index_name = _blob_name(root_index["manifests"][0].get("digest", ""))
            image_index = _load_json(archive, old_index_name)
            if not isinstance(image_index, dict) or not isinstance(
                image_index.get("manifests"), list
            ):
                raise RebuildError("source image index is invalid")
            runtime_descriptors = [
                descriptor
                for descriptor in image_index["manifests"]
                if isinstance(descriptor, dict)
                and descriptor.get("mediaType") == OCI_MANIFEST
                and descriptor.get("platform") == {"architecture": "amd64", "os": "linux"}
            ]
            if len(runtime_descriptors) != 1:
                raise RebuildError("source image index does not select one linux/amd64 image")
            runtime_descriptor = _without_annotations(runtime_descriptors[0])
            assert isinstance(runtime_descriptor, dict)
            old_manifest_name = _blob_name(str(runtime_descriptor.get("digest", "")))
            image_manifest = _without_annotations(_load_json(archive, old_manifest_name))
            if (
                not isinstance(image_manifest, dict)
                or image_manifest.get("mediaType") != OCI_MANIFEST
                or image_manifest.get("schemaVersion") != 2
                or not isinstance(image_manifest.get("config"), dict)
                or not isinstance(image_manifest.get("layers"), list)
            ):
                raise RebuildError("source image manifest is invalid")
            config = image_manifest["config"]
            layers = image_manifest["layers"]
            assert isinstance(config, dict)
            config_name = _blob_name(str(config.get("digest", "")))
            layer_names = [
                _blob_name(str(layer.get("digest", "")))
                for layer in layers
                if isinstance(layer, dict)
            ]
            if len(layer_names) != len(layers):
                raise RebuildError("source image layers are invalid")
            legacy = legacy_manifest[0]
            if (
                not isinstance(legacy, dict)
                or legacy.get("Config") != config_name
                or legacy.get("Layers") != layer_names
                or legacy.get("RepoTags") not in (None, [])
            ):
                raise RebuildError("legacy Docker manifest does not bind the runtime graph")
            source_members = {member.name: member for member in archive.getmembers()}
            original_names = {config_name, *layer_names}
            for descriptor, name in [
                (config, config_name),
                *zip(layers, layer_names, strict=True),
            ]:
                if (
                    name not in source_members
                    or type(descriptor.get("size")) is not int
                    or source_members[name].size != descriptor["size"]
                ):
                    raise RebuildError("source descriptor size does not bind its blob")

            image_manifest_payload = _canonical_json(image_manifest)
            image_manifest_digest = _digest(image_manifest_payload)
            rebuilt_descriptor = {
                "digest": "sha256:" + image_manifest_digest,
                "mediaType": OCI_MANIFEST,
                "platform": {"architecture": "amd64", "os": "linux"},
                "size": len(image_manifest_payload),
            }
            image_index_payload = _canonical_json(
                {
                    "manifests": [rebuilt_descriptor],
                    "mediaType": OCI_INDEX,
                    "schemaVersion": 2,
                }
            )
            image_index_digest = _digest(image_index_payload)
            root_index_payload = _canonical_json(
                {
                    "manifests": [
                        {
                            "digest": "sha256:" + image_index_digest,
                            "mediaType": OCI_INDEX,
                            "size": len(image_index_payload),
                        }
                    ],
                    "mediaType": OCI_INDEX,
                    "schemaVersion": 2,
                }
            )
            generated = {
                "blobs/sha256/" + image_manifest_digest: image_manifest_payload,
                "blobs/sha256/" + image_index_digest: image_index_payload,
                "index.json": root_index_payload,
                "manifest.json": _canonical_json(legacy_manifest),
                "oci-layout": _canonical_json(oci_layout),
            }

        destination.parent.mkdir(parents=True, exist_ok=True)
        with destination.open("xb") as output:
            with gzip.GzipFile(fileobj=output, mode="wb", filename="", mtime=0) as compressed:
                with tarfile.open(
                    fileobj=compressed, mode="w", format=tarfile.PAX_FORMAT
                ) as rebuilt:
                    rebuilt.addfile(_normalized_directory("blobs"))
                    rebuilt.addfile(_normalized_directory("blobs/sha256"))
                    with tarfile.open(source, "r:gz") as archive:
                        for member in archive:
                            if member.name not in original_names:
                                continue
                            stream = archive.extractfile(member)
                            if stream is None:
                                raise RebuildError("source runtime blob is unreadable")
                            rebuilt.addfile(_normalized_file(member.name, member.size), stream)
                    for name, payload in sorted(generated.items()):
                        rebuilt.addfile(_normalized_file(name, len(payload)), BytesIO(payload))
        os.chmod(destination, 0o600)
        archive_size = destination.stat().st_size
        archive_digest = _file_sha256(destination)
        identity = RebuiltArchiveIdentity(
            archive_sha256=archive_digest,
            archive_size=archive_size,
            config_id=str(config["digest"]),
            oci_index_id="sha256:" + image_index_digest,
            retained_blob_count=len(original_names) + 2,
        )
        verify_managed_runtime_archive(
            destination,
            release=replace(
                MANAGED_RUNTIME_ARCHIVE_RELEASE,
                filename=destination.name,
                sha256=identity.archive_sha256,
                asset_api_digest="sha256:" + identity.archive_sha256,
                byte_size=identity.archive_size,
                config_id=identity.config_id,
                oci_index_id=identity.oci_index_id,
            ),
        )
        return identity
    except (OSError, tarfile.TarError) as exc:
        raise RebuildError("managed runtime archive rebuild failed") from exc


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path)
    parser.add_argument("destination", type=Path)
    args = parser.parse_args()
    identity = rebuild_archive(args.source, args.destination)
    print(json.dumps(identity._asdict(), separators=(",", ":"), sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
