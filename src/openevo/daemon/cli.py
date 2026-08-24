"""Command-line lifecycle for the remote OpenEvo daemon."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Sequence

import uvicorn

from openevo.daemon.app import create_daemon_app
from openevo.daemon.runtime import (
    DaemonProcessResult,
    DaemonProcessStatus,
    DaemonRuntime,
    DaemonRuntimePaths,
    DaemonStartOptions,
    read_token_file,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="openevo-daemon")
    subparsers = parser.add_subparsers(dest="command", required=True)

    for name in ("start", "restart"):
        command = subparsers.add_parser(name)
        command.add_argument("--state-root", type=Path, default=None)
        command.add_argument("--host", default="127.0.0.1")
        command.add_argument("--port", type=int, default=8787)
        command.add_argument("--timeout", type=float, default=10.0)

    serve = subparsers.add_parser("serve")
    serve.add_argument("--state-root", type=Path, default=None)
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8787)
    serve.add_argument("--token-file", type=Path, required=True)

    status = subparsers.add_parser("status")
    status.add_argument("--state-root", type=Path, default=None)
    stop = subparsers.add_parser("stop")
    stop.add_argument("--state-root", type=Path, default=None)
    stop.add_argument("--timeout", type=float, default=20.0)
    logs = subparsers.add_parser("logs")
    logs.add_argument("--state-root", type=Path, default=None)
    logs.add_argument("--tail", type=int, default=200)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    paths = DaemonRuntimePaths.resolve(args.state_root)
    if args.command == "serve":
        if args.host not in {"127.0.0.1", "localhost", "::1"}:
            raise SystemExit("OpenEvo daemon must bind to loopback")
        token = read_token_file(args.token_file)
        uvicorn.run(
            create_daemon_app(token=token),
            host=args.host,
            port=args.port,
            log_level="info",
        )
        return 0

    runtime = DaemonRuntime(paths=paths)
    if args.command == "status":
        status = runtime.status()
        print(json.dumps(_status_payload(status), ensure_ascii=False, sort_keys=True))
        return 0 if status.running else 1
    if args.command == "logs":
        if not 1 <= args.tail <= 2000:
            raise SystemExit("--tail must be between 1 and 2000")
        for line in runtime.read_log_tail(tail=args.tail):
            print(line)
        return 0
    if args.command == "stop":
        return _print_result(runtime.stop(timeout_s=args.timeout))

    options = DaemonStartOptions(host=args.host, port=args.port)
    result = (
        runtime.restart(options, timeout_s=args.timeout)
        if args.command == "restart"
        else runtime.start(options, timeout_s=args.timeout)
    )
    return _print_result(result)


def _print_result(result: DaemonProcessResult) -> int:
    payload = {"message": result.message, **_status_payload(result.status)}
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
    return 0 if result.ok else 1


def _status_payload(status: DaemonProcessStatus) -> dict[str, object]:
    return {
        "schema_version": "1",
        "running": status.running,
        "pid": status.pid,
        "host": status.host,
        "port": status.port,
        "started_at": status.started_at,
        "reason": status.reason,
        "state_path": str(status.state_path),
        "log_path": str(status.log_path),
    }
