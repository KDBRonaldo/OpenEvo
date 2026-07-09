# Task-Local Parametric Memory Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build and evaluate task-local parametric-memory adapters from Terminal-Bench 2.1 mixed success/failure trajectory pools using local Qwen3.6/Qwen3/Qwen3.5 inference.

**Architecture:** Add a focused task-local dataset builder that converts trajectory-pool rows and Harbor/Codex transcripts into compact tool-call SFT dataset artifacts. Reuse the existing `parametric_memory_lora_sft` worker boundary and local parametric evaluation runner instead of adding a new artifact type. Keep task-local extraction, job creation, local eval, and experiment documentation as separate layers so datasets can be inspected before expensive training/eval.

**Tech Stack:** Python 3.11+ package code under `src/polar_evolution`, Pydantic evolution models, existing SQLite-backed `EvolutionStore`, pytest, existing Harbor/EvoLab Terminal-Bench runner, vLLM local OpenAI-compatible serving, PEFT LoRA adapters.

---

## Current Facts To Preserve

- Tracking issue: `CompLifeLab-ZJU/OpenEvo#36`.
- Active PR branch: `codex/parametric-memory-local-eval`.
- Design spec: `docs/superpowers/specs/2026-07-05-task-local-parametric-memory-design.md`.
- Candidate trajectory pool:
  `/home/jyw/openevo-skill-rl-experiments/pools/tb21-current-20260702/trajectory_pool.jsonl`.
- Mixed candidate counts observed on 2026-07-07:
  - `train-fasttext`: 11 fail, 4 null, 4 pass.
  - `make-mips-interpreter`: 15 fail, 5 pass.
  - `gcode-to-text`: 17 fail, 5 pass.
  - `password-recovery`: 2 fail, 15 pass.
- Candidate historical trial dirs do not reliably contain EvoLab
  `agent/evolab_lab/.evolab/registries/trajectory/llm_calls.jsonl`.
- Candidate historical trial dirs do contain Codex event-stream transcript files such as
  `agent/codex.txt`.
- Locally cached deployable models:
  - `Qwen/Qwen3.6-35B-A3B`, snapshot `995ad96eacd98c81ed38be0c5b274b04031597b0`.
  - `Qwen/Qwen3-30B-A3B-Instruct-2507`, snapshot `0d7cf23991f47feeb3a57ecb4c9cee8ea4a17bfe`.
  - `Qwen/Qwen3.5-9B`, snapshot `c202236235762e1c871ad0ccb60c8ee5ba337b9a`.
- GPU state observed on 2026-07-07: GPUs 6 and 7 were idle; GPUs 0-5 were occupied. Use a fresh `nvidia-smi` check before live runs.

---

## File Structure

- Create `src/polar_evolution/terminal_bench_task_local_parametric.py`.
  - Owns trajectory-pool loading, task selection, Codex event parsing, successful command extraction, task-local SFT record generation, dataset artifact writing, and job config creation.
  - Does not call vLLM, Harbor, or the worker directly.
- Modify `src/polar_evolution/methods.py`.
  - Preserve trace-level `tools` when `parametric_memory_lora_sft` consumes task-local records through the default `full_trace` projection.
- Modify `src/polar_evolution/cli.py`.
  - Add `terminal-bench-task-local-parametric-memory-job`.
  - It builds a task-local dataset artifact and creates a `parametric_memory_lora_sft` job.
  - It may optionally run the worker once, matching the existing `terminal-bench-parametric-memory-job` pattern.
- Create `tests/evolution/test_terminal_bench_task_local_parametric.py`.
  - Unit tests for task selection, transcript parsing, command extraction, dataset writing, and job config creation.
- Modify `tests/evolution/test_worker_methods.py`.
  - Regression test that task-local full-trace records preserve `tools`.
- Modify `tests/evolution/test_terminal_bench_local_parametric.py`.
  - CLI coverage for the new subcommand.
- Modify docs:
  - `docs/dev/terminal-bench-memory-eval.md`.
  - `docs/architecture/reference-evolution-worker.md`.
  - `docs/architecture/evolution-api-and-method-integration.md`.

---

## Task 1: Task-Local Pool Selection

**Files:**
- Create: `src/polar_evolution/terminal_bench_task_local_parametric.py`
- Create: `tests/evolution/test_terminal_bench_task_local_parametric.py`

- [ ] **Step 1: Write the failing test for mixed task selection**

Add this test file with the imports and first test:

