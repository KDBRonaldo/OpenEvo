# Parametric Memory Continued LoRA V4 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Run a v4 parametric-memory experiment that starts from the best v2 LoRA adapter, preserves its fast-command behavior, and tests whether a small finish-boundary update can add validation/collect/stop behavior.

**Architecture:** Keep OpenEvo's `parametric_memory_lora_sft` worker contract unchanged and use a temporary external trainer that can resume from an existing PEFT adapter. The training dataset will replay v2 fast-command records heavily and add only a small number of synthetic finish-boundary records so the update is a constrained adapter continuation rather than a fresh mixed-policy train.

**Tech Stack:** Python, PyTorch, Transformers, PEFT, vLLM, Qwen/Qwen3.6-35B-A3B, Terminal-Bench 2.1 Harbor/EvoLab direct-solver runner.

---

### Task 1: Write A Resume-Capable Temporary Trainer

**Files:**
- Create: `/tmp/qwen36_lora_sft_resume.py`
- Read-only reference: `/tmp/qwen36_lora_sft.py`

- [ ] **Step 1: Copy the current trainer structure**

Use `/tmp/qwen36_lora_sft.py` as the reference implementation. Keep its existing chat-template rendering, label masking, Qwen tokenizer settings, optimizer loop, and diagnostics output.

- [ ] **Step 2: Add `--init-adapter-path`**

Add one optional argument:

```python
parser.add_argument("--init-adapter-path")
```

- [ ] **Step 3: Load an existing PEFT adapter when supplied**

Do not use `PeftModel.from_pretrained` for this temporary trainer. In the local
PEFT/Transformers environment that path can enter a weight-conversion code path
that is incompatible with the installed `WeightConverter`. Instead, construct
the trainable LoRA modules with `get_peft_model(...)`, then directly load the
saved safetensors adapter weights:

```python
from safetensors.torch import load_file as load_safetensors_file


def _map_saved_lora_key(key: str) -> str:
    return (
        key.replace(".lora_A.weight", ".lora_A.default.weight")
        .replace(".lora_B.weight", ".lora_B.default.weight")
    )


def _load_initial_adapter_weights(model: torch.nn.Module, adapter_path: str) -> int:
    adapter_file = Path(adapter_path) / "adapter_model.safetensors"
    if not adapter_file.is_file():
        raise ValueError(f"init adapter missing adapter_model.safetensors: {adapter_path}")
    adapter_state = load_safetensors_file(str(adapter_file), device="cpu")
    model_keys = set(model.state_dict().keys())
    mapped_state: dict[str, torch.Tensor] = {}
    unmapped_keys: list[str] = []
    for key, tensor in adapter_state.items():
        mapped_key = key if key in model_keys else _map_saved_lora_key(key)
        if mapped_key not in model_keys:
            unmapped_keys.append(key)
            continue
        mapped_state[mapped_key] = tensor
    if unmapped_keys:
        preview = ", ".join(unmapped_keys[:5])
        raise ValueError(
            "init adapter contains keys that do not map to this model: "
            f"{preview}"
        )
    model.load_state_dict(mapped_state, strict=False)
    return len(mapped_state)
```

Replace the unconditional PEFT setup with:

```python
model = get_peft_model(
    model,
    LoraConfig(
        r=args.lora_r,
        lora_alpha=args.lora_alpha,
        lora_dropout=0.0,
        bias="none",
        task_type="CAUSAL_LM",
        target_modules=["q_proj", "k_proj", "v_proj", "o_proj"],
    )
)
loaded_adapter_tensors = 0
if args.init_adapter_path:
    loaded_adapter_tensors = _load_initial_adapter_weights(
        model,
        args.init_adapter_path,
    )
```

- [ ] **Step 4: Record resume metadata**

Add the path to `trainer_diagnostics.json`:

```python
"init_adapter_path": args.init_adapter_path,
"init_adapter_loaded_tensors": loaded_adapter_tensors,
```

