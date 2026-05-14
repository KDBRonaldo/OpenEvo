#!/usr/bin/env python3
"""Submit one calculator rollout through the local Polar services."""

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
TEST_FILE = ASSETS_DIR / "test_calculator.py"
STARTER_FILE = ASSETS_DIR / "calculator.py"
DEFAULT_TOPOLOGY = EXAMPLE_DIR / "topology.yaml"
DEFAULT_IMAGE = "polar-localhost-calculator:latest"
DEFAULT_BACKEND = "docker"
DEFAULT_NUM_SAMPLES = 1
DEFAULT_TIMEOUT_SECONDS = 600.0
SUPPORTED_HARNESSES = (
    "claude_code",
    "codex",
    "gemini_cli",
    "opencode",
    "pi",
    "qwen_code",
)

BASE_INSTRUCTION = """\
`calculator.py` has a `Calculator` class with a tokenizer and three stub methods.
Each stub is marked with a `# TODO` comment and returns `0`.

Implement the three methods to build a recursive-descent expression parser:

1. `_parse_expr`  — handle `+` and `-` by calling `_parse_term`
2. `_parse_term`  — handle `*` and `/` (integer division) by calling `_parse_factor`
3. `_parse_factor` — handle integer literals and parenthesized sub-expressions

Also fix `__call__` to return the parsed value instead of `0`.

Requirements:
- Work only in `/polar/session/workspace/calculator.py`.
- Keep the existing file structure, `_tokenize`, `_peek`, and `_consume` as-is.
- Do not add imports.
- Use `//` for division (integer division).
- You must make actual edits. An empty git diff fails the task.

After editing, run `python3 test_calculator.py` and stop.

These checks must pass exactly:
- `cal("4*3-3") == 9`
- `cal("(2+3)*4") == 20`
- `cal("10/2+7") == 12`
- `cal("18-(3*4)") == 6`
- `cal(" 8 + 2 * 5 ") == 18`
"""

# Pinned versions keep the quickstart stable. Bump intentionally.
NODE_HARNESS_PACKAGES: dict[str, str] = {
    "claude_code": "@anthropic-ai/claude-code@2.1.111",
    "codex": "@openai/codex@0.121.0",
    "gemini_cli": "@google/gemini-cli@0.38.1",
    "opencode": "opencode-ai@1.4.6",
    "pi": "@mariozechner/pi-coding-agent@0.67.68",
    "qwen_code": "@qwen-code/qwen-code@0.14.5",
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
        install_command = f'npm install -g {NODE_HARNESS_PACKAGES[harness]} && '
    return install_command + WORKSPACE_PREPARE


# Common stray artifacts that can end up in cwd regardless of harness.
# The evaluator already skips __pycache__, *.pyc, *.pyo, .pytest_cache.
_COMMON_EVAL_EXCLUDES: list[str] = [
    "node_modules/**",
    "**/node_modules/**",
    ".cache/**",
    "**/.cache/**",
    ".venv/**",
    "**/.venv/**",
]

# Per-harness config / session dirs that can leak into the workspace git diff.
_HARNESS_EVAL_EXCLUDES: dict[str, list[str]] = {
    "claude_code": [".claude/**", "**/.claude/**"],
    "codex": [".codex/**", "**/.codex/**"],
    "gemini_cli": [".gemini/**", "**/.gemini/**"],
    "opencode": [".opencode/**", "**/.opencode/**", ".config/opencode/**"],
    "pi": [".pi/**", "**/.pi/**"],
    "qwen_code": [".qwen/**", "**/.qwen/**"],
}


def evaluator_exclude_patterns_for_harness(harness: str) -> list[str]:
    return [*_COMMON_EVAL_EXCLUDES, *_HARNESS_EVAL_EXCLUDES.get(harness, [])]


def model_name_for_harness(harness: str) -> str | None:
    defaults = {
        "codex": "gpt-5.4",
        "claude_code": "claude-opus-4-5",
        "gemini_cli": "gemini-2.5-flash-lite",
        "opencode": "openai/gpt-5.4",
        "pi": "openai/gpt-5.4",
        "qwen_code": "qwen3-coder-plus",
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
        default="claude_code",
        help="Harness to run. Defaults to claude_code.",
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
    test_file_abs = str(TEST_FILE.resolve())
    starter_file_abs = str(STARTER_FILE.resolve())
    runtime_image = runtime_image_for_backend(DEFAULT_IMAGE, backend)
    return {
        "task_id": f"calculator-{harness}-{batch_id}",
        "instruction": BASE_INSTRUCTION,
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
                    "source": test_file_abs,
                    "target": "/polar/session/workspace/test_calculator.py",
                },
                {
                    "type": "upload_file",
                    "source": starter_file_abs,
                    "target": "/polar/session/workspace/calculator.py",
                },
                {
                    "type": "exec",
                    "command": (
                        "cd /polar/session/workspace && "
                        "git add -A && git commit -qm 'initial'"
                    ),
                },
            ],
            "network": "host",
            "workdir": "/polar/session/workspace",
        },
        "agent": agent_spec_for_harness(harness),
        "builder": builder_spec_for_harness(harness),
        "evaluator": {
            "strategy": "test_on_output",
            "config": {
                "repo_dir": "/polar/session/workspace",
                "patch_command": (
                    "cd /polar/session/workspace && "
                    "git add -A && git diff --cached --binary"
                ),
                "test_command": (
                    "cd /polar/session/workspace && "
                    "python3 test_calculator.py && echo 'PASSED test_calculator'"
                ),
                "test_timeout": 60.0,
                "expected_output_json": {"test_calculator": "PASSED"},
                "exclude_patterns": evaluator_exclude_patterns_for_harness(harness),
            },
            "refresh_runtime": True,
        },
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
        "n/a" if reward is None else f"{reward:.1f}"
        for reward in summary["rewards"]
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
