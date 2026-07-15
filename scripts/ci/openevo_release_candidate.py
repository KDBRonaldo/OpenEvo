#!/usr/bin/env python3
"""Create and verify one exact unsigned OpenEvo Desktop release candidate."""

from __future__ import annotations

import argparse
from email.parser import Parser
import hashlib
import json
from pathlib import Path
import re
import stat
import sys
from typing import Any
from zipfile import BadZipFile, ZipFile


MANIFEST_NAME = "release-candidate.json"
CORE_DESCRIPTOR_NAME = "core-install-artifact.json"
CHECKSUMS_NAME = "SHA256SUMS"
MINIMUM_MACOS_VERSION = "12.0"
REPO_ROOT = Path(__file__).resolve().parents[2]
ARCHITECTURE_TARGETS = {
    "aarch64": "aarch64-apple-darwin",
    "x64": "x86_64-apple-darwin",
}
SOURCE_COMMIT_PATTERN = re.compile(r"[0-9a-f]{40}")
DIGEST_PATTERN = re.compile(r"[0-9a-f]{64}")
REQUIRED_INPUT_ROLES = (
    ("desktop_dmg", None),
    ("core_wheel", None),
    ("framework_lock", "framework-lock.json"),
    ("release_notes", "release-notes.md"),
    ("dependency_inventory", "dependency-inventory.json"),
    ("license_inventory", "license-inventory.json"),
    ("security_audit", "security-audit.json"),
    ("app_bundle_smoke", "app-bundle-smoke.json"),
    ("dmg_copy_smoke", "dmg-copy-smoke.json"),
)
FINAL_ROLES = tuple(role for role, _name in REQUIRED_INPUT_ROLES) + (
    "core_descriptor",
    "checksums",
)


class CandidateError(RuntimeError):
    pass


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _is_regular_file(path: Path) -> bool:
    try:
        return stat.S_ISREG(path.lstat().st_mode)
    except OSError:
        return False


def _canonical_json(payload: object) -> bytes:
    return (json.dumps(payload, separators=(",", ":"), sort_keys=True) + "\n").encode("utf-8")


def _write_new(path: Path, payload: bytes) -> None:
    try:
        with path.open("xb") as stream:
            stream.write(payload)
    except FileExistsError as exc:
        raise CandidateError(f"Refusing to replace existing candidate file: {path.name}") from exc


def _load_json(path: Path) -> Any:
    try:
        if path.stat().st_size > 1024 * 1024:
            raise CandidateError(f"Candidate JSON exceeds 1 MiB: {path.name}")
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CandidateError(f"Candidate JSON is unreadable: {path.name}") from exc


def _require_digest(value: object, subject: str) -> str:
    if type(value) is not str or DIGEST_PATTERN.fullmatch(value) is None:
        raise CandidateError(f"{subject} must be a lowercase SHA-256 digest")
    return value


def _require_safe_basename(value: object, subject: str) -> str:
    if (
        type(value) is not str
        or not value
        or Path(value).name != value
        or value in {".", ".."}
        or any(ord(character) < 32 or ord(character) == 127 for character in value)
    ):
        raise CandidateError(f"{subject} must be one safe filename")
    return value


def _single_match(root: Path, pattern: str, subject: str) -> Path:
    matches = sorted(root.glob(pattern))
    if len(matches) != 1 or not _is_regular_file(matches[0]):
        raise CandidateError(f"Candidate must contain exactly one {subject}; found {len(matches)}")
    return matches[0]


def _validate_wheel(wheel: Path, *, version: str) -> None:
    try:
        with ZipFile(wheel) as archive:
            names = archive.namelist()
            metadata_names = [name for name in names if name.endswith(".dist-info/METADATA")]
            entry_names = [name for name in names if name.endswith(".dist-info/entry_points.txt")]
            if len(metadata_names) != 1 or len(entry_names) != 1:
                raise CandidateError("Core wheel metadata or entry points are incomplete")
            metadata = Parser().parsestr(archive.read(metadata_names[0]).decode("utf-8"))
            entry_points = archive.read(entry_names[0]).decode("utf-8")
    except (BadZipFile, OSError, UnicodeDecodeError) as exc:
        raise CandidateError("Core wheel is unreadable") from exc
    if metadata.get("Name") != "openevo" or metadata.get("Version") != version:
        raise CandidateError("Core wheel name/version does not match the candidate")
    required = (
        "openevo-backend = openevo.backend.launcher:main",
        "openevo-core-service = openevo.backend.service:main",
    )
    if any(entry not in entry_points for entry in required):
        raise CandidateError("Core wheel does not expose the required backend launchers")