```python
from __future__ import annotations

import json
from pathlib import Path

from polar_evolution.terminal_bench_task_local_parametric import (
    TaskLocalSelection,
    TrajectoryPoolRow,
    select_task_local_candidates,
)


def _write_pool(path: Path, rows: list[dict]) -> None:
    path.write_text(
        "".join(json.dumps(row, sort_keys=True) + "\n" for row in rows),
        encoding="utf-8",
    )


def test_select_task_local_candidates_requires_success_and_failure(tmp_path: Path) -> None:
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

    [selection] = select_task_local_candidates(pool, task_ids=["train-fasttext", "query-optimize", "dna-insert"])

    assert isinstance(selection, TaskLocalSelection)
    assert selection.task_id == "train-fasttext"
    assert [row.trajectory_id for row in selection.failed] == ["train-fail-1"]
    assert [row.trajectory_id for row in selection.successful] == ["train-pass-1"]
    assert [row.trajectory_id for row in selection.null_reward] == ["null-run"]
```

- [ ] **Step 2: Run the test to verify it fails**

Run:

```bash
uv run pytest tests/evolution/test_terminal_bench_task_local_parametric.py::test_select_task_local_candidates_requires_success_and_failure -q
```

Expected: FAIL with `ModuleNotFoundError: No module named 'polar_evolution.terminal_bench_task_local_parametric'`.

- [ ] **Step 3: Implement the minimal selector**

Create `src/polar_evolution/terminal_bench_task_local_parametric.py`:

```python
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
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
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
        reward = payload.get("reward")
        parsed_reward = float(reward) if isinstance(reward, int | float) else None
        rows.append(
            TrajectoryPoolRow(
                trajectory_id=trajectory_id,
                task_id=task_id.strip(),
                reward=parsed_reward,
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
        successful = [row for row in rows if row.reward is not None and row.reward >= 1.0]
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
```

- [ ] **Step 4: Run the test to verify it passes**

Run:

```bash
uv run pytest tests/evolution/test_terminal_bench_task_local_parametric.py::test_select_task_local_candidates_requires_success_and_failure -q
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/polar_evolution/terminal_bench_task_local_parametric.py tests/evolution/test_terminal_bench_task_local_parametric.py
git commit -m "feat: select task-local parametric memory candidates"
```

---

## Task 2: Codex Transcript Parsing And Successful Command Extraction

**Files:**
- Modify: `src/polar_evolution/terminal_bench_task_local_parametric.py`
- Modify: `tests/evolution/test_terminal_bench_task_local_parametric.py`

- [ ] **Step 1: Write the failing transcript extraction test**

Append:

```python
from polar_evolution.terminal_bench_task_local_parametric import (
    extract_successful_codex_commands,
)


def test_extract_successful_codex_commands_reads_completed_command_events(tmp_path: Path) -> None:
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
                            "command": "/bin/bash -lc 'python train.py && cp model.bin /app/model.bin'",
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

    commands = extract_successful_codex_commands(transcript, command_contains=["/app/model.bin"])

    assert [command.command for command in commands] == [
        "/bin/bash -lc 'python train.py && cp model.bin /app/model.bin'"
    ]
    assert commands[0].event_index == 2
    assert commands[0].exit_code == 0
    assert "accuracy" in commands[0].output_excerpt
```

- [ ] **Step 2: Run the test to verify it fails**

Run:

```bash
uv run pytest tests/evolution/test_terminal_bench_task_local_parametric.py::test_extract_successful_codex_commands_reads_completed_command_events -q
```

Expected: FAIL with import error for `extract_successful_codex_commands`.

- [ ] **Step 3: Implement transcript parsing**

Add to `src/polar_evolution/terminal_bench_task_local_parametric.py`:

```python
@dataclass(frozen=True)
class CodexCommandEvent:
    event_index: int
    command: str
    exit_code: int | None
    status: str | None
    output_excerpt: str


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
                output_excerpt=str(output)[:max(1, int(max_output_chars))],
            )
        )
    return commands
```

- [ ] **Step 4: Run the test to verify it passes**

Run:

```bash
uv run pytest tests/evolution/test_terminal_bench_task_local_parametric.py::test_extract_successful_codex_commands_reads_completed_command_events -q
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/polar_evolution/terminal_bench_task_local_parametric.py tests/evolution/test_terminal_bench_task_local_parametric.py
git commit -m "feat: parse terminal bench codex command transcripts"
```

---

## Task 3: Build Task-Local SFT Records

**Files:**
- Modify: `src/polar_evolution/terminal_bench_task_local_parametric.py`
- Modify: `tests/evolution/test_terminal_bench_task_local_parametric.py`

- [ ] **Step 1: Write the failing record-builder test**

Append:

```python
from polar_evolution.terminal_bench_task_local_parametric import (
    build_task_local_sft_records,
    TrajectoryPoolRow,
)


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
                    "command": "/bin/bash -lc 'python train.py && cp model.bin /app/model.bin'",
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
    assert [message["role"] for message in trace["prompt_messages"]] == ["system", "user"]
    assert trace["response_messages"][0]["tool_calls"][0]["function"]["name"] == "tb_exec"
    assert trace["response_messages"][0]["tool_calls"][0]["function"]["arguments"] == {
        "task_id": "terminal-bench-task",
        "command": "/bin/bash -lc 'python train.py && cp model.bin /app/model.bin'",
    }
    assert record["metadata"]["source_failed_trajectory_id"] == "failed-1"
    assert record["metadata"]["source_successful_trajectory_id"] == "success-1"
    assert record["metadata"]["prefix_source"] == "task_summary_fallback"
```

- [ ] **Step 2: Run the test to verify it fails**

Run:

```bash
uv run pytest tests/evolution/test_terminal_bench_task_local_parametric.py::test_build_task_local_sft_records_uses_successful_command_as_tb_exec_target -q
```

Expected: FAIL with import error for `build_task_local_sft_records`.

- [ ] **Step 3: Implement the record builder**

Add constants and builder code:

```python
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

_TASK_LOCAL_SYSTEM = "Use Terminal-Bench tools to solve the task. Emit one useful next tool call."


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
            command = commands[-1]
            prompt = (
                "Task-local parametric memory correction.\n\n"
                f"Task id: {selection.task_id}\n"
                f"Failed trajectory summary: {_task_summary(failed)}\n"
                f"Successful trajectory summary: {_task_summary(successful)}\n\n"
                "Produce the next solve action as a tool call."
            )
            response_message = {
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
            records.append(
                {
                    "event_id": (
                        f"task-local-parametric:{selection.task_id}:"
                        f"{failed.trajectory_id}:{successful.trajectory_id}:"
                        f"{command.event_index}"
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
                            "response_messages": [response_message],
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
            )
            break
    return records
```

- [ ] **Step 4: Run the test to verify it passes**

Run:

```bash
uv run pytest tests/evolution/test_terminal_bench_task_local_parametric.py::test_build_task_local_sft_records_uses_successful_command_as_tb_exec_target -q
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/polar_evolution/terminal_bench_task_local_parametric.py tests/evolution/test_terminal_bench_task_local_parametric.py
git commit -m "feat: build task-local terminal bench SFT records"
```

---

## Task 4: Write Dataset Artifact And Job Config

**Files:**
- Modify: `src/polar_evolution/terminal_bench_task_local_parametric.py`
- Modify: `tests/evolution/test_terminal_bench_task_local_parametric.py`

- [ ] **Step 1: Write the failing dataset/job test**

Append:

```python
from polar_evolution.terminal_bench_task_local_parametric import (
    build_task_local_parametric_job_payload,
)


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
    assert json.loads(records_path.read_text(encoding="utf-8"))["task_id"] == "train-fasttext"
    assert payload["dataset"]["artifact"]["type"] == "dataset"
    assert payload["job"]["method"] == "parametric_memory_lora_sft"
    assert payload["job"]["input_artifacts"][0]["uri"] == manifest_path.resolve().as_uri()
    assert payload["job"]["config"]["training_projection"] == {"type": "full_trace"}
    assert payload["job"]["config"]["compatibility"]["task_tags"] == [
        "terminal-bench",
        "terminal-bench:train-fasttext",
    ]
```

- [ ] **Step 2: Run the test to verify it fails**

Run:

```bash
uv run pytest tests/evolution/test_terminal_bench_task_local_parametric.py::test_build_task_local_parametric_job_payload_writes_dataset_and_lora_job -q
```

Expected: FAIL with import error for `build_task_local_parametric_job_payload`.

- [ ] **Step 3: Implement dataset/job payload writing**

Add imports and function:

```python
from polar_evolution.models import ArtifactType, WorkerClaimInputArtifact, WorkerClaimedJob


def _safe_name(value: str) -> str:
    safe = "".join(ch if ch.isalnum() or ch in {"-", "_"} else "-" for ch in value.strip())
    return "-".join(part for part in safe.split("-") if part) or "task-local-parametric"


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
        "".join(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n" for record in records),
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
    job_id = f"job-{_safe_name(adapter_id)}"
    input_artifact = WorkerClaimInputArtifact(
        artifact_id=dataset_artifact_id,
        type=ArtifactType.DATASET,
        uri=manifest_path.resolve().as_uri(),
        name=dataset_name,
    )
    config = {
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
            "task_tags": ["terminal-bench", *[f"terminal-bench:{task_id}" for task_id in task_ids]],
            "base_model": [base_model],
        },
        "lineage": {
            "method": "terminal_bench_task_local_parametric",
            "source_task_ids": list(task_ids),
            "dataset_manifest_uri": manifest_path.resolve().as_uri(),
        },
        "scores": {"quality": 0.0, "train_record_count": float(len(records))},
        "promoted": False,
    }
    job = WorkerClaimedJob(
        job_id=job_id,
        lease_id="local-task-local-plan",
        job_type="parametric_memory_lora_sft",
        method="parametric_memory_lora_sft",
        input_artifacts=[input_artifact],
        config=config,
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
```

