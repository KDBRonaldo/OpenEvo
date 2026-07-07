from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
from typing import Any

from polar_evolution.models import ArtifactType, WorkerClaimInputArtifact, WorkerClaimedJob


@dataclass(frozen=True)
class TrajectoryPoolRow:
    trajectory_id: str
    task_id: str
    reward: float | None
    trial_dir: Path
    raw: dict[str, Any]


@dataclass(frozen=True)
class TaskLocalSelection:
    task_id: str
    failed: list[TrajectoryPoolRow]
    successful: list[TrajectoryPoolRow]
    null_reward: list[TrajectoryPoolRow]


@dataclass(frozen=True)
class CodexCommandEvent:
    event_index: int
    command: str
    exit_code: int | None
    status: str | None
    output_excerpt: str


_TERMINAL_BENCH_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "tb_read_task",
            "description": "Read the current Terminal-Bench task instruction.",
            "parameters": {
                "type": "object",
                "properties": {"task_id": {"type": "string"}},
                "required": ["task_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "tb_exec",
            "description": "Run a shell command in the Terminal-Bench task container.",
            "parameters": {
                "type": "object",
                "properties": {
                    "task_id": {"type": "string"},
                    "command": {"type": "string"},
                },
                "required": ["task_id", "command"],
            },
        },
    },
]

_TASK_LOCAL_SYSTEM = (
    "Use Terminal-Bench tools to solve the task. Emit one useful next tool call."
)

_TARGET_WRITE_COMMAND_NEEDLES = (
    "save_model(",
    "torch.save(",
    "pickle.dump(",
    "json.dump(",
    "write_text(",
    "shutil.copy",
    " cp ",
    "\ncp ",
    " mv ",
    "\nmv ",
    "> /app/",
    "cat >",
)

_TARGET_VALIDATION_COMMAND_NEEDLES = (
    "path.exists()",
    ".exists()",
    ".stat()",
    "load_model(",
    "ls -",
    "du -",
    "test -f",
)


def load_trajectory_pool(path: Path) -> list[TrajectoryPoolRow]:
    rows: list[TrajectoryPoolRow] = []
    for line_number, line in enumerate(
        path.read_text(encoding="utf-8").splitlines(),
        start=1,
    ):
        if not line.strip():
            continue
        payload = json.loads(line)
        if not isinstance(payload, dict):
            continue

        task_id = payload.get("task_id")
        if not isinstance(task_id, str) or not task_id.strip():
            continue

        trajectory_id = payload.get("trajectory_id")
        if not isinstance(trajectory_id, str) or not trajectory_id.strip():
            trajectory_id = f"{path.name}:{line_number}"

        trial_dir = payload.get("trial_dir")
        if not isinstance(trial_dir, str) or not trial_dir.strip():
            trial_dir = ""

        rows.append(
            TrajectoryPoolRow(
                trajectory_id=trajectory_id,
                task_id=task_id.strip(),
                reward=_parse_reward(payload.get("reward")),
                trial_dir=Path(trial_dir),
                raw=payload,
            )
        )
    return rows


def select_task_local_candidates(
    pool_path: Path,
    *,
    task_ids: list[str] | None = None,
) -> list[TaskLocalSelection]:
    requested = set(task_ids or [])
    grouped: dict[str, list[TrajectoryPoolRow]] = {}
    for row in load_trajectory_pool(pool_path):
        if requested and row.task_id not in requested:
            continue
        grouped.setdefault(row.task_id, []).append(row)

    selections: list[TaskLocalSelection] = []
    for task_id in sorted(grouped):
        rows = grouped[task_id]
        failed = [row for row in rows if row.reward is not None and row.reward < 1.0]
        successful = [
            row for row in rows if row.reward is not None and row.reward >= 1.0
        ]
        null_reward = [row for row in rows if row.reward is None]
        if failed and successful:
            selections.append(
                TaskLocalSelection(
                    task_id=task_id,
                    failed=failed,
                    successful=successful,
                    null_reward=null_reward,
                )
            )
    return selections


def iter_codex_events(path: Path) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    if not path.is_file():
        return events
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        stripped = line.strip()
        if not stripped.startswith("{"):
            continue
        try:
            payload = json.loads(stripped)
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict):
            events.append(payload)
    return events


