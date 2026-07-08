from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
import re
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
                    "timeout_seconds": {"type": "integer", "minimum": 1},
                },
                "required": ["task_id", "command"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "tb_run_tests",
            "description": "Run the visible Terminal-Bench verifier for the task.",
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
            "name": "tb_collect_result",
            "description": "Collect the latest Terminal-Bench verifier result.",
            "parameters": {
                "type": "object",
                "properties": {"task_id": {"type": "string"}},
                "required": ["task_id"],
            },
        },
    },
]

_TASK_LOCAL_SYNTHETIC_SYSTEM = (
    "Use Terminal-Bench tools to solve the task. Emit one useful next tool call."
)
_TASK_LOCAL_DIRECT_SOLVER_SYSTEM = (
    "Solve exactly one task_id. Use tb_read_task first. Use tb_exec to inspect "
    "and edit files in the Harbor task container. Run tb_run_tests after changes "
    "without supplying a custom test command; it runs the visible test or "
    "evaluation entrypoint available in the container. Keep commands small and "
    "auditable. When verifier feedback names a failed assertion, predicate, "
    "expected property, or hard constraint, synthesize a temporary local checker "
    "from visible task files and make candidate outputs pass that checker before "
    "finalizing. For git recovery tasks, use git reflog/log plus git show --stat "
    "or git diff-tree --name-only to identify the recovered commit and verify all "
    "touched files before reporting completion."
)
_TASK_LOCAL_PROMPT_STYLES = {
    "direct_solver",
    "live_replay",
    "synthetic_correction",
}
_TASK_LOCAL_TARGET_MODES = {
    "final",
    "sequence",
}

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
    prompt_style: str = "direct_solver",
    target_mode: str = "final",
    target_exec_timeout_seconds: int | None = None,
    include_run_tests_correction: bool = False,
    include_collect_result_correction: bool = False,
    include_tb_exec_failure_correction: bool = False,
) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    if prompt_style not in _TASK_LOCAL_PROMPT_STYLES:
        raise ValueError(
            "task-local parametric prompt_style must be direct_solver, "
            "live_replay, or synthetic_correction"
        )
    if target_mode not in _TASK_LOCAL_TARGET_MODES:
        raise ValueError("task-local parametric target_mode must be final or sequence")
    if target_exec_timeout_seconds is not None and target_exec_timeout_seconds <= 0:
        raise ValueError("target_exec_timeout_seconds must be positive")
    if include_run_tests_correction and target_mode != "final":
        raise ValueError(
            "include_run_tests_correction is only supported with target_mode=final"
        )
    if include_collect_result_correction and target_mode != "final":
        raise ValueError(
            "include_collect_result_correction is only supported with target_mode=final"
        )
    if include_tb_exec_failure_correction and target_mode != "final":
        raise ValueError(
            "include_tb_exec_failure_correction is only supported with "
            "target_mode=final"
        )
    for failed in selection.failed:
        if len(records) >= max_records:
            break
        for successful in selection.successful:
            transcript_path = _trial_codex_transcript(successful.trial_dir)
            if target_mode == "sequence":
                commands = _sequence_target_commands(
                    transcript_path,
                    command_contains=command_contains,
                    exclude_command_contains=exclude_command_contains,
                )
            else:
                commands = extract_successful_codex_commands(
                    transcript_path,
                    command_contains=command_contains,
                    exclude_command_contains=exclude_command_contains,
                )
            if not commands:
                continue

            if target_mode == "sequence":
                remaining_records = max_records - len(records)
                start_index = max(0, len(commands) - remaining_records)
                for target_index in range(start_index, len(commands)):
                    command = commands[target_index]
                    if len(records) >= max_records:
                        break
                    records.append(
                        _task_local_sft_record(
                            selection=selection,
                            failed=failed,
                            successful=successful,
                            command=command,
                            prompt_style=prompt_style,
                            target_mode=target_mode,
                            target_exec_timeout_seconds=target_exec_timeout_seconds,
                            previous_commands=commands[:target_index],
                            target_sequence_index=target_index,
                            target_sequence_length=len(commands),
                        )
                    )
            else:
                command = _select_task_local_target_command(commands)
                records.append(
                    _task_local_sft_record(
                        selection=selection,
                        failed=failed,
                        successful=successful,
                        command=command,
                        prompt_style=prompt_style,
                        target_mode=target_mode,
                        target_exec_timeout_seconds=target_exec_timeout_seconds,
                    )
                )
                if include_tb_exec_failure_correction and len(records) < max_records:
                    correction_record = _task_local_tb_exec_failure_correction_record(
                        selection=selection,
                        failed=failed,
                        successful=successful,
                        command=command,
                        prompt_style=prompt_style,
                        target_mode=target_mode,
                        target_exec_timeout_seconds=target_exec_timeout_seconds,
                    )
                    if correction_record is not None:
                        records.append(correction_record)
                if include_run_tests_correction and len(records) < max_records:
                    correction_record = _task_local_run_tests_correction_record(
                        selection=selection,
                        failed=failed,
                        successful=successful,
                        command=command,
                        prompt_style=prompt_style,
                        target_mode=target_mode,
                        target_exec_timeout_seconds=target_exec_timeout_seconds,
                    )
                    if correction_record is not None:
                        records.append(correction_record)
                if include_collect_result_correction and len(records) < max_records:
                    correction_record = _task_local_collect_result_correction_record(
                        selection=selection,
                        failed=failed,
                        successful=successful,
                        command=command,
                        prompt_style=prompt_style,
                        target_mode=target_mode,
                        target_exec_timeout_seconds=target_exec_timeout_seconds,
                    )
                    if correction_record is not None:
                        records.append(correction_record)
            if records:
                break
    return records


