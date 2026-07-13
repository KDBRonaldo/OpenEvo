#!/usr/bin/env python3
"""Smoke a standalone Terminal Bench wheel against an installed Core wheel."""

from __future__ import annotations

import argparse
from io import BytesIO
from pathlib import Path
import subprocess
import tempfile
import venv
from zipfile import BadZipFile, ZipFile


FORBIDDEN_LEGACY_CORE_MODULE_FILES = frozenset(
    {
        "openevo/evolution/terminal_bench_bridge.py",
        "openevo/evolution/terminal_bench_local_parametric.py",
        "openevo/evolution/terminal_bench_per_task.py",
        "openevo/evolution/terminal_bench_task_local_parametric.py",
    }
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--core-wheel", type=Path, required=True)
    parser.add_argument("--benchmark-wheel", type=Path, required=True)
    return parser.parse_args()


def _wheel_names(path: Path) -> set[str]:
    with ZipFile(path) as archive:
        return set(archive.namelist())


def _validate_core_wheel_inventory(names: set[str]) -> None:
    if any(
        name.startswith(("openevo_terminal_bench/", "benchmarks/terminal_bench/"))
        for name in names
    ):
        raise RuntimeError("Core wheel contains Terminal Bench automation")
    legacy_modules = sorted(names & FORBIDDEN_LEGACY_CORE_MODULE_FILES)
    if legacy_modules:
        raise RuntimeError(
            "Core wheel contains removed Terminal Bench Core modules: "
            f"{legacy_modules}"
        )


def _validate_core_wheel(path: Path) -> None:
    with ZipFile(path) as archive:
        names = set(archive.namelist())
        _validate_core_wheel_inventory(names)
        for nested_name in sorted(name for name in names if name.endswith(".whl")):
            try:
                with ZipFile(BytesIO(archive.read(nested_name))) as nested:
                    _validate_core_wheel_inventory(set(nested.namelist()))
            except BadZipFile as exc:
                raise RuntimeError(
                    f"Core wheel contains unreadable nested wheel: {nested_name}"
                ) from exc


def main() -> int:
    args = parse_args()
    core_wheel = args.core_wheel.resolve(strict=True)
    benchmark_wheel = args.benchmark_wheel.resolve(strict=True)

    _validate_core_wheel(core_wheel)

    benchmark_names = _wheel_names(benchmark_wheel)
    if not any(name.startswith("openevo_terminal_bench/") for name in benchmark_names):
        raise RuntimeError("Terminal Bench wheel is missing its automation package")
    if any(name.startswith("openevo/") for name in benchmark_names):
        raise RuntimeError("Terminal Bench wheel vendors OpenEvo Core")

    with tempfile.TemporaryDirectory(prefix="openevo-terminal-bench-smoke-") as temp_dir:
        environment = Path(temp_dir) / "venv"
        venv.EnvBuilder(with_pip=True).create(environment)
        python = environment / "bin" / "python"
        entrypoint = environment / "bin" / "openevo-terminal-bench"
        subprocess.run(
            [
                str(python),
                "-m",
                "pip",
                "install",
                str(core_wheel),
                str(benchmark_wheel),
            ],
            check=True,
        )
        subprocess.run(
            [
                str(python),
                "-c",
                (
                    "import openevo.evolution.store; "
                    "import openevo_terminal_bench.cli; "
                    "import openevo_terminal_bench.per_task"
                ),
            ],
            check=True,
            cwd=temp_dir,
            env={"PATH": str(environment / "bin")},
        )
        help_result = subprocess.run(
            [str(entrypoint), "--help"],
            check=True,
            capture_output=True,
            text=True,
            cwd=temp_dir,
            env={"PATH": str(environment / "bin")},
        )
        if "terminal-bench-events" not in help_result.stdout:
            raise RuntimeError("installed Terminal Bench entrypoint has unexpected help output")
        if "Start the Evolution Backend" in help_result.stdout:
            raise RuntimeError("Terminal Bench entrypoint exposes Core backend commands")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
