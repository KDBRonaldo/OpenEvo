from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
import re
import sys

BUILD_METADATA_RELATIVE_PATH = Path("desktop/packaging/sidecar-build-metadata.json")
BUILD_METADATA_MAX_BYTES = 1024
NATIVE_EXECUTABLE_PATH_ENV = "OPENEVO_NATIVE_EXECUTABLE_PATH"
NATIVE_LISTENER_FD_ENV = "OPENEVO_NATIVE_LISTENER_FD"
NATIVE_EXECUTABLE_FD_ENV = "OPENEVO_NATIVE_EXECUTABLE_FD"


@dataclass(frozen=True)
class _PackagedBuildMetadata:
    source_commit: str


def _strict_json_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    value: dict[str, object] = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("duplicate packaged sidecar build metadata key")
        value[key] = item
    return value


def _load_packaged_build_metadata() -> _PackagedBuildMetadata:
    bundle_root = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parents[2]))
    path = bundle_root / BUILD_METADATA_RELATIVE_PATH
    payload = path.read_bytes()
    if len(payload) > BUILD_METADATA_MAX_BYTES:
        raise ValueError("invalid packaged sidecar build metadata")
    try:
        value = json.loads(
            payload.decode("utf-8", errors="strict"),
            object_pairs_hook=_strict_json_object,
        )
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise ValueError("invalid packaged sidecar build metadata") from exc
    if type(value) is not dict or set(value) != {"schema_version", "source_commit"}:
        raise ValueError("invalid packaged sidecar build metadata")
    source_commit = value["source_commit"]
    if (
        value["schema_version"] != "1"
        or type(source_commit) is not str
        or re.fullmatch(r"[0-9a-f]{7,40}", source_commit) is None
        or set(source_commit) == {"0"}
    ):
        raise ValueError("invalid packaged sidecar build metadata")
    return _PackagedBuildMetadata(source_commit=source_commit)


if __name__ == "__main__":
    from openevo.deployment.system_executables import (
        OWNED_SUBPROCESS_BIRTH_ARGUMENT,
        run_packaged_owned_subprocess_birth,
    )

    if OWNED_SUBPROCESS_BIRTH_ARGUMENT in sys.argv:
        run_packaged_owned_subprocess_birth(sys.argv)
        raise SystemExit(126)
    from desktop.server.launcher import main

    if os.environ.pop(NATIVE_LISTENER_FD_ENV, None) != "3":
        raise ValueError("invalid packaged native listener handoff")
    if os.environ.pop(NATIVE_EXECUTABLE_FD_ENV, None) != "4":
        raise ValueError("invalid packaged native archive handoff")
    os.environ.pop(NATIVE_EXECUTABLE_PATH_ENV, None)
    metadata = _load_packaged_build_metadata()
    raise SystemExit(main(packaged_source_commit=metadata.source_commit))
