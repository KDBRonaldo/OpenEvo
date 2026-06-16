from __future__ import annotations

import argparse
from pathlib import Path
import time

from polar_evolution.methods import METHOD_REGISTRY
from polar_evolution.server import create_app
from polar_evolution.worker import EvolutionWorkerClient, run_once


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="polar-evolution")
    subparsers = parser.add_subparsers(dest="command", required=True)
    serve = subparsers.add_parser("serve", help="Start the Evolution Backend.")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8200)
    serve.add_argument("--db", default=".polar_evolution/evolution.db")
    serve.add_argument("--artifact-root", default=".polar_evolution")
    worker = subparsers.add_parser("worker", help="Run an Evolution reference worker.")
    worker.add_argument("--base-url", default="http://127.0.0.1:8200")
    worker.add_argument("--worker-id", default="reference-worker")
    worker.add_argument("--capability", action="append", default=[])
    worker.add_argument("--artifact-root", default=".polar_evolution")
    worker.add_argument("--once", action="store_true")
    worker.add_argument("--sleep-seconds", type=float, default=5.0)
    worker.add_argument("--lease-seconds", type=int, default=600)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "serve":
        import uvicorn

        app = create_app(db_path=Path(args.db), artifact_root=Path(args.artifact_root))
        uvicorn.run(app, host=args.host, port=args.port)
        return 0
    if args.command == "worker":
        capabilities = _parse_capabilities(args.capability)
        artifact_root = Path(args.artifact_root)
        with EvolutionWorkerClient(args.base_url) as client:
            while True:
                claimed = run_once(
                    client,
                    worker_id=args.worker_id,
                    capabilities=capabilities,
                    artifact_root=artifact_root,
                    lease_seconds=args.lease_seconds,
                )
                if args.once:
                    return 0
                if not claimed:
                    time.sleep(args.sleep_seconds)
    raise ValueError(f"Unknown command: {args.command}")


def _parse_capabilities(values: list[str]) -> list[str]:
    capabilities: list[str] = []
    for value in values:
        capabilities.extend(item.strip() for item in value.split(",") if item.strip())
    return capabilities or list(METHOD_REGISTRY)
