# Local Success Replay Parametric Memory Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add an opt-in Terminal Bench local-success replay dataset path that turns successful local Qwen Harbor `llm_calls.jsonl` rows into `parametric_memory_lora_sft` jobs.

**Architecture:** Keep the existing `parametric_memory` artifact boundary and LoRA worker contract. Add a small builder beside the existing task-local corrective builder, reuse the existing Terminal Bench tool schema and live-message compaction helpers, and expose the path through a separate CLI subcommand so it cannot change existing corrective-data behavior by default.

**Tech Stack:** Python 3.11, argparse, Pydantic worker job models, JSONL datasets, pytest focused tests, Terminal Bench Harbor local Qwen/vLLM evaluation.

---

## File Structure

- Modify `src/polar_evolution/terminal_bench_task_local_parametric.py`
  - Add `LocalSuccessReplayTrial`.
  - Add `build_local_success_replay_sft_records`.
  - Add `build_local_success_replay_parametric_job_payload`.
  - Add private helpers for replay row selection, source metadata, token usage extraction, and substring filtering.
  - Extend `build_task_local_parametric_job_payload` with backward-compatible optional manifest and lineage parameters.
- Modify `src/polar_evolution/cli.py`
  - Import the new builder API.
  - Add `terminal-bench-local-success-replay-parametric-memory-job`.
  - Add `_create_terminal_bench_local_success_replay_parametric_memory_job`.
  - Route the new subcommand in `main`.
- Modify `tests/evolution/test_terminal_bench_task_local_parametric.py`
  - Add focused tests for replay record building, filtering, payload writing, and CLI wiring.
  - Keep existing task-local corrective tests unchanged.
- Modify `docs/dev/terminal-bench-memory-eval.md`
  - Document the local-success replay path, its memory-only eval constraint, and the first v2b command shape.

---

### Task 1: Failing Builder Tests

**Files:**
- Modify: `tests/evolution/test_terminal_bench_task_local_parametric.py`
- Test: `tests/evolution/test_terminal_bench_task_local_parametric.py`

- [ ] **Step 1: Add imports for the new replay API**

Change the import block from `polar_evolution.terminal_bench_task_local_parametric` so it includes these names:

```python
from polar_evolution.terminal_bench_task_local_parametric import (
    LocalSuccessReplayTrial,
    TaskLocalSelection,
    TrajectoryPoolRow,
    build_local_success_replay_parametric_job_payload,
    build_local_success_replay_sft_records,
    build_task_local_parametric_job_payload,
    build_task_local_sft_records,
    extract_successful_codex_commands,
    select_task_local_candidates,
)
```

- [ ] **Step 2: Add a fixture helper for minimal local Qwen Harbor calls**

Append this helper near the existing test helpers:

```python
def _write_local_success_llm_calls(
    trial_dir: Path,
    rows: list[dict],
) -> Path:
    calls_path = (
        trial_dir
        / "agent"
        / "evolab_lab"
        / ".evolab"
        / "registries"
        / "trajectory"
        / "llm_calls.jsonl"
    )
    calls_path.parent.mkdir(parents=True)
    calls_path.write_text(
        "".join(json.dumps(row, sort_keys=True) + "\n" for row in rows),
        encoding="utf-8",
    )
    return calls_path
```

- [ ] **Step 3: Add the positive replay-record test**

Append this test after `test_build_task_local_parametric_job_payload_writes_dataset_and_lora_job`:

```python
def test_build_local_success_replay_sft_records_exports_tool_call_trace(
    tmp_path: Path,
) -> None:
    trial_dir = tmp_path / "train-fasttext__success"
    assistant_call = {
        "role": "assistant",
        "content": "",
        "tool_calls": [
            {
                "id": "call-exec",
                "type": "function",
                "function": {
                    "name": "tb_exec",
                    "arguments": {
                        "task_id": "terminal-bench-task",
                        "command": "python - <<'PY'\nfrom pathlib import Path\nPath('/app/model.bin').write_bytes(b'fasttext')\nPY",
                        "timeout_seconds": 300,
                    },
                },
            }
        ],
    }
    _write_local_success_llm_calls(
        trial_dir,
        [
            {
                "trajectory_id": "qwen-success-1",
                "model": "Qwen/Qwen3.5-9B",
                "usage": {"prompt_tokens": 123, "completion_tokens": 45},
                "input_messages": [
                    {"role": "system", "content": "Solve exactly one task_id."},
                    {"role": "user", "content": "Instruction:\n{}"},
                    {
                        "role": "tool",
                        "name": "tb_read_task",
                        "tool_call_id": "call-read",
                        "content": "short fallback",
                        "metadata": {
                            "tool_result": {
                                "content": "{\"tool\":\"tb_read_task\",\"task_id\":\"terminal-bench-task\"}",
                                "status": "ok",
                            }
                        },
                    },
                ],
                "output_messages": [assistant_call],
            }
        ],
    )

    [record] = build_local_success_replay_sft_records(
        [
            LocalSuccessReplayTrial(
                task_id="train-fasttext",
                trial_dir=trial_dir,
                trajectory_id="trial-success",
            )
        ]
    )

    trace = record["traces"][0]
    assert trace["prompt_messages"][2]["content"] == (
        "{\"tool\":\"tb_read_task\",\"task_id\":\"terminal-bench-task\"}"
    )
    assert trace["response_messages"] == [assistant_call]
    assert [tool["function"]["name"] for tool in trace["tools"]] == [
        "tb_read_task",
        "tb_exec",
        "tb_run_tests",
        "tb_collect_result",
    ]
    assert record["task_id"] == "train-fasttext"
    assert record["reward"] == 1.0
    assert record["metadata"]["builder"] == "terminal_bench_local_success_replay"
    assert record["metadata"]["source_trial_dir"] == str(trial_dir)
    assert record["metadata"]["source_trajectory_id"] == "qwen-success-1"
    assert record["metadata"]["source_llm_call_index"] == 1
    assert record["metadata"]["output_tool_names"] == ["tb_exec"]
    assert record["metadata"]["source_model"] == "Qwen/Qwen3.5-9B"
    assert record["metadata"]["prompt_tokens"] == 123
    assert record["metadata"]["completion_tokens"] == 45
    assert record["metadata"]["selection_filters"]["allowed_tools"] == [
        "tb_read_task",
        "tb_exec",
        "tb_run_tests",
    ]
```

