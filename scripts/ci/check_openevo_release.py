#!/usr/bin/env python3
"""Validate that built wheels carry the OpenEvo release identity and assets."""

from __future__ import annotations

import argparse
import configparser
import sys
from email.parser import Parser
from pathlib import Path
from zipfile import BadZipFile, ZipFile

EXPECTED_PROJECT_NAME = "openevo"
EXPECTED_SUMMARY = "OpenEvo Desktop and agent evolution orchestration."
EXPECTED_CONSOLE_SCRIPTS = {
    "openevo": "openevo.cli:main",
    "polar": "polar.cli:main",
    "polar-evolution": "polar_evolution.cli:main",
}
REQUIRED_DESKTOP_INDEX = "openevo/desktop/web/index.html"
REQUIRED_DESKTOP_ASSET_PREFIX = "openevo/desktop/web/assets/"
FORBIDDEN_SHARED_DASHBOARD_PREFIX = "polar/platform/web/dist/"
FORBIDDEN_DESKTOP_CONTENT_MARKERS = (
    "Polar Dashboard",
    'href="/tasks"',
    'href=\\"/tasks\\"',
    ">Dashboard<",
)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--wheel",
        type=Path,
        nargs="+",
        required=True,
        help="One or more built wheel files to validate.",
    )
    return parser.parse_args(argv)


def validate_wheel(wheel_path: Path) -> list[str]:
    errors: list[str] = []
    if not wheel_path.exists():
        return [f"Wheel path does not exist: {wheel_path}"]
    if wheel_path.suffix != ".whl":
        errors.append(f"Release artifact should be a .whl file: {wheel_path}")

    try:
        with ZipFile(wheel_path) as wheel:
            names = set(wheel.namelist())
            errors.extend(_validate_metadata(wheel, names))
            errors.extend(_validate_entry_points(wheel, names))
            errors.extend(_validate_desktop_assets(wheel, names))
    except BadZipFile:
        errors.append(f"Wheel is not a readable zip archive: {wheel_path}")

    return errors


def _validate_metadata(wheel: ZipFile, names: set[str]) -> list[str]:
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


def _validate_desktop_assets(wheel: ZipFile, names: set[str]) -> list[str]:
    errors: list[str] = []
    if REQUIRED_DESKTOP_INDEX not in names:
        errors.append(f"Wheel is missing packaged Desktop index: {REQUIRED_DESKTOP_INDEX}.")
    if not any(
        name.startswith(REQUIRED_DESKTOP_ASSET_PREFIX) and not name.endswith("/")
        for name in names
    ):
        errors.append(
            "Wheel must include at least one packaged Desktop asset under "
            f"{REQUIRED_DESKTOP_ASSET_PREFIX}."
        )
    forbidden = sorted(
        name for name in names if name.startswith(FORBIDDEN_SHARED_DASHBOARD_PREFIX)
    )
    if forbidden:
        errors.append(
            "Wheel must not package shared dashboard assets under "
            f"{FORBIDDEN_SHARED_DASHBOARD_PREFIX}; found {forbidden[0]}."
        )
    errors.extend(_find_shared_dashboard_content(wheel, names))
    return errors


def _find_shared_dashboard_content(wheel: ZipFile, names: set[str]) -> list[str]:
    errors: list[str] = []
    for name in sorted(names):
        if not name.startswith("openevo/desktop/web/") or name.endswith("/"):
            continue
        try:
            content = _read_text(wheel, name)
        except UnicodeDecodeError:
            continue
        for marker in FORBIDDEN_DESKTOP_CONTENT_MARKERS:
            if marker in content:
                errors.append(
                    "Packaged OpenEvo Desktop asset contains shared dashboard marker "
                    f"`{marker}` in {name}."
                )
                break
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


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    all_errors: list[str] = []
    for wheel_path in args.wheel:
        errors = validate_wheel(wheel_path)
        if errors:
            all_errors.append(f"{wheel_path}:")
            all_errors.extend(f"  - {error}" for error in errors)

    if all_errors:
        print("\n".join(all_errors), file=sys.stderr)
        return 1

    print(f"OpenEvo release wheel checks passed for {len(args.wheel)} wheel(s).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
