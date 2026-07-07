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
    target_tool_call = trace["response_messages"][-1]["tool_calls"][0]["function"]
    assert target_tool_call["name"] == (
        "tb_exec"
    )
    assert target_tool_call["arguments"] == {
        "task_id": "terminal-bench-task",
        "command": "/bin/bash -lc 'python train.py && cp model.bin /app/model.bin'",
    }
    assert record["metadata"]["source_failed_trajectory_id"] == "failed-1"
    assert record["metadata"]["source_successful_trajectory_id"] == "success-1"
    assert record["metadata"]["prefix_source"] == "direct_solver_read_task"


def test_build_task_local_sft_records_default_matches_direct_solver_prefix(
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
                    "command": "python train.py && cp model.bin /app/model.bin",
                    "aggregated_output": "accuracy 0.6257",
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
                raw={"prompt_summary": "Please train a fasttext model."},
            )
        ],
        successful=[
            TrajectoryPoolRow(
                trajectory_id="success-1",
                task_id="train-fasttext",
                reward=1.0,
                trial_dir=successful_trial,
                raw={},
            )
        ],
        null_reward=[],
    )

    [record] = build_task_local_sft_records(
        selection,
        command_contains=["/app/model.bin"],
        max_records=1,
    )

    trace = record["traces"][0]
    assert trace["prompt_messages"][0]["role"] == "system"
    assert trace["prompt_messages"][0]["content"].startswith(
        "Solve exactly one task_id. Use tb_read_task first."
    )
    assert trace["prompt_messages"][1]["content"].startswith("Instruction:\n{")
    assert [message["role"] for message in trace["response_messages"]] == [
        "assistant",
        "tool",
        "assistant",
    ]
    assert trace["response_messages"][0]["tool_calls"][0]["function"] == {
        "name": "tb_read_task",
        "arguments": {"task_id": "terminal-bench-task"},
    }
    assert "Please train a fasttext model." in trace["response_messages"][1]["content"]
    assert trace["response_messages"][2]["tool_calls"][0]["function"] == {
        "name": "tb_exec",
        "arguments": {
            "task_id": "terminal-bench-task",
            "command": "python train.py && cp model.bin /app/model.bin",
        },
    }
    assert record["metadata"]["prefix_source"] == "direct_solver_read_task"


