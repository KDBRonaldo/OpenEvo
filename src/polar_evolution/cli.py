from __future__ import annotations

import argparse
from pathlib import Path

from polar_evolution.server import create_app


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="polar-evolution")
    subparsers = parser.add_subparsers(dest="command", required=True)
    serve = subparsers.add_parser("serve", help="Start the Evolution Backend.")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8200)
    serve.add_argument("--db", default=".polar_evolution/evolution.db")
    serve.add_argument("--artifact-root", default=".polar_evolution")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "serve":
        import uvicorn

        app = create_app(db_path=Path(args.db), artifact_root=Path(args.artifact_root))
        uvicorn.run(app, host=args.host, port=args.port)
        return 0
    raise ValueError(f"Unknown command: {args.command}")
