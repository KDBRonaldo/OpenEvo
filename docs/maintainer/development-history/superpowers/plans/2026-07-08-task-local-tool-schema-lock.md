# Task-Local Tool Schema Lock Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add an optional task-local parametric-memory dataset augmentation that teaches Qwen local LoRA adapters to emit complete `tb_exec` tool arguments immediately after `tb_read_task`.

**Architecture:** Extend the existing Terminal Bench task-local SFT record builder with a narrow `include_tool_schema_lock` flag. When enabled, the builder adds one extra short-prefix record per selected failed/successful pair when the record budget allows it: prompt messages stop after the synthetic `tb_read_task` result, and the supervised response is a complete `tb_exec` tool call with `task_id`, `command`, and optional `timeout_seconds`. In `target_mode=sequence`, the schema-lock target is the first successful command in the selected sequence.

**Tech Stack:** Python, pytest, argparse CLI, existing `src/polar_evolution/terminal_bench_task_local_parametric.py` record projections.

---

### Task 1: Builder Test

**Files:**
- Modify: `tests/evolution/test_terminal_bench_task_local_parametric.py`
- Modify: `src/polar_evolution/terminal_bench_task_local_parametric.py`

- [x] **Step 1: Write the failing test**

Add a focused pytest near `test_build_task_local_sft_records_can_pin_target_exec_timeout`:

```python
def test_build_task_local_sft_records_can_add_tool_schema_lock_record(
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
                    "aggregated_output": "ok",
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

    records = build_task_local_sft_records(
        selection,
        command_contains=["/app/out.txt"],
        max_records=2,
        prompt_style="live_replay",
        target_exec_timeout_seconds=30,
        include_tool_schema_lock=True,
    )

    assert len(records) == 2
    schema_lock = records[1]
    assert schema_lock["metadata"]["target_correction_stage"] == "tool_schema_lock"
    assert schema_lock["metadata"]["prefix_source"] == "direct_solver_tool_schema_lock"
    trace = schema_lock["traces"][0]
    assert [message["role"] for message in trace["prompt_messages"]] == [
        "system",
        "user",
        "assistant",
        "tool",
    ]
    target_args = trace["response_messages"][0]["tool_calls"][0]["function"][
        "arguments"
    ]
    assert target_args == {
        "task_id": "terminal-bench-task",
        "command": "printf solved > /app/out.txt",
        "timeout_seconds": 30,
    }
```

- [x] **Step 2: Run test to verify it fails**

Run:

```bash
uv run pytest tests/evolution/test_terminal_bench_task_local_parametric.py::test_build_task_local_sft_records_can_add_tool_schema_lock_record -q
```

Expected: FAIL because `build_task_local_sft_records` does not accept `include_tool_schema_lock`.

- [x] **Step 3: Implement minimal builder support**

Add `include_tool_schema_lock: bool = False` to `build_task_local_sft_records`, append the schema-lock record after the base final record or sequence records when there is capacity, and create `_task_local_tool_schema_lock_record` that calls `_task_local_sft_record` with `prompt_messages_override=_task_local_direct_solver_prefix_messages(...)`, `prefix_source_override="direct_solver_tool_schema_lock"`, `target_correction_stage="tool_schema_lock"`, and a stable event suffix.

- [x] **Step 4: Verify builder test passes**

Run:

```bash
uv run pytest tests/evolution/test_terminal_bench_task_local_parametric.py::test_build_task_local_sft_records_can_add_tool_schema_lock_record -q
```

Expected: PASS.

### Task 2: CLI Plumbing Test

**Files:**
- Modify: `tests/evolution/test_terminal_bench_task_local_parametric.py`
- Modify: `src/polar_evolution/cli.py`

- [x] **Step 1: Write the failing CLI test**

Add a pytest near `test_terminal_bench_task_local_parametric_memory_job_cli_accepts_target_exec_timeout` that runs `main([... "--include-tool-schema-lock", ...])`, asserts `payload["include_tool_schema_lock"] is True`, asserts `payload["dataset"]["record_count"] == 2`, and checks the second record metadata stage is `tool_schema_lock`.

- [x] **Step 2: Run test to verify it fails**

Run:

```bash
uv run pytest tests/evolution/test_terminal_bench_task_local_parametric.py::test_terminal_bench_task_local_parametric_memory_job_cli_accepts_tool_schema_lock -q
```

Expected: FAIL because argparse does not know `--include-tool-schema-lock`.

- [x] **Step 3: Implement CLI flag**

Add `--include-tool-schema-lock` to the task-local parametric-memory job parser. Pass `args.include_tool_schema_lock` into `build_task_local_sft_records`, include it in payload top-level metadata, and include it in `target_filters` so dryrun manifests record the dataset recipe.

- [x] **Step 4: Verify CLI test passes**

Run:

```bash
uv run pytest tests/evolution/test_terminal_bench_task_local_parametric.py::test_terminal_bench_task_local_parametric_memory_job_cli_accepts_tool_schema_lock -q
```

Expected: PASS.

### Task 3: Docs And Regression

**Files:**
- Modify: `docs/dev/terminal-bench-memory-eval.md`
- Test: `tests/evolution/test_terminal_bench_task_local_parametric.py`

- [x] **Step 1: Document the flag**

Add a concise note to the task-local parametric-memory section explaining that `--include-tool-schema-lock` adds a short after-read-task `tb_exec` schema example for local inference adapters that generate malformed tool arguments.

- [x] **Step 2: Run focused tests**

Run:

```bash
uv run pytest tests/evolution/test_terminal_bench_task_local_parametric.py -q
uv run ruff check src/polar_evolution/terminal_bench_task_local_parametric.py src/polar_evolution/cli.py tests/evolution/test_terminal_bench_task_local_parametric.py
git diff --check
```

Expected: all pass.