- [ ] **Step 5: Verify the script parses**

Run:

```bash
/root/evolab-vllm/bin/python /tmp/qwen36_lora_sft_resume.py --help
```

Expected: help output includes `--init-adapter-path`.

### Task 2: Build A V4 Replay-And-Finish Dataset

**Files:**
- Existing v2 dataset: `/tmp/tb21-parametric-memory-recipe-framework-20260702-195641/artifacts/workers/job_4f635c98173f4a3a/parametric_memory_lora_sft/training.jsonl`
- New temporary dataset: `/tmp/tb21-parametric-memory-continued-v4-<timestamp>/training.jsonl`

- [ ] **Step 1: Read v2 records**

Load every JSONL record from the v2 training file. Preserve the record objects exactly.

- [ ] **Step 2: Keep fast-command replay dominant**

Select records by `metadata.projection_stage`:

```python
{
    "read_task": "keep all",
    "fast_exec_after_read": "keep all",
    "correct_drift_to_fast_exec": "keep all",
    "run_tests_after_grep_context": "keep all",
}
```

- [ ] **Step 3: Add small synthetic finish records**

Use the v3 training file only as a source for finish-boundary records:

```python
allowed_finish_stages = {
    "collect_after_synthetic_tests",
    "finish_after_synthetic_collect",
}
max_per_finish_stage = 16
```

- [ ] **Step 4: Write the combined JSONL**

Write the combined records in this order:

1. v2 records.
2. capped `collect_after_synthetic_tests` records.
3. capped `finish_after_synthetic_collect` records.
4. v2 `fast_exec_after_read` records again as replay tail.

Expected distribution is roughly fast/replay-heavy, not finish-heavy.

- [ ] **Step 5: Print stage distribution**

Print only counts by stage and output file path. Do not print command strings or recovered values.

### Task 3: Train V4 From V2 Adapter

**Files:**
- Read-only init adapter: `/tmp/tb21-parametric-memory-recipe-framework-20260702-195641/artifacts/workers/job_4f635c98173f4a3a/parametric_memory_lora_sft/adapter`
- Output adapter: `/tmp/tb21-parametric-memory-continued-v4-<timestamp>/adapter`

- [ ] **Step 1: Check GPUs 4-7 are free**

Run:

```bash
nvidia-smi --query-gpu=index,memory.used,utilization.gpu --format=csv,noheader,nounits
```

Expected: GPUs 4, 5, 6, and 7 have no large active allocation.

- [ ] **Step 2: Run the resume trainer**

Run:

```bash
env CUDA_VISIBLE_DEVICES=4,5,6,7 \
  /root/evolab-vllm/bin/python /tmp/qwen36_lora_sft_resume.py \
  --train-file /tmp/tb21-parametric-memory-continued-v4-<timestamp>/training.jsonl \
  --output-dir /tmp/tb21-parametric-memory-continued-v4-<timestamp>/adapter \
  --init-adapter-path /tmp/tb21-parametric-memory-recipe-framework-20260702-195641/artifacts/workers/job_4f635c98173f4a3a/parametric_memory_lora_sft/adapter \
  --max-records 0 \
  --epochs 1 \
  --max-steps 80 \
  --max-length 2048 \
  --lora-r 8 \
  --lora-alpha 32 \
  --lr 5e-5
```

Expected: `adapter_config.json`, `adapter_model.safetensors`, tokenizer files, and `trainer_diagnostics.json` exist under the output adapter path.

- [ ] **Step 3: Verify diagnostics**

Read `trainer_diagnostics.json` and report:

```json
{
  "record_count": "...",
  "trained_steps": 80,
  "init_adapter_path": "...",
  "init_adapter_loaded_tensors": 80,
  "loss_tail": "last five losses"
}
```

### Task 4: Evaluate V4 With The Local Parametric Runner

**Files:**
- Output summary: `/tmp/tb21-parametric-memory-continued-v4-<timestamp>/local-eval-successguard/summary.json`