- [ ] **Step 4: Run the test to verify it passes**

Run:

```bash
uv run pytest tests/evolution/test_terminal_bench_task_local_parametric.py::test_build_task_local_parametric_job_payload_writes_dataset_and_lora_job -q
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/polar_evolution/terminal_bench_task_local_parametric.py tests/evolution/test_terminal_bench_task_local_parametric.py
git commit -m "feat: write task-local parametric memory job payloads"
```

---

## Task 5: Preserve Trace-Level Tools In LoRA SFT Export

**Files:**
- Modify: `src/polar_evolution/methods.py`
- Modify: `tests/evolution/test_worker_methods.py`

- [ ] **Step 1: Write the failing worker-method test**

Append near the other `parametric_memory_lora_sft` tests:

```python
def test_parametric_memory_lora_sft_full_trace_preserves_trace_tools(tmp_path: Path):
    trainer_script = tmp_path / "fake_trainer.py"
    trainer_script.write_text(
        "from pathlib import Path\n"
        "import argparse\n"
        "parser = argparse.ArgumentParser()\n"
        "parser.add_argument('--train-file')\n"
        "parser.add_argument('--output-dir')\n"
        "args = parser.parse_args()\n"
        "Path(args.output_dir).mkdir(parents=True, exist_ok=True)\n"
        "(Path(args.output_dir) / 'adapter_config.json').write_text('{}')\n",
        encoding="utf-8",
    )
    tools = [
        {
            "type": "function",
            "function": {
                "name": "tb_exec",
                "parameters": {
                    "type": "object",
                    "properties": {"command": {"type": "string"}},
                },
            },
        }
    ]
    dataset = _parametric_dataset_artifact(
        tmp_path,
        [
            {
                "event_id": "evt_task_local",
                "task_id": "train-fasttext",
                "status": "COMPLETED",
                "reward": 1.0,
                "traces": [
                    {
                        "prompt_messages": [{"role": "user", "content": "Train model."}],
                        "response_messages": [
                            {
                                "role": "assistant",
                                "content": "",
                                "tool_calls": [
                                    {
                                        "id": "call-target",
                                        "type": "function",
                                        "function": {
                                            "name": "tb_exec",
                                            "arguments": {
                                                "command": "cp model.bin /app/model.bin"
                                            },
                                        },
                                    }
                                ],
                            }
                        ],
                        "tools": tools,
                    }
                ],
            }
        ],
    )
    job = _job(
        "parametric_memory_lora_sft",
        tmp_path,
        input_artifacts=[dataset],
        config={
            "base_model": "Qwen/Qwen3.6-35B-A3B",
            "training_projection": {"type": "full_trace"},
            "trainer": {
                "command": "python",
                "args": [
                    str(trainer_script),
                    "--train-file",
                    "{training_dataset}",
                    "--output-dir",
                    "{adapter_dir}",
                ],
            },
        },
    )

    run_method(job, artifact_root=tmp_path / "artifacts")

    train_path = (
        tmp_path
        / "artifacts"
        / "workers"
        / job.job_id
        / "parametric_memory_lora_sft"
        / "training.jsonl"
    )
    [training_line] = [
        json.loads(line) for line in train_path.read_text(encoding="utf-8").splitlines()
    ]
    assert training_line["tools"] == tools
```

- [ ] **Step 2: Run the test to verify it fails**

Run:

```bash
uv run pytest tests/evolution/test_worker_methods.py::test_parametric_memory_lora_sft_full_trace_preserves_trace_tools -q
```

Expected: FAIL because `tools` is absent from the exported training record.

- [ ] **Step 3: Implement trace-level tools preservation**

In `_sft_message_sets_from_record`, after `response_messages` is computed and before projection branches, add:

```python
        trace_extra_fields: dict[str, Any] = {}
        trace_tools = trace.get("tools")
        if isinstance(trace_tools, list):
            trace_extra_fields["tools"] = trace_tools
```

Then change the default append from:

```python
            message_sets.append((trace_index, [*prompt_messages, *response_messages], {}))
```

to:

```python
            message_sets.append(
                (trace_index, [*prompt_messages, *response_messages], dict(trace_extra_fields))
            )
```

Do not add `trace_extra_fields` to existing `terminal_bench_tool_call_policy` or
`terminal_bench_corrective_tool_call_policy` branches because those projections already
compute their own tool schemas.

- [ ] **Step 4: Run the test to verify it passes**

Run:

```bash
uv run pytest tests/evolution/test_worker_methods.py::test_parametric_memory_lora_sft_full_trace_preserves_trace_tools -q
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/polar_evolution/methods.py tests/evolution/test_worker_methods.py
git commit -m "fix: preserve task-local tools in parametric SFT records"
```

