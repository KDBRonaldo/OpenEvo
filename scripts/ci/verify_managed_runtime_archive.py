#!/usr/bin/env python3
"""Verify the exact offline managed Science runtime release archive."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from openevo.runtime.managed import (
    MANAGED_RUNTIME_ARCHIVE_RELEASE,
    ManagedRuntimeArchiveVerificationError,
    verify_managed_runtime_archive,
)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("archive", type=Path)
    args = parser.parse_args()
    try:
        authority = verify_managed_runtime_archive(
            args.archive,
            release=MANAGED_RUNTIME_ARCHIVE_RELEASE,
        )
    except ManagedRuntimeArchiveVerificationError as exc:
        raise SystemExit("managed runtime archive verification failed") from exc
    print(
        json.dumps(
            {
                "archive_sha256": MANAGED_RUNTIME_ARCHIVE_RELEASE.sha256,
                "archive_size": MANAGED_RUNTIME_ARCHIVE_RELEASE.byte_size,
                "config_id": authority.config_id,
                "oci_index_id": authority.oci_index_id,
                "managed_label": authority.managed_label,
                "platform": authority.platform,
                "schema_version": 1,
            },
            separators=(",", ":"),
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