- [ ] **Step 4: Add rejection and filter tests**

Append these tests after the positive replay-record test:

```python
def test_build_local_success_replay_sft_records_rejects_no_tool_outputs(
    tmp_path: Path,
) -> None:
    trial_dir = tmp_path / "train-fasttext__success"
    _write_local_success_llm_calls(
        trial_dir,
        [
            {
                "model": "Qwen/Qwen3.5-9B",
                "input_messages": [{"role": "user", "content": "Instruction:\n{}"}],
                "output_messages": [
                    {"role": "assistant", "content": "The task is complete."}
                ],
            }
        ],
    )

    records = build_local_success_replay_sft_records(
        [LocalSuccessReplayTrial(task_id="train-fasttext", trial_dir=trial_dir)]
    )

    assert records == []


def test_build_local_success_replay_sft_records_applies_tool_and_substring_filters(
    tmp_path: Path,
) -> None:
    trial_dir = tmp_path / "train-fasttext__success"
    _write_local_success_llm_calls(
        trial_dir,
        [
            {
                "model": "Qwen/Qwen3.5-9B",
                "input_messages": [
                    {"role": "user", "content": "Instruction:\n{} noisy probe"}
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
                                    "arguments": {"task_id": "terminal-bench-task"},
                                },
                            }
                        ],
                    }
                ],
            },
            {
                "model": "Qwen/Qwen3.5-9B",
                "input_messages": [
                    {"role": "user", "content": "Instruction:\n{} clean"}
                ],
                "output_messages": [
                    {
                        "role": "assistant",
                        "content": "",
                        "tool_calls": [
                            {
                                "id": "call-exec",
                                "type": "function",
                                "function": {
                                    "name": "tb_exec",
                                    "arguments": {
                                        "task_id": "terminal-bench-task",
                                        "command": "python - <<'PY'\nprint('ok')\nPY",
                                    },
                                },
                            }
                        ],
                    }
                ],
            },
        ],
    )

    [record] = build_local_success_replay_sft_records(
        [LocalSuccessReplayTrial(task_id="train-fasttext", trial_dir=trial_dir)],
        require_tool_name="tb_exec",
        exclude_if_input_contains=["noisy probe"],
        max_records=1,
        max_records_per_trial=1,
    )

    assert record["metadata"]["source_llm_call_index"] == 2
    assert record["metadata"]["output_tool_names"] == ["tb_exec"]
    assert record["metadata"]["selection_filters"]["require_tool_name"] == "tb_exec"
    assert record["metadata"]["selection_filters"]["exclude_if_input_contains"] == [
        "noisy probe"
    ]
```

- [ ] **Step 5: Run the new tests and confirm they fail for the expected reason**

Run:

```bash
pytest tests/evolution/test_terminal_bench_task_local_parametric.py::test_build_local_success_replay_sft_records_exports_tool_call_trace tests/evolution/test_terminal_bench_task_local_parametric.py::test_build_local_success_replay_sft_records_rejects_no_tool_outputs tests/evolution/test_terminal_bench_task_local_parametric.py::test_build_local_success_replay_sft_records_applies_tool_and_substring_filters -q
```

Expected: FAIL because `LocalSuccessReplayTrial` and `build_local_success_replay_sft_records` are not defined.

---

### Task 2: Replay Record Builder

**Files:**
- Modify: `src/polar_evolution/terminal_bench_task_local_parametric.py`
- Test: `tests/evolution/test_terminal_bench_task_local_parametric.py`

- [ ] **Step 1: Add the replay trial dataclass and default tool allow-list**

Add this near the existing dataclasses and constants:

```python
@dataclass(frozen=True)
class LocalSuccessReplayTrial:
    task_id: str
    trial_dir: Path
    trajectory_id: str | None = None


_LOCAL_SUCCESS_REPLAY_DEFAULT_ALLOWED_TOOLS = (
    "tb_read_task",
    "tb_exec",
    "tb_run_tests",
)
```

- [ ] **Step 2: Add the public replay builder**

Add this function after `build_task_local_sft_records` and before `_sequence_target_commands`:

```python
def build_local_success_replay_sft_records(
    trials: list[LocalSuccessReplayTrial],
    *,
    allowed_tools: list[str] | None = None,
    require_tool_name: str | None = None,
    exclude_if_input_contains: list[str] | None = None,
    exclude_if_output_contains: list[str] | None = None,
    max_records: int | None = None,
    max_records_per_trial: int | None = None,
) -> list[dict[str, Any]]:
    allowed = _normalize_allowed_success_replay_tools(allowed_tools)
    selection_filters = _local_success_replay_selection_filters(
        allowed_tools=allowed,
        require_tool_name=require_tool_name,
        exclude_if_input_contains=exclude_if_input_contains,
        exclude_if_output_contains=exclude_if_output_contains,
        max_records=max_records,
        max_records_per_trial=max_records_per_trial,
    )
    records: list[dict[str, Any]] = []
    for trial in trials:
        if max_records is not None and len(records) >= max_records:
            break
        trial_records = 0
        calls_path = _trial_evolab_llm_calls(trial.trial_dir)
        if not calls_path.is_file():
            continue
        for call_index, line in enumerate(
            calls_path.read_text(encoding="utf-8", errors="replace").splitlines(),
            start=1,
        ):
            if max_records is not None and len(records) >= max_records:
                break
            if (
                max_records_per_trial is not None
                and trial_records >= max_records_per_trial
            ):
                break
            if not line.strip():
                continue
            try:
                payload = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(payload, dict):
                continue
            record = _local_success_replay_record(
                trial=trial,
                payload=payload,
                call_index=call_index,
                allowed_tools=allowed,
                require_tool_name=require_tool_name,
                exclude_if_input_contains=exclude_if_input_contains,
                exclude_if_output_contains=exclude_if_output_contains,
                selection_filters=selection_filters,
            )
            if record is None:
                continue
            records.append(record)
            trial_records += 1
    return records
```

