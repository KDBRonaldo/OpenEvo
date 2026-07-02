from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
import time
from typing import Any
from urllib.parse import unquote, urlparse

from polar_evolution.agent_system import DEFAULT_AGENT_SYSTEM_TARGET_PATH
from polar_evolution.methods import METHOD_REGISTRY, _parametric_memory_training_projection
from polar_evolution.models import DatasetCreateRequest, EventIngestRequest, JobCreateRequest
from polar_evolution.server import create_app
from polar_evolution.store import EvolutionStore
from polar_evolution.terminal_bench_bridge import build_terminal_bench_events
from polar_evolution.terminal_bench_local_parametric import (
    ADAPTER_KEY_REWRITE_CHOICES,
    ADAPTER_KEY_REWRITE_NONE,
    DEFAULT_LOCAL_MODEL,
    DEFAULT_LOCAL_PARAMETRIC_CONTEXT_RESERVE_TOKENS,
    DEFAULT_LOCAL_PARAMETRIC_CONTEXT_WINDOW_TOKENS,
    DEFAULT_LOCAL_PARAMETRIC_ADAPTER_ID,
    DEFAULT_LOCAL_PARAMETRIC_MAX_OUTPUT_TOKENS,
    DEFAULT_VLLM_EXECUTABLE,
    run_local_parametric_memory_eval,
    run_local_parametric_memory_eval_dry_run,
)
from polar_evolution.terminal_bench_per_task import (
    DEFAULT_TERMINAL_BENCH_PACKAGE_ROOT,
    TerminalBenchTaskGroup,
    _run_worker_once_local,
    run_group_evolution,
    run_group_evolution_dry_run,
    run_per_task_evolution,
    run_per_task_evolution_dry_run,
)
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
    tb_dataset.add_argument(
        "--input", required=True, help="Terminal Bench trial or job directory."
    )
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
    tb_job = subparsers.add_parser(
        "terminal-bench-agent-system-job",
        help=("Ingest Terminal Bench results and create an audited agent-system reflector job."),
    )
    tb_job.add_argument(
        "--input",
        action="append",
        default=[],
        help="Terminal Bench trial or job directory to ingest. Can be repeated.",
    )
    tb_job.add_argument("--db", default=".polar_evolution/evolution.db")
    tb_job.add_argument("--artifact-root", default=".polar_evolution")
    tb_job.add_argument(
        "--dataset-artifact-id",
        action="append",
        default=[],
        help="Existing dataset artifact id to use as job input. Can be repeated.",
    )
    tb_job.add_argument(
        "--dataset-name",
        help="Dataset name when --input is provided.",
    )
    tb_job.add_argument("--purpose", default="agent_system_reflection")
    tb_job.add_argument("--policy-version")
    tb_job.add_argument("--rollout-step", type=int)
    tb_job.add_argument("--status", action="append", default=["COMPLETED"])
    tb_job.add_argument("--output", help="Output JSON summary path. Defaults to stdout.")
    tb_job.add_argument("--max-transcript-chars", type=int, default=60000)
    tb_job.add_argument("--max-verifier-stdout-chars", type=int, default=12000)
    tb_job.add_argument(
        "--method",
        choices=[
            "auto",
            "agent_system_reflector",
            "agent_system_history_reflector",
            "agent_system_pareto_reflector",
            "agent_system_gepa_reflector",
        ],
        default="auto",
    )
    tb_job.add_argument("--candidate-count", type=int)
    tb_job.add_argument("--mutation-strategy", action="append", default=[])
    tb_job.add_argument("--target-path", default=DEFAULT_AGENT_SYSTEM_TARGET_PATH)
    tb_job.add_argument("--job-name")
    tb_job.add_argument("--priority", type=int, default=100)
    tb_job.add_argument("--max-records", type=int)
    tb_job.add_argument("--reflector-provider", default="openai_chat")
    tb_job.add_argument("--reflector-model", required=True)
    tb_job.add_argument("--reflector-base-url")
    tb_job.add_argument("--reflector-api-key-env")
    tb_job.add_argument("--codex-home")
    tb_job.add_argument("--temperature", type=float)
    tb_job.add_argument("--max-tokens", type=int)
    tb_job.add_argument("--reflector-timeout-seconds", type=float)
    tb_job.add_argument("--audit-max-repair-attempts", type=int, default=2)
    tb_job.add_argument(
        "--audit-forbidden-literal",
        action="append",
        default=[],
        help="Additional exact literal that the generated agent system must not contain.",
    )
    tb_job.add_argument(
        "--no-auto-forbidden-literals",
        action="store_true",
        help="Do not derive forbidden literals from structured protected metadata.",
    )
    tb_memory_job = subparsers.add_parser(
        "terminal-bench-text-memory-job",
        help="Ingest Terminal Bench results and create a text-memory reflector job.",
    )
    tb_memory_job.add_argument(
        "--input",
        action="append",
        default=[],
        help="Terminal Bench trial or job directory to ingest. Can be repeated.",
    )
    tb_memory_job.add_argument("--db", default=".polar_evolution/evolution.db")
    tb_memory_job.add_argument("--artifact-root", default=".polar_evolution")
    tb_memory_job.add_argument(
        "--dataset-artifact-id",
        action="append",
        default=[],
        help="Existing dataset artifact id to use as job input. Can be repeated.",
    )
    tb_memory_job.add_argument(
        "--dataset-name",
        help="Dataset name when --input is provided.",
    )
    tb_memory_job.add_argument("--purpose", default="text_memory_reflection")
    tb_memory_job.add_argument("--policy-version")
    tb_memory_job.add_argument("--rollout-step", type=int)
    tb_memory_job.add_argument("--status", action="append", default=["COMPLETED", "ERROR"])
    tb_memory_job.add_argument("--output", help="Output JSON summary path. Defaults to stdout.")
    tb_memory_job.add_argument("--max-transcript-chars", type=int, default=60000)
    tb_memory_job.add_argument("--max-verifier-stdout-chars", type=int, default=12000)
    tb_memory_job.add_argument(
        "--method",
        choices=[
            "text_memory_reflector",
            "text_memory_expel_reflector",
        ],
        default="text_memory_expel_reflector",
    )
    tb_memory_job.add_argument("--job-name")
    tb_memory_job.add_argument("--priority", type=int, default=100)
    tb_memory_job.add_argument("--max-records", type=int)
    tb_memory_job.add_argument("--reflector-provider", default="openai_chat")
    tb_memory_job.add_argument("--reflector-model", required=True)
    tb_memory_job.add_argument("--reflector-base-url")
    tb_memory_job.add_argument("--reflector-api-key-env")
    tb_memory_job.add_argument("--codex-home")
    tb_memory_job.add_argument("--temperature", type=float)
    tb_memory_job.add_argument("--max-tokens", type=int)
    tb_memory_job.add_argument("--reflector-timeout-seconds", type=float)
    tb_memory_job.add_argument(
        "--audit-forbidden-literal",
        action="append",
        default=[],
        help="Additional exact literal that the generated memory must not contain.",
    )
    tb_memory_job.add_argument(
        "--no-auto-forbidden-literals",
        action="store_true",
        help="Do not derive forbidden literals from structured protected metadata.",
    )
    tb_parametric_job = subparsers.add_parser(
        "terminal-bench-parametric-memory-job",
        help="Ingest Terminal Bench results and create a parametric-memory LoRA SFT job.",
    )
    tb_parametric_job.add_argument(
        "--input",
        action="append",
        default=[],
        help="Terminal Bench trial or job directory to ingest. Can be repeated.",
    )
    tb_parametric_job.add_argument(
        "--dataset-artifact-id",
        action="append",
        default=[],
        help="Existing dataset artifact id to use as job input. Can be repeated.",
    )
    tb_parametric_job.add_argument("--db", default=".polar_evolution/evolution.db")
    tb_parametric_job.add_argument("--artifact-root", default=".polar_evolution")
    tb_parametric_job.add_argument("--dataset-name")
    tb_parametric_job.add_argument("--purpose", default="parametric_memory_lora_sft")
    tb_parametric_job.add_argument("--policy-version")
    tb_parametric_job.add_argument("--rollout-step", type=int)
    tb_parametric_job.add_argument("--status", action="append", default=["COMPLETED"])
    tb_parametric_job.add_argument("--output", help="Output JSON summary path. Defaults to stdout.")
    tb_parametric_job.add_argument("--max-transcript-chars", type=int, default=60000)
    tb_parametric_job.add_argument("--max-verifier-stdout-chars", type=int, default=12000)
    tb_parametric_job.add_argument("--base-model", required=True)
    tb_parametric_job.add_argument(
        "--adapter-id",
        default=DEFAULT_LOCAL_PARAMETRIC_ADAPTER_ID,
    )
    tb_parametric_job.add_argument("--adapter-format", default="lora")
    tb_parametric_job.add_argument("--trainer-command", required=True)
    tb_parametric_job.add_argument("--trainer-arg", action="append", default=[])
    tb_parametric_job.add_argument("--trainer-timeout-seconds", type=float, default=3600.0)
    tb_parametric_job.add_argument(
        "--training-projection",
        choices=[
            "full_trace",
            "response_tail",
            "terminal_bench_final_actions",
            "terminal_bench_tool_call_policy",
            "terminal_bench_corrective_tool_call_policy",
            "terminal_bench_password_recovery_shorttarget_recipe",
        ],
        default="full_trace",
        help="Projection applied when exporting traces to SFT JSONL.",
    )
    tb_parametric_job.add_argument(
        "--training-response-tail-chars",
        type=int,
        help="Response tail size used with --training-projection response_tail.",
    )
    tb_parametric_job.add_argument(
        "--training-final-action-max-events",
        type=int,
        default=8,
        help=(
            "Maximum completed command/message events to keep with "
            "--training-projection terminal_bench_final_actions."
        ),
    )
    tb_parametric_job.add_argument(
        "--training-final-action-output-chars",
        type=int,
        default=2000,
        help=(
            "Maximum command output excerpt length with "
            "--training-projection terminal_bench_final_actions."
        ),
    )
    tb_parametric_job.add_argument(
        "--training-tool-call-max-commands",
        type=int,
        default=1,
        help=(
            "Maximum tb_exec commands to export with --training-projection "
            "terminal_bench_tool_call_policy."
        ),
    )
    tb_parametric_job.add_argument(
        "--training-tool-call-command-contains",
        action="append",
        default=[],
        help=(
            "Substring filter for commands exported with terminal_bench_tool_call_policy. "
            "Can be repeated."
        ),
    )
    tb_parametric_job.add_argument(
        "--training-tool-call-exclude-command-contains",
        action="append",
        default=[],
        help=(
            "Substring exclusion filter for commands exported with "
            "terminal_bench_tool_call_policy. Can be repeated."
        ),
    )
    tb_parametric_job.add_argument(
        "--training-tool-call-derive-password-recovery-command",
        action="store_true",
        help=(
            "Derive a direct tb_exec write command from password-recovery successful "
            "transcripts when using terminal_bench_tool_call_policy."
        ),
    )
    tb_parametric_job.add_argument(
        "--training-corrective-input-contains",
        action="append",
        default=[],
        help=(
            "Substring that must appear in a compact real LLM-call prefix exported "
            "with terminal_bench_corrective_tool_call_policy. Can be repeated."
        ),
    )
    tb_parametric_job.add_argument(
        "--training-corrective-max-examples",
        type=int,
        default=64,
        help=(
            "Maximum real LLM-call prefixes to export per trace with "
            "terminal_bench_corrective_tool_call_policy."
        ),
    )
    tb_parametric_job.add_argument(
        "--training-corrective-max-input-tool-messages",
        type=int,
        help=(
            "Keep only the last N tool-result input messages in each real prefix "
            "exported with terminal_bench_corrective_tool_call_policy."
        ),
    )
    tb_parametric_job.add_argument(
        "--training-corrective-target-command",
        help=(
            "Target tb_exec command for terminal_bench_corrective_tool_call_policy."
        ),
    )
    tb_parametric_job.add_argument(
        "--training-corrective-target-task-id",
        default="terminal-bench-task",
        help=(
            "Target task_id argument for terminal_bench_corrective_tool_call_policy."
        ),
    )
    tb_parametric_job.add_argument(
        "--training-corrective-stage-json",
        action="append",
        default=[],
        help=(
            "JSON object for one terminal_bench_corrective_tool_call_policy stage. "
            "Can be repeated and takes precedence over the single target-command flags."
        ),
    )
    tb_parametric_job.add_argument(
        "--training-recipe-target-command",
        help=(
            "Target tb_exec command for "
            "terminal_bench_password_recovery_shorttarget_recipe."
        ),
    )
    tb_parametric_job.add_argument(
        "--training-recipe-target-task-id",
        default="terminal-bench-task",
        help=(
            "Target task_id argument for "
            "terminal_bench_password_recovery_shorttarget_recipe."
        ),
    )
    tb_parametric_job.add_argument(
        "--training-recipe-read-task-input-contains",
        action="append",
        default=[],
        help=(
            "Substring filter for the read-task recipe stage. Defaults to the "
            "Terminal Bench Harbor marker when omitted. Can be repeated."
        ),
    )
    tb_parametric_job.add_argument(
        "--training-recipe-after-read-input-contains",
        action="append",
        default=[],
        help=(
            "Substring filter for the after-read recipe stage. Defaults to "
            "recovered_passwords.txt when omitted. Can be repeated."
        ),
    )
    tb_parametric_job.add_argument(
        "--training-recipe-correction-input-contains",
        action="append",
        default=[],
        help=(
            "Substring filter enabling the optional correction recipe stage. "
            "Can be repeated."
        ),
    )
    tb_parametric_job.add_argument(
        "--training-recipe-read-task-max-examples",
        type=int,
        default=1,
        help="Maximum read-task recipe examples to export per trace.",
    )
    tb_parametric_job.add_argument(
        "--training-recipe-after-read-max-examples",
        type=int,
        default=1,
        help="Maximum after-read recipe examples to export per trace.",
    )
    tb_parametric_job.add_argument(
        "--training-recipe-after-read-repeat",
        type=int,
        default=6,
        help="Repeat count for each after-read recipe example.",
    )
    tb_parametric_job.add_argument(
        "--training-recipe-correction-max-examples",
        type=int,
        default=1,
        help="Maximum optional correction recipe examples to export per trace.",
    )
    tb_parametric_job.add_argument(
        "--training-recipe-correction-repeat",
        type=int,
        default=1,
        help="Repeat count for each optional correction recipe example.",
    )
    tb_parametric_job.add_argument(
        "--training-recipe-max-input-tool-messages",
        type=int,
        help="Keep only the last N tool-result input messages in recipe stages.",
    )
    tb_parametric_job.add_argument("--run-worker", action="store_true")
    tb_parametric_job.add_argument("--job-name")
    tb_parametric_job.add_argument("--priority", type=int, default=100)
    tb_parametric_job.add_argument("--max-records", type=int)
    tb_per_task = subparsers.add_parser(
        "terminal-bench-per-task-evolution",
        help="Run or plan per-task Terminal Bench evolution.",
    )
    tb_per_task.add_argument("--task-root", required=True)
    tb_per_task.add_argument("--task-id", action="append", default=[], required=True)
    tb_per_task.add_argument("--run-root", required=True)
    tb_per_task.add_argument("--baseline-root")
    tb_per_task.add_argument(
        "--terminal-bench-package-root",
        default=str(DEFAULT_TERMINAL_BENCH_PACKAGE_ROOT),
    )
    tb_per_task.add_argument("--model", required=True)
    tb_per_task.add_argument("--reflector-model", required=True)
    tb_per_task.add_argument(
        "--reflector-provider",
        choices=["codex_cli", "openai_chat"],
        default="codex_cli",
    )
    tb_per_task.add_argument("--codex-home")
    tb_per_task.add_argument(
        "--agent-system-method",
        choices=[
            "auto",
            "agent_system_reflector",
            "agent_system_history_reflector",
            "agent_system_pareto_reflector",
            "agent_system_gepa_reflector",
        ],
        default="auto",
    )
    tb_per_task.add_argument("--gepa-candidate-count", type=int, default=1)
    tb_per_task.add_argument("--gepa-generations", type=int, default=1)
    tb_per_task.add_argument(
        "--memory-method",
        choices=[
            "text_memory_reflector",
            "text_memory_expel_reflector",
        ],
        default="text_memory_expel_reflector",
    )
    tb_per_task.add_argument("--reflector-timeout-seconds", type=float, default=180.0)
    tb_per_task.add_argument("--rounds", type=int, default=1)
    tb_per_task.add_argument("--n-attempts", type=int, default=1)
    tb_per_task.add_argument(
        "--artifact-type",
        action="append",
        default=None,
        choices=["agent_system", "skill_bundle", "memory", "text_memory", "parametric_memory"],
    )
    tb_per_task.add_argument("--env-json", default="{}")
    tb_per_task.add_argument("--verifier-env", action="append", default=[])
    tb_per_task.add_argument("--dry-run", action="store_true")
    tb_per_task.add_argument("--output", required=True)
    tb_group = subparsers.add_parser(
        "terminal-bench-group-evolution",
        help="Run or plan shared Terminal Bench evolution for one task group.",
    )
    tb_group.add_argument("--task-root", required=True)
    tb_group.add_argument("--group-id", required=True)
    tb_group.add_argument("--task-id", action="append", default=[], required=True)
    tb_group.add_argument("--objective", choices=["macro_mean_reward"], default="macro_mean_reward")
    tb_group.add_argument("--run-root", required=True)
    tb_group.add_argument("--baseline-root")
    tb_group.add_argument(
        "--terminal-bench-package-root",
        default=str(DEFAULT_TERMINAL_BENCH_PACKAGE_ROOT),
    )
    tb_group.add_argument("--model", required=True)
    tb_group.add_argument("--reflector-model", required=True)
    tb_group.add_argument(
        "--reflector-provider",
        choices=["codex_cli", "openai_chat"],
        default="codex_cli",
    )
    tb_group.add_argument("--codex-home")
    tb_group.add_argument(
        "--agent-system-method",
        choices=[
            "auto",
            "agent_system_reflector",
            "agent_system_history_reflector",
            "agent_system_pareto_reflector",
            "agent_system_gepa_reflector",
        ],
        default="auto",
    )
    tb_group.add_argument("--gepa-candidate-count", type=int, default=1)
    tb_group.add_argument("--gepa-generations", type=int, default=1)
    tb_group.add_argument("--reflector-timeout-seconds", type=float, default=180.0)
    tb_group.add_argument("--rounds", type=int, default=1)
    tb_group.add_argument(
        "--artifact-type",
        action="append",
        default=None,
        choices=["agent_system", "skill_bundle", "memory", "text_memory", "parametric_memory"],
    )
    tb_group.add_argument("--env-json", default="{}")
    tb_group.add_argument("--verifier-env", action="append", default=[])
    tb_group.add_argument("--dry-run", action="store_true")
    tb_group.add_argument("--output", required=True)
    tb_local_parametric = subparsers.add_parser(
        "terminal-bench-local-parametric-memory-eval",
        help="Run or plan local Terminal Bench parametric-memory evaluation.",
    )
    tb_local_parametric.add_argument("--task-root", required=True)
    tb_local_parametric.add_argument("--task-id", action="append", default=[], required=True)
    tb_local_parametric.add_argument("--run-root", required=True)
    tb_local_parametric.add_argument(
        "--terminal-bench-package-root",
        default=str(DEFAULT_TERMINAL_BENCH_PACKAGE_ROOT),
    )
    tb_local_parametric.add_argument("--model", default=DEFAULT_LOCAL_MODEL)
    tb_local_parametric.add_argument("--adapter-path", required=True)
    tb_local_parametric.add_argument(
        "--adapter-id",
        default=DEFAULT_LOCAL_PARAMETRIC_ADAPTER_ID,
    )
    tb_local_parametric.add_argument(
        "--adapter-key-rewrite",
        choices=ADAPTER_KEY_REWRITE_CHOICES,
        default=ADAPTER_KEY_REWRITE_NONE,
        help=(
            "Optional serving-time adapter key rewrite. Use "
            "qwen3_5_moe_vllm_language_model for PEFT adapters trained on "
            "Qwen3.5/Qwen3.6 MoE whose vLLM language model lives under "
            "language_model.model.*."
        ),
    )
    tb_local_parametric.add_argument("--adapter-artifact-id")
    tb_local_parametric.add_argument("--server-url", default="http://127.0.0.1:8000/v1")
    tb_local_parametric.add_argument("--server-port", type=int, default=8000)
    tb_local_parametric.add_argument("--vllm-executable", default=DEFAULT_VLLM_EXECUTABLE)
    tb_local_parametric.add_argument("--gpu", action="append", default=[])
    tb_local_parametric.add_argument("--n-attempts", type=int, default=1)
    tb_local_parametric.add_argument(
        "--max-output-tokens",
        type=int,
        default=DEFAULT_LOCAL_PARAMETRIC_MAX_OUTPUT_TOKENS,
        help="Maximum output tokens per local Terminal Bench solver completion.",
    )
    tb_local_parametric.add_argument(
        "--context-window-tokens",
        type=int,
        default=DEFAULT_LOCAL_PARAMETRIC_CONTEXT_WINDOW_TOKENS,
        help=(
            "Serving context window used for local parametric-memory evaluation "
            "and managed vLLM --max-model-len."
        ),
    )
    tb_local_parametric.add_argument(
        "--context-reserve-tokens",
        type=int,
        default=DEFAULT_LOCAL_PARAMETRIC_CONTEXT_RESERVE_TOKENS,
        help=(
            "Maximum solver output budget reserved inside the context window. "
            "--max-output-tokens is clamped to this value."
        ),
    )
    tb_local_parametric.add_argument(
        "--tool-result-prompt-max-chars",
        type=int,
        help=(
            "Optional maximum tool-result characters that the Terminal Bench "
            "EvoLab agent may add back into the model prompt."
        ),
    )
    tb_local_parametric.add_argument("--manage-server", action="store_true")
    tb_local_parametric.add_argument("--server-timeout-seconds", type=float, default=600.0)
    tb_local_parametric.add_argument(
        "--auth-mode",
        choices=["local", "proxy", "subscription"],
        default="local",
    )
    tb_local_parametric.add_argument("--verifier-env", action="append", default=[])
    tb_local_parametric.add_argument(
        "--verifier-python-install-mirror",
        help=(
            "Optional UV_PYTHON_INSTALL_MIRROR value for Terminal Bench verifier "
            "uvx Python downloads."
        ),
    )
    tb_local_parametric.add_argument("--dry-run", action="store_true")
    tb_local_parametric.add_argument("--output", required=True)
    return parser