def test_build_task_local_sft_records_live_replay_uses_failed_llm_prefix(
    tmp_path: Path,
) -> None:
    failed_trial = tmp_path / "failed-trial"
    successful_trial = tmp_path / "successful-trial"
    llm_calls = (
        failed_trial
        / "agent"
        / "evolab_lab"
        / ".evolab"
        / "registries"
        / "trajectory"
        / "llm_calls.jsonl"
    )
    llm_calls.parent.mkdir(parents=True)
    (successful_trial / "agent").mkdir(parents=True)
    llm_calls.write_text(
        "\n".join(
            [
                json.dumps(
                    {
                        "input_messages": [
                            {"role": "system", "content": "Solve exactly one task_id."},
                            {
                                "role": "user",
                                "content": "Instruction:\n{}\n\nMemory:\n\n\nSkills:",
                            },
                        ],
                        "output_messages": [
                            {
                                "role": "assistant",
                                "content": "",
                                "tool_calls": [
                                    {
                                        "id": "call-read",
                                        "type": "function",
                                        "function": {
                                            "name": "tb_read_task",
                                            "arguments": {"task_id": "static-node-0"},
                                        },
                                    }
                                ],
                            }
                        ],
                    }
                ),
                json.dumps(
                    {
                        "input_messages": [
                            {"role": "system", "content": "Solve exactly one task_id."},
                            {
                                "role": "user",
                                "content": (
                                    "Instruction:\n{}\n\nMemory:\n\n\nSkills:\n\n\n"
                                    "Skill Context:\n{\"required_tools\": [\"tb_read_task\"]}"
                                ),
                            },
                            {
                                "role": "tool",
                                "content": (
                                    "{\n"
                                    '  "container_inventory": {"stdout": "/app\\n"},\n'
                                    '  "task_yaml": "Please train a fasttext model",\n'
                                    '  "tool": "tb_read_task"\n'
                                    "}"
                                ),
                                "tool_call_id": "call-read",
                            },
                        ],
                        "output_messages": [
                            {
                                "role": "assistant",
                                "content": "",
                                "tool_calls": [
                                    {
                                        "id": "call-ls",
                                        "type": "function",
                                        "function": {
                                            "name": "tb_exec",
                                            "arguments": {
                                                "task_id": "terminal-bench-task",
                                                "command": "ls -la",
                                            },
                                        },
                                    }
                                ],
                            }
                        ],
                    }
                ),
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    (successful_trial / "agent" / "codex.txt").write_text(
        json.dumps(
            {
                "type": "item.completed",
                "item": {
                    "type": "command_execution",
                    "command": "python train.py && cp model.bin /app/model.bin",
                    "aggregated_output": "accuracy 0.63",
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
                trajectory_id="failed-live",
                task_id="train-fasttext",
                reward=0.0,
                trial_dir=failed_trial,
                raw={},
            )
        ],
        successful=[
            TrajectoryPoolRow(
                trajectory_id="success",
                task_id="train-fasttext",
                reward=1.0,
                trial_dir=successful_trial,
                raw={},
            )
        ],
        null_reward=[],
    )

    [record] = build_task_local_sft_records(
        selection,
        command_contains=["/app/model.bin"],
        max_records=1,
        prompt_style="live_replay",
    )

    trace = record["traces"][0]
    assert [message["role"] for message in trace["prompt_messages"]] == [
        "system",
        "user",
        "tool",
    ]
    assert "Skill Context" in trace["prompt_messages"][1]["content"]
    assert "container_inventory" in trace["prompt_messages"][2]["content"]
    assert trace["prompt_messages"][2]["tool_call_id"] == "call-read"
    assert trace["response_messages"] == [
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": "polar-task-local-target",
                    "type": "function",
                    "function": {
                        "name": "tb_exec",
                        "arguments": {
                            "task_id": "terminal-bench-task",
                            "command": (
                                "python train.py && cp model.bin /app/model.bin"
                            ),
                        },
                    },
                }
            ],
        }
    ]
    assert record["metadata"]["prefix_source"] == "live_replay_llm_call:2"


def test_build_task_local_sft_records_live_replay_preserves_full_tool_result(
    tmp_path: Path,
) -> None:
    failed_trial = tmp_path / "failed-trial"
    successful_trial = tmp_path / "successful-trial"
    llm_calls = (
        failed_trial
        / "agent"
        / "evolab_lab"
        / ".evolab"
        / "registries"
        / "trajectory"
        / "llm_calls.jsonl"
    )
    llm_calls.parent.mkdir(parents=True)
    (successful_trial / "agent").mkdir(parents=True)
    full_tool_result = json.dumps(
        {
            "message": "read Harbor task terminal-bench-task",
            "task_yaml": (
                "descriptions:\n"
                "  - key: base\n"
                "    description: |\n"
                "      Write the output to /app/out.txt\n"
            ),
            "tool": "tb_read_task",
        },
        indent=2,
        sort_keys=True,
    )
    llm_calls.write_text(
        json.dumps(
            {
                "input_messages": [
                    {"role": "system", "content": "Solve exactly one task_id."},
                    {"role": "user", "content": "Instruction:\n{}"},
                    {
                        "role": "tool",
                        "content": (
                            "{\n"
                            '  "message": "read Harbor task terminal-bench-task",\n'
                            '  "task_yaml": "descriptions: ...[truncated 91 chars]",\n'
                            '  "tool": "tb_read_task"\n'
                            "}"
                        ),
                        "metadata": {
                            "tool_result": {
                                "content": full_tool_result,
                                "status": "ok",
                            }
                        },
                        "tool_call_id": "call-read",
                    },
                ],
                "output_messages": [
                    {
                        "role": "assistant",
                        "content": "",
                        "tool_calls": [
                            {
                                "id": "call-write",
                                "type": "function",
                                "function": {
                                    "name": "tb_exec",
                                    "arguments": {
                                        "task_id": "terminal-bench-task",
                                        "command": "write wrong path",
                                    },
                                },
                            }
                        ],
                    }
                ],
            }
        )
        + "\n",
        encoding="utf-8",
    )
    (successful_trial / "agent" / "codex.txt").write_text(
        json.dumps(
            {
                "type": "item.completed",
                "item": {
                    "type": "command_execution",
                    "command": "printf solved > /app/out.txt",
                    "aggregated_output": "",
                    "exit_code": 0,
                    "status": "completed",
                },
            }
        )
        + "\n",
        encoding="utf-8",
    )
    selection = TaskLocalSelection(
        task_id="gcode-to-text",
        failed=[
            TrajectoryPoolRow(
                trajectory_id="failed-live",
                task_id="gcode-to-text",
                reward=0.0,
                trial_dir=failed_trial,
                raw={},
            )
        ],
        successful=[
            TrajectoryPoolRow(
                trajectory_id="success",
                task_id="gcode-to-text",
                reward=1.0,
                trial_dir=successful_trial,
                raw={},
            )
        ],
        null_reward=[],
    )

    [record] = build_task_local_sft_records(
        selection,
        command_contains=["/app/out.txt"],
        max_records=1,
        prompt_style="live_replay",
    )

    tool_message = record["traces"][0]["prompt_messages"][-1]
    assert tool_message["role"] == "tool"
    assert "/app/out.txt" in tool_message["content"]
    assert "[truncated" not in tool_message["content"]


def test_build_task_local_sft_records_sequence_builds_progressive_records(
    tmp_path: Path,
) -> None:
    failed_trial = tmp_path / "failed-trial"
    successful_trial = tmp_path / "successful-trial"
    llm_calls = (
        failed_trial
        / "agent"
        / "evolab_lab"
        / ".evolab"
        / "registries"
        / "trajectory"
        / "llm_calls.jsonl"
    )
    llm_calls.parent.mkdir(parents=True)
    (successful_trial / "agent").mkdir(parents=True)
    llm_calls.write_text(
        json.dumps(
            {
                "input_messages": [
                    {"role": "system", "content": "Solve exactly one task_id."},
                    {"role": "user", "content": "Instruction:\n{}"},
                    {
                        "role": "tool",
                        "content": '{"task_yaml": "train fastText"}',
                        "tool_call_id": "read",
                    },
                ],
                "output_messages": [
                    {
                        "role": "assistant",
                        "content": "",
                        "tool_calls": [
                            {
                                "id": "probe",
                                "type": "function",
                                "function": {
                                    "name": "tb_exec",
                                    "arguments": {
                                        "task_id": "terminal-bench-task",
                                        "command": "ls -la /app",
                                    },
                                },
                            }
                        ],
                    }
                ],
            }
        )
        + "\n",
        encoding="utf-8",
    )
    (successful_trial / "agent" / "codex.txt").write_text(
        "\n".join(
            [
                json.dumps(
                    {
                        "type": "item.completed",
                        "item": {
                            "type": "command_execution",
                            "command": "apt-get update && apt-get install -y g++",
                            "aggregated_output": "installed compiler",
                            "exit_code": 0,
                            "status": "completed",
                        },
                    }
                ),
                json.dumps(
                    {
                        "type": "item.completed",
                        "item": {
                            "type": "command_execution",
                            "command": "python prepare_fasttext_data.py",
                            "aggregated_output": "wrote train_full.ft.txt",
                            "exit_code": 0,
                            "status": "completed",
                        },
                    }
                ),
                json.dumps(
                    {
                        "type": "item.completed",
                        "item": {
                            "type": "command_execution",
                            "command": (
                                "python train_fasttext.py && "
                                "cp model.bin /app/model.bin"
                            ),
                            "aggregated_output": "saved /app/model.bin",
                            "exit_code": 0,
                            "status": "completed",
                        },
                    }
                ),
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    selection = TaskLocalSelection(
        task_id="train-fasttext",
        failed=[
            TrajectoryPoolRow(
                trajectory_id="failed-live",
                task_id="train-fasttext",
                reward=0.0,
                trial_dir=failed_trial,
                raw={},
            )
        ],
        successful=[
            TrajectoryPoolRow(
                trajectory_id="success",
                task_id="train-fasttext",
                reward=1.0,
                trial_dir=successful_trial,
                raw={},
            )
        ],
        null_reward=[],
    )

    records = build_task_local_sft_records(
        selection,
        command_contains=["/app/model.bin"],
        max_records=8,
        prompt_style="live_replay",
        target_mode="sequence",
    )

    assert len(records) == 3
    target_commands = [
        record["traces"][0]["response_messages"][0]["tool_calls"][0]["function"][
            "arguments"
        ]["command"]
        for record in records
    ]
    assert target_commands == [
        "apt-get update && apt-get install -y g++",
        "python prepare_fasttext_data.py",
        "python train_fasttext.py && cp model.bin /app/model.bin",
    ]
    assert records[0]["metadata"]["target_sequence_index"] == 0
    assert records[2]["metadata"]["target_sequence_index"] == 2
    assert all(record["metadata"]["target_sequence_length"] == 3 for record in records)

    second_prompt = records[1]["traces"][0]["prompt_messages"]
    assert [message["role"] for message in second_prompt][-2:] == [
        "assistant",
        "tool",
    ]
    assert second_prompt[-2]["tool_calls"][0]["function"]["arguments"]["command"] == (
        "apt-get update && apt-get install -y g++"
    )
    assert "installed compiler" in second_prompt[-1]["content"]

    third_prompt = records[2]["traces"][0]["prompt_messages"]
    prompt_commands = [
        message["tool_calls"][0]["function"]["arguments"]["command"]
        for message in third_prompt
        if message.get("role") == "assistant" and message.get("tool_calls")
    ]
    assert prompt_commands == [
        "apt-get update && apt-get install -y g++",
        "python prepare_fasttext_data.py",
    ]
    assert "wrote train_full.ft.txt" in third_prompt[-1]["content"]


def test_build_task_local_sft_records_sequence_cap_keeps_final_target(
    tmp_path: Path,
) -> None:
    failed_trial = tmp_path / "failed-trial"
    successful_trial = tmp_path / "successful-trial"
    (failed_trial / "agent").mkdir(parents=True)
    (successful_trial / "agent").mkdir(parents=True)
    commands = [
        "inspect data",
        "install compiler",
        "prepare fasttext rows",
        "train validation model",
        "train final model && cp model.bin /app/model.bin",
    ]
    (successful_trial / "agent" / "codex.txt").write_text(
        "\n".join(
            json.dumps(
                {
                    "type": "item.completed",
                    "item": {
                        "type": "command_execution",
                        "command": command,
                        "aggregated_output": f"finished {index}",
                        "exit_code": 0,
                        "status": "completed",
                    },
                }
            )
            for index, command in enumerate(commands)
        )
        + "\n",
        encoding="utf-8",
    )
    selection = TaskLocalSelection(
        task_id="train-fasttext",
        failed=[
            TrajectoryPoolRow(
                trajectory_id="failed",
                task_id="train-fasttext",
                reward=0.0,
                trial_dir=failed_trial,
                raw={"prompt_summary": "Train fastText."},
            )
        ],
        successful=[
            TrajectoryPoolRow(
                trajectory_id="success",
                task_id="train-fasttext",
                reward=1.0,
                trial_dir=successful_trial,
                raw={},
            )
        ],
        null_reward=[],
    )

    records = build_task_local_sft_records(
        selection,
        command_contains=["/app/model.bin"],
        max_records=2,
        target_mode="sequence",
    )

    target_commands = [
        record["traces"][0]["response_messages"][0]["tool_calls"][0]["function"][
            "arguments"
        ]["command"]
        for record in records
    ]
    assert target_commands == commands[-2:]
    assert [record["metadata"]["target_sequence_index"] for record in records] == [3, 4]
    assert all(record["metadata"]["target_sequence_length"] == 5 for record in records)

    final_prompt_commands = [
        message["tool_calls"][0]["function"]["arguments"]["command"]
        for message in records[-1]["traces"][0]["prompt_messages"]
        if message.get("role") == "assistant"
        and message.get("tool_calls")
        and message["tool_calls"][0]["function"]["name"] == "tb_exec"
    ]
    assert final_prompt_commands[-4:] == commands[:-1]
    assert "finished 3" in records[-1]["traces"][0]["prompt_messages"][-1]["content"]


def test_build_task_local_sft_records_prefers_write_command_over_later_validation(
    tmp_path: Path,
) -> None:
    failed_trial = tmp_path / "failed-trial"
    successful_trial = tmp_path / "successful-trial"
    (failed_trial / "agent").mkdir(parents=True)
    (successful_trial / "agent").mkdir(parents=True)
    (successful_trial / "agent" / "codex.txt").write_text(
        "\n".join(
            [
                json.dumps(
                    {
                        "type": "item.completed",
                        "item": {
                            "type": "command_execution",
                            "command": (
                                "/bin/bash -lc \"python - <<'PY'\n"
                                "import fasttext\n"
                                "model = fasttext.train_supervised(input='train.txt')\n"
                                "model.save_model('/app/model.bin')\n"
                                "PY\""
                            ),
                            "aggregated_output": "trained",
                            "exit_code": 0,
                            "status": "completed",
                        },
                    }
                ),
                json.dumps(
                    {
                        "type": "item.completed",
                        "item": {
                            "type": "command_execution",
                            "command": (
                                "/bin/bash -lc \"python - <<'PY'\n"
                                "from pathlib import Path\n"
                                "path = Path('/app/model.bin')\n"
                                "print(path.exists())\n"
                                "print(path.stat().st_size)\n"
                                "PY\""
                            ),
                            "aggregated_output": "True\n143211714",
                            "exit_code": 0,
                            "status": "completed",
                        },
                    }
                ),
            ]
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
                raw={},
            )
        ],
        successful=[
            TrajectoryPoolRow(
                trajectory_id="success-1",
                task_id="train-fasttext",
                reward=1.0,
                trial_dir=successful_trial,
                raw={},
            )
        ],
        null_reward=[],
    )

    [record] = build_task_local_sft_records(
        selection,
        command_contains=["/app/model.bin"],
        max_records=1,
    )

    target = record["traces"][0]["response_messages"][-1]["tool_calls"][0][
        "function"
    ]["arguments"]["command"]
    assert "save_model('/app/model.bin')" in target
    assert "path.exists()" not in target


def test_build_task_local_sft_records_can_pin_target_exec_timeout(
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
                    "command": "printf solved > /app/out.txt",
                    "aggregated_output": "",
                    "exit_code": 0,
                    "status": "completed",
                },
            }
        )
        + "\n",
        encoding="utf-8",
    )

    selection = TaskLocalSelection(
        task_id="gcode-to-text",
        failed=[
            TrajectoryPoolRow(
                trajectory_id="failed",
                task_id="gcode-to-text",
                reward=0.0,
                trial_dir=failed_trial,
                raw={"prompt_summary": "Write /app/out.txt"},
            )
        ],
        successful=[
            TrajectoryPoolRow(
                trajectory_id="success",
                task_id="gcode-to-text",
                reward=1.0,
                trial_dir=successful_trial,
                raw={},
            )
        ],
        null_reward=[],
    )

    [record] = build_task_local_sft_records(
        selection,
        command_contains=["/app/out.txt"],
        max_records=1,
        target_exec_timeout_seconds=30,
    )

    trace = record["traces"][0]
    tb_exec_tool = next(
        tool
        for tool in trace["tools"]
        if tool["function"]["name"] == "tb_exec"
    )
    assert tb_exec_tool["function"]["parameters"]["properties"]["timeout_seconds"] == {
        "type": "integer",
        "minimum": 1,
    }
    target_args = trace["response_messages"][-1]["tool_calls"][0]["function"][
        "arguments"
    ]
    assert target_args == {
        "task_id": "terminal-bench-task",
        "command": "printf solved > /app/out.txt",
        "timeout_seconds": 30,
    }
    assert record["metadata"]["target_exec_timeout_seconds"] == 30


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
    record = json.loads(Path(payload["dataset"]["records_path"]).read_text())
    assert record["metadata"]["prefix_source"] == "direct_solver_read_task"
    assert "completed_artifacts" not in payload


