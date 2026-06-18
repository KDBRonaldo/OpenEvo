from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
import time

from polar_evolution.methods import METHOD_REGISTRY
from polar_evolution.models import DatasetCreateRequest
from polar_evolution.server import create_app
from polar_evolution.store import EvolutionStore
from polar_evolution.terminal_bench_bridge import build_terminal_bench_events
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
    tb_events = subparsers.add_parser(
        "terminal-bench-events",
        help="Convert Terminal Bench Harbor/EvoLab results to Polar event JSONL.",
    )
    tb_events.add_argument("--input", required=True, help="Terminal Bench trial or job directory.")
    tb_events.add_argument("--output", help="Output JSONL path. Defaults to stdout.")
    tb_events.add_argument("--max-transcript-chars", type=int, default=60000)
    tb_events.add_argument("--max-verifier-stdout-chars", type=int, default=12000)
    tb_events.add_argument("--policy-version")
    tb_events.add_argument("--rollout-step", type=int)
    tb_dataset = subparsers.add_parser(
        "terminal-bench-dataset",
        help="Ingest Terminal Bench Harbor/EvoLab results into a local Polar dataset.",
    )
    tb_dataset.add_argument("--input", required=True, help="Terminal Bench trial or job directory.")
    tb_dataset.add_argument("--db", default=".polar_evolution/evolution.db")
    tb_dataset.add_argument("--artifact-root", default=".polar_evolution")
    tb_dataset.add_argument("--name", required=True)
    tb_dataset.add_argument("--purpose", default="agent_system_reflection")
    tb_dataset.add_argument("--policy-version")
    tb_dataset.add_argument("--rollout-step", type=int)
    tb_dataset.add_argument("--status", action="append", default=["COMPLETED"])
    tb_dataset.add_argument("--output", help="Output JSON summary path. Defaults to stdout.")
    tb_dataset.add_argument("--max-transcript-chars", type=int, default=60000)
    tb_dataset.add_argument("--max-verifier-stdout-chars", type=int, default=12000)
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
    if args.command == "terminal-bench-events":
        events = build_terminal_bench_events(
            args.input,
            max_transcript_chars=args.max_transcript_chars,
            max_verifier_stdout_chars=args.max_verifier_stdout_chars,
            policy_version=args.policy_version,
            rollout_step=args.rollout_step,
        )
        lines = [
            json.dumps(event.model_dump(mode="json"), sort_keys=True, allow_nan=False)
            for event in events
        ]
        payload = "".join(f"{line}\n" for line in lines)
        if args.output:
            output_path = Path(args.output)
            output_path.parent.mkdir(parents=True, exist_ok=True)
            output_path.write_text(payload, encoding="utf-8")
        else:
            sys.stdout.write(payload)
        return 0
    if args.command == "terminal-bench-dataset":
        events = build_terminal_bench_events(
            args.input,
            max_transcript_chars=args.max_transcript_chars,
            max_verifier_stdout_chars=args.max_verifier_stdout_chars,
            policy_version=args.policy_version,
            rollout_step=args.rollout_step,
        )
        store = EvolutionStore(db_path=Path(args.db), artifact_root=Path(args.artifact_root))
        store.initialize()
        ingested_events = []
        for event in events:
            response = store.ingest_event(event)
            ingested_events.append(
                {
                    "event_id": response.event_id,
                    "ingested": response.ingested,
                    "duplicate": response.duplicate,
                    "task_id": event.task_id,
                    "session_id": event.session_id,
                }
            )
        dataset = store.create_dataset(
            DatasetCreateRequest(
                name=args.name,
                purpose=args.purpose,
                query={
                    "event_types": ["polar.session_completed"],
                    "status": args.status,
                    "policy_version": args.policy_version,
                },
            )
        )
        payload = {
            "ingested_events": ingested_events,
            "dataset": {
                "dataset_id": dataset.dataset_id,
                "artifact_id": dataset.artifact_id,
                "name": args.name,
                "purpose": args.purpose,
                "event_count": dataset.event_count,
                "trace_count": dataset.trace_count,
                "manifest_uri": _artifact_uri(store, dataset.artifact_id),
            },
        }
        _write_json_output(payload, args.output)
        return 0
    raise ValueError(f"Unknown command: {args.command}")


def _parse_capabilities(values: list[str]) -> list[str]:
    capabilities: list[str] = []
    for value in values:
        capabilities.extend(item.strip() for item in value.split(",") if item.strip())
    return capabilities or list(METHOD_REGISTRY)


def _artifact_uri(store: EvolutionStore, artifact_id: str) -> str:
    with store.connect() as conn:
        row = conn.execute(
            "SELECT uri FROM artifacts WHERE artifact_id = ?",
            (artifact_id,),
        ).fetchone()
    if row is None:
        raise ValueError(f"artifact not found: {artifact_id}")
    return str(row["uri"])


def _write_json_output(payload: dict, output: str | None) -> None:
    text = json.dumps(payload, indent=2, sort_keys=True, allow_nan=False)
    if output:
        output_path = Path(output)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(f"{text}\n", encoding="utf-8")
    else:
        sys.stdout.write(f"{text}\n")
