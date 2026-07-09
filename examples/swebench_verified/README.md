# SWE-bench Verified Example

Evaluate OpenEvo agent harnesses on [SWE-bench Verified](https://huggingface.co/datasets/princeton-nlp/SWE-bench_Verified)
(500 human-validated tasks). Each task runs an agent inside a per-instance
container at the repo's `base_commit`, then grades the patch with the official
`swebench` harness.

## Prerequisites

Install **OpenEvo** + the SWE-bench extra and **vLLM** as described in the
[top-level README](../../README.md#installation):

```bash
uv pip install -e ".[swebench]"
```

This example assumes 1 node **8×B200** — two vLLM servers (tensor-parallel 4 each).

## Quick Start

### 1. Build runtime images

Each runtime image layers Node.js on the per-instance SWE-bench image; harness
CLIs install at task time during the **INIT** stage. Build a subset first:

```bash
uv run python examples/swebench_verified/build_images.py --max-tasks 10   # or no flag for all 500
```

### 2. Start two vLLM servers

```bash
CUDA_VISIBLE_DEVICES=0,1,2,3 vllm serve Qwen/Qwen3.6-27B --port 8000 \
  --tensor-parallel-size 4 --max-model-len 262144 \
  --reasoning-parser qwen3 --enable-auto-tool-choice --tool-call-parser qwen3_coder

CUDA_VISIBLE_DEVICES=4,5,6,7 vllm serve Qwen/Qwen3.6-27B --port 8001 \
  --tensor-parallel-size 4 --max-model-len 262144 \
  --reasoning-parser qwen3 --enable-auto-tool-choice --tool-call-parser qwen3_coder
```

### 3. Start OpenEvo Core Servers

```bash
uv run python -m openevo.rollout.server --config examples/swebench_verified/topology.yaml
uv run python -m openevo.gateway.server --config examples/swebench_verified/topology.yaml --node-id localhost-node-01
uv run python -m openevo.gateway.server --config examples/swebench_verified/topology.yaml --node-id localhost-node-02
```

### 4. Submit tasks

Pick a harness and how many tasks to run; the resolved-rate summary prints to
the console when the batch finishes. Supported harnesses: `claude_code`, `codex`, `opencode`, `qwen_code`.


```bash
# pass@1 over the first 10 tasks
uv run python examples/swebench_verified/submit_swebench_tasks.py --harness claude_code --max-tasks 10

# pass@8 over the first 10 tasks
uv run python examples/swebench_verified/submit_swebench_tasks.py --harness claude_code --max-tasks 10 --num-samples 8

# a single instance
uv run python examples/swebench_verified/submit_swebench_tasks.py --harness codex --instance-id django__django-15098
```

Use Apptainer instead of Docker with `--runtime-backend apptainer`.

### 5. (Optional) Inspect the services

```bash
curl http://127.0.0.1:8080/health
```

Use the rollout and gateway logs for per-task patches, trajectories, and grading.