def _validate_framework_lock(lock_path: Path, wheel: Path, *, version: str) -> dict[str, object]:
    lock = _load_json(lock_path)
    required_keys = {
        "schema_version",
        "distribution",
        "distribution_version",
        "distribution_digest",
        "wheel_filename",
    }
    if type(lock) is not dict or set(lock) != required_keys:
        raise CandidateError("framework-lock.json does not use the closed release schema")
    if (
        lock.get("schema_version") != "1"
        or lock.get("distribution") != "openevo"
        or lock.get("distribution_version") != version
        or lock.get("wheel_filename") != wheel.name
        or lock.get("distribution_digest") != _sha256(wheel)
    ):
        raise CandidateError("framework-lock.json does not bind the exact Core wheel")
    return lock


def _validate_evidence(root: Path) -> None:
    dependency = _load_json(root / "dependency-inventory.json")
    license_inventory = _load_json(root / "license-inventory.json")
    security = _load_json(root / "security-audit.json")
    ecosystems = {"python", "npm", "cargo"}
    if type(dependency) is not dict or set(dependency) != {"schema_version", "ecosystems"}:
        raise CandidateError("dependency inventory does not use the closed evidence schema")
    dependency_ecosystems = dependency.get("ecosystems")
    if dependency.get("schema_version") != 1 or type(dependency_ecosystems) is not dict:
        raise CandidateError("dependency inventory schema version is invalid")
    if set(dependency_ecosystems) != ecosystems:
        raise CandidateError("dependency inventory must cover Python, npm, and Cargo")
    for ecosystem, entry in dependency_ecosystems.items():
        if type(entry) is not dict or set(entry) != {"lockfile_sha256", "packages"}:
            raise CandidateError(f"dependency evidence is invalid for {ecosystem}")
        _require_digest(entry.get("lockfile_sha256"), f"{ecosystem} lockfile digest")
        if type(entry.get("packages")) is not int or entry["packages"] < 1:
            raise CandidateError(f"dependency evidence is empty for {ecosystem}")
    lockfiles = {
        "python": REPO_ROOT / "uv.lock",
        "npm": REPO_ROOT / "desktop/package-lock.json",
        "cargo": REPO_ROOT / "desktop/src-tauri/Cargo.lock",
    }
    for ecosystem, path in lockfiles.items():
        if path.is_file() and dependency_ecosystems[ecosystem]["lockfile_sha256"] != _sha256(path):
            raise CandidateError(f"dependency evidence does not bind the checkout {ecosystem} lockfile")

    if type(license_inventory) is not dict or set(license_inventory) != {
        "schema_version",
        "project_license_sha256",
        "ecosystems",
    }:
        raise CandidateError("license inventory does not use the closed evidence schema")
    license_ecosystems = license_inventory.get("ecosystems")
    if license_inventory.get("schema_version") != 1 or type(license_ecosystems) is not dict:
        raise CandidateError("license inventory schema version is invalid")
    _require_digest(license_inventory.get("project_license_sha256"), "project license digest")
    project_license = REPO_ROOT / "LICENSE"
    if (
        project_license.is_file()
        and license_inventory.get("project_license_sha256") != _sha256(project_license)
    ):
        raise CandidateError("license evidence does not bind the checkout LICENSE")
    if set(license_ecosystems) != ecosystems:
        raise CandidateError("license inventory must cover Python, npm, and Cargo")
    for ecosystem, entry in license_ecosystems.items():
        if type(entry) is not dict or set(entry) != {"packages", "unresolved"}:
            raise CandidateError(f"license evidence is invalid for {ecosystem}")
        if type(entry.get("packages")) is not int or entry["packages"] < 1:
            raise CandidateError(f"license evidence is empty for {ecosystem}")
        if entry.get("unresolved") != 0:
            raise CandidateError(f"license evidence has unresolved packages for {ecosystem}")

    expected_audits = {"npm-audit-high", "pip-audit", "cargo-audit"}
    if type(security) is not dict or set(security) != {"schema_version", "audits"}:
        raise CandidateError("security evidence does not use the closed evidence schema")
    audits = security.get("audits")
    if security.get("schema_version") != 1 or type(audits) is not dict:
        raise CandidateError("security evidence schema version is invalid")
    if set(audits) != expected_audits:
        raise CandidateError("security evidence must cover npm, Python, and Cargo")
    for audit, entry in audits.items():
        if type(entry) is not dict or set(entry) != {"status", "vulnerabilities"}:
            raise CandidateError(f"security evidence is invalid for {audit}")
        if entry.get("status") != "passed" or entry.get("vulnerabilities") != 0:
            raise CandidateError(f"security evidence did not pass for {audit}")

    smoke_keys = {
        "schema_version",
        "native_executable",
        "bundled_external_bin",
        "renderer_ready",
        "sidecar_ready",
        "bundled_external_bin_resolved",
        "native_listener_fd_handoff",
        "native_executable_fd_handoff",
        "process_group_cleanup",
    }
    for filename in ("app-bundle-smoke.json", "dmg-copy-smoke.json"):
        smoke = _load_json(root / filename)
        if type(smoke) is not dict or set(smoke) != smoke_keys:
            raise CandidateError(f"{filename} does not use the closed smoke evidence schema")
        if smoke.get("schema_version") != 1:
            raise CandidateError(f"{filename} schema version is invalid")
        if smoke.get("native_executable") != "OpenEvo Desktop":
            raise CandidateError(f"{filename} did not launch the Tauri executable")
        if smoke.get("bundled_external_bin") != "openevo-desktop-sidecar":
            raise CandidateError(f"{filename} did not resolve the packaged externalBin")
        boolean_keys = smoke_keys - {"schema_version", "native_executable", "bundled_external_bin"}
        if any(smoke.get(key) is not True for key in boolean_keys):
            raise CandidateError(f"{filename} contains failing native evidence")


