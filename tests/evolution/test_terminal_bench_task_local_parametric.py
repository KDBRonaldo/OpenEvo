from __future__ import annotations

import json
from pathlib import Path

from polar_evolution.cli import main
from polar_evolution.terminal_bench_task_local_parametric import (
    TaskLocalSelection,
    TrajectoryPoolRow,
    build_task_local_parametric_job_payload,
    build_task_local_sft_records,
    extract_successful_codex_commands,
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


def test_extract_successful_codex_commands_reads_completed_command_events(
    tmp_path: Path,
) -> None:
    transcript = tmp_path / "codex.txt"
    transcript.write_text(
        "\n".join(
            [
                "WARNING: non-json prefix",
                json.dumps({"type": "thread.started", "thread_id": "t"}),
                json.dumps(
                    {
                        "type": "item.completed",
                        "item": {
                            "id": "cmd-1",
                            "type": "command_execution",
                            "command": "/bin/bash -lc 'cat data/train.parquet'",
                            "aggregated_output": "too much output",
                            "exit_code": 0,
                            "status": "completed",
                        },
                    }
                ),
                json.dumps(
                    {
                        "type": "item.completed",
                        "item": {
                            "id": "cmd-2",
                            "type": "command_execution",
                            "command": (
                                "/bin/bash -lc 'python train.py && "
                                "cp model.bin /app/model.bin'"
                            ),
                            "aggregated_output": "accuracy 0.6257\nsize 143211714",
                            "exit_code": 0,
                            "status": "completed",
                        },
                    }
                ),
                json.dumps(
                    {
                        "type": "item.completed",
                        "item": {
                            "id": "msg-1",
                            "type": "agent_message",
                            "text": "Done.",
                        },
                    }
                ),
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    commands = extract_successful_codex_commands(
        transcript,
        command_contains=["/app/model.bin"],
    )

    assert [command.command for command in commands] == [
        "/bin/bash -lc 'python train.py && cp model.bin /app/model.bin'"
    ]
    assert commands[0].event_index == 2
    assert commands[0].exit_code == 0
    assert "accuracy" in commands[0].output_excerpt


def test_build_task_local_sft_records_uses_successful_command_as_tb_exec_target(
    tmp_path: Path,
) -> None:
    failed_trial = tmp_path / "failed-trial"
    successful_trial = tmp_path / "successful-trial"
    (failed_trial / "agent").mkdir(parents=True)
    (successful_trial / "agent").mkdir(parents=True)
    (successful_trial / "agent" / "codex.txt").write_text(
        json.dumps(
            {
                "type": "item.completed",
                "item": {
                    "type": "command_execution",
                    "command": (
                        "/bin/bash -lc 'python train.py && "
                        "cp model.bin /app/model.bin'"
                    ),
                    "aggregated_output": "accuracy 0.6257\nsize 143211714",
                    "exit_code": 0,
                    "status": "completed",
                },
            }
        )
        + "\n",
        encoding="utf-8",
    )

    selection = TaskLocalSelection(
        task_id="train-fasttext",
        failed=[
            TrajectoryPoolRow(
                trajectory_id="failed-1",
                task_id="train-fasttext",
                reward=0.0,
                trial_dir=failed_trial,
                raw={"prompt_summary": "Train fastText and write /app/model.bin"},
            )
        ],
        successful=[
            TrajectoryPoolRow(
                trajectory_id="success-1",
                task_id="train-fasttext",
                reward=1.0,
                trial_dir=successful_trial,
                raw={"response_summary": "Created /app/model.bin under 150MB"},
            )
        ],
        null_reward=[],
    )

    [record] = build_task_local_sft_records(
        selection,
        command_contains=["/app/model.bin"],
        max_records=1,
    )

    assert record["task_id"] == "train-fasttext"
    assert record["status"] == "COMPLETED"
    assert record["reward"] == 1.0
    trace = record["traces"][0]
    assert trace["tools"][1]["function"]["name"] == "tb_exec"
    assert [message["role"] for message in trace["prompt_messages"]] == [
        "system",
        "user",
    ]
    assert trace["response_messages"][0]["tool_calls"][0]["function"]["name"] == (
        "tb_exec"
    )
    assert trace["response_messages"][0]["tool_calls"][0]["function"][
        "arguments"
    ] == {
        "task_id": "terminal-bench-task",
        "command": "/bin/bash -lc 'python train.py && cp model.bin /app/model.bin'",
    }
    assert record["metadata"]["source_failed_trajectory_id"] == "failed-1"
    assert record["metadata"]["source_successful_trajectory_id"] == "success-1"
    assert record["metadata"]["prefix_source"] == "task_summary_fallback"


def test_build_task_local_parametric_job_payload_writes_dataset_and_lora_job(
    tmp_path: Path,
) -> None:
    record = {
        "event_id": "task-local-parametric:train-fasttext:failed:success:1",
        "task_id": "train-fasttext",
        "session_id": "task-local-parametric:train-fasttext",
        "status": "COMPLETED",
        "reward": 1.0,
        "traces": [
            {
                "prompt_messages": [{"role": "user", "content": "Train fastText."}],
                "response_messages": [
                    {
                        "role": "assistant",
                        "content": "",
                        "tool_calls": [
                            {
                                "id": "target",
                                "type": "function",
                                "function": {
                                    "name": "tb_exec",
                                    "arguments": {
                                        "task_id": "terminal-bench-task",
                                        "command": "cp model.bin /app/model.bin",
                                    },
                                },
                            }
                        ],
                    }
                ],
                "tools": [],
            }
        ],
        "metadata": {"builder": "terminal_bench_task_local_parametric"},
    }

    payload = build_task_local_parametric_job_payload(
        records=[record],
        output_root=tmp_path / "out",
        dataset_name="tb21-task-local-train-fasttext",
        base_model="Qwen/Qwen3.6-35B-A3B",
        adapter_id="tb-parametric-memory-train-fasttext",
        trainer_command="python",
        trainer_args=[
            "/opt/train_lora.py",
            "--train-file",
            "{training_dataset}",
            "--output-dir",
            "{adapter_dir}",
        ],
        task_ids=["train-fasttext"],
    )

    manifest_path = Path(payload["dataset"]["manifest_path"])
    records_path = manifest_path.with_name("records.jsonl")
    assert manifest_path.is_file()
    assert records_path.is_file()
    assert json.loads(records_path.read_text(encoding="utf-8"))["task_id"] == (
        "train-fasttext"
    )
    assert payload["dataset"]["artifact"]["type"] == "dataset"
    assert payload["job"]["method"] == "parametric_memory_lora_sft"
    assert payload["job"]["input_artifacts"][0]["uri"] == (
        manifest_path.resolve().as_uri()
    )
    assert payload["job"]["config"]["training_projection"] == {"type": "full_trace"}
    assert payload["job"]["config"]["compatibility"]["task_tags"] == [
        "terminal-bench",
        "terminal-bench:train-fasttext",
    ]


def test_terminal_bench_task_local_parametric_memory_job_cli_writes_payload(
    tmp_path: Path,
) -> None:
    failed_trial = tmp_path / "failed-trial"
    successful_trial = tmp_path / "successful-trial"
    (failed_trial / "agent").mkdir(parents=True)
    (successful_trial / "agent").mkdir(parents=True)
    (successful_trial / "agent" / "codex.txt").write_text(
        json.dumps(
            {
                "type": "item.completed",
                "item": {
                    "type": "command_execution",
                    "command": (
                        "/bin/bash -lc 'python train.py && "
                        "cp model.bin /app/model.bin'"
                    ),
                    "aggregated_output": "accuracy 0.6257",
                    "exit_code": 0,
                    "status": "completed",
                },
            }
        )
        + "\n",
        encoding="utf-8",
    )
    pool = tmp_path / "trajectory_pool.jsonl"
    _write_pool(
        pool,
        [
            {
                "trajectory_id": "failed-1",
                "task_id": "train-fasttext",
                "reward": 0.0,
                "trial_dir": str(failed_trial),
                "prompt_summary": "Train fastText and write /app/model.bin",
            },
            {
                "trajectory_id": "success-1",
                "task_id": "train-fasttext",
                "reward": 1.0,
                "trial_dir": str(successful_trial),
            },
        ],
    )

    output = tmp_path / "job.json"
    assert (
        main(
            [
                "terminal-bench-task-local-parametric-memory-job",
                "--trajectory-pool",
                str(pool),
                "--task-id",
                "train-fasttext",
                "--output-root",
                str(tmp_path / "out"),
                "--dataset-name",
                "tb21-task-local-train-fasttext",
                "--base-model",
                "Qwen/Qwen3.6-35B-A3B",
                "--adapter-id",
                "tb-parametric-memory-train-fasttext",
                "--trainer-command",
                "python",
                "--trainer-arg",
                "train_lora.py",
                "--trainer-arg",
                "--train-file",
                "--trainer-arg",
                "{training_dataset}",
                "--trainer-arg",
                "--output-dir",
                "--trainer-arg",
                "{adapter_dir}",
                "--command-contains",
                "/app/model.bin",
                "--output",
                str(output),
            ]
        )
        == 0
    )

    payload = json.loads(output.read_text(encoding="utf-8"))
    assert payload["selected_tasks"] == ["train-fasttext"]
    assert payload["dataset"]["record_count"] == 1
    assert Path(payload["dataset"]["records_path"]).is_file()
    assert payload["job"]["method"] == "parametric_memory_lora_sft"
    assert payload["job"]["config"]["output_adapter_id"] == (
        "tb-parametric-memory-train-fasttext"
    )
    assert payload["job"]["config"]["trainer"]["args"] == [
        "train_lora.py",
        "--train-file",
        "{training_dataset}",
        "--output-dir",
        "{adapter_dir}",
    ]
    assert "completed_artifacts" not in payload
