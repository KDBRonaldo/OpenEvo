# Calculator Example

This is a small end-to-end Polar rollout example. Each agent gets a tiny
`calculator.py` file with parser stubs, edits it, and the evaluator runs
`python3 test_calculator.py`.

Use this example when you want a quick local check that rollout, gateway,
runtime setup, harness execution, and evaluation still work together.

The topology setup is used on 4 x B200 GPUs. Adjust based on your hardware.

## What It Runs

- rollout server on `:8080`
- two gateway nodes on `:8100` and `:8101`
- two local SGLang backends on `:8000` and `:8001`
- one shared runtime image: `polar-localhost-calculator:latest`
- six harnesses: `claude_code`, `codex`, `gemini_cli`, `opencode`, `pi`,
  `qwen_code`

The default scripts use Docker. Apptainer is also supported with
`--backend apptainer`.

## Setup

From the repo root:

```bash
uv venv
uv pip install -e .
uv pip install --prerelease=allow sglang==0.5.10
bash scripts/patch/patch_sglang.sh
```

Build the runtime image once:

```bash
uv run python examples/calculator/build_image.py
```

## Start Services

Start two SGLang servers, one per GPU group:

```bash
CUDA_VISIBLE_DEVICES=0 uv run python -m sglang.launch_server \
  --model-path Qwen/Qwen3.5-4B \
  --host 0.0.0.0 \
  --port 8000 \
  --tool-call-parser qwen3_coder \
  --reasoning-parser qwen3 \
  --mem-fraction-static 0.7 \
  --context-length 262144 \
  --trust-remote-code
```

```bash
CUDA_VISIBLE_DEVICES=1 uv run python -m sglang.launch_server \
  --model-path Qwen/Qwen3.5-4B \
  --host 0.0.0.0 \
  --port 8001 \
  --tool-call-parser qwen3_coder \
  --reasoning-parser qwen3 \
  --mem-fraction-static 0.7 \
  --context-length 262144 \
  --trust-remote-code
```

Start Polar:

```bash
uv run polar serve_rollout -c examples/calculator/topology.yaml
```

```bash
uv run polar serve_gateway -c examples/calculator/topology.yaml --node-id localhost-node-01
```

```bash
uv run polar serve_gateway -c examples/calculator/topology.yaml --node-id localhost-node-02
```

## Run

Run every harness:

```bash
uv run python examples/calculator/submit_all.py
```

Run one harness:

```bash
uv run python examples/calculator/submit_calculator_task.py claude_code
```

Use Apptainer instead of Docker:

```bash
uv run python examples/calculator/submit_all.py --backend apptainer
```

Results are written under:

```text
examples/calculator/batches/<timestamp>/
```

Each harness directory contains `request.json`, `response.json`, and
`summary.json`.
