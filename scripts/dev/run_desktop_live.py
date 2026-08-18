"""Launch the real Desktop provider against sealed Daemon/runtime assets.

This is a source-development launcher. It keeps the normal local Sidecar ->
system OpenSSH -> remote Daemon boundary and can host the renderer in either
Tauri or the system browser. It never enables fixtures.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import shutil
import stat
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import NoReturn

try:
    from .formal_desktop_assets import (
        FormalDevelopmentAssetError,
        prepare_formal_development_assets,
    )
except ImportError:  # Direct script execution used by the npm development command.
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


@dataclass(frozen=True, slots=True)
class CheckoutIdentity:
    source_commit: str
    development_snapshot_sha256: str | None

    @property
    def is_dirty(self) -> bool:
        return self.development_snapshot_sha256 is not None


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
        help=(
            "Build and cache the current source-development snapshot's "
            "Daemon/Desktop assets."
        ),
    )
    parser.add_argument(
        "--browser",
        action="store_true",
        help="Host the formal Desktop UI on localhost and open the system browser instead of Tauri.",
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


def _parse_node_version(output: str) -> tuple[int, int, int] | None:
    value = output.strip()
    if value.startswith("v"):
        value = value[1:]
    parts = value.split(".")
    if len(parts) < 3:
        return None
    try:
        return int(parts[0]), int(parts[1]), int(parts[2].split("-", 1)[0])
    except ValueError:
        return None


def _vite_compatible_node(version: tuple[int, int, int] | None) -> bool:
    if version is None:
        return False
    major, minor, _patch = version
    return (major == 20 and minor >= 19) or (major == 22 and minor >= 12) or major > 22


def _node_version(command: list[str]) -> tuple[int, int, int] | None:
    try:
        completed = subprocess.run(
            command,
            check=False,
            capture_output=True,
            text=True,
        )
    except OSError:
        return None
    if completed.returncode != 0:
        return None
    return _parse_node_version(completed.stdout)


def _browser_npm_command() -> list[str]:
    """Select a Vite-compatible npm without making WSL users duplicate Node."""

    native_version = _node_version(["node", "--version"])
    if _vite_compatible_node(native_version):
        return ["npm"]

    # A Windows checkout is commonly developed from WSL while Node is installed
    # on Windows.  WSL interop translates the working directory for cmd.exe, so
    # reusing that supported Node installation is both faster and less surprising
    # than requiring a second Node installation inside WSL.
    is_wsl = bool(os.environ.get("WSL_DISTRO_NAME")) or "microsoft" in platform.release().lower()
    if is_wsl:
        windows_version = _node_version(["cmd.exe", "/d", "/c", "node", "--version"])
        if _vite_compatible_node(windows_version):
            return ["cmd.exe", "/d", "/c", "npm"]

    rendered = (
        ".".join(str(part) for part in native_version)
        if native_version is not None
        else "unavailable"
    )
    _fail(
        "the browser renderer requires Node.js 20.19+ or 22.12+ "
        f"(selected Node: {rendered})"
    )


def _ensure_user_tool_on_path(name: str, *user_candidates: Path) -> Path:
    """Make a user-local build tool visible to nested build processes."""

    discovered = shutil.which(name)
    candidates = [] if discovered is None else [Path(discovered)]
    candidates.extend(user_candidates)
    for candidate in candidates:
        try:
            resolved = candidate.expanduser().resolve(strict=True)
        except OSError:
            continue
        if not resolved.is_file() or not os.access(resolved, os.X_OK):
            continue
        path_entries = os.environ.get("PATH", "").split(os.pathsep)
        parent = os.fspath(resolved.parent)
        if parent not in path_entries:
            os.environ["PATH"] = os.pathsep.join(
                entry for entry in (parent, *path_entries) if entry
            )
        return resolved
    _fail(f"{name} is required to build the formal Desktop assets")


def _ensure_uv_on_path() -> Path:
    """Make the user-local uv installation visible to nested build tools.

    Browser development may be launched by a non-login WSL process.  In that
    case the launcher itself can be invoked through an absolute uv path while
    the child Daemon builder cannot resolve the plain ``uv`` command.  Resolve
    the same trusted user installation once and propagate its directory.
    """

    return _ensure_user_tool_on_path(
        "uv",
        Path.home() / ".local/bin/uv",
        Path.home() / ".cargo/bin/uv",
    )


def _ensure_cargo_on_path() -> Path:
    """Resolve rustup's default user-local Cargo installation."""

    return _ensure_user_tool_on_path("cargo", Path.home() / ".cargo/bin/cargo")


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


def _run_git_bytes(repository_root: Path, *arguments: str) -> bytes:
    try:
        return subprocess.run(
            ["git", *arguments],
            cwd=repository_root,
            check=True,
            capture_output=True,
        ).stdout
    except (OSError, subprocess.CalledProcessError) as exc:
        _fail(f"the Git checkout identity is unavailable ({exc})")


