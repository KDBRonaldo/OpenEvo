"""Launch the real Desktop provider against sealed Daemon/runtime assets.

This is a source-development launcher. It keeps the normal Tauri -> local
sidecar -> system OpenSSH -> remote Daemon boundary and never enables fixtures.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import stat
import subprocess
import sys
from pathlib import Path
from typing import NoReturn

from formal_desktop_assets import (
    FormalDevelopmentAssetError,
    prepare_formal_development_assets,
)


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
DESKTOP_ROOT = REPOSITORY_ROOT / "desktop"
ASSET_MANIFEST = "release-assets.json"
REQUIRED_ASSETS = (
    "core/framework-lock.json",
    "daemon/openevo-daemon-bundle.json",
    "daemon/openevo-daemon-linux-x86_64",
)


def _parser() -> argparse.ArgumentParser:
    configured_assets = os.environ.get("OPENEVO_DEV_RELEASE_ASSETS_ROOT")
    configured_helper = os.environ.get("OPENEVO_DEV_ASKPASS_HELPER")
    parser = argparse.ArgumentParser(
        description="Run OpenEvo Desktop with its real local sidecar and remote Daemon path."
    )
    parser.add_argument(
        "--release-assets-root",
        type=Path,
        default=Path(configured_assets) if configured_assets else None,
        help="Path to the staged openevo-release-assets directory.",
    )
    parser.add_argument(
        "--askpass-helper",
        type=Path,
        default=Path(configured_helper) if configured_helper else None,
        help="Path to the locally built openevo-ssh-askpass executable.",
    )
    parser.add_argument(
        "--prepare",
        action="store_true",
        help="Build and cache the current commit's formal Daemon/Desktop assets.",
    )
    parser.add_argument(
        "--managed-runtime-archive",
        type=Path,
        default=(
            Path(os.environ["OPENEVO_DEV_MANAGED_RUNTIME_ARCHIVE"])
            if os.environ.get("OPENEVO_DEV_MANAGED_RUNTIME_ARCHIVE")
            else None
        ),
        help="Optional verified managed-runtime archive; otherwise the pinned release is downloaded once.",
    )
    parser.add_argument(
        "--cache-root",
        type=Path,
        default=(
            Path(os.environ["OPENEVO_DEV_FORMAL_CACHE"])
            if os.environ.get("OPENEVO_DEV_FORMAL_CACHE")
            else Path.home() / ".cache/openevo/formal-desktop"
        ),
        help="Private cache used by --prepare.",
    )
    return parser


def _fail(message: str) -> NoReturn:
    raise SystemExit(f"OpenEvo live development is not ready: {message}")


def _read_asset_source_commit(root: Path) -> str:
    manifest_path = root / ASSET_MANIFEST
    try:
        raw = manifest_path.read_bytes()
        manifest = json.loads(raw.decode("utf-8", errors="strict"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        _fail(f"{manifest_path} is missing or invalid ({exc})")
    if not isinstance(manifest, dict) or set(manifest) != {
        "files",
        "schema_version",
        "source_commit",
    }:
        _fail("release-assets.json does not use the closed asset schema")
    source_commit = manifest.get("source_commit")
    if (
        manifest.get("schema_version") != 1
        or not isinstance(source_commit, str)
        or len(source_commit) != 40
        or any(character not in "0123456789abcdef" for character in source_commit)
    ):
        _fail("release-assets.json has an invalid source commit")
    for relative_path in REQUIRED_ASSETS:
        if not (root / relative_path).is_file():
            _fail(f"release asset is missing: {relative_path}")
    runtime_files = list((root / "runtime").glob("*.tar.gz"))
    if len(runtime_files) != 1 or not runtime_files[0].is_file():
        _fail("the staged managed runtime archive is missing")
    return source_commit


def _helper_identity(path: Path) -> tuple[str, int]:
    try:
        metadata = path.stat(follow_symlinks=False)
        payload = path.read_bytes()
    except OSError as exc:
        _fail(f"askpass helper is unavailable ({exc})")
    if (
        not stat.S_ISREG(metadata.st_mode)
        or metadata.st_nlink != 1
        or stat.S_IMODE(metadata.st_mode) != 0o755
        or metadata.st_size != len(payload)
        or metadata.st_size <= 0
    ):
        _fail("askpass helper must be one regular, link-count-one 0755 file")
    if hasattr(os, "geteuid") and metadata.st_uid not in {0, os.geteuid()}:
        _fail("askpass helper must be owned by the current user or root")
    return hashlib.sha256(payload).hexdigest(), len(payload)


def _checkout_identity() -> str:
    try:
        status = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=REPOSITORY_ROOT,
            check=True,
            capture_output=True,
            text=True,
        ).stdout
        source_commit = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=REPOSITORY_ROOT,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
    except (OSError, subprocess.CalledProcessError) as exc:
        _fail(f"the Git checkout identity is unavailable ({exc})")
    if status:
        _fail("commit or stash local changes before building the formal Daemon")
    if (
        len(source_commit) != 40
        or any(character not in "0123456789abcdef" for character in source_commit)
    ):
        _fail("the Git checkout does not have one full source commit")
    return source_commit


def main() -> int:
    if platform.system() not in {"Linux", "Darwin"}:
        _fail("the native sidecar requires Linux/WSL or macOS; Windows is unsupported")
    args = _parser().parse_args()
    checkout_commit = _checkout_identity()
    if args.prepare:
        if args.release_assets_root is not None or args.askpass_helper is not None:
            _fail("--prepare cannot be combined with prebuilt release assets or askpass helper")
        try:
            prepared = prepare_formal_development_assets(
                repository_root=REPOSITORY_ROOT,
                source_commit=checkout_commit,
                cache_root=args.cache_root,
                managed_runtime_archive=args.managed_runtime_archive,
            )
        except FormalDevelopmentAssetError as exc:
            _fail(str(exc))
        args.release_assets_root = prepared.release_assets_root
        args.askpass_helper = prepared.askpass_helper
    if args.release_assets_root is None:
        _fail("set OPENEVO_DEV_RELEASE_ASSETS_ROOT or pass --release-assets-root")
    if args.askpass_helper is None:
        _fail("set OPENEVO_DEV_ASKPASS_HELPER or pass --askpass-helper")

    try:
        assets_root = args.release_assets_root.expanduser().resolve(strict=True)
        helper = args.askpass_helper.expanduser().resolve(strict=True)
    except OSError as exc:
        _fail(f"a configured live-development input is unavailable ({exc})")
    if assets_root.name != "openevo-release-assets" or not assets_root.is_dir():
        _fail("release assets root must be the staged openevo-release-assets directory")
    source_commit = _read_asset_source_commit(assets_root)
    if source_commit != checkout_commit:
        _fail("release assets do not match the current Git commit")
    helper_sha256, helper_size = _helper_identity(helper)

    sidecar_args = [
        "-m",
        "desktop.server.launcher",
        "--source-commit",
        source_commit,
        "--build-channel",
        "development",
        "--release-assets-root",
        os.fspath(assets_root),
        "--ssh-askpass-helper-path",
        os.fspath(helper),
        "--ssh-askpass-helper-sha256",
        helper_sha256,
        "--ssh-askpass-helper-byte-size",
        str(helper_size),
    ]
    environment = os.environ.copy()
    environment.update(
        {
            # Keep the virtual-environment entry point intact. Resolving this
            # symlink escapes .venv and launches the bare base interpreter,
            # which cannot import the installed Desktop dependencies.
            "OPENEVO_DESKTOP_SIDECAR_PROGRAM": os.path.abspath(sys.executable),
            "OPENEVO_DESKTOP_SIDECAR_ARGS_JSON": json.dumps(
                sidecar_args, separators=(",", ":")
            ),
            "OPENEVO_DESKTOP_SIDECAR_WORKDIR": os.fspath(REPOSITORY_ROOT),
            # Source development launches the sidecar through the explicit
            # program/args contract above.  The release-only externalBin
            # entries are not built into src-tauri/binaries for this path and
            # must not make `tauri dev` validate packaged bundle inputs.
            "TAURI_CONFIG": json.dumps(
                {"bundle": {"externalBin": []}},
                separators=(",", ":"),
                sort_keys=True,
            ),
        }
    )
    print("Starting the real OpenEvo Desktop provider (fixtures disabled).")
    print(f"Daemon/runtime assets: {assets_root}")
    print(f"Asset source commit: {source_commit}")
    completed = subprocess.run(
        ["npm", "run", "tauri:dev"],
        cwd=DESKTOP_ROOT,
        env=environment,
        check=False,
    )
    return completed.returncode


if __name__ == "__main__":
    raise SystemExit(main())
