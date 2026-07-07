from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

import httpx
import yaml
from pydantic import ValidationError

from openevo.experiment.models import load_experiment_config
from openevo.experiment.runner import dry_run_experiment, run_experiment
from openevo.remote import (
    RemoteCommandResult,
    SshRemoteExecutorTransport,
    execute_sidecar_plan,
)
from openevo.science import (
    PreparedWorkspace,
    compile_science_project,
    load_science_project_config,
)
from openevo.sidecar import (
    build_sidecar_science_plan,
    load_remote_profile_config,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="openevo",
        description="Run OpenEvo experiments on top of Polar rollout and evolution services.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    run_parser = subparsers.add_parser(
        "run",
        help="Run or dry-run an OpenEvo experiment config.",
    )
    run_parser.add_argument("config", help="Path to experiment YAML.")
    run_parser.add_argument("--dry-run", action="store_true", help="Print the compiled plan.")
    run_parser.add_argument("--output-dir", help="Directory for run summaries and artifacts.")
    run_parser.add_argument(
        "--task-id",
        action="append",
        default=None,
        help="Run only this task id. Can be repeated.",
    )
    run_parser.add_argument("--rounds", type=int, help="Override evolution.rounds.")
    run_parser.add_argument("--json", action="store_true", help="Print JSON output.")
    run_parser.add_argument(
        "--poll-interval",
        type=float,
        default=2.0,
        help="Seconds between rollout status polls.",
    )
    run_parser.add_argument(
        "--max-poll-attempts",
        type=int,
        default=1800,
        help="Maximum rollout status polls per task round.",
    )

    science_parser = subparsers.add_parser(
        "science",
        help="Work with OpenEvo Science project configs.",
    )
    science_subparsers = science_parser.add_subparsers(
        dest="science_command",
        required=True,
    )
    compile_parser = science_subparsers.add_parser(
        "compile",
        help="Compile a Science Project YAML to an experiment config.",
    )
    compile_parser.add_argument("config", help="Path to science project YAML.")
    compile_parser.add_argument("--json", action="store_true", help="Print JSON output.")
    compile_parser.add_argument(
        "--prepared-workspace",
        action="append",
        default=None,
        help=(
            "Prepared workspace mapping in task_id=/remote/path form. "
            "Can be repeated."
        ),
    )

    sidecar_parser = subparsers.add_parser(
        "sidecar",
        help="Work with OpenEvo Desktop sidecar plans.",
    )
    sidecar_subparsers = sidecar_parser.add_subparsers(
        dest="sidecar_command",
        required=True,
    )
    plan_parser = sidecar_subparsers.add_parser(
        "plan",
        help="Build a Desktop sidecar dry-run plan for a Science Project.",
    )
    plan_parser.add_argument("config", help="Path to science project YAML.")
    plan_parser.add_argument(
        "--remote-profile",
        required=True,
        help="Path to remote profile YAML.",
    )
    plan_parser.add_argument("--json", action="store_true", help="Print JSON output.")
    execute_parser = sidecar_subparsers.add_parser(
        "execute",
        help="Build a sidecar plan and execute it through a selected transport.",
    )
    execute_parser.add_argument("config", help="Path to science project YAML.")
    execute_parser.add_argument(
        "--remote-profile",
        required=True,
        help="Path to remote profile YAML.",
    )
    execute_parser.add_argument(
        "--skip-preflight",
        action="store_true",
        help="Skip remote preflight before workspace preparation.",
    )
    execute_parser.add_argument(
        "--transport",
        choices=("dry-run", "ssh"),
        default="dry-run",
        help="Remote executor transport to use. Defaults to dry-run.",
    )
    execute_parser.add_argument("--json", action="store_true", help="Print JSON output.")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        if args.command == "run":
            return _handle_run(args)
        if args.command == "science":
            return _handle_science(args)
        if args.command == "sidecar":
            return _handle_sidecar(args)
    except (FileNotFoundError, ValueError, ValidationError, TimeoutError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except httpx.HTTPStatusError as exc:
        body = exc.response.text.strip()
        detail = f": {body}" if body else ""
        print(
            f"error: service returned {exc.response.status_code}{detail}",
            file=sys.stderr,
        )
        return 1
    except httpx.HTTPError as exc:
        print(f"error: could not reach configured service: {exc}", file=sys.stderr)
        return 1
    parser.error(f"Unknown command: {args.command}")
    return 2


def _handle_run(args: argparse.Namespace) -> int:
    config = load_experiment_config(Path(args.config))
    output_dir = Path(args.output_dir) if args.output_dir else None
    task_ids = args.task_id
    if args.dry_run:
        result = dry_run_experiment(
            config,
            task_ids=task_ids,
            rounds_override=args.rounds,
        )
        if output_dir is not None:
            output_dir.mkdir(parents=True, exist_ok=True)
            plan_path = output_dir / "plan.json"
            result["plan_path"] = str(plan_path)
            plan_path.write_text(_json_dumps(result), encoding="utf-8")
        _print_result(result, as_json=args.json)
        return 0

    result = run_experiment(
        config,
        task_ids=task_ids,
        rounds_override=args.rounds,
        output_dir=output_dir,
        poll_interval_seconds=args.poll_interval,
        max_poll_attempts=args.max_poll_attempts,
    )
    _print_result(result, as_json=args.json)
    return 0 if result.get("status") == "completed" else 1


def _handle_science(args: argparse.Namespace) -> int:
    if args.science_command == "compile":
        return _handle_science_compile(args)
    raise ValueError(f"Unknown science command: {args.science_command}")


def _handle_science_compile(args: argparse.Namespace) -> int:
    project = load_science_project_config(Path(args.config))
    config = compile_science_project(
        project,
        prepared_workspaces=_parse_prepared_workspaces(args.prepared_workspace),
    )
    result = config.model_dump(mode="json", exclude={"path"})
    if args.json:
        print(_json_dumps(result), end="")
        return 0
    print(yaml.safe_dump(result, sort_keys=True), end="")
    return 0


def _handle_sidecar(args: argparse.Namespace) -> int:
    if args.sidecar_command == "plan":
        return _handle_sidecar_plan(args)
    if args.sidecar_command == "execute":
        return _handle_sidecar_execute(args)
    raise ValueError(f"Unknown sidecar command: {args.sidecar_command}")


def _handle_sidecar_plan(args: argparse.Namespace) -> int:
    project = load_science_project_config(Path(args.config))
    profile = load_remote_profile_config(Path(args.remote_profile))
    plan = build_sidecar_science_plan(project, profile)
    result = plan.model_dump(mode="json")
    if args.json:
        print(_json_dumps(result), end="")
        return 0
    print(yaml.safe_dump(result, sort_keys=True), end="")
    return 0


def _handle_sidecar_execute(args: argparse.Namespace) -> int:
    project = load_science_project_config(Path(args.config))
    profile = load_remote_profile_config(Path(args.remote_profile))
    plan = build_sidecar_science_plan(project, profile)
    report = execute_sidecar_plan(
        plan,
        _sidecar_transport(args, profile),
        run_remote_preflight=not args.skip_preflight,
    )
    result = report.model_dump(mode="json")
    if args.json:
        print(_json_dumps(result), end="")
        return 0 if report.ready else 1
    print(yaml.safe_dump(result, sort_keys=True), end="")
    return 0 if report.ready else 1


def _sidecar_transport(args: argparse.Namespace, profile):
    if args.transport == "dry-run":
        return _CliDryRunTransport()
    if args.transport == "ssh":
        return SshRemoteExecutorTransport(profile)
    raise ValueError(f"Unknown sidecar transport: {args.transport}")


class _CliDryRunTransport:
    def run(
        self,
        command: str,
        *,
        cwd: str | None = None,
        env: dict[str, str] | None = None,
        timeout_seconds: float = 30.0,
    ) -> RemoteCommandResult:
        if command == 'df -Pk "$HOME"':
            stdout = (
                "Filesystem 1024-blocks Used Available Capacity Mounted on\n"
                "/dev/root 100000000 1 99999999 1% /home\n"
            )
            return RemoteCommandResult(command=command, return_code=0, stdout=stdout)
        return RemoteCommandResult(command=command, return_code=0, stdout="ok")

    def upload_dir(self, local_path: str, remote_path: str) -> None:
        return None


def _parse_prepared_workspaces(
    values: list[str] | None,
) -> dict[str, PreparedWorkspace] | None:
    if not values:
        return None

    prepared_workspaces: dict[str, PreparedWorkspace] = {}
    for value in values:
        task_id, separator, path = value.partition("=")
        if not task_id or separator != "=" or not path.startswith("/"):
            raise ValueError("--prepared-workspace must use task_id=/remote/path")
        prepared_workspaces[task_id] = PreparedWorkspace(path=path)
    return prepared_workspaces


def _print_result(result: dict[str, Any], *, as_json: bool) -> None:
    if as_json:
        print(_json_dumps(result), end="")
        return
    if result.get("mode") == "dry_run":
        print(f"Experiment: {result.get('experiment_name')}")
        print("Mode: dry-run")
        print(f"Rounds: {result.get('round_count')}")
        for task in result.get("tasks") or []:
            print(f"- {task.get('task_id')}: {len(task.get('rounds') or [])} round(s)")
        plan_path = result.get("plan_path")
        if plan_path:
            print(f"Plan: {plan_path}")
        return

    print(f"Experiment: {result.get('experiment_name')}")
    print(f"Status: {result.get('status')}")
    for task in result.get("tasks") or []:
        rounds = task.get("rounds") or []
        print(f"- {task.get('task_id')}: {len(rounds)} round(s)")
        for round_result in rounds:
            job_count = len(round_result.get("jobs") or [])
            print(
                "  "
                f"round {round_result.get('round_index')}: "
                f"rollout={round_result.get('rollout_status')} jobs={job_count}"
            )
    summary_path = result.get("summary_path")
    if summary_path:
        print(f"Summary: {summary_path}")


def _json_dumps(result: dict[str, Any]) -> str:
    return json.dumps(result, indent=2, sort_keys=True, allow_nan=False) + "\n"
