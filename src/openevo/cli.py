from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

import httpx
import yaml
from pydantic import ValidationError

from openevo.desktop import create_desktop_app
from openevo.experiment.models import load_experiment_config
from openevo.experiment.runner import dry_run_experiment, run_experiment
from openevo.remote import (
    RemoteCommandResult,
    SshRemoteExecutorTransport,
    build_remote_bootstrap_plan,
    execute_remote_bootstrap_plan,
    execute_sidecar_plan,
)
from openevo.science import (
    PreparedWorkspace,
    compile_science_project,
    load_science_project_config,
)
from openevo.sidecar import (
    build_sidecar_science_plan,
    create_sidecar_app,
    create_sidecar_app_for_project,
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
    bootstrap_parser = sidecar_subparsers.add_parser(
        "bootstrap",
        help="Prepare a remote OpenEvo run directory and return a bootstrap report.",
    )
    bootstrap_parser.add_argument("config", help="Path to science project YAML.")
    bootstrap_parser.add_argument(
        "--remote-profile",
        required=True,
        help="Path to remote profile YAML.",
    )
    bootstrap_parser.add_argument(
        "--skip-preflight",
        action="store_true",
        help="Skip remote preflight before bootstrap preparation.",
    )
    bootstrap_parser.add_argument(
        "--transport",
        choices=("dry-run", "ssh"),
        default="dry-run",
        help="Remote executor transport to use. Defaults to dry-run.",
    )
    bootstrap_parser.add_argument("--json", action="store_true", help="Print JSON output.")
    serve_parser = sidecar_subparsers.add_parser(
        "serve",
        help="Run the local OpenEvo Desktop sidecar API.",
    )
    _add_desktop_serve_arguments(serve_parser)

    desktop_parser = subparsers.add_parser(
        "desktop",
        help="Run the packaged OpenEvo Desktop app.",
    )
    desktop_subparsers = desktop_parser.add_subparsers(
        dest="desktop_command",
        required=True,
    )
    desktop_serve_parser = desktop_subparsers.add_parser(
        "serve",
        help="Serve the packaged OpenEvo Desktop UI and local sidecar API.",
    )
    _add_desktop_serve_arguments(desktop_serve_parser)
    desktop_serve_parser.add_argument(
        "--static-root",
        help="Override the packaged OpenEvo Desktop static asset directory.",
    )
    desktop_open_parser = desktop_subparsers.add_parser(
        "open",
        help="Open the packaged OpenEvo Desktop UI in a browser.",
    )
    _add_desktop_serve_arguments(desktop_open_parser, transport_default="ssh")
    desktop_open_parser.add_argument(
        "--static-root",
        help="Override the packaged OpenEvo Desktop static asset directory.",
    )
    desktop_open_parser.add_argument(
        "--no-browser",
        action="store_true",
        help="Start the Desktop server without opening a browser.",
    )
    return parser


def _add_desktop_serve_arguments(
    parser: argparse.ArgumentParser,
    *,
    transport_default: str = "dry-run",
) -> None:
    parser.add_argument("--host", default="127.0.0.1", help="Host to bind.")
    parser.add_argument("--port", type=int, default=3766, help="Port to bind.")
    parser.add_argument(
        "--config",
        help="Optional Science Project YAML used to derive Desktop shell status.",
    )
    parser.add_argument(
        "--remote-profile",
        help="Optional remote profile YAML used with --config.",
    )
    parser.add_argument(
        "--transport",
        choices=("dry-run", "ssh"),
        default=transport_default,
        help=(
            "Remote executor transport used by sidecar mutating endpoints. "
            f"Defaults to {transport_default}."
        ),
    )
    parser.add_argument(
        "--desktop-config-root",
        help=(
            "Writable local directory for Desktop-created Science Project and "
            "remote profile configs."
        ),
    )


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
        if args.command == "desktop":
            return _handle_desktop(args)
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
    if args.sidecar_command == "bootstrap":
        return _handle_sidecar_bootstrap(args)
    if args.sidecar_command == "serve":
        return _handle_sidecar_serve(args)
    raise ValueError(f"Unknown sidecar command: {args.sidecar_command}")


def _handle_desktop(args: argparse.Namespace) -> int:
    if args.desktop_command == "serve":
        return _handle_desktop_serve(args)
    if args.desktop_command == "open":
        return _handle_desktop_open(args)
    raise ValueError(f"Unknown desktop command: {args.desktop_command}")


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


def _handle_sidecar_bootstrap(args: argparse.Namespace) -> int:
    project = load_science_project_config(Path(args.config))
    profile = load_remote_profile_config(Path(args.remote_profile))
    sidecar_plan = build_sidecar_science_plan(project, profile)
    bootstrap_plan = build_remote_bootstrap_plan(sidecar_plan)
    report = execute_remote_bootstrap_plan(
        bootstrap_plan,
        _sidecar_transport(args, profile),
        run_remote_preflight=not args.skip_preflight,
    )
    result = report.model_dump(mode="json")
    if args.json:
        print(_json_dumps(result), end="")
        return 0 if report.ready else 1
    print(yaml.safe_dump(result, sort_keys=True), end="")
    return 0 if report.ready else 1


def _handle_sidecar_serve(args: argparse.Namespace) -> int:
    app = _build_sidecar_serve_app(args, command_name="sidecar serve")
    _run_sidecar_server(app, host=args.host, port=args.port)
    return 0


def _handle_desktop_serve(args: argparse.Namespace) -> int:
    app = _build_desktop_app(args, command_name="desktop serve")
    print(f"OpenEvo Desktop: http://{args.host}:{args.port}/openevo", file=sys.stderr)
    _run_sidecar_server(app, host=args.host, port=args.port)
    return 0


def _handle_desktop_open(args: argparse.Namespace) -> int:
    app = _build_desktop_app(args, command_name="desktop open")
    sock = _bind_desktop_open_socket(host=args.host, preferred_port=args.port)
    port = int(sock.getsockname()[1])
    url = _desktop_url(host=args.host, port=port)
    print(f"OpenEvo Desktop: {url}", file=sys.stderr)
    _run_desktop_open_server(
        app,
        host=args.host,
        port=port,
        sock=sock,
        url=url,
        open_browser=not args.no_browser,
    )
    return 0


def _build_desktop_app(args: argparse.Namespace, *, command_name: str):
    app = _build_sidecar_serve_app(args, command_name=command_name)
    static_root = Path(args.static_root).expanduser() if args.static_root else None
    return create_desktop_app(app, static_root=static_root)


def _build_sidecar_serve_app(args: argparse.Namespace, *, command_name: str):
    if bool(args.config) != bool(args.remote_profile):
        raise ValueError(
            f"{command_name} --config and --remote-profile must be used together"
        )
    if args.config and args.remote_profile:
        project = load_science_project_config(Path(args.config))
        profile = load_remote_profile_config(Path(args.remote_profile))
        return create_sidecar_app_for_project(
            project,
            profile,
            transport_factory=_sidecar_transport_factory(args.transport),
            transport_kind=args.transport,
        )
    config_root = (
        Path(args.desktop_config_root).expanduser()
        if args.desktop_config_root
        else _default_desktop_config_root()
    )
    return create_sidecar_app(
        config_root=config_root,
        transport_factory=_sidecar_transport_factory(args.transport),
        transport_kind=args.transport,
    )


def _run_sidecar_server(app, *, host: str, port: int) -> None:
    import uvicorn

    uvicorn.run(app, host=host, port=port)


def _run_desktop_open_server(
    app,
    *,
    host: str,
    port: int,
    sock,
    url: str,
    open_browser: bool,
) -> None:
    server = _create_uvicorn_server(app, host=host, port=port)
    thread = _start_uvicorn_thread(server, sock=sock)
    try:
        _wait_for_desktop_url(url=url, thread=thread)
        if open_browser:
            _open_desktop_url(url)
        _join_desktop_server_thread(thread)
    except (KeyboardInterrupt, TimeoutError):
        server.should_exit = True
        _join_desktop_server_thread(thread, timeout=5.0)
        raise


def _create_uvicorn_server(app, *, host: str, port: int):
    import uvicorn

    config = uvicorn.Config(app, host=host, port=port)
    return uvicorn.Server(config)


def _start_uvicorn_thread(server, *, sock):
    import threading

    thread = threading.Thread(
        target=server.run,
        kwargs={"sockets": [sock]},
        name="openevo-desktop-server",
    )
    thread.start()
    return thread


def _join_desktop_server_thread(thread, timeout: float | None = None) -> None:
    thread.join(timeout=timeout)


def _wait_for_desktop_url(*, url: str, thread) -> None:
    import time

    deadline = time.monotonic() + 10.0
    while time.monotonic() < deadline:
        if _can_fetch_desktop_url(url):
            return
        if not thread.is_alive():
            raise TimeoutError("OpenEvo Desktop server exited before it became ready")
        time.sleep(0.05)
    raise TimeoutError(f"OpenEvo Desktop server did not become ready at {url}")


def _can_fetch_desktop_url(url: str) -> bool:
    import http.client
    from urllib.parse import urlsplit

    parsed = urlsplit(url)
    if parsed.scheme != "http" or parsed.hostname is None or parsed.port is None:
        return False
    path = parsed.path or "/"
    if parsed.query:
        path = f"{path}?{parsed.query}"
    connection = http.client.HTTPConnection(parsed.hostname, parsed.port, timeout=0.2)
    try:
        connection.request("GET", path)
        response = connection.getresponse()
        response.read()
        return response.status < 500
    except OSError:
        return False
    finally:
        connection.close()


def _bind_desktop_open_socket(*, host: str, preferred_port: int):
    if preferred_port > 0:
        try:
            return _bind_server_socket(host=host, port=preferred_port)
        except OSError:
            pass
    return _bind_server_socket(host=host, port=0)


def _bind_server_socket(*, host: str, port: int):
    import socket

    family = socket.AF_INET6 if ":" in host else socket.AF_INET
    sock = socket.socket(family, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        sock.bind((host, port))
        sock.listen(socket.SOMAXCONN)
    except OSError:
        sock.close()
        raise
    return sock


def _desktop_url(*, host: str, port: int) -> str:
    browser_host = _desktop_browser_host(host)
    return f"http://{browser_host}:{port}/openevo"


def _desktop_browser_host(host: str) -> str:
    if host in {"", "0.0.0.0"}:
        return "127.0.0.1"
    if host == "::":
        return "[::1]"
    if ":" in host and not host.startswith("["):
        return f"[{host}]"
    return host


def _open_desktop_url(url: str) -> None:
    import webbrowser

    webbrowser.open(url)


def _sidecar_transport(args: argparse.Namespace, profile):
    if args.transport == "dry-run":
        return _CliDryRunTransport()
    if args.transport == "ssh":
        return SshRemoteExecutorTransport(profile)
    raise ValueError(f"Unknown sidecar transport: {args.transport}")


def _sidecar_transport_factory(transport: str):
    if transport == "dry-run":
        return lambda profile: _CliDryRunTransport()
    if transport == "ssh":
        return lambda profile: SshRemoteExecutorTransport(profile)
    raise ValueError(f"Unknown sidecar transport: {transport}")


def _default_desktop_config_root() -> Path:
    configured = os.environ.get("OPENEVO_DESKTOP_CONFIG_DIR")
    if configured:
        return Path(configured).expanduser()
    return Path.home() / ".openevo" / "desktop"


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