- [ ] **Step 3: Add replay selection helpers**

Add these helpers near `_llm_call_outputs_tool`:

```python
def _normalize_allowed_success_replay_tools(
    allowed_tools: list[str] | None,
) -> list[str]:
    values = [
        tool.strip()
        for tool in (allowed_tools or list(_LOCAL_SUCCESS_REPLAY_DEFAULT_ALLOWED_TOOLS))
        if isinstance(tool, str) and tool.strip()
    ]
    if not values:
        raise ValueError("local success replay allowed_tools must not be empty")
    return values


def _local_success_replay_selection_filters(
    *,
    allowed_tools: list[str],
    require_tool_name: str | None,
    exclude_if_input_contains: list[str] | None,
    exclude_if_output_contains: list[str] | None,
    max_records: int | None,
    max_records_per_trial: int | None,
) -> dict[str, Any]:
    filters: dict[str, Any] = {"allowed_tools": list(allowed_tools)}
    if require_tool_name:
        filters["require_tool_name"] = require_tool_name
    if exclude_if_input_contains:
        filters["exclude_if_input_contains"] = list(exclude_if_input_contains)
    if exclude_if_output_contains:
        filters["exclude_if_output_contains"] = list(exclude_if_output_contains)
    if max_records is not None:
        filters["max_records"] = max_records
    if max_records_per_trial is not None:
        filters["max_records_per_trial"] = max_records_per_trial
    return filters


def _local_success_replay_record(
    *,
    trial: LocalSuccessReplayTrial,
    payload: dict[str, Any],
    call_index: int,
    allowed_tools: list[str],
    require_tool_name: str | None,
    exclude_if_input_contains: list[str] | None,
    exclude_if_output_contains: list[str] | None,
    selection_filters: dict[str, Any],
) -> dict[str, Any] | None:
    prompt_messages = _compact_live_replay_messages(payload.get("input_messages"))
    if not prompt_messages:
        return None
    response_message = _local_success_replay_response_message(payload)
    if response_message is None:
        return None
    output_tool_names = sorted(_message_tool_call_names(response_message))
    if not output_tool_names:
        return None
    if not any(tool in allowed_tools for tool in output_tool_names):
        return None
    if require_tool_name and require_tool_name not in output_tool_names:
        return None
    response_messages = [response_message]
    if _messages_contain_any(prompt_messages, exclude_if_input_contains):
        return None
    if _messages_contain_any(response_messages, exclude_if_output_contains):
        return None
    metadata = _local_success_replay_metadata(
        trial=trial,
        payload=payload,
        call_index=call_index,
        output_tool_names=output_tool_names,
        selection_filters=selection_filters,
    )
    return {
        "event_id": (
            "local-success-replay:"
            f"{_safe_name(trial.task_id)}:"
            f"{_safe_name(trial.trial_dir.name)}:{call_index}"
        ),
        "task_id": trial.task_id,
        "session_id": f"local-success-replay:{_safe_name(trial.task_id)}",
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
```

- [ ] **Step 4: Add response, metadata, token usage, and substring helpers**

Add these helpers near the replay selection helpers:

```python
def _local_success_replay_response_message(
    payload: dict[str, Any],
) -> dict[str, Any] | None:
    messages = _compact_live_replay_messages(payload.get("output_messages"))
    for message in messages:
        if message.get("role") != "assistant":
            continue
        if _message_tool_call_names(message):
            return message
    return None


def _local_success_replay_metadata(
    *,
    trial: LocalSuccessReplayTrial,
    payload: dict[str, Any],
    call_index: int,
    output_tool_names: list[str],
    selection_filters: dict[str, Any],
) -> dict[str, Any]:
    metadata: dict[str, Any] = {
        "builder": "terminal_bench_local_success_replay",
        "source_trial_dir": str(trial.trial_dir),
        "source_llm_call_index": call_index,
        "output_tool_names": list(output_tool_names),
        "selection_filters": selection_filters,
    }
    source_trajectory_id = _local_success_replay_source_trajectory_id(
        trial,
        payload,
    )
    if source_trajectory_id is not None:
        metadata["source_trajectory_id"] = source_trajectory_id
    source_model = _local_success_replay_source_model(payload)
    if source_model is not None:
        metadata["source_model"] = source_model
    prompt_tokens, completion_tokens = _local_success_replay_token_usage(payload)
    if prompt_tokens is not None:
        metadata["prompt_tokens"] = prompt_tokens
    if completion_tokens is not None:
        metadata["completion_tokens"] = completion_tokens
    return metadata


def _local_success_replay_source_trajectory_id(
    trial: LocalSuccessReplayTrial,
    payload: dict[str, Any],
) -> str | None:
    for key in ("trajectory_id", "trace_id", "session_id"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    if trial.trajectory_id and trial.trajectory_id.strip():
        return trial.trajectory_id.strip()
    return None


def _local_success_replay_source_model(payload: dict[str, Any]) -> str | None:
    for key in ("model", "model_name", "served_model_name"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _local_success_replay_token_usage(
    payload: dict[str, Any],
) -> tuple[int | None, int | None]:
    usage = payload.get("usage")
    if not isinstance(usage, dict):
        return None, None
    prompt_tokens = _int_usage_value(usage, "prompt_tokens", "input_tokens")
    completion_tokens = _int_usage_value(
        usage,
        "completion_tokens",
        "output_tokens",
    )
    return prompt_tokens, completion_tokens


def _int_usage_value(usage: dict[str, Any], *keys: str) -> int | None:
    for key in keys:
        value = usage.get(key)
        if isinstance(value, bool):
            continue
        if isinstance(value, int):
            return value
    return None


def _messages_contain_any(
    messages: list[dict[str, Any]],
    needles: list[str] | None,
) -> bool:
    active_needles = [needle for needle in needles or [] if needle]
    if not active_needles:
        return False
    haystack = json.dumps(messages, ensure_ascii=False, sort_keys=True)
    return any(needle in haystack for needle in active_needles)
```

