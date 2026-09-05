#!/usr/bin/env python3
"""Build a repository-free EvoLab ordinary-user launcher archive."""

from __future__ import annotations

import argparse
from pathlib import Path

from launcher_distribution import (
    LAUNCHER_DISTRIBUTION_SUFFIXES,
    LAUNCHER_TARGET_PLATFORMS,
    LauncherDistributionError,
    build_launcher_distribution,
)


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--commit", default="HEAD", help="exact committed release tree")
    parser.add_argument(
        "--platform",
        choices=sorted(LAUNCHER_TARGET_PLATFORMS),
        default="universal",
        help="local client platform encoded by the archive and installation guidance",
    )
    parser.add_argument(
        "--output",
        type=Path,
        help=f"output ending in {' or '.join(LAUNCHER_DISTRIBUTION_SUFFIXES)}; defaults under dist/",
    )
    args = parser.parse_args()
    default_name = {
        "windows": "evolab-launcher-windows.zip",
        "macos": "evolab-launcher-macos.tar.gz",
        "universal": "evolab-launcher.tar.gz",
    }[args.platform]
    output = args.output or REPOSITORY_ROOT / "dist" / default_name
    try:
        receipt = build_launcher_distribution(
            REPOSITORY_ROOT,
            output,
            commit=args.commit,
            target_platform=args.platform,
        )
    except LauncherDistributionError as exc:
        parser.error(str(exc))
    print(f"Built {receipt.path}")
    print(f"Version: {receipt.product_version}")
    print(f"Target platform: {args.platform}")
    print(f"Source commit: {receipt.source_commit}")
    print(f"Launcher distribution ID: {receipt.distribution_id}")
    print(f"Server release ID: {receipt.server_release_id}")
    print(f"SHA-256: {receipt.sha256}")
    print(f"Files: {receipt.file_count}; bytes: {receipt.byte_size}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
