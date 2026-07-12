#!/usr/bin/env python3
"""Build the bundled OpenEvo Desktop sidecar executable for Tauri."""

from __future__ import annotations

import argparse
from email.parser import Parser
import hashlib
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
from tempfile import TemporaryDirectory
import tomllib
from zipfile import ZipFile

SIDECAR_NAME = "openevo-desktop-sidecar"
CORE_WHEEL_ARCHIVE_ROOT = Path("openevo/wheels")


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


def _normalized_distribution_name(value: str) -> str:
    return re.sub(r"[-_.]+", "-", value).lower()


def _project_identity(repo: Path) -> tuple[str, str]:
    payload = tomllib.loads((repo / "pyproject.toml").read_text(encoding="utf-8"))
    project = payload.get("project")
    if not isinstance(project, dict):
        raise RuntimeError("pyproject.toml does not define [project]")
    name = project.get("name")
    version = project.get("version")
    if not isinstance(name, str) or not name or not isinstance(version, str) or not version:
        raise RuntimeError("pyproject.toml project name and version must be non-empty strings")
    return name, version


def _validate_core_wheel(wheel: Path, *, name: str, version: str) -> None:
    try:
        with ZipFile(wheel) as archive:
            nested_wheels = [
                member for member in archive.namelist() if member.endswith(".whl")
            ]
            if nested_wheels:
                raise RuntimeError(
                    f"Core wheel must not contain nested wheels: {nested_wheels}"
                )
            metadata_names = [
                member
                for member in archive.namelist()
                if member.endswith(".dist-info/METADATA")
            ]
            if len(metadata_names) != 1:
                raise RuntimeError(
                    f"Core wheel must contain one METADATA file, found {len(metadata_names)}"
                )
            metadata = Parser().parsestr(
                archive.read(metadata_names[0]).decode("utf-8")
            )
    except OSError as exc:
        raise RuntimeError(f"failed to read built Core wheel: {wheel}") from exc

    actual_name = metadata.get("Name")
    actual_version = metadata.get("Version")
    if (
        not isinstance(actual_name, str)
        or _normalized_distribution_name(actual_name)
        != _normalized_distribution_name(name)
        or actual_version != version
    ):
        raise RuntimeError(
            "built Core wheel identity does not match pyproject.toml: "
            f"expected {name}=={version}, got {actual_name}=={actual_version}"
        )


def _copy_core_build_source(repo: Path, destination: Path) -> None:
    destination.mkdir()
    for filename in ("pyproject.toml", "README.md", "LICENSE"):
        source = repo / filename
        if not source.is_file():
            raise RuntimeError(f"Core build source is missing required file: {source}")
        shutil.copy2(source, destination / filename)
    shutil.copytree(
        repo / "src",
        destination / "src",
        ignore=shutil.ignore_patterns(
            "wheels",
            "*.egg-info",
            "__pycache__",
            ".pytest_cache",
            ".ruff_cache",
            ".mypy_cache",
            ".cache",
            "*.pyc",
            "*.pyo",
        ),
    )


def _build_core_wheel(repo: Path, build_root: Path) -> Path:
    source_root = build_root / "source"
    output_dir = build_root / "wheel-dist"
    _copy_core_build_source(repo, source_root)
    output_dir.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [
            sys.executable,
            "-m",
            "build",
            "--wheel",
            "--no-isolation",
            "--outdir",
            str(output_dir),
        ],
        check=True,
        cwd=source_root,
    )
    wheels = sorted(output_dir.glob("*.whl"))
    if len(wheels) != 1:
        raise RuntimeError(
            f"Core build must produce exactly one wheel, found {len(wheels)} in {output_dir}"
        )
    name, version = _project_identity(repo)
    _validate_core_wheel(wheels[0], name=name, version=version)
    return wheels[0]


def _archive_member_names(executable: Path) -> tuple[str, ...]:
    try:
        from PyInstaller.archive.readers import CArchiveReader
    except ImportError as exc:
        raise RuntimeError("PyInstaller is required to inspect the sidecar archive") from exc
    archive = CArchiveReader(str(executable))
    return tuple(str(name).replace("\\", "/") for name in archive.toc)


def _archive_member_bytes(executable: Path, member_name: str) -> bytes:
    try:
        from PyInstaller.archive.readers import CArchiveReader
    except ImportError as exc:
        raise RuntimeError("PyInstaller is required to inspect the sidecar archive") from exc
    payload = CArchiveReader(str(executable)).extract(member_name)
    if not isinstance(payload, bytes):
        raise RuntimeError(f"sidecar archive member is not byte data: {member_name}")
    return payload