---

## Task 6: CLI For Task-Local Job Creation

**Files:**
- Modify: `src/polar_evolution/cli.py`
- Modify: `tests/evolution/test_terminal_bench_local_parametric.py`

- [ ] **Step 1: Write the failing CLI test**

Append to `tests/evolution/test_terminal_bench_local_parametric.py`:

```python
def test_terminal_bench_task_local_parametric_memory_job_cli_writes_payload(
    tmp_path: Path,
) -> None:
    pool = tmp_path / "trajectory_pool.jsonl"
    failed = tmp_path / "failed"
    success = tmp_path / "success"
    (failed / "agent").mkdir(parents=True)
    (success / "agent").mkdir(parents=True)
    (success / "agent" / "codex.txt").write_text(
        json.dumps(
            {
                "type": "item.completed",
                "item": {
                    "type": "command_execution",
                    "command": "cp model.bin /app/model.bin",
                    "aggregated_output": "ok",
                    "exit_code": 0,
                    "status": "completed",
                },
            }
        )
        + "\n",
        encoding="utf-8",
    )
    pool.write_text(
        "".join(
            json.dumps(row, sort_keys=True) + "\n"
            for row in [
                {
                    "trajectory_id": "failed-1",
                    "task_id": "train-fasttext",
                    "reward": 0.0,
                    "trial_dir": str(failed),
                    "prompt_summary": "Train fastText.",
                },
                {
                    "trajectory_id": "success-1",
                    "task_id": "train-fasttext",
                    "reward": 1.0,
                    "trial_dir": str(success),
                    "response_summary": "Wrote model.bin.",
                },
            ]
        ),
        encoding="utf-8",
    )
    output = tmp_path / "payload.json"

    exit_code = main(
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
            "/opt/train_lora.py",
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

    assert exit_code == 0
    payload = json.loads(output.read_text(encoding="utf-8"))
    assert payload["selected_tasks"] == ["train-fasttext"]
    assert payload["dataset"]["record_count"] == 1
    assert payload["job"]["config"]["output_adapter_id"] == "tb-parametric-memory-train-fasttext"
    assert "completed_artifacts" not in payload
```

- [ ] **Step 2: Run the test to verify it fails**

Run:

```bash
uv run pytest tests/evolution/test_terminal_bench_local_parametric.py::test_terminal_bench_task_local_parametric_memory_job_cli_writes_payload -q
```

Expected: FAIL because the CLI subcommand is unknown.

- [ ] **Step 3: Add parser and command handler**

In `src/polar_evolution/cli.py`, import:

```python
from polar_evolution.terminal_bench_task_local_parametric import (
    build_task_local_parametric_job_payload,
    build_task_local_sft_records,
    select_task_local_candidates,
)
from polar_evolution.methods import run_method
from polar_evolution.models import WorkerClaimedJob
```

Add parser near the existing parametric commands:

```python
    task_local_parametric = subparsers.add_parser(
        "terminal-bench-task-local-parametric-memory-job",
        help="Build a task-local Terminal-Bench parametric-memory LoRA SFT job.",
    )
    task_local_parametric.add_argument("--trajectory-pool", required=True)
    task_local_parametric.add_argument("--task-id", action="append", default=[])
    task_local_parametric.add_argument("--output-root", required=True)
    task_local_parametric.add_argument("--dataset-name", required=True)
    task_local_parametric.add_argument("--base-model", default="Qwen/Qwen3.6-35B-A3B")
    task_local_parametric.add_argument("--adapter-id", required=True)
    task_local_parametric.add_argument("--trainer-command", required=True)
    task_local_parametric.add_argument("--trainer-arg", action="append", default=[])
    task_local_parametric.add_argument("--trainer-timeout-seconds", type=float, default=3600.0)
    task_local_parametric.add_argument("--artifact-root")
    task_local_parametric.add_argument("--run-worker", action="store_true")
    task_local_parametric.add_argument("--command-contains", action="append", default=[])
    task_local_parametric.add_argument("--exclude-command-contains", action="append", default=[])
    task_local_parametric.add_argument("--max-records-per-task", type=int, default=16)
    task_local_parametric.add_argument("--output")
```

Add dispatch:

```python
    if args.command == "terminal-bench-task-local-parametric-memory-job":
        _write_json_output(_create_terminal_bench_task_local_parametric_memory_job(args), args.output)
        return 0
```

Add handler:

```python
def _create_terminal_bench_task_local_parametric_memory_job(
    args: argparse.Namespace,
) -> dict[str, Any]:
    selections = select_task_local_candidates(
        Path(args.trajectory_pool),
        task_ids=list(args.task_id),
    )
    records: list[dict[str, Any]] = []
    for selection in selections:
        records.extend(
            build_task_local_sft_records(
                selection,
                command_contains=list(args.command_contains),
                exclude_command_contains=list(args.exclude_command_contains),
                max_records=args.max_records_per_task,
            )
        )
    if not records:
        raise ValueError("terminal-bench-task-local-parametric-memory-job produced no records")
    selected_task_ids = [selection.task_id for selection in selections]
    payload = build_task_local_parametric_job_payload(
        records=records,
        output_root=Path(args.output_root),
        dataset_name=args.dataset_name,
        base_model=args.base_model,
        adapter_id=args.adapter_id,
        trainer_command=args.trainer_command,
        trainer_args=list(args.trainer_arg),
        task_ids=selected_task_ids,
        trainer_timeout_seconds=args.trainer_timeout_seconds,
    )
    payload["selected_tasks"] = selected_task_ids
    payload["trajectory_pool"] = str(Path(args.trajectory_pool))
    if args.run_worker:
        artifact_root = (
            Path(args.artifact_root)
            if args.artifact_root
            else Path(args.output_root) / "artifacts"
        )
        artifacts = run_method(
            WorkerClaimedJob.model_validate(payload["job"]),
            artifact_root=artifact_root,
        )
        payload["completed_artifacts"] = [
            artifact.model_dump(mode="json") for artifact in artifacts
        ]
        completed_path = Path(args.output_root) / "completed_artifacts.json"
        completed_path.write_text(
            json.dumps(payload["completed_artifacts"], indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        payload["completed_artifacts_path"] = str(completed_path)
    return payload
```

- [ ] **Step 4: Run the test to verify it passes**

Run:

```bash
uv run pytest tests/evolution/test_terminal_bench_local_parametric.py::test_terminal_bench_task_local_parametric_memory_job_cli_writes_payload -q
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/polar_evolution/cli.py tests/evolution/test_terminal_bench_local_parametric.py
git commit -m "feat: add task-local parametric memory CLI"
```

---

## Task 7: Documentation

**Files:**
- Modify: `docs/dev/terminal-bench-memory-eval.md`
- Modify: `docs/architecture/reference-evolution-worker.md`
- Modify: `docs/architecture/evolution-api-and-method-integration.md`

- [ ] **Step 1: Update developer experiment docs**

Add a "Task-local parametric-memory datasets" section to
`docs/dev/terminal-bench-memory-eval.md`:

````markdown
## Task-local parametric-memory datasets

Use `terminal-bench-task-local-parametric-memory-job` to turn a mixed
success/failure trajectory pool into a compact SFT dataset and a
`parametric_memory_lora_sft` job payload:

```bash
uv run polar-evolution terminal-bench-task-local-parametric-memory-job \
  --trajectory-pool /home/jyw/openevo-skill-rl-experiments/pools/tb21-current-20260702/trajectory_pool.jsonl \
  --task-id train-fasttext \
  --output-root /tmp/tb21-task-local-parametric/train-fasttext \
  --dataset-name tb21-task-local-train-fasttext \
  --base-model Qwen/Qwen3.6-35B-A3B \
  --adapter-id tb-parametric-memory-train-fasttext \
  --trainer-command python \
  --trainer-arg /tmp/qwen36_lora_sft.py \
  --trainer-arg --train-file \
  --trainer-arg '{training_dataset}' \
  --trainer-arg --output-dir \
  --trainer-arg '{adapter_dir}' \
  --command-contains /app/model.bin \
  --output /tmp/tb21-task-local-parametric/train-fasttext/job.json
```

The command writes an inspectable dataset artifact under `--output-root/dataset`
and a local `parametric_memory_lora_sft` job payload. It uses Codex transcript
command events from successful same-task trials as solve-action targets and
failed same-task rows as correction context. It does not enable `text_memory`,
`skill_bundle`, or `agent_system`.
````

- [ ] **Step 2: Update worker reference docs**

In `docs/architecture/reference-evolution-worker.md`, extend the
`parametric_memory_lora_sft` section with:

```markdown
- Task-local Terminal-Bench records may provide `trace.tools` alongside
  `prompt_messages` and `response_messages`. The default `full_trace`
  projection preserves that tool schema in the exported training JSONL so Qwen
  chat-template trainers can render tool calls correctly.
```

- [ ] **Step 3: Update API/method integration docs**

In `docs/architecture/evolution-api-and-method-integration.md`, add:

```markdown
Task-local parametric-memory builders should emit ordinary dataset artifacts
whose records contain SFT-ready traces. They should still train through
`parametric_memory_lora_sft` and register `ArtifactRegisterRequest(type =
parametric_memory)`. The builder is a data-preparation layer, not a new artifact
contract.
```