def _normalize_cli_argv(argv: list[str]) -> list[str]:
    if not argv or argv[0] != "terminal-bench-parametric-memory-job":
        return list(argv)
    normalized: list[str] = []
    index = 0
    while index < len(argv):
        item = argv[index]
        if item == "--trainer-arg" and index + 1 < len(argv):
            normalized.append(f"--trainer-arg={argv[index + 1]}")
            index += 2
            continue
        normalized.append(item)
        index += 1
    return normalized


def _json_object(value: str) -> dict[str, Any]:
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError as exc:
        raise ValueError("expected valid JSON object") from exc
    if not isinstance(parsed, dict):
        raise ValueError("expected JSON object")
    return parsed


def main(argv: list[str] | None = None) -> int:
    raw_argv = sys.argv[1:] if argv is None else argv
    args = build_parser().parse_args(_normalize_cli_argv(raw_argv))
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
    if args.command == "terminal-bench-agent-system-job":
        payload = _create_terminal_bench_agent_system_job(args)
        _write_json_output(payload, args.output)
        return 0
    if args.command == "terminal-bench-text-memory-job":
        payload = _create_terminal_bench_text_memory_job(args)
        _write_json_output(payload, args.output)
        return 0
    if args.command == "terminal-bench-parametric-memory-job":
        payload = _create_terminal_bench_parametric_memory_job(args)
        _write_json_output(payload, args.output)
        return 0
    if args.command == "terminal-bench-local-parametric-memory-eval":
        if args.auth_mode == "subscription":
            raise ValueError("parametric_memory requires local or proxy auth")
        verifier_env = _local_parametric_verifier_env(
            args.verifier_env,
            python_install_mirror=args.verifier_python_install_mirror,
        )
        if args.dry_run:
            payload = run_local_parametric_memory_eval_dry_run(
                task_root=Path(args.task_root),
                task_ids=args.task_id,
                run_root=Path(args.run_root),
                model=args.model,
                adapter_path=Path(args.adapter_path),
                adapter_id=args.adapter_id,
                server_url=args.server_url,
                n_attempts=args.n_attempts,
                max_output_tokens=args.max_output_tokens,
                context_window_tokens=args.context_window_tokens,
                context_reserve_tokens=args.context_reserve_tokens,
                tool_result_prompt_max_chars=args.tool_result_prompt_max_chars,
                manage_server=args.manage_server,
                verifier_env=verifier_env,
                auth_mode=args.auth_mode,
                adapter_key_rewrite=args.adapter_key_rewrite,
            )
            _write_json_output(payload, args.output)
            return 0
        payload = run_local_parametric_memory_eval(
            task_root=Path(args.task_root),
            task_ids=args.task_id,
            run_root=Path(args.run_root),
            terminal_bench_package_root=Path(args.terminal_bench_package_root),
            model=args.model,
            adapter_path=Path(args.adapter_path),
            adapter_id=args.adapter_id,
            adapter_artifact_id=args.adapter_artifact_id,
            server_url=args.server_url,
            n_attempts=args.n_attempts,
            max_output_tokens=args.max_output_tokens,
            context_window_tokens=args.context_window_tokens,
            context_reserve_tokens=args.context_reserve_tokens,
            tool_result_prompt_max_chars=args.tool_result_prompt_max_chars,
            verifier_env=verifier_env,
            manage_server=args.manage_server,
            server_timeout_seconds=args.server_timeout_seconds,
            vllm_executable=args.vllm_executable,
            gpus=args.gpu or None,
            port=args.server_port,
            auth_mode=args.auth_mode,
            adapter_key_rewrite=args.adapter_key_rewrite,
        )
        _write_json_output(payload, args.output)
        return 0
    if args.command == "terminal-bench-per-task-evolution":
        artifact_types = _normalize_terminal_bench_artifact_types(args.artifact_type)
        if args.dry_run:
            payload = run_per_task_evolution_dry_run(
                task_root=Path(args.task_root),
                task_ids=args.task_id,
                run_root=Path(args.run_root),
                model=args.model,
                reflector_model=args.reflector_model,
                reflector_provider=args.reflector_provider,
                reflector_timeout_seconds=args.reflector_timeout_seconds,
                terminal_bench_package_root=Path(args.terminal_bench_package_root),
                agent_system_method=args.agent_system_method,
                memory_method=args.memory_method,
                gepa_candidate_count=args.gepa_candidate_count,
                gepa_generations=args.gepa_generations,
                rounds=args.rounds,
                artifact_types=artifact_types,
                n_attempts=args.n_attempts,
            )
            _write_json_output(payload, args.output)
            return 0
        if not args.baseline_root:
            raise ValueError(
                "terminal-bench-per-task-evolution requires --baseline-root unless --dry-run is used"
            )
        _validate_terminal_bench_live_artifact_types(artifact_types)
        try:
            parsed_env_json = json.loads(args.env_json)
        except json.JSONDecodeError as exc:
            raise ValueError("--env-json must be valid JSON") from exc
        if not isinstance(parsed_env_json, dict):
            raise ValueError("--env-json must decode to a JSON object")
        payload = run_per_task_evolution(
            task_root=Path(args.task_root),
            task_ids=args.task_id,
            run_root=Path(args.run_root),
            baseline_root=Path(args.baseline_root),
            model=args.model,
            reflector_model=args.reflector_model,
            reflector_provider=args.reflector_provider,
            reflector_timeout_seconds=args.reflector_timeout_seconds,
            codex_home=args.codex_home,
            terminal_bench_package_root=Path(args.terminal_bench_package_root),
            agent_system_method=args.agent_system_method,
            memory_method=args.memory_method,
            gepa_candidate_count=args.gepa_candidate_count,
            gepa_generations=args.gepa_generations,
            rounds=args.rounds,
            artifact_types=artifact_types,
            n_attempts=args.n_attempts,
            env_json={str(key): str(value) for key, value in parsed_env_json.items()},
            verifier_env=_parse_key_value_entries(args.verifier_env),
        )
        _write_json_output(payload, args.output)
        return 0
    if args.command == "terminal-bench-group-evolution":
        artifact_types = _normalize_terminal_bench_artifact_types(args.artifact_type)
        group = _terminal_bench_cli_group(args)
        if args.dry_run:
            payload = run_group_evolution_dry_run(
                task_root=Path(args.task_root),
                groups=[group],
                run_root=Path(args.run_root),
                model=args.model,
                reflector_model=args.reflector_model,
                reflector_provider=args.reflector_provider,
                reflector_timeout_seconds=args.reflector_timeout_seconds,
                terminal_bench_package_root=Path(args.terminal_bench_package_root),
                agent_system_method=args.agent_system_method,
                gepa_candidate_count=args.gepa_candidate_count,
                gepa_generations=args.gepa_generations,
                rounds=args.rounds,
                artifact_types=artifact_types,
            )
            _write_json_output(payload, args.output)
            return 0
        if not args.baseline_root:
            raise ValueError(
                "terminal-bench-group-evolution requires --baseline-root unless --dry-run is used"
            )
        _validate_terminal_bench_group_live_artifact_types(artifact_types)
        try:
            parsed_env_json = json.loads(args.env_json)
        except json.JSONDecodeError as exc:
            raise ValueError("--env-json must be valid JSON") from exc
        if not isinstance(parsed_env_json, dict):
            raise ValueError("--env-json must decode to a JSON object")
        payload = run_group_evolution(
            task_root=Path(args.task_root),
            groups=[group],
            run_root=Path(args.run_root),
            baseline_root=Path(args.baseline_root),
            model=args.model,
            reflector_model=args.reflector_model,
            reflector_provider=args.reflector_provider,
            reflector_timeout_seconds=args.reflector_timeout_seconds,
            codex_home=args.codex_home,
            terminal_bench_package_root=Path(args.terminal_bench_package_root),
            agent_system_method=args.agent_system_method,
            gepa_candidate_count=args.gepa_candidate_count,
            gepa_generations=args.gepa_generations,
            rounds=args.rounds,
            env_json={str(key): str(value) for key, value in parsed_env_json.items()},
            verifier_env=_parse_key_value_entries(args.verifier_env),
        )
        _write_json_output(payload, args.output)
        return 0
    raise ValueError(f"Unknown command: {args.command}")