def _sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _prepare_core_wheel_output_dir(output_dir: Path) -> None:
    if output_dir.exists() and not output_dir.is_dir():
        raise RuntimeError(f"Core wheel output is not a directory: {output_dir}")
    output_dir.mkdir(parents=True, exist_ok=True)
    existing = list(output_dir.iterdir())
    if existing:
        raise RuntimeError(f"Core wheel output directory must be empty: {output_dir}")


def _paths_overlap(left: Path, right: Path) -> bool:
    return left == right or left.is_relative_to(right) or right.is_relative_to(left)


def _copy_exclusive(source: Path, destination: Path) -> None:
    try:
        with source.open("rb") as source_file, destination.open("xb") as destination_file:
            shutil.copyfileobj(source_file, destination_file)
    except FileExistsError as exc:
        raise RuntimeError(f"refusing to replace existing Core wheel: {destination}") from exc
    shutil.copystat(source, destination)


def _validate_embedded_core_wheel(executable: Path, wheel: Path) -> str:
    expected = (CORE_WHEEL_ARCHIVE_ROOT / wheel.name).as_posix()
    embedded_wheels = sorted(
        name
        for name in _archive_member_names(executable)
        if name.startswith(f"{CORE_WHEEL_ARCHIVE_ROOT.as_posix()}/")
        and name.endswith(".whl")
    )
    if embedded_wheels != [expected]:
        raise RuntimeError(
            "sidecar archive does not contain the exact staged Core wheel: "
            f"expected {[expected]}, found {embedded_wheels}"
        )
    source_digest = _sha256_bytes(wheel.read_bytes())
    embedded_digest = _sha256_bytes(_archive_member_bytes(executable, expected))
    if embedded_digest != source_digest:
        raise RuntimeError(
            "sidecar embedded Core wheel digest does not match the built wheel: "
            f"expected {source_digest}, got {embedded_digest}"
        )
    return source_digest


def build_sidecar(
    *,
    clean: bool,
    core_wheel_output_dir: Path | None = None,
) -> Path:
    repo = _repo_root()
    desktop_root = repo / "desktop"
    packaging_root = desktop_root / "packaging"
    tauri_root = desktop_root / "src-tauri"
    binary_dir = tauri_root / "binaries"
    dist_dir = packaging_root / "sidecar-dist"
    build_dir = packaging_root / "sidecar-build"
    entrypoint = packaging_root / "sidecar_entry.py"
    static_root = packaging_root / "web"

    if core_wheel_output_dir is not None:
        resolved_output = core_wheel_output_dir.resolve()
        if any(
            _paths_overlap(resolved_output, path.resolve())
            for path in (dist_dir, build_dir, binary_dir)
        ):
            raise RuntimeError("Core wheel output directory overlaps generated paths")
        core_wheel_output_dir = resolved_output
        _prepare_core_wheel_output_dir(resolved_output)
    if clean:
        shutil.rmtree(dist_dir, ignore_errors=True)
        shutil.rmtree(build_dir, ignore_errors=True)
    binary_dir.mkdir(parents=True, exist_ok=True)
    target = binary_dir / f"{SIDECAR_NAME}-{_target_triple()}{_platform_extension()}"
    target.unlink(missing_ok=True)

    with TemporaryDirectory(prefix="openevo-sidecar-core-") as temporary_dir:
        core_wheel = _build_core_wheel(repo, Path(temporary_dir))

        command = [
            sys.executable,
            "-m",
            "PyInstaller",
            *(["--clean"] if clean else []),
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
            "--add-data",
            f"{core_wheel}{os.pathsep}{CORE_WHEEL_ARCHIVE_ROOT.as_posix()}",
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
        _validate_embedded_core_wheel(built, core_wheel)

        if core_wheel_output_dir is not None:
            _copy_exclusive(core_wheel, core_wheel_output_dir / core_wheel.name)

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
    parser.add_argument(
        "--core-wheel-output-dir",
        type=Path,
        help="Preserve the exact embedded Core wheel in this generated output directory.",
    )
    args = parser.parse_args(argv)
    target = build_sidecar(
        clean=not args.no_clean,
        core_wheel_output_dir=args.core_wheel_output_dir,
    )
    print(target)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