- [ ] **Step 4: Commit**

```bash
git add docs/dev/terminal-bench-memory-eval.md docs/architecture/reference-evolution-worker.md docs/architecture/evolution-api-and-method-integration.md
git commit -m "docs: describe task-local parametric memory datasets"
```

---

## Task 8: Focused Verification

**Files:**
- No edits.

- [ ] **Step 1: Run focused tests**

Run:

```bash
uv run pytest tests/evolution/test_terminal_bench_task_local_parametric.py tests/evolution/test_worker_methods.py::test_parametric_memory_lora_sft_full_trace_preserves_trace_tools tests/evolution/test_terminal_bench_local_parametric.py::test_terminal_bench_task_local_parametric_memory_job_cli_writes_payload -q
```

Expected: all selected tests pass.

- [ ] **Step 2: Run existing local parametric regression tests**

Run:

```bash
uv run pytest tests/evolution/test_terminal_bench_local_parametric.py -q
```

Expected: all tests pass.

- [ ] **Step 3: Run lint on touched Python files**

Run:

```bash
uv run ruff check src/polar_evolution/terminal_bench_task_local_parametric.py src/polar_evolution/methods.py src/polar_evolution/cli.py tests/evolution/test_terminal_bench_task_local_parametric.py tests/evolution/test_terminal_bench_local_parametric.py tests/evolution/test_worker_methods.py
```

Expected: `All checks passed!`

- [ ] **Step 4: Run diff check**

Run:

```bash
git diff --check
```

Expected: no output, exit code 0.

---

## Task 9: Dry-Run Dataset Build On Real Pool

**Files:**
- No repo edits unless the command reveals a bug.

- [ ] **Step 1: Build a real train-fasttext task-local payload**

Run:

```bash
RUN_ROOT=/tmp/tb21-task-local-parametric-$(date -u +%Y%m%d-%H%M%S)
mkdir -p "$RUN_ROOT"
uv run polar-evolution terminal-bench-task-local-parametric-memory-job \
  --trajectory-pool /home/jyw/openevo-skill-rl-experiments/pools/tb21-current-20260702/trajectory_pool.jsonl \
  --task-id train-fasttext \
  --output-root "$RUN_ROOT/train-fasttext" \
  --dataset-name tb21-task-local-train-fasttext \
  --base-model Qwen/Qwen3.6-35B-A3B \
  --adapter-id tb-parametric-memory-train-fasttext \
  --trainer-command python \
  --trainer-arg /tmp/qwen36_lora_sft.py \
  --trainer-arg --train-file \
  --trainer-arg '{training_dataset}' \
  --trainer-arg --output-dir \
  --trainer-arg '{adapter_dir}' \
  --command-contains /app/model.bin \
  --output "$RUN_ROOT/train-fasttext/job.json"
```

Expected: `job.json` exists and reports `dataset.record_count >= 1`.

- [ ] **Step 2: Inspect the generated SFT records**

Run:

```bash
python3 - <<'PY'
import json
from pathlib import Path
root = sorted(Path('/tmp').glob('tb21-task-local-parametric-*'))[-1]
records = root / 'train-fasttext' / 'dataset' / 'records.jsonl'
rows = [json.loads(line) for line in records.read_text(encoding='utf-8').splitlines() if line.strip()]
print('run_root', root)
print('record_count', len(rows))
for row in rows[:3]:
    call = row['traces'][0]['response_messages'][0]['tool_calls'][0]['function']
    print(row['task_id'], row['metadata']['source_failed_trajectory_id'], row['metadata']['source_successful_trajectory_id'])
    print(call['name'], call['arguments']['command'][:240].replace('\n', '\\n'))
PY
```

Expected: command targets are solve actions for `train-fasttext`, not hidden verifier literals.

- [ ] **Step 3: Save the run root for live training**

Record the printed run root in the PR comment and in
`docs/dev/terminal-bench-memory-eval.md` only after the command output has been
reviewed.

---

## Task 10: Live Training And Local Eval Smoke

**Files:**
- No repo edits unless a bug is found.
- Later docs update expected after real results.

- [ ] **Step 1: Check GPU/process state**

Run:

```bash
nvidia-smi --query-gpu=index,name,memory.used,memory.total,utilization.gpu --format=csv,noheader,nounits
ps -eo pid,ppid,stat,cmd | rg 'vllm|Qwen3|Qwen3\\.5|Qwen3\\.6|Qwen/Qwen' | rg -v 'rg '
```

Expected: choose idle GPUs. On 2026-07-07, GPUs 6 and 7 were idle; do not assume this remains true.

- [ ] **Step 2: Train the first adapter**

Use the generated dataset path with the existing Qwen LoRA trainer script used
by prior parametric-memory runs. If the trainer script path has moved, locate it
with:

```bash
find /tmp /root -maxdepth 4 -name '*qwen* lora*' -o -name 'qwen36_lora_sft.py' 2>/dev/null | head
```

Then run:

```bash
RUN_ROOT=$(python3 - <<'PY'
from pathlib import Path
print(sorted(Path('/tmp').glob('tb21-task-local-parametric-*'))[-1])
PY
)
uv run polar-evolution terminal-bench-task-local-parametric-memory-job \
  --trajectory-pool /home/jyw/openevo-skill-rl-experiments/pools/tb21-current-20260702/trajectory_pool.jsonl \
  --task-id train-fasttext \
  --output-root "$RUN_ROOT/train-fasttext" \
  --artifact-root "$RUN_ROOT/train-fasttext/artifacts" \
  --dataset-name tb21-task-local-train-fasttext \
  --base-model Qwen/Qwen3.6-35B-A3B \
  --adapter-id tb-parametric-memory-train-fasttext \
  --trainer-command python \
  --trainer-arg /tmp/qwen36_lora_sft.py \
  --trainer-arg --train-file \
  --trainer-arg '{training_dataset}' \
  --trainer-arg --output-dir \
  --trainer-arg '{adapter_dir}' \
  --command-contains /app/model.bin \
  --run-worker \
  --output "$RUN_ROOT/train-fasttext/job-with-worker.json"
```

Expected: `$RUN_ROOT/train-fasttext/completed_artifacts.json` exists and contains
one `parametric_memory` artifact whose URI points to a PEFT adapter directory
with `adapter_config.json`.

- [ ] **Step 3: Run baseline versus adapter on local Qwen3.6**

Run the existing local eval command, selecting idle GPUs:

```bash
ADAPTER_PATH=$(python3 - <<'PY'
import json
from pathlib import Path
root = sorted(Path('/tmp').glob('tb21-task-local-parametric-*'))[-1]
payload = json.loads((root / 'train-fasttext' / 'completed_artifacts.json').read_text(encoding='utf-8'))
for artifact in payload:
    if artifact.get('type') == 'parametric_memory':
        print(artifact['uri'].removeprefix('file://'))
        break
PY
)
uv run polar-evolution terminal-bench-local-parametric-memory-eval \
  --task-root /root/datasets/terminal-bench-2-1/tasks \
  --task-id train-fasttext \
  --run-root /tmp/tb21-task-local-parametric-eval-train-fasttext \
  --terminal-bench-package-root /root/EvoLabCore-terminal-bench-task-package \
  --model Qwen/Qwen3.6-35B-A3B \
  --adapter-path "$ADAPTER_PATH" \
  --adapter-id tb-parametric-memory-train-fasttext \
  --adapter-key-rewrite qwen3_5_moe_vllm_language_model \
  --n-attempts 1 \
  --gpus 6,7 \
  --port 8000 \
  --agent-env EVOLAB_TB_DIRECT_SOLVER_COMPLETION_GUARD=successful_auto_tested_exec \
  --agent-env EVOLAB_TB_TEST_COMMAND='test -s /app/model.bin' \
  --output /tmp/tb21-task-local-parametric-eval-train-fasttext/summary.json
```

Expected: summary contains baseline and parametric-memory conditions, no Harbor
exception count, and server metadata for both conditions.

- [ ] **Step 4: If Qwen3.6 cannot fit on available GPUs, use Qwen3.5-9B smoke**

Run the same command with:

```text
--model Qwen/Qwen3.5-9B
--adapter-key-rewrite none
--gpus 6
```

Expected: this is labeled as a low-cost smoke only. Do not compare the resulting
delta with Qwen3.6 results.

- [ ] **Step 5: Record live evidence**

Update `docs/dev/terminal-bench-memory-eval.md` with:

- run root;
- model and GPUs;
- adapter artifact path/id;
- baseline pass@1/pass@k;
- treatment pass@1/pass@k;
- Harbor exception/timeout status;
- tool and LLM call counts if available;
- explicit caveat that this is a controlled subset result.

Commit:

```bash
git add docs/dev/terminal-bench-memory-eval.md
git commit -m "docs: record task-local parametric memory smoke"
```

---

## Self-Review Checklist

- Spec coverage:
  - Task-local-first: Tasks 1-4, 9.
  - Existing `parametric_memory` contract reuse: Tasks 4-5.
  - Local Qwen model matrix and deployability: Task 10.
  - Smoke/stability gates: Tasks 8-10.
  - Negative prior evidence for finish-boundary mixing: preserved by solve-action-only record builder in Task 3.
- No artifact schema changes.
- No gateway/store/scheduler method logic changes.
- No subscription-mode parametric-memory enablement.
- Implementation starts with failing tests before production code.
- Real experiment is gated behind dry-run dataset inspection and fresh GPU/process checks.
