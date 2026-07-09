#!/usr/bin/env python3
"""Build the bundled OpenEvo Desktop sidecar executable for Tauri."""

from __future__ import annotations

import argparse
import os
from pathlib import Path
import shutil
import subprocess
import sys

SIDECAR_NAME = "openevo-desktop-sidecar"


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _target_triple() -> str:
    try:
        result = subprocess.run(
            ["rustc", "--print", "host-tuple"],
            check=True,
            capture_output=True,
            text=True,
        )
        triple = result.stdout.strip()
        if triple:
            return triple
    except subprocess.CalledProcessError:
        pass

    result = subprocess.run(
        ["rustc", "-Vv"],
        check=True,
        capture_output=True,
        text=True,
    )
    for line in result.stdout.splitlines():
        if line.startswith("host:"):
            return line.split(":", 1)[1].strip()
    raise RuntimeError("failed to determine Rust host target triple")


def _platform_extension() -> str:
    return ".exe" if os.name == "nt" else ""


def build_sidecar(*, clean: bool) -> Path:
    repo = _repo_root()
    desktop_root = repo / "desktop"
    packaging_root = desktop_root / "packaging"
    tauri_root = desktop_root / "src-tauri"
    binary_dir = tauri_root / "binaries"
    dist_dir = packaging_root / "sidecar-dist"
    build_dir = packaging_root / "sidecar-build"
    entrypoint = packaging_root / "sidecar_entry.py"
    static_root = packaging_root / "web"

    if clean:
        shutil.rmtree(dist_dir, ignore_errors=True)
        shutil.rmtree(build_dir, ignore_errors=True)
    binary_dir.mkdir(parents=True, exist_ok=True)

    command = [
        sys.executable,
        "-m",
        "PyInstaller",
        "--clean",
        "--noconfirm",
        "--onefile",
        "--name",
        SIDECAR_NAME,
        "--distpath",
        str(dist_dir),
        "--workpath",
        str(build_dir),
        "--specpath",
        str(build_dir),
        "--paths",
        str(repo),
        "--paths",
        str(repo / "src"),
        "--collect-submodules",
        "desktop",
        "--collect-submodules",
        "openevo",
        "--collect-data",
        "openevo",
        "--add-data",
        f"{static_root}{os.pathsep}desktop/packaging/web",
        "--hidden-import",
        "uvicorn.logging",
        "--hidden-import",
        "uvicorn.loops.auto",
        "--hidden-import",
        "uvicorn.protocols.http.auto",
        "--hidden-import",
        "uvicorn.protocols.websockets.auto",
        str(entrypoint),
    ]
    subprocess.run(command, check=True, cwd=repo)

    built = dist_dir / f"{SIDECAR_NAME}{_platform_extension()}"
    if not built.is_file():
        raise RuntimeError(f"PyInstaller did not produce expected sidecar: {built}")

    target = binary_dir / f"{SIDECAR_NAME}-{_target_triple()}{_platform_extension()}"
    shutil.copy2(built, target)
    target.chmod(target.stat().st_mode | 0o755)
    return target


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--no-clean",
        action="store_true",
        help="Reuse PyInstaller build directories instead of removing them first.",
    )
    args = parser.parse_args(argv)
    target = build_sidecar(clean=not args.no_clean)
    print(target)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