- [ ] **Step 5: Run builder tests**

Run:

```bash
pytest tests/evolution/test_terminal_bench_task_local_parametric.py::test_build_local_success_replay_sft_records_exports_tool_call_trace tests/evolution/test_terminal_bench_task_local_parametric.py::test_build_local_success_replay_sft_records_rejects_no_tool_outputs tests/evolution/test_terminal_bench_task_local_parametric.py::test_build_local_success_replay_sft_records_applies_tool_and_substring_filters -q
```

Expected: PASS.

- [ ] **Step 6: Commit builder changes**

Run:

```bash
git status --short
git add src/polar_evolution/terminal_bench_task_local_parametric.py tests/evolution/test_terminal_bench_task_local_parametric.py
git commit -m "feat: add local success replay records"
```

---

### Task 3: Payload And Manifest Support

**Files:**
- Modify: `src/polar_evolution/terminal_bench_task_local_parametric.py`
- Modify: `tests/evolution/test_terminal_bench_task_local_parametric.py`
- Test: `tests/evolution/test_terminal_bench_task_local_parametric.py`

- [ ] **Step 1: Add a failing payload test**

Append this test after the replay builder tests:

```python
def test_build_local_success_replay_parametric_job_payload_writes_replay_manifest(
    tmp_path: Path,
) -> None:
    record = {
        "event_id": "local-success-replay:train-fasttext:success:1",
        "task_id": "train-fasttext",
        "session_id": "local-success-replay:train-fasttext",
        "status": "COMPLETED",
        "reward": 1.0,
        "traces": [
            {
                "prompt_messages": [{"role": "user", "content": "Instruction:\n{}"}],
                "response_messages": [
                    {
                        "role": "assistant",
                        "content": "",
                        "tool_calls": [
                            {
                                "id": "call-exec",
                                "type": "function",
                                "function": {
                                    "name": "tb_exec",
                                    "arguments": {
                                        "task_id": "terminal-bench-task",
                                        "command": "python - <<'PY'\nprint('ok')\nPY",
                                    },
                                },
                            }
                        ],
                    }
                ],
                "tools": [],
            }
        ],
        "metadata": {
            "builder": "terminal_bench_local_success_replay",
            "source_trial_dir": str(tmp_path / "trial"),
            "source_model": "Qwen/Qwen3.5-9B",
            "selection_filters": {"allowed_tools": ["tb_exec"]},
        },
    }

    payload = build_local_success_replay_parametric_job_payload(
        records=[record],
        output_root=tmp_path / "out",
        dataset_name="tb21-local-success-replay-train-fasttext",
        base_model="Qwen/Qwen3.5-9B",
        adapter_id="tb-parametric-memory-train-fasttext-replay",
        trainer_command="python",
        trainer_args=[
            "train_lora.py",
            "--train-file",
            "{training_dataset}",
            "--output-dir",
            "{adapter_dir}",
        ],
        task_ids=["train-fasttext"],
        source_trial_dirs=[tmp_path / "trial"],
        selection_filters={"allowed_tools": ["tb_exec"]},
        source_models=["Qwen/Qwen3.5-9B"],
    )

    manifest = json.loads(Path(payload["dataset"]["manifest_path"]).read_text())
    assert manifest["builder"] == "terminal_bench_local_success_replay"
    assert manifest["purpose"] == "local-success-replay-parametric-memory"
    assert manifest["source_trial_dirs"] == [str(tmp_path / "trial")]
    assert manifest["source_models"] == ["Qwen/Qwen3.5-9B"]
    assert manifest["target_filters"] == {"allowed_tools": ["tb_exec"]}
    assert payload["job"]["config"]["lineage"]["method"] == (
        "terminal_bench_local_success_replay"
    )
    assert payload["job"]["config"]["training_projection"] == {"type": "full_trace"}
    assert payload["job"]["config"]["compatibility"]["base_model"] == [
        "Qwen/Qwen3.5-9B"
    ]
```

- [ ] **Step 2: Run the payload test and confirm it fails for the expected reason**

Run:

```bash
pytest tests/evolution/test_terminal_bench_task_local_parametric.py::test_build_local_success_replay_parametric_job_payload_writes_replay_manifest -q
```

Expected: FAIL because `build_local_success_replay_parametric_job_payload` is not defined.

- [ ] **Step 3: Extend the existing payload writer without changing default behavior**

Change the signature of `build_task_local_parametric_job_payload` to:

```python
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
    purpose: str = "task-local-parametric-memory",
    builder: str = "terminal_bench_task_local_parametric",
    manifest_extra: dict[str, Any] | None = None,
    lineage_method: str | None = None,
) -> dict[str, Any]:
```

Change the manifest and lineage blocks inside the function to:

```python
    manifest = {
        "name": dataset_name,
        "purpose": purpose,
        "records_path": "records.jsonl",
        "record_count": len(records),
        "task_ids": list(task_ids),
        "builder": builder,
    }
    if target_filters is not None:
        manifest["target_filters"] = target_filters
    if manifest_extra is not None:
        manifest.update(manifest_extra)
```