def _normalize_terminal_bench_artifact_types(values: list[str] | None) -> list[str]:
    raw_values = values or ["agent_system"]
    normalized: list[str] = []
    for value in raw_values:
        artifact_type = value.strip()
        if artifact_type == "memory":
            artifact_type = "text_memory"
        normalized.append(artifact_type)
    return normalized


def _validate_terminal_bench_live_artifact_types(artifact_types: list[str]) -> None:
    if any(artifact_type == "parametric_memory" for artifact_type in artifact_types):
        raise ValueError("Terminal Bench Codex subscription runs do not support parametric_memory")
    if len(artifact_types) != 1:
        raise ValueError("live Terminal Bench evolution requires exactly one artifact type")
    if artifact_types[0] not in {"agent_system", "text_memory"}:
        raise ValueError(
            "live Terminal Bench evolution currently supports only agent_system or text_memory"
        )


def _validate_terminal_bench_group_live_artifact_types(artifact_types: list[str]) -> None:
    if any(artifact_type == "parametric_memory" for artifact_type in artifact_types):
        raise ValueError("Terminal Bench Codex subscription runs do not support parametric_memory")
    if artifact_types != ["agent_system"]:
        raise ValueError("live group evolution currently supports only agent_system")


def _terminal_bench_cli_group(args: argparse.Namespace) -> TerminalBenchTaskGroup:
    task_ids = list(args.task_id)
    if len(task_ids) < 2:
        raise ValueError("terminal-bench-group-evolution requires at least two --task-id values")
    return TerminalBenchTaskGroup(
        group_id=args.group_id,
        task_ids=task_ids,
        objective=args.objective,
    )