def extract_successful_codex_commands(
    transcript_path: Path,
    *,
    command_contains: list[str] | None = None,
    exclude_command_contains: list[str] | None = None,
    max_output_chars: int = 1000,
) -> list[CodexCommandEvent]:
    required = [needle for needle in command_contains or [] if needle]
    excluded = [needle for needle in exclude_command_contains or [] if needle]
    commands: list[CodexCommandEvent] = []
    for event_index, event in enumerate(iter_codex_events(transcript_path)):
        if event.get("type") != "item.completed":
            continue
        item = event.get("item")
        if not isinstance(item, dict) or item.get("type") != "command_execution":
            continue
        command = item.get("command")
        if not isinstance(command, str) or not command.strip():
            continue
        if item.get("exit_code") != 0 or item.get("status") != "completed":
            continue
        if required and not all(needle in command for needle in required):
            continue
        if excluded and any(needle in command for needle in excluded):
            continue
        output = item.get("aggregated_output")
        if output is None:
            output = ""
        commands.append(
            CodexCommandEvent(
                event_index=event_index,
                command=command.strip(),
                exit_code=0,
                status="completed",
                output_excerpt=str(output)[: max(1, int(max_output_chars))],
            )
        )
    return commands


def build_task_local_sft_records(
    selection: TaskLocalSelection,
    *,
    command_contains: list[str] | None = None,
    exclude_command_contains: list[str] | None = None,
    max_records: int = 16,
) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for failed in selection.failed:
        if len(records) >= max_records:
            break
        for successful in selection.successful:
            commands = extract_successful_codex_commands(
                _trial_codex_transcript(successful.trial_dir),
                command_contains=command_contains,
                exclude_command_contains=exclude_command_contains,
            )
            if not commands:
                continue

            command = _select_task_local_target_command(commands)
            records.append(
                _task_local_sft_record(
                    selection=selection,
                    failed=failed,
                    successful=successful,
                    command=command,
                )
            )
            break
    return records


def _select_task_local_target_command(
    commands: list[CodexCommandEvent],
) -> CodexCommandEvent:
    return max(
        commands,
        key=lambda command: (
            _task_local_target_command_score(command.command),
            command.event_index,
        ),
    )


def _task_local_target_command_score(command: str) -> int:
    lowered = command.lower()
    score = 0
    if any(needle in lowered for needle in _TARGET_WRITE_COMMAND_NEEDLES):
        score += 2
    if any(needle in lowered for needle in _TARGET_VALIDATION_COMMAND_NEEDLES):
        score -= 1
    return score


def build_task_local_parametric_job_payload(
    *,
    records: list[dict[str, Any]],
    output_root: Path,
    dataset_name: str,
    base_model: str,
    adapter_id: str,
    trainer_command: str,
    trainer_args: list[str],
    task_ids: list[str],
    trainer_timeout_seconds: float = 3600.0,
) -> dict[str, Any]:
    if not records:
        raise ValueError("task-local parametric dataset requires at least one record")
    missing_placeholders = [
        placeholder
        for placeholder in ("{training_dataset}", "{adapter_dir}")
        if not any(placeholder in arg for arg in trainer_args)
    ]
    if missing_placeholders:
        raise ValueError("trainer_args require {training_dataset} and {adapter_dir}")

    dataset_dir = output_root / "dataset"
    dataset_dir.mkdir(parents=True, exist_ok=True)
    records_path = dataset_dir / "records.jsonl"
    records_path.write_text(
        "".join(
            json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n"
            for record in records
        ),
        encoding="utf-8",
    )

    manifest = {
        "name": dataset_name,
        "purpose": "task-local-parametric-memory",
        "records_path": "records.jsonl",
        "record_count": len(records),
        "task_ids": list(task_ids),
        "builder": "terminal_bench_task_local_parametric",
    }
    manifest_path = dataset_dir / "manifest.json"
    manifest_path.write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )

    dataset_artifact_id = f"dataset-{_safe_name(dataset_name)}"
    input_artifact = WorkerClaimInputArtifact(
        artifact_id=dataset_artifact_id,
        type=ArtifactType.DATASET,
        uri=manifest_path.resolve().as_uri(),
        name=dataset_name,
    )
    job = WorkerClaimedJob(
        job_id=f"job-{_safe_name(adapter_id)}",
        lease_id="local-task-local-parametric",
        job_type="parametric_memory_lora_sft",
        method="parametric_memory_lora_sft",
        input_artifacts=[input_artifact],
        config={
            "name": f"Task-local parametric memory {', '.join(task_ids)}",
            "base_model": base_model,
            "output_adapter_id": adapter_id,
            "adapter_format": "lora",
            "training_projection": {"type": "full_trace"},
            "trainer": {
                "command": trainer_command,
                "args": list(trainer_args),
                "timeout_seconds": float(trainer_timeout_seconds),
            },
            "compatibility": {
                "agent_harness": ["terminal-bench-harbor"],
                "task_tags": [
                    "terminal-bench",
                    *[f"terminal-bench:{task_id}" for task_id in task_ids],
                ],
                "base_model": [base_model],
            },
            "lineage": {
                "method": "terminal_bench_task_local_parametric",
                "source_task_ids": list(task_ids),
                "dataset_manifest_uri": manifest_path.resolve().as_uri(),
            },
            "scores": {
                "quality": 0.0,
                "train_record_count": float(len(records)),
            },
            "promoted": False,
        },
    )
    return {
        "dataset": {
            "manifest_path": str(manifest_path),
            "records_path": str(records_path),
            "artifact": input_artifact.model_dump(mode="json"),
            "record_count": len(records),
        },
        "job": job.model_dump(mode="json"),
    }


