#!/usr/bin/env python3
"""Write sibling SHA256 checksum files for release artifacts."""

from __future__ import annotations

import argparse
import hashlib
from pathlib import Path


def write_sha256(path: Path) -> Path:
    checksum_path = path.with_name(f"{path.name}.sha256")
    checksum_path.write_text(
        f"{hashlib.sha256(path.read_bytes()).hexdigest()}  {path.name}\n",
        encoding="utf-8",
    )
    return checksum_path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("artifacts", type=Path, nargs="+")
    args = parser.parse_args(argv)

    for artifact in args.artifacts:
        if not artifact.is_file():
            parser.error(f"artifact does not exist: {artifact}")
        print(write_sha256(artifact))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