def _create_terminal_bench_agent_system_job(args: argparse.Namespace) -> dict[str, Any]:
    if not args.input and not args.dataset_artifact_id:
        raise ValueError(
            "terminal-bench-agent-system-job requires --input or --dataset-artifact-id"
        )
    if args.input and not args.dataset_name:
        raise ValueError("terminal-bench-agent-system-job requires --dataset-name with --input")
    if args.input and not args.policy_version:
        raise ValueError("terminal-bench-agent-system-job requires --policy-version with --input")

    store = EvolutionStore(db_path=Path(args.db), artifact_root=Path(args.artifact_root))
    store.initialize()

    events: list[EventIngestRequest] = []
    for input_path in args.input:
        events.extend(
            build_terminal_bench_events(
                input_path,
                max_transcript_chars=args.max_transcript_chars,
                max_verifier_stdout_chars=args.max_verifier_stdout_chars,
                policy_version=args.policy_version,
                rollout_step=args.rollout_step,
            )
        )

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

    dataset_payload: dict[str, Any] | None = None
    input_artifact_ids = list(args.dataset_artifact_id)
    if events:
        dataset = store.create_dataset(
            DatasetCreateRequest(
                name=args.dataset_name,
                purpose=args.purpose,
                query={
                    "event_types": ["polar.session_completed"],
                    "status": args.status,
                    "policy_version": args.policy_version,
                },
            )
        )
        dataset_payload = {
            "dataset_id": dataset.dataset_id,
            "artifact_id": dataset.artifact_id,
            "name": args.dataset_name,
            "purpose": args.purpose,
            "event_count": dataset.event_count,
            "trace_count": dataset.trace_count,
            "manifest_uri": _artifact_uri(store, dataset.artifact_id),
        }
        input_artifact_ids.append(dataset.artifact_id)

    method = _terminal_bench_job_method(args.method, input_artifact_ids)
    config = _terminal_bench_agent_system_job_config(
        args,
        store=store,
        input_artifact_ids=input_artifact_ids,
        events=events,
    )
    job = store.create_job(
        JobCreateRequest(
            method=method,
            job_type=method,
            input_artifact_ids=input_artifact_ids,
            config=config,
            priority=args.priority,
        )
    )
    return {
        "ingested_events": ingested_events,
        "dataset": dataset_payload,
        "job": {
            "job_id": job.job_id,
            "state": str(job.state),
            "job_type": method,
            "method": method,
            "input_artifact_ids": input_artifact_ids,
            "config": config,
        },
    }


