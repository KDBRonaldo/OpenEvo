from __future__ import annotations

import json
from pathlib import Path

from polar_evolution.terminal_bench_task_local_parametric import (
    TaskLocalSelection,
    select_task_local_candidates,
)


def _write_pool(path: Path, rows: list[dict]) -> None:
    path.write_text(
        "".join(json.dumps(row, sort_keys=True) + "\n" for row in rows),
        encoding="utf-8",
    )


def test_select_task_local_candidates_requires_success_and_failure(
    tmp_path: Path,
) -> None:
    pool = tmp_path / "trajectory_pool.jsonl"
    _write_pool(
        pool,
        [
            {
                "trajectory_id": "train-fail-1",
                "task_id": "train-fasttext",
                "reward": 0.0,
                "trial_dir": str(tmp_path / "train-fail-1"),
            },
            {
                "trajectory_id": "train-pass-1",
                "task_id": "train-fasttext",
                "reward": 1.0,
                "trial_dir": str(tmp_path / "train-pass-1"),
            },
            {
                "trajectory_id": "only-pass",
                "task_id": "query-optimize",
                "reward": 1.0,
                "trial_dir": str(tmp_path / "only-pass"),
            },
            {
                "trajectory_id": "only-fail",
                "task_id": "dna-insert",
                "reward": 0.0,
                "trial_dir": str(tmp_path / "only-fail"),
            },
            {
                "trajectory_id": "null-run",
                "task_id": "train-fasttext",
                "reward": None,
                "trial_dir": str(tmp_path / "null-run"),
            },
        ],
    )

    [selection] = select_task_local_candidates(
        pool,
        task_ids=["train-fasttext", "query-optimize", "dna-insert"],
    )

    assert isinstance(selection, TaskLocalSelection)
    assert selection.task_id == "train-fasttext"
    assert [row.trajectory_id for row in selection.failed] == ["train-fail-1"]
    assert [row.trajectory_id for row in selection.successful] == ["train-pass-1"]
    assert [row.trajectory_id for row in selection.null_reward] == ["null-run"]
