#!/usr/bin/env python3
"""Smoke a packaged sidecar against an exact installed Core wheel backend."""

from __future__ import annotations

import argparse
import hashlib
from importlib import metadata, util
import json
from pathlib import Path
import re
import shutil
import tempfile
from types import ModuleType

import openevo
from openevo.backend.runtime_identity import default_core_service_root
from openevo.backend.service import ensure_core_service, stop_core_service


_SOURCE_COMMIT_PATTERN = re.compile(r"[0-9a-f]{40}")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _source_commit(value: str) -> str:
    commit = value.strip()
    if _SOURCE_COMMIT_PATTERN.fullmatch(commit) is None or set(commit) == {"0"}:
        raise RuntimeError("remote capability smoke could not resolve the source commit")
    return commit


def _load_sidecar_smoke() -> ModuleType:
    path = Path(__file__).with_name("smoke_openevo_desktop_sidecar.py")
    spec = util.spec_from_file_location("smoke_openevo_desktop_sidecar", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"could not load packaged sidecar smoke: {path}")
    module = util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def smoke(
    wheel_path: Path,
    sidecar_path: Path,
    *,
    source_commit: str,
    timeout_seconds: float,
) -> dict[str, str]:
    wheel = wheel_path.resolve(strict=True)
    sidecar = sidecar_path.resolve(strict=True)
    script_parents = Path(__file__).resolve().parents
    repository_src = script_parents[2] / "src" if len(script_parents) > 2 else None
    import_path = Path(openevo.__file__).resolve(strict=True)
    if repository_src is not None and import_path.is_relative_to(repository_src):
        raise RuntimeError("remote capability smoke imported Core from source")

    version = metadata.version("openevo")
    digest = _sha256(wheel)
    sidecar_smoke = _load_sidecar_smoke()
    with tempfile.TemporaryDirectory(
        prefix="openevo-remote-capability-smoke-"
    ) as temp_dir:
        root = Path(temp_dir)
        locked_wheel = root / wheel.name
        shutil.copy2(wheel, locked_wheel)
        lock_path = root / "framework-lock.json"
        lock_path.write_text(
            json.dumps(
                {
                    "schema_version": "1",
                    "distribution": "openevo",
                    "distribution_version": version,
                    "distribution_digest": digest,
                    "wheel_filename": locked_wheel.name,
                },
                sort_keys=True,
                separators=(",", ":"),
            ),
            encoding="utf-8",
        )
        service_root = default_core_service_root()
        attachment = ensure_core_service(
            service_root=service_root,
            framework_lock=lock_path,
            source_commit=_source_commit(source_commit),
            deadline_seconds=timeout_seconds,
        )
        base_url = f"http://127.0.0.1:{attachment.port}"
        headers = {"Authorization": f"Bearer {attachment.bearer_token}"}
        try:
            sidecar_smoke.smoke_sidecar(
                sidecar,
                timeout_seconds=timeout_seconds,
            )
            registry_digests: set[str] = set()
            for execution_mode in (
                "codex_subscription_transcript",
                "self-deployed",
            ):
                payload = sidecar_smoke._read_json(
                    f"{base_url}/v1/capabilities?execution_mode={execution_mode}",
                    headers=headers,
                )
                sidecar_smoke._assert_capabilities(
                    payload,
                    execution_mode=execution_mode,
                    expected_core_version=version,
                )
                registry_digests.add(payload["registry_digest"])
            if len(registry_digests) != 1:
                raise RuntimeError("exact Core release modes resolved different registries")
            registry_digest = next(iter(registry_digests))
        finally:
            if not attachment.attached:
                stop_core_service(
                    service_root=service_root,
                    deadline_seconds=min(timeout_seconds, 60.0),
                )

    return {
        "core_import_path": str(import_path),
        "registry_digest": registry_digest,
        "sidecar_path": str(sidecar),
        "wheel_sha256": digest,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--wheel", type=Path, required=True)
    parser.add_argument("--sidecar", type=Path, required=True)
    parser.add_argument("--source-commit", required=True)
    parser.add_argument("--timeout-seconds", type=float, default=60.0)
    args = parser.parse_args(argv)
    print(
        json.dumps(
            smoke(
                args.wheel,
                args.sidecar,
                source_commit=args.source_commit,
                timeout_seconds=args.timeout_seconds,
            ),
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