def _validate_release_notes(
    path: Path,
    *,
    source_commit: str,
    version: str,
    architecture: str,
) -> None:
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise CandidateError("Release notes are unreadable") from exc
    required = (source_commit, version, architecture, "unsigned", "not notarized")
    if any(value.lower() not in text.lower() for value in required):
        raise CandidateError("Release notes do not bind commit, version, architecture, and signing status")


def _file_entry(role: str, path: Path) -> dict[str, object]:
    return {
        "byte_size": path.stat().st_size,
        "filename": path.name,
        "role": role,
        "sha256": _sha256(path),
    }


def _candidate_paths(root: Path, *, version: str, architecture: str) -> dict[str, Path]:
    paths: dict[str, Path] = {}
    for role, filename in REQUIRED_INPUT_ROLES:
        if role == "desktop_dmg":
            path = _single_match(
                root,
                f"OpenEvo-Desktop-{version}-{architecture}.dmg",
                "architecture-specific Desktop DMG",
            )
        elif role == "core_wheel":
            path = _single_match(root, f"openevo-{version}-*.whl", "Core wheel")
        else:
            assert filename is not None
            path = root / filename
            if not _is_regular_file(path):
                raise CandidateError(f"Candidate input is missing: {filename}")
        paths[role] = path
    return paths


def create_candidate_manifest(
    candidate_root: Path,
    *,
    source_commit: str,
    version: str,
    architecture: str,
    rust_target: str,
    registry_digest: str,
) -> Path:
    root = candidate_root.resolve()
    if not root.is_dir():
        raise CandidateError("Candidate root must be a directory")
    if SOURCE_COMMIT_PATTERN.fullmatch(source_commit) is None:
        raise CandidateError("source_commit must be one full lowercase Git commit")
    if architecture not in ARCHITECTURE_TARGETS:
        raise CandidateError("candidate architecture must be the actual host architecture, not universal")
    if ARCHITECTURE_TARGETS[architecture] != rust_target:
        raise CandidateError("candidate architecture does not match the actual Rust host target")
    _require_digest(registry_digest, "registry digest")
    if not version or any(character.isspace() for character in version):
        raise CandidateError("candidate version is invalid")
    generated = [root / name for name in (CORE_DESCRIPTOR_NAME, CHECKSUMS_NAME, MANIFEST_NAME)]
    if any(path.exists() for path in generated):
        raise CandidateError("candidate generated files must not already exist")

    paths = _candidate_paths(root, version=version, architecture=architecture)
    wheel = paths["core_wheel"]
    _validate_wheel(wheel, version=version)
    _validate_framework_lock(paths["framework_lock"], wheel, version=version)
    _validate_evidence(root)
    _validate_release_notes(
        paths["release_notes"],
        source_commit=source_commit,
        version=version,
        architecture=architecture,
    )

    descriptor = {
        "artifact": _file_entry("core_wheel", wheel),
        "framework_lock": _file_entry("framework_lock", paths["framework_lock"]),
        "registry_digest": registry_digest,
        "schema_version": 1,
        "source_commit": source_commit,
        "version": version,
    }
    descriptor_path = root / CORE_DESCRIPTOR_NAME
    _write_new(descriptor_path, _canonical_json(descriptor))
    paths["core_descriptor"] = descriptor_path

    checksum_subjects = sorted(paths.values(), key=lambda path: path.name)
    checksum_payload = "".join(f"{_sha256(path)}  {path.name}\n" for path in checksum_subjects)
    checksums_path = root / CHECKSUMS_NAME
    _write_new(checksums_path, checksum_payload.encode("ascii"))
    paths["checksums"] = checksums_path

    files = [_file_entry(role, paths[role]) for role in FINAL_ROLES]
    manifest = {
        "core": {
            "descriptor_filename": CORE_DESCRIPTOR_NAME,
            "descriptor_sha256": _sha256(descriptor_path),
            "registry_digest": registry_digest,
        },
        "files": files,
        "macos": {
            "architecture": architecture,
            "minimum_system_version": MINIMUM_MACOS_VERSION,
            "rust_target": rust_target,
        },
        "release": {
            "channel": "unsigned-draft-prerelease",
            "notarized": False,
            "signed": False,
        },
        "schema_version": 1,
        "source_commit": source_commit,
        "version": version,
    }
    manifest_path = root / MANIFEST_NAME
    _write_new(manifest_path, _canonical_json(manifest))
    errors = validate_candidate_manifest(manifest_path, expected_source_commit=source_commit)
    if errors:
        raise CandidateError("generated candidate failed validation: " + "; ".join(errors))
    return manifest_path