def _create_terminal_bench_text_memory_job(args: argparse.Namespace) -> dict[str, Any]:
    if not args.input and not args.dataset_artifact_id:
        raise ValueError(
            "terminal-bench-text-memory-job requires --input or --dataset-artifact-id"
        )
    if args.input and not args.dataset_name:
        raise ValueError("terminal-bench-text-memory-job requires --dataset-name with --input")
    if args.input and not args.policy_version:
        raise ValueError("terminal-bench-text-memory-job requires --policy-version with --input")

    store = EvolutionStore(db_path=Path(args.db), artifact_root=Path(args.artifact_root))
    store.initialize()

    events: list[EventIngestRequest] = []
    for input_path in args.input:
        events.extend(
            build_terminal_bench_events(
                input_path,
                max_transcript_chars=args.max_transcript_chars,
                max_verifier_stdout_chars=args.max_verifier_stdout_chars,
                policy_version=args.policy_version,
                rollout_step=args.rollout_step,
            )
        )

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

    dataset_payload: dict[str, Any] | None = None
    previous_input_artifact_ids = list(args.dataset_artifact_id)
    input_artifact_ids: list[str] = []
    if events:
        dataset = store.create_dataset(
            DatasetCreateRequest(
                name=args.dataset_name,
                purpose=args.purpose,
                query={
                    "event_types": ["polar.session_completed"],
                    "status": args.status,
                    "policy_version": args.policy_version,
                },
            )
        )
        dataset_payload = {
            "dataset_id": dataset.dataset_id,
            "artifact_id": dataset.artifact_id,
            "name": args.dataset_name,
            "purpose": args.purpose,
            "event_count": dataset.event_count,
            "trace_count": dataset.trace_count,
            "manifest_uri": _artifact_uri(store, dataset.artifact_id),
        }
        input_artifact_ids.append(dataset.artifact_id)
    input_artifact_ids.extend(previous_input_artifact_ids)

    method = args.method
    config = _terminal_bench_text_memory_job_config(
        args,
        store=store,
        input_artifact_ids=input_artifact_ids,
        events=events,
    )
    job = store.create_job(
        JobCreateRequest(
            method=method,
            job_type=method,
            input_artifact_ids=input_artifact_ids,
            config=config,
            priority=args.priority,
        )
    )
    return {
        "ingested_events": ingested_events,
        "dataset": dataset_payload,
        "job": {
            "job_id": job.job_id,
            "state": str(job.state),
            "job_type": method,
            "method": method,
            "input_artifact_ids": input_artifact_ids,
            "config": config,
        },
    }


