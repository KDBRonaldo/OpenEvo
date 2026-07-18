"""Core-owned runtime identities for managed Science execution."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat
import tarfile
from typing import Final, Literal, TypeAlias


ManagedRuntimeProfile: TypeAlias = Literal["managed_science", "python_research"]

MANAGED_RUNTIME_IMAGES: Final[dict[ManagedRuntimeProfile, str]] = {
    "managed_science": "openevo/science-runtime:0.1.1",
    "python_research": "openevo/python-research-runtime:0.1.1",
}
_SHA256_DIGEST_RE: Final[re.Pattern[str]] = re.compile(r"^sha256:[0-9a-f]{64}$")
_SHA256_RE: Final[re.Pattern[str]] = re.compile(r"^[0-9a-f]{64}$")
MANAGED_RUNTIME_LABEL: Final[str] = "io.openevo.managed-runtime"
MANAGED_RUNTIME_LABEL_VALUE: Final[str] = "true"
_MAX_ARCHIVE_MEMBERS: Final[int] = 64
_MAX_ARCHIVE_DECLARED_BYTES: Final[int] = 512 * 1024 * 1024
_MAX_ARCHIVE_MEMBER_BYTES: Final[int] = 256 * 1024 * 1024
_MAX_ARCHIVE_METADATA_BYTES: Final[int] = 4 * 1024 * 1024
_MAX_MANIFEST_BYTES: Final[int] = 64 * 1024
_MAX_CONFIG_BYTES: Final[int] = 1024 * 1024


class ManagedRuntimeArchiveVerificationError(ValueError):
    """Renderer-safe failure for an untrusted managed runtime archive."""


@dataclass(frozen=True, slots=True)
class ManagedRuntimeArchiveAuthority:
    config_id: str
    oci_index_id: str
    platform: Literal["linux-amd64"]
    managed_label: bool

    def __post_init__(self) -> None:
        if (
            not isinstance(self.config_id, str)
            or _SHA256_DIGEST_RE.fullmatch(self.config_id) is None
            or not isinstance(self.oci_index_id, str)
            or _SHA256_DIGEST_RE.fullmatch(self.oci_index_id) is None
            or self.config_id == self.oci_index_id
            or self.platform != "linux-amd64"
            or self.managed_label is not True
        ):
            raise ValueError("managed runtime archive authority is invalid")


@dataclass(frozen=True, slots=True)
class ManagedRuntimeArchiveRelease:
    """Core-owned offline archive and loaded-image identity for one release."""

    asset_release_id: int
    asset_release_tag: str
    asset_id: int
    asset_api_digest: str
    filename: str
    sha256: str
    byte_size: int
    platform: Literal["linux-amd64"]
    config_id: str
    oci_index_id: str
    aliases: tuple[str, ...]

    def __post_init__(self) -> None:
        if (
            type(self.asset_release_id) is not int
            or self.asset_release_id <= 0
            or not isinstance(self.asset_release_tag, str)
            or re.fullmatch(r"[A-Za-z0-9._-]{1,128}", self.asset_release_tag) is None
            or not isinstance(self.filename, str)
            or Path(self.filename).name != self.filename
            or not self.filename.endswith(".tar.gz")
            or type(self.asset_id) is not int
            or self.asset_id <= 0
            or not isinstance(self.asset_api_digest, str)
            or not isinstance(self.sha256, str)
            or self.asset_api_digest != "sha256:" + self.sha256
            or _SHA256_RE.fullmatch(self.sha256) is None
            or type(self.byte_size) is not int
            or not 0 < self.byte_size <= _MAX_ARCHIVE_DECLARED_BYTES
            or self.platform != "linux-amd64"
            or not isinstance(self.config_id, str)
            or _SHA256_DIGEST_RE.fullmatch(self.config_id) is None
            or not isinstance(self.oci_index_id, str)
            or _SHA256_DIGEST_RE.fullmatch(self.oci_index_id) is None
            or self.config_id == self.oci_index_id
            or not isinstance(self.aliases, tuple)
            or self.aliases != (MANAGED_RUNTIME_IMAGES["managed_science"],)
            or any(
                not isinstance(alias, str)
                or not alias
                or "@" in alias
                or alias.rfind(":") <= alias.rfind("/")
                for alias in self.aliases
            )
        ):
            raise ValueError("managed runtime archive release identity is invalid")


@dataclass(frozen=True, slots=True)
class ManagedRuntimeImageRelease:
    """Core-owned immutable image identity shipped by a release."""

    image: str
    trusted_digest: str
    loaded_image_id: str

    def __post_init__(self) -> None:
        if not isinstance(self.image, str) or "@" in self.image or not self.image.strip():
            raise ValueError("managed runtime image alias must be a non-empty tag")
        if (
            not isinstance(self.trusted_digest, str)
            or _SHA256_DIGEST_RE.fullmatch(self.trusted_digest) is None
        ):
            raise ValueError("managed runtime release digest must be a full sha256")
        if (
            not isinstance(self.loaded_image_id, str)
            or _SHA256_DIGEST_RE.fullmatch(self.loaded_image_id) is None
        ):
            raise ValueError("managed runtime loaded image ID must be a full sha256")

    @property
    def repository(self) -> str:
        final_slash = self.image.rfind("/")
        final_colon = self.image.rfind(":")
        if final_colon <= final_slash:
            raise ValueError("managed runtime image alias must include an explicit tag")
        return self.image[:final_colon]

    @property
    def immutable_reference(self) -> str:
        return f"{self.repository}@{self.trusted_digest}"


@dataclass(frozen=True, slots=True)
class ManagedCredentialMount:
    """Identity-bound host authority adopted by a managed runtime."""

    root: Path
    root_identity: tuple[int, int, int]
    auth_identity: tuple[int, int, int, int, int, int, int, int]


# The profile aliases remain internal compiler/runtime wiring. Trust comes only
# from these full release digests; a tag is never accepted as image identity.
MANAGED_RUNTIME_RELEASES: Final[dict[ManagedRuntimeProfile, ManagedRuntimeImageRelease]] = {
    profile: ManagedRuntimeImageRelease(
        image=image,
        trusted_digest=("sha256:af67c6b8c9cb0debd3a29addc23f518a680369ad53ec5347a829ef7318529c5c"),
        loaded_image_id=(
            "sha256:7a0079f9cb1bce5768cff5bce3d1181811c6a231ad800cac8fb503d66852c81b"
        ),
    )
    for profile, image in MANAGED_RUNTIME_IMAGES.items()
}
MANAGED_RUNTIME_ARCHIVE_RELEASE: Final[ManagedRuntimeArchiveRelease] = (
    ManagedRuntimeArchiveRelease(
        asset_release_id=356072935,
        asset_release_tag="openevo-managed-runtime-assets-v0.1.1",
        asset_id=481361975,
        asset_api_digest=(
            "sha256:ad9c5ebd69b5785b94dd52dc077d93ababfa9cf8cbcbf92940f60bee48a91149"
        ),
        filename="openevo-science-runtime-0.1.1-linux-amd64.tar.gz",
        sha256="ad9c5ebd69b5785b94dd52dc077d93ababfa9cf8cbcbf92940f60bee48a91149",
        byte_size=352_236_726,
        platform="linux-amd64",
        config_id=("sha256:0e5783e7839fe06d2df14d7a431c90f0982ca2099ef33bfa4c9e5933149bf5f2"),
        oci_index_id=("sha256:7a0079f9cb1bce5768cff5bce3d1181811c6a231ad800cac8fb503d66852c81b"),
        aliases=(MANAGED_RUNTIME_IMAGES["managed_science"],),
    )
)


def verify_managed_runtime_archive(
    archive: Path | str,
    *,
    release: ManagedRuntimeArchiveRelease | None = None,
) -> ManagedRuntimeArchiveAuthority:
    """Verify the sealed outer archive and its Docker/OCI config authority."""

    expected = MANAGED_RUNTIME_ARCHIVE_RELEASE if release is None else release
    try:
        if not isinstance(expected, ManagedRuntimeArchiveRelease):
            raise ValueError
        expected.__post_init__()
        requested = Path(os.path.abspath(archive))
        if requested.name != expected.filename:
            raise ValueError
        parent_fd = _open_archive_parent(requested.parent)
        try:
            descriptor = os.open(
                requested.name,
                os.O_RDONLY | os.O_CLOEXEC | getattr(os, "O_NOFOLLOW", 0),
                dir_fd=parent_fd,
            )
            try:
                metadata = os.fstat(descriptor)
                if (
                    not stat.S_ISREG(metadata.st_mode)
                    or metadata.st_uid != os.getuid()
                    or metadata.st_nlink != 1
                    or stat.S_IMODE(metadata.st_mode) & 0o077
                    or metadata.st_size != expected.byte_size
                ):
                    raise ValueError
                if _hash_exact_archive(descriptor, expected.byte_size) != expected.sha256:
                    raise ValueError
                os.lseek(descriptor, 0, os.SEEK_SET)
                authority = _verify_managed_runtime_tar(descriptor, expected)
                _after_managed_runtime_archive_structure(descriptor)
                os.lseek(descriptor, 0, os.SEEK_SET)
                if _hash_exact_archive(descriptor, expected.byte_size) != expected.sha256:
                    raise ValueError
                current = os.stat(
                    requested.name,
                    dir_fd=parent_fd,
                    follow_symlinks=False,
                )
                if (
                    not stat.S_ISREG(current.st_mode)
                    or current.st_uid != os.getuid()
                    or current.st_nlink != 1
                    or stat.S_IMODE(current.st_mode) & 0o077
                    or current.st_size != expected.byte_size
                    or (current.st_dev, current.st_ino) != (metadata.st_dev, metadata.st_ino)
                    or current.st_mtime_ns != metadata.st_mtime_ns
                    or current.st_ctime_ns != metadata.st_ctime_ns
                ):
                    raise ValueError
                return authority
            finally:
                os.close(descriptor)
        finally:
            os.close(parent_fd)
    except (OSError, tarfile.TarError, TypeError, ValueError) as exc:
        raise ManagedRuntimeArchiveVerificationError(
            "managed runtime archive authority is invalid"
        ) from exc


def _hash_exact_archive(descriptor: int, expected_size: int) -> str:
    digest = hashlib.sha256()
    observed = 0
    while observed < expected_size:
        chunk = os.read(descriptor, min(1024 * 1024, expected_size - observed))
        if not chunk:
            break
        observed += len(chunk)
        digest.update(chunk)
    if observed != expected_size or os.read(descriptor, 1):
        raise ValueError
    return digest.hexdigest()


def _after_managed_runtime_archive_structure(descriptor: int) -> None:
    del descriptor


def _open_archive_parent(path: Path) -> int:
    current_fd = os.open("/", os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    try:
        for part in path.parts[1:]:
            next_fd = os.open(
                part,
                os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | getattr(os, "O_NOFOLLOW", 0),
                dir_fd=current_fd,
            )
            os.close(current_fd)
            current_fd = next_fd
        return current_fd
    except BaseException:
        os.close(current_fd)
        raise


def _verify_managed_runtime_tar(
    descriptor: int,
    release: ManagedRuntimeArchiveRelease,
) -> ManagedRuntimeArchiveAuthority:
    names: set[str] = set()
    blob_names: set[str] = set()
    blob_sizes: dict[str, int] = {}
    metadata_payloads: dict[str, bytes] = {}
    declared_bytes = 0
    metadata_bytes = 0
    member_count = 0
    with os.fdopen(os.dup(descriptor), "rb", closefd=True) as stream:
        with tarfile.open(fileobj=stream, mode="r|gz") as archive:
            for member in archive:
                member_count += 1
                if member_count > _MAX_ARCHIVE_MEMBERS or member.name in names:
                    raise ValueError
                name = _safe_archive_member_name(member.name)
                names.add(name)
                if member.isdir():
                    if name not in {"blobs", "blobs/sha256"} or member.size != 0:
                        raise ValueError
                    continue
                if not member.isfile() or not 0 <= member.size <= _MAX_ARCHIVE_MEMBER_BYTES:
                    raise ValueError
                declared_bytes += member.size
                if declared_bytes > _MAX_ARCHIVE_DECLARED_BYTES:
                    raise ValueError
                is_blob = re.fullmatch(r"blobs/sha256/([0-9a-f]{64})", name)
                if is_blob is None and name not in {"index.json", "manifest.json", "oci-layout"}:
                    raise ValueError
                extracted = archive.extractfile(member)
                if extracted is None:
                    raise ValueError
                digest = hashlib.sha256()
                retained = bytearray()
                observed = 0
                while observed < member.size:
                    chunk = extracted.read(min(1024 * 1024, member.size - observed))
                    if not chunk:
                        break
                    observed += len(chunk)
                    digest.update(chunk)
                    if member.size <= _MAX_CONFIG_BYTES:
                        retained.extend(chunk)
                if observed != member.size or extracted.read(1):
                    raise ValueError
                payload = bytes(retained)
                if is_blob is not None:
                    if digest.hexdigest() != is_blob.group(1):
                        raise ValueError
                    blob_names.add(name)
                    blob_sizes[name] = member.size
                if name in {"index.json", "manifest.json", "oci-layout"}:
                    if member.size > _MAX_MANIFEST_BYTES:
                        raise ValueError
                    metadata_payloads[name] = payload
                    metadata_bytes += len(payload)
                elif is_blob is not None and member.size <= _MAX_CONFIG_BYTES:
                    metadata_payloads[name] = payload
                    metadata_bytes += len(payload)
                if metadata_bytes > _MAX_ARCHIVE_METADATA_BYTES:
                    raise ValueError
    if not {"blobs", "blobs/sha256", "index.json", "manifest.json", "oci-layout"}.issubset(names):
        raise ValueError
    manifest = _load_closed_json(metadata_payloads["manifest.json"])
    index = _load_closed_json(metadata_payloads["index.json"])
    oci_layout = _load_closed_json(metadata_payloads["oci-layout"])
    if (
        not isinstance(index, dict)
        or set(index) != {"manifests", "mediaType", "schemaVersion"}
        or index.get("schemaVersion") != 2
        or index.get("mediaType") != "application/vnd.oci.image.index.v1+json"
        or not isinstance(index.get("manifests"), list)
        or len(index["manifests"]) != 1
        or oci_layout != {"imageLayoutVersion": "1.0.0"}
    ):
        raise ValueError
    root_descriptor = index["manifests"][0]
    if (
        not isinstance(root_descriptor, dict)
        or set(root_descriptor) != {"digest", "mediaType", "size"}
        or root_descriptor.get("mediaType") != "application/vnd.oci.image.index.v1+json"
        or root_descriptor.get("digest") != release.oci_index_id
        or type(root_descriptor.get("size")) is not int
        or root_descriptor["size"] <= 0
    ):
        raise ValueError
    index_blob_name = "blobs/sha256/" + release.oci_index_id.removeprefix("sha256:")
    if blob_sizes.get(index_blob_name) != root_descriptor["size"]:
        raise ValueError
    image_index = _load_retained_json_blob(metadata_payloads, index_blob_name)
    if (
        not isinstance(image_index, dict)
        or set(image_index) != {"manifests", "mediaType", "schemaVersion"}
        or image_index.get("schemaVersion") != 2
        or image_index.get("mediaType") != "application/vnd.oci.image.index.v1+json"
        or not isinstance(image_index.get("manifests"), list)
        or len(image_index["manifests"]) != 1
    ):
        raise ValueError
    image_descriptor = image_index["manifests"][0]
    if (
        not isinstance(image_descriptor, dict)
        or set(image_descriptor) != {"digest", "mediaType", "platform", "size"}
        or image_descriptor.get("mediaType") != "application/vnd.oci.image.manifest.v1+json"
        or not isinstance(image_descriptor.get("digest"), str)
        or _SHA256_DIGEST_RE.fullmatch(image_descriptor["digest"]) is None
        or image_descriptor.get("platform") != {"architecture": "amd64", "os": "linux"}
        or type(image_descriptor.get("size")) is not int
        or image_descriptor["size"] <= 0
    ):
        raise ValueError
    image_manifest_name = "blobs/sha256/" + image_descriptor["digest"].removeprefix("sha256:")
    if blob_sizes.get(image_manifest_name) != image_descriptor["size"]:
        raise ValueError
    image_manifest = _load_retained_json_blob(metadata_payloads, image_manifest_name)
    if (
        not isinstance(image_manifest, dict)
        or set(image_manifest) != {"config", "layers", "mediaType", "schemaVersion"}
        or image_manifest.get("schemaVersion") != 2
        or image_manifest.get("mediaType") != "application/vnd.oci.image.manifest.v1+json"
    ):
        raise ValueError
    config_descriptor = image_manifest.get("config")
    if (
        not isinstance(config_descriptor, dict)
        or set(config_descriptor) != {"digest", "mediaType", "size"}
        or config_descriptor.get("mediaType") != "application/vnd.oci.image.config.v1+json"
        or config_descriptor.get("digest") != release.config_id
        or type(config_descriptor.get("size")) is not int
        or config_descriptor["size"] <= 0
    ):
        raise ValueError
    if not isinstance(manifest, list) or len(manifest) != 1:
        raise ValueError
    record = manifest[0]
    if not isinstance(record, dict) or set(record) != {"Config", "Layers", "RepoTags"}:
        raise ValueError
    config_name = record.get("Config")
    expected_config = f"blobs/sha256/{release.config_id.removeprefix('sha256:')}"
    if config_name != expected_config or config_name not in blob_names:
        raise ValueError
    layers = record.get("Layers")
    if (
        not isinstance(layers, list)
        or not layers
        or len(layers) > _MAX_ARCHIVE_MEMBERS
        or len(set(layers)) != len(layers)
        or any(not isinstance(layer, str) or layer not in blob_names for layer in layers)
        or record.get("RepoTags") not in (None, [])
    ):
        raise ValueError
    config_payload = metadata_payloads.get(expected_config)
    if (
        blob_sizes.get(expected_config) != config_descriptor["size"]
        or config_payload is None
        or hashlib.sha256(config_payload).hexdigest() != release.config_id[7:]
    ):
        raise ValueError
    layer_descriptors = image_manifest.get("layers")
    expected_layer_names = list(layers)
    if (
        not isinstance(layer_descriptors, list)
        or len(layer_descriptors) != len(expected_layer_names)
        or any(
            not isinstance(layer, dict)
            or set(layer) != {"digest", "mediaType", "size"}
            or layer.get("mediaType")
            not in {
                "application/vnd.oci.image.layer.v1.tar",
                "application/vnd.oci.image.layer.v1.tar+gzip",
            }
            or not isinstance(layer.get("digest"), str)
            or _SHA256_DIGEST_RE.fullmatch(layer["digest"]) is None
            or type(layer.get("size")) is not int
            or layer["size"] < 0
            for layer in layer_descriptors
        )
    ):
        raise ValueError
    oci_layer_names = [
        "blobs/sha256/" + layer["digest"].removeprefix("sha256:") for layer in layer_descriptors
    ]
    if oci_layer_names != expected_layer_names or any(
        blob_sizes.get(name) != layer["size"]
        for name, layer in zip(oci_layer_names, layer_descriptors, strict=True)
    ):
        raise ValueError
    if blob_names != {
        index_blob_name,
        image_manifest_name,
        expected_config,
        *oci_layer_names,
    }:
        raise ValueError
    config = _load_closed_json(config_payload)
    if not isinstance(config, dict):
        raise ValueError
    labels = (
        config.get("config", {}).get("Labels") if isinstance(config.get("config"), dict) else None
    )
    if (
        config.get("os") != "linux"
        or config.get("architecture") != "amd64"
        or not isinstance(labels, dict)
        or labels.get(MANAGED_RUNTIME_LABEL) != MANAGED_RUNTIME_LABEL_VALUE
    ):
        raise ValueError
    return ManagedRuntimeArchiveAuthority(
        config_id=release.config_id,
        oci_index_id=release.oci_index_id,
        platform=release.platform,
        managed_label=True,
    )


def _load_retained_json_blob(payloads: Mapping[str, bytes], name: str) -> object:
    payload = payloads.get(name)
    if payload is None:
        raise ValueError
    return _load_closed_json(payload)


def _load_closed_json(payload: bytes) -> object:
    try:
        return json.loads(payload.decode("utf-8"), object_pairs_hook=_closed_json_object)
    except (json.JSONDecodeError, RecursionError, UnicodeError) as exc:
        raise ValueError("managed runtime archive JSON is invalid") from exc


def _closed_json_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def _safe_archive_member_name(value: str) -> str:
    path = PurePosixPath(value)
    if (
        not value
        or path.is_absolute()
        or str(path) != value
        or any(part in {"", ".", ".."} for part in path.parts)
    ):
        raise ValueError
    return value


MANAGED_HOME: Final[str] = "/openevo/session/home"
MANAGED_WORKSPACE: Final[str] = "/openevo/session/workspace"
MANAGED_CODEX_HOME: Final[str] = "/openevo/credentials/codex"
MANAGED_CODEX_PACKAGE_ROOT: Final[str] = "/opt/codex"
MANAGED_CODEX_BINARY: Final[str] = f"{MANAGED_CODEX_PACKAGE_ROOT}/bin/codex"
MANAGED_CODEX_VERSION: Final[str] = "0.144.1"
MANAGED_CODEX_NPM_PACKAGE: Final[str] = f"@openai/codex@{MANAGED_CODEX_VERSION}"
MANAGED_CODEX_DEFAULT_MODEL: Final[str] = "gpt-5.5"
MANAGED_PATH: Final[str] = (
    f"{MANAGED_CODEX_PACKAGE_ROOT}/bin:"
    "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
)
MANAGED_SUBSCRIPTION_ENV: Final[dict[str, str]] = {
    "HOME": MANAGED_HOME,
    "PATH": MANAGED_PATH,
    "CODEX_HOME": MANAGED_CODEX_HOME,
}
MANAGED_SUBSCRIPTION_ENV_KEYS: Final[frozenset[str]] = frozenset(MANAGED_SUBSCRIPTION_ENV)
MANAGED_SUBSCRIPTION_PREPARE_COMMAND: Final[str] = (
    f"mkdir -p {MANAGED_HOME}/.codex {MANAGED_WORKSPACE} "
    "/openevo/session/logs/agent && "
    f"chmod 700 {MANAGED_HOME} {MANAGED_HOME}/.codex "
    "/openevo/session/logs /openevo/session/logs/agent"
)


def reject_managed_subscription_env(
    env: Mapping[str, object] | None,
    *,
    owner: str,
    allow_exact: bool = False,
) -> None:
    """Reject caller-controlled values for the managed subscription environment."""

    if not env:
        return
    for name in MANAGED_SUBSCRIPTION_ENV:
        if name in env:
            if allow_exact and env[name] == MANAGED_SUBSCRIPTION_ENV[name]:
                continue
            raise ValueError(f"subscription {owner} {name} is Core-owned and must be omitted")


def require_exact_managed_runtime_binding(
    *,
    profile: str | None,
    image: str | None,
) -> bool:
    """Return true only for a closed managed profile and its exact image."""

    if profile is None:
        return False
    if profile not in MANAGED_RUNTIME_IMAGES:
        raise ValueError(f"runtime profile/image binding is not Core-managed: {profile!r}")
    expected_image = MANAGED_RUNTIME_IMAGES[profile]
    if image != expected_image:
        raise ValueError(
            "runtime profile/image binding does not match the exact Core-managed "
            f"image {expected_image!r} for profile {profile!r}"
        )
    return True


def managed_runtime_image_release(
    *,
    profile: str | None,
    image: str | None,
) -> ManagedRuntimeImageRelease | None:
    """Return the release for its alias or one exact immutable authority."""

    if profile is None:
        return None
    if profile not in MANAGED_RUNTIME_RELEASES:
        raise ValueError(f"runtime profile/image binding is not Core-managed: {profile!r}")
    release = MANAGED_RUNTIME_RELEASES[profile]
    allowed_images = {
        release.image,
        release.trusted_digest,
        release.immutable_reference,
    }
    if profile == "managed_science":
        allowed_images.add(release.loaded_image_id)
    if image not in allowed_images:
        raise ValueError(
            "runtime profile/image binding does not match the exact Core-managed image release "
            f"for profile {profile!r}"
        )
    return release


def require_immutable_managed_runtime_image(
    *,
    profile: str | None,
    image: str | None,
) -> ManagedRuntimeImageRelease:
    """Require one exact immutable digest/reference for a managed release."""

    release = managed_runtime_image_release(profile=profile, image=image)
    if release is None or image not in {
        release.trusted_digest,
        release.loaded_image_id,
        release.immutable_reference,
    }:
        raise ValueError("managed runtime execution requires an immutable image authority")
    return release


def verified_managed_runtime_image_reference(
    *,
    profile: str | None,
    image: str | None,
    image_id: object,
    repo_digests: object,
    labels: object,
) -> str:
    """Validate inspected image evidence and return an immutable Docker reference."""

    release = managed_runtime_image_release(profile=profile, image=image)
    if release is None:
        raise ValueError("managed runtime image release is unavailable")
    if (
        not isinstance(image_id, str)
        or _SHA256_DIGEST_RE.fullmatch(image_id) is None
        or not isinstance(labels, dict)
        or labels.get("io.openevo.managed-runtime") != "true"
    ):
        raise ValueError("managed runtime image identity is invalid")
    if repo_digests is not None and (
        not isinstance(repo_digests, list)
        or any(not isinstance(item, str) for item in repo_digests)
    ):
        raise ValueError("managed runtime repository digests are invalid")
    repository_authority = (
        isinstance(repo_digests, list) and release.immutable_reference in repo_digests
    )
    offline_repository_digests = {
        f"{release.repository}@{release.loaded_image_id}",
        f"docker.io/{release.repository}@{release.loaded_image_id}",
    }
    offline_repository_authority = (
        isinstance(repo_digests, list)
        and 0 < len(repo_digests) <= len(offline_repository_digests)
        and len(repo_digests) == len(set(repo_digests))
        and all(item in offline_repository_digests for item in repo_digests)
    )
    if image == release.loaded_image_id:
        if profile != "managed_science":
            raise ValueError("managed runtime offline image is unavailable for this profile")
        if repo_digests and not offline_repository_authority:
            raise ValueError("managed runtime offline image has registry authority")
        if image_id == release.loaded_image_id:
            return release.loaded_image_id
        raise ValueError("managed runtime image digest mismatch")
    if (
        profile == "managed_science"
        and image == release.image
        and image_id == release.loaded_image_id
    ):
        if not repo_digests or offline_repository_authority:
            return release.loaded_image_id
        raise ValueError("managed runtime image digest mismatch")
    if image_id == release.trusted_digest:
        return release.trusted_digest
    if repository_authority:
        return release.immutable_reference
    raise ValueError("managed runtime image digest mismatch")


def require_managed_subscription_runtime(
    *,
    profile: str | None,
    image: str | None,
    backend: str,
    container_user: str,
    workdir: str | None = None,
) -> None:
    """Require the exact Core-managed Docker runtime used for subscription auth."""

    if profile not in MANAGED_RUNTIME_IMAGES:
        raise ValueError("subscription execution requires a managed runtime profile")
    try:
        require_immutable_managed_runtime_image(profile=profile, image=image)
    except ValueError as exc:
        raise ValueError(
            "subscription execution requires the exact immutable managed runtime "
            f"image for profile {profile!r}"
        ) from exc
    if backend != "docker":
        raise ValueError("subscription execution requires the managed Docker runtime")
    if container_user != "host":
        raise ValueError("subscription credentials require runtime.container_user='host'")
    if workdir is not None and workdir != MANAGED_WORKSPACE:
        raise ValueError(f"subscription execution requires runtime.workdir={MANAGED_WORKSPACE!r}")


def require_managed_runtime_binding(
    *,
    profile: str | None,
    image: str | None,
    backend: str,
    container_user: str,
) -> bool:
    """Validate any explicitly selected Core-managed runtime profile."""

    if profile is None:
        return False
    managed_runtime_image_release(profile=profile, image=image)
    if backend != "docker":
        raise ValueError("Core-managed runtime profiles require the Docker runtime")
    if container_user != "host":
        raise ValueError("Core-managed runtime profiles require runtime.container_user='host'")
    return True


__all__ = [
    "MANAGED_CODEX_HOME",
    "MANAGED_CODEX_BINARY",
    "MANAGED_CODEX_PACKAGE_ROOT",
    "MANAGED_CODEX_VERSION",
    "MANAGED_CODEX_NPM_PACKAGE",
    "MANAGED_CODEX_DEFAULT_MODEL",
    "MANAGED_HOME",
    "MANAGED_PATH",
    "MANAGED_WORKSPACE",
    "MANAGED_RUNTIME_IMAGES",
    "MANAGED_RUNTIME_LABEL",
    "MANAGED_RUNTIME_LABEL_VALUE",
    "MANAGED_RUNTIME_ARCHIVE_RELEASE",
    "MANAGED_RUNTIME_RELEASES",
    "MANAGED_SUBSCRIPTION_ENV",
    "MANAGED_SUBSCRIPTION_ENV_KEYS",
    "MANAGED_SUBSCRIPTION_PREPARE_COMMAND",
    "ManagedRuntimeProfile",
    "ManagedRuntimeArchiveAuthority",
    "ManagedRuntimeArchiveVerificationError",
    "ManagedRuntimeImageRelease",
    "ManagedRuntimeArchiveRelease",
    "ManagedCredentialMount",
    "verify_managed_runtime_archive",
    "managed_runtime_image_release",
    "require_immutable_managed_runtime_image",
    "verified_managed_runtime_image_reference",
    "reject_managed_subscription_env",
    "require_exact_managed_runtime_binding",
    "require_managed_runtime_binding",
    "require_managed_subscription_runtime",
]