def _sequence_target_commands(
    transcript_path: Path,
    *,
    command_contains: list[str] | None,
    exclude_command_contains: list[str] | None,
) -> list[CodexCommandEvent]:
    commands = extract_successful_codex_commands(
        transcript_path,
        exclude_command_contains=exclude_command_contains,
    )
    required = [needle for needle in command_contains or [] if needle]
    target_candidates = [
        command
        for command in commands
        if not required or all(needle in command.command for needle in required)
    ]
    if not target_candidates:
        return []
    target = _select_task_local_target_command(target_candidates)
    target_position = commands.index(target)
    return commands[: target_position + 1]


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


def _app_paths_in_command(command: str) -> list[str]:
    paths: list[str] = []
    seen: set[str] = set()
    for match in re.finditer(r"/app/[A-Za-z0-9._/\-]+", command):
        path = match.group(0).rstrip(".,:")
        if path and path not in seen:
            seen.add(path)
            paths.append(path)
    return paths


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
    target_filters: dict[str, Any] | None = None,
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
    if target_filters is not None:
        manifest["target_filters"] = target_filters
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
    prompt_style: str,
    target_mode: str,
    target_exec_timeout_seconds: int | None = None,
    previous_commands: list[CodexCommandEvent] | None = None,
    target_sequence_index: int | None = None,
    target_sequence_length: int | None = None,
    prompt_messages_override: list[dict[str, Any]] | None = None,
    prefix_source_override: str | None = None,
    target_correction_stage: str | None = None,
    metadata_overrides: dict[str, Any] | None = None,
    event_id_suffix: str = "",
) -> dict[str, Any]:
    if prompt_messages_override is not None:
        prompt_messages = prompt_messages_override
        response_messages = [
            _task_local_target_exec_message(
                command.command,
                timeout_seconds=target_exec_timeout_seconds,
            )
        ]
        prefix_source = prefix_source_override or "override"
    elif target_mode == "sequence":
        prompt_messages, response_messages, prefix_source = (
            _task_local_sequence_messages(
                selection=selection,
                failed=failed,
                successful=successful,
                command=command,
                prompt_style=prompt_style,
                target_exec_timeout_seconds=target_exec_timeout_seconds,
                previous_commands=previous_commands or [],
            )
        )
    elif prompt_style == "synthetic_correction":
        prompt_messages, response_messages, prefix_source = (
            _task_local_synthetic_correction_messages(
                selection=selection,
                failed=failed,
                successful=successful,
                command=command,
                target_exec_timeout_seconds=target_exec_timeout_seconds,
            )
        )
    elif prompt_style == "live_replay":
        prompt_messages, response_messages, prefix_source = (
            _task_local_live_replay_messages(
                selection=selection,
                failed=failed,
                successful=successful,
                command=command,
                target_exec_timeout_seconds=target_exec_timeout_seconds,
            )
        )
    else:
        prompt_messages, response_messages, prefix_source = (
            _task_local_direct_solver_messages(
                selection=selection,
                failed=failed,
                successful=successful,
                command=command,
                target_exec_timeout_seconds=target_exec_timeout_seconds,
            )
        )
    metadata = {
        "builder": "terminal_bench_task_local_parametric",
        "source_failed_trajectory_id": failed.trajectory_id,
        "source_failed_trial_dir": str(failed.trial_dir),
        "source_successful_trajectory_id": successful.trajectory_id,
        "source_successful_trial_dir": str(successful.trial_dir),
        "source_successful_command_event_index": command.event_index,
        "source_successful_command_output_excerpt": command.output_excerpt,
        "prefix_source": prefix_source,
        "target_app_paths": _app_paths_in_command(command.command),
        "target_command": command.command,
        "target_tool_name": "tb_exec",
    }
    if target_exec_timeout_seconds is not None:
        metadata["target_exec_timeout_seconds"] = target_exec_timeout_seconds
    if target_sequence_index is not None and target_sequence_length is not None:
        metadata["target_sequence_index"] = target_sequence_index
        metadata["target_sequence_length"] = target_sequence_length
    if target_correction_stage is not None:
        metadata["target_correction_stage"] = target_correction_stage
    if metadata_overrides is not None:
        metadata.update(metadata_overrides)
    return {
        "event_id": (
            f"task-local-parametric:{selection.task_id}:"
            f"{failed.trajectory_id}:{successful.trajectory_id}:{command.event_index}"
            f"{event_id_suffix}"
        ),
        "task_id": selection.task_id,
        "session_id": f"task-local-parametric:{selection.task_id}",
        "status": "COMPLETED",
        "reward": 1.0,
        "traces": [
            {
                "prompt_messages": prompt_messages,
                "response_messages": response_messages,
                "tools": _TERMINAL_BENCH_TOOLS,
            }
        ],
        "metadata": metadata,
    }


