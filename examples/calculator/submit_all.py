#!/usr/bin/env python3
"""Submit one calculator task to every supported harness."""

from __future__ import annotations

import argparse
import sys
import time
from datetime import UTC, datetime
from typing import Any

import httpx

from submit_calculator_task import (
    DEFAULT_BACKEND,
    DEFAULT_NUM_SAMPLES,
    DEFAULT_TOPOLOGY,
    EXAMPLE_DIR,
    SUPPORTED_HARNESSES,
    build_task_payload,
    summarize_result,
    write_json,
)

POLL_INTERVAL_SECONDS = 10.0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--backend",
        choices=["docker", "apptainer"],
        default=DEFAULT_BACKEND,
        help="Runtime backend. Defaults to docker.",
    )
    return parser.parse_args()


def resolve_rollout_url() -> str:
    from polar.config import TopologyConfig

    topo = TopologyConfig.load(DEFAULT_TOPOLOGY)
    return topo.rollout.public_url


def print_combined_summary(
    results: dict[str, dict[str, Any]],
    summaries: dict[str, dict[str, Any]],
    elapsed: float,
) -> None:
    header = f"{'Harness':<16} {'Rewards':<28} {'Mean':>6}  {'Done':>6}  {'Err':>4}"
    print("\n" + "=" * len(header))
    print(header)
    print("-" * len(header))
    for harness in results:
        s = summaries[harness]
        rtext = ", ".join(
            "n/a" if r is None else f"{r:.1f}" for r in s["rewards"]
        )
        print(
            f"{harness:<16} [{rtext:<26}] "
            f"{s['reward_mean']:>5.3f}  "
            f"{s['completed_sessions']:>2}/{s['total_sessions']:<2}  "
            f"{s['errors'] or '':>4}"
        )
    print("-" * len(header))
    all_rewards = [r for s in summaries.values() for r in s["rewards"] if r is not None]
    total_done = sum(s["completed_sessions"] for s in summaries.values())
    total_all = sum(s["total_sessions"] for s in summaries.values())
    mean = sum(all_rewards) / max(1, len(all_rewards))
    print(f"{'TOTAL':<16} {'':28} {mean:>5.3f}  {total_done:>2}/{total_all:<2}")
    print(f"Wall time: {elapsed:.0f}s")
    print("=" * len(header))


def main() -> int:
    args = parse_args()
    harnesses = list(SUPPORTED_HARNESSES)
    batch_id = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    rollout_url = resolve_rollout_url()
    batch_dir = EXAMPLE_DIR / "batches" / batch_id

    n_total = len(harnesses) * DEFAULT_NUM_SAMPLES
    print(
        f"Submitting {len(harnesses)} harnesses x {DEFAULT_NUM_SAMPLES} "
        f"samples = {n_total} sessions"
    )
    print(f"Rollout URL: {rollout_url}")
    print(f"Runtime backend: {args.backend}")

    # 1. Build and submit all tasks (async endpoint)
    timeout = httpx.Timeout(None, connect=30.0)
    task_ids: dict[str, str] = {}  # harness -> task_id

    with httpx.Client(base_url=rollout_url, timeout=timeout) as client:
        for harness in harnesses:
            payload = build_task_payload(harness, batch_id, backend=args.backend)
            out_dir = batch_dir / harness
            write_json(out_dir / "request.json", payload)

            resp = client.post("/rollout/task/submit", json=payload)
            resp.raise_for_status()
            data = resp.json()
            task_ids[harness] = data["task_id"]
            print(f"  {harness:<16} -> {data['task_id']}")

        # 2. Poll until all tasks finish
        print(f"\nPolling every {POLL_INTERVAL_SECONDS:.0f}s ...")
        t0 = time.monotonic()
        finished: dict[str, dict[str, Any]] = {}

        while len(finished) < len(harnesses):
            time.sleep(POLL_INTERVAL_SECONDS)
            sessions_done = sum(s["completed_sessions"] for s in finished.values())
            newly_done: list[str] = []
            for harness, tid in task_ids.items():
                if harness in finished:
                    continue
                resp = client.get(f"/rollout/task/{tid}")
                resp.raise_for_status()
                task_status = resp.json()
                sessions_done += task_status["completed_sessions"]
                if task_status["status"] != "running":
                    finished[harness] = task_status
                    newly_done.append(harness)

            elapsed = time.monotonic() - t0
            if newly_done:
                # Clear progress line then print completion
                sys.stdout.write("\r" + " " * 60 + "\r")
                for h in newly_done:
                    d = finished[h]["completed_sessions"]
                    t = finished[h]["total_sessions"]
                    print(f"  [{elapsed:>5.0f}s] {h:<16} done ({d}/{t})")
            else:
                sys.stdout.write(
                    f"\r  [{elapsed:>5.0f}s] {sessions_done}/{n_total} sessions, "
                    f"{len(finished)}/{len(harnesses)} tasks done"
                )
                sys.stdout.flush()

        elapsed = time.monotonic() - t0
        print()

    # 3. Save results and print summary
    summaries: dict[str, dict[str, Any]] = {}
    for harness in harnesses:
        result = finished[harness]
        out_dir = batch_dir / harness
        write_json(out_dir / "response.json", result)
        summary = summarize_result(result)
        write_json(out_dir / "summary.json", summary)
        summaries[harness] = summary

    print_combined_summary(finished, summaries, elapsed)
    print(f"\nResults saved to {batch_dir}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