and:

```python
            "lineage": {
                "method": lineage_method or builder,
                "source_task_ids": list(task_ids),
                "dataset_manifest_uri": manifest_path.resolve().as_uri(),
            },
```

- [ ] **Step 4: Add the replay payload wrapper**

Add this function after `build_task_local_parametric_job_payload`:

```python
def build_local_success_replay_parametric_job_payload(
    *,
    records: list[dict[str, Any]],
    output_root: Path,
    dataset_name: str,
    base_model: str,
    adapter_id: str,
    trainer_command: str,
    trainer_args: list[str],
    task_ids: list[str],
    source_trial_dirs: list[Path],
    selection_filters: dict[str, Any],
    source_models: list[str] | None = None,
    trainer_timeout_seconds: float = 3600.0,
) -> dict[str, Any]:
    manifest_extra: dict[str, Any] = {
        "source_trial_dirs": [str(path) for path in source_trial_dirs],
    }
    if source_models:
        manifest_extra["source_models"] = list(source_models)
    return build_task_local_parametric_job_payload(
        records=records,
        output_root=output_root,
        dataset_name=dataset_name,
        base_model=base_model,
        adapter_id=adapter_id,
        trainer_command=trainer_command,
        trainer_args=trainer_args,
        task_ids=task_ids,
        trainer_timeout_seconds=trainer_timeout_seconds,
        target_filters=selection_filters,
        purpose="local-success-replay-parametric-memory",
        builder="terminal_bench_local_success_replay",
        manifest_extra=manifest_extra,
        lineage_method="terminal_bench_local_success_replay",
    )
```

- [ ] **Step 5: Run payload and regression tests**

Run:

```bash
pytest tests/evolution/test_terminal_bench_task_local_parametric.py::test_build_local_success_replay_parametric_job_payload_writes_replay_manifest tests/evolution/test_terminal_bench_task_local_parametric.py::test_build_task_local_parametric_job_payload_writes_dataset_and_lora_job -q
```

Expected: PASS. The existing task-local payload test must still see `builder == "terminal_bench_task_local_parametric"` in the manifest if it checks that field later.

- [ ] **Step 6: Commit payload changes**

Run:

```bash
git status --short
git add src/polar_evolution/terminal_bench_task_local_parametric.py tests/evolution/test_terminal_bench_task_local_parametric.py
git commit -m "feat: write local success replay lora jobs"
```

---

### Task 4: CLI Subcommand

**Files:**
- Modify: `src/polar_evolution/cli.py`
- Modify: `tests/evolution/test_terminal_bench_task_local_parametric.py`
- Test: `tests/evolution/test_terminal_bench_task_local_parametric.py`

- [ ] **Step 1: Add a failing CLI test**

Append this test near the existing `terminal-bench-task-local-parametric-memory-job` CLI tests:

```python
def test_terminal_bench_local_success_replay_parametric_memory_job_cli_writes_payload(
    tmp_path: Path,
) -> None:
    trial_dir = tmp_path / "train-fasttext__success"
    _write_local_success_llm_calls(
        trial_dir,
        [
            {
                "trajectory_id": "qwen-success-1",
                "model": "Qwen/Qwen3.5-9B",
                "usage": {"prompt_tokens": 12, "completion_tokens": 8},
                "input_messages": [{"role": "user", "content": "Instruction:\n{}"}],
                "output_messages": [
                    {
                        "role": "assistant",
                        "content": "",
                        "tool_calls": [
                            {
                                "id": "call-exec",
                                "type": "function",
                                "function": {
                                    "name": "tb_exec",
                                    "arguments": {
                                        "task_id": "terminal-bench-task",
                                        "command": "python - <<'PY'\nprint('ok')\nPY",
                                    },
                                },
                            }
                        ],
                    }
                ],
            }
        ],
    )
    output = tmp_path / "job.json"

    assert (
        main(
            [
                "terminal-bench-local-success-replay-parametric-memory-job",
                "--success-trial-dir",
                str(trial_dir),
                "--task-id",
                "train-fasttext",
                "--output-root",
                str(tmp_path / "out"),
                "--dataset-name",
                "tb21-local-success-replay-train-fasttext",
                "--base-model",
                "Qwen/Qwen3.5-9B",
                "--adapter-id",
                "tb-parametric-memory-train-fasttext-replay",
                "--trainer-command",
                "/root/evolab-vllm/bin/python",
                "--trainer-arg",
                "/root/.config/superpowers/worktrees/ProRL-Agent-Server/openevo-memory-backends/scripts/qwen_lora_sft.py",
                "--trainer-arg",
                "--train-file",
                "--trainer-arg",
                "{training_dataset}",
                "--trainer-arg",
                "--output-dir",
                "--trainer-arg",
                "{adapter_dir}",
                "--require-tool-name",
                "tb_exec",
                "--max-records",
                "1",
                "--output",
                str(output),
            ]
        )
        == 0
    )

    payload = json.loads(output.read_text(encoding="utf-8"))
    assert payload["selected_tasks"] == ["train-fasttext"]
    assert payload["dataset"]["record_count"] == 1
    assert payload["source_trial_dirs"] == [str(trial_dir)]
    assert payload["selection_filters"]["require_tool_name"] == "tb_exec"
    assert payload["job"]["method"] == "parametric_memory_lora_sft"
    record = json.loads(Path(payload["dataset"]["records_path"]).read_text())
    assert record["metadata"]["builder"] == "terminal_bench_local_success_replay"
    assert record["metadata"]["output_tool_names"] == ["tb_exec"]
```

- [ ] **Step 2: Run the CLI test and confirm it fails for the expected reason**

Run:

```bash
pytest tests/evolution/test_terminal_bench_task_local_parametric.py::test_terminal_bench_local_success_replay_parametric_memory_job_cli_writes_payload -q
```

Expected: FAIL because the parser does not know `terminal-bench-local-success-replay-parametric-memory-job`.

- [ ] **Step 3: Import the replay API in CLI**

Change the import block in `src/polar_evolution/cli.py` to include:

```python
from polar_evolution.terminal_bench_task_local_parametric import (
    LocalSuccessReplayTrial,
    build_local_success_replay_parametric_job_payload,
    build_local_success_replay_sft_records,
)
```

Keep the existing imported task-local names in the same block.

- [ ] **Step 4: Add the parser subcommand**

Add this parser immediately after `tb_task_local_parametric_job`:

```python
    tb_local_success_replay_job = subparsers.add_parser(
        "terminal-bench-local-success-replay-parametric-memory-job",
        help=(
            "Build Terminal Bench parametric-memory LoRA job payloads from "
            "successful local Harbor llm_calls.jsonl trajectories."
        ),
    )
    tb_local_success_replay_job.add_argument(
        "--success-trial-dir",
        action="append",
        default=[],
        required=True,
    )
    tb_local_success_replay_job.add_argument("--task-id", action="append", default=[], required=True)
    tb_local_success_replay_job.add_argument("--output-root", required=True)
    tb_local_success_replay_job.add_argument("--dataset-name")
    tb_local_success_replay_job.add_argument("--base-model", default=DEFAULT_LOCAL_MODEL)
    tb_local_success_replay_job.add_argument(
        "--adapter-id",
        default=DEFAULT_LOCAL_PARAMETRIC_ADAPTER_ID,
    )
    tb_local_success_replay_job.add_argument("--trainer-command", required=True)
    tb_local_success_replay_job.add_argument("--trainer-arg", action="append", default=[])
    tb_local_success_replay_job.add_argument(
        "--trainer-timeout-seconds",
        type=float,
        default=3600.0,
    )
    tb_local_success_replay_job.add_argument(
        "--allowed-tool",
        action="append",
        default=[],
        help=(
            "Allowed output tool name. Defaults to tb_read_task, tb_exec, and "
            "tb_run_tests when omitted. Can be repeated."
        ),
    )
    tb_local_success_replay_job.add_argument("--require-tool-name")
    tb_local_success_replay_job.add_argument(
        "--exclude-if-input-contains",
        action="append",
        default=[],
    )
    tb_local_success_replay_job.add_argument(
        "--exclude-if-output-contains",
        action="append",
        default=[],
    )
    tb_local_success_replay_job.add_argument("--max-records", type=int)
    tb_local_success_replay_job.add_argument("--max-records-per-trial", type=int)
    tb_local_success_replay_job.add_argument(
        "--artifact-root",
        help="Artifact root used only when --run-worker is set.",
    )
    tb_local_success_replay_job.add_argument("--run-worker", action="store_true")
    tb_local_success_replay_job.add_argument("--output", help="Output JSON path. Defaults to stdout.")
```

- [ ] **Step 5: Route the new command in `main`**

Add this block next to the existing task-local route:

```python
    if args.command == "terminal-bench-local-success-replay-parametric-memory-job":
        payload = _create_terminal_bench_local_success_replay_parametric_memory_job(
            args
        )
        _write_json_output(payload, args.output)
        return 0
```

- [ ] **Step 6: Add trial mapping and handler helpers**

Add these functions near `_create_terminal_bench_task_local_parametric_memory_job`:

```python
def _local_success_replay_trials_from_args(
    args: argparse.Namespace,
) -> list[LocalSuccessReplayTrial]:
    trial_dirs = [Path(path) for path in args.success_trial_dir]
    task_ids = list(args.task_id)
    if len(task_ids) == 1 and len(trial_dirs) > 1:
        task_ids = task_ids * len(trial_dirs)
    if len(task_ids) != len(trial_dirs):
        raise ValueError(
            "terminal-bench-local-success-replay-parametric-memory-job requires "
            "one --task-id for all success trials or one --task-id per "
            "--success-trial-dir"
        )
    return [
        LocalSuccessReplayTrial(task_id=task_id, trial_dir=trial_dir)
        for task_id, trial_dir in zip(task_ids, trial_dirs, strict=True)
    ]


def _create_terminal_bench_local_success_replay_parametric_memory_job(
    args: argparse.Namespace,
) -> dict[str, Any]:
    trials = _local_success_replay_trials_from_args(args)
    allowed_tools = list(args.allowed_tool) or None
    records = build_local_success_replay_sft_records(
        trials,
        allowed_tools=allowed_tools,
        require_tool_name=args.require_tool_name,
        exclude_if_input_contains=list(args.exclude_if_input_contains),
        exclude_if_output_contains=list(args.exclude_if_output_contains),
        max_records=args.max_records,
        max_records_per_trial=args.max_records_per_trial,
    )
    if not records:
        raise ValueError(
            "terminal-bench-local-success-replay-parametric-memory-job found no "
            "usable local success replay records"
        )
    selected_task_ids = sorted({trial.task_id for trial in trials})
    selection_filters = records[0]["metadata"]["selection_filters"]
    source_models = sorted(
        {
            record["metadata"]["source_model"]
            for record in records
            if isinstance(record.get("metadata"), dict)
            and isinstance(record["metadata"].get("source_model"), str)
        }
    )
    task_suffix = "-".join(selected_task_ids)
    dataset_name = args.dataset_name or f"tb21-local-success-replay-{task_suffix}"
    payload = build_local_success_replay_parametric_job_payload(
        records=records,
        output_root=Path(args.output_root),
        dataset_name=dataset_name,
        base_model=args.base_model,
        adapter_id=args.adapter_id,
        trainer_command=args.trainer_command,
        trainer_args=list(args.trainer_arg),
        trainer_timeout_seconds=args.trainer_timeout_seconds,
        task_ids=selected_task_ids,
        source_trial_dirs=[trial.trial_dir for trial in trials],
        selection_filters=selection_filters,
        source_models=source_models,
    )
    payload["source_trial_dirs"] = [str(trial.trial_dir) for trial in trials]
    payload["selected_tasks"] = selected_task_ids
    payload["selection_filters"] = selection_filters
    payload["source_models"] = source_models
    if args.run_worker:
        output_root = Path(args.output_root)
        artifact_root = (
            Path(args.artifact_root) if args.artifact_root else output_root / "artifacts"
        )
        artifacts = run_method(
            WorkerClaimedJob.model_validate(payload["job"]),
            artifact_root=artifact_root,
        )
        completed_artifacts = [
            artifact.model_dump(mode="json") for artifact in artifacts
        ]
        payload["completed_artifacts"] = completed_artifacts
        completed_path = output_root / "completed_artifacts.json"
        completed_path.write_text(
            json.dumps(completed_artifacts, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        payload["completed_artifacts_path"] = str(completed_path)
    return payload
```

