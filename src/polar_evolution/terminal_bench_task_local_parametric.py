from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
from typing import Any


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


def _parse_reward(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    return float(value)