def _task_local_tb_exec_failure_correction_record(
    *,
    selection: TaskLocalSelection,
    failed: TrajectoryPoolRow,
    successful: TrajectoryPoolRow,
    command: CodexCommandEvent,
    prompt_style: str,
    target_mode: str,
    target_exec_timeout_seconds: int | None,
) -> dict[str, Any] | None:
    if prompt_style != "live_replay" or target_mode != "final":
        return None
    correction_prefix = _task_local_tb_exec_failure_correction_prefix(
        failed.trial_dir
    )
    if correction_prefix is None:
        return None
    prompt_messages, call_index, failed_tool_metadata = correction_prefix
    return _task_local_sft_record(
        selection=selection,
        failed=failed,
        successful=successful,
        command=command,
        prompt_style=prompt_style,
        target_mode=target_mode,
        target_exec_timeout_seconds=target_exec_timeout_seconds,
        prompt_messages_override=prompt_messages,
        prefix_source_override=(
            f"live_replay_tb_exec_failure_correction_llm_call:{call_index}"
        ),
        target_correction_stage="tb_exec_failure",
        metadata_overrides=failed_tool_metadata,
        event_id_suffix=f":tb-exec-failure-correction:{call_index}",
    )


def _task_local_run_tests_correction_record(
    *,
    selection: TaskLocalSelection,
    failed: TrajectoryPoolRow,
    successful: TrajectoryPoolRow,
    command: CodexCommandEvent,
    prompt_style: str,
    target_mode: str,
    target_exec_timeout_seconds: int | None,
) -> dict[str, Any] | None:
    if prompt_style != "live_replay" or target_mode != "final":
        return None
    correction_prefix = _task_local_run_tests_correction_prefix(failed.trial_dir)
    if correction_prefix is None:
        return None
    prompt_messages, call_index = correction_prefix
    return _task_local_sft_record(
        selection=selection,
        failed=failed,
        successful=successful,
        command=command,
        prompt_style=prompt_style,
        target_mode=target_mode,
        target_exec_timeout_seconds=target_exec_timeout_seconds,
        prompt_messages_override=prompt_messages,
        prefix_source_override=(
            f"live_replay_run_tests_correction_llm_call:{call_index}"
        ),
        target_correction_stage="run_tests_failure",
        event_id_suffix=f":run-tests-correction:{call_index}",
    )