- [ ] **Step 7: Run CLI and adjacent regression tests**

Run:

```bash
pytest tests/evolution/test_terminal_bench_task_local_parametric.py::test_terminal_bench_local_success_replay_parametric_memory_job_cli_writes_payload tests/evolution/test_terminal_bench_task_local_parametric.py::test_terminal_bench_task_local_parametric_memory_job_cli_writes_payload -q
```

Expected: PASS.

- [ ] **Step 8: Commit CLI changes**

Run:

```bash
git status --short
git add src/polar_evolution/cli.py tests/evolution/test_terminal_bench_task_local_parametric.py
git commit -m "feat: add local success replay cli"
```

---

### Task 5: Documentation

**Files:**
- Modify: `docs/dev/terminal-bench-memory-eval.md`

- [ ] **Step 1: Add the local-success replay documentation section**

Append this section to `docs/dev/terminal-bench-memory-eval.md`:

```markdown
## Local-Success Replay Parametric Memory

`terminal-bench-local-success-replay-parametric-memory-job` builds a
`parametric_memory_lora_sft` job from successful local Harbor
`llm_calls.jsonl` rows. This path is opt-in and does not replace the
failed-prefix task-local corrective builder.

Use this path only with local/proxy inference. Textual memory works across
capture modes, but `parametric_memory` evaluation must use a local serving
backend that can load the LoRA adapter. Subscription harnesses are not valid for
this eval path.

The first controlled v2b run uses the successful local Qwen train-fasttext trial:

```bash
python -m polar_evolution.cli terminal-bench-local-success-replay-parametric-memory-job \
  --success-trial-dir /tmp/tb21-task-local-parametric-trainfasttext-oneshot-20260709/eval-oneshot-r8-s260-pass2-cacheenv/parametric_memory/harbor_jobs/parametric_memory-train-fasttext/train-fasttext__7GGAsRu \
  --task-id train-fasttext \
  --output-root /tmp/tb21-local-success-replay-trainfasttext-20260709/train-replay-r8-s260 \
  --dataset-name tb21-local-success-replay-train-fasttext \
  --base-model Qwen/Qwen3.5-9B \
  --adapter-id tb-parametric-memory-train-fasttext-replay-r8-s260 \
  --trainer-command /root/evolab-vllm/bin/python \
  --trainer-arg /root/.config/superpowers/worktrees/ProRL-Agent-Server/openevo-memory-backends/scripts/qwen_lora_sft.py \
  --trainer-arg --train-file \
  --trainer-arg '{training_dataset}' \
  --trainer-arg --output-dir \
  --trainer-arg '{adapter_dir}' \
  --trainer-arg --model-name \
  --trainer-arg Qwen/Qwen3.5-9B \
  --trainer-arg --max-steps \
  --trainer-arg 260 \
  --trainer-arg --lora-r \
  --trainer-arg 8 \
  --trainer-arg --lora-alpha \
  --trainer-arg 16 \
  --trainer-arg --max-length \
  --trainer-arg 4096 \
  --require-tool-name tb_exec \
  --max-records 16 \
  --max-records-per-trial 16 \
  --output /tmp/tb21-local-success-replay-trainfasttext-20260709/train-replay-r8-s260/job.json
```

During paired eval, enable only `parametric_memory`; keep `text_memory`,
`skill_bundle`, and `agent_system` disabled. Set
`EVOLAB_TB_UV_CACHE_TARBALL` and `EVOLAB_TB_UV_PYTHON_TARBALL`, and do not pass
`--verifier-python-install-mirror` for this host's cached verifier path.
```

- [ ] **Step 2: Run docs diff check**

Run:

```bash
git diff --check
```

Expected: no output.

- [ ] **Step 3: Commit documentation**

Run:

```bash
git status --short
git add docs/dev/terminal-bench-memory-eval.md
git commit -m "docs: document local success replay eval"
```

---

### Task 6: Focused Verification And PR Update

**Files:**
- No new file edits unless verification exposes a defect.

- [ ] **Step 1: Run the full focused test file**

Run:

```bash
pytest tests/evolution/test_terminal_bench_task_local_parametric.py -q
```

Expected: PASS.

- [ ] **Step 2: Run adjacent evolution and gateway checks affected by artifact contracts**

Run:

```bash
pytest tests/evolution/test_datasets_jobs.py tests/gateway/test_evolution_integration.py -q
```

Expected: PASS.

- [ ] **Step 3: Run patch whitespace check**

Run:

```bash
git diff --check
```

Expected: no output.

- [ ] **Step 4: Review changed files**

Run:

```bash
git status --short
git diff --stat
git diff
```

