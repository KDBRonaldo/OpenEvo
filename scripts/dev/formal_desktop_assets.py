"""Prepare source-development assets for the formal Desktop/Daemon path.

The release sidecar deliberately accepts only sealed, commit-bound assets.  This
module gives developers the same boundary without making them run the release
workflow by hand.  It does not start the legacy live-agent daemon.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import secrets
import shutil
import stat
import subprocess
import sys
from typing import Callable
from urllib.request import Request, urlopen

from openevo.runtime.managed import (
    MANAGED_RUNTIME_ARCHIVE_RELEASE,
    verify_managed_runtime_archive,
)


RUNTIME_DOWNLOAD_URL = (
    "https://github.com/CompLifeLab-ZJU/OpenEvo/releases/download/"
    f"{MANAGED_RUNTIME_ARCHIVE_RELEASE.asset_release_tag}/"
    f"{MANAGED_RUNTIME_ARCHIVE_RELEASE.filename}"
)


@dataclass(frozen=True, slots=True)
class FormalDevelopmentAssets:
    source_commit: str
    development_snapshot_sha256: str | None
    release_assets_root: Path
    askpass_helper: Path


class FormalDevelopmentAssetError(RuntimeError):
    """A developer-readable failure while preparing formal release assets."""


def prepare_formal_development_assets(
    *,
    repository_root: Path,
    source_commit: str,
    cache_root: Path,
    development_snapshot_sha256: str | None = None,
    managed_runtime_archive: Path | None = None,
    run: Callable[..., subprocess.CompletedProcess[object]] = subprocess.run,
) -> FormalDevelopmentAssets:
    """Build and cache the exact assets consumed by the formal local sidecar.

    ``development_snapshot_sha256`` is deliberately separate from the release
    ``source_commit``.  Source-development launchers may build a dirty checkout,
    but those bytes must never reuse the clean commit cache.  Release tooling
    continues to use only the immutable commit identity.
    """

    repository_root = repository_root.resolve(strict=True)
    cache_root = cache_root.expanduser().absolute()
    if development_snapshot_sha256 is not None and (
        len(development_snapshot_sha256) != 64
        or any(
            character not in "0123456789abcdef"
            for character in development_snapshot_sha256
        )
    ):
        raise FormalDevelopmentAssetError(
            "development snapshot identity must be one lowercase SHA-256"
        )
    cache_identity = source_commit
    if development_snapshot_sha256 is not None:
        cache_identity = f"{source_commit}-dirty-{development_snapshot_sha256}"
    commit_root = cache_root / cache_identity
    commit_root.mkdir(mode=0o700, parents=True, exist_ok=True)
    try:
        os.chmod(commit_root, 0o700)
    except OSError as exc:
        raise FormalDevelopmentAssetError("formal development cache is not private") from exc

    runtime_archive = _prepare_runtime_archive(
        cache_root=cache_root,
        configured=managed_runtime_archive,
    )
    release_assets_root = commit_root / "openevo-release-assets"
    if not release_assets_root.exists():
        _build_release_assets(
            repository_root=repository_root,
            source_commit=source_commit,
            commit_root=commit_root,
            runtime_archive=runtime_archive,
            run=run,
        )
    _verify_staged_assets(release_assets_root, source_commit=source_commit)

    askpass_helper = commit_root / "openevo-ssh-askpass"
    if not askpass_helper.exists():
        _build_askpass_helper(
            repository_root=repository_root,
            destination=askpass_helper,
            cargo_target=commit_root / "cargo-target",
            run=run,
        )
    _verify_askpass_helper(askpass_helper)
    return FormalDevelopmentAssets(
        source_commit=source_commit,
        development_snapshot_sha256=development_snapshot_sha256,
        release_assets_root=release_assets_root,
        askpass_helper=askpass_helper,
    )


def _prepare_runtime_archive(*, cache_root: Path, configured: Path | None) -> Path:
    if configured is not None:
        archive = configured.expanduser().resolve(strict=True)
        _verify_runtime_archive(archive)
        return archive

    runtime_root = cache_root / "runtime"
    runtime_root.mkdir(mode=0o700, parents=True, exist_ok=True)
    destination = runtime_root / MANAGED_RUNTIME_ARCHIVE_RELEASE.filename
    if destination.exists():
        _verify_runtime_archive(destination)
        return destination

    print(
        "Downloading the pinned managed runtime archive "
        f"({MANAGED_RUNTIME_ARCHIVE_RELEASE.byte_size / (1024 * 1024):.1f} MiB)..."
    )
    staging = runtime_root / f".{destination.name}.part-{secrets.token_hex(8)}"
    digest = hashlib.sha256()
    byte_size = 0
    request = Request(RUNTIME_DOWNLOAD_URL, headers={"User-Agent": "OpenEvo-formal-dev"})
    try:
        with urlopen(request, timeout=60) as response, staging.open("xb") as target:
            os.chmod(staging, 0o600)
            while chunk := response.read(1024 * 1024):
                byte_size += len(chunk)
                if byte_size > MANAGED_RUNTIME_ARCHIVE_RELEASE.byte_size:
                    raise FormalDevelopmentAssetError(
                        "managed runtime download exceeded its declared size"
                    )
                digest.update(chunk)
                target.write(chunk)
            target.flush()
            os.fsync(target.fileno())
        if (
            byte_size != MANAGED_RUNTIME_ARCHIVE_RELEASE.byte_size
            or digest.hexdigest() != MANAGED_RUNTIME_ARCHIVE_RELEASE.sha256
        ):
            raise FormalDevelopmentAssetError("managed runtime download identity is invalid")
        os.replace(staging, destination)
    except FormalDevelopmentAssetError:
        staging.unlink(missing_ok=True)
        raise
    except (OSError, ValueError) as exc:
        staging.unlink(missing_ok=True)
        raise FormalDevelopmentAssetError(
            "managed runtime download failed; pass --managed-runtime-archive to use a verified local copy"
        ) from exc
    _verify_runtime_archive(destination)
    return destination


def _verify_runtime_archive(path: Path) -> None:
    try:
        verify_managed_runtime_archive(path, release=MANAGED_RUNTIME_ARCHIVE_RELEASE)
    except (OSError, ValueError) as exc:
        raise FormalDevelopmentAssetError("managed runtime archive is invalid") from exc


def _build_release_assets(
    *,
    repository_root: Path,
    source_commit: str,
    commit_root: Path,
    runtime_archive: Path,
    run: Callable[..., subprocess.CompletedProcess[object]],
) -> None:
    inputs = commit_root / "daemon-inputs"
    resource_script = repository_root / "scripts/ci/openevo_desktop_daemon_resource.py"
    if not _release_inputs_complete(inputs, source_commit=source_commit):
        _discard_incomplete_release_inputs(inputs, commit_root=commit_root)
        print("[formal 1/3] Building the commit-bound Core wheel and Daemon bundle...")
        _run_checked(
            run,
            [
                sys.executable,
                os.fspath(resource_script),
                "build",
                "--output-dir",
                os.fspath(inputs),
                "--source-commit",
                source_commit,
            ],
            cwd=repository_root,
        )

    manifest_path = inputs / "daemon/openevo-daemon-bundle.json"
    wheel_files = list((inputs / "core").glob("openevo-*.whl"))
    if len(wheel_files) != 1:
        raise FormalDevelopmentAssetError("formal build did not produce one Core wheel")
    registry_digest = _read_registry_digest(manifest_path, source_commit=source_commit)
    print("[formal 2/3] Staging verified formal Desktop release assets...")
    _run_checked(
        run,
        [
            sys.executable,
            os.fspath(resource_script),
            "stage",
            "--bundle",
            os.fspath(inputs / "daemon/openevo-daemon-linux-x86_64"),
            "--manifest",
            os.fspath(manifest_path),
            "--wheel",
            os.fspath(wheel_files[0]),
            "--framework-lock",
            os.fspath(inputs / "core/framework-lock.json"),
            "--managed-runtime-archive",
            os.fspath(runtime_archive),
            "--source-commit",
            source_commit,
            "--registry-digest",
            registry_digest,
            "--output-dir",
            os.fspath(commit_root / "openevo-release-assets"),
        ],
        cwd=repository_root,
    )


def _release_inputs_complete(inputs: Path, *, source_commit: str) -> bool:
    """Return whether an interrupted build left a reusable input bundle."""

    try:
        metadata = inputs.stat(follow_symlinks=False)
    except FileNotFoundError:
        return False
    except OSError:
        return False
    if not stat.S_ISDIR(metadata.st_mode):
        return False
    required = (
        inputs / "daemon/openevo-daemon-linux-x86_64",
        inputs / "daemon/openevo-daemon-bundle.json",
        inputs / "core/framework-lock.json",
    )
    if any(not path.is_file() or path.is_symlink() for path in required):
        return False
    wheels = list((inputs / "core").glob("openevo-*.whl"))
    if len(wheels) != 1 or wheels[0].is_symlink():
        return False
    try:
        _read_registry_digest(required[1], source_commit=source_commit)
    except FormalDevelopmentAssetError:
        return False
    return True


def _discard_incomplete_release_inputs(inputs: Path, *, commit_root: Path) -> None:
    """Remove only the private, commit-scoped, non-authoritative build cache."""

    if not inputs.exists() and not inputs.is_symlink():
        return
    if inputs.parent != commit_root or inputs.name != "daemon-inputs":
        raise FormalDevelopmentAssetError("refusing to replace an unexpected build cache path")
    try:
        if inputs.is_symlink() or not inputs.is_dir():
            inputs.unlink()
        else:
            shutil.rmtree(inputs)
    except OSError as exc:
        raise FormalDevelopmentAssetError(
            "incomplete formal build cache could not be replaced"
        ) from exc


def _read_registry_digest(manifest_path: Path, *, source_commit: str) -> str:
    try:
        value = json.loads(manifest_path.read_text(encoding="utf-8"))
        digest = value["core"]["registry_digest"]
        observed_commit = value["release"]["source_commit"]
    except (OSError, KeyError, TypeError, json.JSONDecodeError) as exc:
        raise FormalDevelopmentAssetError("Daemon bundle manifest is invalid") from exc
    if (
        observed_commit != source_commit
        or not isinstance(digest, str)
        or len(digest) != 64
        or any(character not in "0123456789abcdef" for character in digest)
    ):
        raise FormalDevelopmentAssetError("Daemon bundle identity does not match the checkout")
    return digest


def _build_askpass_helper(
    *,
    repository_root: Path,
    destination: Path,
    cargo_target: Path,
    run: Callable[..., subprocess.CompletedProcess[object]],
) -> None:
    print("[formal 3/3] Building the native OpenSSH askpass helper...")
    environment = os.environ.copy()
    environment["CARGO_TARGET_DIR"] = os.fspath(cargo_target)
    environment["TAURI_CONFIG"] = json.dumps(
        {"bundle": {"externalBin": []}}, separators=(",", ":"), sort_keys=True
    )
    _run_checked(
        run,
        ["cargo", "build", "--locked", "--release", "--bin", "openevo-ssh-askpass"],
        cwd=repository_root / "desktop/src-tauri",
        env=environment,
    )
    built = cargo_target / "release/openevo-ssh-askpass"
    if not built.is_file() or built.is_symlink():
        raise FormalDevelopmentAssetError("Cargo did not produce the askpass helper")
    staging = destination.with_name(f".{destination.name}.part-{secrets.token_hex(8)}")
    try:
        with built.open("rb") as source, staging.open("xb") as target:
            shutil.copyfileobj(source, target, length=1024 * 1024)
            target.flush()
            os.fsync(target.fileno())
        os.chmod(staging, 0o755)
        os.replace(staging, destination)
    except OSError as exc:
        staging.unlink(missing_ok=True)
        raise FormalDevelopmentAssetError("askpass helper could not be published") from exc


def _verify_staged_assets(root: Path, *, source_commit: str) -> None:
    manifest = root / "release-assets.json"
    try:
        value = json.loads(manifest.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise FormalDevelopmentAssetError("formal release asset cache is incomplete") from exc
    if value.get("schema_version") != 1 or value.get("source_commit") != source_commit:
        raise FormalDevelopmentAssetError("formal release assets belong to another commit")
    required = (
        "core/framework-lock.json",
        "daemon/openevo-daemon-bundle.json",
        "daemon/openevo-daemon-linux-x86_64",
    )
    if any(not (root / relative).is_file() for relative in required):
        raise FormalDevelopmentAssetError("formal release asset cache is incomplete")
    runtime_files = list((root / "runtime").glob("*.tar.gz"))
    if len(runtime_files) != 1:
        raise FormalDevelopmentAssetError("formal release asset cache has no managed runtime")


def _verify_askpass_helper(path: Path) -> None:
    try:
        metadata = path.stat(follow_symlinks=False)
    except OSError as exc:
        raise FormalDevelopmentAssetError("askpass helper is unavailable") from exc
    if (
        not stat.S_ISREG(metadata.st_mode)
        or metadata.st_nlink != 1
        or stat.S_IMODE(metadata.st_mode) != 0o755
        or metadata.st_size <= 0
    ):
        raise FormalDevelopmentAssetError("askpass helper identity is invalid")


def _run_checked(
    run: Callable[..., subprocess.CompletedProcess[object]],
    command: list[str],
    *,
    cwd: Path,
    env: dict[str, str] | None = None,
) -> None:
    try:
        completed = run(command, cwd=cwd, env=env, check=False)
    except OSError as exc:
        raise FormalDevelopmentAssetError(f"required build tool is unavailable: {command[0]}") from exc
    if completed.returncode != 0:
        raise FormalDevelopmentAssetError(
            f"formal asset preparation failed with exit code {completed.returncode}"
        )


__all__ = [
    "FormalDevelopmentAssetError",
    "FormalDevelopmentAssets",
    "prepare_formal_development_assets",
]
