#!/usr/bin/env python3
"""Build and verify the Linux Daemon resource shipped by OpenEvo Desktop."""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import stat
import sys
from tempfile import TemporaryDirectory
from types import ModuleType
from typing import Sequence


REPO_ROOT = Path(__file__).resolve().parents[2]
DAEMON_BUNDLE_NAME = "openevo-daemon-linux-x86_64"
DAEMON_MANIFEST_NAME = "openevo-daemon-bundle.json"
RESOURCE_DIRECTORY = Path("openevo-daemon")
MACOS_RESOURCE_ROOT = Path("Contents/Resources") / RESOURCE_DIRECTORY
SOURCE_COMMIT_LENGTH = 40


class ResourceCompositionError(RuntimeError):
    pass


def _load_module(name: str, path: Path) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise ResourceCompositionError(f"Release composition module is unavailable: {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def _sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _canonical_json(value: object) -> bytes:
    return (json.dumps(value, separators=(",", ":"), sort_keys=True) + "\n").encode("utf-8")


def _read_controlled_file(path: Path, *, executable: bool = False) -> bytes:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise ResourceCompositionError(f"Controlled release input is unavailable: {path}") from exc
    try:
        before = os.fstat(descriptor)
        pathname = os.stat(path, follow_symlinks=False)
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_nlink != 1
            or before.st_mode & (stat.S_IWGRP | stat.S_IWOTH)
            or (before.st_dev, before.st_ino) != (pathname.st_dev, pathname.st_ino)
            or (executable and not before.st_mode & stat.S_IXUSR)
        ):
            raise ResourceCompositionError(f"Controlled release input is not trusted: {path}")
        chunks: list[bytes] = []
        while chunk := os.read(descriptor, 1024 * 1024):
            chunks.append(chunk)
        payload = b"".join(chunks)
        after = os.fstat(descriptor)
        current = os.stat(path, follow_symlinks=False)
        if (
            (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
            != (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
            or (current.st_dev, current.st_ino) != (before.st_dev, before.st_ino)
            or len(payload) != before.st_size
        ):
            raise ResourceCompositionError(f"Controlled release input changed while reading: {path}")
        return payload
    finally:
        os.close(descriptor)


def _write_new(path: Path, payload: bytes, *, mode: int) -> None:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0)
    try:
        descriptor = os.open(path, flags, mode)
    except FileExistsError as exc:
        raise ResourceCompositionError(f"Refusing to replace release output: {path}") from exc
    try:
        os.fchmod(descriptor, mode)
        view = memoryview(payload)
        while view:
            written = os.write(descriptor, view)
            if written <= 0:
                raise ResourceCompositionError(f"Release output copy stalled: {path}")
            view = view[written:]
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _candidate_module() -> ModuleType:
    return _load_module(
        "_openevo_release_candidate_for_daemon_resource",
        REPO_ROOT / "scripts/ci/openevo_release_candidate.py",
    )


def _validate_release_inputs(
    *,
    bundle: Path,
    manifest: Path,
    wheel: Path,
    framework_lock: Path,
    source_commit: str,
    registry_digest: str,
) -> None:
    candidate = _candidate_module()
    try:
        candidate.validate_daemon_release_inputs(
            bundle=bundle,
            manifest_path=manifest,
            wheel=wheel,
            framework_lock=framework_lock,
            source_commit=source_commit,
            registry_digest=registry_digest,
        )
    except candidate.CandidateError as exc:
        raise ResourceCompositionError(str(exc)) from exc


def build_daemon_release_inputs(*, output_dir: Path, source_commit: str) -> None:
    if len(source_commit) != SOURCE_COMMIT_LENGTH or any(
        character not in "0123456789abcdef" for character in source_commit
    ):
        raise ResourceCompositionError("source_commit must be one full lowercase Git commit")
    output = output_dir.absolute()
    if output.exists():
        raise ResourceCompositionError("Daemon release output directory must not already exist")
    output.mkdir(mode=0o700, parents=True)
    os.chmod(output, 0o700)

    sidecar_builder = _load_module(
        "_openevo_sidecar_builder_for_daemon_resource",
        REPO_ROOT / "desktop/packaging/build_sidecar.py",
    )
    daemon_builder = _load_module(
        "_openevo_daemon_builder_for_desktop_resource",
        REPO_ROOT / "scripts/ci/build_openevo_daemon_bundle.py",
    )
    if sidecar_builder._BUILD_SOURCE_COMMIT != source_commit:
        raise ResourceCompositionError("Checkout identity changed before Daemon composition")

    with TemporaryDirectory(prefix="openevo-desktop-core-", dir=output) as temporary:
        temporary_root = Path(temporary)
        wheel = sidecar_builder._build_core_wheel(
            REPO_ROOT,
            temporary_root / "core-build",
        )
        _name, version = sidecar_builder._project_identity(REPO_ROOT)
        framework_lock = sidecar_builder._write_core_framework_lock(wheel, version=version)
        core_output = output / "core"
        sidecar_builder._publish_core_release_inputs_once(
            core_output,
            wheel,
            framework_lock,
        )

    published_wheels = list((output / "core").glob("openevo-*.whl"))
    if len(published_wheels) != 1:
        raise ResourceCompositionError("Daemon composition did not publish one exact Core wheel")
    daemon_builder.build_bundle(
        wheel=published_wheels[0],
        framework_lock=output / "core/framework-lock.json",
        uv_lock=REPO_ROOT / "uv.lock",
        source_commit=source_commit,
        output_dir=output / "daemon",
    )


def stage_daemon_resource(
    *,
    bundle: Path,
    manifest: Path,
    wheel: Path,
    framework_lock: Path,
    source_commit: str,
    registry_digest: str,
    output_dir: Path,
) -> None:
    _validate_release_inputs(
        bundle=bundle,
        manifest=manifest,
        wheel=wheel,
        framework_lock=framework_lock,
        source_commit=source_commit,
        registry_digest=registry_digest,
    )
    bundle_payload = _read_controlled_file(bundle, executable=True)
    manifest_payload = _read_controlled_file(manifest)
    output = output_dir.absolute()
    if output.exists():
        raise ResourceCompositionError("Daemon resource output directory must not already exist")
    output.mkdir(mode=0o700, parents=True)
    os.chmod(output, 0o700)
    _write_new(output / DAEMON_BUNDLE_NAME, bundle_payload, mode=0o755)
    _write_new(output / DAEMON_MANIFEST_NAME, manifest_payload, mode=0o644)
    if (
        _sha256_bytes(_read_controlled_file(output / DAEMON_BUNDLE_NAME, executable=True))
        != _sha256_bytes(bundle_payload)
        or _read_controlled_file(output / DAEMON_MANIFEST_NAME) != manifest_payload
    ):
        raise ResourceCompositionError("Staged Daemon resources differ from verified inputs")


def verify_app_resource(
    *,
    app: Path,
    bundle: Path,
    manifest: Path,
    source_dmg: Path,
    launch_origin: str,
    evidence_out: Path,
) -> None:
    if launch_origin not in {"mounted_dmg", "detached_copy"}:
        raise ResourceCompositionError("Daemon resource launch origin is invalid")
    source_bundle = _read_controlled_file(bundle, executable=True)
    source_manifest = _read_controlled_file(manifest)
    dmg_payload = _read_controlled_file(source_dmg)
    resource_root = app / MACOS_RESOURCE_ROOT
    packaged_bundle = resource_root / DAEMON_BUNDLE_NAME
    packaged_manifest = resource_root / DAEMON_MANIFEST_NAME
    packaged_bundle_payload = _read_controlled_file(packaged_bundle, executable=True)
    packaged_manifest_payload = _read_controlled_file(packaged_manifest)
    if packaged_bundle_payload != source_bundle or packaged_manifest_payload != source_manifest:
        raise ResourceCompositionError("Packaged Daemon resources differ from verified inputs")
    evidence = {
        "daemon_bundle": {
            "byte_size": len(packaged_bundle_payload),
            "filename": DAEMON_BUNDLE_NAME,
            "relative_path": (MACOS_RESOURCE_ROOT / DAEMON_BUNDLE_NAME).as_posix(),
            "sha256": _sha256_bytes(packaged_bundle_payload),
        },
        "daemon_manifest": {
            "byte_size": len(packaged_manifest_payload),
            "filename": DAEMON_MANIFEST_NAME,
            "relative_path": (MACOS_RESOURCE_ROOT / DAEMON_MANIFEST_NAME).as_posix(),
            "sha256": _sha256_bytes(packaged_manifest_payload),
        },
        "launch_origin": launch_origin,
        "schema_version": 1,
        "source_dmg": {
            "filename": source_dmg.name,
            "sha256": _sha256_bytes(dmg_payload),
        },
    }
    evidence_path = evidence_out.absolute()
    evidence_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    _write_new(evidence_path, _canonical_json(evidence), mode=0o600)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)

    build = commands.add_parser("build")
    build.add_argument("--output-dir", type=Path, required=True)
    build.add_argument("--source-commit", required=True)

    stage = commands.add_parser("stage")
    stage.add_argument("--bundle", type=Path, required=True)
    stage.add_argument("--manifest", type=Path, required=True)
    stage.add_argument("--wheel", type=Path, required=True)
    stage.add_argument("--framework-lock", type=Path, required=True)
    stage.add_argument("--source-commit", required=True)
    stage.add_argument("--registry-digest", required=True)
    stage.add_argument("--output-dir", type=Path, required=True)

    verify = commands.add_parser("verify-app")
    verify.add_argument("--app", type=Path, required=True)
    verify.add_argument("--bundle", type=Path, required=True)
    verify.add_argument("--manifest", type=Path, required=True)
    verify.add_argument("--source-dmg", type=Path, required=True)
    verify.add_argument(
        "--launch-origin",
        choices=("mounted_dmg", "detached_copy"),
        required=True,
    )
    verify.add_argument("--evidence-out", type=Path, required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "build":
        build_daemon_release_inputs(
            output_dir=args.output_dir,
            source_commit=args.source_commit,
        )
    elif args.command == "stage":
        stage_daemon_resource(
            bundle=args.bundle,
            manifest=args.manifest,
            wheel=args.wheel,
            framework_lock=args.framework_lock,
            source_commit=args.source_commit,
            registry_digest=args.registry_digest,
            output_dir=args.output_dir,
        )
    else:
        verify_app_resource(
            app=args.app,
            bundle=args.bundle,
            manifest=args.manifest,
            source_dmg=args.source_dmg,
            launch_origin=args.launch_origin,
            evidence_out=args.evidence_out,
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
