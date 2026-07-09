# Local Parametric Memory Evaluation Design

Issue: #31
Date: 2026-07-01
Status: design option A approved

## Goal

Evaluate `parametric_memory` on Terminal Bench 2.1 with local inference, using only
parametric memory as the evolving artifact. The controlled comparison is:

- baseline: local Qwen inference without a LoRA adapter;
- treatment: the same local Qwen inference endpoint with a LoRA adapter registered
  and selected as `parametric_memory`;
- disabled during both runs: `text_memory`, `skill_bundle`, and `agent_system`.

The first run targets a small reproducible task subset before expanding to the full
benchmark:

- `train-fasttext`
- `query-optimize`
- `make-mips-interpreter`

## Non-Goals

- Do not use Codex subscription for parametric memory evaluation. Subscription
  modes can use textual memory, but parametric memory requires local/proxy
  inference so that an adapter can affect model responses.
- Do not run the full Terminal Bench 2.1 suite until the local server, adapter
  selection, and result aggregation are verified on the controlled subset.
- Do not use GPU 5 for this experiment because it currently shows unexplained
  memory and utilization state.
- Do not physically merge adapter weights inside the Polar gateway. The serving
  backend must load or expose the adapter.

## Selected Model And Runtime

Use the complete local cache for:

```text
Qwen/Qwen3.6-35B-A3B
```

The model cache is present under:

```text
/root/.cache/huggingface/hub/models--Qwen--Qwen3.6-35B-A3B
```

Use the local vLLM installation:

```text
/root/evolab-vllm/bin/vllm
```

Use GPUs:

```text
CUDA_VISIBLE_DEVICES=1,2,3,4
```

The baseline server command is expected to be:

```bash
CUDA_VISIBLE_DEVICES=1,2,3,4 /root/evolab-vllm/bin/vllm serve Qwen/Qwen3.6-35B-A3B \
  --host 127.0.0.1 \
  --port 8000 \
  --served-model-name Qwen/Qwen3.6-35B-A3B \
  --tensor-parallel-size 4 \
  --max-model-len 16384 \
  --gpu-memory-utilization 0.75 \
  --dtype bfloat16 \
  --reasoning-parser qwen3 \
  --language-model-only \
  --enforce-eager \
  --enable-auto-tool-choice \
  --tool-call-parser qwen3_xml
```

The adapter server command adds static LoRA exposure:

```bash
CUDA_VISIBLE_DEVICES=1,2,3,4 /root/evolab-vllm/bin/vllm serve Qwen/Qwen3.6-35B-A3B \
  --host 127.0.0.1 \
  --port 8000 \
  --served-model-name Qwen/Qwen3.6-35B-A3B \
  --tensor-parallel-size 4 \
  --max-model-len 16384 \
  --gpu-memory-utilization 0.75 \
  --dtype bfloat16 \
  --reasoning-parser qwen3 \
  --language-model-only \
  --enforce-eager \
  --enable-auto-tool-choice \
  --tool-call-parser qwen3_xml \
  --enable-lora \
  --max-loras 1 \
  --max-lora-rank 64 \
  --lora-modules tb-parametric-memory=/path/to/adapter
```

Before any Terminal Bench run, the orchestrator must verify:

- `/v1/models` is reachable;
- the expected base or adapter model name is listed;
- a minimal chat completion succeeds;
- the server PID, command, port, model name, GPU list, and log path are recorded.

For LoRA runs, requests should use the adapter model name exposed by vLLM:

```text
tb-parametric-memory
```

The exact vLLM LoRA request semantics must be verified by the smoke check before
launching benchmark tasks.

## Terminal Bench Harness Path

The existing Terminal Bench per-task evolution command is subscription-oriented:
it invokes Harbor with `mode=codex_subscription`, rejects `parametric_memory` as a
single live artifact type, and skips materializing `parametric_memory` artifacts.
The parametric-memory experiment needs a separate local/proxy path rather than a
small flag flip on that runner.

Use Harbor's EvoLab mode:

```text
--ak mode=evolab
```

The EvoLab runner supports OpenAI-compatible chat completions. The Harbor process
environment must include:

```text
EVOLAB_TB_LLM_API=openai-chat-completions
EVOLAB_TB_MODEL=<base-or-adapter-model-name>
OPENAI_BASE_URL=http://127.0.0.1:8000/v1
AIGOCODE_GPT_BASE_URL=http://127.0.0.1:8000/v1
OPENAI_API_KEY=dummy-local-key
```

