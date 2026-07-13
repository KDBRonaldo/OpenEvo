from __future__ import annotations

import ast
import importlib.util
from pathlib import Path


PACKAGE_ROOT = Path(__file__).resolve().parents[1]
SOURCE_ROOT = PACKAGE_ROOT / "src" / "openevo_terminal_bench"


def test_automation_imports_installed_core_contract_without_vendoring_it() -> None:
    core_spec = importlib.util.find_spec("openevo.evolution.store")

    assert core_spec is not None
    assert core_spec.origin is not None
    assert PACKAGE_ROOT not in Path(core_spec.origin).resolve().parents
    assert not (PACKAGE_ROOT / "src" / "openevo").exists()

    imported_core_modules: set[str] = set()
    for path in SOURCE_ROOT.glob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        imported_core_modules.update(
            node.module
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom)
            and node.module is not None
            and node.module.startswith("openevo.")
        )
        imported_core_modules.update(
            alias.name
            for node in ast.walk(tree)
            if isinstance(node, ast.Import)
            for alias in node.names
            if alias.name.startswith("openevo.")
        )

    assert "openevo.evolution.store" in imported_core_modules
    assert "openevo.evolution.models" in imported_core_modules
    assert "openevo.evolution.agent_system_gepa_kernel" in imported_core_modules


def test_automation_cli_contains_only_terminal_bench_commands() -> None:
    from openevo_terminal_bench.cli import build_parser

    parser = build_parser()
    subparsers = next(
        action for action in parser._actions if action.dest == "command"  # noqa: SLF001
    )

    assert subparsers.choices
    assert all(name.startswith("terminal-bench-") for name in subparsers.choices)
    assert "serve" not in subparsers.choices
    assert "worker" not in subparsers.choices
