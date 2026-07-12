from __future__ import annotations

import argparse
import json
from pathlib import Path

from openevo import __version__
from openevo.backend.api import create_backend_app
from openevo.evolution.framework import (
    EvolutionExecutionProfile,
    load_verified_framework_registry,
)
from openevo.harness.capture import transcript_capture_enabled


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="openevo-backend")
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    subparsers = parser.add_subparsers(dest="command", required=True)
    serve = subparsers.add_parser("serve", help="Start the OpenEvo backend server.")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8765)
    serve.add_argument("--state-root", type=Path, default=None)
    serve.add_argument("--framework-lock", type=Path, required=True)
    run = subparsers.add_parser("run", help="Run an OpenEvo experiment snapshot.")
    run.add_argument("config", type=Path)
    run.add_argument("--output-dir", type=Path)
    run.add_argument("--artifact-root", type=Path)
    run.add_argument("--rounds", type=int, dest="rounds_override")
    run.add_argument("--task-id", action="append", default=[])
    run.add_argument("--dry-run", action="store_true")
    run.add_argument("--json", action="store_true")
    run.add_argument("--framework-lock", type=Path, required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "serve":
        import uvicorn

        registry = load_verified_framework_registry(args.framework_lock)
        uvicorn.run(
            create_backend_app(
                state_root=args.state_root,
                evolution_registry=registry,
            ),
            host=args.host,
            port=args.port,
        )
        return 0
    if args.command == "run":
        return _run_experiment_command(args)
    raise ValueError(args.command)


def _run_experiment_command(args: argparse.Namespace) -> int:
    from openevo.experiments import dry_run_experiment, load_experiment_config, run_experiment

    config = load_experiment_config(args.config)
    registry = load_verified_framework_registry(args.framework_lock)
    execution_profile = _execution_profile_for_config(config)
    task_ids = args.task_id or None
    if args.dry_run:
        result = dry_run_experiment(
            config,
            task_ids=task_ids,
            rounds_override=args.rounds_override,
            registry_snapshot=registry.snapshot,
            execution_profile=execution_profile,
        )
    else:
        result = run_experiment(
            config,
            task_ids=task_ids,
            rounds_override=args.rounds_override,
            output_dir=args.output_dir,
            artifact_root=args.artifact_root,
            executable_registry=registry,
            execution_profile=execution_profile,
        )
    output = json.dumps(result, indent=None if args.json else 2, sort_keys=True)
    print(output)
    return 0


def _execution_profile_for_config(config) -> EvolutionExecutionProfile:
    subscription = config.agent.auth in {"subscription", "chatgpt_subscription"}
    configured_capture = config.agent.settings.get("capture_mode")
    capture_mode = (
        "transcript"
        if subscription or transcript_capture_enabled(configured_capture)
        else "token_level"
    )
    return EvolutionExecutionProfile(
        execution_mode="subscription" if subscription else "self_deployed",
        capture_mode=capture_mode,
        harness_id=config.agent.preset,
        runtime_capabilities=("adapter_serving",) if not subscription else (),
    )