def _create_terminal_bench_parametric_memory_job(args: argparse.Namespace) -> dict[str, Any]:
    if not args.input and not args.dataset_artifact_id:
        raise ValueError(
            "terminal-bench-parametric-memory-job requires --input or --dataset-artifact-id"
        )
    if args.input and not args.dataset_name:
        raise ValueError(
            "terminal-bench-parametric-memory-job requires --dataset-name with --input"
        )
    if args.input and not args.policy_version:
        raise ValueError(
            "terminal-bench-parametric-memory-job requires --policy-version with --input"
        )

    missing_placeholders = [
        placeholder
        for placeholder in ("{training_dataset}", "{adapter_dir}")
        if not any(placeholder in str(arg) for arg in args.trainer_arg)
    ]
    if missing_placeholders:
        raise ValueError(
            "terminal-bench-parametric-memory-job trainer args require "
            "{training_dataset} and {adapter_dir} placeholders"
        )

    store = EvolutionStore(db_path=Path(args.db), artifact_root=Path(args.artifact_root))
    store.initialize()

    events: list[EventIngestRequest] = []
    for input_path in args.input:
        events.extend(
            build_terminal_bench_events(
                input_path,
                max_transcript_chars=args.max_transcript_chars,
                max_verifier_stdout_chars=args.max_verifier_stdout_chars,
                include_llm_calls=(
                    args.training_projection
                    in {
                        "terminal_bench_corrective_tool_call_policy",
                        "terminal_bench_password_recovery_shorttarget_recipe",
                    }
                ),
                policy_version=args.policy_version,
                rollout_step=args.rollout_step,
            )
        )

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

    dataset_payload: dict[str, Any] | None = None
    input_artifact_ids = list(args.dataset_artifact_id)
    if events:
        dataset = store.create_dataset(
            DatasetCreateRequest(
                name=args.dataset_name,
                purpose=args.purpose,
                query={
                    "event_types": ["polar.session_completed"],
                    "status": args.status,
                    "policy_version": args.policy_version,
                },
            )
        )
        dataset_payload = {
            "dataset_id": dataset.dataset_id,
            "artifact_id": dataset.artifact_id,
            "name": args.dataset_name,
            "purpose": args.purpose,
            "event_count": dataset.event_count,
            "trace_count": dataset.trace_count,
            "manifest_uri": _artifact_uri(store, dataset.artifact_id),
        }
        input_artifact_ids.append(dataset.artifact_id)

    method = "parametric_memory_lora_sft"
    config: dict[str, Any] = {
        "name": args.job_name or "Terminal Bench parametric-memory LoRA SFT",
        "base_model": args.base_model,
        "output_adapter_id": args.adapter_id,
        "adapter_format": args.adapter_format,
        "trainer": {
            "command": args.trainer_command,
            "args": list(args.trainer_arg),
            "timeout_seconds": args.trainer_timeout_seconds,
        },
        "compatibility": {
            "agent_harness": ["terminal-bench-harbor"],
            "task_tags": _terminal_bench_task_tags(store, input_artifact_ids, events),
            "base_model": [args.base_model],
        },
        "scores": {"quality": 0.0},
        "promoted": False,
    }
    if args.max_records is not None:
        config["max_records"] = args.max_records
    if args.training_projection == "response_tail":
        if args.training_response_tail_chars is None:
            raise ValueError(
                "terminal-bench-parametric-memory-job requires "
                "--training-response-tail-chars with response_tail projection"
            )
        config["training_projection"] = {
            "type": "response_tail",
            "response_tail_chars": args.training_response_tail_chars,
        }
    if args.training_projection == "terminal_bench_final_actions":
        config["training_projection"] = {
            "type": "terminal_bench_final_actions",
            "max_events": args.training_final_action_max_events,
            "max_output_chars": args.training_final_action_output_chars,
        }
    if args.training_projection == "terminal_bench_tool_call_policy":
        config["training_projection"] = {
            "type": "terminal_bench_tool_call_policy",
            "max_commands": args.training_tool_call_max_commands,
            "command_contains": list(args.training_tool_call_command_contains),
            "exclude_command_contains": list(
                args.training_tool_call_exclude_command_contains
            ),
            "derive_password_recovery_command": (
                args.training_tool_call_derive_password_recovery_command
            ),
        }
    if args.training_projection == "terminal_bench_corrective_tool_call_policy":
        if args.training_corrective_stage_json:
            config["training_projection"] = _parametric_memory_training_projection(
                {
                    "type": "terminal_bench_corrective_tool_call_policy",
                    "stages": [
                        _json_object(stage_json)
                        for stage_json in args.training_corrective_stage_json
                    ],
                }
            )
        elif not args.training_corrective_target_command:
            raise ValueError(
                "terminal-bench-parametric-memory-job requires "
                "--training-corrective-target-command with "
                "terminal_bench_corrective_tool_call_policy"
            )
        else:
            config["training_projection"] = {
                "type": "terminal_bench_corrective_tool_call_policy",
                "input_contains": list(args.training_corrective_input_contains),
                "max_examples": args.training_corrective_max_examples,
                "target_tool_call": {
                    "name": "tb_exec",
                    "arguments": {
                        "task_id": args.training_corrective_target_task_id,
                        "command": args.training_corrective_target_command,
                    },
                },
            }
            if args.training_corrective_max_input_tool_messages is not None:
                config["training_projection"]["max_input_tool_messages"] = (
                    args.training_corrective_max_input_tool_messages
                )
    if args.training_projection == "terminal_bench_password_recovery_shorttarget_recipe":
        if not args.training_recipe_target_command:
            raise ValueError(
                "terminal-bench-parametric-memory-job requires "
                "--training-recipe-target-command with "
                "terminal_bench_password_recovery_shorttarget_recipe"
            )
        recipe_projection: dict[str, Any] = {
            "type": "terminal_bench_password_recovery_shorttarget_recipe",
            "target_command": args.training_recipe_target_command,
            "target_task_id": args.training_recipe_target_task_id,
            "read_task_max_examples": args.training_recipe_read_task_max_examples,
            "after_read_max_examples": args.training_recipe_after_read_max_examples,
            "after_read_repeat": args.training_recipe_after_read_repeat,
            "correction_input_contains": list(
                args.training_recipe_correction_input_contains
            ),
            "correction_max_examples": args.training_recipe_correction_max_examples,
            "correction_repeat": args.training_recipe_correction_repeat,
        }
        if args.training_recipe_read_task_input_contains:
            recipe_projection["read_task_input_contains"] = list(
                args.training_recipe_read_task_input_contains
            )
        if args.training_recipe_after_read_input_contains:
            recipe_projection["after_read_input_contains"] = list(
                args.training_recipe_after_read_input_contains
            )
        if args.training_recipe_max_input_tool_messages is not None:
            recipe_projection["max_input_tool_messages"] = (
                args.training_recipe_max_input_tool_messages
            )
        config["training_projection"] = _parametric_memory_training_projection(
            recipe_projection
        )

    job = store.create_job(
        JobCreateRequest(
            method=method,
            job_type=method,
            input_artifact_ids=input_artifact_ids,
            config=config,
            priority=args.priority,
        )
    )
    payload: dict[str, Any] = {
        "ingested_events": ingested_events,
        "dataset": dataset_payload,
        "job": {
            "job_id": job.job_id,
            "state": str(job.state),
            "job_type": method,
            "method": method,
            "input_artifact_ids": input_artifact_ids,
            "config": config,
        },
    }
    if args.run_worker:
        payload["completed_artifacts"] = _run_worker_once_local(
            db_path=Path(args.db),
            artifact_root=Path(args.artifact_root),
        )
    return payload


