# SWE-bench Verified Example

Evaluate Polar agent harnesses on the full [SWE-bench Verified](https://huggingface.co/datasets/princeton-nlp/SWE-bench_Verified) benchmark (500 human-validated tasks).

Each task runs an agent inside a per-instance container with the repo at `base_commit`, then grades the resulting patch via `swebench.harness.grading`.

The topology setup is used on 4 x B200 GPUs. Adjust based on your hardware.

## Installation

```bash
uv venv
uv pip install -e ".[swebench]"
uv pip install --prerelease=allow sglang==0.5.10
bash scripts/patch/patch_sglang.sh
```

## Quick Start

### 1. Start SGLang backends

```bash
CUDA_VISIBLE_DEVICES=0,1 uv run python -m sglang.launch_server \
   --model-path Qwen/Qwen3.5-4B \
   --host 0.0.0.0 \
   --port 8000 \
   --tp-size 2 \
   --tool-call-parser qwen3_coder \
   --reasoning-parser qwen3 \
   --mem-fraction-static 0.7 \
   --context-length 262144 \
   --trust-remote-code

CUDA_VISIBLE_DEVICES=2,3 uv run python -m sglang.launch_server \
   --model-path Qwen/Qwen3.5-4B \
   --host 0.0.0.0 \
   --port 8001 \
   --tp-size 2 \
   --tool-call-parser qwen3_coder \
   --reasoning-parser qwen3 \
   --mem-fraction-static 0.7 \
   --context-length 262144 \
   --trust-remote-code
```

### 2. Start Polar services

```bash
uv run polar serve_rollout -c examples/swebench_verified/topology.yaml
uv run polar serve_gateway -c examples/swebench_verified/topology.yaml --node-id localhost-node-01
uv run polar serve_gateway -c examples/swebench_verified/topology.yaml --node-id localhost-node-02
```

### 3. Build runtime images

```bash
# Build all 500
uv run python examples/swebench_verified/build_images.py

# Or build a subset
uv run python examples/swebench_verified/build_images.py --max-tasks 10
```

### 4. Submit tasks

```bash
# Run all 500 tasks for pass@1
uv run python examples/swebench_verified/submit_swebench_tasks.py \
  --harness claude_code \
  --topology examples/swebench_verified/topology.yaml \
  --runtime-backend docker \
  --num-samples 1 \
  --max-tasks 10

# pass@8 for first 10 tasks
uv run python examples/swebench_verified/submit_swebench_tasks.py \
  --harness claude_code \
  --topology examples/swebench_verified/topology.yaml \
  --runtime-backend docker \
  --num-samples 8 \
  --max-tasks 10
```