def _task_local_collect_result_correction_record(
    *,
    selection: TaskLocalSelection,
    failed: TrajectoryPoolRow,
    successful: TrajectoryPoolRow,
    command: CodexCommandEvent,
    prompt_style: str,
    target_mode: str,
    target_exec_timeout_seconds: int | None,
) -> dict[str, Any] | None:
    if prompt_style != "live_replay" or target_mode != "final":
        return None
    correction_prefix = _task_local_collect_result_correction_prefix(failed.trial_dir)
    if correction_prefix is None:
        return None
    prompt_messages, call_index = correction_prefix
    return _task_local_sft_record(
        selection=selection,
        failed=failed,
        successful=successful,
        command=command,
        prompt_style=prompt_style,
        target_mode=target_mode,
        target_exec_timeout_seconds=target_exec_timeout_seconds,
        prompt_messages_override=prompt_messages,
        prefix_source_override=(
            f"live_replay_collect_result_correction_llm_call:{call_index}"
        ),
        target_correction_stage="collect_result_failure",
        event_id_suffix=f":collect-result-correction:{call_index}",
    )


def _task_local_sequence_messages(
    *,
    selection: TaskLocalSelection,
    failed: TrajectoryPoolRow,
    successful: TrajectoryPoolRow,
    command: CodexCommandEvent,
    prompt_style: str,
    target_exec_timeout_seconds: int | None,
    previous_commands: list[CodexCommandEvent],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], str]:
    if prompt_style == "live_replay":
        live_prefix = _task_local_live_replay_prefix(failed.trial_dir)
        if live_prefix is not None:
            prompt_messages, call_index = live_prefix
            prefix_source = f"live_replay_llm_call:{call_index}"
        else:
            prompt_messages = _task_local_direct_solver_prefix_messages(
                failed=failed,
                successful=successful,
            )
            prefix_source = "direct_solver_read_task"
    elif prompt_style == "synthetic_correction":
        prompt = (
            "Task-local parametric memory correction.\n\n"
            f"Task id: {selection.task_id}\n"
            f"Failed trajectory summary: {_task_summary(failed)}\n"
            f"Successful trajectory summary: {_task_summary(successful)}\n\n"
            "Produce the next solve action as a tool call."
        )
        prompt_messages = [
            {"role": "system", "content": _TASK_LOCAL_SYNTHETIC_SYSTEM},
            {"role": "user", "content": prompt},
        ]
        prefix_source = "task_summary_fallback"
    else:
        prompt_messages = _task_local_direct_solver_prefix_messages(
            failed=failed,
            successful=successful,
        )
        prefix_source = "direct_solver_read_task"

    prompt_messages = [
        *prompt_messages,
        *_previous_command_messages(
            previous_commands,
            timeout_seconds=target_exec_timeout_seconds,
        ),
    ]
    return (
        prompt_messages,
        [
            _task_local_target_exec_message(
                command.command,
                timeout_seconds=target_exec_timeout_seconds,
            )
        ],
        prefix_source,
    )


def _task_local_synthetic_correction_messages(
    *,
    selection: TaskLocalSelection,
    failed: TrajectoryPoolRow,
    successful: TrajectoryPoolRow,
    command: CodexCommandEvent,
    target_exec_timeout_seconds: int | None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], str]:
    prompt = (
        "Task-local parametric memory correction.\n\n"
        f"Task id: {selection.task_id}\n"
        f"Failed trajectory summary: {_task_summary(failed)}\n"
        f"Successful trajectory summary: {_task_summary(successful)}\n\n"
        "Produce the next solve action as a tool call."
    )
    return (
        [
            {"role": "system", "content": _TASK_LOCAL_SYNTHETIC_SYSTEM},
            {"role": "user", "content": prompt},
        ],
        [
            _task_local_target_exec_message(
                command.command,
                timeout_seconds=target_exec_timeout_seconds,
            )
        ],
        "task_summary_fallback",
    )


