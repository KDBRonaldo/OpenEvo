"""Ordinary-user EvoLab command entry point."""

from __future__ import annotations

import argparse
import sys

from openevo.launcher import command_main as launcher_main


def _root_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="evolab",
        description="Connect to and operate a self-hosted EvoLab deployment.",
    )
    parser.add_argument(
        "command",
        nargs="?",
        choices=("webui",),
        help="webui: install/connect through SSH and open the EvoLab WebUI",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    if not arguments or arguments[0] in {"-h", "--help"}:
        _root_parser().print_help()
        return 0
    command = arguments.pop(0)
    if command != "webui":
        _root_parser().error(f"unknown command: {command}")
    return launcher_main(["--self-hosted-webui", *arguments])


if __name__ == "__main__":
    raise SystemExit(main())