def test_terminal_bench_task_local_parametric_memory_job_cli_accepts_prompt_style(
    tmp_path: Path,
) -> None:
    failed_trial = tmp_path / "failed-trial"
    successful_trial = tmp_path / "successful-trial"
    llm_calls = (
        failed_trial
        / "agent"
        / "evolab_lab"
        / ".evolab"
        / "registries"
        / "trajectory"
        / "llm_calls.jsonl"
    )
    llm_calls.parent.mkdir(parents=True)
    (successful_trial / "agent").mkdir(parents=True)
    llm_calls.write_text(
        json.dumps(
            {
                "input_messages": [
                    {"role": "system", "content": "system"},
                    {"role": "user", "content": "user"},
                    {"role": "tool", "content": "read task", "tool_call_id": "read"},
                ],
                "output_messages": [
                    {
                        "role": "assistant",
                        "content": "",
                        "tool_calls": [
                            {
                                "id": "ls",
                                "type": "function",
                                "function": {
                                    "name": "tb_exec",
                                    "arguments": {
                                        "task_id": "terminal-bench-task",
                                        "command": "ls",
                                    },
                                },
                            }
                        ],
                    }
                ],
            }
        )
        + "\n",
        encoding="utf-8",
    )
    (successful_trial / "agent" / "codex.txt").write_text(
        json.dumps(
            {
                "type": "item.completed",
                "item": {
                    "type": "command_execution",
                    "command": "cp model.bin /app/model.bin",
                    "aggregated_output": "copied",
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
                "--prompt-style",
                "live_replay",
                "--output",
                str(output),
            ]
        )
        == 0
    )

    payload = json.loads(output.read_text(encoding="utf-8"))
    record = json.loads(Path(payload["dataset"]["records_path"]).read_text())
    assert payload["prompt_style"] == "live_replay"
    assert record["metadata"]["prefix_source"] == "live_replay_llm_call:1"


def test_terminal_bench_task_local_parametric_memory_job_cli_accepts_sequence_target_mode(
    tmp_path: Path,
) -> None:
    failed_trial = tmp_path / "failed-trial"
    successful_trial = tmp_path / "successful-trial"
    (failed_trial / "agent").mkdir(parents=True)
    (successful_trial / "agent").mkdir(parents=True)
    (successful_trial / "agent" / "codex.txt").write_text(
        "\n".join(
            [
                json.dumps(
                    {
                        "type": "item.completed",
                        "item": {
                            "type": "command_execution",
                            "command": "apt-get update && apt-get install -y g++",
                            "aggregated_output": "installed compiler",
                            "exit_code": 0,
                            "status": "completed",
                        },
                    }
                ),
                json.dumps(
                    {
                        "type": "item.completed",
                        "item": {
                            "type": "command_execution",
                            "command": (
                                "python train.py && cp model.bin /app/model.bin"
                            ),
                            "aggregated_output": "saved /app/model.bin",
                            "exit_code": 0,
                            "status": "completed",
                        },
                    }
                ),
            ]
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
                "prompt_summary": "Train a fastText classifier.",
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
                "--target-mode",
                "sequence",
                "--output",
                str(output),
            ]
        )
        == 0
    )

    payload = json.loads(output.read_text(encoding="utf-8"))
    records = [
        json.loads(line)
        for line in Path(payload["dataset"]["records_path"]).read_text().splitlines()
    ]
    assert payload["target_mode"] == "sequence"
    assert payload["dataset"]["record_count"] == 2
    assert [record["metadata"]["target_sequence_index"] for record in records] == [0, 1]
    assert [record["metadata"]["target_sequence_length"] for record in records] == [2, 2]


def test_terminal_bench_task_local_parametric_memory_job_cli_accepts_target_exec_timeout(
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
                    "command": "printf solved > /app/out.txt",
                    "aggregated_output": "",
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
                "task_id": "gcode-to-text",
                "reward": 0.0,
                "trial_dir": str(failed_trial),
                "prompt_summary": "Write /app/out.txt.",
            },
            {
                "trajectory_id": "success-1",
                "task_id": "gcode-to-text",
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
                "gcode-to-text",
                "--output-root",
                str(tmp_path / "out"),
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
                "/app/out.txt",
                "--target-exec-timeout-seconds",
                "30",
                "--output",
                str(output),
            ]
        )
        == 0
    )

    payload = json.loads(output.read_text(encoding="utf-8"))
    [record] = [
        json.loads(line)
        for line in Path(payload["dataset"]["records_path"]).read_text().splitlines()
    ]
    target_args = record["traces"][0]["response_messages"][-1]["tool_calls"][0][
        "function"
    ]["arguments"]
    assert payload["target_exec_timeout_seconds"] == 30
    assert target_args["timeout_seconds"] == 30