def _task_local_live_replay_messages(
    *,
    selection: TaskLocalSelection,
    failed: TrajectoryPoolRow,
    successful: TrajectoryPoolRow,
    command: CodexCommandEvent,
    target_exec_timeout_seconds: int | None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], str]:
    live_prefix = _task_local_live_replay_prefix(failed.trial_dir)
    if live_prefix is None:
        return _task_local_direct_solver_messages(
            selection=selection,
            failed=failed,
            successful=successful,
            command=command,
            target_exec_timeout_seconds=target_exec_timeout_seconds,
        )
    prompt_messages, call_index = live_prefix
    return (
        prompt_messages,
        [
            _task_local_target_exec_message(
                command.command,
                timeout_seconds=target_exec_timeout_seconds,
            )
        ],
        f"live_replay_llm_call:{call_index}",
    )


def _task_local_direct_solver_messages(
    *,
    selection: TaskLocalSelection,
    failed: TrajectoryPoolRow,
    successful: TrajectoryPoolRow,
    command: CodexCommandEvent,
    target_exec_timeout_seconds: int | None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], str]:
    del selection
    prompt_messages = _task_local_direct_solver_prefix_messages(
        failed=failed,
        successful=successful,
    )
    read_task_messages = prompt_messages[2:]
    return (
        prompt_messages[:2],
        [
            *read_task_messages,
            _task_local_target_exec_message(
                command.command,
                timeout_seconds=target_exec_timeout_seconds,
            ),
        ],
        "direct_solver_read_task",
    )


def _task_local_direct_solver_prefix_messages(
    *,
    failed: TrajectoryPoolRow,
    successful: TrajectoryPoolRow,
) -> list[dict[str, Any]]:
    read_task_call_id = "polar-task-local-read-task"
    task_text = _task_instruction_text(failed) or _task_instruction_text(successful)
    if not task_text:
        task_text = _task_summary(failed)
    return [
        {"role": "system", "content": _TASK_LOCAL_DIRECT_SOLVER_SYSTEM},
        {"role": "user", "content": _task_local_direct_solver_instruction()},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                _tool_call(
                    "tb_read_task",
                    {"task_id": "terminal-bench-task"},
                    call_id=read_task_call_id,
                )
            ],
        },
        {
            "role": "tool",
            "tool_call_id": read_task_call_id,
            "content": _task_local_read_task_response(task_text),
        },
    ]


def _previous_command_messages(
    commands: list[CodexCommandEvent],
    *,
    timeout_seconds: int | None = None,
) -> list[dict[str, Any]]:
    messages: list[dict[str, Any]] = []
    for index, command in enumerate(commands):
        call_id = f"polar-task-local-sequence-{index}"
        messages.extend(
            [
                _task_local_target_exec_message(
                    command.command,
                    call_id=call_id,
                    timeout_seconds=timeout_seconds,
                ),
                {
                    "role": "tool",
                    "tool_call_id": call_id,
                    "content": _task_local_command_result_message(command),
                },
            ]
        )
    return messages


def _task_local_command_result_message(command: CodexCommandEvent) -> str:
    payload = {
        "command": command.command,
        "exit_code": command.exit_code,
        "output_excerpt": command.output_excerpt,
        "status": command.status,
        "tool": "tb_exec",
    }
    return json.dumps(payload, indent=2, sort_keys=True)


def _task_local_live_replay_prefix(
    trial_dir: Path,
) -> tuple[list[dict[str, Any]], int] | None:
    calls_path = _trial_evolab_llm_calls(trial_dir)
    if not calls_path.is_file():
        return None
    for call_index, line in enumerate(
        calls_path.read_text(encoding="utf-8", errors="replace").splitlines(),
        start=1,
    ):
        if not line.strip():
            continue
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(payload, dict):
            continue
        input_messages = _compact_live_replay_messages(payload.get("input_messages"))
        if not input_messages:
            continue
        if not any(message.get("role") == "tool" for message in input_messages):
            continue
        if not _llm_call_outputs_tool(payload, "tb_exec"):
            continue
        return input_messages, call_index
    return None


