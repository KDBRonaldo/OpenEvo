#!/usr/bin/env python3
"""Validate that built wheels carry the OpenEvo release identity and boundaries."""

from __future__ import annotations

import argparse
import configparser
import hashlib
import json
from io import BytesIO
import sys
from email.parser import Parser
from pathlib import Path
from zipfile import BadZipFile, ZipFile

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - older local interpreter support.
    import tomli as tomllib

EXPECTED_PROJECT_NAME = "openevo"
EXPECTED_SUMMARY = "OpenEvo Desktop and agent evolution orchestration."
EXPECTED_CONSOLE_SCRIPTS = {
    "openevo-backend": "openevo.backend.launcher:main",
    "openevo-core-service": "openevo.backend.service:main",
}
REQUIRED_REMOTE_WHEEL_PREFIX = "openevo/wheels/"
REQUIRED_RELEASE_NOTES = "release-notes.md"
RELEASE_BINARY_SUFFIXES = (".whl", ".dmg")
ALLOWED_DMG_TARGETS = {"aarch64", "x64", "universal"}
FORBIDDEN_CORE_PACKAGE_PREFIXES = (
    "openevo_terminal_bench/",
    "benchmarks/terminal_bench/",
    "openevo/desktop/",
    "openevo/sidecar/",
    "desktop/server/",
    "desktop/sidecar/",
    "desktop/src/",
    "desktop/src-tauri/",
    "desktop/packaging/web/",
)
FORBIDDEN_CORE_PACKAGE_FILES = (
    "openevo/cli.py",
)
FORBIDDEN_LEGACY_CORE_MODULE_FILES = frozenset(
    {
        "openevo/evolution/terminal_bench_bridge.py",
        "openevo/evolution/terminal_bench_local_parametric.py",
        "openevo/evolution/terminal_bench_per_task.py",
        "openevo/evolution/terminal_bench_task_local_parametric.py",
    }
)
FORBIDDEN_SHARED_DASHBOARD_PREFIXES = (
    "openevo/platform/desktop/dist/",
    "openevo/platform/web/dist/",
)
REPO_ROOT = Path(__file__).resolve().parents[2]
SRC_ROOT = REPO_ROOT / "src"


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--wheel",
        type=Path,
        nargs="+",
        required=True,
        help="One or more built wheel files to validate.",
    )
    parser.add_argument(
        "--artifact",
        type=Path,
        nargs="+",
        default=None,
        help=(
            "Complete release artifact list to validate. When omitted, only wheel "
            "contents are checked."
        ),
    )
    return parser.parse_args(argv)


def validate_wheel(wheel_path: Path, *, expected_version: str) -> list[str]:
    errors: list[str] = []
    if not wheel_path.exists():
        return [f"Wheel path does not exist: {wheel_path}"]
    if wheel_path.suffix != ".whl":
        errors.append(f"Release artifact should be a .whl file: {wheel_path}")

    try:
        with ZipFile(wheel_path) as wheel:
            names = set(wheel.namelist())
            errors.extend(_validate_metadata(wheel, names, expected_version))
            errors.extend(_validate_entry_points(wheel, names))
            errors.extend(_validate_core_package_boundaries(names))
            errors.extend(_validate_packaged_remote_install_wheel(wheel, names, expected_version))
    except BadZipFile:
        errors.append(f"Wheel is not a readable zip archive: {wheel_path}")

    return errors


def _validate_metadata(
    wheel: ZipFile,
    names: set[str],
    expected_version: str,
) -> list[str]:
    errors: list[str] = []
    metadata_path = _find_dist_info_file(names, "METADATA")
    if metadata_path is None:
        return ["Wheel is missing a .dist-info/METADATA file."]

    metadata = Parser().parsestr(_read_text(wheel, metadata_path))
    if metadata.get("Name") != EXPECTED_PROJECT_NAME:
        errors.append(
            f"METADATA Name should be `{EXPECTED_PROJECT_NAME}`, got "
            f"`{metadata.get('Name') or '<missing>'}`."
        )
    if metadata.get("Summary") != EXPECTED_SUMMARY:
        errors.append(
            f"METADATA Summary should be `{EXPECTED_SUMMARY}`, got "
            f"`{metadata.get('Summary') or '<missing>'}`."
        )
    if metadata.get("Version") != expected_version:
        errors.append(
            f"METADATA Version should be `{expected_version}`, got "
            f"`{metadata.get('Version') or '<missing>'}`."
        )
    return errors


