from __future__ import annotations

import ast
import importlib.util
from io import BytesIO
from pathlib import Path
from zipfile import ZipFile

import pytest

from openevo.evolution.cli import build_parser


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]


def _load_package_smoke():
    path = REPOSITORY_ROOT / "scripts" / "ci" / "smoke_terminal_bench_package.py"
    spec = importlib.util.spec_from_file_location("smoke_terminal_bench_package", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _python_imports(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    imports: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imports.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module is not None:
            imports.add(node.module)
    return imports


def test_terminal_bench_modules_live_outside_core() -> None:
    core_root = REPOSITORY_ROOT / "src" / "openevo"

    assert not list(core_root.rglob("terminal_bench*.py"))
    assert (REPOSITORY_ROOT / "benchmarks" / "terminal_bench" / "pyproject.toml").is_file()


def test_core_cli_has_no_terminal_bench_commands_or_dispatch() -> None:
    parser = build_parser()
    subparsers = next(
        action for action in parser._actions if action.dest == "command"  # noqa: SLF001
    )

    assert set(subparsers.choices) == {"serve", "worker"}
    assert "terminal-bench" not in parser.format_help()
    assert "terminal_bench" not in (
        REPOSITORY_ROOT / "src" / "openevo" / "evolution" / "cli.py"
    ).read_text(encoding="utf-8")


def test_core_and_desktop_do_not_import_benchmark_package() -> None:
    roots = [REPOSITORY_ROOT / "src" / "openevo", REPOSITORY_ROOT / "desktop"]
    offenders: list[str] = []
    for root in roots:
        for path in root.rglob("*.py"):
            if any(name.startswith("openevo_terminal_bench") for name in _python_imports(path)):
                offenders.append(str(path.relative_to(REPOSITORY_ROOT)))

    assert offenders == []


def test_core_package_configuration_cannot_discover_benchmarks() -> None:
    pyproject = (REPOSITORY_ROOT / "pyproject.toml").read_text(encoding="utf-8")

    assert 'where = ["src"]' in pyproject
    assert 'include = ["openevo*", "slime_bridge*"]' in pyproject
    package_data = pyproject.split("[tool.setuptools.package-data]", 1)[1]
    assert "benchmarks" not in package_data


def test_package_smoke_rejects_exact_removed_core_modules(tmp_path: Path) -> None:
    smoke = _load_package_smoke()
    legacy_modules = {
        "openevo/evolution/terminal_bench_bridge.py",
        "openevo/evolution/terminal_bench_local_parametric.py",
        "openevo/evolution/terminal_bench_per_task.py",
        "openevo/evolution/terminal_bench_task_local_parametric.py",
    }

    with pytest.raises(RuntimeError, match="removed Terminal Bench Core modules") as exc:
        smoke._validate_core_wheel_inventory(legacy_modules)

    assert all(path in str(exc.value) for path in legacy_modules)
    smoke._validate_core_wheel_inventory(
        {"openevo/evolution/terminal_bench_per_task_v2.py"}
    )

    nested = BytesIO()
    with ZipFile(nested, "w") as archive:
        archive.writestr("openevo/evolution/terminal_bench_per_task.py", b"")
    wheel = tmp_path / "openevo-0.1.0-py3-none-any.whl"
    with ZipFile(wheel, "w") as archive:
        archive.writestr(
            "openevo/wheels/openevo-0.1.0-py3-none-any.whl",
            nested.getvalue(),
        )

    with pytest.raises(RuntimeError, match="terminal_bench_per_task.py"):
        smoke._validate_core_wheel(wheel)