def _task_local_run_tests_correction_prefix(
    trial_dir: Path,
) -> tuple[list[dict[str, Any]], int] | None:
    calls_path = _trial_evolab_llm_calls(trial_dir)
    if not calls_path.is_file():
        return None
    for call_index, line in enumerate(
        calls_path.read_text(encoding="utf-8", errors="replace").splitlines(),
        start=1,
    ):
        if not line.strip():
            continue
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(payload, dict):
            continue
        raw_input_messages = payload.get("input_messages")
        if not _input_messages_have_failed_run_tests_result(raw_input_messages):
            continue
        input_messages = _compact_live_replay_messages(raw_input_messages)
        if input_messages:
            return input_messages, call_index
    return None


def _task_local_collect_result_correction_prefix(
    trial_dir: Path,
) -> tuple[list[dict[str, Any]], int] | None:
    calls_path = _trial_evolab_llm_calls(trial_dir)
    if not calls_path.is_file():
        return None
    for call_index, line in enumerate(
        calls_path.read_text(encoding="utf-8", errors="replace").splitlines(),
        start=1,
    ):
        if not line.strip():
            continue
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(payload, dict):
            continue
        raw_input_messages = payload.get("input_messages")
        if not _input_messages_have_failed_tool_result(
            raw_input_messages,
            "tb_collect_result",
        ):
            continue
        input_messages = _compact_live_replay_messages(raw_input_messages)
        if input_messages:
            return input_messages, call_index
    return None


def _task_local_tb_exec_failure_correction_prefix(
    trial_dir: Path,
) -> tuple[list[dict[str, Any]], int, dict[str, Any]] | None:
    calls_path = _trial_evolab_llm_calls(trial_dir)
    if not calls_path.is_file():
        return None
    for call_index, line in enumerate(
        calls_path.read_text(encoding="utf-8", errors="replace").splitlines(),
        start=1,
    ):
        if not line.strip():
            continue
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(payload, dict):
            continue
        raw_input_messages = payload.get("input_messages")
        failed_metadata = _first_failed_tool_result_metadata(
            raw_input_messages,
            "tb_exec",
        )
        if failed_metadata is None:
            continue
        input_messages = _compact_live_replay_messages(raw_input_messages)
        if input_messages:
            return input_messages, call_index, failed_metadata
    return None


def _input_messages_have_failed_run_tests_result(value: Any) -> bool:
    return _input_messages_have_failed_tool_result(value, "tb_run_tests")


def _input_messages_have_failed_tool_result(value: Any, tool_name: str) -> bool:
    if not isinstance(value, list):
        return False
    for message in value:
        if not isinstance(message, dict):
            continue
        if message.get("role") != "tool":
            continue
        if _tool_result_name(message) != tool_name:
            continue
        if _tool_result_indicates_failure(message):
            return True
    return False


def _first_failed_tool_result_metadata(
    value: Any,
    tool_name: str,
) -> dict[str, Any] | None:
    if not isinstance(value, list):
        return None
    for message_index, message in enumerate(value, start=1):
        if not isinstance(message, dict):
            continue
        if message.get("role") != "tool":
            continue
        if _tool_result_name(message) != tool_name:
            continue
        if not _tool_result_indicates_failure(message):
            continue
        metadata: dict[str, Any] = {
            "failed_tool_name": tool_name,
            "failed_tool_index": message_index,
        }
        exit_code = _tool_result_exit_code(message)
        if exit_code is not None:
            metadata["failed_exit_code"] = exit_code
        failure_flags = _tool_result_failure_flags(message)
        if failure_flags:
            metadata["failed_tool_failure_flags"] = failure_flags
        return metadata
    return None


def _tool_result_name(message: dict[str, Any]) -> str | None:
    name = message.get("name")
    if isinstance(name, str) and name.strip():
        return name.strip()
    for payload in _tool_result_payloads(message):
        for key in ("tool", "kind", "name"):
            value = payload.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
    return None


def _tool_result_indicates_failure(message: dict[str, Any]) -> bool:
    for payload in _tool_result_payloads(message):
        if _payload_indicates_failure(payload):
            return True
    return False


def _tool_result_exit_code(message: dict[str, Any]) -> int | None:
    for payload in _tool_result_payloads(message):
        exit_code = _payload_exit_code(payload)
        if exit_code is not None:
            return exit_code
    return None