def _validate_entry_points(wheel: ZipFile, names: set[str]) -> list[str]:
    errors: list[str] = []
    entry_points_path = _find_dist_info_file(names, "entry_points.txt")
    if entry_points_path is None:
        return ["Wheel is missing a .dist-info/entry_points.txt file."]

    parser = configparser.ConfigParser()
    parser.optionxform = str
    parser.read_string(_read_text(wheel, entry_points_path))
    console_scripts = (
        dict(parser["console_scripts"]) if parser.has_section("console_scripts") else {}
    )
    if console_scripts != EXPECTED_CONSOLE_SCRIPTS:
        unexpected = sorted(
            script for script in console_scripts if script not in EXPECTED_CONSOLE_SCRIPTS
        )
        if unexpected:
            errors.append(
                "Console scripts must expose only OpenEvo Core Backend entrypoints; "
                f"unexpected script(s): {', '.join(unexpected)}."
            )
    for script, expected_target in EXPECTED_CONSOLE_SCRIPTS.items():
        actual_target = console_scripts.get(script)
        if actual_target != expected_target:
            errors.append(
                f"Console script `{script} = {expected_target}` is required, got "
                f"`{actual_target or '<missing>'}`."
            )
    return errors


def _validate_core_package_boundaries(names: set[str]) -> list[str]:
    errors: list[str] = []
    for prefix in FORBIDDEN_CORE_PACKAGE_PREFIXES:
        packaged = sorted(name for name in names if name.startswith(prefix))
        if packaged:
            errors.append(
                "OpenEvo Core wheel must not package non-Core files under "
                f"{prefix}; found {packaged[0]}."
            )
    for filename in FORBIDDEN_CORE_PACKAGE_FILES:
        if filename in names:
            errors.append(
                "OpenEvo Core wheel must not expose the removed product CLI file "
                f"{filename}."
            )
    legacy_modules = sorted(names & FORBIDDEN_LEGACY_CORE_MODULE_FILES)
    if legacy_modules:
        errors.append(
            "OpenEvo Core wheel must not package removed Terminal Bench modules: "
            f"{legacy_modules}."
        )
    for prefix in FORBIDDEN_SHARED_DASHBOARD_PREFIXES:
        forbidden_dashboard = sorted(name for name in names if name.startswith(prefix))
        if forbidden_dashboard:
            errors.append(
                "OpenEvo Core wheel must not package shared dashboard assets under "
                f"{prefix}; found {forbidden_dashboard[0]}."
            )
    return errors


def _validate_packaged_remote_install_wheel(
    wheel: ZipFile,
    names: set[str],
    expected_version: str,
) -> list[str]:
    expected_prefix = f"{REQUIRED_REMOTE_WHEEL_PREFIX}openevo-{expected_version}-"
    matches = sorted(
        name for name in names if name.startswith(expected_prefix) and name.endswith(".whl")
    )
    if not matches:
        return [
            "Wheel must include a bundled remote-install wheel under "
            f"`{expected_prefix}*.whl`."
        ]

    errors: list[str] = []
    for nested_name in matches:
        nested_errors = _validate_nested_remote_install_wheel(
            wheel.read(nested_name),
            expected_version=expected_version,
        )
        if not nested_errors:
            return []
        errors.extend(f"{nested_name}: {error}" for error in nested_errors)
    return errors


def _validate_nested_remote_install_wheel(
    contents: bytes,
    *,
    expected_version: str,
) -> list[str]:
    try:
        with ZipFile(BytesIO(contents)) as nested:
            names = set(nested.namelist())
            metadata_path = _find_dist_info_file(names, "METADATA")
            if metadata_path is None:
                return ["Nested remote-install wheel is missing a .dist-info/METADATA file."]
            metadata = Parser().parsestr(_read_text(nested, metadata_path))
            boundary_errors = _validate_core_package_boundaries(names)
    except BadZipFile:
        return ["Nested remote-install wheel is not a readable zip archive."]

    errors: list[str] = []
    errors.extend(boundary_errors)
    if metadata.get("Name") != EXPECTED_PROJECT_NAME:
        errors.append(
            "Nested remote-install wheel METADATA Name should be "
            f"`{EXPECTED_PROJECT_NAME}`, got `{metadata.get('Name') or '<missing>'}`."
        )
    if metadata.get("Version") != expected_version:
        errors.append(
            "Nested remote-install wheel METADATA Version should be "
            f"`{expected_version}`, got `{metadata.get('Version') or '<missing>'}`."
        )
    return errors


