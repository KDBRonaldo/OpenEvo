#!/usr/bin/env python3
"""Validate that built wheels carry the OpenEvo release identity and boundaries."""

from __future__ import annotations

import argparse
import configparser
import json
from io import BytesIO
import sys
from email.parser import Parser
from pathlib import Path
from zipfile import BadZipFile, ZipFile
import tomllib

EXPECTED_PROJECT_NAME = "openevo"
EXPECTED_SUMMARY = "OpenEvo Desktop and agent evolution orchestration."
EXPECTED_CONSOLE_SCRIPTS = {
    "openevo-backend": "openevo.backend.launcher:main",
}
REQUIRED_REMOTE_WHEEL_PREFIX = "openevo/wheels/"
FORBIDDEN_CORE_PACKAGE_PREFIXES = (
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
                "OpenEvo Core wheel must not package Desktop control-plane files under "
                f"{prefix}; found {packaged[0]}."
            )
    for filename in FORBIDDEN_CORE_PACKAGE_FILES:
        if filename in names:
            errors.append(
                "OpenEvo Core wheel must not expose the removed product CLI file "
                f"{filename}."
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
    except BadZipFile:
        return ["Nested remote-install wheel is not a readable zip archive."]

    errors: list[str] = []
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


def _desktop_package_metadata_paths() -> tuple[Path, ...]:
    candidates = (
        REPO_ROOT / "desktop" / "package.json",
        REPO_ROOT / "desktop" / "src-tauri" / "tauri.conf.json",
    )
    return tuple(path for path in candidates if path.exists())


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
        validate_release_wheel_artifacts(
            existing_artifact_paths,
            expected_version=expected_version,
        )
    )
    if not any(path.suffix == ".dmg" for path in existing_artifact_paths):
        errors.append("Release artifacts must include an OpenEvo Desktop macOS .dmg.")
    return errors


def validate_release_wheel_artifacts(
    wheel_paths: list[Path],
    *,
    expected_version: str,
) -> list[str]:
    expected_prefix = f"openevo-{expected_version}-"
    if any(
        path.name.startswith(expected_prefix) and path.suffix == ".whl"
        for path in wheel_paths
    ):
        return []
    return [
        "Release artifacts must include an exact OpenEvo wheel for remote install: "
        f"{expected_prefix}*.whl."
    ]


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        expected_version = _project_version()
    except Exception as exc:
        print(f"Could not read pyproject.toml version: {exc}", file=sys.stderr)
        return 1
    all_errors: list[str] = []
    all_errors.extend(validate_local_versions(expected_version))
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