def _payload_exit_code(payload: dict[str, Any]) -> int | None:
    exit_code = payload.get("exit_code")
    if isinstance(exit_code, bool):
        return None
    if isinstance(exit_code, int):
        return exit_code
    if isinstance(exit_code, float) and exit_code.is_integer():
        return int(exit_code)
    result = payload.get("result")
    if isinstance(result, dict):
        return _payload_exit_code(result)
    return None


def _tool_result_failure_flags(message: dict[str, Any]) -> list[str]:
    text = _tool_result_failure_text(message).lower()
    flags: list[str] = []
    if any(token in text for token in ("syntax", "unterminated", "unexpected eof")):
        flags.append("syntax")
    if "traceback" in text:
        flags.append("traceback")
    if "fasttext" in text:
        flags.append("fasttext")
    if "parquet" in text:
        flags.append("parquet")
    if "model.bin" in text or "model_bin" in text:
        flags.append("model_bin")
    if "timeout" in text or "timed out" in text:
        flags.append("timeout")
    return flags


def _tool_result_failure_text(message: dict[str, Any]) -> str:
    parts: list[str] = []
    for payload in _tool_result_payloads(message):
        parts.append(json.dumps(payload, sort_keys=True, default=str))
    content = _message_content(message.get("content"))
    if content:
        parts.append(content)
    return "\n".join(parts)


def _payload_indicates_failure(payload: dict[str, Any]) -> bool:
    if _payload_has_missing_candidate_artifact(payload):
        return True
    status = payload.get("status")
    if isinstance(status, str):
        normalized = status.strip().lower()
        if normalized and normalized not in {
            "completed",
            "ok",
            "passed",
            "success",
            "succeeded",
        }:
            return True
    exit_code = payload.get("exit_code")
    if isinstance(exit_code, int | float) and exit_code != 0:
        return True
    result = payload.get("result")
    if isinstance(result, dict) and _payload_indicates_failure(result):
        return True
    return False


def _payload_has_missing_candidate_artifact(payload: dict[str, Any]) -> bool:
    task_progress = payload.get("task_progress")
    if not isinstance(task_progress, dict):
        return False
    candidate_artifacts = task_progress.get("candidate_artifacts")
    if not isinstance(candidate_artifacts, list):
        return False
    return any(
        isinstance(candidate, dict) and candidate.get("present") is False
        for candidate in candidate_artifacts
    )


def _tool_result_payloads(message: dict[str, Any]) -> list[dict[str, Any]]:
    payloads: list[dict[str, Any]] = []
    metadata = message.get("metadata")
    if isinstance(metadata, dict):
        tool_result = metadata.get("tool_result")
        if isinstance(tool_result, dict):
            payloads.append(tool_result)
            parsed = _json_object_from_text(tool_result.get("content"))
            if parsed is not None:
                payloads.append(parsed)
    parsed_content = _json_object_from_text(message.get("content"))
    if parsed_content is not None:
        payloads.append(parsed_content)
    return payloads


