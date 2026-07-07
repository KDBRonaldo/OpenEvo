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


def _parse_reward(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    return float(value)
