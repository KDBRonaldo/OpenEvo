#!/usr/bin/env python3
"""Build an integrity-checked OpenEvo self-hosted server release bundle."""

from __future__ import annotations

import argparse
from pathlib import Path

from openevo.release_bundle import BUNDLE_SUFFIX, ReleaseBundleError, build_release_bundle


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--commit", default="HEAD", help="exact committed tree to package")
    parser.add_argument(
        "--output",
        type=Path,
        help=f"output path ending in {BUNDLE_SUFFIX}; defaults under dist/",
    )
    args = parser.parse_args()
    output = args.output or REPOSITORY_ROOT / "dist" / f"openevo-self-hosted{BUNDLE_SUFFIX}"
    try:
        receipt = build_release_bundle(REPOSITORY_ROOT, output, commit=args.commit)
    except ReleaseBundleError as exc:
        parser.error(str(exc))
    print(f"Built {receipt.path}")
    print(f"Version: {receipt.product_version}")
    print(f"Source commit: {receipt.source_commit}")
    print(f"Release ID: {receipt.release_id}")
    print(f"SHA-256: {receipt.sha256}")
    print(f"Files: {receipt.file_count}; bytes: {receipt.byte_size}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