def _dirty_snapshot_sha256(
    repository_root: Path,
    *,
    status_payload: bytes,
) -> str:
    """Hash every Git-visible dirty byte used by a source-development build."""

    digest = hashlib.sha256()
    digest.update(b"openevo-source-development-snapshot-v1\0")
    digest.update(len(status_payload).to_bytes(8, "big"))
    digest.update(status_payload)
    diff_payload = _run_git_bytes(
        repository_root,
        "diff",
        "--binary",
        "--no-ext-diff",
        "HEAD",
        "--",
    )
    digest.update(len(diff_payload).to_bytes(8, "big"))
    digest.update(diff_payload)

    untracked_payload = _run_git_bytes(
        repository_root,
        "ls-files",
        "--others",
        "--exclude-standard",
        "-z",
    )
    untracked_paths = [item for item in untracked_payload.split(b"\0") if item]
    for encoded_relative_path in sorted(untracked_paths):
        relative_path = Path(os.fsdecode(encoded_relative_path))
        if relative_path.is_absolute() or ".." in relative_path.parts:
            _fail("Git reported an unsafe untracked source path")
        source_path = repository_root / relative_path
        try:
            metadata = source_path.lstat()
            if stat.S_ISLNK(metadata.st_mode):
                content = os.fsencode(os.readlink(source_path))
                kind = b"symlink"
            elif stat.S_ISREG(metadata.st_mode):
                content_digest = hashlib.sha256()
                with source_path.open("rb") as source:
                    while chunk := source.read(1024 * 1024):
                        content_digest.update(chunk)
                content = content_digest.digest()
                kind = b"file"
            else:
                _fail("dirty source snapshots support only files and symlinks")
        except OSError as exc:
            _fail(f"an untracked source file could not be hashed ({exc})")
        digest.update(len(encoded_relative_path).to_bytes(8, "big"))
        digest.update(encoded_relative_path)
        digest.update(kind)
        digest.update((metadata.st_mode & 0o111).to_bytes(2, "big"))
        digest.update(metadata.st_size.to_bytes(8, "big"))
        digest.update(content)
    return digest.hexdigest()


def _checkout_identity(repository_root: Path = REPOSITORY_ROOT) -> CheckoutIdentity:
    try:
        status = subprocess.run(
            ["git", "status", "--porcelain=v1", "-z", "--untracked-files=all"],
            cwd=repository_root,
            check=True,
            capture_output=True,
        ).stdout
        source_commit = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=repository_root,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
    except (OSError, subprocess.CalledProcessError) as exc:
        _fail(f"the Git checkout identity is unavailable ({exc})")
    if (
        len(source_commit) != 40
        or any(character not in "0123456789abcdef" for character in source_commit)
    ):
        _fail("the Git checkout does not have one full source commit")
    snapshot_sha256 = None
    if status:
        snapshot_sha256 = _dirty_snapshot_sha256(
            repository_root,
            status_payload=status,
        )
    return CheckoutIdentity(
        source_commit=source_commit,
        development_snapshot_sha256=snapshot_sha256,
    )


def main() -> int:
    if platform.system() not in {"Linux", "Darwin"}:
        _fail("the native sidecar requires Linux/WSL or macOS; Windows is unsupported")
    args = _parser().parse_args()
    if args.prepare:
        _ensure_uv_on_path()
        _ensure_cargo_on_path()
    checkout = _checkout_identity()
    checkout_commit = checkout.source_commit
    if checkout.is_dirty:
        print(
            "Source-development dirty build: "
            f"HEAD {checkout_commit[:12]}, snapshot "
            f"{checkout.development_snapshot_sha256[:12]}."
        )
        print("This build is development-only and cannot be used as a release build.")
    if checkout.is_dirty and not args.prepare:
        _fail("a dirty checkout must use --prepare so its Daemon assets are rebuilt")
    if args.prepare:
        if args.release_assets_root is not None or args.askpass_helper is not None:
            _fail("--prepare cannot be combined with prebuilt release assets or askpass helper")
        try:
            prepared = prepare_formal_development_assets(
                repository_root=REPOSITORY_ROOT,
                source_commit=checkout_commit,
                cache_root=args.cache_root,
                development_snapshot_sha256=(
                    checkout.development_snapshot_sha256
                ),
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

    if args.browser:
        print("Building the browser-hosted OpenEvo renderer.")
        browser_build_environment = dict(os.environ)
        browser_build_environment["VITE_OPENEVO_SOURCE_DEVELOPMENT"] = "1"
        built = subprocess.run(
            [
                *_browser_npm_command(),
                "run",
                "build",
                "--",
                "--mode",
                "openevo-desktop",
            ],
            cwd=DESKTOP_ROOT,
            check=False,
            env=browser_build_environment,
        )
        if built.returncode != 0:
            return built.returncode
        print("Starting the real OpenEvo Sidecar in the system browser (fixtures disabled).")
        completed = subprocess.run(
            [
                sys.executable,
                "-m",
                "desktop.server.browser_launcher",
                "--static-root",
                os.fspath(DESKTOP_ROOT / "dist"),
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
            ],
            cwd=REPOSITORY_ROOT,
            check=False,
        )
        return completed.returncode

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