This must be passed as the subprocess environment for the Harbor agent process.
`env_json` alone is not sufficient for `mode=evolab`, because the current
Terminal Bench package reads these values from `os.environ` inside the EvoLab
runner path.

## Adapter Artifact Flow

Use the existing `parametric_memory_lora_sft` method as the artifact boundary:

1. Build a dataset artifact from selected successful text trajectories.
2. Run a local trainer command that writes a PEFT-compatible adapter directory.
3. Register an `ArtifactRegisterRequest(type="parametric_memory")` with:
   - `manifest.adapter_id`;
   - `manifest.base_model="Qwen/Qwen3.6-35B-A3B"`;
   - `manifest.adapter_format`;
   - the trainer command metadata;
   - dataset lineage;
   - scores such as `quality` and observed evaluation deltas when available.
4. Resolve only this artifact type into the local evaluation run.
5. Start vLLM with `--lora-modules` pointing at the resolved adapter directory.

The first controlled experiment may use successful trajectories from previous
Terminal Bench runs as the memory source. This measures whether parametric memory
can distill reusable task-solving behavior into the local model. Results must
clearly distinguish same-task rescue from held-out task generalization.

## Experiment Matrix

Run the following matrix on the three-task subset:

```text
baseline local Qwen, no adapter
parametric memory local Qwen, adapter selected
```

For each cell, record:

- task id;
- attempt id and seed if present;
- model name requested by EvoLab;
- vLLM server metadata;
- artifact ids selected by context resolution;
- `parametric_memory` adapter id and path for treatment runs;
- disabled artifact types;
- pass/fail result;
- score fields emitted by Terminal Bench;
- transcript and logs needed to reproduce failures.

The summary must report pass rate by task and aggregate pass rate for baseline
and treatment. Any delta must be labeled as measured on the controlled subset,
not as a full Terminal Bench 2.1 result.

## Orchestration Requirements

Add a local parametric-memory evaluation entry point rather than changing the
subscription runner semantics. The entry point should:

- accept task ids, attempts, model name, server port, GPU list, adapter artifact
  id or adapter path, output root, and timeout settings;
- start and stop vLLM in its own process group when `--manage-server` is set;
- also support `--server-url` for an already running endpoint;
- write server logs and metadata under the run output root;
- pass local inference environment variables directly to the Harbor subprocess;
- refuse to enable `parametric_memory` unless the selected mode is local/proxy;
- refuse to use subscription auth for parametric memory;
- keep `text_memory`, `skill_bundle`, and `agent_system` disabled unless
  explicitly requested in a later experiment;
- clean up the server process group on normal completion and on failure.

The implementation should keep research method logic out of gateway and store
layers. The new runner should use existing artifact contracts and context
resolution wherever possible.

## Validation Plan

Code changes following this design should add focused tests for:

- local Harbor command construction with `mode=evolab`;
- subprocess environment propagation for `OPENAI_BASE_URL`,
  `EVOLAB_TB_LLM_API`, and `EVOLAB_TB_MODEL`;
- rejection of subscription mode when `parametric_memory` is enabled;
- materialization or direct selection of `parametric_memory` adapter paths;
- server lifecycle metadata and cleanup behavior with a fake server process;
- summary fields showing enabled and disabled artifact types.

Manual preflight before benchmark execution:

```bash
/root/evolab-vllm/bin/vllm --version
nvidia-smi
curl http://127.0.0.1:8000/v1/models
```

Repository verification before PR:

```bash
pytest tests/evolution -q
pytest tests/gateway/test_evolution_integration.py -q
git diff --check
```

Run narrower tests if the final implementation touches only the new local
Terminal Bench runner and associated artifact utilities.

## Failure Handling

If vLLM fails to start, preserve:

- server stdout and stderr logs;
- command and environment metadata with secrets redacted;
- GPU state before and after the attempt;
- timeout duration and health-check errors.

If a benchmark task fails because the local model cannot use tools or emits
invalid protocol messages, preserve the transcript and classify it separately
from task-solution failure.

If LoRA loading fails, stop before Terminal Bench execution and report adapter
path, adapter config metadata, base model compatibility, and the vLLM error log.

## Expected Deliverables

- A committed design spec for the local parametric-memory evaluation path.
- An implementation plan after spec review.
- A PR linked to issue #31 containing code, tests, and docs.
- A controlled Terminal Bench 2.1 subset report comparing baseline local Qwen
  and parametric-memory local Qwen.
