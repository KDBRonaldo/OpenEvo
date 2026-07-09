from __future__ import annotations

import argparse
import json
from pathlib import Path

from openevo import __version__


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="openevo-backend")
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    subparsers = parser.add_subparsers(dest="command", required=True)
    serve = subparsers.add_parser("serve", help="Start the OpenEvo backend server.")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8765)
    run = subparsers.add_parser("run", help="Run an OpenEvo experiment snapshot.")
    run.add_argument("config", type=Path)
    run.add_argument("--output-dir", type=Path)
    run.add_argument("--rounds", type=int, dest="rounds_override")
    run.add_argument("--task-id", action="append", default=[])
    run.add_argument("--dry-run", action="store_true")
    run.add_argument("--json", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "serve":
        raise SystemExit("openevo-backend serve is introduced in the backend API phase.")
    if args.command == "run":
        return _run_experiment_command(args)
    raise ValueError(args.command)


def _run_experiment_command(args: argparse.Namespace) -> int:
    from openevo.experiments import dry_run_experiment, load_experiment_config, run_experiment

    config = load_experiment_config(args.config)
    task_ids = args.task_id or None
    if args.dry_run:
        result = dry_run_experiment(
            config,
            task_ids=task_ids,
            rounds_override=args.rounds_override,
        )
    else:
        result = run_experiment(
            config,
            task_ids=task_ids,
            rounds_override=args.rounds_override,
            output_dir=args.output_dir,
        )
    output = json.dumps(result, indent=None if args.json else 2, sort_keys=True)
    print(output)
    return 0