def _validate_candidate_manifest(
    manifest_path: Path,
    *,
    expected_source_commit: str | None,
) -> None:
    if not _is_regular_file(manifest_path):
        raise CandidateError("candidate manifest must be a regular non-symlink file")
    root = manifest_path.absolute().parent
    manifest = _load_json(manifest_path)
    required_keys = {
        "schema_version",
        "source_commit",
        "version",
        "release",
        "macos",
        "core",
        "files",
    }
    if type(manifest) is not dict or set(manifest) != required_keys:
        raise CandidateError("candidate manifest does not use the closed release schema")
    source_commit = manifest.get("source_commit")
    version = manifest.get("version")
    release = manifest.get("release")
    macos = manifest.get("macos")
    core = manifest.get("core")
    files = manifest.get("files")
    if manifest.get("schema_version") != 1:
        raise CandidateError("candidate manifest schema version is invalid")
    if type(source_commit) is not str or SOURCE_COMMIT_PATTERN.fullmatch(source_commit) is None:
        raise CandidateError("candidate source commit is invalid")
    if expected_source_commit is not None and source_commit != expected_source_commit:
        raise CandidateError("candidate source commit does not match the expected checkout")
    if type(version) is not str or not version:
        raise CandidateError("candidate version is invalid")
    if release != {"channel": "unsigned-draft-prerelease", "notarized": False, "signed": False}:
        raise CandidateError("candidate release must remain unsigned, not notarized, and prerelease")
    if type(macos) is not dict or set(macos) != {
        "architecture",
        "minimum_system_version",
        "rust_target",
    }:
        raise CandidateError("candidate macOS identity is invalid")
    architecture = macos.get("architecture")
    if architecture not in ARCHITECTURE_TARGETS:
        raise CandidateError("candidate declares an unsupported or unbuilt architecture")
    if (
        macos.get("rust_target") != ARCHITECTURE_TARGETS[architecture]
        or macos.get("minimum_system_version") != MINIMUM_MACOS_VERSION
    ):
        raise CandidateError("candidate macOS target identity is inconsistent")
    if type(files) is not list or len(files) != len(FINAL_ROLES):
        raise CandidateError("candidate file inventory is incomplete")
    by_role: dict[str, dict[str, object]] = {}
    filenames: set[str] = set()
    for entry in files:
        if type(entry) is not dict or set(entry) != {"role", "filename", "byte_size", "sha256"}:
            raise CandidateError("candidate file entry is invalid")
        role = entry.get("role")
        if type(role) is not str or role not in FINAL_ROLES or role in by_role:
            raise CandidateError("candidate file roles are invalid or duplicated")
        filename = _require_safe_basename(entry.get("filename"), f"{role} filename")
        if filename in filenames:
            raise CandidateError("candidate filenames must be unique")
        filenames.add(filename)
        byte_size = entry.get("byte_size")
        if type(byte_size) is not int or byte_size < 1:
            raise CandidateError(f"candidate file size is invalid for {filename}")
        _require_digest(entry.get("sha256"), f"{filename} digest")
        path = root / filename
        if (
            not _is_regular_file(path)
            or path.stat().st_size != byte_size
            or _sha256(path) != entry["sha256"]
        ):
            raise CandidateError(f"candidate file digest mismatch: {filename}")
        by_role[role] = entry
    if tuple(by_role) != FINAL_ROLES:
        raise CandidateError("candidate file role order is not canonical")
    entries = list(root.iterdir())
    if any(not _is_regular_file(path) for path in entries):
        raise CandidateError("candidate directory contains a non-regular or symbolic-link entry")
    actual_inventory = {path.name for path in entries}
    if actual_inventory != filenames | {MANIFEST_NAME}:
        raise CandidateError("candidate directory inventory does not exactly match the manifest")

    if type(core) is not dict or set(core) != {
        "descriptor_filename",
        "descriptor_sha256",
        "registry_digest",
    }:
        raise CandidateError("candidate Core identity is invalid")
    _require_digest(core.get("registry_digest"), "candidate registry digest")
    if (
        core.get("descriptor_filename") != by_role["core_descriptor"]["filename"]
        or core.get("descriptor_sha256") != by_role["core_descriptor"]["sha256"]
    ):
        raise CandidateError("candidate Core descriptor identity is inconsistent")

    descriptor_path = root / str(by_role["core_descriptor"]["filename"])
    descriptor = _load_json(descriptor_path)
    if type(descriptor) is not dict or set(descriptor) != {
        "schema_version",
        "source_commit",
        "version",
        "registry_digest",
        "artifact",
        "framework_lock",
    }:
        raise CandidateError("Core install descriptor does not use the closed release schema")
    if (
        descriptor.get("schema_version") != 1
        or descriptor.get("source_commit") != source_commit
        or descriptor.get("version") != version
        or descriptor.get("registry_digest") != core.get("registry_digest")
        or descriptor.get("artifact") != by_role["core_wheel"]
        or descriptor.get("framework_lock") != by_role["framework_lock"]
    ):
        raise CandidateError("Core install descriptor does not bind the candidate wheel and lock")

    wheel_path = root / str(by_role["core_wheel"]["filename"])
    lock_path = root / str(by_role["framework_lock"]["filename"])
    _validate_wheel(wheel_path, version=version)
    _validate_framework_lock(lock_path, wheel_path, version=version)
    _validate_evidence(root)
    _validate_release_notes(
        root / str(by_role["release_notes"]["filename"]),
        source_commit=source_commit,
        version=version,
        architecture=str(architecture),
    )

    checksums_path = root / str(by_role["checksums"]["filename"])
    expected_subjects = sorted(
        (root / str(entry["filename"]) for role, entry in by_role.items() if role != "checksums"),
        key=lambda path: path.name,
    )
    expected_checksums = "".join(f"{_sha256(path)}  {path.name}\n" for path in expected_subjects)
    try:
        checksums = checksums_path.read_text(encoding="ascii")
    except (OSError, UnicodeDecodeError) as exc:
        raise CandidateError("SHA256SUMS is unreadable") from exc
    if checksums != expected_checksums:
        raise CandidateError("SHA256SUMS is not the canonical exact candidate checksum set")


