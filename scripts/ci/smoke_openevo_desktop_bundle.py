#!/usr/bin/env python3
"""Smoke the sidecar executable bundled inside an OpenEvo Desktop macOS app."""

from __future__ import annotations

import argparse
from pathlib import Path
import sys

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from smoke_openevo_desktop_sidecar import SmokeFailure, smoke_sidecar  # noqa: E402


SIDECAR_NAME = "openevo-desktop-sidecar"
APP_BUNDLE_NAME = "OpenEvo Desktop.app"


def find_bundled_sidecar(bundle_root: Path) -> Path:
    if not bundle_root.exists():
        raise SmokeFailure(f"OpenEvo Desktop bundle root does not exist: {bundle_root}")

    app_bundle = bundle_root if bundle_root.name == APP_BUNDLE_NAME else bundle_root / APP_BUNDLE_NAME
    if not app_bundle.is_dir():
        raise SmokeFailure(f"No {APP_BUNDLE_NAME} bundle found under {bundle_root}")

    contents = app_bundle / "Contents"
    candidates = [
        contents / "MacOS" / SIDECAR_NAME,
        contents / "Resources" / SIDECAR_NAME,
        contents / "Resources" / "binaries" / SIDECAR_NAME,
    ]
    candidates = [path for path in candidates if path.is_file()]
    if not candidates:
        raise SmokeFailure(
            "No bundled OpenEvo Desktop sidecar executable found under "
            f"{app_bundle}"
        )

    for path in candidates:
        if path.stat().st_mode & 0o111:
            return path

    candidate_names = ", ".join(str(path) for path in candidates)
    raise SmokeFailure(
        "Bundled OpenEvo Desktop sidecar candidate(s) are not executable: "
        f"{candidate_names}"
    )


def smoke_bundle(bundle_root: Path, *, timeout_seconds: float) -> Path:
    sidecar = find_bundled_sidecar(bundle_root)
    smoke_sidecar(sidecar, timeout_seconds=timeout_seconds)
    return sidecar


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("bundle_root", type=Path)
    parser.add_argument("--timeout-seconds", type=float, default=30.0)
    args = parser.parse_args(argv)

    try:
        sidecar = smoke_bundle(args.bundle_root, timeout_seconds=args.timeout_seconds)
    except SmokeFailure as exc:
        print(str(exc), file=sys.stderr)
        return 1
    print(f"OpenEvo Desktop app bundle sidecar smoke passed: {sidecar}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