def _json_object_from_text(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError:
        return None
    if isinstance(parsed, dict):
        return parsed
    return None


def _compact_live_replay_messages(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    messages: list[dict[str, Any]] = []
    for message in value:
        if not isinstance(message, dict):
            continue
        role = message.get("role")
        if not isinstance(role, str) or not role.strip():
            continue
        role = role.strip()
        content = _live_replay_message_content(message, role=role)
        compact: dict[str, Any] = {"role": role, "content": content}
        tool_calls = message.get("tool_calls")
        if isinstance(tool_calls, list) and tool_calls:
            compact["tool_calls"] = tool_calls
        tool_call_id = message.get("tool_call_id")
        if isinstance(tool_call_id, str) and tool_call_id.strip():
            compact["tool_call_id"] = tool_call_id.strip()
        name = message.get("name")
        if isinstance(name, str) and name.strip():
            compact["name"] = name.strip()
        if content or compact.get("tool_calls"):
            messages.append(compact)
    return messages


def _live_replay_message_content(message: dict[str, Any], *, role: str) -> str:
    if role == "tool":
        metadata = message.get("metadata")
        if isinstance(metadata, dict):
            tool_result = metadata.get("tool_result")
            if isinstance(tool_result, dict):
                content = tool_result.get("content")
                if isinstance(content, str) and content.strip():
                    return content
    return _message_content(message.get("content"))


def _message_content(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        parts: list[str] = []
        for item in value:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict) and isinstance(item.get("text"), str):
                parts.append(item["text"])
        return "\n".join(parts)
    return ""


def _llm_call_outputs_tool(payload: dict[str, Any], tool_name: str) -> bool:
    output_messages = payload.get("output_messages")
    if not isinstance(output_messages, list):
        return False
    for message in output_messages:
        if not isinstance(message, dict):
            continue
        if tool_name in _message_tool_call_names(message):
            return True
    return False


def _message_tool_call_names(message: dict[str, Any]) -> set[str]:
    names: set[str] = set()
    _collect_tool_call_names(message.get("tool_calls"), names)
    metadata = message.get("metadata")
    if isinstance(metadata, dict):
        _collect_tool_call_names(metadata.get("tool_call"), names)
        _collect_tool_call_names(metadata.get("tool_calls"), names)
    return names


def _collect_tool_call_names(value: Any, names: set[str]) -> None:
    if isinstance(value, list):
        for item in value:
            _collect_tool_call_names(item, names)
        return
    if not isinstance(value, dict):
        return
    name = value.get("name")
    if isinstance(name, str) and name:
        names.add(name)
    function = value.get("function")
    if isinstance(function, dict):
        function_name = function.get("name")
        if isinstance(function_name, str) and function_name:
            names.add(function_name)


def _task_local_direct_solver_instruction() -> str:
    instruction = {
        "subagent_goal": (
            "Solve the current Terminal-Bench task in the Harbor task container. "
            "Use tb_read_task to read the task instruction, then use tb_exec to "
            "modify files so the official verifier passes."
        ),
        "task_summary": (
            "There is exactly one work item for this Harbor trial. The task is "
            "complete when the expected files in /app satisfy the verifier."
        ),
        "available_input_artifacts": [],
        "expected_output_artifacts": [],
    }
    return "Instruction:\n" + json.dumps(instruction, indent=2, sort_keys=True)


def _task_local_read_task_response(task_text: str) -> str:
    task_yaml = "descriptions:\n  - key: base\n    description: |\n"
    for line in task_text.splitlines() or [""]:
        task_yaml += f"      {line}\n"
    payload = {
        "message": "read Harbor task terminal-bench-task",
        "task_id": "terminal-bench-task",
        "task_yaml": task_yaml,
        "tool": "tb_read_task",
        "visible_baselines": [],
        "workspace": "Harbor task container",
    }
    return json.dumps(payload, indent=2, sort_keys=True)


def _task_local_target_exec_message(
    command: str,
    *,
    call_id: str = "polar-task-local-target",
    timeout_seconds: int | None = None,
) -> dict[str, Any]:
    arguments: dict[str, Any] = {
        "task_id": "terminal-bench-task",
        "command": command,
    }
    if timeout_seconds is not None:
        arguments["timeout_seconds"] = timeout_seconds
    return {
        "role": "assistant",
        "content": "",
        "tool_calls": [
            _tool_call(
                "tb_exec",
                arguments,
                call_id=call_id,
            )
        ],
    }


def _trial_codex_transcript(trial_dir: Path) -> Path:
    return trial_dir / "agent" / "codex.txt"


def _trial_evolab_llm_calls(trial_dir: Path) -> Path:
    return (
        trial_dir
        / "agent"
        / "evolab_lab"
        / ".evolab"
        / "registries"
        / "trajectory"
        / "llm_calls.jsonl"
    )


def _trial_instruction(trial_dir: Path) -> str | None:
    path = trial_dir / "agent" / "instruction.txt"
    if not path.is_file():
        return None
    text = path.read_text(encoding="utf-8", errors="replace").strip()
    return text or None


def _task_instruction_text(row: TrajectoryPoolRow) -> str | None:
    for key in ("instruction", "prompt", "task_description", "prompt_summary"):
        value = row.raw.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return _trial_instruction(row.trial_dir)


def _task_summary(row: TrajectoryPoolRow) -> str:
    for key in ("prompt_summary", "response_summary", "verifier_summary"):
        value = row.raw.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return f"Terminal-Bench task {row.task_id}"


def _tool_call(
    name: str,
    arguments: dict[str, Any],
    *,
    call_id: str = "polar-task-local-target",
) -> dict[str, Any]:
    return {
        "id": call_id,
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