def validate_candidate_manifest(
    manifest_path: Path,
    *,
    expected_source_commit: str | None = None,
) -> list[str]:
    try:
        _validate_candidate_manifest(
            manifest_path,
            expected_source_commit=expected_source_commit,
        )
    except CandidateError as exc:
        return [str(exc)]
    return []


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    create = subparsers.add_parser("create")
    create.add_argument("candidate_root", type=Path)
    create.add_argument("--source-commit", required=True)
    create.add_argument("--version", required=True)
    create.add_argument("--architecture", required=True)
    create.add_argument("--rust-target", required=True)
    create.add_argument("--registry-digest", required=True)
    validate = subparsers.add_parser("validate")
    validate.add_argument("manifest", type=Path)
    validate.add_argument("--expected-source-commit")
    args = parser.parse_args(argv)
    try:
        if args.command == "create":
            path = create_candidate_manifest(
                args.candidate_root,
                source_commit=args.source_commit,
                version=args.version,
                architecture=args.architecture,
                rust_target=args.rust_target,
                registry_digest=args.registry_digest,
            )
            print(path)
            return 0
        errors = validate_candidate_manifest(
            args.manifest,
            expected_source_commit=args.expected_source_commit,
        )
        if errors:
            raise CandidateError("; ".join(errors))
        print(f"OpenEvo candidate validation passed: {args.manifest}")
        return 0
    except CandidateError as exc:
        print(str(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