- [ ] **Step 1: Run deterministic baseline vs treatment eval**

Run:

```bash
uv run polar-evolution terminal-bench-local-parametric-memory-eval \
  --task-root /root/datasets/terminal-bench-2-1/tasks \
  --task-id password-recovery \
  --run-root /tmp/tb21-parametric-memory-continued-v4-<timestamp>/local-eval-successguard \
  --terminal-bench-package-root /root/.config/superpowers/worktrees/EvoLabCore-terminal-bench-direct-solver \
  --model Qwen/Qwen3.6-35B-A3B \
  --adapter-path /tmp/tb21-parametric-memory-continued-v4-<timestamp>/adapter \
  --adapter-id tb-parametric-memory-password-continued-v4 \
  --adapter-artifact-id local-continued-v4 \
  --adapter-key-rewrite qwen3_5_moe_vllm_language_model \
  --server-port 8000 \
  --vllm-executable /root/evolab-vllm/bin/vllm \
  --gpu 4 --gpu 5 --gpu 6 --gpu 7 \
  --n-attempts 1 \
  --max-output-tokens 1536 \
  --context-window-tokens 16384 \
  --context-reserve-tokens 1536 \
  --solver-temperature 0.0 \
  --vllm-generation-config vllm \
  --tool-result-prompt-max-chars 8000 \
  --manage-server \
  --server-timeout-seconds 900 \
  --verifier-env NO_PROXY=localhost,127.0.0.1,::1,172.17.0.8 \
  --verifier-env no_proxy=localhost,127.0.0.1,::1,172.17.0.8 \
  --verifier-env UV_DOWNLOAD_URL=http://172.17.0.8:8765 \
  --verifier-env UV_FIND_LINKS=http://172.17.0.8:8765/wheels \
  --verifier-env UV_NO_INDEX=1 \
  --verifier-python-install-mirror http://172.17.0.8:8765/python-build-standalone \
  --agent-env EVOLAB_TB_DIRECT_SOLVER_COMPLETION_GUARD=successful_auto_tested_exec \
  --agent-env 'EVOLAB_TB_TEST_COMMAND=test -s /app/recovered_passwords.txt' \
  --output /tmp/tb21-parametric-memory-continued-v4-<timestamp>/local-eval-successguard/summary.json
```

Expected: summary contains baseline and parametric-memory conditions, both using deterministic solver temperature and `vllm` generation config.

- [ ] **Step 2: Summarize result**

Report:

```json
{
  "baseline_pass_at_1": "...",
  "parametric_pass_at_1": "...",
  "delta_pass_at_1": "...",
  "parametric_exception": "...",
  "tool_sequence": "tool names only"
}
```

Do not print recovered passwords or command output.

### Task 5: Record The Experiment

**Files:**
- Modify: `docs/dev/terminal-bench-memory-eval.md`

- [ ] **Step 1: Add a concise v4 paragraph**

Record run root, adapter path, training distribution, eval result, and conclusion.

- [ ] **Step 2: Run verification**

Run:

```bash
uv run pytest tests/evolution/test_terminal_bench_local_parametric.py -q
uv run pytest tests/evolution/test_worker_methods.py -k "parametric_memory_lora_sft or parametric_memory_register" -q
uv run ruff check src/polar_evolution/methods.py tests/evolution/test_worker_methods.py tests/evolution/test_terminal_bench_local_parametric.py
git diff --check
```

Expected: all commands pass.

- [ ] **Step 3: Commit and update PR**

Commit only the plan/doc changes if any repo files changed:

```bash
git status --short --branch
git add docs/superpowers/plans/2026-07-03-parametric-memory-continued-lora-v4.md docs/dev/terminal-bench-memory-eval.md
git commit -m "docs: record continued parametric memory experiment"
git push openevo codex/parametric-memory-local-eval
```

Post a PR comment with result and verification evidence.
