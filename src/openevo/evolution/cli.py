from __future__ import annotations

import argparse
from pathlib import Path
import sys
import time

from openevo.evolution.framework import load_verified_framework_registry
from openevo.evolution.methods import METHOD_REGISTRY
from openevo.evolution.server import create_app
from openevo.evolution.worker import EvolutionWorkerClient, run_once


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m openevo.evolution.cli")
    subparsers = parser.add_subparsers(dest="command", required=True)

    serve = subparsers.add_parser("serve", help="Start the Evolution Backend.")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8200)
    serve.add_argument("--db", default=".openevo/evolution/evolution.db")
    serve.add_argument("--artifact-root", default=".openevo/evolution")
    serve.add_argument("--framework-lock", type=Path, required=True)

    worker = subparsers.add_parser("worker", help="Run an Evolution reference worker.")
    worker.add_argument("--base-url", default="http://127.0.0.1:8200")
    worker.add_argument("--worker-id", default="reference-worker")
    worker.add_argument("--capability", action="append", default=[])
    worker.add_argument("--artifact-root", default=".openevo/evolution")
    worker.add_argument("--once", action="store_true")
    worker.add_argument("--sleep-seconds", type=float, default=5.0)
    worker.add_argument("--lease-seconds", type=int, default=600)
    worker.add_argument("--framework-lock", type=Path)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(sys.argv[1:] if argv is None else argv)
    if args.command == "serve":
        import uvicorn

        registry = load_verified_framework_registry(args.framework_lock)
        app = create_app(
            db_path=Path(args.db),
            artifact_root=Path(args.artifact_root),
            executable_registry=registry,
        )
        uvicorn.run(app, host=args.host, port=args.port)
        return 0

    capabilities = _parse_capabilities(args.capability)
    artifact_root = Path(args.artifact_root)
    registry = (
        load_verified_framework_registry(args.framework_lock)
        if args.framework_lock is not None
        else None
    )
    with EvolutionWorkerClient(args.base_url) as client:
        while True:
            claimed = run_once(
                client,
                worker_id=args.worker_id,
                capabilities=capabilities,
                artifact_root=artifact_root,
                lease_seconds=args.lease_seconds,
                executable_registry=registry,
            )
            if args.once:
                return 0
            if not claimed:
                time.sleep(args.sleep_seconds)


def _parse_capabilities(values: list[str]) -> list[str]:
    capabilities: list[str] = []
    for value in values:
        capabilities.extend(part.strip() for part in value.split(",") if part.strip())
    return capabilities or list(METHOD_REGISTRY)


if __name__ == "__main__":
    raise SystemExit(main())