def _find_dist_info_file(names: set[str], filename: str) -> str | None:
    matches = sorted(
        name
        for name in names
        if name.endswith(f".dist-info/{filename}") and name.count(".dist-info/") == 1
    )
    return matches[0] if matches else None


def _read_text(wheel: ZipFile, name: str) -> str:
    return wheel.read(name).decode("utf-8")


def _project_version() -> str:
    pyproject_path = REPO_ROOT / "pyproject.toml"
    with pyproject_path.open("rb") as handle:
        pyproject = tomllib.load(handle)
    version = pyproject.get("project", {}).get("version")
    if not isinstance(version, str) or not version.strip():
        raise ValueError("pyproject.toml is missing project.version.")
    return version


def _openevo_package_version() -> str:
    if str(SRC_ROOT) not in sys.path:
        sys.path.insert(0, str(SRC_ROOT))
    from openevo import __version__

    return __version__


def validate_local_versions(expected_version: str) -> list[str]:
    errors: list[str] = []
    openevo_version = _openevo_package_version()
    if openevo_version != expected_version:
        errors.append(
            f"openevo.__version__ should be `{expected_version}`, got "
            f"`{openevo_version}`."
        )

    for package_json in _desktop_package_metadata_paths():
        try:
            payload = json.loads(package_json.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            errors.append(f"{package_json} is not valid JSON: {exc}")
            continue
        package_version = payload.get("version")
        if package_version != expected_version:
            errors.append(
                f"{package_json} version should be `{expected_version}`, got "
                f"`{package_version or '<missing>'}`."
            )
    return errors


def validate_v019_contract_manifest(
    repo_root: Path = REPO_ROOT,
    *,
    expected_version: str,
) -> list[str]:
    """Require exact Core v2 mutation authority for the 0.1.9 release."""

    if expected_version != "0.1.9":
        return []

    manifest_path = repo_root / "desktop/release-contract.json"
    try:
        payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return ["The 0.1.9 release contract manifest is missing or unreadable."]
    if not isinstance(payload, dict):
        return ["The 0.1.9 release contract manifest must be a JSON object."]

    if str(SRC_ROOT) not in sys.path:
        sys.path.insert(0, str(SRC_ROOT))
    from openevo.backend.contracts.v2.snapshots import (
        events_schema_sha256,
        openapi_sha256,
    )

    policy = payload.get("v019")
    if not isinstance(policy, dict):
        return ["The 0.1.9 release contract manifest is missing its closed v019 policy."]

    errors: list[str] = []
    if policy.get("release_version") != "0.1.9":
        errors.append("The v019 release authority must bind release version 0.1.9.")
    if policy.get("core_control_mutation_major") != 2:
        errors.append("The 0.1.9 release must require Core Control API v2 for mutation.")
    if policy.get("accepted_core_openapi_digests") != [openapi_sha256()]:
        errors.append(
            "The 0.1.9 release manifest must pin the exact generated Core v2 OpenAPI digest."
        )
    if policy.get("accepted_core_event_schema_digests") != [events_schema_sha256()]:
        errors.append(
            "The 0.1.9 release manifest must pin the exact generated Core v2 event schema digest."
        )
    if policy.get("allow_legacy_route_fallback") is not False:
        errors.append("The 0.1.9 release manifest must forbid legacy route fallback.")
    return errors


def _desktop_package_metadata_paths() -> tuple[Path, ...]:
    candidates = (
        REPO_ROOT / "desktop" / "package.json",
        REPO_ROOT / "desktop" / "src-tauri" / "tauri.conf.json",
    )
    return tuple(path for path in candidates if path.exists())


def validate_unsigned_macos_release_policy(repo_root: Path = REPO_ROOT) -> list[str]:
    config_paths = (
        repo_root / "desktop/src-tauri/tauri.conf.json",
        repo_root / "desktop/src-tauri/tauri.release.conf.json",
    )
    configs: list[dict[str, object]] = []
    for path in config_paths:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            return [f"Unsigned macOS release configuration is unreadable: {path.name}."]
        if not isinstance(payload, dict):
            return [f"Unsigned macOS release configuration is invalid: {path.name}."]
        configs.append(payload)

    macos_values: list[dict[str, object]] = []
    for path, config in zip(config_paths, configs, strict=True):
        bundle = config.get("bundle")
        macos = bundle.get("macOS") if isinstance(bundle, dict) else None
        if not isinstance(macos, dict):
            return [f"Unsigned macOS release policy is missing from {path.name}."]
        macos_values.append(macos)
    base_macos, release_macos = macos_values
    merged_macos = {**base_macos, **release_macos}

    errors: list[str] = []
    if base_macos.get("signingIdentity") != "-":
        errors.append("Unsigned macOS release must use the ad-hoc signing identity.")
    if release_macos.get("hardenedRuntime") is not False:
        errors.append("Unsigned macOS release must explicitly disable hardened runtime.")
    if "entitlements" in merged_macos:
        errors.append("Unsigned macOS release must not configure entitlements.")
    return errors


def validate_release_artifacts(
    artifact_paths: list[Path],
    *,
    expected_version: str,
) -> list[str]:
    errors: list[str] = []
    existing_artifact_paths = []
    for path in artifact_paths:
        if not path.exists():
            errors.append(f"Release artifact does not exist: {path}")
            continue
        existing_artifact_paths.append(path)

    errors.extend(
        validate_allowed_release_artifact_set(
            existing_artifact_paths,
            expected_version=expected_version,
        )
    )
    errors.extend(
        validate_release_wheel_artifacts(
            existing_artifact_paths,
            expected_version=expected_version,
        )
    )
    errors.extend(
        validate_release_dmg_artifacts(
            existing_artifact_paths,
            expected_version=expected_version,
        )
    )
    errors.extend(validate_release_notes_artifacts(existing_artifact_paths))
    errors.extend(validate_release_checksum_artifacts(existing_artifact_paths))
    return errors


def validate_release_wheel_artifacts(
    wheel_paths: list[Path],
    *,
    expected_version: str,
) -> list[str]:
    expected_prefix = f"openevo-{expected_version}-"
    matches = [
        path
        for path in wheel_paths
        if path.name.startswith(expected_prefix) and path.suffix == ".whl"
    ]
    if len(matches) == 1:
        return []
    if len(matches) > 1:
        return [
            "Release artifacts must include exactly one exact OpenEvo wheel for "
            f"remote install, found: {', '.join(path.name for path in matches)}."
        ]
    return [
        "Release artifacts must include an exact OpenEvo wheel for remote install: "
        f"{expected_prefix}*.whl."
    ]


def validate_release_dmg_artifacts(
    artifact_paths: list[Path],
    *,
    expected_version: str,
) -> list[str]:
    matches = [
        path
        for path in artifact_paths
        if _allowed_dmg_name(path.name, expected_version=expected_version)
    ]
    if len(matches) == 1:
        return []
    if len(matches) > 1:
        return [
            "Release artifacts must include exactly one OpenEvo Desktop macOS .dmg, "
            f"found: {', '.join(path.name for path in matches)}."
        ]
    return ["Release artifacts must include an OpenEvo Desktop macOS .dmg."]


def validate_allowed_release_artifact_set(
    artifact_paths: list[Path],
    *,
    expected_version: str,
) -> list[str]:
    errors: list[str] = []
    for path in artifact_paths:
        if _allowed_release_artifact(path, expected_version=expected_version):
            continue
        errors.append(f"Unexpected release artifact: {path.name}")
    return errors


def _allowed_release_artifact(path: Path, *, expected_version: str) -> bool:
    expected_wheel_prefix = f"openevo-{expected_version}-"
    if path.name == REQUIRED_RELEASE_NOTES:
        return True
    if path.suffix == ".whl":
        return path.name.startswith(expected_wheel_prefix)
    if path.suffix == ".dmg":
        return _allowed_dmg_name(path.name, expected_version=expected_version)
    if path.name.endswith(".sha256"):
        subject = path.name.removesuffix(".sha256")
        return (
            subject.startswith(expected_wheel_prefix)
            and subject.endswith(".whl")
        ) or _allowed_dmg_name(subject, expected_version=expected_version)
    return False


def _allowed_dmg_name(name: str, *, expected_version: str) -> bool:
    prefix = f"OpenEvo-Desktop-{expected_version}-"
    if not (name.startswith(prefix) and name.endswith(".dmg")):
        return False
    target = name.removeprefix(prefix).removesuffix(".dmg")
    return target in ALLOWED_DMG_TARGETS


def _is_release_binary(path: Path) -> bool:
    return path.suffix in RELEASE_BINARY_SUFFIXES


def _is_release_checksum(path: Path) -> bool:
    return path.name.endswith(".sha256")


def _expected_checksum_path(path: Path) -> Path:
    return path.with_name(f"{path.name}.sha256")


def _checksum_subject_path(path: Path) -> Path:
    return path.with_name(path.name.removesuffix(".sha256"))


def validate_release_notes_artifacts(artifact_paths: list[Path]) -> list[str]:
    notes = [path for path in artifact_paths if path.name == REQUIRED_RELEASE_NOTES]
    if not notes:
        return [f"Release artifacts must include {REQUIRED_RELEASE_NOTES}."]

    errors: list[str] = []
    for note in notes:
        try:
            text = note.read_text(encoding="utf-8")
        except OSError as exc:
            errors.append(f"Could not read release notes {note}: {exc}")
            continue
        if "OpenEvo" not in text or not text.strip():
            errors.append(f"{note} must contain non-empty OpenEvo release notes.")
    return errors


def validate_release_checksum_artifacts(artifact_paths: list[Path]) -> list[str]:
    artifact_set = set(artifact_paths)
    binary_artifacts = [path for path in artifact_paths if _is_release_binary(path)]
    checksum_artifacts = [path for path in artifact_paths if _is_release_checksum(path)]

    errors: list[str] = []
    for artifact in binary_artifacts:
        checksum_path = _expected_checksum_path(artifact)
        if checksum_path not in artifact_set:
            errors.append(
                f"Release artifact {artifact.name} must have a sibling "
                f"{artifact.name}.sha256 checksum."
            )
            continue
        errors.extend(_validate_sha256_checksum(artifact, checksum_path))
    binary_set = set(binary_artifacts)
    for checksum_path in checksum_artifacts:
        if _checksum_subject_path(checksum_path) not in binary_set:
            errors.append(
                f"Checksum artifact {checksum_path.name} must have a sibling "
                f"{checksum_path.name.removesuffix('.sha256')} artifact."
            )
    return errors


def _validate_sha256_checksum(artifact: Path, checksum_path: Path) -> list[str]:
    try:
        parts = checksum_path.read_text(encoding="utf-8").strip().split(maxsplit=1)
    except OSError as exc:
        return [f"Could not read checksum {checksum_path}: {exc}"]
    if len(parts) < 2:
        return [f"{checksum_path} must use '<sha256>  <filename>' format."]

    expected_hash, expected_filename = parts[0].lower(), parts[1].strip()
    errors: list[str] = []
    if len(expected_hash) != 64 or any(char not in "0123456789abcdef" for char in expected_hash):
        errors.append(f"{checksum_path} does not contain a valid SHA256 digest.")
    if expected_filename != artifact.name:
        errors.append(
            f"{checksum_path} should reference `{artifact.name}`, got "
            f"`{expected_filename}`."
        )
    if errors:
        return errors

    actual_hash = hashlib.sha256(artifact.read_bytes()).hexdigest()
    if actual_hash != expected_hash:
        errors.append(
            f"{checksum_path} checksum mismatch for {artifact.name}: "
            f"expected {expected_hash}, got {actual_hash}."
        )
    return errors


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        expected_version = _project_version()
    except Exception as exc:
        print(f"Could not read pyproject.toml version: {exc}", file=sys.stderr)
        return 1
    all_errors: list[str] = []
    all_errors.extend(validate_local_versions(expected_version))
    all_errors.extend(validate_v019_contract_manifest(expected_version=expected_version))
    all_errors.extend(validate_unsigned_macos_release_policy())
    if args.artifact is not None:
        all_errors.extend(
            validate_release_artifacts(args.artifact, expected_version=expected_version)
        )
    else:
        all_errors.extend(
            validate_release_wheel_artifacts(args.wheel, expected_version=expected_version)
        )
    for wheel_path in args.wheel:
        errors = validate_wheel(wheel_path, expected_version=expected_version)
        if errors:
            all_errors.append(f"{wheel_path}:")
            all_errors.extend(f"  - {error}" for error in errors)

    if all_errors:
        print("\n".join(all_errors), file=sys.stderr)
        return 1

    if args.artifact is not None:
        print(
            "OpenEvo release checks passed for "
            f"{len(args.wheel)} wheel(s) and {len(args.artifact)} artifact(s)."
        )
    else:
        print(f"OpenEvo release wheel checks passed for {len(args.wheel)} wheel(s).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
