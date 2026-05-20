# Count Stars Example

This is a small Polar rollout example with an image file in the task workspace.
Each supported harness gets the same file at
`/polar/session/workspace/polar_stars.png` and is asked to count the visible
stars.

Use this example to check that coding-agent harnesses can work from an image
path in the runtime workspace through the local SGLang OpenAI-compatible
backend.

The topology and model setup match the calculator example.

## What It Runs

- rollout server on `:8080`
- two gateway nodes on `:8100` and `:8101`
- two local SGLang backends on `:8000` and `:8001`
- one shared runtime image: `polar-localhost-count-stars:latest`
- three harnesses: `claude_code`, `codex`, `gemini_cli`
- evaluator: `session_completed`

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
uv run python examples/count_stars/build_image.py
```

## Start Services

Start two SGLang servers, one per GPU:

Use the Qwen tool-call parser so `<tool_call>` responses are returned as
structured tool calls instead of plain assistant text.

```bash
CUDA_VISIBLE_DEVICES=0 uv run python -m sglang.launch_server \
  --model-path Qwen/Qwen3.6-27B \
  --host 127.0.0.1 \
  --port 8000 \
  --context-length 262144 \
  --tool-call-parser qwen3_coder \
  --reasoning-parser qwen3 \
  --trust-remote-code
```

```bash
CUDA_VISIBLE_DEVICES=1 uv run python -m sglang.launch_server \
  --model-path Qwen/Qwen3.6-27B \
  --host 127.0.0.1 \
  --port 8001 \
  --context-length 262144 \
  --tool-call-parser qwen3_coder \
  --reasoning-parser qwen3 \
  --trust-remote-code
```

If Hugging Face download access is rate-limited, set `HF_TOKEN` before starting
SGLang.

Start Polar:

```bash
uv run polar serve_rollout -c examples/count_stars/topology.yaml
```

```bash
uv run polar serve_gateway -c examples/count_stars/topology.yaml --node-id localhost-node-01
```

```bash
uv run polar serve_gateway -c examples/count_stars/topology.yaml --node-id localhost-node-02
```

## Run

Run every harness:

```bash
uv run python examples/count_stars/submit_all.py
```

Run one harness:

```bash
uv run python examples/count_stars/submit_count_stars_task.py codex
```