def _terminal_bench_job_method(method: str, input_artifact_ids: list[str]) -> str:
    if method != "auto":
        return method
    if len(input_artifact_ids) > 1:
        return "agent_system_history_reflector"
    return "agent_system_reflector"


def _terminal_bench_agent_system_job_config(
    args: argparse.Namespace,
    *,
    store: EvolutionStore,
    input_artifact_ids: list[str],
    events: list[EventIngestRequest],
) -> dict[str, Any]:
    reflector_llm = _terminal_bench_reflector_llm_config(args)
    forbidden_literals = _terminal_bench_forbidden_literal_map(args.audit_forbidden_literal)
    if not args.no_auto_forbidden_literals:
        _merge_forbidden_literal_map(
            forbidden_literals,
            _terminal_bench_forbidden_literals_from_events(events),
        )
        _merge_forbidden_literal_map(
            forbidden_literals,
            _terminal_bench_forbidden_literals_from_dataset_artifacts(
                store,
                input_artifact_ids,
            ),
        )
    config: dict[str, Any] = {
        "name": args.job_name or "Terminal Bench agent-system reflection",
        "target_path": args.target_path,
        "reflector_llm": reflector_llm,
        "agent_system_audit": {
            "max_repair_attempts": args.audit_max_repair_attempts,
            "forbidden_literals": _unique_forbidden_literal_map(forbidden_literals),
        },
        "compatibility": {
            "agent_harness": ["terminal-bench-harbor"],
            "task_tags": _terminal_bench_task_tags(store, input_artifact_ids, events),
        },
        "scores": {"quality": 0.0},
        "promoted": False,
    }
    if args.max_records is not None:
        config["max_records"] = args.max_records
    if args.candidate_count is not None:
        config["candidate_count"] = args.candidate_count
    if args.mutation_strategy:
        config["mutation_strategies"] = list(args.mutation_strategy)
    return config


def _terminal_bench_text_memory_job_config(
    args: argparse.Namespace,
    *,
    store: EvolutionStore,
    input_artifact_ids: list[str],
    events: list[EventIngestRequest],
) -> dict[str, Any]:
    reflector_llm = _terminal_bench_reflector_llm_config(args)
    forbidden_literals = _terminal_bench_forbidden_literal_map(args.audit_forbidden_literal)
    if not args.no_auto_forbidden_literals:
        _merge_forbidden_literal_map(
            forbidden_literals,
            _terminal_bench_forbidden_literals_from_events(events),
        )
        _merge_forbidden_literal_map(
            forbidden_literals,
            _terminal_bench_forbidden_literals_from_dataset_artifacts(
                store,
                input_artifact_ids,
            ),
        )
    config: dict[str, Any] = {
        "name": args.job_name or "Terminal Bench text-memory reflection",
        "reflector_llm": reflector_llm,
        "forbidden_literals": _unique_forbidden_literal_map(forbidden_literals),
        "compatibility": {
            "agent_harness": ["terminal-bench-harbor"],
            "task_tags": _terminal_bench_task_tags(store, input_artifact_ids, events),
        },
        "scores": {"quality": 0.0},
        "promoted": False,
    }
    if args.max_records is not None:
        config["max_records"] = args.max_records
    return config


def _terminal_bench_task_tags(
    store: EvolutionStore,
    input_artifact_ids: list[str],
    events: list[EventIngestRequest],
) -> list[str]:
    tags = ["terminal-bench"]
    task_ids: list[str] = []
    for event in events:
        _append_nonempty_text(task_ids, event.task_id)
    for artifact_id in input_artifact_ids:
        manifest_path = _file_uri_to_path(_artifact_uri(store, artifact_id))
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        records_path = _dataset_records_path(manifest_path, manifest)
        for line in records_path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            record = json.loads(line)
            if isinstance(record, dict):
                _append_nonempty_text(task_ids, record.get("task_id"))
    for task_id in _unique_nonempty_text(task_ids):
        tags.append(f"terminal-bench:{task_id}")
    return tags


