"""Isolated, resumable Hugging Face snapshot download worker."""

from __future__ import annotations

import argparse
from pathlib import Path


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo-id", required=True)
    parser.add_argument("--revision", required=True)
    parser.add_argument("--local-dir", type=Path, required=True)
    parser.add_argument("--allow-pattern", action="append", default=[])
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    from huggingface_hub import snapshot_download

    snapshot_download(
        repo_id=args.repo_id,
        revision=args.revision,
        local_dir=args.local_dir,
        token=False,
        etag_timeout=30,
        max_workers=4,
        allow_patterns=args.allow_pattern,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
