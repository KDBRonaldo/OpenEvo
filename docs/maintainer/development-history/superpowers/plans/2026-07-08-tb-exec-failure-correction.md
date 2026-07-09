# Failed tb_exec Correction Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [x]`) syntax for tracking.

**Goal:** Add a reusable task-local parametric-memory SFT export path for failed `tb_exec` correction prefixes.

**Architecture:** Extend the existing task-local Terminal Bench parametric dataset builder with a final-target-only `include_tb_exec_failure_correction` option. The new record reuses live replay prefixes from failed Harbor/EvoLab `llm_calls.jsonl`, selects prefixes that already contain a failed `tb_exec` tool result, and supervises the same successful `tb_exec` target used by the base record.

**Tech Stack:** Python, pytest, argparse CLI, Polar evolution task-local dataset builder.

---

### Task 1: Builder Regression Test

**Files:**
- Modify: `tests/evolution/test_terminal_bench_task_local_parametric.py`

- [x] **Step 1: Add a failed-`tb_exec` fixture**

Create `_write_train_fasttext_tb_exec_failure_fixture` near the existing correction fixtures. It must write a failed trial `agent/evolab_lab/.evolab/registries/trajectory/llm_calls.jsonl` containing:

```python
{"role": "tool", "name": "tb_read_task", "content": read_task_result}
{"role": "assistant", "tool_calls": [{"function": {"name": "tb_exec"}}]}
{"role": "tool", "name": "tb_exec", "content": failed_exec_result}
```

It must also write a successful trial `agent/codex.txt` containing a successful command that creates `/app/model.bin`.

- [x] **Step 2: Add a failing builder test**

Add `test_build_task_local_sft_records_can_add_tb_exec_failure_correction_prefix`. It must call:

```python
build_task_local_sft_records(
    selection,
    command_contains=["/app/model.bin"],
    max_records=2,
    prompt_style="live_replay",
    target_exec_timeout_seconds=300,
    include_tb_exec_failure_correction=True,
)
```

Expected assertions:

```python
assert len(records) == 2
assert records[1]["metadata"]["prefix_source"] == (
    "live_replay_tb_exec_failure_correction_llm_call:2"
)
assert records[1]["metadata"]["target_correction_stage"] == "tb_exec_failure"
assert records[1]["metadata"]["failed_tool_name"] == "tb_exec"
assert records[1]["metadata"]["failed_exit_code"] == 1
assert "traceback" in records[1]["metadata"]["failed_tool_failure_flags"]
assert "fasttext" in records[1]["metadata"]["failed_tool_failure_flags"]
```

- [x] **Step 3: Verify red**

Run:

```bash
uv run pytest tests/evolution/test_terminal_bench_task_local_parametric.py::test_build_task_local_sft_records_can_add_tb_exec_failure_correction_prefix -q
```

Expected: fail with an unexpected keyword argument for `include_tb_exec_failure_correction`.

### Task 2: Builder Implementation

**Files:**
- Modify: `src/polar_evolution/terminal_bench_task_local_parametric.py`

- [x] **Step 1: Add the public builder parameter**

Add `include_tb_exec_failure_correction: bool = False` to `build_task_local_sft_records`. Like the existing correction knobs, reject it unless `target_mode == "final"`.

- [x] **Step 2: Add correction record construction**

Add `_task_local_tb_exec_failure_correction_record` next to the existing correction record helpers. It must require `prompt_style == "live_replay"` and `target_mode == "final"`, call a prefix selector, and pass metadata into `_task_local_sft_record`.

- [x] **Step 3: Add prefix selection and metadata extraction**

Add `_task_local_tb_exec_failure_correction_prefix(trial_dir)` that scans `_trial_evolab_llm_calls(trial_dir)` and returns the first compact live-replay prefix whose raw input messages contain a failed `tb_exec` tool result. Return `(input_messages, call_index, failed_tool_metadata)`.

Required metadata keys:

```python
{
    "failed_tool_name": "tb_exec",
    "failed_tool_index": <1-based index in input_messages>,
    "failed_exit_code": <integer when available>,
    "failed_tool_failure_flags": ["traceback", "fasttext", "model_bin"],
}
```

Failure flags come from lowercased tool payload text and include only detected values from this fixed set: `syntax`, `traceback`, `fasttext`, `parquet`, `model_bin`, `timeout`.

- [x] **Step 4: Verify green**

Run the single test from Task 1 again. Expected: pass.

### Task 3: CLI Regression Test And Implementation

**Files:**
- Modify: `tests/evolution/test_terminal_bench_task_local_parametric.py`
- Modify: `src/polar_evolution/cli.py`

- [x] **Step 1: Add a failing CLI test**

Add `test_terminal_bench_task_local_parametric_memory_job_cli_accepts_tb_exec_failure_correction`. The CLI args must include:

```text
--prompt-style live_replay
--target-exec-timeout-seconds 300
--include-tb-exec-failure-correction
--max-records-per-task 2
```

Expected assertions:

```python
assert payload["include_tb_exec_failure_correction"] is True
assert payload["dataset"]["record_count"] == 2
assert records[1]["metadata"]["target_correction_stage"] == "tb_exec_failure"
```

- [x] **Step 2: Verify red**

Run:

```bash
uv run pytest tests/evolution/test_terminal_bench_task_local_parametric.py::test_terminal_bench_task_local_parametric_memory_job_cli_accepts_tb_exec_failure_correction -q
```

Expected: fail because argparse does not recognize `--include-tb-exec-failure-correction`.

- [x] **Step 3: Add CLI flag and payload field**

Add the argparse flag, pass `args.include_tb_exec_failure_correction` into `build_task_local_sft_records`, and include the boolean in the output payload.

- [x] **Step 4: Verify green**

Run the two new tests. Expected: both pass.

### Task 4: Documentation And Full Focused Verification

**Files:**
- Modify: `docs/architecture/evolution-api-and-method-integration.md`
- Modify: `docs/architecture/reference-evolution-worker.md`
- Modify: `docs/dev/terminal-bench-memory-eval.md`

- [x] **Step 1: Document the new flag**

Describe that `--include-tb-exec-failure-correction` is final-target-only, requires `--prompt-style live_replay`, preserves the failed `tb_exec` prefix, and supervises the selected successful `tb_exec` target.

- [x] **Step 2: Run focused checks**

Run:

```bash
uv run pytest tests/evolution/test_terminal_bench_task_local_parametric.py -q
uv run pytest tests/evolution/test_terminal_bench_local_parametric.py tests/evolution/test_terminal_bench_task_local_parametric.py -q
uv run ruff check src/polar_evolution/terminal_bench_task_local_parametric.py src/polar_evolution/cli.py tests/evolution/test_terminal_bench_task_local_parametric.py
git diff --check
```

Expected: all pass with no warnings relevant to this change.