Expected: changes are limited to the replay builder, CLI, focused tests, and docs.

- [ ] **Step 5: Push branch and update PR #33**

Run:

```bash
git push https://github.com/CompLifeLab-ZJU/OpenEvo.git codex/parametric-memory-local-eval
```

Then update PR #33 body so it mentions:

- local-success replay builder and CLI path;
- issue link `Fixes #32`;
- docs path `docs/dev/terminal-bench-memory-eval.md`;
- tests from Steps 1-3;
- no aggregate Terminal Bench 2.1 claim yet.

---

### Task 7: Real GPU7 V2b Experiment

**Files:**
- Modify: `docs/dev/terminal-bench-memory-eval.md` after the experiment completes.

- [ ] **Step 1: Confirm GPU7 is free or only contains user-approved disposable processes**

Run:

```bash
nvidia-smi
nvidia-smi --query-compute-apps=pid,gpu_uuid,used_memory,process_name --format=csv,noheader,nounits
```

Expected: GPU7 has no compute app. If a GPU7 process is present, inspect it with `ps -fp <pid>` before killing it.

- [ ] **Step 2: Build the replay job payload from the successful v2 trial**

Use the committed CLI subcommand with the exact successful local Qwen trial path:

```bash
python -m polar_evolution.cli terminal-bench-local-success-replay-parametric-memory-job \
  --success-trial-dir /tmp/tb21-task-local-parametric-trainfasttext-oneshot-20260709/eval-oneshot-r8-s260-pass2-cacheenv/parametric_memory/harbor_jobs/parametric_memory-train-fasttext/train-fasttext__7GGAsRu \
  --task-id train-fasttext \
  --output-root /tmp/tb21-local-success-replay-trainfasttext-20260709/train-replay-r8-s260 \
  --dataset-name tb21-local-success-replay-train-fasttext \
  --base-model Qwen/Qwen3.5-9B \
  --adapter-id tb-parametric-memory-train-fasttext-replay-r8-s260 \
  --trainer-command /root/evolab-vllm/bin/python \
  --trainer-arg /root/.config/superpowers/worktrees/ProRL-Agent-Server/openevo-memory-backends/scripts/qwen_lora_sft.py \
  --trainer-arg --train-file \
  --trainer-arg '{training_dataset}' \
  --trainer-arg --output-dir \
  --trainer-arg '{adapter_dir}' \
  --trainer-arg --model-name \
  --trainer-arg Qwen/Qwen3.5-9B \
  --trainer-arg --max-steps \
  --trainer-arg 260 \
  --trainer-arg --lora-r \
  --trainer-arg 8 \
  --trainer-arg --lora-alpha \
  --trainer-arg 16 \
  --trainer-arg --max-length \
  --trainer-arg 4096 \
  --require-tool-name tb_exec \
  --max-records 16 \
  --max-records-per-trial 16 \
  --output /tmp/tb21-local-success-replay-trainfasttext-20260709/train-replay-r8-s260/job.json
```

Expected: job JSON exists and dataset manifest reports builder `terminal_bench_local_success_replay`.

- [ ] **Step 3: Run the LoRA worker/trainer on GPU7**

Use the same trainer command and environment from the successful v2 one-shot run, replacing only the dataset and adapter id from Step 2. Set:

```bash
CUDA_VISIBLE_DEVICES=7
```

Expected: adapter directory is created under:

```text
/tmp/tb21-local-success-replay-trainfasttext-20260709/train-replay-r8-s260/artifacts/workers/job-tb-parametric-memory-train-fasttext-replay-r8-s260/parametric_memory_lora_sft/adapter
```

- [ ] **Step 4: Run paired local Terminal Bench eval**

Run the same local eval harness used for the v2 one-shot experiment with:

```bash
EVOLAB_TB_UV_CACHE_TARBALL=/root/.cache/evolab-terminal-bench/uv-x86_64-unknown-linux-gnu.tar.gz
EVOLAB_TB_UV_PYTHON_TARBALL=/root/.cache/evolab-terminal-bench/uv-managed-python-3.13.13.tar.gz
```

Use:

```text
baseline: Qwen/Qwen3.5-9B without adapter
treatment: Qwen/Qwen3.5-9B with tb-parametric-memory-train-fasttext-replay-r8-s260
enabled artifacts: parametric_memory only
disabled artifacts: text_memory, skill_bundle, agent_system
```

Expected: paired eval completes without Harbor exceptions and reports baseline/treatment pass counts.

- [ ] **Step 5: Record experiment evidence**

Append the result to `docs/dev/terminal-bench-memory-eval.md` with:

- run root;
- adapter directory;
- model and GPU;
- enabled and disabled artifact types;
- pass@1/pass@k baseline and treatment;
- whether adapter keys were rewritten;
- first failure mode if treatment does not pass.

- [ ] **Step 6: Commit experiment evidence**

Run:

```bash
git status --short
git add docs/dev/terminal-bench-memory-eval.md
git commit -m "docs: record local success replay experiment"
git push https://github.com/CompLifeLab-ZJU/OpenEvo.git codex/parametric-memory-local-eval
```

---

## Self-Review Checklist

- Spec coverage: Tasks 1-4 implement opt-in replay records, artifact compatibility, CLI flags, filters, and metadata. Task 5 documents the contract. Task 7 runs the controlled local-only v2b experiment on GPU7.
- Existing behavior: The old `terminal-bench-task-local-parametric-memory-job` path remains default and has adjacent regression tests in Tasks 3, 4, and 6.
- Artifact contract: The worker method remains `parametric_memory_lora_sft` and `training_projection` remains `{"type": "full_trace"}`.
- Memory-only control: The real experiment explicitly enables only `parametric_memory`.
- Placeholder scan: The plan contains concrete function names, file paths, commands, expected results, and commit messages.
