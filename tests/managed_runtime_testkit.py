from __future__ import annotations

from io import BytesIO
import hashlib
import json
from pathlib import Path
import tarfile

from openevo.runtime.managed import ManagedRuntimeArchiveRelease


RUNTIME_ASSET_TAG = "openevo-managed-runtime-assets-v0.1.0"
RUNTIME_FILENAME = "openevo-science-runtime-0.1.0-linux-amd64.tar.gz"
RUNTIME_RELEASE_ID = 354404740
RUNTIME_ASSET_ID = 478167627
RUNTIME_ALIASES = ("openevo/science-runtime:0.1.0",)


def write_test_managed_runtime_archive(
    path: Path,
    *,
    os_name: str = "linux",
    architecture: str = "amd64",
    managed_label: str | None = "true",
    config_reference_digest: str | None = None,
    repo_tags: tuple[str, ...] | None = (),
    index_reference: str | None = None,
    manifest_reference: str | None = None,
    include_attestation: bool = False,
    duplicate_root_index_key: bool = False,
) -> ManagedRuntimeArchiveRelease:
    labels = {} if managed_label is None else {"io.openevo.managed-runtime": managed_label}
    config = _canonical_json(
        {
            "architecture": architecture,
            "config": {"Labels": labels},
            "os": os_name,
            "rootfs": {"diff_ids": ["sha256:" + "3" * 64], "type": "layers"},
        }
    )
    config_digest = hashlib.sha256(config).hexdigest()
    referenced_digest = config_reference_digest or config_digest
    layer = b"test-layer"
    layer_digest = hashlib.sha256(layer).hexdigest()
    manifest = _canonical_json(
        [
            {
                "Config": f"blobs/sha256/{referenced_digest}",
                "Layers": [f"blobs/sha256/{layer_digest}"],
                "RepoTags": None if repo_tags is None else list(repo_tags),
            }
        ]
    )
    image_manifest = _canonical_json(
        {
            "config": {
                "digest": "sha256:" + config_digest,
                "mediaType": "application/vnd.oci.image.config.v1+json",
                "size": len(config),
            },
            "layers": [
                {
                    "digest": "sha256:" + layer_digest,
                    "mediaType": "application/vnd.oci.image.layer.v1.tar",
                    "size": len(layer),
                }
            ],
            "mediaType": "application/vnd.oci.image.manifest.v1+json",
            "schemaVersion": 2,
        }
    )
    image_manifest_digest = hashlib.sha256(image_manifest).hexdigest()
    image_descriptor = {
        "digest": "sha256:" + image_manifest_digest,
        "mediaType": "application/vnd.oci.image.manifest.v1+json",
        "platform": {"architecture": architecture, "os": os_name},
        "size": len(image_manifest),
    }
    if manifest_reference is not None:
        image_descriptor["annotations"] = {
            "io.containerd.image.name": manifest_reference,
            "org.opencontainers.image.ref.name": "0.1.0",
        }
    image_descriptors = [image_descriptor]
    attestation = _canonical_json({"schemaVersion": 2})
    attestation_digest = hashlib.sha256(attestation).hexdigest()
    if include_attestation:
        image_descriptors.append(
            {
                "annotations": {
                    "vnd.docker.reference.digest": "sha256:" + image_manifest_digest,
                    "vnd.docker.reference.type": "attestation-manifest",
                },
                "digest": "sha256:" + attestation_digest,
                "mediaType": "application/vnd.oci.image.manifest.v1+json",
                "platform": {"architecture": "unknown", "os": "unknown"},
                "size": len(attestation),
            }
        )
    image_index = _canonical_json(
        {
            "manifests": image_descriptors,
            "mediaType": "application/vnd.oci.image.index.v1+json",
            "schemaVersion": 2,
        }
    )
    image_index_digest = hashlib.sha256(image_index).hexdigest()
    descriptor = {
        "digest": "sha256:" + image_index_digest,
        "mediaType": "application/vnd.oci.image.index.v1+json",
        "size": len(image_index),
    }
    if index_reference is not None:
        descriptor["annotations"] = {
            "io.containerd.image.name": index_reference,
            "org.opencontainers.image.ref.name": "0.1.0",
        }
    index = _canonical_json(
        {
            "manifests": [descriptor],
            "mediaType": "application/vnd.oci.image.index.v1+json",
            "schemaVersion": 2,
        }
    )
    if duplicate_root_index_key:
        index = index.replace(
            b'"schemaVersion":2}',
            b'"schemaVersion":2,"schemaVersion":2}',
        )
    oci_layout = _canonical_json({"imageLayoutVersion": "1.0.0"})
    buffer = BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz", format=tarfile.PAX_FORMAT) as archive:
        _add_directory(archive, "blobs")
        _add_directory(archive, "blobs/sha256")
        _add_file(archive, f"blobs/sha256/{config_digest}", config)
        _add_file(archive, f"blobs/sha256/{image_manifest_digest}", image_manifest)
        _add_file(archive, f"blobs/sha256/{image_index_digest}", image_index)
        if include_attestation:
            _add_file(archive, f"blobs/sha256/{attestation_digest}", attestation)
        _add_file(archive, f"blobs/sha256/{layer_digest}", layer)
        _add_file(archive, "index.json", index)
        _add_file(archive, "manifest.json", manifest)
        _add_file(archive, "oci-layout", oci_layout)
    payload = buffer.getvalue()
    path.write_bytes(payload)
    path.chmod(0o600)
    return ManagedRuntimeArchiveRelease(
        asset_release_id=RUNTIME_RELEASE_ID,
        asset_release_tag=RUNTIME_ASSET_TAG,
        asset_id=RUNTIME_ASSET_ID,
        asset_api_digest="sha256:" + hashlib.sha256(payload).hexdigest(),
        filename=path.name,
        sha256=hashlib.sha256(payload).hexdigest(),
        byte_size=len(payload),
        platform="linux-amd64",
        config_id="sha256:" + config_digest,
        oci_index_id="sha256:" + image_index_digest,
        aliases=RUNTIME_ALIASES,
    )


def _canonical_json(value: object) -> bytes:
    return (json.dumps(value, separators=(",", ":"), sort_keys=True) + "\n").encode()


def _add_directory(archive: tarfile.TarFile, name: str) -> None:
    member = tarfile.TarInfo(name)
    member.type = tarfile.DIRTYPE
    member.mode = 0o755
    member.mtime = 0
    archive.addfile(member)


def _add_file(archive: tarfile.TarFile, name: str, payload: bytes) -> None:
    member = tarfile.TarInfo(name)
    member.mode = 0o644
    member.mtime = 0
    member.size = len(payload)
    archive.addfile(member, BytesIO(payload))
