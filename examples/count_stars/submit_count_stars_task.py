#!/usr/bin/env python3
"""Submit one count_stars rollout through the local Polar services."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

EXAMPLE_DIR = Path(__file__).resolve().parent
ASSETS_DIR = EXAMPLE_DIR / "assets"
IMAGE_FILE = ASSETS_DIR / "polar_stars.png"
DEFAULT_TOPOLOGY = EXAMPLE_DIR / "topology.yaml"
DEFAULT_IMAGE = "polar-localhost-count-stars:latest"
DEFAULT_BACKEND = "docker"
DEFAULT_NUM_SAMPLES = 1
DEFAULT_TIMEOUT_SECONDS = 300.0
RUNTIME_IMAGE_PATH = "/polar/session/workspace/polar_stars.png"
SUPPORTED_HARNESSES = (
    "claude_code",
    "codex",
    "gemini_cli",
)

TASK_BODY = """\
Use your image viewing tool to inspect `/polar/session/workspace/polar_stars.png`.
Count the visible stars in that image.

Write the answer as a single integer line to `/polar/session/workspace/answer.txt`.
Do not write any other text to that file. Stop after writing the file.
"""

NODE_HARNESS_PACKAGES: dict[str, str] = {
    "claude_code": "@anthropic-ai/claude-code@2.1.111",
    "codex": "@openai/codex@0.121.0",
    "gemini_cli": "@google/gemini-cli@0.38.1",
}

WORKSPACE_PREPARE = (
    "rm -rf /polar/session/workspace && "
    "mkdir -p /polar/session/workspace /polar/session/logs/agent && "
    "cd /polar/session/workspace && "
    "git init -q && "
    "git config user.email 'polar@test' && "
    "git config user.name 'Polar'"
)


def prepare_command_for_harness(harness: str) -> str:
    install_command = ""
    if harness in NODE_HARNESS_PACKAGES:
        install_command = f"npm install -g {NODE_HARNESS_PACKAGES[harness]} && "
    return install_command + WORKSPACE_PREPARE


def instruction_for_harness(harness: str) -> str:
    if harness not in SUPPORTED_HARNESSES:
        raise ValueError(f"Unsupported harness: {harness}")
    return TASK_BODY


def model_name_for_harness(harness: str) -> str | None:
    defaults = {
        "codex": "gpt-5.4",
        "claude_code": "claude-opus-4-5",
        "gemini_cli": "gemini-2.5-flash-lite",
    }
    return defaults.get(harness)


def agent_spec_for_harness(harness: str) -> dict[str, Any]:
    spec: dict[str, Any] = {"harness": harness}
    model_name = model_name_for_harness(harness)
    if model_name is not None:
        spec["model_name"] = model_name
    return spec


def builder_spec_for_harness(harness: str) -> dict[str, Any]:
    if harness not in SUPPORTED_HARNESSES:
        raise ValueError(f"Unsupported harness: {harness}")
    return {"strategy": "prefix_merging"}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "harness",
        nargs="?",
        choices=SUPPORTED_HARNESSES,
        default="codex",
        help="Harness to run. Defaults to codex.",
    )
    parser.add_argument(
        "--backend",
        choices=["docker", "apptainer"],
        default=DEFAULT_BACKEND,
        help="Runtime backend. Defaults to docker.",
    )
    return parser.parse_args()


def build_task_payload(
    harness: str,
    batch_id: str,
    *,
    backend: str = DEFAULT_BACKEND,
) -> dict[str, Any]:
    image_file_abs = str(IMAGE_FILE.resolve())
    runtime_image = runtime_image_for_backend(DEFAULT_IMAGE, backend)
    return {
        "task_id": f"count-stars-{harness}-{batch_id}",
        "instruction": instruction_for_harness(harness),
        "num_samples": DEFAULT_NUM_SAMPLES,
        "timeout_seconds": DEFAULT_TIMEOUT_SECONDS,
        "runtime": {
            "backend": backend,
            "image": runtime_image,
            "prepare": [
                {
                    "type": "exec",
                    "command": prepare_command_for_harness(harness),
                },
                {
                    "type": "upload_file",
                    "source": image_file_abs,
                    "target": RUNTIME_IMAGE_PATH,
                },
            ],
            "network": "host",
            "workdir": "/polar/session/workspace",
        },
        "agent": agent_spec_for_harness(harness),
        "builder": builder_spec_for_harness(harness),
        "evaluator": {"strategy": "session_completed"},
    }


def runtime_image_for_backend(image: str, backend: str) -> str:
    if backend != "apptainer":
        return image
    if image.startswith(("docker-daemon:", "docker://", "oras://")):
        return image
    return f"docker-daemon:{image}"


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=True, sort_keys=True))


def summarize_result(response: dict[str, Any]) -> dict[str, Any]:
    sessions = response.get("results") or []
    rewards: list[float | None] = []
    completed = 0
    errors = 0
    for session in sessions:
        if session.get("status") == "COMPLETED":
            completed += 1
        if session.get("error"):
            errors += 1
        trajectory = session.get("trajectory") or {}
        if trajectory.get("status") == "ERROR" or trajectory.get("error"):
            errors += 1
        traces = trajectory.get("traces") or []
        reward = traces[-1].get("reward") if traces else None
        rewards.append(float(reward) if isinstance(reward, (int, float)) else None)
    return {
        "completed_sessions": completed,
        "errors": errors,
        "rewards": rewards,
        "reward_mean": (
            sum(reward for reward in rewards if reward is not None)
            / max(1, sum(1 for reward in rewards if reward is not None))
        ),
        "total_sessions": len(sessions),
    }


def print_reward_summary(harness: str, summary: dict[str, Any]) -> None:
    reward_text = ", ".join(
        "n/a" if reward is None else f"{reward:.1f}" for reward in summary["rewards"]
    )
    print("\nReward summary")
    print(f"Harness:    {harness}")
    print(f"Rewards:    [{reward_text}]")
    print(f"Mean:       {summary['reward_mean']:.3f}")
    print(f"Completed:  {summary['completed_sessions']}/{summary['total_sessions']}")
    if summary["errors"]:
        print(f"Errors:     {summary['errors']}")


def main() -> int:
    args = parse_args()
    batch_id = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    payload = build_task_payload(args.harness, batch_id, backend=args.backend)
    output_dir = EXAMPLE_DIR / "batches" / batch_id / args.harness
    request_path = output_dir / "request.json"
    response_path = output_dir / "response.json"
    write_json(request_path, payload)
    print(f"Wrote request to {request_path}")

    command = [
        sys.executable,
        "-m",
        "polar.cli",
        "submit",
        str(request_path),
        "-c",
        str(DEFAULT_TOPOLOGY),
        "--json",
    ]

    completed = subprocess.run(
        command,
        check=True,
        capture_output=True,
        text=True,
    )
    result = json.loads(completed.stdout)
    write_json(response_path, result)
    print(f"Task completed. Wrote response to {response_path}")
    summary = summarize_result(result)
    write_json(output_dir / "summary.json", summary)
    print_reward_summary(args.harness, summary)

    return 0


if __name__ == "__main__":
    sys.exit(main())
