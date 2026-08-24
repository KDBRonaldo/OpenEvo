"""Build and verify versioned self-hosted OpenEvo server release bundles."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import io
import json
import os
from pathlib import Path, PurePosixPath
import re
import subprocess
import tarfile
import tempfile
import tomllib
from typing import Any, Iterable
import zipfile


BUNDLE_SCHEMA_VERSION = "1"
BUNDLE_SUFFIX = ".oevobundle"
MAX_BUNDLE_FILES = 10_000
MAX_BUNDLE_FILE_BYTES = 128 * 1024 * 1024
MAX_BUNDLE_PAYLOAD_BYTES = 512 * 1024 * 1024
COMMIT_PATTERN = re.compile(r"[0-9a-f]{40,64}")
RELEASE_ID_PATTERN = re.compile(r"[0-9a-f]{64}")
VERSION_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._+-]{0,63}")
REQUIRED_PATHS = frozenset(
    {
        "LICENSE",
        "README.md",
        "pyproject.toml",
        "uv.lock",
        "src/openevo/__init__.py",
        "src/openevo/daemon/product_app.py",
        "src/openevo/web_gateway/product_app.py",
        "src/openevo/web_gateway/static/index.html",
        "desktop/server/__init__.py",
        "desktop/sidecar/__init__.py",
    }
)
PAYLOAD_PREFIXES = (
    "src/openevo/",
    "src/slime_bridge/",
    "desktop/server/",
    "desktop/sidecar/",
)
PAYLOAD_ROOT_FILES = frozenset({"LICENSE", "README.md", "pyproject.toml", "uv.lock"})


class ReleaseBundleError(RuntimeError):
    """A release bundle is incomplete, malformed, or inconsistent."""


@dataclass(frozen=True)
class ReleaseBundleReceipt:
    path: Path
    release_id: str
    product_version: str
    source_commit: str
    sha256: str
    byte_size: int
    file_count: int


def _canonical_json(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=True,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        while chunk := source.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _git(repository_root: Path, *arguments: str, text: bool = True) -> str | bytes:
    completed = subprocess.run(
        ["git", *arguments],
        cwd=repository_root,
        check=False,
        capture_output=True,
        text=text,
        timeout=120,
    )
    if completed.returncode != 0:
        stderr = completed.stderr
        detail = stderr.strip() if isinstance(stderr, str) else stderr.decode(errors="replace").strip()
        raise ReleaseBundleError(f"git {' '.join(arguments)} failed: {detail}")
    return completed.stdout


def _validate_payload_path(value: str) -> str:
    if not value or "\\" in value or "\x00" in value:
        raise ReleaseBundleError(f"invalid release payload path: {value!r}")
    path = PurePosixPath(value)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        raise ReleaseBundleError(f"unsafe release payload path: {value!r}")
    normalized = path.as_posix()
    if normalized != value:
        raise ReleaseBundleError(f"non-canonical release payload path: {value!r}")
    return normalized


def _is_payload_path(path: str) -> bool:
    return path in PAYLOAD_ROOT_FILES or any(path.startswith(prefix) for prefix in PAYLOAD_PREFIXES)


def _committed_payload(repository_root: Path, commit: str) -> list[tuple[str, bytes]]:
    """Read the selected committed directories with one bounded Git process."""

    completed = subprocess.run(
        [
            "git",
            "archive",
            "--format=tar",
            commit,
            "--",
            *sorted(PAYLOAD_ROOT_FILES),
            *PAYLOAD_PREFIXES,
        ],
        cwd=repository_root,
        check=False,
        capture_output=True,
        timeout=120,
    )
    if completed.returncode != 0:
        raise ReleaseBundleError(
            f"git archive failed: {completed.stderr.decode(errors='replace').strip()}"
        )
    if len(completed.stdout) > MAX_BUNDLE_PAYLOAD_BYTES + 64 * 1024 * 1024:
        raise ReleaseBundleError("committed release archive exceeds its transport byte limit")
    payload: list[tuple[str, bytes]] = []
    total_bytes = 0
    try:
        with tarfile.open(fileobj=io.BytesIO(completed.stdout), mode="r:") as archive:
            for member in archive:
                if member.isdir():
                    continue
                path = _validate_payload_path(member.name)
                if not _is_payload_path(path) or not member.isfile():
                    raise ReleaseBundleError(
                        f"committed release payload contains a disallowed entry: {path}"
                    )
                if member.size > MAX_BUNDLE_FILE_BYTES:
                    raise ReleaseBundleError(f"release payload file exceeds its byte limit: {path}")
                source = archive.extractfile(member)
                if source is None:
                    raise ReleaseBundleError(f"could not read committed payload file: {path}")
                data = source.read(MAX_BUNDLE_FILE_BYTES + 1)
                if len(data) != member.size:
                    raise ReleaseBundleError(f"committed payload size changed while reading: {path}")
                total_bytes += len(data)
                if total_bytes > MAX_BUNDLE_PAYLOAD_BYTES:
                    raise ReleaseBundleError("release payload exceeds its aggregate byte limit")
                payload.append((path, data))
    except tarfile.TarError as exc:
        raise ReleaseBundleError("git produced an invalid release archive") from exc
    payload.sort(key=lambda item: item[0])
    if len(payload) > MAX_BUNDLE_FILES:
        raise ReleaseBundleError(f"release payload contains more than {MAX_BUNDLE_FILES} files")
    paths = {path for path, _ in payload}
    if len(paths) != len(payload):
        raise ReleaseBundleError("committed release payload contains duplicate paths")
    missing = sorted(REQUIRED_PATHS.difference(paths))
    if missing:
        raise ReleaseBundleError(f"release payload is missing required files: {', '.join(missing)}")
    return payload


def _product_version(raw: bytes) -> str:
    try:
        value = tomllib.loads(raw.decode("utf-8"))["project"]["version"]
    except (UnicodeDecodeError, tomllib.TOMLDecodeError, KeyError, TypeError) as exc:
        raise ReleaseBundleError("pyproject.toml has no valid project.version") from exc
    if not isinstance(value, str) or not VERSION_PATTERN.fullmatch(value):
        raise ReleaseBundleError("project.version is not safe for a release identity")
    return value


def _release_identity_manifest(
    *,
    product_version: str,
    source_commit: str,
    files: Iterable[dict[str, object]],
) -> dict[str, object]:
    return {
        "schema_version": BUNDLE_SCHEMA_VERSION,
        "product_version": product_version,
        "source_commit": source_commit,
        "payload_root": "payload",
        "files": list(files),
    }


def _zip_info(path: str) -> zipfile.ZipInfo:
    info = zipfile.ZipInfo(path, date_time=(1980, 1, 1, 0, 0, 0))
    info.compress_type = zipfile.ZIP_DEFLATED
    info.create_system = 3
    info.external_attr = (0o100644 & 0xFFFF) << 16
    return info


def build_release_bundle(
    repository_root: Path,
    output_path: Path,
    *,
    commit: str = "HEAD",
) -> ReleaseBundleReceipt:
    """Build a deterministic bundle from the exact committed runtime payload."""

    root = repository_root.resolve()
    resolved_commit = _git(root, "rev-parse", f"{commit}^{{commit}}")
    assert isinstance(resolved_commit, str)
    resolved_commit = resolved_commit.strip()
    if not COMMIT_PATTERN.fullmatch(resolved_commit):
        raise ReleaseBundleError("git returned an invalid source commit")
    payload = _committed_payload(root, resolved_commit)
    payload_by_path = dict(payload)
    version = _product_version(payload_by_path["pyproject.toml"])
    files: list[dict[str, object]] = []
    total_bytes = 0
    for path, data in payload:
        total_bytes += len(data)
        if total_bytes > MAX_BUNDLE_PAYLOAD_BYTES:
            raise ReleaseBundleError("release payload exceeds its aggregate byte limit")
        files.append({"path": path, "byte_size": len(data), "sha256": _sha256_bytes(data)})

    identity = _release_identity_manifest(
        product_version=version,
        source_commit=resolved_commit,
        files=files,
    )
    release_id = _sha256_bytes(_canonical_json(identity))
    manifest = {**identity, "release_id": release_id}
    target = output_path.resolve()
    if target.suffix != BUNDLE_SUFFIX:
        raise ReleaseBundleError(f"release bundle output must end with {BUNDLE_SUFFIX}")
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary_name = tempfile.mkstemp(prefix="openevo-release-", suffix=".tmp", dir=target.parent)
    os.close(fd)
    temporary = Path(temporary_name)
    try:
        with zipfile.ZipFile(temporary, "w", allowZip64=False) as archive:
            archive.writestr(_zip_info("manifest.json"), _canonical_json(manifest) + b"\n")
            for path, data in payload:
                archive.writestr(_zip_info(f"payload/{path}"), data)
        os.replace(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)
    return verify_release_bundle(target)


def _parse_manifest(value: Any) -> tuple[dict[str, object], list[dict[str, object]]]:
    if not isinstance(value, dict) or set(value) != {
        "schema_version",
        "release_id",
        "product_version",
        "source_commit",
        "payload_root",
        "files",
    }:
        raise ReleaseBundleError("release manifest does not match the closed schema")
    if value["schema_version"] != BUNDLE_SCHEMA_VERSION or value["payload_root"] != "payload":
        raise ReleaseBundleError("release manifest schema or payload root is unsupported")
    if not isinstance(value["product_version"], str) or not VERSION_PATTERN.fullmatch(
        value["product_version"]
    ):
        raise ReleaseBundleError("release manifest product version is invalid")
    if not isinstance(value["source_commit"], str) or not COMMIT_PATTERN.fullmatch(
        value["source_commit"]
    ):
        raise ReleaseBundleError("release manifest source commit is invalid")
    if not isinstance(value["release_id"], str) or not RELEASE_ID_PATTERN.fullmatch(
        value["release_id"]
    ):
        raise ReleaseBundleError("release manifest identity is invalid")
    raw_files = value["files"]
    if not isinstance(raw_files, list) or not 1 <= len(raw_files) <= MAX_BUNDLE_FILES:
        raise ReleaseBundleError("release manifest file inventory is invalid")
    files: list[dict[str, object]] = []
    seen: set[str] = set()
    total_bytes = 0
    for item in raw_files:
        if not isinstance(item, dict) or set(item) != {"path", "byte_size", "sha256"}:
            raise ReleaseBundleError("release manifest contains an invalid file record")
        path = _validate_payload_path(item["path"] if isinstance(item["path"], str) else "")
        size = item["byte_size"]
        digest = item["sha256"]
        if path in seen or not _is_payload_path(path):
            raise ReleaseBundleError(f"release manifest contains a duplicate or disallowed path: {path}")
        if not isinstance(size, int) or isinstance(size, bool) or not 0 <= size <= MAX_BUNDLE_FILE_BYTES:
            raise ReleaseBundleError(f"release manifest contains an invalid size for {path}")
        if not isinstance(digest, str) or not RELEASE_ID_PATTERN.fullmatch(digest):
            raise ReleaseBundleError(f"release manifest contains an invalid digest for {path}")
        total_bytes += size
        if total_bytes > MAX_BUNDLE_PAYLOAD_BYTES:
            raise ReleaseBundleError("release manifest exceeds its aggregate byte limit")
        seen.add(path)
        files.append({"path": path, "byte_size": size, "sha256": digest})
    if [item["path"] for item in files] != sorted(seen):
        raise ReleaseBundleError("release manifest file inventory is not canonically sorted")
    missing = sorted(REQUIRED_PATHS.difference(seen))
    if missing:
        raise ReleaseBundleError(f"release manifest is missing required files: {', '.join(missing)}")
    identity = _release_identity_manifest(
        product_version=value["product_version"],
        source_commit=value["source_commit"],
        files=files,
    )
    if _sha256_bytes(_canonical_json(identity)) != value["release_id"]:
        raise ReleaseBundleError("release manifest identity does not match its contents")
    return value, files


def verify_release_bundle(path: Path) -> ReleaseBundleReceipt:
    """Strictly verify archive shape, manifest identity, sizes, and every payload digest."""

    bundle = path.resolve()
    try:
        with zipfile.ZipFile(bundle, "r") as archive:
            infos = archive.infolist()
            names = [info.filename for info in infos]
            if len(names) != len(set(names)) or names.count("manifest.json") != 1:
                raise ReleaseBundleError("release archive has duplicate entries or no manifest")
            if len(infos) > MAX_BUNDLE_FILES + 1:
                raise ReleaseBundleError("release archive contains too many entries")
            manifest_info = archive.getinfo("manifest.json")
            if manifest_info.file_size > 4 * 1024 * 1024:
                raise ReleaseBundleError("release manifest exceeds its byte limit")
            try:
                manifest_value = json.loads(archive.read(manifest_info))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise ReleaseBundleError("release manifest is not valid UTF-8 JSON") from exc
            manifest, files = _parse_manifest(manifest_value)
            expected_names = {"manifest.json", *(f"payload/{item['path']}" for item in files)}
            if set(names) != expected_names:
                raise ReleaseBundleError("release archive entries do not match the manifest")
            for item in files:
                name = f"payload/{item['path']}"
                info = archive.getinfo(name)
                mode = (info.external_attr >> 16) & 0xFFFF
                if info.is_dir() or (mode and (mode & 0o170000) != 0o100000):
                    raise ReleaseBundleError(f"release archive entry is not a regular file: {name}")
                if info.file_size != item["byte_size"]:
                    raise ReleaseBundleError(f"release archive size mismatch: {name}")
                digest = hashlib.sha256()
                read_bytes = 0
                with archive.open(info, "r") as source:
                    while chunk := source.read(1024 * 1024):
                        read_bytes += len(chunk)
                        if read_bytes > item["byte_size"]:
                            raise ReleaseBundleError(f"release archive expands beyond its manifest: {name}")
                        digest.update(chunk)
                if digest.hexdigest() != item["sha256"]:
                    raise ReleaseBundleError(f"release archive digest mismatch: {name}")
    except (OSError, zipfile.BadZipFile, KeyError) as exc:
        raise ReleaseBundleError(f"could not read release bundle: {exc}") from exc
    return ReleaseBundleReceipt(
        path=bundle,
        release_id=str(manifest["release_id"]),
        product_version=str(manifest["product_version"]),
        source_commit=str(manifest["source_commit"]),
        sha256=_sha256_file(bundle),
        byte_size=bundle.stat().st_size,
        file_count=len(files),
    )


__all__ = [
    "BUNDLE_SUFFIX",
    "ReleaseBundleError",
    "ReleaseBundleReceipt",
    "build_release_bundle",
    "verify_release_bundle",
]