def _task_local_sft_record(
    *,
    selection: TaskLocalSelection,
    failed: TrajectoryPoolRow,
    successful: TrajectoryPoolRow,
    command: CodexCommandEvent,
) -> dict[str, Any]:
    prompt = (
        "Task-local parametric memory correction.\n\n"
        f"Task id: {selection.task_id}\n"
        f"Failed trajectory summary: {_task_summary(failed)}\n"
        f"Successful trajectory summary: {_task_summary(successful)}\n\n"
        "Produce the next solve action as a tool call."
    )
    return {
        "event_id": (
            f"task-local-parametric:{selection.task_id}:"
            f"{failed.trajectory_id}:{successful.trajectory_id}:{command.event_index}"
        ),
        "task_id": selection.task_id,
        "session_id": f"task-local-parametric:{selection.task_id}",
        "status": "COMPLETED",
        "reward": 1.0,
        "traces": [
            {
                "prompt_messages": [
                    {"role": "system", "content": _TASK_LOCAL_SYSTEM},
                    {"role": "user", "content": prompt},
                ],
                "response_messages": [
                    {
                        "role": "assistant",
                        "content": "",
                        "tool_calls": [
                            _tool_call(
                                "tb_exec",
                                {
                                    "task_id": "terminal-bench-task",
                                    "command": command.command,
                                },
                            )
                        ],
                    }
                ],
                "tools": _TERMINAL_BENCH_TOOLS,
            }
        ],
        "metadata": {
            "builder": "terminal_bench_task_local_parametric",
            "source_failed_trajectory_id": failed.trajectory_id,
            "source_failed_trial_dir": str(failed.trial_dir),
            "source_successful_trajectory_id": successful.trajectory_id,
            "source_successful_trial_dir": str(successful.trial_dir),
            "source_successful_command_event_index": command.event_index,
            "source_successful_command_output_excerpt": command.output_excerpt,
            "prefix_source": "task_summary_fallback",
            "target_tool_name": "tb_exec",
        },
    }


def _trial_codex_transcript(trial_dir: Path) -> Path:
    return trial_dir / "agent" / "codex.txt"


def _task_summary(row: TrajectoryPoolRow) -> str:
    for key in ("prompt_summary", "response_summary", "verifier_summary"):
        value = row.raw.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return f"Terminal-Bench task {row.task_id}"


def _tool_call(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": "polar-task-local-target",
        "type": "function",
        "function": {"name": name, "arguments": arguments},
    }


def _safe_name(value: str) -> str:
    safe = "".join(
        ch if ch.isalnum() or ch in {"-", "_"} else "-"
        for ch in value.strip()
    )
    return "-".join(part for part in safe.split("-") if part) or "task-local-parametric"


def _parse_reward(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    return float(value)