def _terminal_bench_reflector_llm_config(args: argparse.Namespace) -> dict[str, Any]:
    config: dict[str, Any] = {
        "provider": args.reflector_provider,
        "model": args.reflector_model,
    }
    optional_fields = {
        "base_url": args.reflector_base_url,
        "api_key_env": args.reflector_api_key_env,
        "codex_home": args.codex_home,
        "temperature": args.temperature,
        "max_tokens": args.max_tokens,
        "timeout_seconds": args.reflector_timeout_seconds,
    }
    for key, value in optional_fields.items():
        if value is not None:
            config[key] = value
    return config


def _terminal_bench_forbidden_literals_from_events(
    events: list[EventIngestRequest],
) -> dict[str, list[str]]:
    literals: dict[str, list[str]] = {}
    for event in events:
        payload = event.payload.get("session_result")
        if isinstance(payload, dict):
            _append_terminal_bench_payload_literals(literals, payload)
    return _unique_forbidden_literal_map(literals)


def _terminal_bench_forbidden_literals_from_dataset_artifacts(
    store: EvolutionStore,
    artifact_ids: list[str],
) -> dict[str, list[str]]:
    literals: dict[str, list[str]] = {}
    for artifact_id in artifact_ids:
        manifest_path = _file_uri_to_path(_artifact_uri(store, artifact_id))
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        records_path = _dataset_records_path(manifest_path, manifest)
        for line in records_path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            record = json.loads(line)
            if isinstance(record, dict):
                payload = record.get("payload")
                if isinstance(payload, dict):
                    session_result = payload.get("session_result")
                    if isinstance(session_result, dict):
                        _append_terminal_bench_payload_literals(literals, session_result)
    return _unique_forbidden_literal_map(literals)


def _dataset_records_path(manifest_path: Path, artifact_manifest: dict[str, Any]) -> Path:
    dataset_manifest = artifact_manifest.get("manifest")
    if not isinstance(dataset_manifest, dict):
        dataset_manifest = artifact_manifest
    records_uri = dataset_manifest.get("records_uri")
    if isinstance(records_uri, str) and records_uri.startswith("file://"):
        return _file_uri_to_path(records_uri)
    records_path = dataset_manifest.get("records_path") or "records.jsonl"
    return manifest_path.parent / str(records_path)


def _file_uri_to_path(uri: str) -> Path:
    parsed = urlparse(uri)
    if parsed.scheme != "file":
        raise ValueError(f"expected file URI, got: {uri}")
    return Path(unquote(parsed.path))


def _append_terminal_bench_payload_literals(
    literals: dict[str, list[str]],
    session_result: dict[str, Any],
) -> None:
    _append_structured_forbidden_literals(literals, session_result)


_STRUCTURED_FORBIDDEN_LITERAL_KEY_ALIASES = {
    "article_id": "article_ids",
    "article_ids": "article_ids",
    "article_title": "article_titles",
    "article_titles": "article_titles",
    "forbidden_literals": "terminal_bench",
    "leakage_basis": "terminal_bench",
    "sequence": "sequences",
    "sequences": "sequences",
    "source_file": "source_files",
    "source_files": "source_files",
    "source_row": "source_rows",
    "source_rows": "source_rows",
    "source_sheet": "source_sheets",
    "source_sheets": "source_sheets",
}


def _append_structured_forbidden_literals(
    literals: dict[str, list[str]],
    value: Any,
    *,
    protected_kind: str | None = None,
) -> None:
    if isinstance(value, str):
        if protected_kind:
            _append_forbidden_literal(literals, protected_kind, value)
        return
    if isinstance(value, int | float) and not isinstance(value, bool):
        if protected_kind:
            _append_forbidden_literal(literals, protected_kind, str(value))
        return
    if isinstance(value, list):
        for item in value:
            _append_structured_forbidden_literals(
                literals,
                item,
                protected_kind=protected_kind,
            )
        return
    if not isinstance(value, dict):
        return
    for key, nested in value.items():
        normalized_key = str(key).strip().lower().replace("-", "_")
        nested_kind = _STRUCTURED_FORBIDDEN_LITERAL_KEY_ALIASES.get(
            normalized_key,
            protected_kind,
        )
        _append_structured_forbidden_literals(
            literals,
            nested,
            protected_kind=nested_kind,
        )


def _terminal_bench_forbidden_literal_map(
    explicit_literals: list[str],
) -> dict[str, list[str]]:
    literals: dict[str, list[str]] = {}
    for literal in explicit_literals:
        _append_text_literal(literals.setdefault("terminal_bench", []), literal)
    return literals


def _merge_forbidden_literal_map(
    target: dict[str, list[str]],
    source: dict[str, list[str]],
) -> None:
    for key, values in source.items():
        target.setdefault(key, []).extend(values)


def _append_forbidden_literal(
    literals: dict[str, list[str]],
    kind: str,
    value: Any,
) -> None:
    if not isinstance(value, str):
        return
    text = value.strip()
    if text:
        literals.setdefault(kind, []).append(text)


def _unique_forbidden_literal_map(
    values: dict[str, list[str]],
) -> dict[str, list[str]]:
    return {
        key: unique
        for key, unique in (
            (key, _unique_nonempty_text(items)) for key, items in values.items()
        )
        if unique
    }


def _append_nonempty_text(literals: list[str], value: Any) -> None:
    if not isinstance(value, str):
        return
    text = value.strip()
    if text:
        literals.append(text)


def _append_text_literal(literals: list[str], value: Any) -> None:
    if not isinstance(value, str):
        return
    text = value.strip()
    if len(text) >= 6:
        literals.append(text)


def _unique_nonempty_text(values: list[str]) -> list[str]:
    seen: set[str] = set()
    unique: list[str] = []
    for value in values:
        text = value.strip() if isinstance(value, str) else ""
        if not text or text in seen:
            continue
        seen.add(text)
        unique.append(text)
    return unique


def _parse_capabilities(values: list[str]) -> list[str]:
    capabilities: list[str] = []
    for value in values:
        capabilities.extend(item.strip() for item in value.split(",") if item.strip())
    return capabilities or list(METHOD_REGISTRY)


def _parse_key_value_entries(entries: list[str]) -> dict[str, str]:
    parsed: dict[str, str] = {}
    for entry in entries:
        key, sep, value = entry.partition("=")
        if not sep or not key:
            raise ValueError(f"expected KEY=VALUE entry, got {entry!r}")
        parsed[key] = value
    return parsed


def _local_parametric_verifier_env(
    entries: list[str],
    *,
    python_install_mirror: str | None,
) -> dict[str, str]:
    env = _parse_key_value_entries(entries)
    if python_install_mirror:
        env.setdefault("UV_PYTHON_INSTALL_MIRROR", python_install_mirror)
    return env


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
